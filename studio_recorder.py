import subprocess
import tempfile
import threading
import wave
from pathlib import Path

import cv2
import mss
import numpy as np
import sounddevice as sd
try:
    import soundcard as sc
except Exception:
    sc = None
try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg = None
from PIL import Image, ImageTk
import tkinter as tk


class StudioRecorder:
    """OBS-style screen + camera + audio recorder with horizontal/vertical layouts."""

    def __init__(self, master, status_callback=None):
        from tkinter import ttk, filedialog, messagebox
        self.master = master
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.status = status_callback or (lambda text: None)
        self.running = False
        self.recording = False
        self.layout = tk.StringVar(master=master, value="horizontal")
        self.output = tk.StringVar(master=master, value=str(Path.home() / "Videos" / "FlowRecorder" / "flowrecorder.mp4"))
        self.cam_var = tk.StringVar(master=master, value="Cámara 0")
        self.monitor_var = tk.StringVar(master=master, value="Monitor 1")
        self.sct = None
        self.monitors = []
        self.cam = None
        self.writer = None
        self.video_size = (1280, 720)
        self.audio_blocks = []
        self.loop_blocks = []
        self.audio_rate = 48000
        self.audio_channels = 2
        self.audio_stop = None
        self.audio_thread = None
        self.temp = None
        self.preview_photo = None
        self._build()
        self._discover()
        self._tick()

    def _build(self):
        root = self.ttk.Frame(self.master)
        root.pack(fill="both", expand=True)
        self.preview = tk.Label(root, bg="#080b10", fg="#8f9aaa", text="Vista previa\nInicia previsualización", font=("Segoe UI", 16))
        self.preview.pack(fill="both", expand=True, padx=12, pady=12)
        controls = self.ttk.Frame(root)
        controls.pack(fill="x", padx=12, pady=(0, 12))
        self.ttk.Label(controls, text="Pantalla").pack(side="left")
        self.mon_combo = self.ttk.Combobox(controls, textvariable=self.monitor_var, state="readonly", width=18)
        self.mon_combo.pack(side="left", padx=5)
        self.ttk.Label(controls, text="Cámara").pack(side="left", padx=(10, 0))
        self.cam_combo = self.ttk.Combobox(controls, textvariable=self.cam_var, state="readonly", width=18)
        self.cam_combo.pack(side="left", padx=5)
        self.ttk.Label(controls, text="Diseño").pack(side="left", padx=(10, 0))
        self.ttk.Radiobutton(controls, text="Horizontal", variable=self.layout, value="horizontal").pack(side="left")
        self.ttk.Radiobutton(controls, text="Vertical", variable=self.layout, value="vertical").pack(side="left")
        self.preview_btn = self.ttk.Button(controls, text="▶ PREVISUALIZAR", command=self.toggle_preview)
        self.preview_btn.pack(side="left", padx=8)
        self.rec_btn = self.ttk.Button(controls, text="● GRABAR", command=self.toggle_record)
        self.rec_btn.pack(side="left", padx=4)
        self.ttk.Entry(controls, textvariable=self.output, width=42).pack(side="left", padx=(10, 3), fill="x", expand=True)
        self.ttk.Button(controls, text="…", command=self.pick_output).pack(side="left")

    def _discover(self):
        try:
            self.sct = mss.mss()
            self.monitors = self.sct.monitors[1:]
            values = [f"Monitor {i + 1} ({m['width']}x{m['height']})" for i, m in enumerate(self.monitors)]
            self.mon_combo["values"] = values
            if values:
                self.mon_combo.current(0)
        except Exception:
            self.monitors = []
        cameras = []
        for idx in range(5):
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
            ok = cap.isOpened()
            cap.release()
            if ok:
                cameras.append(f"Cámara {idx}")
        if not cameras:
            cameras = ["Sin cámara (solo pantalla)"]
        self.cam_combo["values"] = cameras
        self.cam_combo.current(0)

    def pick_output(self):
        p = self.filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("Video MP4", "*.mp4")])
        if p:
            self.output.set(p)

    def _open_camera(self):
        name = self.cam_var.get()
        try:
            idx = int(name.split()[-1])
        except Exception:
            idx = -1
        if idx >= 0 and self.cam is None:
            self.cam = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
            self.cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)

    def _capture_screen(self):
        if not self.monitors or self.sct is None:
            return None
        idx = max(0, self.mon_combo.current())
        img = np.array(self.sct.grab(self.monitors[idx]))[:, :, :3]
        return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    def _camera_frame(self):
        if self.cam is None:
            return None
        ok, frame = self.cam.read()
        if not ok:
            return None
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def _compose(self, screen, camera):
        if screen is None:
            return None
        if camera is None:
            return screen
        h, w = screen.shape[:2]
        target_w = max(240, w // 3)
        target_h = max(180, int(camera.shape[0] * target_w / camera.shape[1]))
        camera = cv2.resize(camera, (target_w, target_h))
        if self.layout.get() == "vertical":
            camera = cv2.resize(camera, (w, int(camera.shape[0] * w / camera.shape[1])))
            return np.vstack([screen, camera])
        canvas = screen.copy()
        x = w - target_w - 24
        y = h - target_h - 24
        canvas[y:y + target_h, x:x + target_w] = camera
        cv2.rectangle(canvas, (x, y), (x + target_w, y + target_h), (255, 255, 255), 2)
        return canvas

    def toggle_preview(self):
        self.running = not self.running
        if self.running:
            self._open_camera()
            self.preview_btn.configure(text="■ DETENER")
            self.status("Previsualización activa")
        else:
            self.preview_btn.configure(text="▶ PREVISUALIZAR")
            self.status("Previsualización detenida")

    def _tick(self):
        if self.running or self.recording:
            self._open_camera()
            frame = self._compose(self._capture_screen(), self._camera_frame())
            if frame is not None:
                width = min(1100, max(600, self.preview.winfo_width() - 24))
                height = max(1, int(frame.shape[0] * width / frame.shape[1]))
                preview = cv2.resize(frame, (width, height))
                self.preview_photo = ImageTk.PhotoImage(Image.fromarray(preview))
                self.preview.configure(image=self.preview_photo, text="")
                if self.recording:
                    bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                    if (bgr.shape[1], bgr.shape[0]) != self.video_size:
                        bgr = cv2.resize(bgr, self.video_size)
                    self.writer.write(bgr)
        self.master.after(33, self._tick)

    def _start_audio(self):
        self.audio_blocks = []
        self.loop_blocks = []
        self.audio_stop = threading.Event()
        try:
            info = sd.query_devices(kind="input")
            self.audio_rate = int(float(info.get("default_samplerate", 48000)))
            self.audio_channels = max(1, min(2, int(info.get("max_input_channels", 1))))
        except Exception:
            self.audio_rate = 48000
            self.audio_channels = 2

        def callback(indata, frames, time_info, status):
            if not self.audio_stop.is_set():
                self.audio_blocks.append(indata.copy())

        self.stream = sd.InputStream(samplerate=self.audio_rate, channels=self.audio_channels, dtype="float32", callback=callback, blocksize=1024)
        self.stream.start()
        if sc is not None:
            try:
                speaker = sc.default_speaker()
                loopback = sc.get_microphone(speaker.name, include_loopback=True)
                def loop_worker():
                    with loopback.recorder(samplerate=self.audio_rate, channels=2, blocksize=1024) as rec:
                        while not self.audio_stop.is_set():
                            self.loop_blocks.append(rec.record(numframes=1024))
                self.audio_thread = threading.Thread(target=loop_worker, daemon=True)
                self.audio_thread.start()
            except Exception:
                self.audio_thread = None

    def start_record(self):
        out = Path(self.output.get().strip())
        if out.suffix.lower() != ".mp4":
            out = out.with_suffix(".mp4")
            self.output.set(str(out))
        out.parent.mkdir(parents=True, exist_ok=True)
        self.running = True
        self._open_camera()
        frame = self._compose(self._capture_screen(), self._camera_frame())
        if frame is None:
            self.messagebox.showerror("Grabación", "No se pudo capturar la pantalla.")
            return
        h, w = frame.shape[:2]
        self.video_size = (w, h)
        self.temp = Path(tempfile.mkdtemp(prefix="flowrecorder_video_"))
        self.video_path = self.temp / "video.mp4"
        self.audio_path = self.temp / "audio.wav"
        self.final_path = out
        self.writer = cv2.VideoWriter(str(self.video_path), cv2.VideoWriter_fourcc(*"mp4v"), 30.0, self.video_size)
        if not self.writer.isOpened():
            self.messagebox.showerror("Grabación", "No se pudo crear el archivo de video.")
            return
        try:
            self._start_audio()
        except Exception as e:
            self.writer.release()
            self.writer = None
            self.messagebox.showerror("Audio", f"No se pudo iniciar el micrófono.\n\n{e}")
            return
        self.recording = True
        self.rec_btn.configure(text="■ DETENER GRABACIÓN")
        self.preview_btn.configure(text="■ GRABANDO")
        self.status("Grabando pantalla + cámara + audio…")

    def toggle_record(self):
        if self.recording:
            self.stop_record()
        else:
            self.start_record()

    def _write_wav(self, path, data, rate):
        if not data:
            return
        arr = np.concatenate(data, axis=0).astype(np.float32)
        if arr.ndim == 1:
            arr = arr[:, None]
        arr = np.clip(arr, -1.0, 1.0)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(arr.shape[1])
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes((arr * 32767.0).astype(np.int16).tobytes())

    def stop_record(self):
        self.recording = False
        self.rec_btn.configure(text="● GRABAR")
        if self.writer:
            self.writer.release()
            self.writer = None
        if self.audio_stop:
            self.audio_stop.set()
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
        if self.audio_thread:
            self.audio_thread.join(timeout=2)
        self.status("Finalizando y sincronizando audio…")
        threading.Thread(target=self._finalize, daemon=True).start()

    def _finalize(self):
        try:
            mic = np.concatenate(self.audio_blocks, axis=0) if self.audio_blocks else np.zeros((0, 2), np.float32)
            desk = np.concatenate(self.loop_blocks, axis=0) if self.loop_blocks else np.zeros((0, 2), np.float32)
            if mic.ndim == 1: mic = mic[:, None]
            if desk.ndim == 1: desk = desk[:, None]
            if mic.shape[1] == 1: mic = np.repeat(mic, 2, axis=1)
            if desk.shape[1] == 1: desk = np.repeat(desk, 2, axis=1)
            n = max(len(mic), len(desk))
            mix = np.zeros((n, 2), np.float32)
            if len(mic): mix[:len(mic)] += mic[:, :2]
            if len(desk): mix[:len(desk)] += desk[:, :2]
            peak = float(np.max(np.abs(mix))) if mix.size else 0.0
            if peak > 0.98: mix *= 0.98 / peak
            self._write_wav(self.audio_path, [mix], self.audio_rate)
            if imageio_ffmpeg is None:
                raise RuntimeError("FFmpeg no está disponible. Reinstala la versión nueva de FlowRecorder.")
            ff = imageio_ffmpeg.get_ffmpeg_exe()
            cmd = [ff, "-y", "-i", str(self.video_path), "-i", str(self.audio_path), "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(self.final_path)]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if p.returncode != 0:
                raise RuntimeError(p.stderr[-1500:])
            self.master.after(0, lambda: self.status(f"Video listo: {self.final_path}"))
            self.master.after(0, lambda: self.messagebox.showinfo("Grabación lista", f"Video guardado en:\n{self.final_path}"))
        except Exception as e:
            self.master.after(0, lambda msg=str(e): self.messagebox.showerror("Grabación", msg))

    def shutdown(self):
        if self.recording:
            self.stop_record()
        self.running = False
        try:
            if self.cam: self.cam.release()
            if self.sct: self.sct.close()
        except Exception:
            pass
