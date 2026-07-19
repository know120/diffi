"""Expandable card that shows a single ID's comparison result."""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ResultCard(QWidget):
    """Collapsible card for one ID's result (diff badges, timing, body)."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__()
        self._expanded = False
        self._result = result
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(8)

        r = self._result
        error = r.get("error")
        has_diff = bool(
            r.get("missing") or r.get("extra") or r.get("typeChanges")
        )

        hdr = QHBoxLayout()
        hdr.setSpacing(10)
        idl = QLabel(f"ID: {r.get('id', '?')}")
        idl.setObjectName("cardTitle")
        hdr.addWidget(idl)

        rt = r.get("responseTimes", {})
        if rt:
            if "old" in rt and "new" in rt:
                rt_t = (
                    f"\u23f1 {rt['old']:.3f}s \u2192 {rt['new']:.3f}s"
                )
            else:
                rt_t = f"\u23f1 {rt.get('single', 0):.3f}s"
            b = QLabel(rt_t)
            b.setObjectName("badgeBlue")
            hdr.addWidget(b)

        sc = r.get("statusCodes", {})
        if sc:
            if "old" in sc and "new" in sc:
                if r.get("statusCodeDiff"):
                    st = f"{sc['old']} \u2192 {sc['new']}"
                    obj = "badgeRed"
                else:
                    st = f"{sc['old']}"
                    obj = "badgeGreen"
            else:
                st = f"{sc.get('single', '?')}"
                obj = "badgeGreen"
            b = QLabel(st)
            b.setObjectName(obj)
            hdr.addWidget(b)

        if error:
            badge, obj = "Error", "badgeRed"
        elif has_diff:
            m = len(r.get("missing", []))
            e = len(r.get("extra", []))
            t = len(r.get("typeChanges", []))
            badge, obj = f"{m}M \u00b7 {e}E \u00b7 {t}T", "badgeYellow"
        else:
            badge, obj = "Identical", "badgeGreen"
        bl = QLabel(badge)
        bl.setObjectName(obj)
        hdr.addWidget(bl)
        hdr.addStretch()

        self._tb = QPushButton("\u25b6")
        self._tb.setObjectName("toggleBtn")
        self._tb.setFixedSize(24, 24)
        self._tb.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tb.clicked.connect(self._toggle)
        hdr.addWidget(self._tb)
        cl.addLayout(hdr)

        # -- expanded body --
        self._body = QWidget()
        self._body.setObjectName("cardInner")
        self._body.setVisible(False)
        bl2 = QVBoxLayout(self._body)
        bl2.setContentsMargins(0, 4, 0, 0)
        bl2.setSpacing(6)
        if error:
            el = QLabel(error)
            el.setWordWrap(True)
            el.setObjectName("errorBox")
            bl2.addWidget(el)
        else:
            # field diffs
            for title, items, obj in [
                ("Missing Fields", r.get("missing", []), "missingLabel"),
                ("Extra Fields", r.get("extra", []), "extraLabel"),
                (
                    "Type Changes",
                    r.get("typeChanges", []),
                    "typeLabel",
                ),
            ]:
                if items:
                    h = QLabel(title)
                    h.setObjectName(obj)
                    bl2.addWidget(h)
                    for it in items:
                        if obj == "typeLabel":
                            txt = (
                                f"  {it['path']}: "
                                f"{it['oldType']} \u2192 {it['newType']}"
                            )
                        else:
                            txt = (
                                f"  {it['path']} \u2192 "
                                f"{json.dumps(it.get('value'))}"
                            )
                        lbl = QLabel(txt)
                        lbl.setTextInteractionFlags(
                            Qt.TextInteractionFlag.TextSelectableByMouse
                        )
                        bl2.addWidget(lbl)

        cl.addWidget(self._body)
        outer.addWidget(card)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._tb.setText("\u25bc" if self._expanded else "\u25b6")
