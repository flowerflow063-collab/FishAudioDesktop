import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from app import APP_NAME, FPS, Recorder, SYSTEM_AUDIO_AVAILABLE
from mss import mss
from scene_panel import ScenePanel
from scenes import SceneManager
from source_canvas import SourceCanvas
from source_properties import SourceProperties


class StudioWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FlowRecorder Studio")
        self.resize(1550, 950)
        self.setMinimumSize(1200, 760)
        self.manager = SceneManager()
        self.recording = False
        self.rec = None
        self.started = None
        self.build_style()
        self.build_ui()
        self.scene_panel.refresh()
        self.scene_panel.sceneChanged.connect(self.scene_changed)
        self.scene_panel.sources.currentRowChanged.connect(self.source_changed)
        self.canvas.sourceMoved.connect(self.source_moved)
        self.canvas.sourceSelected.connect(self.properties.set_source)
        self.refresh_monitors()
        self.scene_changed(self.manager.active_index)

        self.clock = QtCore.QTimer(self)
        self.clock.timeout.connect(self.update_clock)
        self.clock.start(250)

    def build_style(self):
        self.setStyleSheet("""
        QMainWindow,QWidget { background:#090d12; color:#edf2f7; font-family:'Segoe UI'; }
        QFrame#top,QFrame#bottom,QFrame#card { background:#111821; border:1px solid #202b38; border-radius:10px; }
        QLabel#brand { font-size:20px; font-weight:800; }
        QLabel#muted { color:#7c8a9d; font-size:10px; }
        QLabel#title { font-size:18px; font-weight:700; }
        QPushButton { background:#18212c; border:1px solid #2b3948; border-radius:7px; padding:7px 11px; color:#e8edf3; }
        QPushButton:hover { background:#202c3a; }
        QPushButton#record { background:#e9434a; border:0; font-weight:800; padding:11px 22px; }
        QPushButton#stop { font-weight:700; padding:11px 20px; }
        QComboBox,QLineEdit,QListWidget,QDoubleSpinBox { background:#0d131b; border:1px solid #293544; border-radius:7px; padding:6px; color:#e8edf3; }
        QListWidget::item { padding:7px; }
        QListWidget::item:selected { background:#26384d; border-radius:5px; }
        QGroupBox { background:#111821; border:1px solid #202b38; border-radius:10px; margin-top:8px; padding:9px; }
        QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 5px; color:#9ba8b9; background:#111821; }
        """)

    def build_ui(self):
        central = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        self.setCentralWidget(central)

        top = QtWidgets.QFrame(objectName="top")
        tl = QtWidgets.QHBoxLayout(top)
        tl.addWidget(QtWidgets.QLabel("FLOWRECORDER", objectName="brand"))
        tl.addWidget(QtWidgets.QLabel("  STUDIO", objectName="muted"))
        tl.addStretch()
        self.scene_label = QtWidgets.QLabel("Escena: —", objectName="muted")
        tl.addWidget(self.scene_label)
        self.status = QtWidgets.QLabel("● Listo", objectName="muted")
        tl.addWidget(self.status)
        root.addWidget(top)

        work = QtWidgets.QHBoxLayout()
        work.setSpacing(10)
        self.scene_panel = ScenePanel(self.manager)
        self.scene_panel.setMinimumWidth(390)
        self.scene_panel.setMaximumWidth(450)
        work.addWidget(self.scene_panel)

        center = QtWidgets.QFrame(objectName="card")
        cv = QtWidgets.QVBoxLayout(center)
        header = QtWidgets.QHBoxLayout()
        header.addWidget(QtWidgets.QLabel("LIENZO / COMPOSICIÓN", objectName="title"))
        header.addStretch()
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(["Horizontal 16:9", "Vertical 9:16"])
        self.mode_combo.currentIndexChanged.connect(self.mode_changed)
        header.addWidget(self.mode_combo)
        cv.addLayout(header)
        self.canvas = SourceCanvas(self.manager)
        self.canvas.setMinimumSize(560, 390)
        cv.addWidget(self.canvas, 1)
        hint = QtWidgets.QLabel("Arrastra una fuente para moverla. Doble clic para renombrarla.", objectName="muted")
        cv.addWidget(hint)
        work.addWidget(center, 3)

        right = QtWidgets.QFrame(objectName="card")
        rv = QtWidgets.QVBoxLayout(right)
        rv.addWidget(QtWidgets.QLabel("PROPIEDADES", objectName="title"))
        self.properties = SourceProperties(self.manager)
        rv.addWidget(self.properties)
        rv.addWidget(self.build_record_settings())
        rv.addStretch()
        work.addWidget(right, 1)
        root.addLayout(work, 1)

        bottom = QtWidgets.QFrame(objectName="bottom")
        bl = QtWidgets.QHBoxLayout(bottom)
        self.start = QtWidgets.QPushButton("●  INICIAR GRABACIÓN", objectName="record")
        self.stop = QtWidgets.QPushButton("■  DETENER", objectName="stop")
        self.stop.setEnabled(False)
        self.start.clicked.connect(self.start_recording)
        self.stop.clicked.connect(self.stop_recording)
        self.timer_label = QtWidgets.QLabel("00:00:00", objectName="title")
        bl.addWidget(self.start)
        bl.addWidget(self.stop)
        bl.addStretch()
        bl.addWidget(self.timer_label)
        root.addWidget(bottom)

    def build_record_settings(self):
        group = QtWidgets.QGroupBox("GRABACIÓN")
        form = QtWidgets.QFormLayout(group)
        self.monitor_combo = QtWidgets.QComboBox()
        self.fps_combo = QtWidgets.QComboBox(); self.fps_combo.addItems(map(str, FPS)); self.fps_combo.setCurrentText("30")
        self.output = QtWidgets.QLineEdit(str(Path.home() / "Videos" / "FlowRecorder"))
        choose = QtWidgets.QPushButton("Elegir carpeta")
        choose.clicked.connect(self.choose_output)
        row = QtWidgets.QHBoxLayout(); row.addWidget(self.output); row.addWidget(choose)
        self.mic = QtWidgets.QCheckBox("Micrófono"); self.mic.setChecked(True)
        self.system = QtWidgets.QCheckBox("Audio del sistema"); self.system.setChecked(SYSTEM_AUDIO_AVAILABLE); self.system.setEnabled(SYSTEM_AUDIO_AVAILABLE)
        form.addRow("Pantalla", self.monitor_combo)
        form.addRow("FPS", self.fps_combo)
        form.addRow("Destino", row)
        form.addRow("", self.mic)
        form.addRow("", self.system)
        return group

    def refresh_monitors(self):
        self.monitor_combo.clear()
        try:
            with mss() as sct:
                for i, monitor in enumerate(sct.monitors[1:], 1):
                    self.monitor_combo.addItem(f"Pantalla {i} · {monitor['width']}×{monitor['height']}", monitor)
        except Exception:
            pass

    def choose_output(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Seleccionar carpeta")
        if path:
            self.output.setText(path)

    def scene_changed(self, index):
        scene = self.manager.active
        if scene:
            self.scene_label.setText("Escena: " + scene.name)
        self.canvas.refresh()
        self.properties.set_source(None)

    def source_changed(self, index):
        scene = self.manager.active
        if scene and 0 <= index < len(scene.sources):
            self.properties.set_source(scene.sources[index])
        else:
            self.properties.set_source(None)

    def source_moved(self, source):
        self.manager.save()
        self.properties.set_source(source)

    def mode_changed(self, index):
        self.canvas.set_mode("horizontal" if index == 0 else "vertical")

    def set_record_controls(self, enabled):
        for w in (self.monitor_combo, self.fps_combo, self.output, self.mic, self.system, self.mode_combo):
            w.setEnabled(enabled)
        self.start.setEnabled(enabled)
        self.stop.setEnabled(not enabled)

    def start_recording(self):
        monitor = self.monitor_combo.currentData()
        if not monitor or not self.output.text().strip():
            QtWidgets.QMessageBox.warning(self, APP_NAME, "Selecciona pantalla y carpeta de destino.")
            return
        mode = "vertical" if self.mode_combo.currentIndex() == 1 else "horizontal"
        self.rec = Recorder(
            monitor, int(self.fps_combo.currentText()), self.output.text().strip(), mode,
            self.mic.isChecked(), self.system.isChecked(), {"mic": 1.0, "system": 1.0}, {"mic": False, "system": False}
        )
        try:
            self.rec.start()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, APP_NAME, str(exc)); self.rec = None; return
        self.recording = True
        self.started = QtCore.QElapsedTimer(); self.started.start()
        self.set_record_controls(False)
        self.status.setText("● GRABANDO")
        self.status.setStyleSheet("color:#ff5960;font-weight:800")

    def stop_recording(self):
        if not self.recording or not self.rec:
            return
        self.status.setText("● GUARDANDO…")
        QtWidgets.QApplication.processEvents()
        try:
            final = self.rec.stop()
            QtWidgets.QMessageBox.information(self, APP_NAME, "Grabación guardada en:\n\n" + str(final))
            self.status.setText("● Listo")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, APP_NAME, str(exc)); self.status.setText("● Error")
        finally:
            self.recording = False; self.rec = None
            self.set_record_controls(True)
            self.status.setStyleSheet("color:#7c8a9d")

    def update_clock(self):
        if self.recording and self.started:
            ms = self.started.elapsed()
            total = ms // 1000
            self.timer_label.setText(f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}")
        else:
            self.timer_label.setText("00:00:00")

    def closeEvent(self, event):
        if self.recording:
            answer = QtWidgets.QMessageBox.question(self, APP_NAME, "Hay una grabación activa. ¿Detenerla?", QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                self.stop_recording()
            else:
                event.ignore(); return
        event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("FlowRecorder Studio")
    app.setStyle("Fusion")
    win = StudioWindow(); win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
