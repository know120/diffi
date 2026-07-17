from __future__ import annotations

import base64
import csv
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QMessageBox,
)

from .comparator import apply_field_mappings, collect_fields, deep_compare

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
TIMEOUT = 30
HISTORY_FILE = Path.home() / ".diffi_history.json"
PROFILES_FILE = Path.home() / ".diffi_profiles.json"
MAX_HISTORY = 50

AUTH_TYPES = ["None", "Basic Auth", "Bearer Token", "OAuth2"]


def load_json(path: Path) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# ApiWorker
# ---------------------------------------------------------------------------


class ApiWorker(QThread):
    finished = Signal(object)
    error_signal = Signal(str, str)

    def __init__(
        self,
        configs: list[dict],
        ids: list[str],
        mappings: list[dict],
        ignore_fields: set[str] | None = None,
        concurrency: int = 1,
        parent=None,
    ):
        super().__init__(parent)
        self.configs = configs
        self.ids = ids
        self.mappings = mappings
        self.ignore_fields = ignore_fields or set()
        self.concurrency = max(1, concurrency)

    def run(self):
        results: list[dict] = []
        if self.concurrency > 1:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                futures = {pool.submit(self._process_id, id_): id_ for id_ in self.ids}
                for future in as_completed(futures):
                    results.append(future.result())
            id_order = {id_: i for i, id_ in enumerate(self.ids)}
            results.sort(key=lambda r: id_order.get(r["id"], 0))
        else:
            for id_ in self.ids:
                results.append(self._process_id(id_))
        self.finished.emit(results)

    def _process_id(self, id_: str) -> dict:
        id_results: dict[str, Any] = {"id": id_}
        try:
            datas: list[Any] = []
            response_times: list[float] = []
            status_codes: list[int] = []
            for config in self.configs:
                data, elapsed, status_code = self._call_api(config, id_)
                datas.append(data)
                response_times.append(elapsed)
                status_codes.append(status_code)

            if len(datas) == 2:
                old_data, raw_new = datas
                new_data = (
                    apply_field_mappings(raw_new, self.mappings)
                    if self.mappings
                    else raw_new
                )
                comparison = deep_compare(
                    old_data, new_data, ignore_fields=self.ignore_fields
                )
                id_results["oldData"] = old_data
                id_results["newData"] = new_data
                id_results["rawNewData"] = raw_new if self.mappings else None
                id_results["oldFields"] = collect_fields(old_data)
                id_results["newFields"] = collect_fields(new_data)
                id_results["responseTimes"] = {
                    "old": response_times[0],
                    "new": response_times[1],
                }
                id_results["statusCodes"] = {
                    "old": status_codes[0],
                    "new": status_codes[1],
                }
                id_results["statusCodeDiff"] = status_codes[0] != status_codes[1]
                id_results.update(comparison)
            else:
                data = datas[0]
                id_results["data"] = data
                id_results["fields"] = collect_fields(data)
                id_results["responseTimes"] = {"single": response_times[0]}
                id_results["statusCodes"] = {"single": status_codes[0]}
            id_results["error"] = None
        except Exception as e:
            id_results["error"] = str(e)
            id_results.setdefault("missing", [])
            id_results.setdefault("extra", [])
            id_results.setdefault("typeChanges", [])
        return id_results

    def _call_api(self, config: dict, id_: str) -> tuple[Any, float, int]:
        url = config["url"].replace("{{id}}", str(id_))
        method = config.get("method", "GET").upper()

        try:
            from urllib.parse import urlencode, urlparse, urlunparse

            parsed = list(urlparse(url))
            params = config.get("params", {})
            if params:
                existing = parsed[4]
                qs = urlencode(
                    {k: v.replace("{{id}}", str(id_)) for k, v in params.items()}
                )
                parsed[4] = (existing + "&" + qs) if existing else qs
            full_url = urlunparse(parsed)
        except Exception:
            raise ValueError(f'Invalid URL: "{url}". Must start with http:// or https://')

        headers = dict(config.get("headers", {}))
        auth_config = config.get("auth", {"type": "none"})
        self._apply_auth(headers, auth_config, id_)

        fetch_options: dict[str, Any] = {"method": method, "headers": headers}

        if method in ("POST", "PUT", "PATCH") and config.get("body"):
            body = config["body"].replace("{{id}}", str(id_))
            fetch_options["data"] = body
            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"

        start = time.time()
        try:
            res = requests.request(
                method, full_url, headers=headers,
                data=fetch_options.get("data"), timeout=TIMEOUT,
            )
            elapsed = round(time.time() - start, 3)
            if not res.ok:
                raise ValueError(f"HTTP {res.status_code}: {res.reason}")
            return res.json(), elapsed, res.status_code
        except requests.exceptions.Timeout:
            raise ValueError(f"Request timed out after {TIMEOUT}s: {method} {full_url}")
        except requests.exceptions.ConnectionError:
            raise ValueError(
                f"Network error: {method} {full_url}\n\n"
                f"Possible causes:\n"
                f"\u2022 The server is unreachable or DNS resolution failed\n"
                f"\u2022 The URL is incorrect or missing a protocol\n"
                f"\u2022 A firewall is blocking the connection"
            )

    def _apply_auth(self, headers: dict, auth_config: dict, id_: str):
        auth_type = auth_config.get("type", "none")
        if auth_type == "basic":
            user = auth_config.get("username", "").replace("{{id}}", str(id_))
            pwd = auth_config.get("password", "").replace("{{id}}", str(id_))
            if user:
                token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
                headers["Authorization"] = f"Basic {token}"
        elif auth_type == "bearer":
            token = auth_config.get("token", "").replace("{{id}}", str(id_))
            if token:
                headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "oauth2":
            token = self._get_oauth2_token(auth_config, id_)
            if token:
                headers["Authorization"] = f"Bearer {token}"

    def _get_oauth2_token(self, config: dict, id_: str) -> str | None:
        token_url = config.get("token_url", "").replace("{{id}}", str(id_))
        client_id = config.get("client_id", "").replace("{{id}}", str(id_))
        client_secret = config.get("client_secret", "").replace("{{id}}", str(id_))
        if not token_url:
            return None
        try:
            res = requests.post(
                token_url,
                data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
                timeout=TIMEOUT,
            )
            res.raise_for_status()
            return res.json().get("access_token")
        except Exception:
            return None


# ---------------------------------------------------------------------------
# ApiFormWidget
# ---------------------------------------------------------------------------


class ApiFormWidget(QWidget):
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        wrapper = QFrame()
        wrapper.setObjectName("card")
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(20, 16, 20, 16)
        wl.setSpacing(12)

        title = QLabel(label)
        title.setObjectName("sectionTitle")
        wl.addWidget(title)

        # Row: URL + Method + Auth
        r1 = QHBoxLayout()
        r1.setSpacing(10)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://api.example.com/v1/{{id}}")
        r1.addWidget(self.url_input, 1)

        self.method_combo = QComboBox()
        self.method_combo.addItems(METHODS)
        self.method_combo.setFixedWidth(80)
        r1.addWidget(self.method_combo)

        self.auth_combo = QComboBox()
        self.auth_combo.addItems(AUTH_TYPES)
        self.auth_combo.setFixedWidth(110)
        self.auth_combo.currentTextChanged.connect(self._on_auth_changed)
        r1.addWidget(self.auth_combo)
        wl.addLayout(r1)

        # Auth fields
        self._auth_basic_user = QLineEdit(); self._auth_basic_user.setPlaceholderText("Username")
        self._auth_basic_pwd = QLineEdit(); self._auth_basic_pwd.setPlaceholderText("Password"); self._auth_basic_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self._auth_bearer_token = QLineEdit(); self._auth_bearer_token.setPlaceholderText("Bearer token")
        self._auth_oauth2_client_id = QLineEdit(); self._auth_oauth2_client_id.setPlaceholderText("Client ID")
        self._auth_oauth2_client_secret = QLineEdit(); self._auth_oauth2_client_secret.setPlaceholderText("Client secret"); self._auth_oauth2_client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._auth_oauth2_token_url = QLineEdit(); self._auth_oauth2_token_url.setPlaceholderText("Token URL")

        self._auth_widgets: dict[str, list[QWidget]] = {
            "None": [],
            "Basic Auth": [self._auth_basic_user, self._auth_basic_pwd],
            "Bearer Token": [self._auth_bearer_token],
            "OAuth2": [self._auth_oauth2_client_id, self._auth_oauth2_client_secret, self._auth_oauth2_token_url],
        }
        self._auth_wrap = QWidget()
        self._auth_wrap.setObjectName("cardInner")
        ar = QHBoxLayout(self._auth_wrap)
        ar.setContentsMargins(0, 0, 0, 0)
        ar.setSpacing(8)
        for w in sum(self._auth_widgets.values(), []):
            ar.addWidget(w)
            w.setVisible(False)
        ar.addStretch()
        wl.addWidget(self._auth_wrap)
        self._auth_wrap.setVisible(False)

        # Headers
        self.header_rows: list[dict] = []
        hlbl = QLabel("Headers")
        hlbl.setObjectName("fieldLabel")
        wl.addWidget(hlbl)
        self.headers_container = QVBoxLayout()
        self.headers_container.setSpacing(4)
        self.headers_container.setContentsMargins(0, 0, 0, 0)
        wl.addLayout(self.headers_container)
        hadd = QPushButton("Add header")
        hadd.setObjectName("linkBtn")
        hadd.setFixedWidth(90)
        hadd.clicked.connect(lambda: self._add_header_row())
        wl.addWidget(hadd)

        # Params
        self.param_rows: list[dict] = []
        plbl = QLabel("Query Parameters")
        plbl.setObjectName("fieldLabel")
        wl.addWidget(plbl)
        self.params_container = QVBoxLayout()
        self.params_container.setSpacing(4)
        self.params_container.setContentsMargins(0, 0, 0, 0)
        wl.addLayout(self.params_container)
        padd = QPushButton("Add parameter")
        padd.setObjectName("linkBtn")
        padd.setFixedWidth(100)
        padd.clicked.connect(lambda: self._add_param_row())
        wl.addWidget(padd)

        # Body
        blbl = QLabel("Request Body")
        blbl.setObjectName("fieldLabel")
        self._body_label = blbl
        wl.addWidget(blbl)
        self.body_input = QPlainTextEdit()
        self.body_input.setPlaceholderText('{"title": "foo", "body": "bar", "userId": {{id}} }')
        self.body_input.setMaximumHeight(72)
        self.body_input.setVisible(False)
        blbl.setVisible(False)
        wl.addWidget(self.body_input)

        self.method_combo.currentTextChanged.connect(self._on_method_changed)
        root.addWidget(wrapper)

        self._add_header_row("Content-Type", "application/json")
        self._add_header_row("Accept", "application/json")

    def _on_method_changed(self, method: str):
        vis = method in ("POST", "PUT", "PATCH")
        self.body_input.setVisible(vis)
        self._body_label.setVisible(vis)

    def _on_auth_changed(self, auth_type: str):
        self._auth_wrap.setVisible(auth_type != "None")
        for w in sum(self._auth_widgets.values(), []):
            w.setVisible(False)
        for w in self._auth_widgets.get(auth_type, []):
            w.setVisible(True)

    def _add_header_row(self, key: str = "", value: str = ""):
        row = self._make_kv(self.header_rows, self.headers_container)
        row["key"].setText(key); row["value"].setText(value)

    def _add_param_row(self):
        self._make_kv(self.param_rows, self.params_container)

    def _make_kv(self, rows: list[dict], container: QVBoxLayout) -> dict:
        w = QWidget(); w.setObjectName("cardInner")
        l = QHBoxLayout(w); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(6)
        cb = QCheckBox(); cb.setChecked(True)
        ki = QLineEdit(); ki.setPlaceholderText("Key"); ki.setFixedWidth(140)
        vi = QLineEdit(); vi.setPlaceholderText("Value")
        rb = QPushButton("\u00d7"); rb.setObjectName("removeBtn"); rb.setFixedSize(26, 26)
        d = {"widget": w, "enabled": cb, "key": ki, "value": vi}
        rows.append(d)
        rb.clicked.connect(lambda: self._rm(rows, w))
        l.addWidget(cb); l.addWidget(ki); l.addWidget(vi, 1); l.addWidget(rb)
        container.addWidget(w)
        return d

    def _rm(self, rows: list[dict], w: QWidget):
        rows[:] = [r for r in rows if r["widget"] is not w]; w.deleteLater()

    def get_config(self) -> dict:
        headers = {r["key"].text().strip(): r["value"].text() for r in self.header_rows if r["enabled"].isChecked() and r["key"].text().strip()}
        params = {r["key"].text().strip(): r["value"].text() for r in self.param_rows if r["enabled"].isChecked() and r["key"].text().strip()}
        at = self.auth_combo.currentText()
        auth: dict[str, str] = {"type": at.lower().replace(" ", "_")}
        if at == "Basic Auth": auth["username"] = self._auth_basic_user.text(); auth["password"] = self._auth_basic_pwd.text()
        elif at == "Bearer Token": auth["token"] = self._auth_bearer_token.text()
        elif at == "OAuth2": auth["client_id"] = self._auth_oauth2_client_id.text(); auth["client_secret"] = self._auth_oauth2_client_secret.text(); auth["token_url"] = self._auth_oauth2_token_url.text()
        return {"url": self.url_input.text(), "method": self.method_combo.currentText(), "headers": headers, "params": params, "body": self.body_input.toPlainText(), "auth": auth}

    def set_config(self, config: dict):
        self.url_input.setText(config.get("url", ""))
        idx = self.method_combo.findText(config.get("method", "GET"))
        if idx >= 0: self.method_combo.setCurrentIndex(idx)
        auth = config.get("auth", {})
        am = {"none": "None", "basic_auth": "Basic Auth", "bearer_token": "Bearer Token", "oauth2": "OAuth2"}
        at = am.get(auth.get("type", "none"), "None")
        idx = self.auth_combo.findText(at)
        if idx >= 0: self.auth_combo.setCurrentIndex(idx)
        if at == "Basic Auth": self._auth_basic_user.setText(auth.get("username", "")); self._auth_basic_pwd.setText(auth.get("password", ""))
        elif at == "Bearer Token": self._auth_bearer_token.setText(auth.get("token", ""))
        elif at == "OAuth2": self._auth_oauth2_client_id.setText(auth.get("client_id", "")); self._auth_oauth2_client_secret.setText(auth.get("client_secret", "")); self._auth_oauth2_token_url.setText(auth.get("token_url", ""))


# ---------------------------------------------------------------------------
# FieldMappingDialog
# ---------------------------------------------------------------------------


class FieldMappingDialog(QWidget):
    applied = Signal(list)

    def __init__(self, mappings: list[dict], missing: list[dict], extra: list[dict]):
        super().__init__()
        self.setWindowTitle("Field Mappings")
        self.setMinimumSize(560, 360)
        l = QVBoxLayout(self); l.setContentsMargins(20, 20, 20, 20); l.setSpacing(12)

        t = QLabel("Map old API field paths to new API field paths")
        t.setObjectName("sectionTitle")
        l.addWidget(t)

        self.rows: list[dict] = []
        if not mappings:
            seen = set()
            for f in missing:
                k = f["path"].rsplit(".", 1)[-1]; self._add_row(f["path"], k); seen.add(f["path"])
            for f in extra:
                k = f["path"].rsplit(".", 1)[-1]
                if f["path"] not in seen: self._add_row(k, f["path"])
        else:
            for m in mappings: self._add_row(m.get("oldPath", ""), m.get("newPath", ""))
        if not self.rows: self._add_row("", "")

        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setObjectName("cardInner")
        sc = QWidget(); sc.setObjectName("cardInner")
        self.sl = QVBoxLayout(sc); self.sl.setSpacing(4)
        self.scroll.setWidget(sc)
        l.addWidget(self.scroll, 1)

        ab = QPushButton("Add mapping"); ab.setObjectName("linkBtn"); ab.setFixedWidth(100)
        ab.clicked.connect(lambda: self._add_row("", ""))
        l.addWidget(ab)

        br = QHBoxLayout(); br.addStretch()
        cb = QPushButton("Cancel"); cb.clicked.connect(self.close)
        ap = QPushButton("Apply && Re-run"); ap.setObjectName("primaryBtn"); ap.clicked.connect(self._on_apply)
        br.addWidget(cb); br.addWidget(ap)
        l.addLayout(br)

    def _add_row(self, old: str = "", new: str = ""):
        w = QWidget(); w.setObjectName("cardInner")
        rl = QHBoxLayout(w); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(8)
        oi = QLineEdit(old); oi.setPlaceholderText("old.field.path")
        ar = QLabel("\u2192"); ar.setObjectName("arrowLabel")
        ni = QLineEdit(new); ni.setPlaceholderText("new.field.path")
        rb = QPushButton("\u00d7"); rb.setObjectName("removeBtn"); rb.setFixedSize(26, 26)
        d = {"widget": w, "oldPath": oi, "newPath": ni}; self.rows.append(d)
        rb.clicked.connect(lambda: self._rm(w))
        rl.addWidget(oi, 1); rl.addWidget(ar); rl.addWidget(ni, 1); rl.addWidget(rb)
        self.sl.addWidget(w)

    def _rm(self, w):
        self.rows[:] = [r for r in self.rows if r["widget"] is not w]; w.deleteLater()

    def _on_apply(self):
        m = [{"oldPath": r["oldPath"].text().strip(), "newPath": r["newPath"].text().strip()} for r in self.rows if r["oldPath"].text().strip() and r["newPath"].text().strip()]
        self.applied.emit(m); self.close()


# ---------------------------------------------------------------------------
# ResultCard
# ---------------------------------------------------------------------------


class ResultCard(QWidget):
    def __init__(self, result: dict):
        super().__init__()
        self._expanded = False
        self._result = result
        self._build()

    def _build(self):
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)
        card = QFrame(); card.setObjectName("card")
        cl = QVBoxLayout(card); cl.setContentsMargins(16, 12, 16, 12); cl.setSpacing(8)

        r = self._result
        error = r.get("error")
        has_diff = bool(r.get("missing") or r.get("extra") or r.get("typeChanges"))

        hdr = QHBoxLayout(); hdr.setSpacing(10)
        idl = QLabel(f"ID: {r.get('id', '?')}"); idl.setObjectName("cardTitle")
        hdr.addWidget(idl)

        rt = r.get("responseTimes", {})
        if rt:
            if "old" in rt and "new" in rt: rt_t = f"\u23f1 {rt['old']:.3f}s \u2192 {rt['new']:.3f}s"
            else: rt_t = f"\u23f1 {rt.get('single', 0):.3f}s"
            b = QLabel(rt_t); b.setObjectName("badgeBlue"); hdr.addWidget(b)

        sc = r.get("statusCodes", {})
        if sc:
            if "old" in sc and "new" in sc:
                if r.get("statusCodeDiff"): st, obj = f"{sc['old']} \u2192 {sc['new']}", "badgeRed"
                else: st, obj = f"{sc['old']}", "badgeGreen"
            else: st, obj = f"{sc.get('single', '?')}", "badgeGreen"
            b = QLabel(st); b.setObjectName(obj); hdr.addWidget(b)

        if error: badge, obj = "Error", "badgeRed"
        elif has_diff:
            m, e, t = len(r.get("missing", [])), len(r.get("extra", [])), len(r.get("typeChanges", []))
            badge, obj = f"{m}M \u00b7 {e}E \u00b7 {t}T", "badgeYellow"
        else: badge, obj = "Identical", "badgeGreen"
        bl = QLabel(badge); bl.setObjectName(obj); hdr.addWidget(bl)
        hdr.addStretch()

        self._tb = QPushButton("\u25b6"); self._tb.setObjectName("toggleBtn"); self._tb.setFixedSize(24, 24)
        self._tb.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tb.clicked.connect(self._toggle); hdr.addWidget(self._tb)
        cl.addLayout(hdr)

        self._body = QWidget(); self._body.setObjectName("cardInner"); self._body.setVisible(False)
        bl2 = QVBoxLayout(self._body); bl2.setContentsMargins(0, 4, 0, 0); bl2.setSpacing(6)
        if error:
            el = QLabel(error); el.setWordWrap(True); el.setObjectName("errorBox"); bl2.addWidget(el)
        else:
            for title, items, obj in [("Missing Fields", r.get("missing", []), "missingLabel"), ("Extra Fields", r.get("extra", []), "extraLabel"), ("Type Changes", r.get("typeChanges", []), "typeLabel")]:
                if items:
                    h = QLabel(title); h.setObjectName(obj); bl2.addWidget(h)
                    for it in items:
                        if obj == "typeLabel": txt = f"  {it['path']}: {it['oldType']} \u2192 {it['newType']}"
                        else: txt = f"  {it['path']} \u2192 {json.dumps(it.get('value'))}"
                        bl2.addWidget(QLabel(txt))
        cl.addWidget(self._body)
        outer.addWidget(card)

    def _toggle(self):
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._tb.setText("\u25bc" if self._expanded else "\u25b6")


# ---------------------------------------------------------------------------
# DiffiWindow
# ---------------------------------------------------------------------------


class DiffiWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Diffi \u2014 API Comparator")
        self.setMinimumSize(1000, 740)
        self._mode = "compare"
        self._results: list[dict] | None = None
        self._field_mappings: list[dict] = []
        self._history: list[dict] = load_json(HISTORY_FILE) or []
        self._profiles: dict[str, dict] = load_json(PROFILES_FILE) or {}
        self._build_ui()
        self._apply_styles()
        self._refresh_history_list()
        self._refresh_profile_combo()

    def _apply_styles(self):
        self.setStyleSheet("""
            * { font-family: "Segoe UI", "SF Pro Display", "Helvetica Neue", sans-serif; }
            QMainWindow, QWidget#central { background: #1e1e2e; }
            QWidget { color: #cdd6f4; font-size: 13px; }
            QLabel { background: transparent; }
            QLabel#appTitle { font-size: 22px; font-weight: 700; color: #cdd6f4; letter-spacing: -0.3px; }
            QLabel#appSub { font-size: 13px; color: #6c7086; font-weight: 400; }
            QLabel#sectionTitle { font-size: 14px; font-weight: 600; color: #bac2de; margin-bottom: 4px; }
            QLabel#fieldLabel { font-size: 12px; font-weight: 500; color: #6c7086; margin-top: 6px; }
            QLabel#cardTitle { font-size: 13px; font-weight: 600; color: #cdd6f4; }
            QLabel#arrowLabel { color: #585b70; font-size: 15px; background: transparent; padding: 0 2px; }
            QLabel#missingLabel { font-size: 12px; font-weight: 600; color: #f38ba8; background: transparent; margin-top: 4px; }
            QLabel#extraLabel { font-size: 12px; font-weight: 600; color: #f9e2af; background: transparent; margin-top: 4px; }
            QLabel#typeLabel { font-size: 12px; font-weight: 600; color: #89b4fa; background: transparent; margin-top: 4px; }

            QFrame#card {
                background: #181825;
                border: 1px solid #313244;
                border-radius: 10px;
            }
            QWidget#cardInner { background: transparent; }

            QLineEdit, QPlainTextEdit {
                background: #11111b;
                color: #cdd6f4;
                border: 1px solid #313244;
                border-radius: 8px;
                padding: 7px 12px;
                font-size: 13px;
                selection-background-color: #45475a;
            }
            QLineEdit:focus, QPlainTextEdit:focus { border-color: #89b4fa; }
            QLineEdit:read-only { background: #181825; }

            QComboBox {
                background: #11111b;
                color: #cdd6f4;
                border: 1px solid #313244;
                border-radius: 8px;
                padding: 6px 10px;
                font-size: 13px;
            }
            QComboBox:focus { border-color: #89b4fa; }
            QComboBox::drop-down { border: none; width: 22px; }
            QComboBox QAbstractItemView {
                background: #1e1e2e;
                color: #cdd6f4;
                border: 1px solid #313244;
                border-radius: 8px;
                padding: 4px;
                selection-background-color: #45475a;
                outline: none;
            }

            QPushButton {
                background: #1e1e2e;
                color: #a6adc8;
                border: 1px solid #313244;
                border-radius: 8px;
                padding: 7px 16px;
                font-size: 13px;
            }
            QPushButton:hover { background: #313244; color: #cdd6f4; border-color: #45475a; }
            QPushButton:pressed { background: #45475a; }
            QPushButton:disabled { color: #45475a; background: #181825; border-color: #313244; }

            QPushButton#primaryBtn {
                background: #89b4fa;
                color: #1e1e2e;
                border: none;
                font-weight: 600;
                padding: 8px 20px;
            }
            QPushButton#primaryBtn:hover { background: #b4d0fb; }

            QPushButton#greenBtn {
                background: #a6e3a1;
                color: #1e1e2e;
                border: none;
                font-weight: 600;
                padding: 0;
            }
            QPushButton#greenBtn:hover { background: #c4eec2; }
            QPushButton#greenBtn:disabled { background: #313244; color: #585b70; }

            QPushButton#iconBtn {
                background: transparent;
                color: #6c7086;
                border: 1px solid transparent;
                border-radius: 8px;
                padding: 4px;
                font-size: 16px;
            }
            QPushButton#iconBtn:hover { background: #313244; color: #cdd6f4; border-color: #45475a; }
            QPushButton#iconBtn:pressed { background: #45475a; }

            QPushButton#linkBtn {
                background: transparent;
                color: #89b4fa;
                border: none;
                padding: 2px 0;
                font-size: 12px;
            }
            QPushButton#linkBtn:hover { color: #b4d0fb; }

            QPushButton#removeBtn {
                background: transparent;
                color: #585b70;
                border: 1px solid transparent;
                border-radius: 6px;
                font-size: 14px;
                font-weight: 600;
            }
            QPushButton#removeBtn:hover { color: #f38ba8; background: #f38ba822; border-color: #f38ba844; }

            QPushButton#toggleBtn {
                background: transparent;
                color: #585b70;
                border: none;
                font-size: 12px;
            }
            QPushButton#toggleBtn:hover { color: #cdd6f4; }

            QLabel#badgeGreen { background: #a6e3a122; color: #a6e3a1; padding: 2px 10px; border-radius: 10px; font-size: 11px; font-weight: 500; }
            QLabel#badgeRed { background: #f38ba822; color: #f38ba8; padding: 2px 10px; border-radius: 10px; font-size: 11px; font-weight: 500; }
            QLabel#badgeYellow { background: #f9e2af22; color: #f9e2af; padding: 2px 10px; border-radius: 10px; font-size: 11px; font-weight: 500; }
            QLabel#badgeBlue { background: #89b4fa22; color: #89b4fa; padding: 2px 10px; border-radius: 10px; font-size: 11px; font-weight: 500; }

            QLabel#errorBox {
                color: #f38ba8; padding: 12px; background: #f38ba811;
                border: 1px solid #f38ba833; border-radius: 8px; font-size: 12px;
            }

            QCheckBox { color: #a6adc8; spacing: 6px; }
            QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #45475a; border-radius: 4px; background: #11111b; }
            QCheckBox::indicator:checked { background: #89b4fa; border-color: #89b4fa; }

            QSpinBox {
                background: #11111b; color: #cdd6f4;
                border: 1px solid #313244; border-radius: 8px;
                padding: 5px 8px; font-size: 13px;
            }
            QSpinBox:focus { border-color: #89b4fa; }

            QTabWidget::pane { border: 1px solid #313244; border-radius: 10px; background: #181825; top: -1px; padding: 4px; }
            QTabBar::tab {
                background: transparent; color: #6c7086;
                padding: 8px 20px; border: none; font-size: 13px; font-weight: 500;
                border-bottom: 2px solid transparent; margin-right: 4px;
            }
            QTabBar::tab:selected { color: #cdd6f4; border-bottom-color: #89b4fa; }
            QTabBar::tab:hover { color: #a6adc8; }

            QProgressBar { border: none; background: #313244; max-height: 4px; border-radius: 2px; }
            QProgressBar::chunk { background: #89b4fa; border-radius: 2px; }

            QDockWidget { color: #cdd6f4; }
            QDockWidget::title { background: #181825; padding: 10px 14px; font-weight: 600; font-size: 13px; border-bottom: 1px solid #313244; }

            QListWidget {
                background: #11111b; color: #cdd6f4;
                border: 1px solid #313244; border-radius: 8px; font-size: 12px; outline: none;
            }
            QListWidget::item { padding: 8px 10px; border-bottom: 1px solid #1e1e2e; }
            QListWidget::item:selected { background: #45475a; color: #cdd6f4; }
            QListWidget::item:hover:!selected { background: #1e1e2e; }

            QScrollBar:vertical { background: transparent; width: 8px; }
            QScrollBar::handle:vertical { background: #313244; border-radius: 4px; min-height: 24px; }
            QScrollBar::handle:vertical:hover { background: #45475a; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

            QSplitter::handle { background: #313244; height: 1px; }
        """)

    def _build_ui(self):
        central = QWidget(); central.setObjectName("central"); self.setCentralWidget(central)
        layout = QVBoxLayout(central); layout.setContentsMargins(28, 24, 28, 24); layout.setSpacing(0)

        # -- Header --
        hdr = QHBoxLayout(); hdr.setSpacing(8)
        t = QLabel("Diffi"); t.setObjectName("appTitle"); hdr.addWidget(t)
        s = QLabel("API Comparator"); s.setObjectName("appSub"); hdr.addWidget(s)
        hdr.addStretch()

        ic = QPushButton("⬇"); ic.setObjectName("iconBtn"); ic.setFixedSize(34, 34); ic.setToolTip("Import Configuration"); ic.clicked.connect(self._on_import_config); hdr.addWidget(ic)
        ec = QPushButton("⬆"); ec.setObjectName("iconBtn"); ec.setFixedSize(34, 34); ec.setToolTip("Export Configuration"); ec.clicked.connect(self._on_export_config); hdr.addWidget(ec)
        hdr.addSpacing(4)
        envl = QLabel("Environment"); envl.setObjectName("fieldLabel"); hdr.addWidget(envl)
        self._profile_combo = QComboBox(); self._profile_combo.setFixedWidth(130)
        self._profile_combo.currentTextChanged.connect(self._on_profile_selected); hdr.addWidget(self._profile_combo)
        sp = QPushButton("\u2713"); sp.setObjectName("removeBtn"); sp.setFixedSize(30, 30); sp.setToolTip("Save profile"); sp.clicked.connect(self._on_save_profile); hdr.addWidget(sp)
        dp = QPushButton("\u00d7"); dp.setObjectName("removeBtn"); dp.setFixedSize(30, 30); dp.setToolTip("Delete profile"); dp.clicked.connect(self._on_delete_profile); hdr.addWidget(dp)
        layout.addLayout(hdr)
        layout.addSpacing(20)

        # -- Mode dropdown --
        mrow = QHBoxLayout(); mrow.setSpacing(8)
        ml = QLabel("Mode"); ml.setObjectName("fieldLabel"); mrow.addWidget(ml)
        self._mode_combo = QComboBox(); self._mode_combo.setFixedWidth(200); self._mode_combo.setFixedHeight(34)
        self._mode_combo.addItems(["Compare (Old vs New)", "Single API"])
        self._mode_combo.currentIndexChanged.connect(lambda i: self._set_mode("compare" if i == 0 else "single"))
        mrow.addWidget(self._mode_combo); mrow.addStretch()
        layout.addLayout(mrow)
        layout.addSpacing(16)

        # -- Content splitter --
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Top config
        cfg = QWidget(); cfg.setObjectName("central")
        cl = QVBoxLayout(cfg); cl.setContentsMargins(0, 0, 0, 0); cl.setSpacing(12)

        self._api_tabs = QTabWidget()
        self._old_form = ApiFormWidget("Old API")
        self._new_form = ApiFormWidget("New API")
        self._single_form = ApiFormWidget("API")
        self._api_tabs.addTab(self._old_form, "Old API"); self._api_tabs.addTab(self._new_form, "New API")
        cl.addWidget(self._api_tabs)

        # IDs card
        ids_card = QFrame(); ids_card.setObjectName("card")
        il = QVBoxLayout(ids_card); il.setContentsMargins(20, 14, 20, 14); il.setSpacing(10)

        r1 = QHBoxLayout(); r1.setSpacing(8)
        self._ids_input = QLineEdit(); self._ids_input.setPlaceholderText("e.g. 1, 2, 3, 5, 10")
        r1.addWidget(self._ids_input, 1)
        imb = QPushButton("⬇"); imb.setObjectName("iconBtn"); imb.setFixedSize(34, 34); imb.setToolTip("Import IDs from File"); imb.clicked.connect(self._on_import_ids); r1.addWidget(imb)
        il.addLayout(r1)

        r2 = QHBoxLayout(); r2.setSpacing(20)
        r2.addWidget(QLabel("Threads"))
        self._concurrency_spin = QSpinBox(); self._concurrency_spin.setRange(1, 20); self._concurrency_spin.setValue(1); self._concurrency_spin.setFixedWidth(52)
        r2.addWidget(self._concurrency_spin)
        r2.addSpacing(4)
        il2 = QLabel("Ignore Fields"); il2.setObjectName("fieldLabel")
        r2.addWidget(il2)
        self._ignore_input = QLineEdit(); self._ignore_input.setPlaceholderText("timestamp, request_id, user.created_at")
        self._ignore_input.setToolTip("Comma-separated field paths to exclude from comparison.\nSupports prefixes: 'user' ignores 'user.name', 'user.email', etc.")
        r2.addWidget(self._ignore_input, 1)
        il.addLayout(r2)
        cl.addWidget(ids_card)

        # Fetch
        fr = QHBoxLayout(); fr.setSpacing(12)
        self._fetch_btn = QPushButton("\u25b6  Run Comparison"); self._fetch_btn.setObjectName("greenBtn")
        self._fetch_btn.setFixedHeight(40); self._fetch_btn.setFixedWidth(180)
        self._fetch_btn.clicked.connect(self._on_fetch)
        self._progress = QProgressBar(); self._progress.setVisible(False); self._progress.setMaximum(0); self._progress.setFixedWidth(160); self._progress.setFixedHeight(4)
        fr.addWidget(self._fetch_btn); fr.addWidget(self._progress); fr.addStretch()
        cl.addLayout(fr)

        splitter.addWidget(cfg)

        # Results
        res = QWidget(); res.setObjectName("central")
        rl = QVBoxLayout(res); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(8)

        rlbl = QHBoxLayout(); rlbl.setSpacing(8)
        rt = QLabel("Results"); rt.setObjectName("sectionTitle"); rlbl.addWidget(rt); rlbl.addStretch()
        self._mapping_btn = QPushButton("Field Mappings"); self._mapping_btn.setVisible(False); self._mapping_btn.clicked.connect(self._on_configure_mappings); rlbl.addWidget(self._mapping_btn)
        hb = QPushButton("↻"); hb.setObjectName("iconBtn"); hb.setFixedSize(34, 34); hb.setToolTip("Toggle History"); hb.clicked.connect(lambda: self._history_dock.setVisible(not self._history_dock.isVisible())); rlbl.addWidget(hb)
        ej = QPushButton("{}"); ej.setObjectName("iconBtn"); ej.setFixedSize(34, 34); ej.setToolTip("Export as JSON"); ej.clicked.connect(lambda: self._on_export_results("json")); rlbl.addWidget(ej)
        ec2 = QPushButton("☰"); ec2.setObjectName("iconBtn"); ec2.setFixedSize(34, 34); ec2.setToolTip("Export as CSV"); ec2.clicked.connect(lambda: self._on_export_results("csv")); rlbl.addWidget(ec2)
        em = QPushButton("✎"); em.setObjectName("iconBtn"); em.setFixedSize(34, 34); em.setToolTip("Export as Markdown"); em.clicked.connect(lambda: self._on_export_results("markdown")); rlbl.addWidget(em)
        rl.addLayout(rlbl)

        self._error_label = QLabel(); self._error_label.setWordWrap(True); self._error_label.setVisible(False); self._error_label.setObjectName("errorBox")
        rl.addWidget(self._error_label)

        self._results_scroll = QScrollArea(); self._results_scroll.setWidgetResizable(True); self._results_scroll.setStyleSheet("border: none; background: transparent;")
        self._results_content = QWidget(); self._results_content.setObjectName("central")
        self._results_layout_inner = QVBoxLayout(self._results_content); self._results_layout_inner.setSpacing(8)
        self._results_scroll.setWidget(self._results_content)
        rl.addWidget(self._results_scroll)

        splitter.addWidget(res)
        splitter.setStretchFactor(0, 1); splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        # History dock
        self._history_dock = QDockWidget("History", self)
        self._history_dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea)
        hc = QWidget(); hc.setObjectName("card")
        hl = QVBoxLayout(hc); hl.setContentsMargins(12, 12, 12, 12); hl.setSpacing(8)
        self._history_list = QListWidget(); self._history_list.itemDoubleClicked.connect(self._on_history_selected)
        hl.addWidget(self._history_list, 1)
        chb = QPushButton("Clear History"); chb.setObjectName("linkBtn"); chb.clicked.connect(self._on_clear_history); hl.addWidget(chb)
        self._history_dock.setWidget(hc)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._history_dock)
        self._history_dock.setVisible(False)

    # -- Mode --
    def _set_mode(self, mode):
        self._mode = mode
        idx = 0 if mode == "compare" else 1
        if self._mode_combo.currentIndex() != idx:
            self._mode_combo.blockSignals(True)
            self._mode_combo.setCurrentIndex(idx)
            self._mode_combo.blockSignals(False)
        self._api_tabs.clear()
        if mode == "compare":
            self._api_tabs.addTab(self._old_form, "Old API"); self._api_tabs.addTab(self._new_form, "New API")
            self._fetch_btn.setText("\u25b6  Run Comparison")
        else:
            self._api_tabs.addTab(self._single_form, "API")
            self._fetch_btn.setText("\u25b6  Fetch Data")

    # -- Config import/export --
    def _on_import_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Configuration", "", "JSON Files (*.json)")
        if not path: return
        try: data = json.loads(Path(path).read_text())
        except Exception as e: QMessageBox.warning(self, "Import Error", str(e)); return
        mode = data.get("mode", "compare"); self._set_mode(mode)
        if mode == "compare":
            if "old_api" in data: self._old_form.set_config(data["old_api"])
            if "new_api" in data: self._new_form.set_config(data["new_api"])
        else:
            if "api" in data: self._single_form.set_config(data["api"])
        self._ids_input.setText(", ".join(data.get("ids", [])))
        self._field_mappings = data.get("field_mappings", [])
        self._ignore_input.setText(data.get("ignore_fields", ""))
        self._concurrency_spin.setValue(data.get("concurrency", 1))

    def _on_export_config(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Configuration", "diffi_config.json", "JSON Files (*.json)")
        if not path: return
        data: dict[str, Any] = {"mode": self._mode}
        if self._mode == "compare": data["old_api"] = self._old_form.get_config(); data["new_api"] = self._new_form.get_config()
        else: data["api"] = self._single_form.get_config()
        data["ids"] = [s.strip() for s in re.split(r"[,;\s]+", self._ids_input.text()) if s.strip()]
        data["field_mappings"] = self._field_mappings; data["ignore_fields"] = self._ignore_input.text(); data["concurrency"] = self._concurrency_spin.value()
        try: Path(path).write_text(json.dumps(data, indent=2))
        except Exception as e: QMessageBox.warning(self, "Export Error", str(e))

    # -- Export results --
    def _on_export_results(self, fmt):
        if not self._results: QMessageBox.information(self, "No Results", "Run a comparison first."); return
        ext = {"json": "json", "csv": "csv", "markdown": "md"}[fmt]
        path, _ = QFileDialog.getSaveFileName(self, "Export Results", f"diffi_results.{ext}", f"Files (*.{ext})")
        if not path: return
        try:
            if fmt == "json": content = json.dumps(self._results, indent=2, default=str)
            elif fmt == "csv": content = self._to_csv()
            else: content = self._to_md()
            Path(path).write_text(content)
        except Exception as e: QMessageBox.warning(self, "Export Error", str(e))

    def _to_csv(self) -> str:
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow(["id", "status", "error", "missing_fields", "extra_fields", "type_changes", "rt_old", "rt_new", "sc_old", "sc_new"])
        for r in self._results:
            rt = r.get("responseTimes", {}); sc = r.get("statusCodes", {})
            st = "error" if r.get("error") else ("changed" if (r.get("missing") or r.get("extra") or r.get("typeChanges")) else "identical")
            w.writerow([r.get("id", ""), st, r.get("error", ""), "; ".join(f["path"] for f in r.get("missing", [])), "; ".join(f["path"] for f in r.get("extra", [])), "; ".join(f"{t['path']}: {t['oldType']}->{t['newType']}" for t in r.get("typeChanges", [])), rt.get("old", rt.get("single", "")), rt.get("new", ""), sc.get("old", sc.get("single", "")), sc.get("new", "")])
        return buf.getvalue()

    def _to_md(self) -> str:
        L = ["# Diffi Comparison Results", f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", f"**Mode:** {self._mode.title()}"]
        t = len(self._results); e = sum(1 for r in self._results if r.get("error"))
        L += [f"**Total IDs:** {t}", f"**Successful:** {t - e}", f"**Errors:** {e}", ""]
        if self._mode == "compare":
            am: dict[str, list[str]] = {}; ae: dict[str, list[str]] = {}; at: dict[str, str] = {}
            for r in self._results:
                if r.get("error"): continue
                for i in r.get("missing", []): am.setdefault(i["path"], []).append(r["id"])
                for i in r.get("extra", []): ae.setdefault(i["path"], []).append(r["id"])
                for i in r.get("typeChanges", []): at.setdefault(f"{i['path']}: {i['oldType']} \u2192 {i['newType']}", []).append(r["id"])
            for title, d in [("Missing Fields", am), ("Extra Fields", ae), ("Type Changes", at)]:
                if d: L += [f"## {title}", "| Field | Affected IDs |", "|-------|-------------|"] + [f"| `{k}` | {', '.join(v)} |" for k, v in d.items()] + [""]
        L.append("## Per-ID Results")
        for r in self._results:
            rid = r.get("id", "?")
            if r.get("error"): L += [f"### ID {rid} \u2014 Error", f"> {r['error']}", ""]
            else:
                rt = r.get("responseTimes", {}); sc = r.get("statusCodes", "")
                rs = ""
                if "old" in rt and "new" in rt: rs += f" | Time: {rt['old']:.3f}s \u2192 {rt['new']:.3f}s"
                elif "single" in rt: rs += f" | Time: {rt['single']:.3f}s"
                ss = ""
                if "old" in sc and "new" in sc: ss += f" | Status: {sc['old']} \u2192 {sc['new']}"
                elif "single" in sc: ss += f" | Status: {sc['single']}"
                L.append(f"### ID {rid}{rs}{ss}")
                for title, items in [("Missing", r.get("missing", [])), ("Extra", r.get("extra", [])), ("Type changes", r.get("typeChanges", []))]:
                    if items: L.append(f"- {title}: {', '.join(i['path'] for i in items)}")
                if not r.get("missing") and not r.get("extra") and not r.get("typeChanges"): L.append("- Identical")
                L.append("")
        return "\n".join(L)

    # -- Bulk ID import --
    def _on_import_ids(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import IDs", "", "Text/CSV (*.txt *.csv);;All (*)")
        if not path: return
        try: content = Path(path).read_text()
        except Exception as e: QMessageBox.warning(self, "Import Error", str(e)); return
        ids = []
        if path.endswith(".csv"):
            for row in csv.reader(io.StringIO(content)):
                if row: v = row[0].strip().strip('"').strip("'")
                if v: ids.append(v)
        else:
            for line in content.splitlines():
                v = line.strip().strip(",").strip(";")
                if v: ids.append(v)
        if ids:
            ex = self._ids_input.text().strip()
            self._ids_input.setText(ex + ", " + ", ".join(ids) if ex else ", ".join(ids))

    # -- Fetch --
    def _on_fetch(self):
        self._clear_results(); self._error_label.setVisible(False)
        ids = [s.strip() for s in re.split(r"[,;\s]+", self._ids_input.text()) if s.strip()]
        if not ids: self._show_error("Provide at least one ID"); return
        if self._mode == "compare":
            configs = [self._old_form.get_config(), self._new_form.get_config()]
            if not configs[0]["url"] or not configs[1]["url"]: self._show_error("Both old and new API URLs are required"); return
        else:
            configs = [self._single_form.get_config()]
            if not configs[0]["url"]: self._show_error("API URL is required"); return
        ig = {s.strip() for s in re.split(r"[,;]+", self._ignore_input.text()) if s.strip()}
        self._fetch_btn.setEnabled(False); self._progress.setVisible(True)
        self._worker = ApiWorker(configs, ids, self._field_mappings, ignore_fields=ig, concurrency=self._concurrency_spin.value())
        self._worker.finished.connect(self._on_results); self._worker.start()

    # -- Results --
    def _on_results(self, results):
        self._results = results; self._fetch_btn.setEnabled(True); self._progress.setVisible(False)
        self._save_to_history(results)
        total = len(results); success = sum(1 for r in results if not r.get("error")); ec = total - success
        am: list[dict] = []; ae: list[dict] = []; atc: list[dict] = []
        if self._mode == "compare":
            _am: dict[str, dict] = {}; _ae: dict[str, dict] = {}; _at: dict[str, dict] = {}
            for r in results:
                if r.get("error"): continue
                for i in r.get("missing", []):
                    p = i["path"]
                    if p not in _am: _am[p] = {"path": p, "value": i["value"], "ids": []}
                    _am[p]["ids"].append(r["id"])
                for i in r.get("extra", []):
                    p = i["path"]
                    if p not in _ae: _ae[p] = {"path": p, "value": i["value"], "ids": []}
                    _ae[p]["ids"].append(r["id"])
                for i in r.get("typeChanges", []):
                    p = i["path"]
                    if p not in _at: _at[p] = {**i, "ids": []}
                    _at[p]["ids"].append(r["id"])
            am = list(_am.values()); ae = list(_ae.values()); atc = list(_at.values())
        has = bool(am or ae or atc)

        # Summary card
        sc = QFrame(); sc.setObjectName("card")
        sl = QVBoxLayout(sc); sl.setContentsMargins(20, 16, 20, 16); sl.setSpacing(10)
        srow = QHBoxLayout(); srow.setSpacing(32)
        for lbl, val, col in [("Total IDs", str(total), "#cdd6f4"), ("Successful", str(success), "#a6e3a1"), ("Errors", str(ec), "#f38ba8"), ("Status", "Changes Detected" if has else "All Identical", "#f9e2af" if has else "#a6e3a1")]:
            c = QVBoxLayout(); c.setSpacing(0)
            v = QLabel(val); v.setStyleSheet(f"font-size: 24px; font-weight: 700; color: {col}; background: transparent;")
            l = QLabel(lbl); l.setStyleSheet("font-size: 11px; color: #6c7086; background: transparent;")
            c.addWidget(v); c.addWidget(l); srow.addLayout(c)
        srow.addStretch(); sl.addLayout(srow)

        all_rt = [r.get("responseTimes", {}) for r in results if not r.get("error")]
        if all_rt and "old" in all_rt[0]:
            ot = [t["old"] for t in all_rt if "old" in t]; nt = [t["new"] for t in all_rt if "new" in t]
            if ot and nt:
                rl = QLabel(f"Average Response Time  \u2014  Old: {sum(ot)/len(ot):.3f}s    New: {sum(nt)/len(nt):.3f}s")
                rl.setStyleSheet("font-size: 12px; color: #6c7086; background: transparent;"); sl.addWidget(rl)

        scr = [r for r in results if not r.get("error") and r.get("statusCodeDiff")]
        if scr:
            scl = QLabel(f"\u26a0  {len(scr)} ID(s) returned different status codes")
            scl.setStyleSheet("color: #f38ba8; font-weight: 600; font-size: 12px; background: transparent;"); sl.addWidget(scl)

        if has and self._mode == "compare":
            mb = QPushButton(f"Configure Field Mappings ({len(self._field_mappings)})" if self._field_mappings else "Configure Field Mappings")
            mb.clicked.connect(self._on_configure_mappings); sl.addWidget(mb)

        for title, items, col in [("Missing Fields", am, "#f38ba8"), ("Extra Fields", ae, "#f9e2af"), ("Type Changes", atc, "#89b4fa")]:
            if items:
                l = QLabel(f"{title} \u2014 {len(items)}"); l.setStyleSheet(f"font-weight: 600; font-size: 12px; color: {col}; background: transparent; margin-top: 4px;")
                sl.addWidget(l)
                for i in items:
                    sl.addWidget(QLabel(f"  {i['path']} \u2192 IDs: {', '.join(str(x) for x in i['ids'])}"))
        self._results_layout_inner.addWidget(sc)

        # Per-ID
        pl = QLabel("Per-ID Breakdown"); pl.setStyleSheet("font-size: 14px; font-weight: 600; color: #6c7086; background: transparent; margin-top: 12px;")
        self._results_layout_inner.addWidget(pl)
        for r in results: self._results_layout_inner.addWidget(ResultCard(r))

        # Raw responses
        if results and not results[0].get("error"):
            rawl = QLabel("Raw Responses"); rawl.setStyleSheet("font-size: 14px; font-weight: 600; color: #6c7086; background: transparent; margin-top: 12px;")
            self._results_layout_inner.addWidget(rawl)
            for r in results:
                il = QLabel(f"ID: {r.get('id', '?')}"); il.setStyleSheet("font-weight: 600; font-size: 12px; background: transparent;")
                self._results_layout_inner.addWidget(il)
                rt = QPlainTextEdit(); rt.setReadOnly(True); rt.setMaximumHeight(150)
                if self._mode == "compare": d = r.get("rawNewData") or r.get("newData", {}); rt.setPlainText(json.dumps(d, indent=2))
                else: rt.setPlainText(json.dumps(r.get("data", {}), indent=2))
                self._results_layout_inner.addWidget(rt)
        self._results_layout_inner.addStretch()

    def _show_error(self, msg): self._error_label.setText(msg); self._error_label.setVisible(True)

    def _clear_results(self):
        while self._results_layout_inner.count():
            it = self._results_layout_inner.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._results = None

    # -- Field Mappings --
    def _on_configure_mappings(self):
        if not self._results: return
        m, e = [], []
        for r in self._results:
            if r.get("error"): continue
            m.extend(r.get("missing", [])); e.extend(r.get("extra", []))
        d = FieldMappingDialog(self._field_mappings, m, e); d.applied.connect(self._on_mappings_applied); d.show()

    def _on_mappings_applied(self, mappings): self._field_mappings = mappings; self._on_fetch()

    # -- History --
    def _save_to_history(self, results):
        entry = {"timestamp": datetime.now().isoformat(), "mode": self._mode, "ids": [s.strip() for s in re.split(r"[,;\s]+", self._ids_input.text()) if s.strip()], "results_summary": []}
        for r in results:
            s: dict[str, Any] = {"id": r.get("id"), "error": r.get("error"), "missing_count": len(r.get("missing", [])), "extra_count": len(r.get("extra", [])), "type_changes_count": len(r.get("typeChanges", []))}
            rt = r.get("responseTimes", {})
            if "old" in rt: s["response_time_old"] = rt["old"]; s["response_time_new"] = rt["new"]
            elif "single" in rt: s["response_time"] = rt["single"]
            sc = r.get("statusCodes", {})
            if "old" in sc: s["status_code_old"] = sc["old"]; s["status_code_new"] = sc["new"]
            elif "single" in sc: s["status_code"] = sc["single"]
            entry["results_summary"].append(s)
        self._history.insert(0, entry); self._history = self._history[:MAX_HISTORY]
        save_json(HISTORY_FILE, self._history); self._refresh_history_list()

    def _refresh_history_list(self):
        self._history_list.clear()
        for e in self._history:
            ts = e.get("timestamp", "?")
            try: ts = datetime.fromisoformat(ts).strftime("%b %d, %H:%M")
            except ValueError: pass
            self._history_list.addItem(f"{ts}  \u00b7  {e.get('mode', '?')}  \u00b7  {len(e.get('ids', []))} IDs")

    def _on_history_selected(self, item):
        row = self._history_list.row(item)
        if row < 0 or row >= len(self._history): return
        e = self._history[row]
        self._ids_input.setText(", ".join(str(i) for i in e.get("ids", [])))
        self._set_mode(e.get("mode", "compare"))

    def _on_clear_history(self): self._history = []; save_json(HISTORY_FILE, self._history); self._refresh_history_list()

    # -- Profiles --
    def _refresh_profile_combo(self):
        self._profile_combo.clear(); self._profile_combo.addItem("")
        for n in sorted(self._profiles.keys()): self._profile_combo.addItem(n)

    def _on_profile_selected(self, name):
        if not name or name not in self._profiles: return
        p = self._profiles[name]; m = p.get("mode", "compare"); self._set_mode(m)
        if m == "compare":
            if "old_api" in p: self._old_form.set_config(p["old_api"])
            if "new_api" in p: self._new_form.set_config(p["new_api"])
        else:
            if "api" in p: self._single_form.set_config(p["api"])
        self._ids_input.setText(", ".join(p.get("ids", [])))
        self._field_mappings = p.get("field_mappings", [])
        self._ignore_input.setText(p.get("ignore_fields", ""))
        self._concurrency_spin.setValue(p.get("concurrency", 1))

    def _on_save_profile(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Save Profile", "Profile name:")
        if not ok or not name.strip(): return
        name = name.strip()
        p: dict[str, Any] = {"mode": self._mode}
        if self._mode == "compare": p["old_api"] = self._old_form.get_config(); p["new_api"] = self._new_form.get_config()
        else: p["api"] = self._single_form.get_config()
        p["ids"] = [s.strip() for s in re.split(r"[,;\s]+", self._ids_input.text()) if s.strip()]
        p["field_mappings"] = self._field_mappings; p["ignore_fields"] = self._ignore_input.text(); p["concurrency"] = self._concurrency_spin.value()
        self._profiles[name] = p; save_json(PROFILES_FILE, self._profiles)
        self._refresh_profile_combo(); self._profile_combo.setCurrentText(name)

    def _on_delete_profile(self):
        name = self._profile_combo.currentText()
        if not name or name not in self._profiles: return
        if QMessageBox.question(self, "Delete Profile", f"Delete '{name}'?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            del self._profiles[name]; save_json(PROFILES_FILE, self._profiles); self._refresh_profile_combo()


def main():
    import sys
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = DiffiWindow()
    window.show()
    sys.exit(app.exec())
