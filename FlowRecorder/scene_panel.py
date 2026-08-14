from PySide6 import QtCore, QtWidgets
from scenes import Source


class ScenePanel(QtWidgets.QWidget):
    """Reusable Scenes/Sources dock for FlowRecorder Studio."""
    sceneChanged = QtCore.Signal(int)

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        self.scenes = QtWidgets.QListWidget()
        self.scenes.setMinimumWidth(180)
        self.scenes.currentRowChanged.connect(self.select_scene)
        root.addWidget(self.panel("ESCENAS", self.scenes, self.add_scene, self.remove_scene), 1)

        self.sources = QtWidgets.QListWidget()
        self.sources.setMinimumWidth(220)
        self.sources.currentRowChanged.connect(self.source_selected)
        root.addWidget(self.panel("FUENTES", self.sources, self.add_source, self.remove_source), 1)
        self.refresh()

    def panel(self, title, widget, add_slot, remove_slot):
        frame = QtWidgets.QFrame()
        frame.setObjectName("card")
        box = QtWidgets.QVBoxLayout(frame)
        box.addWidget(QtWidgets.QLabel(title))
        box.addWidget(widget, 1)
        row = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("＋")
        delete = QtWidgets.QPushButton("−")
        add.clicked.connect(add_slot)
        delete.clicked.connect(remove_slot)
        row.addWidget(add)
        row.addWidget(delete)
        box.addLayout(row)
        return frame

    def refresh(self):
        self.scenes.blockSignals(True)
        self.scenes.clear()
        for scene in self.manager.scenes:
            self.scenes.addItem("🎬  " + scene.name)
        self.scenes.setCurrentRow(self.manager.active_index)
        self.scenes.blockSignals(False)
        self.refresh_sources()

    def refresh_sources(self):
        self.sources.clear()
        scene = self.manager.active
        if not scene:
            return
        icons = {"screen": "🖥", "window": "🪟", "camera": "📷", "image": "🖼", "video": "🎞", "text": "T", "mic": "🎙", "system": "🔊"}
        for source in scene.sources:
            mark = "✓" if source.enabled else "○"
            self.sources.addItem(f"{mark}  {icons.get(source.kind, '◉')}  {source.name}")

    def select_scene(self, index):
        if index < 0 or index >= len(self.manager.scenes):
            return
        self.manager.active_index = index
        self.manager.save()
        self.refresh_sources()
        self.sceneChanged.emit(index)

    def add_scene(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "Nueva escena", "Nombre:")
        if ok:
            self.manager.add_scene(name)
            self.refresh()

    def remove_scene(self):
        self.manager.remove_scene(self.scenes.currentRow())
        self.refresh()

    def add_source(self):
        scene_index = self.manager.active_index
        choices = [
            ("Pantalla", "screen"), ("Ventana", "window"),
            ("Cámara", "camera"), ("Imagen", "image"),
            ("Vídeo", "video"), ("Texto", "text"),
            ("Micrófono", "mic"), ("Audio del sistema", "system"),
        ]
        names = [x[0] for x in choices]
        name, ok = QtWidgets.QInputDialog.getItem(self, "Añadir fuente", "Tipo:", names, 0, False)
        if not ok:
            return
        kind = dict(choices)[name]
        self.manager.add_source(scene_index, Source(name, kind))
        self.refresh_sources()

    def remove_source(self):
        self.manager.remove_source(self.manager.active_index, self.sources.currentRow())
        self.refresh_sources()

    def source_selected(self, index):
        scene = self.manager.active
        if scene and 0 <= index < len(scene.sources):
            self.manager.save()
