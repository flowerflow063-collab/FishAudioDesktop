from PySide6 import QtCore, QtGui, QtWidgets


class SourceItem(QtWidgets.QGraphicsRectItem):
    def __init__(self, source, canvas, scene_size=1000):
        self.source = source
        self.canvas = canvas
        self.scene_size = scene_size
        self.handle_size = 10
        super().__init__(0, 0, max(40, source.width * scene_size), max(30, source.height * scene_size))
        self.setPos(source.x * scene_size, source.y * scene_size)
        self.setFlags(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(10)
        self.setPen(QtGui.QPen(QtGui.QColor("#62a0ff"), 2))
        self.setBrush(QtGui.QBrush(QtGui.QColor(40, 80, 120, 80)))
        self.setToolTip(source.name)

    def paint(self, painter, option, widget=None):
        rect = self.rect()
        painter.setBrush(self.brush())
        pen = QtGui.QPen(QtGui.QColor("#62a0ff") if self.isSelected() else QtGui.QColor("#52677f"), 2)
        painter.setPen(pen)
        painter.drawRoundedRect(rect, 6, 6)
        painter.setPen(QtGui.QPen(QtGui.QColor("#f3f7fb")))
        painter.drawText(rect.adjusted(8, 6, -8, -6), QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop, self.source.name)
        if self.isSelected():
            painter.setBrush(QtGui.QBrush(QtGui.QColor("#62a0ff")))
            for x, y in ((0, 0), (rect.width(), 0), (0, rect.height()), (rect.width(), rect.height())):
                painter.drawEllipse(QtCore.QPointF(x, y), self.handle_size / 2, self.handle_size / 2)

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            pos = value
            max_x = self.canvas.scene_width - self.rect().width()
            max_y = self.canvas.scene_height - self.rect().height()
            pos.setX(max(0, min(pos.x(), max_x)))
            pos.setY(max(0, min(pos.y(), max_y)))
            self.source.x = pos.x() / self.scene_size
            self.source.y = pos.y() / self.scene_size
            self.canvas.sourceMoved.emit(self.source)
            return pos
        return super().itemChange(change, value)


class SourceCanvas(QtWidgets.QGraphicsView):
    sourceMoved = QtCore.Signal(object)
    sourceSelected = QtCore.Signal(object)

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.scene_width = 1000
        self.scene_height = 562
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QtGui.QBrush(QtGui.QColor("#05070a")))
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.graphics = QtWidgets.QGraphicsScene(self)
        self.setScene(self.graphics)
        self.graphics.selectionChanged.connect(self.selection_changed)
        self.refresh()

    def refresh(self):
        scene = self.manager.active
        self.graphics.clear()
        if not scene:
            return
        self.scene_height = 562 if getattr(self, "mode", "horizontal") == "horizontal" else 1000
        self.graphics.setSceneRect(0, 0, self.scene_width, self.scene_height)
        bg = self.graphics.addRect(0, 0, self.scene_width, self.scene_height, QtGui.QPen(QtGui.QColor("#26313f"), 2), QtGui.QBrush(QtGui.QColor("#090d12")))
        bg.setZValue(-100)
        for source in scene.sources:
            if source.enabled:
                item = SourceItem(source, self)
                self.graphics.addItem(item)
        self.fitInView(self.graphics.sceneRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def set_mode(self, mode):
        self.mode = mode
        self.scene_height = 562 if mode == "horizontal" else 1000
        self.refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fitInView(self.graphics.sceneRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def selection_changed(self):
        items = self.graphics.selectedItems()
        self.sourceSelected.emit(items[0].source if items else None)

    def mouseDoubleClickEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if isinstance(item, SourceItem):
            name, ok = QtWidgets.QInputDialog.getText(self, "Renombrar fuente", "Nombre:", text=item.source.name)
            if ok and name.strip():
                item.source.name = name.strip()
                item.setToolTip(item.source.name)
                self.manager.save()
                self.refresh()
        else:
            super().mouseDoubleClickEvent(event)
