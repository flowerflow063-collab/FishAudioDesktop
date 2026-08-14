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

def level(data):
    a = np.asarray(data, dtype=np.float32)
    if not a.size: return 0.0
    rms = float(np.sqrt(np.mean(a*a)))
    if rms <= 1: return 0.0
    db = 20*np.log10(rms/32768.0)
    return max(0.0, min(100.0, (db+60)*100/60))

class Recorder:
    def __init__(self, monitor, fps, out, mode, mic, system, gains, mutes):
        self.monitor, self.fps, self.out, self.mode = dict(monitor), fps, Path(out), mode
        self.mic, self.system = mic, system and SYSTEM_AUDIO_AVAILABLE
        self.gains, self.mutes = dict(gains), dict(mutes)
        self.stop_event = threading.Event(); self.lock = threading.Lock()
        self.mic_level = self.system_level = 0.0
        self.errors = []
        self.mq = queue.Queue(maxsize=150); self.mt = self.st = self.vt = None
        self.mw = self.sw = self.ss = self.spa = None

    def set_channel(self, ch, gain=None, mute=None):
        with self.lock:
            if gain is not None: self.gains[ch] = gain
            if mute is not None: self.mutes[ch] = mute

    def state(self, ch):
        with self.lock: return self.gains[ch], self.mutes[ch]

    def rect(self):
        m = self.monitor; w, h = int(m['width']), int(m['height'])
        if self.mode == 'vertical':
            nw = min(w, int(h*9/16)); nw -= nw % 2
            return {'left': m['left']+(w-nw)//2, 'top': m['top'], 'width': nw, 'height': h-h%2}
        nh = min(h, int(w*9/16)); nh -= nh % 2
        return {'left': m['left'], 'top': m['top']+(h-nh)//2, 'width': w-w%2, 'height': nh}

    def start(self):
        self.out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y-%m-%d_%H-%M-%S'); tag = 'vertical' if self.mode == 'vertical' else 'horizontal'
        base = self.out / f'FlowRecorder_{tag}_{stamp}'
        self.video_file = base.with_suffix('.video.mp4'); self.micfile = base.with_suffix('.mic.wav') if self.mic else None
        self.sysfile = base.with_suffix('.system.wav') if self.system else None; self.final = base.with_suffix('.mp4')
        self.stop_event.clear(); self.vt = threading.Thread(target=self.video, daemon=True)
        self.vt.start()
        if self.mic: self.mt = threading.Thread(target=self.mic_capture, daemon=True); self.mt.start()
        if self.system: self.st = threading.Thread(target=self.system_capture, daemon=True); self.st.start()

    def video(self):
        r = self.rect(); cmd = [ffmpeg(),'-y','-f','rawvideo','-pix_fmt','bgra','-video_size',f"{r['width']}x{r['height']}",'-framerate',str(self.fps),'-i','-','-an','-c:v','libx264','-preset','veryfast','-crf','23','-pix_fmt','yuv420p',str(self.video_file)]
        p = None
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            with mss() as sct:
                step=1/self.fps; nxt=time.perf_counter()
                while not self.stop_event.is_set():
                    p.stdin.write(np.asarray(sct.grab(r), dtype=np.uint8).tobytes()); nxt += step
                    d=nxt-time.perf_counter()
                    if d>0: time.sleep(d)
                    elif d < -step*2: nxt=time.perf_counter()
            p.stdin.close(); err=p.stderr.read().decode(errors='replace'); code=p.wait()
            if code: self.errors.append('FFmpeg: '+(err[-2500:] or str(code)))
        except Exception as e:
            self.errors.append('Vídeo: '+str(e))
            if p:
                try: p.kill()
                except: pass

    def mic_cb(self, data, frames, t, status):
        self.mic_level = level(data)
        if status: self.errors.append('Micrófono: '+str(status))
        try: self.mq.put_nowait(data.copy())
        except queue.Full: pass

    def process(self, data, ch):
        gain, mute = self.state(ch); a=np.asarray(data,dtype=np.float32)
        if mute or gain <= 0: return np.zeros_like(a,dtype=np.int16)
        return np.clip(a*gain,-32768,32767).astype(np.int16)

    def mic_capture(self):
        try:
            dev=sd.query_devices(kind='input'); channels=min(2,int(dev.get('max_input_channels',0)))
            if channels<1: raise RuntimeError('No hay micrófono de entrada disponible en Windows.')
            rate=int(dev.get('default_samplerate') or 48000)
            self.mw=wave.open(str(self.micfile),'wb'); self.mw.setnchannels(channels); self.mw.setsampwidth(2); self.mw.setframerate(rate)
            s=sd.InputStream(device=dev['name'],samplerate=rate,channels=channels,dtype='int16',callback=self.mic_cb,blocksize=1024); self.mic_stream=s; s.start()
            while not self.stop_event.is_set() or not self.mq.empty():
                try: self.mw.writeframes(self.process(self.mq.get(timeout=.2),'mic').tobytes())
                except queue.Empty: pass
        except Exception as e: self.errors.append('Micrófono: '+str(e))
        finally:
            try: self.mic_stream.stop(); self.mic_stream.close()
            except: pass
            try: self.mw.close()
            except: pass

    def system_capture(self):
        pa=stream=wf=None
        try:
            pa=pyaudio.PyAudio()
            try: loop=pa.get_default_wasapi_loopback()
            except Exception:
                api=pa.get_host_api_info_by_type(pyaudio.paWASAPI); out=pa.get_device_info_by_index(api['defaultOutputDevice']); loop=next((x for x in pa.get_loopback_device_info_generator() if out['name'] in x['name']),None)
                if loop is None: raise RuntimeError('No se encontró la salida WASAPI de Windows.')
            ch=max(1,min(2,int(loop.get('maxInputChannels',2)))); rate=int(loop.get('defaultSampleRate') or 48000); idx=int(loop['index'])
            wf=wave.open(str(self.sysfile),'wb'); wf.setnchannels(ch); wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16)); wf.setframerate(rate)
            stream=pa.open(format=pyaudio.paInt16,channels=ch,rate=rate,frames_per_buffer=1024,input=True,input_device_index=idx)
            self.ss=stream; self.spa=pa
            while not self.stop_event.is_set():
                raw=np.frombuffer(stream.read(1024,exception_on_overflow=False),dtype=np.int16); self.system_level=level(raw); wf.writeframes(self.process(raw,'system').tobytes())
        except Exception as e: self.errors.append('Audio del sistema: '+str(e))
        finally:
            try: stream.stop_stream(); stream.close()
            except: pass
            try: wf.close()
            except: pass
            try: pa.terminate()
            except: pass

    def stop(self):
        self.stop_event.set()
        for t,sec in ((self.vt,30),(self.mt,10),(self.st,10)):
            if t: t.join(sec)
        if self.errors: raise RuntimeError('\n\n'.join(self.errors[-3:]))
        if not self.video_file.exists(): raise RuntimeError('FFmpeg no creó el vídeo.')
        aud=[x for x in (self.micfile,self.sysfile) if x]
        if len(aud)==0: cmd=[ffmpeg(),'-y','-i',str(self.video_file),'-c','copy',str(self.final)]
        elif len(aud)==1: cmd=[ffmpeg(),'-y','-i',str(self.video_file),'-i',str(aud[0]),'-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k','-shortest',str(self.final)]
        else: cmd=[ffmpeg(),'-y','-i',str(self.video_file),'-i',str(aud[0]),'-i',str(aud[1]),'-filter_complex','[1:a]aresample=48000[a1];[2:a]aresample=48000[a2];[a1][a2]amix=inputs=2:duration=longest:dropout_transition=2[aout]','-map','0:v:0','-map','[aout]','-c:v','copy','-c:a','aac','-b:a','192k','-shortest',str(self.final)]
        r=subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8',errors='replace')
        if r.returncode: raise RuntimeError('No se pudo crear el MP4:\n\n'+r.stderr[-3000:])
        for f in (self.video_file,self.micfile,self.sysfile):
            try:
                if f and f.exists(): f.unlink()
            except: pass
        return self.final

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__(); self.setWindowTitle(APP_NAME); self.resize(1400,900); self.setMinimumSize(1120,720)
        self.recording=False; self.rec=None; self.mode='horizontal'; self.started=0
        self.setStyleSheet('''QMainWindow,QWidget{background:#0a0e13;color:#edf2f7;font-family:Segoe UI} QFrame#side{background:#0f151d;border-right:1px solid #202936} QLabel#brand{font-size:22px;font-weight:800} QLabel#muted{color:#728096;font-size:10px} QFrame#card,QGroupBox{background:#111821;border:1px solid #202b38;border-radius:12px} QLabel#title{font-size:20px;font-weight:700} QLabel#ct{color:#a9b5c5;font-size:11px;font-weight:700} QLabel#big{font-size:19px;font-weight:750} QLabel#preview{background:#05070a;border:1px solid #263241;border-radius:12px} QComboBox,QLineEdit{background:#0d131b;border:1px solid #293544;border-radius:8px;padding:7px;color:#e8edf3} QPushButton{background:#18212c;border:1px solid #2b3948;border-radius:8px;padding:8px 12px;color:#e8edf3} QPushButton:hover{background:#202c3a} QPushButton#record{background:#e9434a;border:0;font-weight:800;padding:12px 24px} QPushButton#stop{font-weight:700;padding:12px 20px} QPushButton#mode{font-weight:700;padding:9px} QPushButton#mode[active="true"]{background:#29384a;border:1px solid #5a7ea3} QCheckBox{spacing:7px} QSlider::groove:horizontal{height:5px;background:#273241} QSlider::handle:horizontal{width:14px;margin:-5px 0;border-radius:7px;background:#a9b8ca} QProgressBar{height:18px;border:0;border-radius:5px;background:#080c11;text-align:center;color:transparent} QProgressBar::chunk{border-radius:5px;background:#35d07f} QGroupBox{margin-top:8px;padding:10px} QGroupBox::title{subcontrol-origin:margin;left:12px;padding:0 5px;color:#9ba8b9;background:#111821}''')
        self.build(); self.refresh_monitors(); self.set_mode('horizontal')
        self.preview_timer=QtCore.QTimer(self); self.preview_timer.timeout.connect(self.preview); self.preview_timer.start(150)
        self.meter_timer=QtCore.QTimer(self); self.meter_timer.timeout.connect(self.meters); self.meter_timer.start(50)
        self.clock_timer=QtCore.QTimer(self); self.clock_timer.timeout.connect(self.clock)

    def build(self):
        central=QtWidgets.QWidget(); root=QtWidgets.QHBoxLayout(central); root.setContentsMargins(0,0,0,0); self.setCentralWidget(central)
        side=QtWidgets.QFrame(objectName='side'); side.setFixedWidth(205); sl=QtWidgets.QVBoxLayout(side); sl.setContentsMargins(14,20,14,18)
        sl.addWidget(QtWidgets.QLabel('FLOWRECORDER',objectName='brand')); sl.addWidget(QtWidgets.QLabel('SCREEN STUDIO',objectName='muted')); sl.addSpacing(20)
        for t,a in [('▣  Estudio',True),('●  Grabaciones',False),('◉  Audio',False),('⚙  Ajustes',False)]:
            b=QtWidgets.QPushButton(t); b.setObjectName('mode'); b.setProperty('active',a); sl.addWidget(b)
        sl.addStretch(); sl.addWidget(QtWidgets.QLabel('FlowRecorder 1.3\nMixer + 16:9 + 9:16',objectName='muted')); root.addWidget(side)
        body=QtWidgets.QWidget(); bl=QtWidgets.QVBoxLayout(body); bl.setContentsMargins(24,18,24,20); root.addWidget(body,1)
        top=QtWidgets.QHBoxLayout(); top.addWidget(QtWidgets.QLabel('Estudio',objectName='title')); top.addStretch(); self.status=QtWidgets.QLabel('● Listo para grabar',objectName='muted'); top.addWidget(self.status); bl.addLayout(top)
        stats=QtWidgets.QHBoxLayout(); self.fpslab=self.stat(stats,'FPS','30'); self.timelab=self.stat(stats,'DURACIÓN','00:00:00'); self.audiolab=self.stat(stats,'AUDIO','Mic + sistema'); self.formatlab=self.stat(stats,'FORMATO','16:9'); bl.addLayout(stats)
        work=QtWidgets.QHBoxLayout(); work.setSpacing(14)
        pc=QtWidgets.QFrame(objectName='card'); pv=QtWidgets.QVBoxLayout(pc); pv.addWidget(QtWidgets.QLabel('VISTA PREVIA',objectName='ct')); self.prev=QtWidgets.QLabel('Preparando vista previa…',objectName='preview'); self.prev.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter); self.prev.setMinimumSize(620,400); pv.addWidget(self.prev,1); work.addWidget(pc,1)
        right=QtWidgets.QVBoxLayout(); self.capture(right); self.format(right); self.sources(right); self.destination(right); right.addStretch(); work.addLayout(right); bl.addLayout(work,1)
        bl.addWidget(self.mixer())
        bottom=QtWidgets.QFrame(objectName='card'); bb=QtWidgets.QHBoxLayout(bottom); self.start=QtWidgets.QPushButton('●  INICIAR GRABACIÓN',objectName='record'); self.start.clicked.connect(self.start_rec); self.stop=QtWidgets.QPushButton('■  DETENER',objectName='stop'); self.stop.clicked.connect(self.stop_rec); self.stop.setEnabled(False); self.timer=QtWidgets.QLabel('00:00:00',objectName='big'); bb.addWidget(self.start); bb.addWidget(self.stop); bb.addStretch(); bb.addWidget(self.timer); bl.addWidget(bottom)

    def stat(self,lay,t,v):
        c=QtWidgets.QFrame(objectName='card'); x=QtWidgets.QVBoxLayout(c); x.addWidget(QtWidgets.QLabel(t,objectName='ct')); q=QtWidgets.QLabel(v,objectName='big'); x.addWidget(q); lay.addWidget(c,1); return q

    def capture(self,lay):
        g=QtWidgets.QGroupBox('CAPTURA'); f=QtWidgets.QFormLayout(g); self.mon=QtWidgets.QComboBox(); self.fps=QtWidgets.QComboBox(); self.fps.addItems(map(str,FPS)); self.fps.setCurrentText('30'); self.fps.currentTextChanged.connect(lambda x:self.fpslab.setText(x)); f.addRow('Pantalla',self.mon); f.addRow('FPS',self.fps); lay.addWidget(g)

    def format(self,lay):
        g=QtWidgets.QGroupBox('VENTANAS / FORMATO'); v=QtWidgets.QVBoxLayout(g); r=QtWidgets.QHBoxLayout(); self.hbtn=QtWidgets.QPushButton('▭  Horizontal\n16:9',objectName='mode'); self.vbtn=QtWidgets.QPushButton('▯  Vertical\n9:16',objectName='mode'); self.hbtn.clicked.connect(lambda:self.set_mode('horizontal')); self.vbtn.clicked.connect(lambda:self.set_mode('vertical')); r.addWidget(self.hbtn); r.addWidget(self.vbtn); v.addLayout(r); self.hint=QtWidgets.QLabel('Horizontal para YouTube y escritorio',objectName='muted'); v.addWidget(self.hint); lay.addWidget(g)

    def sources(self,lay):
        g=QtWidgets.QGroupBox('FUENTES DE AUDIO'); v=QtWidgets.QVBoxLayout(g); self.mic=QtWidgets.QCheckBox('Grabar micrófono'); self.mic.setChecked(True); self.sys=QtWidgets.QCheckBox('Grabar audio del sistema'); self.sys.setChecked(SYSTEM_AUDIO_AVAILABLE); self.sys.setEnabled(SYSTEM_AUDIO_AVAILABLE); self.mic.stateChanged.connect(self.audio_text); self.sys.stateChanged.connect(self.audio_text); v.addWidget(self.mic); v.addWidget(self.sys); lay.addWidget(g)

    def destination(self,lay):
        g=QtWidgets.QGroupBox('DESTINO'); v=QtWidgets.QVBoxLayout(g); r=QtWidgets.QHBoxLayout(); self.out=QtWidgets.QLineEdit(str(Path.home()/'Videos'/'FlowRecorder')); b=QtWidgets.QPushButton('Elegir'); b.clicked.connect(self.choose); r.addWidget(self.out); r.addWidget(b); v.addLayout(r); self.file=QtWidgets.QLabel('MP4 • H.264 • AAC',objectName='muted'); v.addWidget(self.file); lay.addWidget(g)

    def mixer(self):
        g=QtWidgets.QFrame(objectName='card'); v=QtWidgets.QVBoxLayout(g); head=QtWidgets.QHBoxLayout(); head.addWidget(QtWidgets.QLabel('MEZCLADOR DE AUDIO',objectName='ct')); head.addStretch(); self.mixstatus=QtWidgets.QLabel('Barras en tiempo real durante la grabación',objectName='muted'); head.addWidget(self.mixstatus); v.addLayout(head); self.rows={}
        for ch,name in [('mic','🎙  Micrófono'),('system','🔊  Audio del sistema')]:
            r=QtWidgets.QHBoxLayout(); r.addWidget(QtWidgets.QLabel(name)); bar=QtWidgets.QProgressBar(); bar.setRange(0,100); r.addWidget(bar,1); db=QtWidgets.QLabel('-∞ dB'); db.setMinimumWidth(55); r.addWidget(db); s=QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal); s.setRange(0,200); s.setValue(100); s.setFixedWidth(90); m=QtWidgets.QPushButton('M'); m.setCheckable(True); m.setFixedWidth(30); r.addWidget(s); r.addWidget(m); v.addLayout(r); self.rows[ch]=(bar,db,s,m); s.valueChanged.connect(lambda x,c=ch:self.gain(c,x)); m.toggled.connect(lambda x,c=ch:self.mute(c,x))
        return g

    def refresh_monitors(self):
        self.mon.clear()
        try:
            with mss() as sct:
                for i,m in enumerate(sct.monitors[1:],1): self.mon.addItem(f"Pantalla {i} · {m['width']}×{m['height']}",m)
        except: pass

    def choose(self):
        p=QtWidgets.QFileDialog.getExistingDirectory(self,'Seleccionar carpeta de grabaciones')
        if p:self.out.setText(p)

    def set_mode(self,mode):
        if self.recording:return
        self.mode=mode; h=mode=='horizontal'; self.hbtn.setProperty('active',h); self.vbtn.setProperty('active',not h)
        for b in (self.hbtn,self.vbtn): b.style().unpolish(b); b.style().polish(b)
        self.formatlab.setText('16:9' if h else '9:16'); self.hint.setText('Horizontal para YouTube y escritorio' if h else 'Vertical para Shorts, Reels y TikTok'); self.preview()

    def audio_text(self):
        a=self.mic.isChecked(); b=self.sys.isChecked(); self.audiolab.setText('Mic + sistema' if a and b else 'Micrófono' if a else 'Sistema' if b else 'Sin audio')

    def gain(self,ch,x):
        if self.recording:self.rec.set_channel(ch,gain=x/100)

    def mute(self,ch,x):
        if self.recording:self.rec.set_channel(ch,mute=x)

    def capture_rect(self,m):
        w,h=int(m['width']),int(m['height'])
        if self.mode=='vertical':
            nw=min(w,int(h*9/16)); nw-=nw%2; return {'left':m['left']+(w-nw)//2,'top':m['top'],'width':nw,'height':h-h%2}
        nh=min(h,int(w*9/16)); nh-=nh%2; return {'left':m['left'],'top':m['top']+(h-nh)//2,'width':w-w%2,'height':nh}

    def preview(self):
        if self.recording:return
        try:
            m=self.mon.currentData()
            if not m:return
            with mss() as sct: a=np.asarray(sct.grab(self.capture_rect(m)))
            h,w,_=a.shape; im=QtGui.QImage(a.data,w,h,4*w,QtGui.QImage.Format.Format_ARGB32).copy(); self.prev.setPixmap(QtGui.QPixmap.fromImage(im).scaled(self.prev.size(),QtCore.Qt.AspectRatioMode.KeepAspectRatio,QtCore.Qt.TransformationMode.SmoothTransformation))
        except: pass

    def meters(self):
        vals={'mic':self.rec.mic_level if self.recording and self.rec else 0,'system':self.rec.system_level if self.recording and self.rec else 0}
        for ch,x in vals.items():
            bar,db,_,_=self.rows[ch]; bar.setValue(int(x)); db.setText('-∞ dB' if x<.1 else f"{-60+x*.6:4.1f} dB")

    def controls(self,on):
        for w in (self.mon,self.fps,self.hbtn,self.vbtn,self.mic):w.setEnabled(on)
        self.sys.setEnabled(on and SYSTEM_AUDIO_AVAILABLE)

    def start_rec(self):
        m=self.mon.currentData(); out=self.out.text().strip()
        if not m or not out:return QtWidgets.QMessageBox.warning(self,APP_NAME,'Selecciona pantalla y carpeta de salida.')
        mic=self.mic.isChecked(); system=self.sys.isChecked()
        if system and not SYSTEM_AUDIO_AVAILABLE:return QtWidgets.QMessageBox.warning(self,APP_NAME,'El audio del sistema no está disponible en esta versión.')
        gains={c:r[2].value()/100 for c,r in self.rows.items()}; mutes={c:r[3].isChecked() for c,r in self.rows.items()}
        self.rec=Recorder(m,int(self.fps.currentText()),out,self.mode,mic,system,gains,mutes); self.rec.start(); self.recording=True; self.started=time.monotonic(); self.controls(False); self.start.setEnabled(False); self.stop.setEnabled(True); self.status.setText('● GRABANDO'); self.status.setStyleSheet('color:#ff5960;font-weight:800'); self.mixstatus.setText('● MONITOREANDO EN VIVO'); self.mixstatus.setStyleSheet('color:#53d88a;font-weight:800'); self.clock_timer.start(250)

    def stop_rec(self):
        if not self.recording:return
        self.stop.setEnabled(False); self.status.setText('● GUARDANDO…'); QtWidgets.QApplication.processEvents()
        try:
            f=self.rec.stop(); self.file.setText('Guardado: '+f.name); QtWidgets.QMessageBox.information(self,APP_NAME,'Grabación terminada.\n\n'+str(f)); self.status.setText('● Grabación guardada')
        except Exception as e:
            QtWidgets.QMessageBox.critical(self,APP_NAME,str(e)); self.status.setText('● Error')
        finally:
            self.recording=False; self.rec=None; self.clock_timer.stop(); self.controls(True); self.start.setEnabled(True); self.stop.setEnabled(False); self.mixstatus.setText('Barras en tiempo real durante la grabación'); self.mixstatus.setStyleSheet('color:#728096;font-size:10px'); self.status.setStyleSheet('color:#728096')

    def clock(self):
        t=int(time.monotonic()-self.started); s=time.strftime('%H:%M:%S',time.gmtime(t)); self.timer.setText(s); self.timelab.setText(s)

    def closeEvent(self,e):
        if self.recording:
            a=QtWidgets.QMessageBox.question(self,APP_NAME,'Hay una grabación activa. ¿Detener antes de salir?',QtWidgets.QMessageBox.StandardButton.Yes|QtWidgets.QMessageBox.StandardButton.No)
            if a==QtWidgets.QMessageBox.StandardButton.Yes:self.stop_rec()
            else:e.ignore();return
        e.accept()

def main():
    app=QtWidgets.QApplication(sys.argv); app.setApplicationName(APP_NAME); app.setStyle('Fusion'); w=MainWindow(); w.show(); sys.exit(app.exec())
if __name__=='__main__': main()
