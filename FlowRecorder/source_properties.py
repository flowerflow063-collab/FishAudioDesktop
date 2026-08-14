from PySide6 import QtCore, QtWidgets


class SourceProperties(QtWidgets.QFrame):
    changed = QtCore.Signal()

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.source = None
        self.setObjectName("card")
        box = QtWidgets.QVBoxLayout(self)
        box.addWidget(QtWidgets.QLabel("PROPIEDADES DE FUENTE"))
        form = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit()
        self.x = QtWidgets.QDoubleSpinBox(); self.x.setRange(0, 1); self.x.setSingleStep(.01)
        self.y = QtWidgets.QDoubleSpinBox(); self.y.setRange(0, 1); self.y.setSingleStep(.01)
        self.width = QtWidgets.QDoubleSpinBox(); self.width.setRange(.01, 1); self.width.setSingleStep(.01)
        self.height = QtWidgets.QDoubleSpinBox(); self.height.setRange(.01, 1); self.height.setSingleStep(.01)
        self.enabled = QtWidgets.QCheckBox("Fuente visible")
        for widget in (self.name, self.x, self.y, self.width, self.height):
            widget.setEnabled(False)
        self.enabled.setEnabled(False)
        form.addRow("Nombre", self.name)
        form.addRow("X", self.x)
        form.addRow("Y", self.y)
        form.addRow("Ancho", self.width)
        form.addRow("Alto", self.height)
        form.addRow("", self.enabled)
        box.addLayout(form)
        for widget in (self.name, self.x, self.y, self.width, self.height):
            widget.editingFinished.connect(self.apply)
        self.enabled.toggled.connect(self.apply)

    def set_source(self, source):
        self.source = source
        active = source is not None
        for widget in (self.name, self.x, self.y, self.width, self.height, self.enabled):
            widget.setEnabled(active)
        if not active:
            return
        self.name.setText(source.name)
        self.x.setValue(source.x); self.y.setValue(source.y)
        self.width.setValue(source.width); self.height.setValue(source.height)
        self.enabled.setChecked(source.enabled)

    def apply(self):
        if not self.source:
            return
        self.source.name = self.name.text().strip() or self.source.name
        self.source.x = self.x.value(); self.source.y = self.y.value()
        self.source.width = self.width.value(); self.source.height = self.height.value()
        self.source.enabled = self.enabled.isChecked()
        self.manager.save()
        self.changed.emit()
