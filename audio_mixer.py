import math
import threading
import wave
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import numpy as np
import sounddevice as sd

try:
    import soundcard as sc
except Exception:
    sc = None

BG = "#0f131a"
PANEL = "#171d27"
TEXT = "#e8ecf1"
MUTED = "#8f9aaa"
ACCENT = "#6d5dfc"
METER_BG = "#242b36"
METER_OK = "#35d07f"
METER_WARN = "#f5c542"
METER_CLIP = "#ff5b5b"


def _db(v):
    return "-∞ dB" if v <= 0.0001 else f"{20.0 * math.log10(max(v, 1e-6)):5.1f} dB"


def _pcm16(data):
    x = np.asarray(data, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    x = np.clip(x, -1.0, 1.0)
    return (x * 32767.0).astype(np.int16).tobytes()


def _read_wav(path):
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    arr = arr.reshape(-1, channels) if channels else np.zeros((0, 1), dtype=np.float32)
    return arr, rate


def _write_wav(path, data, rate=48000):
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr[:, None]
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(arr.shape[1])
        wf.setsampwidth(2)
        wf.setframerate(int(rate))
        wf.writeframes(_pcm16(arr))


def _resample(data, src_rate, dst_rate):
    if not len(data) or int(src_rate) == int(dst_rate):
        return data
    n = max(1, int(round(len(data) * float(dst_rate) / float(src_rate))))
    old_x = np.linspace(0.0, 1.0, len(data), endpoint=False)
    new_x = np.linspace(0.0, 1.0, n, endpoint=False)
    out = np.empty((n, data.shape[1]), dtype=np.float32)
    for ch in range(data.shape[1]):
        out[:, ch] = np.interp(new_x, old_x, data[:, ch])
    return out


class AudioChannel:
    def __init__(self, kind, device_id, name, channels=2, rate=48000):
        self.kind = kind
        self.device_id = device_id
        self.name = name
        self.channels = max(1, min(int(channels or 1), 2))
        self.rate = int(rate or 48000)
        self.stream = None
        self.recorder = None
        self.thread = None
        self.stop_event = threading.Event()
        self.level = 0.0
        self.peak = 0.0
        self.gain = 1.0
        self.muted = False
        self.lock = threading.Lock()
        self.record_wave = None
        self.record_path = None
        self.loopback_error = None

    def _update_level(self, samples):
        samples = np.asarray(samples, dtype=np.float32)
        if samples.size == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(samples))))
        peak = float(np.max(np.abs(samples)))
        with self.lock:
            self.level = min(1.0, rms * self.gain)
            self.peak = min(1.0, peak * self.gain)

    def _record_chunk(self, data):
        x = np.asarray(data, dtype=np.float32)
        if x.ndim == 1:
            x = x[:, None]
        if x.shape[1] > 2:
            x = x[:, :2]
        if x.shape[1] == 1 and self.channels == 2:
            x = np.repeat(x, 2, axis=1)
        with self.lock:
            wf = self.record_wave
            gain = 0.0 if self.muted else self.gain
            if wf is not None:
                wf.writeframes(_pcm16(x * gain))

    def _input_callback(self, indata, frames, time_info, status):
        self._update_level(indata)
        self._record_chunk(indata)

    def _loopback_worker(self):
        try:
            with self.recorder:
                while not self.stop_event.is_set():
                    data = self.recorder.record(numframes=1024)
                    self._update_level(data)
                    self._record_chunk(data)
        except Exception as e:
            self.loopback_error = str(e)
            self.stop_event.set()

    def start(self):
        if self.stream is not None or self.thread is not None:
            return
        self.stop_event.clear()
        self.loopback_error = None
        if self.kind == "mic":
            self.stream = sd.InputStream(
                device=self.device_id,
                channels=self.channels,
                samplerate=self.rate,
                blocksize=512,
                dtype="float32",
                callback=self._input_callback,
            )
            self.stream.start()
        else:
            if sc is None:
                raise RuntimeError("La captura de audio del escritorio requiere el paquete soundcard.")
            self.recorder = self.device_id.recorder(
                samplerate=self.rate,
                channels=self.channels,
                blocksize=1024,
            )
            self.thread = threading.Thread(target=self._loopback_worker, daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None
        if self.thread is not None:
            self.thread.join(timeout=2.0)
            self.thread = None
        self.recorder = None
        self._close_record_file()

    def begin_record(self, path):
        self._close_record_file()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        wf = wave.open(str(path), "wb")
        wf.setnchannels(2 if self.channels > 1 else 1)
        wf.setsampwidth(2)
        wf.setframerate(self.rate)
        with self.lock:
            self.record_path = Path(path)
            self.record_wave = wf

    def _close_record_file(self):
        with self.lock:
            wf = self.record_wave
            self.record_wave = None
        if wf is not None:
            try:
                wf.close()
            except Exception:
                pass

    def snapshot(self):
        with self.lock:
            return self.level, self.peak, self.gain, self.muted


class Meter(tk.Canvas):
    def __init__(self, master, width=34, height=170, **kwargs):
        super().__init__(master, width=width, height=height, bg=METER_BG, highlightthickness=0, **kwargs)
        self.width, self.height = width, height
        self.value, self.peak = 0.0, 0.0
        self.draw()

    def set_value(self, value, peak=None):
        self.value = max(0.0, min(1.0, float(value)))
        self.peak = max(0.0, min(1.0, float(self.value if peak is None else peak)))
        self.draw()

    def draw(self):
        self.delete("all")
        h, y0, segments, gap = self.height - 8, 4, 28, 2
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
        ttk.Label(self, textvariable=self.name_var, style="ChannelTitle.TLabel", wraplength=170).pack(fill="x")
        ttk.Label(self, text="Audio de escritorio" if channel.kind == "desktop" else f"Input {channel.device_id}", style="Muted.TLabel").pack(anchor="w", pady=(1, 8))
        self.meter = Meter(self, width=36, height=190)
        self.meter.pack(pady=(0, 8))
        ttk.Label(self, textvariable=self.db_var, style="MeterDb.TLabel").pack()
        ttk.Scale(self, from_=-60, to=12, variable=self.gain_var, command=self._gain_changed, orient="horizontal").pack(fill="x", pady=(10, 3))
        ttk.Label(self, text="Ganancia", style="Muted.TLabel").pack()
        ttk.Checkbutton(self, text="MUTE", variable=self.mute_var, command=self._mute_changed, style="Mute.TCheckbutton").pack(pady=(10, 5))
        if on_remove:
            ttk.Button(self, text="Quitar", command=lambda: on_remove(self)).pack(fill="x")

    def _gain_changed(self, value):
        self.channel.gain = math.pow(10.0, float(value) / 20.0)

    def _mute_changed(self):
        self.channel.muted = self.mute_var.get()

    def update_meter(self):
        level, peak, _, muted = self.channel.snapshot()
        shown = 0.0 if muted else level
        self.meter.set_value(shown, 0.0 if muted else peak)
        self.db_var.set(_db(shown))

    def stop(self):
        self.channel.stop()


class AudioMixer(ttk.Frame):
    """OBS-style mixer with microphones, Windows desktop loopback and WAV recording."""

    def __init__(self, master, status_callback=None):
        super().__init__(master, style="RootMixer.TFrame", padding=12)
        self.status_callback = status_callback or (lambda text: None)
        self.channels = []
        self.cards = []
        self.device_map = []
        self.desktop_map = []
        self.running = False
        self.recording = False
        self.record_temp = Path(tempfile.mkdtemp(prefix="flowrecorder_"))
        self._build()
        self.refresh_devices()
        self.after(60, self._tick)

    def _build(self):
        toolbar = ttk.Frame(self, style="RootMixer.TFrame")
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="MEZCLADOR DE AUDIO", style="MixerHeader.TLabel").pack(side="left")
        ttk.Button(toolbar, text="Actualizar", command=self.refresh_devices).pack(side="left", padx=10)
        self.start_btn = ttk.Button(toolbar, text="▶ Iniciar monitoreo", command=self.toggle_monitoring, style="Accent.TButton")
        self.start_btn.pack(side="right")

        source = ttk.Frame(self, style="RootMixer.TFrame")
        source.pack(fill="x", pady=(0, 8))
        ttk.Label(source, text="Micrófonos / entradas", style="Muted.TLabel").pack(anchor="w")
        row = ttk.Frame(source, style="RootMixer.TFrame")
        row.pack(fill="x")
        self.device_combo = ttk.Combobox(row, state="readonly", width=52)
        self.device_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="＋ Añadir entrada", command=self.add_input).pack(side="left", padx=6)
        self.desktop_combo = ttk.Combobox(row, state="readonly", width=46)
        self.desktop_combo.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Button(row, text="＋ Audio escritorio", command=self.add_desktop).pack(side="left", padx=6)

        self.scroller = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="horizontal", command=self.scroller.xview)
        self.channel_frame = ttk.Frame(self.scroller, style="RootMixer.TFrame")
        self.channel_frame.bind("<Configure>", lambda e: self.scroller.configure(scrollregion=self.scroller.bbox("all")))
        self.scroller.create_window((0, 0), window=self.channel_frame, anchor="nw")
        self.scroller.configure(xscrollcommand=self.scrollbar.set)
        self.scroller.pack(fill="both", expand=True, pady=(8, 0))
        self.scrollbar.pack(fill="x")

        bottom = ttk.Frame(self, style="RootMixer.TFrame")
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Label(bottom, text="MASTER", style="ChannelTitle.TLabel").pack(side="left")
        self.master_meter = Meter(bottom, width=46, height=70)
        self.master_meter.pack(side="left", padx=12)
        self.master_db = tk.StringVar(value="-∞ dB")
        ttk.Label(bottom, textvariable=self.master_db, style="MeterDb.TLabel").pack(side="left")
        rec = ttk.Frame(bottom, style="RootMixer.TFrame")
        rec.pack(side="right")
        self.rec_path = tk.StringVar(value=str(Path.home() / "Videos" / "FlowRecorder" / "grabacion.wav"))
        ttk.Entry(rec, textvariable=self.rec_path, width=36).pack(side="left", padx=5)
        ttk.Button(rec, text="…", command=self.pick_record_path).pack(side="left")
        self.rec_btn = ttk.Button(rec, text="● REC", command=self.toggle_recording, style="Accent.TButton")
        self.rec_btn.pack(side="left", padx=(8, 0))

    def refresh_devices(self):
        try:
            devices = sd.query_devices()
        except Exception as e:
            messagebox.showerror("Audio", f"No se pudieron consultar los dispositivos.\n\n{e}")
            return
        self.device_map = []
        labels = []
        for idx, d in enumerate(devices):
            if int(d.get("max_input_channels", 0)) <= 0:
                continue
            name = str(d.get("name", f"Dispositivo {idx}"))
            chans = max(1, min(int(d.get("max_input_channels", 1)), 2))
            rate = int(float(d.get("default_samplerate", 48000)))
            self.device_map.append((idx, name, chans, rate))
            labels.append(f"{idx} — {name} ({chans} ch)")
        self.device_combo["values"] = labels
        if labels:
            self.device_combo.current(0)

        self.desktop_map = []
        dlabels = []
        if sc is not None:
            try:
                loopbacks = sc.all_microphones(include_loopback=True)
                for mic in loopbacks:
                    if not getattr(mic, "isloopback", False):
                        continue
                    name = str(getattr(mic, "name", "Audio de escritorio"))
                    chans = max(2, min(int(getattr(mic, "channels", 2) or 2), 2))
                    self.desktop_map.append((mic, name, chans, 48000))
                    dlabels.append(f"{name} — Windows WASAPI")
            except Exception:
                dlabels = []
        self.desktop_combo["values"] = dlabels
        if dlabels:
            self.desktop_combo.current(0)
            self.status_callback(f"{len(labels)} entradas y {len(dlabels)} fuentes de escritorio detectadas")
        else:
            self.desktop_combo.set("")
            self.status_callback(f"{len(labels)} entradas detectadas; audio de escritorio no disponible")

    def add_input(self):
        pos = self.device_combo.current()
        if pos < 0 or pos >= len(self.device_map):
            messagebox.showwarning("Mezclador", "Selecciona un micrófono o entrada.")
            return
        idx, name, chans, rate = self.device_map[pos]
        if any(c.kind == "mic" and c.device_id == idx for c in self.channels):
            messagebox.showinfo("Mezclador", "Esa entrada ya está añadida.")
            return
        self._add_channel(AudioChannel("mic", idx, name, chans, rate))

    def add_desktop(self):
        pos = self.desktop_combo.current()
        if pos < 0 or pos >= len(self.desktop_map):
            messagebox.showwarning("Audio de escritorio", "No hay una fuente WASAPI loopback disponible en Windows.")
            return
        mic, name, chans, rate = self.desktop_map[pos]
        if any(c.kind == "desktop" and c.device_id is mic for c in self.channels):
            messagebox.showinfo("Mezclador", "Ese audio de escritorio ya está añadido.")
            return
        self._add_channel(AudioChannel("desktop", mic, name, chans, rate))

    def _add_channel(self, channel):
        card = ChannelCard(self.channel_frame, channel, self.remove_channel)
        card.pack(side="left", fill="y", padx=6, pady=6)
        self.channels.append(channel)
        self.cards.append(card)
        self.status_callback(f"Canal añadido: {channel.name}")
        if self.running:
            try:
                channel.start()
            except Exception as e:
                messagebox.showerror("Audio", f"No se pudo abrir '{channel.name}'.\n\n{e}")

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
            messagebox.showwarning("Mezclador", "Añade al menos un canal.")
            return
        if self.running:
            for c in self.channels:
                c.stop()
            self.running = False
            self.start_btn.configure(text="▶ Iniciar monitoreo")
            self.status_callback("Monitoreo detenido")
            return
        started = []
        try:
            for c in self.channels:
                c.start()
                started.append(c)
            self.running = True
            self.start_btn.configure(text="■ Detener monitoreo")
            self.status_callback("Monitoreo de micrófonos y escritorio activo")
        except Exception as e:
            for c in started:
                c.stop()
            self.running = False
            self.start_btn.configure(text="▶ Iniciar monitoreo")
            messagebox.showerror("Audio", f"No se pudo iniciar el monitoreo.\n\n{e}")

    def pick_record_path(self):
        p = filedialog.asksaveasfilename(defaultextension=".wav", initialfile="grabacion.wav", filetypes=[("WAV PCM", "*.wav")])
        if p:
            self.rec_path.set(p)

    def toggle_recording(self):
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        if not self.channels:
            messagebox.showwarning("Grabación", "Añade al menos un canal antes de grabar.")
            return
        if not self.running:
            self.toggle_monitoring()
            if not self.running:
                return
        out = Path(self.rec_path.get().strip())
        if not out.name:
            messagebox.showwarning("Grabación", "Elige un nombre de archivo.")
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        self.record_temp = Path(tempfile.mkdtemp(prefix="flowrecorder_"))
        try:
            for i, channel in enumerate(self.channels):
                channel.begin_record(self.record_temp / f"track_{i}.wav")
            self.recording = True
            self.rec_btn.configure(text="■ DETENER REC")
            self.status_callback("Grabando mezcla…")
        except Exception as e:
            for c in self.channels:
                c._close_record_file()
            messagebox.showerror("Grabación", f"No se pudo iniciar la grabación.\n\n{e}")

    def stop_recording(self):
        self.recording = False
        self.rec_btn.configure(text="● REC")
        for c in self.channels:
            c._close_record_file()
        self.status_callback("Mezclando pistas…")
        threading.Thread(target=self._mix_recorded_tracks, daemon=True).start()

    def _mix_recorded_tracks(self):
        try:
            tracks = []
            for i, channel in enumerate(self.channels):
                path = self.record_temp / f"track_{i}.wav"
                if path.exists():
                    data, rate = _read_wav(path)
                    data = _resample(data, rate, 48000)
                    if data.shape[1] == 1:
                        data = np.repeat(data, 2, axis=1)
                    tracks.append(data[:, :2])
            if not tracks:
                raise RuntimeError("No se grabaron datos de audio.")
            length = max(len(x) for x in tracks)
            mix = np.zeros((length, 2), dtype=np.float32)
            for data in tracks:
                mix[:len(data)] += data
            peak = float(np.max(np.abs(mix))) if mix.size else 0.0
            if peak > 0.98:
                mix *= 0.98 / peak
            output = Path(self.rec_path.get().strip())
            _write_wav(output, mix, 48000)
            self.after(0, lambda p=str(output): self.status_callback(f"Grabación guardada: {p}"))
            self.after(0, lambda p=str(output): messagebox.showinfo("Grabación lista", f"Se guardó la grabación en:\n{p}"))
        except Exception as e:
            self.after(0, lambda msg=str(e): messagebox.showerror("Grabación", msg))

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
        self.master_db.set(_db(master))
        self.after(60, self._tick)

    def shutdown(self):
        if self.recording:
            self.stop_recording()
        for c in self.channels:
            c.stop()
        self.running = False


if __name__ == "__main__":
    root = tk.Tk()
    root.title("FlowRecorder — Mezclador de audio")
    root.geometry("1250x700")
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
