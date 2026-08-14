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
    if found:
        return found
    return "ffmpeg"


class Recorder:
    def __init__(self, monitor, fps, output_dir, mic_enabled=True):
        self.monitor = monitor
        self.fps = fps
        self.output_dir = Path(output_dir)
        self.mic_enabled = mic_enabled
        self.stop_event = threading.Event()
        self.video_thread = None
        self.audio_thread = None
        self.audio_queue = queue.Queue(maxsize=100)
        self.audio_error = None
        self.video_error = None
        self.started_at = None
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
        self.started_at = time.monotonic()
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
            ffmpeg_path(), "-y",
            "-f", "rawvideo",
            "-pix_fmt", "bgra",
            "-video_size", f"{width}x{height}",
            "-framerate", str(self.fps),
            "-i", "-",
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            str(self.temp_video),
        ]
        try:
            process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
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
                samplerate=sample_rate,
                channels=channels,
                dtype="int16",
                callback=self._audio_callback,
                blocksize=1024,
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
            cmd = [
                ffmpeg_path(), "-y",
                "-i", str(self.temp_video),
                "-i", str(self.temp_wav),
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                str(self.final_file),
            ]
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
        self.resize(1180, 760)
        self.setMinimumSize(980, 650)
        self.recorder = None
        self.recording = False
        self.preview_timer = QtCore.QTimer(self)
        self.preview_timer.timeout.connect(self.update_preview)
        self.clock_timer = QtCore.QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)
        self._build_ui()
        self.refresh_monitors()
        self.preview_timer.start(120)

    def _build_ui(self):
        self.setStyleSheet("""
            QMainWindow { background: #10141b; color: #e9edf3; }
            QWidget { color: #e9edf3; font-family: Segoe UI; font-size: 13px; }
            QFrame#top { background: #171c25; border: 1px solid #2b3340; border-radius: 10px; }
            QLabel#title { font-size: 25px; font-weight: 700; }
            QLabel#status { color: #aab4c3; }
            QGroupBox { border: 1px solid #2b3340; border-radius: 8px; margin-top: 12px; padding: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #9aa8bb; }
            QComboBox, QLineEdit { background: #1b222d; border: 1px solid #3a4453; border-radius: 6px; padding: 7px; }
            QPushButton { background: #252e3b; border: 1px solid #455163; border-radius: 7px; padding: 9px 14px; }
            QPushButton:hover { background: #303b4b; }
            QPushButton#record { background: #e5484d; border: none; font-weight: 700; font-size: 15px; padding: 12px 22px; }
            QPushButton#record:hover { background: #f15b60; }
            QPushButton#stop { background: #394352; font-weight: 700; font-size: 15px; padding: 12px 22px; }
            QCheckBox { spacing: 8px; }
            QLabel#preview { background: #080a0e; border: 1px solid #2b3340; border-radius: 8px; }
            QProgressBar { border: 1px solid #303947; border-radius: 4px; background: #171c25; height: 8px; }
            QProgressBar::chunk { background: #e5484d; border-radius: 4px; }
        """)

        root = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        top = QtWidgets.QFrame(objectName="top")
        top_layout = QtWidgets.QHBoxLayout(top)
        title = QtWidgets.QLabel("FLOWRECORDER", objectName="title")
        status = QtWidgets.QLabel("Listo para grabar", objectName="status")
        self.status_label = status
        top_layout.addWidget(title)
        top_layout.addStretch()
        top_layout.addWidget(status)
        layout.addWidget(top)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(14)

        left = QtWidgets.QVBoxLayout()
        preview_box = QtWidgets.QGroupBox("Vista previa")
        preview_layout = QtWidgets.QVBoxLayout(preview_box)
        self.preview = QtWidgets.QLabel("Vista previa de pantalla", objectName="preview")
        self.preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(640, 390)
        preview_layout.addWidget(self.preview)
        left.addWidget(preview_box, 1)

        controls = QtWidgets.QHBoxLayout()
        self.record_btn = QtWidgets.QPushButton("●  GRABAR", objectName="record")
        self.record_btn.clicked.connect(self.start_recording)
        self.stop_btn = QtWidgets.QPushButton("■  DETENER", objectName="stop")
        self.stop_btn.clicked.connect(self.stop_recording)
        self.stop_btn.setEnabled(False)
        self.timer_label = QtWidgets.QLabel("00:00:00")
        self.timer_label.setStyleSheet("font-size: 20px; font-weight: 700; font-family: Consolas;")
        controls.addWidget(self.record_btn)
        controls.addWidget(self.stop_btn)
        controls.addStretch()
        controls.addWidget(self.timer_label)
        left.addLayout(controls)
        body.addLayout(left, 1)

        settings = QtWidgets.QVBoxLayout()
        capture = QtWidgets.QGroupBox("Captura")
        form = QtWidgets.QFormLayout(capture)
        self.monitor_combo = QtWidgets.QComboBox()
        self.fps_combo = QtWidgets.QComboBox()
        self.fps_combo.addItems([str(x) for x in FPS_OPTIONS])
        self.fps_combo.setCurrentText("30")
        form.addRow("Pantalla:", self.monitor_combo)
        form.addRow("FPS:", self.fps_combo)
        settings.addWidget(capture)

        audio = QtWidgets.QGroupBox("Audio")
        audio_form = QtWidgets.QVBoxLayout(audio)
        self.mic_check = QtWidgets.QCheckBox("Grabar micrófono")
        self.mic_check.setChecked(True)
        audio_form.addWidget(self.mic_check)
        audio_form.addWidget(QtWidgets.QLabel("En esta primera versión grabamos el micrófono. El audio del sistema se añadirá en la siguiente fase."))
        settings.addWidget(audio)

        output = QtWidgets.QGroupBox("Destino")
        out_layout = QtWidgets.QVBoxLayout(output)
        row = QtWidgets.QHBoxLayout()
        self.output_edit = QtWidgets.QLineEdit(str(Path.home() / "Videos" / "FlowRecorder"))
        browse = QtWidgets.QPushButton("Elegir")
        browse.clicked.connect(self.choose_output)
        row.addWidget(self.output_edit)
        row.addWidget(browse)
        out_layout.addLayout(row)
        settings.addWidget(output)

        info = QtWidgets.QGroupBox("Estado")
        info_layout = QtWidgets.QVBoxLayout(info)
        self.info_label = QtWidgets.QLabel("FlowRecorder 1.0\nPantalla + micrófono + MP4")
        self.info_label.setWordWrap(True)
        info_layout.addWidget(self.info_label)
        settings.addWidget(info)
        settings.addStretch()
        body.addLayout(settings, 0)

        layout.addLayout(body, 1)
        self.setCentralWidget(root)

    def refresh_monitors(self):
        self.monitor_combo.clear()
        with mss() as sct:
            for i, monitor in enumerate(sct.monitors[1:], 1):
                self.monitor_combo.addItem(f"Pantalla {i} — {monitor['width']}×{monitor['height']}", monitor)
        if self.monitor_combo.count() == 0:
            self.monitor_combo.addItem("No se detectó pantalla")

    def choose_output(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Seleccionar carpeta de grabaciones")
        if path:
            self.output_edit.setText(path)

    def update_preview(self):
        if self.recording:
            return
        try:
            with mss() as sct:
                monitor = self.monitor_combo.currentData()
                if not monitor:
                    return
                shot = np.asarray(sct.grab(monitor))
                h, w, _ = shot.shape
                image = QtGui.QImage(shot.data, w, h, 4 * w, QtGui.QImage.Format.Format_ARGB32).copy()
                pix = QtGui.QPixmap.fromImage(image)
                self.preview.setPixmap(pix.scaled(self.preview.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation))
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
        self.record_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.mic_check.setEnabled(False)
        self.monitor_combo.setEnabled(False)
        self.fps_combo.setEnabled(False)
        self.status_label.setText("● GRABANDO")
        self.status_label.setStyleSheet("color: #ff6b70; font-weight: 700;")
        self.timer_label.setText("00:00:00")
        self.clock_timer.start(250)

    def stop_recording(self):
        if not self.recording or not self.recorder:
            return
        self.stop_btn.setEnabled(False)
        self.status_label.setText("Guardando…")
        QtWidgets.QApplication.processEvents()
        try:
            final_file = self.recorder.stop()
            self.status_label.setText("Grabación guardada")
            self.info_label.setText(f"Archivo creado:\n{final_file}")
            QtWidgets.QMessageBox.information(self, APP_NAME, f"Grabación terminada.\n\n{final_file}")
        except Exception as exc:
            self.status_label.setText("Error")
            QtWidgets.QMessageBox.critical(self, APP_NAME, str(exc))
        finally:
            self.recording = False
            self.record_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.mic_check.setEnabled(True)
            self.monitor_combo.setEnabled(True)
            self.fps_combo.setEnabled(True)
            self.clock_timer.stop()
            self.timer_label.setText("00:00:00")
            self.recorder = None
            self.preview_timer.start(120)

    def update_clock(self):
        if not self.recorder or not self.recorder.started_at:
            return
        seconds = int(time.monotonic() - self.recorder.started_at)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        self.timer_label.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def closeEvent(self, event):
        if self.recording:
            answer = QtWidgets.QMessageBox.question(self, APP_NAME, "Hay una grabación en curso. ¿Detenerla y cerrar?")
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                self.stop_recording()
            else:
                event.ignore()
                return
        event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
