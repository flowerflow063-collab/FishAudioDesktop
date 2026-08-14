import os, sys, time, wave, queue, shutil, threading, subprocess
from pathlib import Path

import numpy as np
import sounddevice as sd
from mss import mss
from PySide6 import QtCore, QtGui, QtWidgets

try:
    import pyaudiowpatch as pyaudio
    SYSTEM_AUDIO_AVAILABLE = True
except ImportError:
    pyaudio = None
    SYSTEM_AUDIO_AVAILABLE = False

APP_NAME = "FlowRecorder"
FPS = [15, 24, 30, 60]


def ffmpeg():
    local = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent / "ffmpeg.exe"
    return str(local) if local.exists() else (shutil.which("ffmpeg") or "ffmpeg")


def meter_value(data):
    a = np.asarray(data, dtype=np.float32)
    if not a.size:
        return 0.0
    rms = float(np.sqrt(np.mean(a * a)))
    if rms <= 1:
        return 0.0
    db = 20 * np.log10(rms / 32768.0)
    return max(0.0, min(100.0, (db + 60.0) * 100.0 / 60.0))


class Recorder:
    def __init__(self, monitor, fps, out_dir, mode, mic, system, gains, mutes):
        self.monitor = dict(monitor)
        self.fps = fps
        self.out_dir = Path(out_dir)
        self.mode = mode
        self.mic = mic
        self.system = system and SYSTEM_AUDIO_AVAILABLE
        self.gains = dict(gains)
        self.mutes = dict(mutes)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.mic_level = 0.0
        self.system_level = 0.0
        self.errors = []
        self.mq = queue.Queue(maxsize=200)
        self.vt = self.mt = self.st = None
        self.mic_stream = None
        self.ss = self.spa = None
        self.mw = self.sw = None

    def set_channel(self, channel, gain=None, mute=None):
        with self.lock:
            if gain is not None:
                self.gains[channel] = gain
            if mute is not None:
                self.mutes[channel] = mute

    def state(self, channel):
        with self.lock:
            return self.gains.get(channel, 1.0), self.mutes.get(channel, False)

    def rect(self):
        m = self.monitor
        w, h = int(m["width"]), int(m["height"])
        if self.mode == "vertical":
            nw = min(w, int(h * 9 / 16))
            nw -= nw % 2
            return {"left": m["left"] + (w - nw) // 2, "top": m["top"],
                    "width": nw, "height": h - h % 2}
        nh = min(h, int(w * 9 / 16))
        nh -= nh % 2
        return {"left": m["left"], "top": m["top"] + (h - nh) // 2,
                "width": w - w % 2, "height": nh}

    def start(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        tag = "vertical" if self.mode == "vertical" else "horizontal"
        base = self.out_dir / f"FlowRecorder_{tag}_{stamp}"
        self.video_file = base.with_suffix(".video.mp4")
        self.micfile = base.with_suffix(".mic.wav") if self.mic else None
        self.sysfile = base.with_suffix(".system.wav") if self.system else None
        self.final = base.with_suffix(".mp4")

        self.stop_event.clear()
        self.vt = threading.Thread(target=self.video, daemon=True)
        self.vt.start()
        if self.mic:
            self.mt = threading.Thread(target=self.mic_capture, daemon=True)
            self.mt.start()
        if self.system:
            self.st = threading.Thread(target=self.system_capture, daemon=True)
            self.st.start()

    def video(self):
        r = self.rect()
        cmd = [
            ffmpeg(), "-y", "-f", "rawvideo", "-pix_fmt", "bgra",
            "-video_size", f"{r['width']}x{r['height']}", "-framerate", str(self.fps),
            "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-pix_fmt", "yuv420p", str(self.video_file)
        ]
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE
            )
            with mss() as sct:
                step = 1 / self.fps
                next_frame = time.perf_counter()
                while not self.stop_event.is_set():
                    frame = np.asarray(sct.grab(r), dtype=np.uint8)
                    proc.stdin.write(frame.tobytes())
                    next_frame += step
                    delay = next_frame - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                    elif delay < -step * 2:
                        next_frame = time.perf_counter()
            proc.stdin.close()
            err = proc.stderr.read().decode(errors="replace")
            if proc.wait():
                self.errors.append("FFmpeg vídeo: " + (err[-2500:] or "código desconocido"))
        except Exception as exc:
            self.errors.append("Vídeo: " + str(exc))
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass

    def mic_cb(self, data, frames, timestamp, status):
        self.mic_level = meter_value(data)
        if status:
            self.errors.append("Micrófono: " + str(status))
        try:
            self.mq.put_nowait(data.copy())
        except queue.Full:
            pass

    def process_audio(self, data, channel):
        gain, mute = self.state(channel)
        a = np.asarray(data, dtype=np.float32)
        if mute or gain <= 0:
            return np.zeros_like(a, dtype=np.int16)
        return np.clip(a * gain, -32768, 32767).astype(np.int16)

    def mic_capture(self):
        try:
            dev = sd.query_devices(kind="input")
            channels = min(2, int(dev.get("max_input_channels", 0)))
            if channels < 1:
                raise RuntimeError("No hay micrófono de entrada disponible en Windows.")
            rate = int(dev.get("default_samplerate") or 48000)
            self.mw = wave.open(str(self.micfile), "wb")
            self.mw.setnchannels(channels)
            self.mw.setsampwidth(2)
            self.mw.setframerate(rate)

            stream = sd.InputStream(
                device=dev["name"], samplerate=rate, channels=channels,
                dtype="int16", callback=self.mic_cb, blocksize=1024
            )
            self.mic_stream = stream
            stream.start()

            while not self.stop_event.is_set() or not self.mq.empty():
                try:
                    self.mw.writeframes(self.process_audio(self.mq.get(timeout=.2), "mic").tobytes())
                except queue.Empty:
                    pass
        except Exception as exc:
            self.errors.append("Micrófono: " + str(exc))
        finally:
            try:
                self.mic_stream.stop()
                self.mic_stream.close()
            except Exception:
                pass
            try:
                self.mw.close()
            except Exception:
                pass

    def system_capture(self):
        pa = stream = wf = None
        try:
            pa = pyaudio.PyAudio()
            try:
                loop = pa.get_default_wasapi_loopback()
            except Exception:
                api = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
                out = pa.get_device_info_by_index(api["defaultOutputDevice"])
                loop = next(
                    (x for x in pa.get_loopback_device_info_generator()
                     if out["name"] in x["name"]), None
                )
                if loop is None:
                    raise RuntimeError("No se encontró la salida WASAPI de Windows.")

            channels = max(1, min(2, int(loop.get("maxInputChannels", 2))))
            rate = int(loop.get("defaultSampleRate") or 48000)
            index = int(loop["index"])
            wf = wave.open(str(self.sysfile), "wb")
            wf.setnchannels(channels)
            wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
            wf.setframerate(rate)

            stream = pa.open(
                format=pyaudio.paInt16, channels=channels, rate=rate,
                frames_per_buffer=1024, input=True, input_device_index=index
            )
            self.ss, self.spa = stream, pa

            while not self.stop_event.is_set():
                raw = np.frombuffer(
                    stream.read(1024, exception_on_overflow=False), dtype=np.int16
                )
                self.system_level = meter_value(raw)
                wf.writeframes(self.process_audio(raw, "system").tobytes())
        except Exception as exc:
            self.errors.append("Audio del sistema: " + str(exc))
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            try:
                wf.close()
            except Exception:
                pass
            try:
                pa.terminate()
            except Exception:
                pass

    def stop(self):
        self.stop_event.set()
        for thread, timeout in ((self.vt, 30), (self.mt, 10), (self.st, 10)):
            if thread:
                thread.join(timeout)

        if self.errors:
            raise RuntimeError("\n\n".join(self.errors[-3:]))
        if not self.video_file.exists():
            raise RuntimeError("FFmpeg no creó el vídeo.")

        audio = [x for x in (self.micfile, self.sysfile) if x]
        if not audio:
            cmd = [ffmpeg(), "-y", "-i", str(self.video_file), "-c", "copy", str(self.final)]
        elif len(audio) == 1:
            cmd = [
                ffmpeg(), "-y", "-i", str(self.video_file), "-i", str(audio[0]),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-shortest", str(self.final)
            ]
        else:
            cmd = [
                ffmpeg(), "-y", "-i", str(self.video_file), "-i", str(audio[0]),
                "-i", str(audio[1]), "-filter_complex",
                "[1:a]aresample=48000[a1];[2:a]aresample=48000[a2];"
                "[a1][a2]amix=inputs=2:duration=longest:dropout_transition=2[aout]",
                "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-shortest", str(self.final)
            ]

        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if result.returncode:
            raise RuntimeError("No se pudo crear el MP4:\n\n" + result.stderr[-3000:])

        for f in (self.video_file, self.micfile, self.sysfile):
            try:
                if f and f.exists():
                    f.unlink()
            except Exception:
                pass
        return self.final


class MeterBar(QtWidgets.QProgressBar):
    def __init__(self):
        super().__init__()
        self.setRange(0, 100)
        self.setValue(0)
        self.setTextVisible(False)
        self.setFixedHeight(16)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1500, 920)
        self.setMinimumSize(1180, 760)
        self.recording = False
        self.rec = None
        self.mode = "horizontal"
        self.started = 0

        self.setStyleSheet("""
        QMainWindow,QWidget { background:#090d12; color:#edf2f7; font-family:"Segoe UI"; }
        QFrame#side { background:#0e141c; border-right:1px solid #202936; }
        QFrame#card,QGroupBox { background:#111821; border:1px solid #202b38; border-radius:12px; }
        QLabel#brand { font-size:22px; font-weight:800; }
        QLabel#muted { color:#7c8a9d; font-size:10px; }
        QLabel#title { font-size:21px; font-weight:700; }
        QLabel#ct { color:#aab7c7; font-size:11px; font-weight:700; }
        QLabel#big { font-size:19px; font-weight:750; }
        QLabel#preview { background:#05070a; border:1px solid #2a3746; border-radius:12px; }
        QLabel#previewTitle { color:#aebccc; font-size:11px; font-weight:700; }
        QComboBox,QLineEdit { background:#0d131b; border:1px solid #293544; border-radius:8px; padding:7px; color:#e8edf3; }
        QPushButton { background:#18212c; border:1px solid #2b3948; border-radius:8px; padding:8px 12px; color:#e8edf3; }
        QPushButton:hover { background:#202c3a; }
        QPushButton#record { background:#e9434a; border:0; font-weight:800; padding:12px 24px; }
        QPushButton#stop { font-weight:700; padding:12px 20px; }
        QPushButton#mode { font-weight:700; padding:9px; }
        QPushButton#mode[active="true"] { background:#29384a; border:1px solid #5a7ea3; }
        QCheckBox { spacing:7px; }
        QSlider::groove:horizontal { height:5px; background:#273241; }
        QSlider::handle:horizontal { width:14px; margin:-5px 0; border-radius:7px; background:#a9b8ca; }
        QProgressBar { border:0; border-radius:5px; background:#080c11; }
        QProgressBar::chunk { border-radius:5px; background:#35d07f; }
        QGroupBox { margin-top:8px; padding:10px; }
        QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; color:#9ba8b9; background:#111821; }
        """)

        self.build_ui()
        self.refresh_monitors()
        self.set_mode("horizontal")

        self.preview_timer = QtCore.QTimer(self)
        self.preview_timer.timeout.connect(self.update_previews)
        self.preview_timer.start(180)

        self.meter_timer = QtCore.QTimer(self)
        self.meter_timer.timeout.connect(self.update_meters)
        self.meter_timer.start(50)

        self.clock_timer = QtCore.QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)

    def build_ui(self):
        central = QtWidgets.QWidget()
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        self.setCentralWidget(central)

        side = QtWidgets.QFrame(objectName="side")
        side.setFixedWidth(205)
        sl = QtWidgets.QVBoxLayout(side)
        sl.setContentsMargins(14, 20, 14, 18)
        sl.addWidget(QtWidgets.QLabel("FLOWRECORDER", objectName="brand"))
        sl.addWidget(QtWidgets.QLabel("SCREEN STUDIO", objectName="muted"))
        sl.addSpacing(20)
        for text, active in [
            ("▣  Estudio", True), ("●  Grabaciones", False),
            ("◉  Audio", False), ("⚙  Ajustes", False)
        ]:
            button = QtWidgets.QPushButton(text)
            button.setObjectName("mode")
            button.setProperty("active", active)
            sl.addWidget(button)
        sl.addStretch()
        sl.addWidget(QtWidgets.QLabel(
            "FlowRecorder 1.4\nDual Preview + Mixer", objectName="muted"
        ))
        root.addWidget(side)

        body = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(body)
        bl.setContentsMargins(22, 16, 22, 18)
        root.addWidget(body, 1)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Estudio", objectName="title"))
        top.addStretch()
        self.status = QtWidgets.QLabel("● Listo para grabar", objectName="muted")
        top.addWidget(self.status)
        bl.addLayout(top)

        stats = QtWidgets.QHBoxLayout()
        self.fps_label = self.stat(stats, "FPS", "30")
        self.time_label = self.stat(stats, "DURACIÓN", "00:00:00")
        self.audio_label = self.stat(stats, "AUDIO", "Mic + sistema")
        self.format_label = self.stat(stats, "GRABACIÓN", "16:9")
        bl.addLayout(stats)

        previews = QtWidgets.QHBoxLayout()
        previews.setSpacing(14)

        hcard = QtWidgets.QFrame(objectName="card")
        hv = QtWidgets.QVBoxLayout(hcard)
        hhead = QtWidgets.QHBoxLayout()
        hhead.addWidget(QtWidgets.QLabel("VISTA HORIZONTAL", objectName="previewTitle"))
        hhead.addStretch()
        hhead.addWidget(QtWidgets.QLabel("16:9", objectName="muted"))
        hv.addLayout(hhead)
        self.horizontal_preview = QtWidgets.QLabel("Preparando…", objectName="preview")
        self.horizontal_preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.horizontal_preview.setMinimumSize(600, 330)
        hv.addWidget(self.horizontal_preview, 1)
        previews.addWidget(hcard, 2)

        vcard = QtWidgets.QFrame(objectName="card")
        vv = QtWidgets.QVBoxLayout(vcard)
        vhead = QtWidgets.QHBoxLayout()
        vhead.addWidget(QtWidgets.QLabel("VISTA VERTICAL", objectName="previewTitle"))
        vhead.addStretch()
        vhead.addWidget(QtWidgets.QLabel("9:16", objectName="muted"))
        vv.addLayout(vhead)
        self.vertical_preview = QtWidgets.QLabel("Preparando…", objectName="preview")
        self.vertical_preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.vertical_preview.setMinimumSize(220, 330)
        vv.addWidget(self.vertical_preview, 1)
        previews.addWidget(vcard, 1)
        bl.addLayout(previews, 2)

        controls = QtWidgets.QHBoxLayout()
        self.build_capture(controls)
        self.build_format(controls)
        self.build_sources(controls)
        self.build_destination(controls)
        bl.addLayout(controls)

        bl.addWidget(self.build_mixer(), 0)

        bottom = QtWidgets.QFrame(objectName="card")
        bb = QtWidgets.QHBoxLayout(bottom)
        self.start_button = QtWidgets.QPushButton("●  INICIAR GRABACIÓN", objectName="record")
        self.start_button.clicked.connect(self.start_recording)
        self.stop_button = QtWidgets.QPushButton("■  DETENER", objectName="stop")
        self.stop_button.clicked.connect(self.stop_recording)
        self.stop_button.setEnabled(False)
        self.timer_label = QtWidgets.QLabel("00:00:00", objectName="big")
        bb.addWidget(self.start_button)
        bb.addWidget(self.stop_button)
        bb.addStretch()
        bb.addWidget(self.timer_label)
        bl.addWidget(bottom)

    def stat(self, layout, title, value):
        card = QtWidgets.QFrame(objectName="card")
        row = QtWidgets.QVBoxLayout(card)
        row.addWidget(QtWidgets.QLabel(title, objectName="ct"))
        label = QtWidgets.QLabel(value, objectName="big")
        row.addWidget(label)
        layout.addWidget(card, 1)
        return label

    def build_capture(self, layout):
        group = QtWidgets.QGroupBox("CAPTURA")
        form = QtWidgets.QFormLayout(group)
        self.monitor_combo = QtWidgets.QComboBox()
        self.fps_combo = QtWidgets.QComboBox()
        self.fps_combo.addItems(map(str, FPS))
        self.fps_combo.setCurrentText("30")
        self.fps_combo.currentTextChanged.connect(self.fps_label.setText)
        form.addRow("Pantalla", self.monitor_combo)
        form.addRow("FPS", self.fps_combo)
        layout.addWidget(group, 2)

    def build_format(self, layout):
        group = QtWidgets.QGroupBox("FORMATO DE GRABACIÓN")
        box = QtWidgets.QVBoxLayout(group)
        row = QtWidgets.QHBoxLayout()
        self.horizontal_button = QtWidgets.QPushButton("▭  Horizontal\n16:9", objectName="mode")
        self.vertical_button = QtWidgets.QPushButton("▯  Vertical\n9:16", objectName="mode")
        self.horizontal_button.clicked.connect(lambda: self.set_mode("horizontal"))
        self.vertical_button.clicked.connect(lambda: self.set_mode("vertical"))
        row.addWidget(self.horizontal_button)
        row.addWidget(self.vertical_button)
        box.addLayout(row)
        self.format_hint = QtWidgets.QLabel(
            "La grabación usa el formato seleccionado.", objectName="muted"
        )
        box.addWidget(self.format_hint)
        layout.addWidget(group, 2)

    def build_sources(self, layout):
        group = QtWidgets.QGroupBox("FUENTES DE AUDIO")
        box = QtWidgets.QVBoxLayout(group)
        self.mic_check = QtWidgets.QCheckBox("Grabar micrófono")
        self.mic_check.setChecked(True)
        self.system_check = QtWidgets.QCheckBox("Grabar audio del sistema")
        self.system_check.setChecked(SYSTEM_AUDIO_AVAILABLE)
        self.system_check.setEnabled(SYSTEM_AUDIO_AVAILABLE)
        self.mic_check.stateChanged.connect(self.update_audio_label)
        self.system_check.stateChanged.connect(self.update_audio_label)
        box.addWidget(self.mic_check)
        box.addWidget(self.system_check)
        layout.addWidget(group, 2)

    def build_destination(self, layout):
        group = QtWidgets.QGroupBox("DESTINO")
        box = QtWidgets.QVBoxLayout(group)
        row = QtWidgets.QHBoxLayout()
        self.output_edit = QtWidgets.QLineEdit(str(Path.home() / "Videos" / "FlowRecorder"))
        choose = QtWidgets.QPushButton("Elegir")
        choose.clicked.connect(self.choose_output)
        row.addWidget(self.output_edit)
        row.addWidget(choose)
        box.addLayout(row)
        self.file_label = QtWidgets.QLabel("MP4 • H.264 • AAC", objectName="muted")
        box.addWidget(self.file_label)
        layout.addWidget(group, 3)

    def build_mixer(self):
        card = QtWidgets.QFrame(objectName="card")
        box = QtWidgets.QVBoxLayout(card)
        header = QtWidgets.QHBoxLayout()
        header.addWidget(QtWidgets.QLabel("MEZCLADOR DE AUDIO", objectName="ct"))
        header.addStretch()
        self.mixer_status = QtWidgets.QLabel(
            "Barras en tiempo real durante la grabación", objectName="muted"
        )
        header.addWidget(self.mixer_status)
        box.addLayout(header)

        self.mixer_rows = {}
        for channel, name in [("mic", "🎙  Micrófono"), ("system", "🔊  Audio del sistema")]:
            row = QtWidgets.QHBoxLayout()
            label = QtWidgets.QLabel(name)
            label.setMinimumWidth(145)
            row.addWidget(label)
            bar = MeterBar()
            row.addWidget(bar, 1)
            db = QtWidgets.QLabel("-∞ dB")
            db.setMinimumWidth(58)
            row.addWidget(db)
            gain = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            gain.setRange(0, 200)
            gain.setValue(100)
            gain.setFixedWidth(100)
            gain.setToolTip("Ganancia 0–200 %")
            mute = QtWidgets.QPushButton("M")
            mute.setCheckable(True)
            mute.setFixedWidth(32)
            row.addWidget(gain)
            row.addWidget(mute)
            box.addLayout(row)
            self.mixer_rows[channel] = (bar, db, gain, mute)
            gain.valueChanged.connect(lambda value, ch=channel: self.change_gain(ch, value))
            mute.toggled.connect(lambda checked, ch=channel: self.change_mute(ch, checked))
        return card

    def refresh_monitors(self):
        self.monitor_combo.clear()
        try:
            with mss() as sct:
                for index, monitor in enumerate(sct.monitors[1:], 1):
                    self.monitor_combo.addItem(
                        f"Pantalla {index} · {monitor['width']}×{monitor['height']}", monitor
                    )
        except Exception:
            pass

    def choose_output(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Seleccionar carpeta de grabaciones")
        if path:
            self.output_edit.setText(path)

    def set_mode(self, mode):
        if self.recording:
            return
        self.mode = mode
        horizontal = mode == "horizontal"
        self.horizontal_button.setProperty("active", horizontal)
        self.vertical_button.setProperty("active", not horizontal)
        for button in (self.horizontal_button, self.vertical_button):
            button.style().unpolish(button)
            button.style().polish(button)
        self.format_label.setText("16:9" if horizontal else "9:16")
        self.format_hint.setText(
            "Grabará la vista horizontal 16:9." if horizontal else "Grabará la vista vertical 9:16."
        )

    def update_audio_label(self):
        mic = self.mic_check.isChecked()
        system = self.system_check.isChecked()
        self.audio_label.setText(
            "Mic + sistema" if mic and system else "Micrófono" if mic else "Sistema" if system else "Sin audio"
        )

    def change_gain(self, channel, value):
        if self.recording and self.rec:
            self.rec.set_channel(channel, gain=value / 100.0)

    def change_mute(self, channel, checked):
        if self.recording and self.rec:
            self.rec.set_channel(channel, mute=checked)

    @staticmethod
    def crop_rect(monitor, mode):
        w, h = int(monitor["width"]), int(monitor["height"])
        if mode == "vertical":
            nw = min(w, int(h * 9 / 16))
            nw -= nw % 2
            return {"left": monitor["left"] + (w - nw) // 2, "top": monitor["top"],
                    "width": nw, "height": h - h % 2}
        nh = min(h, int(w * 9 / 16))
        nh -= nh % 2
        return {"left": monitor["left"], "top": monitor["top"] + (h - nh) // 2,
                "width": w - w % 2, "height": nh}

    def update_previews(self):
        if self.recording:
            return
        monitor = self.monitor_combo.currentData()
        if not monitor:
            return
        try:
            with mss() as sct:
                horizontal = np.asarray(sct.grab(self.crop_rect(monitor, "horizontal")))
                vertical = np.asarray(sct.grab(self.crop_rect(monitor, "vertical")))
            self.set_preview(self.horizontal_preview, horizontal)
            self.set_preview(self.vertical_preview, vertical)
        except Exception:
            pass

    @staticmethod
    def set_preview(widget, frame):
        height, width, _ = frame.shape
        image = QtGui.QImage(frame.data, width, height, 4 * width, QtGui.QImage.Format.Format_ARGB32).copy()
        pixmap = QtGui.QPixmap.fromImage(image).scaled(
            widget.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation
        )
        widget.setPixmap(pixmap)

    def update_meters(self):
        mic = self.rec.mic_level if self.recording and self.rec else 0.0
        system = self.rec.system_level if self.recording and self.rec else 0.0
        for channel, value in {"mic": mic, "system": system}.items():
            bar, db, _, _ = self.mixer_rows[channel]
            bar.setValue(int(value))
            db.setText("-∞ dB" if value < 0.1 else f"{-60 + value * 0.6:4.1f} dB")

    def set_controls_enabled(self, enabled):
        for widget in (self.monitor_combo, self.fps_combo, self.horizontal_button, self.vertical_button,
                       self.mic_check, self.system_check):
            widget.setEnabled(enabled)
        self.system_check.setEnabled(enabled and SYSTEM_AUDIO_AVAILABLE)

    def start_recording(self):
        monitor = self.monitor_combo.currentData()
        output = self.output_edit.text().strip()
        if not monitor or not output:
            QtWidgets.QMessageBox.warning(self, APP_NAME, "Selecciona pantalla y carpeta de salida.")
            return
        mic = self.mic_check.isChecked()
        system = self.system_check.isChecked()
        if system and not SYSTEM_AUDIO_AVAILABLE:
            QtWidgets.QMessageBox.warning(self, APP_NAME, "El audio del sistema no está disponible en esta versión.")
            return
        gains = {channel: row[2].value() / 100.0 for channel, row in self.mixer_rows.items()}
        mutes = {channel: row[3].isChecked() for channel, row in self.mixer_rows.items()}
        self.rec = Recorder(monitor, int(self.fps_combo.currentText()), output, self.mode, mic, system, gains, mutes)
        self.rec.start()
        self.recording = True
        self.started = time.monotonic()
        self.set_controls_enabled(False)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status.setText("● GRABANDO")
        self.status.setStyleSheet("color:#ff5960;font-weight:800")
        self.mixer_status.setText("● MONITOREANDO EN VIVO")
        self.mixer_status.setStyleSheet("color:#53d88a;font-weight:800")
        self.clock_timer.start(250)

    def stop_recording(self):
        if not self.recording:
            return
        self.stop_button.setEnabled(False)
        self.status.setText("● GUARDANDO…")
        QtWidgets.QApplication.processEvents()
        try:
            final_file = self.rec.stop()
            self.file_label.setText("Guardado: " + final_file.name)
            QtWidgets.QMessageBox.information(self, APP_NAME, "Grabación terminada.\n\n" + str(final_file))
            self.status.setText("● Grabación guardada")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, APP_NAME, str(exc))
            self.status.setText("● Error")
        finally:
            self.recording = False
            self.rec = None
            self.clock_timer.stop()
            self.set_controls_enabled(True)
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.mixer_status.setText("Barras en tiempo real durante la grabación")
            self.mixer_status.setStyleSheet("color:#7c8a9d;font-size:10px")
            self.status.setStyleSheet("color:#7c8a9d")

    def update_clock(self):
        seconds = int(time.monotonic() - self.started)
        value = time.strftime("%H:%M:%S", time.gmtime(seconds))
        self.timer_label.setText(value)
        self.time_label.setText(value)

    def closeEvent(self, event):
        if self.recording:
            answer = QtWidgets.QMessageBox.question(
                self, APP_NAME, "Hay una grabación activa. ¿Detener antes de salir?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
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
