"""Dialog for configuring field mappings between old and new APIs."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class FieldMappingDialog(QWidget):
    """Popup dialog that lets the user map old field paths to new ones."""

    applied = Signal(list)

    def __init__(
        self,
        mappings: list[dict],
        missing: list[dict],
        extra: list[dict],
    ) -> None:
        super().__init__()
        self.setWindowTitle("Field Mappings")
        self.setMinimumSize(560, 360)
        l = QVBoxLayout(self)
        l.setContentsMargins(20, 20, 20, 20)
        l.setSpacing(12)

        t = QLabel("Map old API field paths to new API field paths")
        t.setObjectName("sectionTitle")
        l.addWidget(t)

        self.rows: list[dict] = []
        if not mappings:
            seen: set[str] = set()
            for f in missing:
                k = f["path"].rsplit(".", 1)[-1]
                self._add_row(f["path"], k)
                seen.add(f["path"])
            for f in extra:
                k = f["path"].rsplit(".", 1)[-1]
                if f["path"] not in seen:
                    self._add_row(k, f["path"])
        else:
            for m in mappings:
                self._add_row(m.get("oldPath", ""), m.get("newPath", ""))
        if not self.rows:
            self._add_row("", "")

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("cardInner")
        sc = QWidget()
        sc.setObjectName("cardInner")
        self.sl = QVBoxLayout(sc)
        self.sl.setSpacing(4)
        self.scroll.setWidget(sc)
        l.addWidget(self.scroll, 1)

        ab = QPushButton("Add mapping")
        ab.setObjectName("linkBtn")
        ab.setFixedWidth(100)
        ab.clicked.connect(lambda: self._add_row("", ""))
        l.addWidget(ab)

        br = QHBoxLayout()
        br.addStretch()
        cb = QPushButton("Cancel")
        cb.clicked.connect(self.close)
        ap = QPushButton("Apply && Re-run")
        ap.setObjectName("primaryBtn")
        ap.clicked.connect(self._on_apply)
        br.addWidget(cb)
        br.addWidget(ap)
        l.addLayout(br)

    def _add_row(self, old: str = "", new: str = "") -> None:
        w = QWidget()
        w.setObjectName("cardInner")
        rl = QHBoxLayout(w)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        oi = QLineEdit(old)
        oi.setPlaceholderText("old.field.path")
        ar = QLabel("\u2192")
        ar.setObjectName("arrowLabel")
        ni = QLineEdit(new)
        ni.setPlaceholderText("new.field.path")
        rb = QPushButton("\u00d7")
        rb.setObjectName("removeBtn")
        rb.setFixedSize(26, 26)
        d: dict = {"widget": w, "oldPath": oi, "newPath": ni}
        self.rows.append(d)
        rb.clicked.connect(lambda: self._rm(w))
        rl.addWidget(oi, 1)
        rl.addWidget(ar)
        rl.addWidget(ni, 1)
        rl.addWidget(rb)
        self.sl.addWidget(w)

    def _rm(self, w: QWidget) -> None:
        self.rows[:] = [r for r in self.rows if r["widget"] is not w]
        w.deleteLater()

    def _on_apply(self) -> None:
        m = [
            {
                "oldPath": r["oldPath"].text().strip(),
                "newPath": r["newPath"].text().strip(),
            }
            for r in self.rows
            if r["oldPath"].text().strip() and r["newPath"].text().strip()
        ]
        self.applied.emit(m)
        self.close()
