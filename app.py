import json, mimetypes, os, threading, time, tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
import requests
from audio_mixer import AudioMixer
from studio_recorder import StudioRecorder

API_BASE = "https://api.fish.audio"
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "FlowRecorder"
CONFIG_FILE = APP_DIR / "config.json"


class FishAPI:
    def __init__(self, key):
        self.key = (key or "").strip()

    def headers(self, extra=None):
        h = {"Authorization": f"Bearer {self.key}", "User-Agent": "FlowRecorder/3.0", "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def err(self, r, action):
        try:
            data = r.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            detail = data.get("message") or data.get("reason") or data.get("detail") or data
        else:
            detail = r.text.strip() or "Sin detalles"
        if r.status_code == 401:
            raise RuntimeError("API key no válida o caducada.")
        if r.status_code == 402:
            raise RuntimeError("Fish Audio rechazó la operación por créditos o cuota insuficientes.")
        if r.status_code == 429:
            raise RuntimeError("Fish Audio está limitando solicitudes. Espera unos segundos.")
        raise RuntimeError(f"Error de Fish Audio ({r.status_code}) en {action}: {detail}")

    def test(self):
        r = requests.get(API_BASE + "/model", headers=self.headers(), timeout=(10, 30))
        if not r.ok:
            self.err(r, "la API key")

    def tts(self, payload, model):
        h = self.headers({"Content-Type": "application/json", "model": model or "s2-pro", "Accept": "audio/mpeg, audio/wav, application/octet-stream"})
        r = requests.post(API_BASE + "/v1/tts", headers=h, json=payload, timeout=(15, 180))
        if not r.ok:
            self.err(r, "generación de audio")
        if not r.content:
            raise RuntimeError("Fish Audio respondió sin audio.")
        return r.content

    def asr(self, path, lang="", timestamps=False):
        p = Path(path)
        if not p.is_file() or p.stat().st_size == 0:
            raise RuntimeError("El archivo de audio no existe o está vacío.")
        data = {"ignore_timestamps": "false" if timestamps else "true"}
        if lang.strip():
            data["language"] = lang.strip().lower()
        ct = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        for attempt in range(3):
            with p.open("rb") as fh:
                r = requests.post(API_BASE + "/v1/asr", headers=self.headers(), data=data, files={"audio": (p.name, fh, ct)}, timeout=(15, 120))
            if r.status_code == 503 and attempt < 2:
                time.sleep(2)
                continue
            if not r.ok:
                self.err(r, "transcripción")
            try:
                result = r.json()
            except Exception:
                raise RuntimeError("Fish Audio devolvió una respuesta no JSON.")
            if "text" not in result:
                raise RuntimeError(f"Respuesta inesperada: {result!r}")
            return result
        raise RuntimeError("No se pudo completar la transcripción.")

    def models(self):
        r = requests.get(API_BASE + "/model", headers=self.headers(), timeout=(10, 60))
        if not r.ok:
            self.err(r, "lista de modelos")
        return r.json()

    def delete_model(self, mid):
        r = requests.delete(API_BASE + f"/model/{mid}", headers=self.headers(), timeout=(10, 60))
        if not r.ok:
            self.err(r, "eliminación del modelo")

    def create_model(self, title, visibility, description, tags, files, texts):
        data = {"type": "tts", "title": title, "train_mode": "fast", "visibility": visibility, "description": description, "enhance_audio_quality": "true", "generate_sample": "false"}
        for x in tags.split(","):
            if x.strip():
                data.setdefault("tags", []).append(x.strip())
        for x in texts:
            data.setdefault("texts", []).append(x)
        opened, multipart = [], []
        try:
            for p in files:
                fh = open(p, "rb")
                opened.append(fh)
                multipart.append(("voices", (Path(p).name, fh, mimetypes.guess_type(p)[0] or "audio/wav")))
            r = requests.post(API_BASE + "/model", headers=self.headers(), data=data, files=multipart, timeout=(15, 300))
        finally:
            for f in opened:
                f.close()
        if not r.ok:
            self.err(r, "creación del clon")
        return r.json()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FlowRecorder — OBS Studio")
        self.geometry("1400x900")
        self.minsize(1150, 760)
        self.configure(bg="#0b0f14")
        self.api = None
        self.status = tk.StringVar(value="Sin API key")
        self.mixer = None
        self.studio = None
        self._style()
        self._build()
        self._load_key()
        self.protocol("WM_DELETE_WINDOW", self.close)

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background="#0b0f14", foreground="#e8ecf1", fieldbackground="#171d27")
        s.configure("TNotebook", background="#0b0f14", borderwidth=0)
        s.configure("TNotebook.Tab", padding=(18, 11), background="#171d27", foreground="#9aa4b2")
        s.map("TNotebook.Tab", background=[("selected", "#252d3a")], foreground=[("selected", "#fff")])
        s.configure("TButton", padding=(12, 8), background="#273142", foreground="#fff")
        s.configure("Accent.TButton", background="#6d5dfc", foreground="#fff")

    def _build(self):
        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="FLOWRECORDER", font=("Segoe UI", 20, "bold")).pack(side="left")
        ttk.Label(top, text="OBS-style Creator Studio", foreground="#8f9aaa").pack(side="left", padx=12)
        ttk.Label(top, textvariable=self.status).pack(side="right")
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.studio_tab = ttk.Frame(self.nb, padding=0)
        self.mixer_tab = ttk.Frame(self.nb, padding=0)
        self.tts_tab = ttk.Frame(self.nb, padding=14)
        self.asr_tab = ttk.Frame(self.nb, padding=14)
        self.voices_tab = ttk.Frame(self.nb, padding=14)
        self.settings_tab = ttk.Frame(self.nb, padding=14)
        for tab, name in [(self.studio_tab, "🎥 Estudio"), (self.mixer_tab, "🎚 Mezclador"), (self.tts_tab, "Texto → Voz"), (self.asr_tab, "Audio → Texto"), (self.voices_tab, "Voces"), (self.settings_tab, "Configuración")]:
            self.nb.add(tab, text=name)
        self._studio()
        self._mixer()
        self._tts()
        self._asr()
        self._voices()
        self._settings()

    def _studio(self):
        self.studio = StudioRecorder(self.studio_tab, self.status.set)

    def _mixer(self):
        self.mixer = AudioMixer(self.mixer_tab, status_callback=self.status.set)
        self.mixer.pack(fill="both", expand=True)

    def bg(self, fn, ok=None, label="Trabajando…"):
        self.status.set(label)
        def worker():
            try:
                result = fn()
                if ok:
                    self.after(0, lambda: ok(result))
            except Exception as e:
                self.after(0, lambda: self.error(str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def error(self, msg):
        self.status.set("Error")
        messagebox.showerror("FlowRecorder", msg)

    def need_api(self):
        if not self.api:
            messagebox.showwarning("API key", "Configura tu API key en Configuración.")
            self.nb.select(self.settings_tab)
            return False
        return True

    def _tts(self):
        f = self.tts_tab
        left = ttk.Frame(f); left.pack(side="left", fill="both", expand=True, padx=(0, 10))
        right = ttk.Frame(f); right.pack(side="right", fill="y")
        ttk.Label(left, text="Texto", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.text = tk.Text(left, bg="#171d27", fg="#e8ecf1", insertbackground="white", relief="flat")
        self.text.pack(fill="both", expand=True, pady=8)
        self.text.insert("1.0", "Hola, este es un ejemplo de FlowRecorder.")
        self.tts_vars = {}
        for label, key, val in [("Modelo", "model", "s2-pro"), ("Voice / reference_id", "voice", ""), ("Formato", "format", "mp3"), ("Velocidad", "speed", "1.0")]:
            ttk.Label(right, text=label).pack(anchor="w", pady=(7, 2))
            v = tk.StringVar(value=val); self.tts_vars[key] = v
            ttk.Entry(right, textvariable=v, width=28).pack(anchor="w")
        self.tts_out = tk.StringVar(value=str(Path.home() / "Downloads" / "flowrecorder_audio.mp3"))
        ttk.Label(right, text="Salida").pack(anchor="w", pady=(10, 2))
        ttk.Entry(right, textvariable=self.tts_out, width=32).pack(fill="x")
        ttk.Button(right, text="Generar audio", style="Accent.TButton", command=self.generate).pack(fill="x", pady=18)

    def generate(self):
        if not self.need_api(): return
        text = self.text.get("1.0", "end").strip()
        if not text: return messagebox.showwarning("Texto", "Escribe texto.")
        p = {"text": text, "format": self.tts_vars["format"].get().strip() or "mp3"}
        voice = self.tts_vars["voice"].get().strip()
        if voice: p["reference_id"] = voice
        try: p["prosody"] = {"speed": float(self.tts_vars["speed"].get())}
        except ValueError: return messagebox.showerror("Parámetros", "La velocidad no es válida.")
        out = Path(self.tts_out.get().strip())
        self.bg(lambda: self.api.tts(p, self.tts_vars["model"].get()), lambda data: self._save_tts(data, out), "Generando audio…")

    def _save_tts(self, data, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        self.status.set(f"Audio listo: {out}")
        messagebox.showinfo("Listo", f"Audio guardado en:\n{out}")

    def _asr(self):
        f = self.asr_tab
        ttk.Label(f, text="Transcripción", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        row = ttk.Frame(f); row.pack(fill="x", pady=12)
        self.asr_file = tk.StringVar()
        ttk.Entry(row, textvariable=self.asr_file).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Seleccionar audio", command=self._pick_asr).pack(side="left", padx=8)
        opts = ttk.Frame(f); opts.pack(fill="x")
        self.asr_lang = tk.StringVar(value="es")
        ttk.Label(opts, text="Idioma").pack(side="left")
        ttk.Entry(opts, textvariable=self.asr_lang, width=10).pack(side="left", padx=6)
        self.asr_ts = tk.BooleanVar()
        ttk.Checkbutton(opts, text="Timestamps", variable=self.asr_ts).pack(side="left")
        ttk.Button(f, text="TRANSCRIBIR", style="Accent.TButton", command=self.transcribe).pack(anchor="w", pady=14)
        self.asr_text = tk.Text(f, bg="#171d27", fg="#e8ecf1", relief="flat")
        self.asr_text.pack(fill="both", expand=True)

    def _pick_asr(self):
        p = filedialog.askopenfilename(filetypes=[("Audio", "*.wav *.mp3 *.m4a *.flac *.ogg"), ("Todos", "*.*")])
        if p: self.asr_file.set(p)

    def transcribe(self):
        if not self.need_api(): return
        p = self.asr_file.get().strip()
        if not p: return messagebox.showwarning("Audio", "Selecciona un audio.")
        self.bg(lambda: self.api.asr(p, self.asr_lang.get(), self.asr_ts.get()), lambda r: self._show_asr(r), "Transcribiendo…")

    def _show_asr(self, r):
        self.asr_text.delete("1.0", "end")
        self.asr_text.insert("1.0", r.get("text", ""))
        self.status.set("Transcripción lista")

    def _voices(self):
        top = ttk.Frame(self.voices_tab); top.pack(fill="x")
        ttk.Button(top, text="Actualizar", command=self.load_models).pack(side="left")
        ttk.Button(top, text="Crear clon", command=self.clone).pack(side="left", padx=6)
        ttk.Button(top, text="Eliminar", command=self.delete_model).pack(side="left")
        self.models = tk.Listbox(self.voices_tab, bg="#171d27", fg="#e8ecf1", selectbackground="#6d5dfc")
        self.models.pack(fill="both", expand=True, pady=10)
        self.model_data = []

    def load_models(self):
        if self.need_api(): self.bg(self.api.models, self.show_models, "Cargando modelos…")

    def show_models(self, r):
        self.model_data = r if isinstance(r, list) else r.get("items", r.get("data", []))
        self.models.delete(0, "end")
        for m in self.model_data:
            self.models.insert("end", f"{m.get('_id','')} — {m.get('title','sin título')} [{m.get('state','')}]")
        self.status.set(f"{len(self.model_data)} modelos")

    def delete_model(self):
        if not self.need_api(): return
        s = self.models.curselection()
        if not s: return
        mid = self.model_data[s[0]].get("_id")
        self.bg(lambda: self.api.delete_model(mid), lambda _: self.load_models(), "Eliminando…")

    def clone(self):
        if not self.need_api(): return
        w = tk.Toplevel(self); w.title("Crear clon"); w.geometry("650x560"); vs = {}
        for label, key, val in [("Título", "title", ""), ("Descripción", "description", ""), ("Visibilidad", "visibility", "private"), ("Tags", "tags", "")]:
            ttk.Label(w, text=label).pack(anchor="w", padx=14, pady=(10, 2))
            v = tk.StringVar(value=val); vs[key] = v
            ttk.Entry(w, textvariable=v).pack(fill="x", padx=14)
        files = tk.StringVar()
        ttk.Button(w, text="Seleccionar audios", command=lambda: files.set("|".join(filedialog.askopenfilenames(filetypes=[("Audio", "*.wav *.mp3 *.m4a *.flac"), ("Todos", "*.*")])))).pack(anchor="w", padx=14, pady=10)
        ttk.Entry(w, textvariable=files).pack(fill="x", padx=14)
        txt = tk.Text(w, height=8, bg="#171d27", fg="#e8ecf1"); txt.pack(fill="both", expand=True, padx=14, pady=10)
        def go():
            ps = [x for x in files.get().split("|") if x]
            if not vs["title"].get() or not ps: return messagebox.showwarning("Datos", "Título y audio son obligatorios.")
            texts = [x for x in txt.get("1.0", "end").splitlines() if x.strip()]
            title, visibility, description, tags = vs["title"].get(), vs["visibility"].get(), vs["description"].get(), vs["tags"].get()
            w.destroy()
            self.bg(lambda: self.api.create_model(title, visibility, description, tags, ps, texts), lambda _: self.load_models(), "Creando clon…")
        ttk.Button(w, text="CREAR MODELO", style="Accent.TButton", command=go).pack(pady=12)

    def _settings(self):
        f = self.settings_tab
        ttk.Label(f, text="Configuración", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(f, text="API key de Fish Audio").pack(anchor="w", pady=(18, 4))
        self.key = tk.StringVar()
        ttk.Entry(f, textvariable=self.key, show="•", width=70).pack(anchor="w")
        ttk.Button(f, text="Guardar y probar", style="Accent.TButton", command=self.save_key).pack(anchor="w", pady=10)
        ttk.Label(f, text="La clave queda guardada localmente en Windows.", foreground="#8f9aaa").pack(anchor="w")
        ttk.Button(f, text="Abrir API keys", command=lambda: os.startfile("https://fish.audio/app/api-keys/")).pack(anchor="w", pady=10)

    def _load_key(self):
        try:
            if CONFIG_FILE.is_file():
                k = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("api_key", "")
                self.key.set(k)
                if k:
                    self.api = FishAPI(k); self.status.set("API key cargada")
        except Exception:
            pass

    def save_key(self):
        k = self.key.get().strip()
        if not k: return messagebox.showwarning("API key", "Pega tu API key.")
        api = FishAPI(k)
        def ok(_):
            APP_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps({"api_key": k}), encoding="utf-8")
            self.api = api
            self.status.set("API key configurada")
            messagebox.showinfo("Listo", "API key válida y guardada.")
        self.bg(api.test, ok, "Comprobando API key…")

    def close(self):
        try:
            self.studio.shutdown()
            self.mixer.shutdown()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
