
import base64, json, mimetypes, os, queue, threading, time, urllib.request, urllib.error, tkinter as tk
from tkinter import ttk, filedialog, messagebox

API_BASE = "https://api.fish.audio"

class FishAPI:
    def __init__(self, key):
        self.key = key.strip()
    def request(self, method, path, data=None, headers=None, raw=False):
        h = {"Authorization": f"Bearer {self.key}", "User-Agent": "FishAudioDesktop/1.0"}
        if headers: h.update(headers)
        body = None
        if data is not None:
            if isinstance(data, (bytes, bytearray)):
                body = data
            else:
                body = json.dumps(data).encode()
                h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(API_BASE + path, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                content = r.read()
                if raw:
                    return content, r.headers
                if not content:
                    return {}
                return json.loads(content.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {msg}")
        except Exception as e:
            raise RuntimeError(str(e))

    def tts(self, payload, model):
        return self.request("POST", "/v1/tts", payload, {"model": model}, raw=True)

    def asr_multipart(self, audio_path, language="", timestamps=False):
        boundary = "----FishAudioBoundary7MA4YWxkTrZu0gW"
        fn = os.path.basename(audio_path)
        ctype = mimetypes.guess_type(fn)[0] or "application/octet-stream"
        audio = Path(audio_path).read_bytes()
        parts = []
        def field(name, value):
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
        field("language", language)
        field("ignore_timestamps", "false" if timestamps else "true")
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="{fn}"\r\nContent-Type: {ctype}\r\n\r\n').encode())
        parts.append(audio + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        return self.request("POST", "/v1/asr", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def models(self):
        return self.request("GET", "/model")
    def model(self, mid):
        return self.request("GET", f"/model/{mid}")
    def delete_model(self, mid):
        return self.request("DELETE", f"/model/{mid}")
    def create_model_multipart(self, title, visibility, description, tags, files, texts):
        boundary = "----FishAudioModelBoundary8MA4YWxkTrZu0gW"
        chunks=[]
        def text(name, value):
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        text("type","tts"); text("title",title); text("train_mode","fast"); text("visibility",visibility)
        text("description",description); text("enhance_audio_quality","true"); text("generate_sample","false")
        for tag in [x.strip() for x in tags.split(",") if x.strip()]:
            text("tags", tag)
        for t in texts:
            text("texts", t)
        for p in files:
            fn=os.path.basename(p); ct=mimetypes.guess_type(fn)[0] or "audio/wav"
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="voices"; filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n'.encode())
            chunks.append(Path(p).read_bytes()+b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        return self.request("POST","/model",b"".join(chunks),{"Content-Type":f"multipart/form-data; boundary={boundary}"})

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fish Audio Desktop")
        self.geometry("1120x760")
        self.minsize(980,650)
        self.configure(bg="#10131a")
        self.api = None
        self.status = tk.StringVar(value="Sin API key")
        self._style()
        self._build()

    def _style(self):
        s=ttk.Style(self); s.theme_use("clam")
        s.configure(".", background="#10131a", foreground="#e8ecf1", fieldbackground="#181d27")
        s.configure("TNotebook", background="#10131a", borderwidth=0)
        s.configure("TNotebook.Tab", padding=(18,10), background="#181d27", foreground="#b9c1cc")
        s.map("TNotebook.Tab", background=[("selected","#252d3a")], foreground=[("selected","#ffffff")])
        s.configure("TButton", padding=(12,7), background="#2d3748", foreground="#fff")
        s.map("TButton", background=[("active","#3b475a")])
        s.configure("Accent.TButton", background="#6d5dfc", foreground="#fff")
        s.configure("TEntry", padding=7)
        s.configure("TCombobox", padding=6)
        s.configure("TLabelframe", background="#10131a", foreground="#dce2ea")
        s.configure("TLabelframe.Label", background="#10131a", foreground="#dce2ea")

    def _build(self):
        top=ttk.Frame(self,padding=12); top.pack(fill="x")
        ttk.Label(top,text="FISH AUDIO DESKTOP",font=("Segoe UI",18,"bold")).pack(side="left")
        ttk.Label(top,textvariable=self.status).pack(side="right")
        self.nb=ttk.Notebook(self); self.nb.pack(fill="both",expand=True,padx=12,pady=(0,12))
        self.tts_tab=ttk.Frame(self,padding=14); self.asr_tab=ttk.Frame(self,padding=14)
        self.voices_tab=ttk.Frame(self,padding=14); self.settings_tab=ttk.Frame(self,padding=14)
        self.nb.add(self.tts_tab,text="Texto → Voz"); self.nb.add(self.asr_tab,text="Audio → Texto")
        self.nb.add(self.voices_tab,text="Voces / Clonación"); self.nb.add(self.settings_tab,text="Configuración")
        self._tts(); self._asr(); self._voices(); self._settings()

    def need_api(self):
        if not self.api:
            messagebox.showwarning("API key","Introduce tu API key de Fish Audio en Configuración.")
            self.nb.select(self.settings_tab); return False
        return True

    def run_bg(self, fn, ok=None):
        def worker():
            try:
                result=fn()
                self.after(0,lambda: ok(result) if ok else None)
            except Exception as e:
                self.after(0,lambda: messagebox.showerror("Fish Audio",str(e)))
        threading.Thread(target=worker,daemon=True).start()

    def _tts(self):
        f=self.tts_tab
        left=ttk.Frame(f); left.pack(side="left",fill="both",expand=True,padx=(0,8))
        right=ttk.Frame(f); right.pack(side="right",fill="y",padx=(8,0))
        ttk.Label(left,text="Texto",font=("Segoe UI",11,"bold")).pack(anchor="w")
        self.text=tk.Text(left,height=24,wrap="word",bg="#181d27",fg="#e8ecf1",insertbackground="white",relief="flat")
        self.text.pack(fill="both",expand=True,pady=8)
        self.text.insert("1.0","Hola. Esta es una prueba de Fish Audio Desktop.")
        self.fields={}
        def add(label,key,default,width=25):
            ttk.Label(right,text=label).pack(anchor="w",pady=(7,2))
            v=tk.StringVar(value=default); self.fields[key]=v
            ttk.Entry(right,textvariable=v,width=width).pack(anchor="w")
        add("Modelo","model","s2-pro")
        add("Voice / reference_id","voice","")
        add("Formato","format","mp3")
        add("Velocidad (0.5–2)","speed","1.0")
        add("Volumen (-20–20)","volume","0")
        add("Temperatura","temperature","")
        add("Top P","top_p","")
        add("Chunk length","chunk_length","200")
        add("Latency","latency","")
        ttk.Label(right,text="Salida").pack(anchor="w",pady=(7,2))
        self.out=tk.StringVar(value=str(Path.home()/"Downloads"/"fish_audio.mp3"))
        row=ttk.Frame(right); row.pack(fill="x")
        ttk.Entry(row,textvariable=self.out,width=28).pack(side="left")
        ttk.Button(row,text="…",command=self.pick_out).pack(side="right")
        ttk.Button(right,text="GENERAR AUDIO",style="Accent.TButton",command=self.generate).pack(fill="x",pady=18)
        ttk.Label(right,text="Admite voice ID, formato MP3/WAV/PCM/Opus,\nprosodia y parámetros avanzados de TTS.",foreground="#9aa4b2").pack(anchor="w")

    def pick_out(self):
        p=filedialog.asksaveasfilename(defaultextension=".mp3",filetypes=[("Audio","*.mp3 *.wav *.opus"),("Todos","*.*")])
        if p:self.out.set(p)

    def generate(self):
        if not self.need_api(): return
        text=self.text.get("1.0","end").strip()
        if not text:return
        p={"text":text,"format":self.fields["format"].get().strip() or "mp3"}
        voice=self.fields["voice"].get().strip()
        if voice:p["reference_id"]=voice
        pros={}
        for k in ("speed","volume"):
            x=self.fields[k].get().strip()
            if x:pros[k]=float(x)
        if pros:p["prosody"]=pros
        for k in ("temperature","top_p"):
            x=self.fields[k].get().strip()
            if x:p[k]=float(x)
        x=self.fields["chunk_length"].get().strip()
        if x:p["chunk_length"]=int(x)
        x=self.fields["latency"].get().strip()
        if x:p["latency"]=int(x)
        model=self.fields["model"].get().strip() or "s2-pro"
        out=self.out.get()
        self.status.set("Generando…")
        def on_done(result):
            data,_=result
            Path(out).parent.mkdir(parents=True,exist_ok=True)
            Path(out).write_bytes(data)
            self.status.set("Audio generado")
            if messagebox.askyesno("Listo","Audio guardado. ¿Abrirlo ahora?"):
                os.startfile(out)
        self.run_bg(lambda:self.api.tts(p,model), on_done)

    def _asr(self):
        f=self.asr_tab
        ttk.Label(f,text="Transcripción de audio",font=("Segoe UI",14,"bold")).pack(anchor="w")
        row=ttk.Frame(f);row.pack(fill="x",pady=15)
        self.asr_file=tk.StringVar()
        ttk.Entry(row,textvariable=self.asr_file).pack(side="left",fill="x",expand=True)
        ttk.Button(row,text="Seleccionar audio",command=lambda:self.asr_file.set(filedialog.askopenfilename(filetypes=[("Audio","*.wav *.mp3 *.m4a *.ogg *.flac"),("Todos","*.*")]))).pack(side="left",padx=8)
        opts=ttk.Frame(f);opts.pack(fill="x")
        self.asr_lang=tk.StringVar(); ttk.Label(opts,text="Idioma (opcional)").pack(side="left")
        ttk.Entry(opts,textvariable=self.asr_lang,width=15).pack(side="left",padx=8)
        self.timestamps=tk.BooleanVar(value=False); ttk.Checkbutton(opts,text="Timestamps",variable=self.timestamps).pack(side="left")
        ttk.Button(f,text="TRANSCRIBIR",style="Accent.TButton",command=self.transcribe).pack(anchor="w",pady=16)
        self.asr_text=tk.Text(f,height=22,bg="#181d27",fg="#e8ecf1",relief="flat")
        self.asr_text.pack(fill="both",expand=True)

    def transcribe(self):
        if not self.need_api():return
        p=self.asr_file.get()
        if not p:return
        self.status.set("Transcribiendo…")
        self.run_bg(lambda:self.api.asr_multipart(p,self.asr_lang.get(),self.timestamps.get()),self.show_asr)
    def show_asr(self,r):
        self.asr_text.delete("1.0","end"); self.asr_text.insert("1.0",r.get("text",""))
        self.status.set("Transcripción lista")

    def _voices(self):
        top=ttk.Frame(self.voices_tab);top.pack(fill="x")
        ttk.Button(top,text="Actualizar mis modelos",command=self.load_models).pack(side="left")
        ttk.Button(top,text="Crear clon",command=self.create_clone).pack(side="left",padx=8)
        ttk.Button(top,text="Eliminar seleccionado",command=self.delete_model).pack(side="left")
        self.models_list=tk.Listbox(self.voices_tab,bg="#181d27",fg="#e8ecf1",selectbackground="#6d5dfc",height=16)
        self.models_list.pack(fill="both",expand=True,pady=12)
        self.model_info=tk.Text(self.voices_tab,height=10,bg="#181d27",fg="#e8ecf1",relief="flat")
        self.model_info.pack(fill="x")
        self.model_data=[]

    def load_models(self):
        if not self.need_api():return
        self.status.set("Cargando modelos…")
        self.run_bg(self.api.models,self.show_models)
    def show_models(self,r):
        self.model_data=r if isinstance(r,list) else r.get("items",r.get("data",[]))
        self.models_list.delete(0,"end")
        for m in self.model_data:self.models_list.insert("end",f'{m.get("_id","")} — {m.get("title","(sin título)")} [{m.get("state","")}]')
        self.status.set(f"{len(self.model_data)} modelos")
    def delete_model(self):
        if not self.need_api():return
        sel=self.models_list.curselection()
        if not sel:return
        mid=self.model_data[sel[0]].get("_id")
        if messagebox.askyesno("Confirmar",f"Eliminar {mid}?"):
            self.run_bg(lambda:self.api.delete_model(mid),lambda _:self.load_models())
    def create_clone(self):
        if not self.need_api():return
        win=tk.Toplevel(self);win.title("Crear clon de voz");win.geometry("620x520")
        vars={}
        def field(lbl,key):
            ttk.Label(win,text=lbl).pack(anchor="w",padx=15,pady=(10,2)); v=tk.StringVar();vars[key]=v;ttk.Entry(win,textvariable=v,width=70).pack(padx=15,fill="x")
        field("Título","title"); field("Descripción","description"); field("Visibilidad (public/unlist/private)","visibility");vars["visibility"].set("private")
        field("Tags separados por coma","tags")
        ttk.Label(win,text="Audios de referencia (10+ segundos recomendado)").pack(anchor="w",padx=15,pady=(10,2))
        files=tk.StringVar(); ttk.Entry(win,textvariable=files).pack(padx=15,fill="x")
        def pick():
            x=filedialog.askopenfilenames(filetypes=[("Audio","*.wav *.mp3 *.m4a *.flac *.ogg"),("Todos","*.*")]);files.set("|".join(x))
        ttk.Button(win,text="Seleccionar audios",command=pick).pack(padx=15,pady=8,anchor="w")
        ttk.Label(win,text="Textos correspondientes, uno por línea (opcional)").pack(anchor="w",padx=15,pady=(8,2))
        texts=tk.Text(win,height=7,bg="#181d27",fg="#e8ecf1");texts.pack(padx=15,fill="both",expand=True)
        def go():
            ps=[p for p in files.get().split("|") if p]
            if not vars["title"].get() or not ps: messagebox.showwarning("Faltan datos","Título y al menos un audio.");return
            win.destroy();self.status.set("Creando clon…")
            self.run_bg(lambda:self.api.create_model_multipart(vars["title"].get(),vars["visibility"].get(),vars["description"].get(),vars["tags"].get(),ps,[x for x in texts.get("1.0","end").splitlines() if x.strip()]),lambda r:self.load_models())
        ttk.Button(win,text="CREAR MODELO",style="Accent.TButton",command=go).pack(pady=12)

    def _settings(self):
        f=self.settings_tab
        ttk.Label(f,text="Configuración",font=("Segoe UI",14,"bold")).pack(anchor="w")
        ttk.Label(f,text="API key de Fish Audio").pack(anchor="w",pady=(18,4))
        self.key=tk.StringVar()
        ttk.Entry(f,textvariable=self.key,show="•",width=70).pack(anchor="w")
        ttk.Button(f,text="Guardar y probar",style="Accent.TButton",command=self.save_key).pack(anchor="w",pady=10)
        ttk.Label(f,text="La clave se guarda solo en la configuración local de Windows de esta app.\nObtén tu clave en el panel oficial de Fish Audio.",foreground="#9aa4b2").pack(anchor="w",pady=10)
        ttk.Button(f,text="Abrir panel de API keys",command=lambda:os.startfile("https://fish.audio/app/api-keys/")).pack(anchor="w")
        ttk.Separator(f).pack(fill="x",pady=25)
        ttk.Label(f,text="Funciones incluidas",font=("Segoe UI",11,"bold")).pack(anchor="w")
        ttk.Label(f,text="• TTS • voces por reference_id • prosodia • parámetros avanzados\n• Speech-to-Text • gestión de modelos • clonación mediante audio\n• exportación local • interfaz Windows 11").pack(anchor="w",pady=8)
    def save_key(self):
        k=self.key.get().strip()
        if not k:return
        self.api=FishAPI(k);self.status.set("API key configurada")
        messagebox.showinfo("Listo","API key configurada. Ya puedes usar la aplicación.")

if __name__=="__main__":
    App().mainloop()
