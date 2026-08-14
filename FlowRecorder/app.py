import os
import sys
import time
import wave
import queue
import shutil
import threading
import subprocess
from pathlib import Path

import numpy as np
import sounddevice as sd
from mss import mss
from PySide6 import QtCore, QtGui, QtWidgets

APP_NAME = "FlowRecorder"
FPS_OPTIONS = [15, 24, 30, 60]


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def ffmpeg_path() -> str:
    bundled = app_dir() / "ffmpeg.exe"
    if bundled.exists():
        return str(bundled)
    found = shutil.which("ffmpeg")
    return found or "ffmpeg"


class Recorder:
    def __init__(self, monitor, fps, output_dir, mic_enabled=True):
        self.monitor = monitor
        self.fps = fps
        self.output_dir = Path(output_dir)
        self.mic_enabled = mic_enabled
        self.stop_event = threading.Event()
        self.video_thread = None
        self.audio_thread = None
        self.audio_queue = queue.Queue(maxsize=150)
        self.audio_error = None
        self.video_error = None
        self.temp_video = None
        self.temp_wav = None
        self.final_file = None
        self._audio_stream = None
        self._wav = None

    def start(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        base = self.output_dir / f"FlowRecorder_{stamp}"
        self.temp_video = base.with_suffix(".video.mp4")
        self.temp_wav = base.with_suffix(".audio.wav")
        self.final_file = base.with_suffix(".mp4")
        self.stop_event.clear()

        self.video_thread = threading.Thread(target=self._record_video, daemon=True)
        self.video_thread.start()
        if self.mic_enabled:
            self.audio_thread = threading.Thread(target=self._record_audio, daemon=True)
            self.audio_thread.start()

    def _record_video(self):
        width = int(self.monitor["width"])
        height = int(self.monitor["height"])
        cmd = [
            ffmpeg_path(), "-y", "-f", "rawvideo", "-pix_fmt", "bgra",
            "-video_size", f"{width}x{height}", "-framerate", str(self.fps),
            "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-pix_fmt", "yuv420p", str(self.temp_video),
        ]
        try:
            process = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            with mss() as sct:
                frame_time = 1.0 / self.fps
                next_frame = time.perf_counter()
                while not self.stop_event.is_set():
                    frame = np.asarray(sct.grab(self.monitor), dtype=np.uint8)
                    process.stdin.write(frame.tobytes())
                    next_frame += frame_time
                    delay = next_frame - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                    elif delay < -frame_time * 2:
                        next_frame = time.perf_counter()
            try:
                process.stdin.close()
            except Exception:
                pass
            stderr = process.stderr.read().decode("utf-8", errors="replace")
            code = process.wait()
            if code != 0:
                self.video_error = stderr[-3000:] or f"FFmpeg terminó con código {code}."
        except Exception as exc:
            self.video_error = str(exc)

    def _audio_callback(self, indata, frames, callback_time, status):
        if status:
            self.audio_error = str(status)
        try:
            self.audio_queue.put_nowait(indata.copy())
        except queue.Full:
            pass

    def _record_audio(self):
        sample_rate = 48000
        channels = 2
        try:
            self._wav = wave.open(str(self.temp_wav), "wb")
            self._wav.setnchannels(channels)
            self._wav.setsampwidth(2)
            self._wav.setframerate(sample_rate)
            self._audio_stream = sd.InputStream(
                samplerate=sample_rate, channels=channels, dtype="int16",
                callback=self._audio_callback, blocksize=1024,
            )
            self._audio_stream.start()
            while not self.stop_event.is_set() or not self.audio_queue.empty():
                try:
                    chunk = self.audio_queue.get(timeout=0.2)
                    self._wav.writeframes(chunk.astype(np.int16).tobytes())
                except queue.Empty:
                    continue
        except Exception as exc:
            self.audio_error = str(exc)
        finally:
            try:
                if self._audio_stream is not None:
                    self._audio_stream.stop()
                    self._audio_stream.close()
            except Exception:
                pass
            try:
                if self._wav is not None:
                    self._wav.close()
            except Exception:
                pass

    def stop(self):
        self.stop_event.set()
        if self.video_thread:
            self.video_thread.join(timeout=30)
        if self.audio_thread:
            self.audio_thread.join(timeout=10)

        if self.video_error:
            raise RuntimeError(f"No se pudo grabar el vídeo:\n\n{self.video_error}")
        if not self.temp_video or not self.temp_video.exists():
            raise RuntimeError("FFmpeg no creó el archivo de vídeo.")
        if self.mic_enabled and self.audio_error:
            raise RuntimeError(f"No se pudo grabar el micrófono:\n\n{self.audio_error}")

        if self.mic_enabled and self.temp_wav and self.temp_wav.exists():
            cmd = [ffmpeg_path(), "-y", "-i", str(self.temp_video), "-i", str(self.temp_wav),
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                   str(self.final_file)]
        else:
            cmd = [ffmpeg_path(), "-y", "-i", str(self.temp_video), "-c", "copy", str(self.final_file)]

        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError("No se pudo unir vídeo y audio:\n\n" + result.stderr[-3000:])

        for temp in (self.temp_video, self.temp_wav):
            try:
                if temp and temp.exists():
                    temp.unlink()
            except Exception:
                pass
        return self.final_file


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1320, 820)
        self.setMinimumSize(1080, 700)
        self.recorder = None
        self.recording = False
        self.started_at = None

        self.preview_timer = QtCore.QTimer(self)
        self.preview_timer.timeout.connect(self.update_preview)
        self.clock_timer = QtCore.QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)

        self.build_ui()
        self.refresh_monitors()
        self.preview_timer.start(150)

    def build_ui(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #0b0f14; color: #edf2f7; font-family: Segoe UI; }
            QFrame#sidebar { background: #10161e; border-right: 1px solid #202936; }
            QLabel#brand { font-size: 22px; font-weight: 800; letter-spacing: 1px; }
            QLabel#brand2 { color: #7f8da1; font-size: 11px; }
            QPushButton#nav { text-align: left; background: transparent; border: 0; border-radius: 10px; padding: 12px 14px; color: #9daabd; font-size: 13px; }
            QPushButton#nav:hover, QPushButton#nav[active="true"] { background: #19222e; color: #ffffff; }
            QFrame#topbar { background: #0f141b; border-bottom: 1px solid #202936; }
            QLabel#pageTitle { font-size: 20px; font-weight: 700; }
            QLabel#status { color: #8997aa; }
            QFrame#card { background: #111821; border: 1px solid #202b38; border-radius: 14px; }
            QLabel#cardTitle { color: #a9b5c5; font-size: 12px; font-weight: 600; }
            QLabel#bigValue { font-size: 22px; font-weight: 750; }
            QLabel#preview { background: #05070a; border: 1px solid #263241; border-radius: 12px; }
            QComboBox, QLineEdit { background: #0d131b; border: 1px solid #293544; border-radius: 9px; padding: 9px 10px; color: #e8edf3; }
            QComboBox:hover, QLineEdit:focus { border: 1px solid #4a6079; }
            QPushButton { background: #18212c; border: 1px solid #2b3948; border-radius: 9px; padding: 9px 13px; color: #e8edf3; }
            QPushButton:hover { background: #202c3a; }
            QPushButton#record { background: #e9434a; border: 0; border-radius: 11px; padding: 13px 26px; font-size: 14px; font-weight: 800; }
            QPushButton#record:hover { background: #f25258; }
            QPushButton#record:disabled { background: #4c2629; color: #a98b8d; }
            QPushButton#stop { background: #18212c; border-radius: 11px; padding: 13px 22px; font-weight: 700; }
            QPushButton#stop:disabled { color: #526071; }
            QCheckBox { spacing: 8px; color: #d8e0e9; }
            QCheckBox::indicator { width: 18px; height: 18px; }
            QGroupBox { background: #111821; border: 1px solid #202b38; border-radius: 14px; margin-top: 10px; padding: 14px; }
            QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; color: #9ba8b9; background: #111821; }
            QProgressBar { background: #0b1016; border: 0; border-radius: 4px; height: 7px; }
            QProgressBar::chunk { background: #e9434a; border-radius: 4px; }
            QScrollArea { border: 0; }
        """)

        central = QtWidgets.QWidget()
        main = QtWidgets.QHBoxLayout(central)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        self.setCentralWidget(central)

        sidebar = QtWidgets.QFrame(objectName="sidebar")
        sidebar.setFixedWidth(205)
        side = QtWidgets.QVBoxLayout(sidebar)
        side.setContentsMargins(14, 20, 14, 18)
        side.setSpacing(6)

        brand = QtWidgets.QLabel("FLOWRECORDER", objectName="brand")
        brand2 = QtWidgets.QLabel("SCREEN STUDIO", objectName="brand2")
        side.addWidget(brand)
        side.addWidget(brand2)
        side.addSpacing(24)

        for text, active in [("▣  Estudio", True), ("●  Grabaciones", False), ("◉  Audio", False), ("⚙  Ajustes", False)]:
            b = QtWidgets.QPushButton(text, objectName="nav")
            b.setProperty("active", active)
            side.addWidget(b)
        side.addStretch()

        version = QtWidgets.QLabel("FlowRecorder 1.1\nConstruido para Windows")
        version.setStyleSheet("color:#667487; font-size:10px;")
        side.addWidget(version)
        main.addWidget(side)

        content = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(24, 18, 24, 20)
        content_layout.setSpacing(16)
        main.addWidget(content, 1)

        top = QtWidgets.QFrame(objectName="topbar")
        top.setFixedHeight(58)
        top_layout = QtWidgets.QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        title = QtWidgets.QLabel("Estudio", objectName="pageTitle")
        self.status_label = QtWidgets.QLabel("●  Listo para grabar", objectName="status")
        top_layout.addWidget(title)
        top_layout.addStretch()
        top_layout.addWidget(self.status_label)
        content_layout.addWidget(top)

        cards = QtWidgets.QHBoxLayout()
        cards.setSpacing(12)
        self.add_stat_card(cards, "MODO", "Pantalla", "▣")
        self.fps_card = self.add_stat_card(cards, "FPS", "30", "◌")
        self.timer_card = self.add_stat_card(cards, "DURACIÓN", "00:00:00", "◷")
        self.audio_card = self.add_stat_card(cards, "AUDIO", "Micrófono", "♫")
        content_layout.addLayout(cards)

        workspace = QtWidgets.QHBoxLayout()
        workspace.setSpacing(16)

        preview_card = QtWidgets.QFrame(objectName="card")
        preview_layout = QtWidgets.QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(14, 14, 14, 14)
        head = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel("VISTA PREVIA", objectName="cardTitle")
        self.live_badge = QtWidgets.QLabel("● LIVE")
        self.live_badge.setStyleSheet("color:#657386; font-size:10px; font-weight:700;")
        head.addWidget(label)
        head.addStretch()
        head.addWidget(self.live_badge)
        preview_layout.addLayout(head)
        self.preview = QtWidgets.QLabel("Preparando vista previa…", objectName="preview")
        self.preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(650, 420)
        preview_layout.addWidget(self.preview, 1)
        workspace.addWidget(preview_card, 1)

        panel = QtWidgets.QVBoxLayout()
        panel.setSpacing(12)
        self.add_capture_card(panel)
        self.add_audio_card(panel)
        self.add_output_card(panel)
        panel.addStretch()
        workspace.addLayout(panel, 0)
        content_layout.addLayout(workspace, 1)

        bottom = QtWidgets.QFrame(objectName="card")
        bottom_layout = QtWidgets.QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(14, 12, 14, 12)
        self.record_btn = QtWidgets.QPushButton("●  INICIAR GRABACIÓN", objectName="record")
        self.record_btn.clicked.connect(self.start_recording)
        self.stop_btn = QtWidgets.QPushButton("■  DETENER")
        self.stop_btn.setObjectName("stop")
        self.stop_btn.clicked.connect(self.stop_recording)
        self.stop_btn.setEnabled(False)
        self.bottom_timer = QtWidgets.QLabel("00:00:00", objectName="bigValue")
        bottom_layout.addWidget(self.record_btn)
        bottom_layout.addWidget(self.stop_btn)
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.bottom_timer)
        content_layout.addWidget(bottom)

    def add_stat_card(self, layout, title, value, icon):
        card = QtWidgets.QFrame(objectName="card")
        box = QtWidgets.QHBoxLayout(card)
        box.setContentsMargins(13, 10, 13, 10)
        ico = QtWidgets.QLabel(icon)
        ico.setStyleSheet("font-size:18px; color:#c4cfdb;")
        texts = QtWidgets.QVBoxLayout()
        t = QtWidgets.QLabel(title, objectName="cardTitle")
        v = QtWidgets.QLabel(value, objectName="bigValue")
        texts.addWidget(t)
        texts.addWidget(v)
        box.addWidget(ico)
        box.addLayout(texts)
        layout.addWidget(card, 1)
        return v

    def add_capture_card(self, layout):
        box = QtWidgets.QGroupBox("CAPTURA")
        form = QtWidgets.QFormLayout(box)
        form.setVerticalSpacing(10)
        self.monitor_combo = QtWidgets.QComboBox()
        self.fps_combo = QtWidgets.QComboBox()
        self.fps_combo.addItems([str(x) for x in FPS_OPTIONS])
        self.fps_combo.setCurrentText("30")
        self.fps_combo.currentTextChanged.connect(self.on_fps_changed)
        form.addRow("Pantalla", self.monitor_combo)
        form.addRow("Calidad", self.fps_combo)
        layout.addWidget(box)

    def add_audio_card(self, layout):
        box = QtWidgets.QGroupBox("AUDIO")
        v = QtWidgets.QVBoxLayout(box)
        self.mic_check = QtWidgets.QCheckBox("Grabar micrófono")
        self.mic_check.setChecked(True)
        self.mic_check.stateChanged.connect(self.on_mic_changed)
        v.addWidget(self.mic_check)
        hint = QtWidgets.QLabel("Audio del sistema: próximamente")
        hint.setStyleSheet("color:#68778a; font-size:10px;")
        v.addWidget(hint)
        layout.addWidget(box)

    def add_output_card(self, layout):
        box = QtWidgets.QGroupBox("DESTINO")
        v = QtWidgets.QVBoxLayout(box)
        row = QtWidgets.QHBoxLayout()
        self.output_edit = QtWidgets.QLineEdit(str(Path.home() / "Videos" / "FlowRecorder"))
        browse = QtWidgets.QPushButton("Elegir")
        browse.clicked.connect(self.choose_output)
        row.addWidget(self.output_edit)
        row.addWidget(browse)
        v.addLayout(row)
        self.file_label = QtWidgets.QLabel("MP4 • H.264 • AAC")
        self.file_label.setStyleSheet("color:#68778a; font-size:10px;")
        v.addWidget(self.file_label)
        layout.addWidget(box)

    def refresh_monitors(self):
        self.monitor_combo.clear()
        try:
            with mss() as sct:
                for i, monitor in enumerate(sct.monitors[1:], 1):
                    self.monitor_combo.addItem(
                        f"Pantalla {i}  ·  {monitor['width']}×{monitor['height']}", monitor
                    )
        except Exception as exc:
            self.monitor_combo.addItem("No se pudo detectar la pantalla")
        if self.monitor_combo.count() == 0:
            self.monitor_combo.addItem("No se detectó pantalla")

    def choose_output(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Seleccionar carpeta de grabaciones")
        if path:
            self.output_edit.setText(path)

    def on_fps_changed(self, value):
        if hasattr(self, "fps_card"):
            self.fps_card.setText(value)

    def on_mic_changed(self, state):
        if hasattr(self, "audio_card"):
            self.audio_card.setText("Micrófono" if state else "Sin audio")

    def update_preview(self):
        if self.recording:
            return
        try:
            monitor = self.monitor_combo.currentData()
            if not monitor:
                return
            with mss() as sct:
                shot = np.asarray(sct.grab(monitor))
            h, w, _ = shot.shape
            image = QtGui.QImage(shot.data, w, h, 4 * w, QtGui.QImage.Format.Format_ARGB32).copy()
            pix = QtGui.QPixmap.fromImage(image)
            self.preview.setPixmap(pix.scaled(
                self.preview.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            ))
        except Exception:
            pass

    def start_recording(self):
        if self.recording:
            return
        monitor = self.monitor_combo.currentData()
        if not monitor:
            QtWidgets.QMessageBox.warning(self, APP_NAME, "No se encontró una pantalla para capturar.")
            return
        output_dir = self.output_edit.text().strip()
        if not output_dir:
            QtWidgets.QMessageBox.warning(self, APP_NAME, "Selecciona una carpeta de salida.")
            return

        fps = int(self.fps_combo.currentText())
        self.recorder = Recorder(monitor, fps, output_dir, self.mic_check.isChecked())
        try:
            self.recorder.start()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, APP_NAME, str(exc))
            return

        self.recording = True
        self.started_at = time.monotonic()
        self.record_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.mic_check.setEnabled(False)
        self.monitor_combo.setEnabled(False)
        self.fps_combo.setEnabled(False)
        self.status_label.setText("●  GRABANDO")
        self.status_label.setStyleSheet("color:#ff5960; font-weight:800;")
        self.live_badge.setStyleSheet("color:#ff5960; font-size:10px; font-weight:800;")
        self.clock_timer.start(250)

    def stop_recording(self):
        if not self.recording or not self.recorder:
            return
        self.stop_btn.setEnabled(False)
        self.status_label.setText("●  GUARDANDO…")
        QtWidgets.QApplication.processEvents()
        try:
            final_file = self.recorder.stop()
            self.status_label.setText("●  Grabación guardada")
            self.file_label.setText(f"Guardado: {final_file.name}")
            QtWidgets.QMessageBox.information(self, APP_NAME, f"Grabación terminada.\n\n{final_file}")
        except Exception as exc:
            self.status_label.setText("●  Error")
            QtWidgets.QMessageBox.critical(self, APP_NAME, str(exc))
        finally:
            self.recording = False
            self.clock_timer.stop()
            self.record_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.mic_check.setEnabled(True)
            self.monitor_combo.setEnabled(True)
            self.fps_combo.setEnabled(True)
            self.live_badge.setStyleSheet("color:#657386; font-size:10px; font-weight:700;")

    def update_clock(self):
        if not self.started_at:
            return
        elapsed = int(time.monotonic() - self.started_at)
        text = time.strftime("%H:%M:%S", time.gmtime(elapsed))
        self.bottom_timer.setText(text)
        self.timer_card.setText(text)

    def closeEvent(self, event):
        if self.recording:
            answer = QtWidgets.QMessageBox.question(
                self, APP_NAME,
                "Hay una grabación activa. ¿Quieres detenerla antes de salir?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                self.stop_recording()
            else:
                event.ignore()
                return
        event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
