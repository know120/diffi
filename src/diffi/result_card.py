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
        cl.setContentsMargins(16, 12, 16, 12)
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

        # response size badge
        rs = r.get("responseSizes", {})
        if rs:
            if "old" in rs and "new" in rs:
                sz_txt = _fmt_size(rs["old"])
                if rs["old"] != rs["new"]:
                    sz_txt += f" \u2192 {_fmt_size(rs['new'])}"
                b = QLabel(sz_txt)
                b.setObjectName("badgeBlue")
                hdr.addWidget(b)
            elif "single" in rs:
                b = QLabel(_fmt_size(rs["single"]))
                b.setObjectName("badgeBlue")
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

            # JSON diff view (#10)
            if "oldData" in r and "newData" in r:
                diff_lbl = QLabel("JSON Diff")
                diff_lbl.setObjectName("fieldLabel")
                bl2.addWidget(diff_lbl)
                diff_text = _format_json_diff(r["oldData"], r["newData"])
                te = QPlainTextEdit()
                te.setReadOnly(True)
                te.setPlainText(diff_text)
                te.setMaximumHeight(200)
                te.setObjectName("diffView")
                bl2.addWidget(te)
            elif "data" in r:
                raw_lbl = QLabel("Response")
                raw_lbl.setObjectName("fieldLabel")
                bl2.addWidget(raw_lbl)
                te = QPlainTextEdit()
                te.setReadOnly(True)
                te.setPlainText(json.dumps(r["data"], indent=2))
                te.setMaximumHeight(200)
                bl2.addWidget(te)

        cl.addWidget(self._body)
        outer.addWidget(card)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._tb.setText("\u25bc" if self._expanded else "\u25b6")


# -- helpers ------------------------------------------------------------------


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def _format_json_diff(old: Any, new: Any, path: str = "") -> str:
    """Return a human-readable line-by-line diff of two JSON trees."""
    lines: list[str] = []
    _diff_walk(old, new, path, lines)
    return "\n".join(lines) if lines else "(no differences)"


def _diff_walk(
    old: Any, new: Any, path: str, lines: list[str], depth: int = 0
) -> None:
    indent = "  " * depth
    if isinstance(old, dict) and isinstance(new, dict):
        all_keys = sorted(set(old) | set(new))
        for k in all_keys:
            p = f"{path}.{k}" if path else k
            if k not in old:
                lines.append(f"{indent}+ {p}: {json.dumps(new[k])}")
            elif k not in new:
                lines.append(f"{indent}- {p}: {json.dumps(old[k])}")
            else:
                _diff_walk(old[k], new[k], p, lines, depth)
    elif isinstance(old, list) and isinstance(new, list):
        for i in range(max(len(old), len(new))):
            p = f"{path}[{i}]"
            if i >= len(old):
                lines.append(f"{indent}+ {p}: {json.dumps(new[i])}")
            elif i >= len(new):
                lines.append(f"{indent}- {p}: {json.dumps(old[i])}")
            else:
                _diff_walk(old[i], new[i], p, lines, depth)
    elif old != new:
        lines.append(
            f"{indent}~ {path}: {json.dumps(old)} \u2192 {json.dumps(new)}"
        )
