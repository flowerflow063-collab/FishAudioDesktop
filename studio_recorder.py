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