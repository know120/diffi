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

# -- Color palette --
C = {
    "bg": "#0d1117",
    "surface": "#161b22",
    "surface2": "#1c2128",
    "border": "#30363d",
    "border2": "#21262d",
    "text": "#f0f6fc",
    "text2": "#8b949e",
    "text3": "#484f58",
    "accent": "#58a6ff",
    "accent2": "#1f6feb",
    "green": "#3fb950",
    "green_bg": "#0d2818",
    "yellow": "#d29922",
    "yellow_bg": "#2d1600",
    "red": "#f85149",
    "red_bg": "#2d0b0b",
    "blue_bg": "#0c2d6b",
}


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
                method,
                full_url,
                headers=headers,
                data=fetch_options.get("data"),
                timeout=TIMEOUT,
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
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=TIMEOUT,
            )
            res.raise_for_status()
            return res.json().get("access_token")
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Helper: Card frame
# ---------------------------------------------------------------------------


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(f"""
            Card {{
                background: {C['surface']};
                border: 1px solid {C['border']};
                border-radius: 8px;
            }}
        """)


# ---------------------------------------------------------------------------
# ApiFormWidget
# ---------------------------------------------------------------------------

AUTH_TYPES = ["None", "Basic Auth", "Bearer Token", "OAuth2"]


class ApiFormWidget(QWidget):
    def __init__(self, label: str, parent=None):
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        card = Card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(10)

        # Section header
        hdr = QLabel(label)
        hdr.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {C['text']}; background: transparent; border: none;")
        card_layout.addWidget(hdr)

        # URL row
        url_row = QHBoxLayout()
        url_row.setSpacing(8)
        url_lbl = QLabel("URL")
        url_lbl.setFixedWidth(32)
        url_lbl.setStyleSheet(f"color: {C['text2']}; font-size: 12px; background: transparent;")
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://api.example.com/v1/{{id}}")
        url_row.addWidget(url_lbl)
        url_row.addWidget(self.url_input, 1)

        method_lbl = QLabel("Method")
        method_lbl.setStyleSheet(f"color: {C['text2']}; font-size: 12px; background: transparent;")
        self.method_combo = QComboBox()
        self.method_combo.addItems(METHODS)
        self.method_combo.setFixedWidth(76)
        url_row.addWidget(method_lbl)
        url_row.addWidget(self.method_combo)

        auth_lbl = QLabel("Auth")
        auth_lbl.setStyleSheet(f"color: {C['text2']}; font-size: 12px; background: transparent;")
        self.auth_combo = QComboBox()
        self.auth_combo.addItems(AUTH_TYPES)
        self.auth_combo.setFixedWidth(100)
        self.auth_combo.currentTextChanged.connect(self._on_auth_changed)
        url_row.addWidget(auth_lbl)
        url_row.addWidget(self.auth_combo)
        card_layout.addLayout(url_row)

        # Auth fields (horizontal, hidden by default)
        self._auth_basic_user = QLineEdit()
        self._auth_basic_user.setPlaceholderText("Username")
        self._auth_basic_pwd = QLineEdit()
        self._auth_basic_pwd.setPlaceholderText("Password")
        self._auth_basic_pwd.setEchoMode(QLineEdit.EchoMode.Password)

        self._auth_bearer_token = QLineEdit()
        self._auth_bearer_token.setPlaceholderText("Bearer token")

        self._auth_oauth2_client_id = QLineEdit()
        self._auth_oauth2_client_id.setPlaceholderText("Client ID")
        self._auth_oauth2_client_secret = QLineEdit()
        self._auth_oauth2_client_secret.setPlaceholderText("Client secret")
        self._auth_oauth2_client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._auth_oauth2_token_url = QLineEdit()
        self._auth_oauth2_token_url.setPlaceholderText("Token URL")

        self._auth_widgets: dict[str, list[QWidget]] = {
            "None": [],
            "Basic Auth": [self._auth_basic_user, self._auth_basic_pwd],
            "Bearer Token": [self._auth_bearer_token],
            "OAuth2": [
                self._auth_oauth2_client_id,
                self._auth_oauth2_client_secret,
                self._auth_oauth2_token_url,
            ],
        }
        self._auth_row_widget = QWidget()
        self._auth_row_widget.setStyleSheet("background: transparent;")
        self._auth_row = QHBoxLayout(self._auth_row_widget)
        self._auth_row.setContentsMargins(0, 0, 0, 0)
        self._auth_row.setSpacing(6)
        for w in sum(self._auth_widgets.values(), []):
            self._auth_row.addWidget(w)
            w.setVisible(False)
        self._auth_row.addStretch()
        card_layout.addWidget(self._auth_row_widget)
        self._auth_row_widget.setVisible(False)

        # Headers
        self.header_rows: list[dict] = []
        h_hdr_row = QHBoxLayout()
        h_hdr_row.setSpacing(4)
        h_label = QLabel("Headers")
        h_label.setStyleSheet(f"color: {C['text2']}; font-size: 12px; background: transparent;")
        h_hdr_row.addWidget(h_label)
        h_hdr_row.addStretch()
        self._add_header_btn = QPushButton("+")
        self._add_header_btn.setFixedSize(24, 24)
        self._add_header_btn.clicked.connect(lambda: self._add_header_row())
        h_hdr_row.addWidget(self._add_header_btn)
        card_layout.addLayout(h_hdr_row)

        self.headers_container = QVBoxLayout()
        self.headers_container.setSpacing(2)
        self.headers_container.setContentsMargins(0, 0, 0, 0)
        card_layout.addLayout(self.headers_container)

        # Params
        self.param_rows: list[dict] = []
        p_hdr_row = QHBoxLayout()
        p_hdr_row.setSpacing(4)
        p_label = QLabel("Query Params")
        p_label.setStyleSheet(f"color: {C['text2']}; font-size: 12px; background: transparent;")
        p_hdr_row.addWidget(p_label)
        p_hdr_row.addStretch()
        self._add_param_btn = QPushButton("+")
        self._add_param_btn.setFixedSize(24, 24)
        self._add_param_btn.clicked.connect(lambda: self._add_param_row())
        p_hdr_row.addWidget(self._add_param_btn)
        card_layout.addLayout(p_hdr_row)

        self.params_container = QVBoxLayout()
        self.params_container.setSpacing(2)
        self.params_container.setContentsMargins(0, 0, 0, 0)
        card_layout.addLayout(self.params_container)

        # Body
        body_lbl = QLabel("Request Body")
        body_lbl.setStyleSheet(f"color: {C['text2']}; font-size: 12px; background: transparent;")
        self._body_label = body_lbl
        card_layout.addWidget(body_lbl)
        self.body_input = QPlainTextEdit()
        self.body_input.setPlaceholderText(
            '{"title": "foo", "body": "bar", "userId": {{id}} }'
        )
        self.body_input.setMaximumHeight(64)
        self.body_input.setVisible(False)
        body_lbl.setVisible(False)
        card_layout.addWidget(self.body_input)

        self.method_combo.currentTextChanged.connect(self._on_method_changed)
        root.addWidget(card)

        self._add_header_row("Content-Type", "application/json")
        self._add_header_row("Accept", "application/json")

    def _on_method_changed(self, method: str):
        vis = method in ("POST", "PUT", "PATCH")
        self.body_input.setVisible(vis)
        self._body_label.setVisible(vis)

    def _on_auth_changed(self, auth_type: str):
        has_auth = auth_type != "None"
        self._auth_row_widget.setVisible(has_auth)
        for w in sum(self._auth_widgets.values(), []):
            w.setVisible(False)
        for w in self._auth_widgets.get(auth_type, []):
            w.setVisible(True)

    def _add_header_row(self, key: str = "", value: str = ""):
        row = self._make_key_value_row(self.header_rows, self.headers_container)
        row["key"].setText(key)
        row["value"].setText(value)

    def _add_param_row(self):
        self._make_key_value_row(self.param_rows, self.params_container)

    def _make_key_value_row(
        self, rows: list[dict], container: QVBoxLayout
    ) -> dict:
        row_widget = QWidget()
        row_widget.setStyleSheet("background: transparent;")
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)

        enabled_cb = QCheckBox()
        enabled_cb.setChecked(True)
        key_input = QLineEdit()
        key_input.setPlaceholderText("Key")
        key_input.setFixedWidth(140)
        value_input = QLineEdit()
        value_input.setPlaceholderText("Value")
        remove_btn = QPushButton("\u00d7")
        remove_btn.setFixedSize(24, 24)

        row_data = {
            "widget": row_widget,
            "enabled": enabled_cb,
            "key": key_input,
            "value": value_input,
        }
        rows.append(row_data)
        remove_btn.clicked.connect(lambda: self._remove_row(rows, row_widget))

        row_layout.addWidget(enabled_cb)
        row_layout.addWidget(key_input)
        row_layout.addWidget(value_input, 1)
        row_layout.addWidget(remove_btn)
        container.addWidget(row_widget)
        return row_data

    def _remove_row(self, rows: list[dict], widget: QWidget):
        rows[:] = [r for r in rows if r["widget"] is not widget]
        widget.deleteLater()

    def get_config(self) -> dict:
        headers = {}
        for r in self.header_rows:
            if r["enabled"].isChecked() and r["key"].text().strip():
                headers[r["key"].text().strip()] = r["value"].text()

        params = {}
        for r in self.param_rows:
            if r["enabled"].isChecked() and r["key"].text().strip():
                params[r["key"].text().strip()] = r["value"].text()

        auth_type = self.auth_combo.currentText()
        auth: dict[str, str] = {"type": auth_type.lower().replace(" ", "_")}
        if auth_type == "Basic Auth":
            auth["username"] = self._auth_basic_user.text()
            auth["password"] = self._auth_basic_pwd.text()
        elif auth_type == "Bearer Token":
            auth["token"] = self._auth_bearer_token.text()
        elif auth_type == "OAuth2":
            auth["client_id"] = self._auth_oauth2_client_id.text()
            auth["client_secret"] = self._auth_oauth2_client_secret.text()
            auth["token_url"] = self._auth_oauth2_token_url.text()

        return {
            "url": self.url_input.text(),
            "method": self.method_combo.currentText(),
            "headers": headers,
            "params": params,
            "body": self.body_input.toPlainText(),
            "auth": auth,
        }

    def set_config(self, config: dict):
        self.url_input.setText(config.get("url", ""))
        idx = self.method_combo.findText(config.get("method", "GET"))
        if idx >= 0:
            self.method_combo.setCurrentIndex(idx)

        auth = config.get("auth", {})
        auth_type_map = {
            "none": "None",
            "basic_auth": "Basic Auth",
            "bearer_token": "Bearer Token",
            "oauth2": "OAuth2",
        }
        auth_type = auth_type_map.get(auth.get("type", "none"), "None")
        idx = self.auth_combo.findText(auth_type)
        if idx >= 0:
            self.auth_combo.setCurrentIndex(idx)
        if auth_type == "Basic Auth":
            self._auth_basic_user.setText(auth.get("username", ""))
            self._auth_basic_pwd.setText(auth.get("password", ""))
        elif auth_type == "Bearer Token":
            self._auth_bearer_token.setText(auth.get("token", ""))
        elif auth_type == "OAuth2":
            self._auth_oauth2_client_id.setText(auth.get("client_id", ""))
            self._auth_oauth2_client_secret.setText(auth.get("client_secret", ""))
            self._auth_oauth2_token_url.setText(auth.get("token_url", ""))


# ---------------------------------------------------------------------------
# FieldMappingDialog
# ---------------------------------------------------------------------------


class FieldMappingDialog(QWidget):
    applied = Signal(list)

    def __init__(self, mappings: list[dict], missing: list[dict], extra: list[dict]):
        super().__init__()
        self.setWindowTitle("Field Mappings")
        self.setMinimumSize(520, 340)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("Map old API field paths to new API field paths")
        title.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {C['text']};")
        layout.addWidget(title)

        self.rows: list[dict] = []

        if not mappings:
            seen = set()
            for f in missing:
                key = f["path"].rsplit(".", 1)[-1]
                self._add_row(f["path"], key)
                seen.add(f["path"])
            for f in extra:
                key = f["path"].rsplit(".", 1)[-1]
                if f["path"] not in seen:
                    self._add_row(key, f["path"])
        else:
            for m in mappings:
                self._add_row(m.get("oldPath", ""), m.get("newPath", ""))

        if not self.rows:
            self._add_row("", "")

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet(f"background: {C['bg']}; border: none;")
        scroll_content = QWidget()
        scroll_content.setStyleSheet("background: transparent;")
        self.scroll_layout = QVBoxLayout(scroll_content)
        self.scroll_layout.setSpacing(4)
        self.scroll.setWidget(scroll_content)
        layout.addWidget(self.scroll, 1)

        add_btn = QPushButton("+ Add mapping")
        add_btn.clicked.connect(lambda: self._add_row("", ""))
        layout.addWidget(add_btn)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.close)
        apply_btn = QPushButton("Apply & Re-run")
        apply_btn.setProperty("accent", True)
        apply_btn.clicked.connect(self._on_apply)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(apply_btn)
        layout.addLayout(btn_row)

    def _add_row(self, old_path: str = "", new_path: str = ""):
        row_widget = QWidget()
        row_widget.setStyleSheet("background: transparent;")
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        old_input = QLineEdit(old_path)
        old_input.setPlaceholderText("old.field.path")
        arrow = QLabel("\u2192")
        arrow.setStyleSheet(f"color: {C['text3']}; background: transparent; font-size: 14px;")
        new_input = QLineEdit(new_path)
        new_input.setPlaceholderText("new.field.path")
        remove_btn = QPushButton("\u00d7")
        remove_btn.setFixedSize(24, 24)

        row_data = {"widget": row_widget, "oldPath": old_input, "newPath": new_input}
        self.rows.append(row_data)
        remove_btn.clicked.connect(lambda: self._remove_row(row_widget))

        row_layout.addWidget(old_input, 1)
        row_layout.addWidget(arrow)
        row_layout.addWidget(new_input, 1)
        row_layout.addWidget(remove_btn)
        self.scroll_layout.addWidget(row_widget)

    def _remove_row(self, widget: QWidget):
        self.rows[:] = [r for r in self.rows if r["widget"] is not widget]
        widget.deleteLater()

    def _on_apply(self):
        mappings = []
        for r in self.rows:
            old = r["oldPath"].text().strip()
            new = r["newPath"].text().strip()
            if old and new:
                mappings.append({"oldPath": old, "newPath": new})
        self.applied.emit(mappings)
        self.close()


# ---------------------------------------------------------------------------
# ResultCard
# ---------------------------------------------------------------------------


class ResultCard(QWidget):
    def __init__(self, result: dict):
        super().__init__()
        self._expanded = False
        self._result = result
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        card = Card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(6)

        r = self._result
        error = r.get("error")
        has_diff = bool(r.get("missing") or r.get("extra") or r.get("typeChanges"))

        # Header row
        header = QHBoxLayout()
        header.setSpacing(8)

        id_label = QLabel(f"ID: {r.get('id', '?')}")
        id_label.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {C['text']}; background: transparent;")
        header.addWidget(id_label)

        # Response time
        rt = r.get("responseTimes", {})
        if rt:
            if "old" in rt and "new" in rt:
                rt_text = f"{rt['old']:.3f}s \u2192 {rt['new']:.3f}s"
            else:
                rt_text = f"{rt.get('single', 0):.3f}s"
            rt_badge = QLabel(rt_text)
            rt_badge.setStyleSheet(
                f"background: {C['blue_bg']}; color: {C['accent']}; padding: 2px 8px; border-radius: 4px; font-size: 11px;"
            )
            header.addWidget(rt_badge)

        # Status code
        sc = r.get("statusCodes", {})
        if sc:
            if "old" in sc and "new" in sc:
                if r.get("statusCodeDiff"):
                    sc_text = f"{sc['old']} \u2192 {sc['new']}"
                    sc_style = f"background: {C['red_bg']}; color: {C['red']};"
                else:
                    sc_text = f"{sc['old']}"
                    sc_style = f"background: {C['green_bg']}; color: {C['green']};"
            else:
                sc_text = f"{sc.get('single', '?')}"
                sc_style = f"background: {C['green_bg']}; color: {C['green']};"
            sc_badge = QLabel(sc_text)
            sc_badge.setStyleSheet(
                f"{sc_style} padding: 2px 8px; border-radius: 4px; font-size: 11px;"
            )
            header.addWidget(sc_badge)

        if error:
            badge = QLabel("Error")
            badge.setStyleSheet(
                f"background: {C['red_bg']}; color: {C['red']}; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;"
            )
        elif has_diff:
            m, e, t = len(r.get("missing", [])), len(r.get("extra", [])), len(r.get("typeChanges", []))
            badge = QLabel(f"{m} missing \u00b7 {e} extra \u00b7 {t} changed")
            badge.setStyleSheet(
                f"background: {C['yellow_bg']}; color: {C['yellow']}; padding: 2px 8px; border-radius: 4px; font-size: 11px;"
            )
        else:
            badge = QLabel("Identical")
            badge.setStyleSheet(
                f"background: {C['green_bg']}; color: {C['green']}; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;"
            )
        header.addWidget(badge)
        header.addStretch()

        self._toggle_btn = QPushButton("\u25b6")
        self._toggle_btn.setFixedSize(24, 24)
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.clicked.connect(self._toggle)
        header.addWidget(self._toggle_btn)
        card_layout.addLayout(header)

        # Body
        self._body = QWidget()
        self._body.setVisible(False)
        self._body.setStyleSheet("background: transparent;")
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 4, 0, 0)
        body_layout.setSpacing(4)

        if error:
            err_label = QLabel(error)
            err_label.setWordWrap(True)
            err_label.setStyleSheet(
                f"color: {C['red']}; padding: 10px; background: {C['red_bg']}; border: 1px solid {C['red']}33; border-radius: 6px; font-size: 12px;"
            )
            body_layout.addWidget(err_label)
        else:
            if r.get("missing"):
                lbl = QLabel("Missing Fields")
                lbl.setStyleSheet(f"font-weight: 600; font-size: 12px; color: {C['red']}; background: transparent;")
                body_layout.addWidget(lbl)
                for item in r["missing"]:
                    body_layout.addWidget(
                        QLabel(f"  {item['path']} \u2192 {json.dumps(item.get('value'))}")
                    )
            if r.get("extra"):
                lbl = QLabel("Extra Fields")
                lbl.setStyleSheet(f"font-weight: 600; font-size: 12px; color: {C['yellow']}; background: transparent;")
                body_layout.addWidget(lbl)
                for item in r["extra"]:
                    body_layout.addWidget(
                        QLabel(f"  {item['path']} \u2192 {json.dumps(item.get('value'))}")
                    )
            if r.get("typeChanges"):
                lbl = QLabel("Type Changes")
                lbl.setStyleSheet(f"font-weight: 600; font-size: 12px; color: {C['accent']}; background: transparent;")
                body_layout.addWidget(lbl)
                for item in r["typeChanges"]:
                    body_layout.addWidget(
                        QLabel(
                            f"  {item['path']}: {item['oldType']} \u2192 {item['newType']}"
                        )
                    )

        card_layout.addWidget(self._body)
        outer.addWidget(card)

    def _toggle(self):
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._toggle_btn.setText("\u25bc" if self._expanded else "\u25b6")


# ---------------------------------------------------------------------------
# DiffiWindow
# ---------------------------------------------------------------------------


class DiffiWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Diffi \u2014 API Comparator")
        self.setMinimumSize(960, 720)

        self._mode = "compare"
        self._results: list[dict] | None = None
        self._field_mappings: list[dict] = []
        self._history: list[dict] = load_json(HISTORY_FILE) or []
        self._profiles: dict[str, dict] = load_json(PROFILES_FILE) or {}

        self._build_ui()
        self._apply_styles()
        self._refresh_history_list()
        self._refresh_profile_combo()

    # ------------------------------------------------------------------
    # Styles
    # ------------------------------------------------------------------

    def _apply_styles(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget#central {{
                background: {C['bg']};
            }}
            QWidget {{
                color: {C['text']};
                font-size: 13px;
            }}
            QLabel {{
                color: {C['text']};
                background: transparent;
            }}
            QLineEdit {{
                background: {C['bg']};
                color: {C['text']};
                border: 1px solid {C['border']};
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 13px;
                selection-background-color: {C['accent2']};
            }}
            QLineEdit:focus {{
                border-color: {C['accent']};
            }}
            QPlainTextEdit {{
                background: {C['bg']};
                color: {C['text']};
                border: 1px solid {C['border']};
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 13px;
                font-family: monospace;
            }}
            QPlainTextEdit:focus {{
                border-color: {C['accent']};
            }}
            QComboBox {{
                background: {C['bg']};
                color: {C['text']};
                border: 1px solid {C['border']};
                border-radius: 6px;
                padding: 5px 8px;
                font-size: 13px;
            }}
            QComboBox:focus, QComboBox:on {{
                border-color: {C['accent']};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox QAbstractItemView {{
                background: {C['surface']};
                color: {C['text']};
                border: 1px solid {C['border']};
                border-radius: 6px;
                padding: 4px;
                selection-background-color: {C['accent2']};
            }}
            QPushButton {{
                background: {C['surface']};
                color: {C['text']};
                border: 1px solid {C['border']};
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background: {C['surface2']};
                border-color: {C['text3']};
            }}
            QPushButton:pressed {{
                background: {C['border2']};
            }}
            QPushButton:disabled {{
                color: {C['text3']};
                background: {C['surface']};
            }}
            QPushButton[accent="true"] {{
                background: {C['accent2']};
                color: white;
                border: 1px solid {C['accent']};
            }}
            QPushButton[accent="true"]:hover {{
                background: {C['accent']};
            }}
            QPushButton[destructive="true"] {{
                background: transparent;
                color: {C['text3']};
                border: 1px solid transparent;
            }}
            QPushButton[destructive="true"]:hover {{
                color: {C['red']};
                background: {C['red_bg']};
                border-color: {C['red']}44;
            }}
            QCheckBox {{
                color: {C['text']};
                spacing: 6px;
                font-size: 13px;
            }}
            QCheckBox::indicator {{
                width: 16px; height: 16px;
                border: 1px solid {C['border']};
                border-radius: 4px;
                background: {C['bg']};
            }}
            QCheckBox::indicator:checked {{
                background: {C['accent2']};
                border-color: {C['accent']};
            }}
            QSpinBox {{
                background: {C['bg']};
                color: {C['text']};
                border: 1px solid {C['border']};
                border-radius: 6px;
                padding: 4px 6px;
                font-size: 13px;
            }}
            QSpinBox:focus {{
                border-color: {C['accent']};
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {C['border']};
                border-radius: 4px;
                min-height: 24px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {C['text3']};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QTabWidget::pane {{
                border: 1px solid {C['border']};
                border-radius: 8px;
                background: {C['surface']};
                top: -1px;
            }}
            QTabBar::tab {{
                background: {C['surface2']};
                color: {C['text2']};
                padding: 7px 18px;
                border: 1px solid {C['border']};
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                margin-right: 2px;
                font-size: 12px;
            }}
            QTabBar::tab:selected {{
                background: {C['surface']};
                color: {C['text']};
                border-color: {C['border']};
            }}
            QTabBar::tab:hover:!selected {{
                background: {C['border2']};
            }}
            QProgressBar {{
                border: 1px solid {C['border']};
                border-radius: 4px;
                text-align: center;
                color: {C['text']};
                background: {C['bg']};
                max-height: 14px;
                font-size: 11px;
            }}
            QProgressBar::chunk {{
                background: {C['accent']};
                border-radius: 3px;
            }}
            QDockWidget {{
                color: {C['text']};
                titlebar-close-icon: none;
                titlebar-normal-icon: none;
            }}
            QDockWidget::title {{
                background: {C['surface']};
                padding: 8px 12px;
                font-weight: 600;
                font-size: 12px;
                border-bottom: 1px solid {C['border']};
            }}
            QListWidget {{
                background: {C['bg']};
                color: {C['text']};
                border: 1px solid {C['border']};
                border-radius: 6px;
                font-size: 12px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 6px 8px;
                border-bottom: 1px solid {C['border2']};
            }}
            QListWidget::item:selected {{
                background: {C['accent2']};
                color: white;
            }}
            QListWidget::item:hover:!selected {{
                background: {C['surface2']};
            }}
            QSplitter::handle {{
                background: {C['border']};
                height: 1px;
            }}
        """)

    # ------------------------------------------------------------------
    # UI Build
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(16)

        # --- Header ---
        header_row = QHBoxLayout()
        header_row.setSpacing(12)

        title = QLabel("Diffi")
        title.setStyleSheet(
            f"font-size: 22px; font-weight: 700; color: {C['text']}; letter-spacing: -0.5px;"
        )
        header_row.addWidget(title)

        subtitle = QLabel("\u2014 API Comparator")
        subtitle.setStyleSheet(
            f"font-size: 14px; color: {C['text2']}; font-weight: 400;"
        )
        header_row.addWidget(subtitle)
        header_row.addStretch()

        # Config buttons
        import_btn = QPushButton("Import Config")
        import_btn.clicked.connect(self._on_import_config)
        header_row.addWidget(import_btn)

        export_cfg_btn = QPushButton("Export Config")
        export_cfg_btn.clicked.connect(self._on_export_config)
        header_row.addWidget(export_cfg_btn)

        header_row.addSpacing(8)

        # Environment
        env_lbl = QLabel("Environment")
        env_lbl.setStyleSheet(f"font-size: 12px; color: {C['text2']};")
        header_row.addWidget(env_lbl)
        self._profile_combo = QComboBox()
        self._profile_combo.setFixedWidth(130)
        self._profile_combo.currentTextChanged.connect(self._on_profile_selected)
        header_row.addWidget(self._profile_combo)

        save_profile_btn = QPushButton("\u2714")
        save_profile_btn.setFixedSize(30, 30)
        save_profile_btn.setToolTip("Save profile")
        save_profile_btn.clicked.connect(self._on_save_profile)
        header_row.addWidget(save_profile_btn)

        del_profile_btn = QPushButton("\u00d7")
        del_profile_btn.setFixedSize(30, 30)
        del_profile_btn.setProperty("destructive", True)
        del_profile_btn.setToolTip("Delete profile")
        del_profile_btn.clicked.connect(self._on_delete_profile)
        header_row.addWidget(del_profile_btn)

        layout.addLayout(header_row)

        # --- Mode toggle (segmented) ---
        mode_row = QHBoxLayout()
        mode_row.setSpacing(0)

        self._mode_compare_btn = QPushButton("Compare (Old vs New)")
        self._mode_compare_btn.setCheckable(True)
        self._mode_compare_btn.setChecked(True)
        self._mode_compare_btn.setStyleSheet(f"""
            QPushButton {{
                padding: 8px 20px;
                font-size: 13px;
                font-weight: 500;
                border-radius: 8px 0 0 8px;
                border-right: none;
            }}
            QPushButton:checked {{
                background: {C['accent2']};
                color: white;
                border-color: {C['accent']};
            }}
        """)
        self._mode_compare_btn.clicked.connect(lambda: self._set_mode("compare"))

        self._mode_single_btn = QPushButton("Single API")
        self._mode_single_btn.setCheckable(True)
        self._mode_single_btn.setStyleSheet(f"""
            QPushButton {{
                padding: 8px 20px;
                font-size: 13px;
                font-weight: 500;
                border-radius: 0 8px 8px 0;
            }}
            QPushButton:checked {{
                background: {C['accent2']};
                color: white;
                border-color: {C['accent']};
            }}
        """)
        self._mode_single_btn.clicked.connect(lambda: self._set_mode("single"))

        mode_row.addWidget(self._mode_compare_btn)
        mode_row.addWidget(self._mode_single_btn)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        # --- Content ---
        content_splitter = QSplitter(Qt.Orientation.Vertical)

        # Top: API config
        config_widget = QWidget()
        config_widget.setStyleSheet("background: transparent;")
        config_layout = QVBoxLayout(config_widget)
        config_layout.setContentsMargins(0, 0, 0, 0)
        config_layout.setSpacing(12)

        self._api_tabs = QTabWidget()
        self._old_form = ApiFormWidget("Old API")
        self._new_form = ApiFormWidget("New API")
        self._single_form = ApiFormWidget("API")

        self._api_tabs.addTab(self._old_form, "Old API")
        self._api_tabs.addTab(self._new_form, "New API")
        config_layout.addWidget(self._api_tabs)

        # IDs + options card
        ids_card = Card()
        ids_card_layout = QVBoxLayout(ids_card)
        ids_card_layout.setContentsMargins(16, 12, 16, 12)
        ids_card_layout.setSpacing(8)

        # Row 1: IDs input
        ids_row = QHBoxLayout()
        ids_row.setSpacing(8)
        ids_lbl = QLabel("IDs")
        ids_lbl.setFixedWidth(24)
        ids_lbl.setStyleSheet(f"color: {C['text2']}; font-size: 12px;")
        self._ids_input = QLineEdit()
        self._ids_input.setPlaceholderText("e.g. 1, 2, 3, 5, 10")
        ids_row.addWidget(ids_lbl)
        ids_row.addWidget(self._ids_input, 1)

        import_ids_btn = QPushButton("Import File")
        import_ids_btn.clicked.connect(self._on_import_ids)
        ids_row.addWidget(import_ids_btn)

        ids_card_layout.addLayout(ids_row)

        # Row 2: Options
        opts_row = QHBoxLayout()
        opts_row.setSpacing(16)

        conc_lbl = QLabel("Threads")
        conc_lbl.setStyleSheet(f"color: {C['text2']}; font-size: 12px;")
        self._concurrency_spin = QSpinBox()
        self._concurrency_spin.setRange(1, 20)
        self._concurrency_spin.setValue(1)
        self._concurrency_spin.setFixedWidth(52)
        opts_row.addWidget(conc_lbl)
        opts_row.addWidget(self._concurrency_spin)

        ignore_lbl = QLabel("Ignore Fields")
        ignore_lbl.setStyleSheet(f"color: {C['text2']}; font-size: 12px;")
        self._ignore_input = QLineEdit()
        self._ignore_input.setPlaceholderText("timestamp, request_id, user.created_at")
        self._ignore_input.setToolTip(
            "Comma-separated field paths to exclude from comparison.\n"
            "Supports prefixes: 'user' ignores 'user.name', 'user.email', etc."
        )
        opts_row.addWidget(ignore_lbl)
        opts_row.addWidget(self._ignore_input, 1)

        ids_card_layout.addLayout(opts_row)
        config_layout.addWidget(ids_card)

        # Fetch button row
        fetch_row = QHBoxLayout()
        fetch_row.setSpacing(10)
        self._fetch_btn = QPushButton("\u25b6  Run Comparison")
        self._fetch_btn.setFixedHeight(38)
        self._fetch_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C['green']};
                color: white;
                font-weight: 600;
                font-size: 14px;
                padding: 0 28px;
                border: none;
                border-radius: 8px;
            }}
            QPushButton:hover {{ background: #2ea043; }}
            QPushButton:disabled {{ background: {C['border2']}; color: {C['text3']}; }}
        """)
        self._fetch_btn.clicked.connect(self._on_fetch)
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setMaximum(0)
        self._progress.setFixedWidth(140)
        self._progress.setFixedHeight(38)
        fetch_row.addWidget(self._fetch_btn)
        fetch_row.addWidget(self._progress)
        fetch_row.addStretch()
        config_layout.addLayout(fetch_row)

        content_splitter.addWidget(config_widget)

        # Bottom: Results
        results_widget = QWidget()
        results_widget.setStyleSheet("background: transparent;")
        results_layout = QVBoxLayout(results_widget)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(8)

        results_header = QHBoxLayout()
        results_header.setSpacing(8)
        results_title = QLabel("Results")
        results_title.setStyleSheet(
            f"font-size: 16px; font-weight: 600; color: {C['text2']};"
        )
        results_header.addWidget(results_title)
        results_header.addStretch()

        self._mapping_btn = QPushButton("Field Mappings")
        self._mapping_btn.setVisible(False)
        self._mapping_btn.clicked.connect(self._on_configure_mappings)
        results_header.addWidget(self._mapping_btn)

        toggle_hist_btn = QPushButton("History")
        toggle_hist_btn.clicked.connect(
            lambda: self._history_dock.setVisible(not self._history_dock.isVisible())
        )
        results_header.addWidget(toggle_hist_btn)

        export_json_btn = QPushButton("Export JSON")
        export_json_btn.clicked.connect(lambda: self._on_export_results("json"))
        results_header.addWidget(export_json_btn)

        export_csv_btn = QPushButton("Export CSV")
        export_csv_btn.clicked.connect(lambda: self._on_export_results("csv"))
        results_header.addWidget(export_csv_btn)

        export_md_btn = QPushButton("Export MD")
        export_md_btn.clicked.connect(lambda: self._on_export_results("markdown"))
        results_header.addWidget(export_md_btn)

        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        self._error_label.setStyleSheet(
            f"color: {C['red']}; padding: 10px; background: {C['red_bg']}; border: 1px solid {C['red']}33; border-radius: 6px; font-size: 12px;"
        )
        results_layout.addWidget(self._error_label)
        results_layout.addLayout(results_header)

        self._results_scroll = QScrollArea()
        self._results_scroll.setWidgetResizable(True)
        self._results_scroll.setStyleSheet(f"background: transparent; border: none;")
        self._results_content = QWidget()
        self._results_content.setStyleSheet("background: transparent;")
        self._results_layout_inner = QVBoxLayout(self._results_content)
        self._results_layout_inner.setSpacing(8)
        self._results_scroll.setWidget(self._results_content)
        results_layout.addWidget(self._results_scroll)

        content_splitter.addWidget(results_widget)
        content_splitter.setStretchFactor(0, 1)
        content_splitter.setStretchFactor(1, 2)

        layout.addWidget(content_splitter, 1)

        # History dock
        self._history_dock = QDockWidget("History", self)
        self._history_dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        history_container = QWidget()
        history_container.setStyleSheet(f"background: {C['surface']};")
        history_layout = QVBoxLayout(history_container)
        history_layout.setContentsMargins(8, 8, 8, 8)
        history_layout.setSpacing(8)

        self._history_list = QListWidget()
        self._history_list.itemDoubleClicked.connect(self._on_history_selected)
        history_layout.addWidget(self._history_list, 1)

        clear_hist_btn = QPushButton("Clear History")
        clear_hist_btn.setProperty("destructive", True)
        clear_hist_btn.clicked.connect(self._on_clear_history)
        history_layout.addWidget(clear_hist_btn)

        self._history_dock.setWidget(history_container)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._history_dock)
        self._history_dock.setVisible(False)

    # ------------------------------------------------------------------
    # Mode
    # ------------------------------------------------------------------

    def _set_mode(self, mode: str):
        self._mode = mode
        self._mode_compare_btn.setChecked(mode == "compare")
        self._mode_single_btn.setChecked(mode == "single")
        self._api_tabs.clear()
        if mode == "compare":
            self._api_tabs.addTab(self._old_form, "Old API")
            self._api_tabs.addTab(self._new_form, "New API")
            self._fetch_btn.setText("\u25b6  Run Comparison")
        else:
            self._api_tabs.addTab(self._single_form, "API")
            self._fetch_btn.setText("\u25b6  Fetch Data")

    # ------------------------------------------------------------------
    # Import / Export Config
    # ------------------------------------------------------------------

    def _on_import_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Configuration", "", "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text())
        except Exception as e:
            QMessageBox.warning(self, "Import Error", str(e))
            return

        mode = data.get("mode", "compare")
        self._set_mode(mode)

        if mode == "compare":
            if "old_api" in data:
                self._old_form.set_config(data["old_api"])
            if "new_api" in data:
                self._new_form.set_config(data["new_api"])
        else:
            if "api" in data:
                self._single_form.set_config(data["api"])

        self._ids_input.setText(", ".join(data.get("ids", [])))
        self._field_mappings = data.get("field_mappings", [])
        self._ignore_input.setText(data.get("ignore_fields", ""))
        self._concurrency_spin.setValue(data.get("concurrency", 1))

    def _on_export_config(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Configuration", "diffi_config.json", "JSON Files (*.json)"
        )
        if not path:
            return

        data: dict[str, Any] = {"mode": self._mode}
        if self._mode == "compare":
            data["old_api"] = self._old_form.get_config()
            data["new_api"] = self._new_form.get_config()
        else:
            data["api"] = self._single_form.get_config()

        raw_ids = self._ids_input.text()
        data["ids"] = [
            s.strip() for s in re.split(r"[,;\s]+", raw_ids) if s.strip()
        ]
        data["field_mappings"] = self._field_mappings
        data["ignore_fields"] = self._ignore_input.text()
        data["concurrency"] = self._concurrency_spin.value()

        try:
            Path(path).write_text(json.dumps(data, indent=2))
        except Exception as e:
            QMessageBox.warning(self, "Export Error", str(e))

    # ------------------------------------------------------------------
    # Export Results
    # ------------------------------------------------------------------

    def _on_export_results(self, fmt: str):
        if not self._results:
            QMessageBox.information(self, "No Results", "Run a comparison first.")
            return

        ext_map = {"json": "json", "csv": "csv", "markdown": "md"}
        default_name = f"diffi_results.{ext_map[fmt]}"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Results", default_name, f"Files (*.{ext_map[fmt]})"
        )
        if not path:
            return

        try:
            if fmt == "json":
                content = json.dumps(self._results, indent=2, default=str)
            elif fmt == "csv":
                content = self._results_to_csv()
            else:
                content = self._results_to_markdown()
            Path(path).write_text(content)
        except Exception as e:
            QMessageBox.warning(self, "Export Error", str(e))

    def _results_to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        header = [
            "id", "status", "error", "missing_fields", "extra_fields",
            "type_changes", "response_time_old", "response_time_new",
            "status_code_old", "status_code_new",
        ]
        writer.writerow(header)
        for r in self._results:
            rt = r.get("responseTimes", {})
            sc = r.get("statusCodes", {})
            status = "error" if r.get("error") else (
                "changed" if (r.get("missing") or r.get("extra") or r.get("typeChanges"))
                else "identical"
            )
            writer.writerow([
                r.get("id", ""),
                status,
                r.get("error", ""),
                "; ".join(f["path"] for f in r.get("missing", [])),
                "; ".join(f["path"] for f in r.get("extra", [])),
                "; ".join(
                    f"{t['path']}: {t['oldType']}->{t['newType']}"
                    for t in r.get("typeChanges", [])
                ),
                rt.get("old", rt.get("single", "")),
                rt.get("new", ""),
                sc.get("old", sc.get("single", "")),
                sc.get("new", ""),
            ])
        return buf.getvalue()

    def _results_to_markdown(self) -> str:
        lines: list[str] = []
        lines.append("# Diffi Comparison Results")
        lines.append(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**Mode:** {self._mode.title()}")

        total = len(self._results)
        errors = sum(1 for r in self._results if r.get("error"))
        lines.append(f"**Total IDs:** {total}")
        lines.append(f"**Successful:** {total - errors}")
        lines.append(f"**Errors:** {errors}")
        lines.append("")

        if self._mode == "compare":
            all_missing: dict[str, list[str]] = {}
            all_extra: dict[str, list[str]] = {}
            all_type: dict[str, str] = {}
            for r in self._results:
                if r.get("error"):
                    continue
                for item in r.get("missing", []):
                    all_missing.setdefault(item["path"], []).append(r["id"])
                for item in r.get("extra", []):
                    all_extra.setdefault(item["path"], []).append(r["id"])
                for item in r.get("typeChanges", []):
                    all_type.setdefault(
                        f"{item['path']}: {item['oldType']} \u2192 {item['newType']}", []
                    ).append(r["id"])

            if all_missing:
                lines.append("## Missing Fields")
                lines.append("| Field | Affected IDs |")
                lines.append("|-------|-------------|")
                for path, ids in all_missing.items():
                    lines.append(f"| `{path}` | {', '.join(ids)} |")
                lines.append("")

            if all_extra:
                lines.append("## Extra Fields")
                lines.append("| Field | Affected IDs |")
                lines.append("|-------|-------------|")
                for path, ids in all_extra.items():
                    lines.append(f"| `{path}` | {', '.join(ids)} |")
                lines.append("")

            if all_type:
                lines.append("## Type Changes")
                lines.append("| Field | Affected IDs |")
                lines.append("|-------|-------------|")
                for desc, ids in all_type.items():
                    lines.append(f"| `{desc}` | {', '.join(ids)} |")
                lines.append("")

        lines.append("## Per-ID Results")
        for r in self._results:
            rid = r.get("id", "?")
            if r.get("error"):
                lines.append(f"### ID {rid} \u2014 Error")
                lines.append(f"> {r['error']}")
            else:
                rt = r.get("responseTimes", {})
                sc = r.get("statusCodes", {})
                rt_str = ""
                if "old" in rt and "new" in rt:
                    rt_str = f" | Time: {rt['old']:.3f}s \u2192 {rt['new']:.3f}s"
                elif "single" in rt:
                    rt_str = f" | Time: {rt['single']:.3f}s"
                sc_str = ""
                if "old" in sc and "new" in sc:
                    sc_str = f" | Status: {sc['old']} \u2192 {sc['new']}"
                elif "single" in sc:
                    sc_str = f" | Status: {sc['single']}"
                lines.append(f"### ID {rid}{rt_str}{sc_str}")
                missing = r.get("missing", [])
                extra = r.get("extra", [])
                tc = r.get("typeChanges", [])
                if missing:
                    lines.append(f"- Missing: {', '.join(i['path'] for i in missing)}")
                if extra:
                    lines.append(f"- Extra: {', '.join(i['path'] for i in extra)}")
                if tc:
                    lines.append(
                        f"- Type changes: {', '.join(i['path'] for i in tc)}"
                    )
                if not missing and not extra and not tc:
                    lines.append("- Identical")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Bulk ID Import
    # ------------------------------------------------------------------

    def _on_import_ids(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import IDs",
            "",
            "Text/CSV Files (*.txt *.csv);;All Files (*)",
        )
        if not path:
            return
        try:
            content = Path(path).read_text()
        except Exception as e:
            QMessageBox.warning(self, "Import Error", str(e))
            return

        ids: list[str] = []
        if path.endswith(".csv"):
            reader = csv.reader(io.StringIO(content))
            for row in reader:
                if row:
                    val = row[0].strip().strip('"').strip("'")
                    if val:
                        ids.append(val)
        else:
            for line in content.splitlines():
                val = line.strip().strip(",").strip(";")
                if val:
                    ids.append(val)

        if ids:
            existing = self._ids_input.text().strip()
            if existing:
                self._ids_input.setText(existing + ", " + ", ".join(ids))
            else:
                self._ids_input.setText(", ".join(ids))

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def _on_fetch(self):
        self._clear_results()
        self._error_label.setVisible(False)

        ids = [
            s.strip()
            for s in re.split(r"[,;\s]+", self._ids_input.text())
            if s.strip()
        ]
        if not ids:
            self._show_error("Provide at least one ID")
            return

        if self._mode == "compare":
            configs = [self._old_form.get_config(), self._new_form.get_config()]
            if not configs[0]["url"] or not configs[1]["url"]:
                self._show_error("Both old and new API URLs are required")
                return
        else:
            configs = [self._single_form.get_config()]
            if not configs[0]["url"]:
                self._show_error("API URL is required")
                return

        ignore_raw = self._ignore_input.text()
        ignore_fields = {
            s.strip() for s in re.split(r"[,;]+", ignore_raw) if s.strip()
        }

        self._fetch_btn.setEnabled(False)
        self._progress.setVisible(True)

        self._worker = ApiWorker(
            configs,
            ids,
            self._field_mappings,
            ignore_fields=ignore_fields,
            concurrency=self._concurrency_spin.value(),
        )
        self._worker.finished.connect(self._on_results)
        self._worker.start()

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _on_results(self, results: list[dict]):
        self._results = results
        self._fetch_btn.setEnabled(True)
        self._progress.setVisible(False)

        self._save_to_history(results)

        total = len(results)
        success = sum(1 for r in results if not r.get("error"))
        error_count = total - success

        all_missing: list[dict] = []
        all_extra: list[dict] = []
        all_type_changes: list[dict] = []

        if self._mode == "compare":
            agg_missing: dict[str, dict] = {}
            agg_extra: dict[str, dict] = {}
            agg_type_changes: dict[str, dict] = {}

            for r in results:
                if r.get("error"):
                    continue
                for item in r.get("missing", []):
                    p = item["path"]
                    if p not in agg_missing:
                        agg_missing[p] = {"path": p, "value": item["value"], "ids": []}
                    agg_missing[p]["ids"].append(r["id"])
                for item in r.get("extra", []):
                    p = item["path"]
                    if p not in agg_extra:
                        agg_extra[p] = {"path": p, "value": item["value"], "ids": []}
                    agg_extra[p]["ids"].append(r["id"])
                for item in r.get("typeChanges", []):
                    p = item["path"]
                    if p not in agg_type_changes:
                        agg_type_changes[p] = {**item, "ids": []}
                    agg_type_changes[p]["ids"].append(r["id"])

            all_missing = list(agg_missing.values())
            all_extra = list(agg_extra.values())
            all_type_changes = list(agg_type_changes.values())

        has_changes = bool(all_missing or all_extra or all_type_changes)

        # Summary card
        summary_card = Card()
        summary_layout = QVBoxLayout(summary_card)
        summary_layout.setContentsMargins(16, 14, 16, 14)
        summary_layout.setSpacing(10)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(24)
        stats = [
            ("Total IDs", str(total), C["text"]),
            ("Successful", str(success), C["green"]),
            ("Errors", str(error_count), C["red"]),
            (
                "Status",
                "Changes Detected" if has_changes else "All Identical",
                C["yellow"] if has_changes else C["green"],
            ),
        ]
        for label, value, color in stats:
            col = QVBoxLayout()
            col.setSpacing(0)
            val = QLabel(value)
            val.setStyleSheet(f"font-size: 22px; font-weight: 700; color: {color}; background: transparent;")
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color: {C['text2']}; font-size: 11px; background: transparent;")
            col.addWidget(val)
            col.addWidget(lbl)
            stats_row.addLayout(col)
        stats_row.addStretch()
        summary_layout.addLayout(stats_row)

        # Response time summary
        all_rt = [r.get("responseTimes", {}) for r in results if not r.get("error")]
        if all_rt and "old" in all_rt[0]:
            old_times = [t["old"] for t in all_rt if "old" in t]
            new_times = [t["new"] for t in all_rt if "new" in t]
            if old_times and new_times:
                avg_old = sum(old_times) / len(old_times)
                avg_new = sum(new_times) / len(new_times)
                rt_label = QLabel(
                    f"Average Response Time  \u2014  Old: {avg_old:.3f}s    New: {avg_new:.3f}s"
                )
                rt_label.setStyleSheet(f"font-size: 12px; color: {C['text2']}; background: transparent;")
                summary_layout.addWidget(rt_label)

        # Status code summary
        sc_results = [r for r in results if not r.get("error") and r.get("statusCodeDiff")]
        if sc_results:
            sc_label = QLabel(
                f"\u26a0  {len(sc_results)} ID(s) returned different status codes"
            )
            sc_label.setStyleSheet(f"color: {C['red']}; font-weight: 600; font-size: 12px; background: transparent;")
            summary_layout.addWidget(sc_label)

        if has_changes and self._mode == "compare":
            map_btn = QPushButton(
                f"Configure Field Mappings ({len(self._field_mappings)})"
                if self._field_mappings
                else "Configure Field Mappings"
            )
            map_btn.clicked.connect(self._on_configure_mappings)
            summary_layout.addWidget(map_btn)

        if all_missing:
            lbl = QLabel(f"Missing Fields \u2014 {len(all_missing)}")
            lbl.setStyleSheet(f"font-weight: 600; font-size: 12px; color: {C['red']}; background: transparent; margin-top: 4px;")
            summary_layout.addWidget(lbl)
            for item in all_missing:
                summary_layout.addWidget(
                    QLabel(
                        f"  {item['path']} \u2192 IDs: {', '.join(str(i) for i in item['ids'])}"
                    )
                )

        if all_extra:
            lbl = QLabel(f"Extra Fields \u2014 {len(all_extra)}")
            lbl.setStyleSheet(f"font-weight: 600; font-size: 12px; color: {C['yellow']}; background: transparent; margin-top: 4px;")
            summary_layout.addWidget(lbl)
            for item in all_extra:
                summary_layout.addWidget(
                    QLabel(
                        f"  {item['path']} \u2192 IDs: {', '.join(str(i) for i in item['ids'])}"
                    )
                )

        if all_type_changes:
            lbl = QLabel(f"Type Changes \u2014 {len(all_type_changes)}")
            lbl.setStyleSheet(f"font-weight: 600; font-size: 12px; color: {C['accent']}; background: transparent; margin-top: 4px;")
            summary_layout.addWidget(lbl)
            for item in all_type_changes:
                summary_layout.addWidget(
                    QLabel(
                        f"  {item['path']}: {item['oldType']} \u2192 {item['newType']}"
                    )
                )

        self._results_layout_inner.addWidget(summary_card)

        # Per-ID breakdown
        per_id_label = QLabel("Per-ID Breakdown")
        per_id_label.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {C['text2']}; background: transparent; margin-top: 8px;"
        )
        self._results_layout_inner.addWidget(per_id_label)

        for r in results:
            card = ResultCard(r)
            self._results_layout_inner.addWidget(card)

        # Raw responses
        if results and not results[0].get("error"):
            raw_label = QLabel("Raw Responses")
            raw_label.setStyleSheet(
                f"font-size: 14px; font-weight: 600; color: {C['text2']}; background: transparent; margin-top: 8px;"
            )
            self._results_layout_inner.addWidget(raw_label)
            for r in results:
                id_label = QLabel(f"ID: {r.get('id', '?')}")
                id_label.setStyleSheet(f"font-weight: 600; font-size: 12px; background: transparent;")
                self._results_layout_inner.addWidget(id_label)
                raw_text = QPlainTextEdit()
                raw_text.setReadOnly(True)
                raw_text.setMaximumHeight(140)
                if self._mode == "compare":
                    display_data = r.get("rawNewData") or r.get("newData", {})
                    raw_text.setPlainText(json.dumps(display_data, indent=2))
                else:
                    raw_text.setPlainText(json.dumps(r.get("data", {}), indent=2))
                self._results_layout_inner.addWidget(raw_text)

        self._results_layout_inner.addStretch()

    def _show_error(self, msg: str):
        self._error_label.setText(msg)
        self._error_label.setVisible(True)

    def _clear_results(self):
        while self._results_layout_inner.count():
            item = self._results_layout_inner.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._results = None

    # ------------------------------------------------------------------
    # Field Mappings
    # ------------------------------------------------------------------

    def _on_configure_mappings(self):
        if not self._results:
            return
        missing = []
        extra = []
        for r in self._results:
            if r.get("error"):
                continue
            missing.extend(r.get("missing", []))
            extra.extend(r.get("extra", []))

        dialog = FieldMappingDialog(self._field_mappings, missing, extra)
        dialog.applied.connect(self._on_mappings_applied)
        dialog.show()

    def _on_mappings_applied(self, mappings: list[dict]):
        self._field_mappings = mappings
        self._on_fetch()

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def _save_to_history(self, results: list[dict]):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "mode": self._mode,
            "ids": [
                s.strip()
                for s in re.split(r"[,;\s]+", self._ids_input.text())
                if s.strip()
            ],
            "results_summary": [],
        }
        for r in results:
            summary: dict[str, Any] = {
                "id": r.get("id"),
                "error": r.get("error"),
                "missing_count": len(r.get("missing", [])),
                "extra_count": len(r.get("extra", [])),
                "type_changes_count": len(r.get("typeChanges", [])),
            }
            rt = r.get("responseTimes", {})
            if "old" in rt:
                summary["response_time_old"] = rt["old"]
                summary["response_time_new"] = rt["new"]
            elif "single" in rt:
                summary["response_time"] = rt["single"]
            sc = r.get("statusCodes", {})
            if "old" in sc:
                summary["status_code_old"] = sc["old"]
                summary["status_code_new"] = sc["new"]
            elif "single" in sc:
                summary["status_code"] = sc["single"]
            entry["results_summary"].append(summary)
        self._history.insert(0, entry)
        self._history = self._history[:MAX_HISTORY]
        save_json(HISTORY_FILE, self._history)
        self._refresh_history_list()

    def _refresh_history_list(self):
        self._history_list.clear()
        for entry in self._history:
            ts = entry.get("timestamp", "?")
            try:
                dt = datetime.fromisoformat(ts)
                ts = dt.strftime("%b %d, %H:%M")
            except ValueError:
                pass
            mode = entry.get("mode", "?")
            n_ids = len(entry.get("ids", []))
            self._history_list.addItem(f"{ts}  \u00b7  {mode}  \u00b7  {n_ids} IDs")

    def _on_history_selected(self, item: QListWidget.Item):
        row = self._history_list.row(item)
        if row < 0 or row >= len(self._history):
            return
        entry = self._history[row]
        ids = entry.get("ids", [])
        self._ids_input.setText(", ".join(str(i) for i in ids))
        mode = entry.get("mode", "compare")
        self._set_mode(mode)

    def _on_clear_history(self):
        self._history = []
        save_json(HISTORY_FILE, self._history)
        self._refresh_history_list()

    # ------------------------------------------------------------------
    # Environment Profiles
    # ------------------------------------------------------------------

    def _refresh_profile_combo(self):
        self._profile_combo.clear()
        self._profile_combo.addItem("")
        for name in sorted(self._profiles.keys()):
            self._profile_combo.addItem(name)

    def _on_profile_selected(self, name: str):
        if not name or name not in self._profiles:
            return
        profile = self._profiles[name]
        mode = profile.get("mode", "compare")
        self._set_mode(mode)
        if mode == "compare":
            if "old_api" in profile:
                self._old_form.set_config(profile["old_api"])
            if "new_api" in profile:
                self._new_form.set_config(profile["new_api"])
        else:
            if "api" in profile:
                self._single_form.set_config(profile["api"])
        self._ids_input.setText(", ".join(profile.get("ids", [])))
        self._field_mappings = profile.get("field_mappings", [])
        self._ignore_input.setText(profile.get("ignore_fields", ""))
        self._concurrency_spin.setValue(profile.get("concurrency", 1))

    def _on_save_profile(self):
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(self, "Save Profile", "Profile name:")
        if not ok or not name.strip():
            return
        name = name.strip()

        profile: dict[str, Any] = {"mode": self._mode}
        if self._mode == "compare":
            profile["old_api"] = self._old_form.get_config()
            profile["new_api"] = self._new_form.get_config()
        else:
            profile["api"] = self._single_form.get_config()

        raw_ids = self._ids_input.text()
        profile["ids"] = [
            s.strip() for s in re.split(r"[,;\s]+", raw_ids) if s.strip()
        ]
        profile["field_mappings"] = self._field_mappings
        profile["ignore_fields"] = self._ignore_input.text()
        profile["concurrency"] = self._concurrency_spin.value()

        self._profiles[name] = profile
        save_json(PROFILES_FILE, self._profiles)
        self._refresh_profile_combo()
        self._profile_combo.setCurrentText(name)

    def _on_delete_profile(self):
        name = self._profile_combo.currentText()
        if not name or name not in self._profiles:
            return
        reply = QMessageBox.question(
            self,
            "Delete Profile",
            f"Delete profile '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            del self._profiles[name]
            save_json(PROFILES_FILE, self._profiles)
            self._refresh_profile_combo()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    import sys

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = DiffiWindow()
    window.show()
    sys.exit(app.exec())
