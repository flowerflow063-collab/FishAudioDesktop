import math
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import numpy as np
import sounddevice as sd


BG = "#0f131a"
PANEL = "#171d27"
TEXT = "#e8ecf1"
MUTED = "#8f9aaa"
ACCENT = "#6d5dfc"
METER_BG = "#242b36"
METER_OK = "#35d07f"
METER_WARN = "#f5c542"
METER_CLIP = "#ff5b5b"


class AudioChannel:
    def __init__(self, device_index, name, channels=1):
        self.device_index = device_index
        self.name = name
        self.channels = max(1, int(channels or 1))
        self.stream = None
        self.level = 0.0
        self.peak = 0.0
        self.gain = 1.0
        self.muted = False
        self.lock = threading.Lock()

    def callback(self, indata, frames, time_info, status):
        if status:
            pass
        try:
            samples = np.asarray(indata, dtype=np.float32)
            if samples.size == 0:
                return
            rms = float(np.sqrt(np.mean(np.square(samples))))
            peak = float(np.max(np.abs(samples)))
            with self.lock:
                self.level = min(1.0, rms * self.gain)
                self.peak = min(1.0, peak * self.gain)
        except Exception:
            return

    def start(self):
        if self.stream is not None:
            return
        self.stream = sd.InputStream(
            device=self.device_index,
            channels=min(self.channels, 2),
            samplerate=None,
            blocksize=512,
            dtype="float32",
            callback=self.callback,
        )
        self.stream.start()

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None

    def snapshot(self):
        with self.lock:
            return self.level, self.peak, self.gain, self.muted


class Meter(tk.Canvas):
    def __init__(self, master, width=34, height=170, **kwargs):
        super().__init__(master, width=width, height=height, bg=METER_BG, highlightthickness=0, **kwargs)
        self.width = width
        self.height = height
        self.value = 0.0
        self.peak = 0.0
        self.draw()

    def set_value(self, value, peak=None):
        self.value = max(0.0, min(1.0, float(value)))
        self.peak = max(0.0, min(1.0, float(self.value if peak is None else peak)))
        self.draw()

    def draw(self):
        self.delete("all")
        h = self.height - 8
        y0 = 4
        filled = h * self.value
        segments = 28
        gap = 2
        seg_h = max(2, (h - gap * (segments - 1)) / segments)
        for i in range(segments):
            bottom = y0 + h - i * (seg_h + gap)
            top = bottom - seg_h
            fraction = (i + 1) / segments
            if fraction > self.value:
                color = "#2a313c"
            elif fraction > 0.90:
                color = METER_CLIP
            elif fraction > 0.72:
                color = METER_WARN
            else:
                color = METER_OK
            self.create_rectangle(5, top, self.width - 5, bottom, fill=color, outline="")
        if self.peak > 0.02:
            py = y0 + h - h * self.peak
            self.create_rectangle(3, py, self.width - 3, py + 2, fill=METER_CLIP, outline="")


class ChannelCard(ttk.Frame):
    def __init__(self, master, channel, on_remove=None):
        super().__init__(master, style="Mixer.TFrame", padding=10)
        self.channel = channel
        self.on_remove = on_remove
        self.mute_var = tk.BooleanVar(value=False)
        self.gain_var = tk.DoubleVar(value=0.0)
        self.name_var = tk.StringVar(value=channel.name)
        self.db_var = tk.StringVar(value="-∞ dB")

        ttk.Label(self, textvariable=self.name_var, style="ChannelTitle.TLabel", wraplength=150).pack(fill="x")
        ttk.Label(self, text=f"Input {channel.device_index}", style="Muted.TLabel").pack(anchor="w", pady=(1, 8))

        self.meter = Meter(self, width=36, height=190)
        self.meter.pack(pady=(0, 8))

        ttk.Label(self, textvariable=self.db_var, style="MeterDb.TLabel").pack()

        self.gain = ttk.Scale(self, from_=-60, to=12, variable=self.gain_var, command=self._gain_changed, orient="horizontal")
        self.gain.pack(fill="x", pady=(10, 3))
        ttk.Label(self, text="Ganancia", style="Muted.TLabel").pack()

        self.mute = ttk.Checkbutton(self, text="MUTE", variable=self.mute_var, command=self._mute_changed, style="Mute.TCheckbutton")
        self.mute.pack(pady=(10, 5))
        if on_remove:
            ttk.Button(self, text="Quitar", command=self._remove).pack(fill="x")

    def _gain_changed(self, value):
        db = float(value)
        self.channel.gain = math.pow(10.0, db / 20.0)

    def _mute_changed(self):
        self.channel.muted = self.mute_var.get()

    def _remove(self):
        if self.on_remove:
            self.on_remove(self)

    def update_meter(self):
        level, peak, gain, muted = self.channel.snapshot()
        shown = 0.0 if muted else level
        self.meter.set_value(shown, 0.0 if muted else peak)
        if shown <= 0.0001:
            self.db_var.set("-∞ dB")
        else:
            db = 20.0 * math.log10(max(shown, 1e-6))
            self.db_var.set(f"{db:5.1f} dB")

    def stop(self):
        self.channel.stop()


class AudioMixer(ttk.Frame):
    """OBS-style real-time input mixer for FlowRecorder.

    It monitors Windows input devices with sounddevice, displays live RMS/peak
    meters, and provides per-channel gain and mute controls. It intentionally
    does not change the operating-system microphone volume.
    """

    def __init__(self, master, status_callback=None):
        super().__init__(master, style="RootMixer.TFrame", padding=12)
        self.status_callback = status_callback or (lambda text: None)
        self.channels = []
        self.cards = []
        self.device_map = []
        self.running = False
        self._build()
        self.refresh_devices()
        self.after(60, self._tick)

    def _build(self):
        toolbar = ttk.Frame(self, style="RootMixer.TFrame")
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Label(toolbar, text="MEZCLADOR DE AUDIO", style="MixerHeader.TLabel").pack(side="left")
        ttk.Button(toolbar, text="Actualizar dispositivos", command=self.refresh_devices).pack(side="left", padx=12)
        self.start_btn = ttk.Button(toolbar, text="▶ Iniciar monitoreo", command=self.toggle_monitoring, style="Accent.TButton")
        self.start_btn.pack(side="right")

        ttk.Label(self, text="Entradas disponibles", style="Muted.TLabel").pack(anchor="w", pady=(0, 4))
        row = ttk.Frame(self, style="RootMixer.TFrame")
        row.pack(fill="x")
        self.device_combo = ttk.Combobox(row, state="readonly", width=65)
        self.device_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="＋ Añadir canal", command=self.add_channel).pack(side="left", padx=(8, 0))

        self.scroller = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="horizontal", command=self.scroller.xview)
        self.channel_frame = ttk.Frame(self.scroller, style="RootMixer.TFrame")
        self.channel_frame.bind("<Configure>", lambda e: self.scroller.configure(scrollregion=self.scroller.bbox("all")))
        self.scroller.create_window((0, 0), window=self.channel_frame, anchor="nw")
        self.scroller.configure(xscrollcommand=self.scrollbar.set)
        self.scroller.pack(fill="both", expand=True, pady=(12, 0))
        self.scrollbar.pack(fill="x")

        bottom = ttk.Frame(self, style="RootMixer.TFrame")
        bottom.pack(fill="x", pady=(10, 0))
        ttk.Label(bottom, text="MASTER", style="ChannelTitle.TLabel").pack(side="left")
        self.master_meter = Meter(bottom, width=46, height=70)
        self.master_meter.pack(side="left", padx=12)
        self.master_db = tk.StringVar(value="-∞ dB")
        ttk.Label(bottom, textvariable=self.master_db, style="MeterDb.TLabel").pack(side="left")
        ttk.Label(bottom, text="Los controles del mezclador afectan la monitorización visual; el volumen físico del sistema no se modifica.", style="Muted.TLabel").pack(side="right")

    def refresh_devices(self):
        try:
            devices = sd.query_devices()
        except Exception as e:
            messagebox.showerror("Audio", f"No se pudieron consultar los dispositivos de audio.\n\n{e}")
            return
        self.device_map = []
        labels = []
        for idx, d in enumerate(devices):
            if int(d.get("max_input_channels", 0)) <= 0:
                continue
            name = str(d.get("name", f"Dispositivo {idx}"))
            chans = int(d.get("max_input_channels", 1))
            self.device_map.append((idx, name, chans))
            labels.append(f"{idx} — {name} ({min(chans, 2)} ch)")
        self.device_combo["values"] = labels
        if labels:
            self.device_combo.current(0)
            self.status_callback(f"{len(labels)} entradas de audio detectadas")
        else:
            self.device_combo.set("")
            self.status_callback("No se encontraron entradas de audio")

    def add_channel(self):
        pos = self.device_combo.current()
        if pos < 0 or pos >= len(self.device_map):
            messagebox.showwarning("Mezclador", "Selecciona primero un dispositivo de entrada.")
            return
        idx, name, chans = self.device_map[pos]
        if any(c.device_index == idx for c in self.channels):
            messagebox.showinfo("Mezclador", "Ese dispositivo ya está añadido al mezclador.")
            return
        channel = AudioChannel(idx, name, chans)
        card = ChannelCard(self.channel_frame, channel, self.remove_channel)
        card.pack(side="left", fill="y", padx=6, pady=6)
        self.channels.append(channel)
        self.cards.append(card)
        self.status_callback(f"Canal añadido: {name}")
        if self.running:
            try:
                channel.start()
            except Exception as e:
                self.running = False
                self.start_btn.configure(text="▶ Iniciar monitoreo")
                card.stop()
                messagebox.showerror("Audio", f"No se pudo abrir '{name}'.\n\n{e}")

    def remove_channel(self, card):
        if card not in self.cards:
            return
        card.stop()
        i = self.cards.index(card)
        self.cards.pop(i)
        self.channels.pop(i)
        card.destroy()

    def toggle_monitoring(self):
        if not self.channels:
            messagebox.showwarning("Mezclador", "Añade al menos un dispositivo de entrada.")
            return
        if self.running:
            for c in self.channels:
                c.stop()
            self.running = False
            self.start_btn.configure(text="▶ Iniciar monitoreo")
            self.status_callback("Monitoreo detenido")
            return
        try:
            started = []
            for c in self.channels:
                c.start()
                started.append(c)
            self.running = True
            self.start_btn.configure(text="■ Detener monitoreo")
            self.status_callback("Monitoreo de audio activo")
        except Exception as e:
            for c in started:
                c.stop()
            self.running = False
            self.start_btn.configure(text="▶ Iniciar monitoreo")
            messagebox.showerror("Audio", f"No se pudo iniciar el monitoreo.\n\n{e}")

    def _tick(self):
        total = 0.0
        peak = 0.0
        for card in list(self.cards):
            card.update_meter()
            level, pk, _, muted = card.channel.snapshot()
            if not muted:
                total += level * level
                peak = max(peak, pk)
        master = min(1.0, math.sqrt(total)) if total else 0.0
        self.master_meter.set_value(master, peak)
        self.master_db.set("-∞ dB" if master <= 0.0001 else f"{20 * math.log10(max(master, 1e-6)):5.1f} dB")
        self.after(60, self._tick)

    def shutdown(self):
        for c in self.channels:
            c.stop()
        self.running = False


if __name__ == "__main__":
    root = tk.Tk()
    root.title("FlowRecorder — Mezclador de audio")
    root.geometry("1100x650")
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL)
    style.configure("RootMixer.TFrame", background=BG)
    style.configure("Mixer.TFrame", background=PANEL)
    style.configure("MixerHeader.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 16, "bold"))
    style.configure("ChannelTitle.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI", 10, "bold"))
    style.configure("Muted.TLabel", background=BG, foreground=MUTED)
    style.configure("MeterDb.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI", 9, "bold"))
    style.configure("Mute.TCheckbutton", background=PANEL, foreground=TEXT)
    style.configure("Accent.TButton", background=ACCENT, foreground="white")
    mixer = AudioMixer(root)
    mixer.pack(fill="both", expand=True)
    root.protocol("WM_DELETE_WINDOW", lambda: (mixer.shutdown(), root.destroy()))
    root.mainloop()
