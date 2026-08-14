import json
import mimetypes
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

import requests

API_BASE = "https://api.fish.audio"
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "FishAudioDesktop"
CONFIG_FILE = APP_DIR / "config.json"


class FishAPI:
    def __init__(self, key):
        self.key = (key or "").strip()

    def _headers(self, extra=None):
        h = {
            "Authorization": f"Bearer {self.key}",
            "User-Agent": "FishAudioDesktop/2.0",
            "Accept": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    def _error(self, response, action):
        try:
            detail = response.json()
        except ValueError:
            detail = response.text.strip()
        if response.status_code == 401:
            raise RuntimeError("API key no válida o caducada. Ve a Configuración y guarda una API key válida de Fish Audio.")
        if response.status_code == 402:
            raise RuntimeError("Fish Audio rechazó la operación por créditos/cuota insuficientes.")
        if response.status_code == 422:
            raise RuntimeError(f"Fish Audio rechazó los datos de {action}: {detail}")
        raise RuntimeError(f"Error de Fish Audio ({response.status_code}) en {action}: {detail}")

    def test_key(self):
        r = requests.get(API_BASE + "/model", headers=self._headers(), timeout=(10, 30))
        if not r.ok:
            self._error(r, "la API key")
        return True

    def tts(self, payload, model):
        # Fish Audio expects the TTS model in the HTTP header.
        headers = self._headers({
            "Content-Type": "application/json",
            "model": model or "s2-pro",
            "Accept": "audio/mpeg, audio/wav, audio/opus, application/octet-stream",
        })
        try:
            r = requests.post(
                API_BASE + "/v1/tts",
                headers=headers,
                json=payload,
                timeout=(15, 180),
            )
        except requests.Timeout:
            raise RuntimeError("La generación de audio superó los 180 segundos. Comprueba la conexión y vuelve a intentarlo.")
        except requests.RequestException as e:
            raise RuntimeError(f"No se pudo conectar con Fish Audio: {e}")
        if not r.ok:
            self._error(r, "generación de audio")
        if not r.content:
            raise RuntimeError("Fish Audio respondió sin audio.")
        return r.content, r.headers

    def asr_multipart(self, audio_path, language="", timestamps=False):
        path = Path(audio_path)
        if not path.is_file():
            raise RuntimeError(f"No se encontró el archivo de audio: {path}")
        lang = (language or "").strip().lower()
        language_map = {
            "español": "es", "espanol": "es", "spanish": "es",
            "inglés": "en", "ingles": "en", "english": "en",
            "francés": "fr", "frances": "fr", "french": "fr",
            "alemán": "de", "aleman": "de", "german": "de",
            "japonés": "ja", "japones": "ja", "japanese": "ja",
            "coreano": "ko", "korean": "ko",
            "chino": "zh", "chinese": "zh",
        }
        lang = language_map.get(lang, lang)
        data = {"ignore_timestamps": "false" if timestamps else "true"}
        if lang:
            data["language"] = lang
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            with path.open("rb") as fh:
                r = requests.post(
                    API_BASE + "/v1/asr",
                    headers=self._headers({"Accept": "application/json"}),
                    data=data,
                    files={"audio": (path.name, fh, ctype)},
                    timeout=(15, 120),
                )
        except requests.Timeout:
            raise RuntimeError("La transcripción superó los 120 segundos. Comprueba la conexión y vuelve a intentarlo.")
        except requests.RequestException as e:
            raise RuntimeError(f"No se pudo conectar con Fish Audio: {e}")
        if not r.ok:
            self._error(r, "transcripción")
        try:
            result = r.json()
        except ValueError:
            raise RuntimeError("Fish Audio devolvió una respuesta que no es JSON.")
        if "text" not in result:
            raise RuntimeError(f"Respuesta inesperada de Fish Audio: {result}")
        return result

    def models(self):
        try:
            r = requests.get(API_BASE + "/model", headers=self._headers(), timeout=(10, 60))
        except requests.RequestException as e:
            raise RuntimeError(f"No se pudo conectar con Fish Audio: {e}")
        if not r.ok:
            self._error(r, "lista de modelos")
        return r.json()

    def model(self, mid):
        r = requests.get(API_BASE + f"/model/{mid}", headers=self._headers(), timeout=(10, 60))
        if not r.ok:
            self._error(r, "modelo")
        return r.json()

    def delete_model(self, mid):
        r = requests.delete(API_BASE + f"/model/{mid}", headers=self._headers(), timeout=(10, 60))
        if not r.ok:
            self._error(r, "eliminación del modelo")
        return True

    def create_model_multipart(self, title, visibility, description, tags, files, texts):
        data = {
            "type": "tts",
            "title": title,
            "train_mode": "fast",
            "visibility": visibility,
            "description": description,
            "enhance_audio_quality": "true",
            "generate_sample": "false",
        }
        for tag in [x.strip() for x in tags.split(",") if x.strip()]:
            data.setdefault("tags", []).append(tag)
        for text in texts:
            data.setdefault("texts", []).append(text)
        opened = []
        try:
            multipart = []
            for p in files:
                fh = open(p, "rb")
                opened.append(fh)
                fn = os.path.basename(p)
                ct = mimetypes.guess_type(fn)[0] or "audio/wav"
                multipart.append(("voices", (fn, fh, ct)))
            r = requests.post(
                API_BASE + "/model",
                headers=self._headers({"Accept": "application/json"}),
                data=data,
                files=multipart,
                timeout=(15, 300),
            )
        except requests.Timeout:
            raise RuntimeError("La creación del clon superó los 300 segundos.")
        except requests.RequestException as e:
            raise RuntimeError(f"No se pudo conectar con Fish Audio: {e}")
        finally:
            for fh in opened:
                fh.close()
        if not r.ok:
            self._error(r, "creación del clon")
        return r.json()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fish Audio Desktop")
        self.geometry("1120x760")
        self.minsize(980, 650)
        self.configure(bg="#10131a")
        self.api = None
        self.status = tk.StringVar(value="Sin API key")
        self._style()
        self._build()
        self._load_saved_key()

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background="#10131a", foreground="#e8ecf1", fieldbackground="#181d27")
        s.configure("TNotebook", background="#10131a", borderwidth=0)
        s.configure("TNotebook.Tab", padding=(18, 10), background="#181d27", foreground="#b9c1cc")
        s.map("TNotebook.Tab", background=[("selected", "#252d3a")], foreground=[("selected", "#ffffff")])
        s.configure("TButton", padding=(12, 7), background="#2d3748", foreground="#fff")
        s.map("TButton", background=[("active", "#3b475a")])
        s.configure("Accent.TButton", background="#6d5dfc", foreground="#fff")
        s.configure("TEntry", padding=7)
        s.configure("TCombobox", padding=6)
        s.configure("TLabelframe", background="#10131a", foreground="#dce2ea")
        s.configure("TLabelframe.Label", background="#10131a", foreground="#dce2ea")

    def _build(self):
        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="FISH AUDIO DESKTOP", font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Label(top, textvariable=self.status).pack(side="right")
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.tts_tab = ttk.Frame(self.nb, padding=14)
        self.asr_tab = ttk.Frame(self.nb, padding=14)
        self.voices_tab = ttk.Frame(self.nb, padding=14)
        self.settings_tab = ttk.Frame(self.nb, padding=14)
        self.nb.add(self.tts_tab, text="Texto → Voz")
        self.nb.add(self.asr_tab, text="Audio → Texto")
        self.nb.add(self.voices_tab, text="Voces / Clonación")
        self.nb.add(self.settings_tab, text="Configuración")
        self._tts()
        self._asr()
        self._voices()
        self._settings()

    def need_api(self):
        if not self.api:
            messagebox.showwarning("API key", "Introduce tu API key de Fish Audio en Configuración.")
            self.nb.select(self.settings_tab)
            return False
        return True

    def run_bg(self, fn, ok=None, label=None):
        if label:
            self.status.set(label)
        def worker():
            try:
                result = fn()
                if ok:
                    self.after(0, lambda: ok(result))
            except Exception as e:
                self.after(0, lambda: self._show_error(str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def _show_error(self, text):
        self.status.set("Error")
        messagebox.showerror("Fish Audio", text)

    def _tts(self):
        f = self.tts_tab
        left = ttk.Frame(f)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = ttk.Frame(f)
        right.pack(side="right", fill="y", padx=(8, 0))
        ttk.Label(left, text="Texto", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.text = tk.Text(left, height=24, wrap="word", bg="#181d27", fg="#e8ecf1", insertbackground="white", relief="flat")
        self.text.pack(fill="both", expand=True, pady=8)
        self.text.insert("1.0", "Hola. Esta es una prueba de Fish Audio Desktop.")
        self.fields = {}
        def add(label, key, default, width=25):
            ttk.Label(right, text=label).pack(anchor="w", pady=(7, 2))
            v = tk.StringVar(value=default)
            self.fields[key] = v
            ttk.Entry(right, textvariable=v, width=width).pack(anchor="w")
        add("Modelo", "model", "s2-pro")
        add("Voice / reference_id", "voice", "")
        add("Formato", "format", "mp3")
        add("Velocidad (0.5–2)", "speed", "1.0")
        add("Volumen (-20–20)", "volume", "0")
        add("Temperatura", "temperature", "")
        add("Top P", "top_p", "")
        add("Chunk length", "chunk_length", "300")
        add("Latency", "latency", "normal")
        ttk.Label(right, text="Salida").pack(anchor="w", pady=(7, 2))
        self.out = tk.StringVar(value=str(Path.home() / "Downloads" / "fish_audio.mp3"))
        row = ttk.Frame(right)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.out, width=28).pack(side="left")
        ttk.Button(row, text="…", command=self.pick_out).pack(side="right")
        ttk.Button(right, text="GENERAR AUDIO", style="Accent.TButton", command=self.generate).pack(fill="x", pady=18)
        ttk.Label(right, text="TTS con reference_id, prosodia y parámetros avanzados.", foreground="#9aa4b2").pack(anchor="w")

    def pick_out(self):
        p = filedialog.asksaveasfilename(defaultextension=".mp3", filetypes=[("Audio", "*.mp3 *.wav *.opus"), ("Todos", "*.*")])
        if p:
            self.out.set(p)

    def generate(self):
        if not self.need_api():
            return
        text = self.text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("Texto vacío", "Escribe el texto que quieres convertir en audio.")
            return
        p = {"text": text, "format": self.fields["format"].get().strip() or "mp3"}
        voice = self.fields["voice"].get().strip()
        if voice:
            p["reference_id"] = voice
        pros = {}
        try:
            for k in ("speed", "volume"):
                x = self.fields[k].get().strip()
                if x:
                    pros[k] = float(x)
            if pros:
                p["prosody"] = pros
            for k in ("temperature", "top_p"):
                x = self.fields[k].get().strip()
                if x:
                    p[k] = float(x)
            x = self.fields["chunk_length"].get().strip()
            if x:
                p["chunk_length"] = int(x)
        except ValueError:
            messagebox.showerror("Parámetros inválidos", "Velocidad, volumen, temperatura, top-p y chunk length deben ser números válidos.")
            return
        latency = self.fields["latency"].get().strip()
        if latency:
            p["latency"] = latency
        model = self.fields["model"].get().strip() or "s2-pro"
        out = self.out.get().strip()
        if not out:
            messagebox.showwarning("Salida", "Selecciona dónde guardar el audio.")
            return
        self.run_bg(lambda: self.api.tts(p, model), self._tts_done, "Generando audio…")
        self._pending_out = out

    def _tts_done(self, result):
        data, _ = result
        out = self._pending_out
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(data)
        self.status.set("Audio generado")
        if messagebox.askyesno("Listo", "Audio guardado. ¿Abrirlo ahora?"):
            os.startfile(out)

    def _asr(self):
        f = self.asr_tab
        ttk.Label(f, text="Transcripción de audio", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=15)
        self.asr_file = tk.StringVar()
        ttk.Entry(row, textvariable=self.asr_file).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Seleccionar audio", command=self.select_audio).pack(side="left", padx=8)
        opts = ttk.Frame(f)
        opts.pack(fill="x")
        self.asr_lang = tk.StringVar(value="es")
        ttk.Label(opts, text="Idioma (código: es, en, zh…)").pack(side="left")
        ttk.Entry(opts, textvariable=self.asr_lang, width=15).pack(side="left", padx=8)
        self.timestamps = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Timestamps", variable=self.timestamps).pack(side="left")
        ttk.Button(f, text="TRANSCRIBIR", style="Accent.TButton", command=self.transcribe).pack(anchor="w", pady=16)
        self.asr_text = tk.Text(f, height=22, bg="#181d27", fg="#e8ecf1", relief="flat")
        self.asr_text.pack(fill="both", expand=True)

    def select_audio(self):
        p = filedialog.askopenfilename(filetypes=[("Audio", "*.wav *.mp3 *.m4a *.ogg *.flac"), ("Todos", "*.*")])
        if p:
            self.asr_file.set(p)

    def transcribe(self):
        if not self.need_api():
            return
        p = self.asr_file.get().strip()
        if not p:
            messagebox.showwarning("Audio", "Selecciona un archivo de audio.")
            return
        self.run_bg(lambda: self.api.asr_multipart(p, self.asr_lang.get(), self.timestamps.get()), self.show_asr, "Transcribiendo… (máx. 120 s)")

    def show_asr(self, r):
        self.asr_text.delete("1.0", "end")
        self.asr_text.insert("1.0", r.get("text", ""))
        self.status.set("Transcripción lista")

    def _voices(self):
        top = ttk.Frame(self.voices_tab)
        top.pack(fill="x")
        ttk.Button(top, text="Actualizar mis modelos", command=self.load_models).pack(side="left")
        ttk.Button(top, text="Crear clon", command=self.create_clone).pack(side="left", padx=8)
        ttk.Button(top, text="Eliminar seleccionado", command=self.delete_model).pack(side="left")
        self.models_list = tk.Listbox(self.voices_tab, bg="#181d27", fg="#e8ecf1", selectbackground="#6d5dfc", height=16)
        self.models_list.pack(fill="both", expand=True, pady=12)
        self.model_info = tk.Text(self.voices_tab, height=10, bg="#181d27", fg="#e8ecf1", relief="flat")
        self.model_info.pack(fill="x")
        self.model_data = []

    def load_models(self):
        if not self.need_api():
            return
        self.run_bg(self.api.models, self.show_models, "Cargando modelos…")

    def show_models(self, r):
        self.model_data = r if isinstance(r, list) else r.get("items", r.get("data", []))
        self.models_list.delete(0, "end")
        for m in self.model_data:
            self.models_list.insert("end", f'{m.get("_id", "")} — {m.get("title", "(sin título)")} [{m.get("state", "")}]')
        self.status.set(f"{len(self.model_data)} modelos")

    def delete_model(self):
        if not self.need_api():
            return
        sel = self.models_list.curselection()
        if not sel:
            return
        mid = self.model_data[sel[0]].get("_id")
        if messagebox.askyesno("Confirmar", f"Eliminar {mid}?"):
            self.run_bg(lambda: self.api.delete_model(mid), lambda _: self.load_models(), "Eliminando…")

    def create_clone(self):
        if not self.need_api():
            return
        win = tk.Toplevel(self)
        win.title("Crear clon de voz")
        win.geometry("620x520")
        vars_ = {}
        def field(lbl, key):
            ttk.Label(win, text=lbl).pack(anchor="w", padx=15, pady=(10, 2))
            v = tk.StringVar()
            vars_[key] = v
            ttk.Entry(win, textvariable=v, width=70).pack(padx=15, fill="x")
        field("Título", "title")
        field("Descripción", "description")
        field("Visibilidad (public/unlist/private)", "visibility")
        vars_["visibility"].set("private")
        field("Tags separados por coma", "tags")
        ttk.Label(win, text="Audios de referencia").pack(anchor="w", padx=15, pady=(10, 2))
        files = tk.StringVar()
        ttk.Entry(win, textvariable=files).pack(padx=15, fill="x")
        def pick():
            x = filedialog.askopenfilenames(filetypes=[("Audio", "*.wav *.mp3 *.m4a *.flac *.ogg"), ("Todos", "*.*")])
            files.set("|".join(x))
        ttk.Button(win, text="Seleccionar audios", command=pick).pack(padx=15, pady=8, anchor="w")
        ttk.Label(win, text="Textos correspondientes, uno por línea (opcional)").pack(anchor="w", padx=15, pady=(8, 2))
        texts = tk.Text(win, height=7, bg="#181d27", fg="#e8ecf1")
        texts.pack(padx=15, fill="both", expand=True)
        def go():
            ps = [p for p in files.get().split("|") if p]
            if not vars_["title"].get() or not ps:
                messagebox.showwarning("Faltan datos", "Título y al menos un audio.")
                return
            title = vars_["title"].get()
            visibility = vars_["visibility"].get() or "private"
            description = vars_["description"].get()
            tags = vars_["tags"].get()
            text_list = [x for x in texts.get("1.0", "end").splitlines() if x.strip()]
            win.destroy()
            self.run_bg(lambda: self.api.create_model_multipart(title, visibility, description, tags, ps, text_list), lambda _: self.load_models(), "Creando clon…")
        ttk.Button(win, text="CREAR MODELO", style="Accent.TButton", command=go).pack(pady=12)

    def _settings(self):
        f = self.settings_tab
        ttk.Label(f, text="Configuración", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(f, text="API key de Fish Audio").pack(anchor="w", pady=(18, 4))
        self.key = tk.StringVar()
        ttk.Entry(f, textvariable=self.key, show="•", width=70).pack(anchor="w")
        ttk.Button(f, text="Guardar y probar", style="Accent.TButton", command=self.save_key).pack(anchor="w", pady=10)
        ttk.Label(f, text="La clave se guarda solo en la configuración local de Windows de esta app.", foreground="#9aa4b2").pack(anchor="w", pady=10)
        ttk.Button(f, text="Abrir panel de API keys", command=lambda: os.startfile("https://fish.audio/app/api-keys/")).pack(anchor="w")
        ttk.Separator(f).pack(fill="x", pady=25)
        ttk.Label(f, text="Funciones incluidas", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(f, text="• TTS • reference_id • prosodia • parámetros avanzados\n• Speech-to-Text • gestión de modelos • clonación\n• exportación local • interfaz Windows 11").pack(anchor="w", pady=8)

    def _load_saved_key(self):
        try:
            if CONFIG_FILE.is_file():
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                key = str(data.get("api_key", "")).strip()
                if key:
                    self.key.set(key)
                    self.api = FishAPI(key)
                    self.status.set("API key cargada")
        except Exception:
            pass

    def save_key(self):
        k = self.key.get().strip()
        if not k:
            messagebox.showwarning("API key", "Pega tu API key de Fish Audio.")
            return
        api = FishAPI(k)
        self.status.set("Comprobando API key…")
        def done(_):
            try:
                APP_DIR.mkdir(parents=True, exist_ok=True)
                CONFIG_FILE.write_text(json.dumps({"api_key": k}, ensure_ascii=False), encoding="utf-8")
                self.api = api
                self.status.set("API key configurada")
                messagebox.showinfo("Listo", "API key válida y guardada. Ya puedes generar audio y transcribir.")
            except Exception as e:
                self._show_error(f"La API key es válida, pero no se pudo guardar la configuración: {e}")
        self.run_bg(api.test_key, done, "Comprobando API key…")


if __name__ == "__main__":
    App().mainloop()
