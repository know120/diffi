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

BTN_STYLE = (
    "QPushButton { padding: 3px 8px; font-size: 11px; border-radius: 3px; }"
)
BTN_PRIMARY = (
    "QPushButton { padding: 3px 8px; font-size: 11px; border-radius: 3px; "
    "background-color: #059669; color: white; }"
    "QPushButton:hover { background-color: #047857; }"
    "QPushButton:disabled { background-color: #065f46; color: #94a3b8; }"
)
BTN_DANGER = (
    "QPushButton { padding: 3px 8px; font-size: 11px; border-radius: 3px; "
    "background-color: #7f1d1d; color: #fca5a5; }"
    "QPushButton:hover { background-color: #991b1b; }"
)


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
# ApiFormWidget
# ---------------------------------------------------------------------------

AUTH_TYPES = ["None", "Basic Auth", "Bearer Token", "OAuth2"]


class ApiFormWidget(QWidget):
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QGroupBox { font-weight: bold; border: 1px solid #2d3748; border-radius: 6px; margin-top: 6px; padding-top: 14px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        group = QGroupBox(label)
        glayout = QGridLayout(group)
        glayout.setSpacing(4)
        glayout.setContentsMargins(8, 12, 8, 8)

        glayout.addWidget(QLabel("URL"), 0, 0)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://api.example.com/v1/{{id}}")
        glayout.addWidget(self.url_input, 0, 1)

        glayout.addWidget(QLabel("Method"), 0, 2)
        self.method_combo = QComboBox()
        self.method_combo.addItems(METHODS)
        self.method_combo.setFixedWidth(80)
        glayout.addWidget(self.method_combo, 0, 3)

        glayout.addWidget(QLabel("Auth"), 0, 4)
        self.auth_combo = QComboBox()
        self.auth_combo.addItems(AUTH_TYPES)
        self.auth_combo.setFixedWidth(100)
        self.auth_combo.currentTextChanged.connect(self._on_auth_changed)
        glayout.addWidget(self.auth_combo, 0, 5)

        # Auth fields
        self._auth_container = QHBoxLayout()
        self._auth_basic_user = QLineEdit()
        self._auth_basic_user.setPlaceholderText("User")
        self._auth_basic_user.setFixedWidth(120)
        self._auth_basic_pwd = QLineEdit()
        self._auth_basic_pwd.setPlaceholderText("Pass")
        self._auth_basic_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self._auth_basic_pwd.setFixedWidth(120)

        self._auth_bearer_token = QLineEdit()
        self._auth_bearer_token.setPlaceholderText("Token")
        self._auth_bearer_token.setFixedWidth(260)

        self._auth_oauth2_client_id = QLineEdit()
        self._auth_oauth2_client_id.setPlaceholderText("Client ID")
        self._auth_oauth2_client_id.setFixedWidth(120)
        self._auth_oauth2_client_secret = QLineEdit()
        self._auth_oauth2_client_secret.setPlaceholderText("Secret")
        self._auth_oauth2_client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._auth_oauth2_client_secret.setFixedWidth(120)
        self._auth_oauth2_token_url = QLineEdit()
        self._auth_oauth2_token_url.setPlaceholderText("Token URL")
        self._auth_oauth2_token_url.setFixedWidth(200)

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
        auth_row = QHBoxLayout()
        auth_row.setSpacing(4)
        for w in sum(self._auth_widgets.values(), []):
            auth_row.addWidget(w)
            w.setVisible(False)
        auth_row.addStretch()
        auth_widget = QWidget()
        auth_widget.setLayout(auth_row)
        glayout.addWidget(auth_widget, 1, 0, 1, 6)

        # Headers
        self.header_rows: list[dict] = []
        headers_widget = QWidget()
        headers_layout = QVBoxLayout(headers_widget)
        headers_layout.setContentsMargins(0, 0, 0, 0)
        headers_layout.setSpacing(2)

        h_row = QHBoxLayout()
        h_row.setSpacing(4)
        h_row.addWidget(QLabel("Headers"))
        h_row.addStretch()
        add_header_btn = QPushButton("+")
        add_header_btn.setFixedSize(22, 22)
        add_header_btn.setStyleSheet(BTN_STYLE)
        add_header_btn.clicked.connect(lambda: self._add_header_row())
        h_row.addWidget(add_header_btn)
        headers_layout.addLayout(h_row)

        self.headers_container = QVBoxLayout()
        self.headers_container.setSpacing(2)
        headers_layout.addLayout(self.headers_container)
        glayout.addWidget(headers_widget, 2, 0, 1, 6)

        # Params
        self.param_rows: list[dict] = []
        params_widget = QWidget()
        params_layout = QVBoxLayout(params_widget)
        params_layout.setContentsMargins(0, 0, 0, 0)
        params_layout.setSpacing(2)

        p_row = QHBoxLayout()
        p_row.setSpacing(4)
        p_row.addWidget(QLabel("Params"))
        p_row.addStretch()
        add_param_btn = QPushButton("+")
        add_param_btn.setFixedSize(22, 22)
        add_param_btn.setStyleSheet(BTN_STYLE)
        add_param_btn.clicked.connect(lambda: self._add_param_row())
        p_row.addWidget(add_param_btn)
        params_layout.addLayout(p_row)

        self.params_container = QVBoxLayout()
        self.params_container.setSpacing(2)
        params_layout.addLayout(self.params_container)
        glayout.addWidget(params_widget, 3, 0, 1, 6)

        # Body
        self.body_input = QPlainTextEdit()
        self.body_input.setPlaceholderText(
            '{"title": "foo", "body": "bar", "userId": {{id}} }'
        )
        self.body_input.setMaximumHeight(60)
        self.body_input.setVisible(False)
        glayout.addWidget(self.body_input, 4, 0, 1, 6)

        self.method_combo.currentTextChanged.connect(self._on_method_changed)
        layout.addWidget(group)

        self._add_header_row("Content-Type", "application/json")
        self._add_header_row("Accept", "application/json")

    def _on_method_changed(self, method: str):
        self.body_input.setVisible(method in ("POST", "PUT", "PATCH"))

    def _on_auth_changed(self, auth_type: str):
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
        remove_btn = QPushButton("\u2715")
        remove_btn.setFixedSize(22, 22)
        remove_btn.setStyleSheet(BTN_DANGER)

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
        self.setMinimumSize(500, 300)

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        title = QLabel("Map old API field paths to new API field paths")
        title.setStyleSheet("font-size: 13px; font-weight: bold;")
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
        scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(scroll_content)
        self.scroll_layout.setSpacing(2)
        self.scroll.setWidget(scroll_content)
        layout.addWidget(self.scroll)

        add_btn = QPushButton("+ Add mapping")
        add_btn.setStyleSheet(BTN_STYLE)
        add_btn.clicked.connect(lambda: self._add_row("", ""))
        layout.addWidget(add_btn)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(BTN_STYLE)
        cancel_btn.clicked.connect(self.close)
        apply_btn = QPushButton("Apply & Re-run")
        apply_btn.setStyleSheet(BTN_PRIMARY)
        apply_btn.clicked.connect(self._on_apply)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(apply_btn)
        layout.addLayout(btn_row)

    def _add_row(self, old_path: str = "", new_path: str = ""):
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)

        old_input = QLineEdit(old_path)
        old_input.setPlaceholderText("old.field.path")
        arrow = QLabel("\u2192")
        new_input = QLineEdit(new_path)
        new_input.setPlaceholderText("new.field.path")
        remove_btn = QPushButton("\u2715")
        remove_btn.setFixedSize(22, 22)
        remove_btn.setStyleSheet(BTN_DANGER)

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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(0)

        r = self._result
        error = r.get("error")
        has_diff = bool(r.get("missing") or r.get("extra") or r.get("typeChanges"))

        # Header
        header = QHBoxLayout()
        header.setSpacing(6)
        id_label = QLabel(f"ID: {r.get('id', '?')}")
        id_label.setStyleSheet("font-size: 12px; font-weight: bold;")
        header.addWidget(id_label)

        # Response time
        rt = r.get("responseTimes", {})
        if rt:
            if "old" in rt and "new" in rt:
                rt_text = f"\u23f1 {rt['old']:.3f}s \u2192 {rt['new']:.3f}s"
            else:
                rt_text = f"\u23f1 {rt.get('single', 0):.3f}s"
            rt_badge = QLabel(rt_text)
            rt_badge.setStyleSheet(
                "background: #172554; color: #93c5fd; padding: 1px 6px; border-radius: 3px; font-size: 10px;"
            )
            header.addWidget(rt_badge)

        # Status code
        sc = r.get("statusCodes", {})
        if sc:
            if "old" in sc and "old" in sc:
                if r.get("statusCodeDiff"):
                    sc_text = f"{sc['old']} \u2192 {sc['new']}"
                    sc_style = "background: #7f1d1d; color: #fca5a5;"
                else:
                    sc_text = f"{sc['old']}"
                    sc_style = "background: #064e3b; color: #6ee7b7;"
            else:
                sc_text = f"{sc.get('single', '?')}"
                sc_style = "background: #064e3b; color: #6ee7b7;"
            sc_badge = QLabel(sc_text)
            sc_badge.setStyleSheet(
                f"{sc_style} padding: 1px 6px; border-radius: 3px; font-size: 10px;"
            )
            header.addWidget(sc_badge)

        if error:
            badge = QLabel("Error")
            badge.setStyleSheet(
                "background: #7f1d1d; color: #fca5a5; padding: 1px 6px; border-radius: 3px; font-size: 10px;"
            )
        elif has_diff:
            badge = QLabel(
                f"{len(r.get('missing', []))}M {len(r.get('extra', []))}E {len(r.get('typeChanges', []))}T"
            )
            badge.setStyleSheet(
                "background: #78350f; color: #fcd34d; padding: 1px 6px; border-radius: 3px; font-size: 10px;"
            )
        else:
            badge = QLabel("OK")
            badge.setStyleSheet(
                "background: #064e3b; color: #6ee7b7; padding: 1px 6px; border-radius: 3px; font-size: 10px;"
            )
        header.addWidget(badge)
        header.addStretch()

        self._toggle_btn = QPushButton("\u25b6")
        self._toggle_btn.setFixedSize(20, 20)
        self._toggle_btn.setStyleSheet(BTN_STYLE)
        self._toggle_btn.clicked.connect(self._toggle)
        header.addWidget(self._toggle_btn)
        layout.addLayout(header)

        # Body
        self._body = QWidget()
        self._body.setVisible(False)
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(4, 4, 4, 4)
        body_layout.setSpacing(2)

        if error:
            err_label = QLabel(error)
            err_label.setWordWrap(True)
            err_label.setStyleSheet(
                "color: #fca5a5; padding: 6px; background: #7f1d1d; border-radius: 4px; font-size: 11px;"
            )
            body_layout.addWidget(err_label)
        else:
            if r.get("missing"):
                lbl = QLabel("Missing")
                lbl.setStyleSheet("font-weight: bold; font-size: 11px; color: #fca5a5;")
                body_layout.addWidget(lbl)
                for item in r["missing"]:
                    body_layout.addWidget(
                        QLabel(f"  {item['path']} \u2192 {json.dumps(item.get('value'))}")
                    )
            if r.get("extra"):
                lbl = QLabel("Extra")
                lbl.setStyleSheet("font-weight: bold; font-size: 11px; color: #fcd34d;")
                body_layout.addWidget(lbl)
                for item in r["extra"]:
                    body_layout.addWidget(
                        QLabel(f"  {item['path']} \u2192 {json.dumps(item.get('value'))}")
                    )
            if r.get("typeChanges"):
                lbl = QLabel("Type Changes")
                lbl.setStyleSheet("font-weight: bold; font-size: 11px; color: #93c5fd;")
                body_layout.addWidget(lbl)
                for item in r["typeChanges"]:
                    body_layout.addWidget(
                        QLabel(
                            f"  {item['path']}: {item['oldType']} \u2192 {item['newType']}"
                        )
                    )

        layout.addWidget(self._body)

        border = "#7f1d1d" if error else ("#78350f" if has_diff else "#1e293b")
        self.setStyleSheet(
            f"ResultCard {{ border: 1px solid {border}; border-radius: 6px; margin: 2px 0; background: #0f172a; }}"
        )

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
        self.setMinimumSize(900, 700)

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
        self.setStyleSheet("""
            QMainWindow { background-color: #0f172a; }
            QWidget { color: #e2e8f0; font-size: 12px; }
            QLabel { color: #e2e8f0; background: transparent; }
            QLineEdit, QPlainTextEdit, QTextEdit {
                background-color: #1e293b;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 3px 6px;
                font-size: 12px;
            }
            QLineEdit:focus, QPlainTextEdit:focus {
                border: 1px solid #3b82f6;
            }
            QComboBox {
                background-color: #1e293b;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 3px 6px;
                font-size: 12px;
            }
            QComboBox::drop-down { border: none; width: 16px; }
            QComboBox QAbstractItemView {
                background-color: #1e293b;
                color: #e2e8f0;
                selection-background-color: #334155;
                border: 1px solid #334155;
            }
            QPushButton {
                background-color: #1e293b;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 4px 10px;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #334155; border-color: #475569; }
            QPushButton:pressed { background-color: #475569; }
            QCheckBox { color: #e2e8f0; spacing: 4px; }
            QCheckBox::indicator {
                width: 14px; height: 14px;
                border: 1px solid #475569;
                border-radius: 3px;
                background: #1e293b;
            }
            QCheckBox::indicator:checked { background: #059669; border-color: #059669; }
            QSpinBox {
                background-color: #1e293b;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 3px 6px;
                font-size: 12px;
            }
            QScrollBar:vertical {
                background-color: #0f172a;
                width: 8px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background-color: #334155;
                border-radius: 4px;
                min-height: 24px;
            }
            QScrollBar::handle:vertical:hover { background-color: #475569; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QTabWidget::pane { border: 1px solid #2d3748; border-radius: 6px; background: transparent; top: -1px; }
            QTabBar::tab {
                background: #1e293b;
                color: #94a3b8;
                padding: 5px 14px;
                border: 1px solid #2d3748;
                border-bottom: none;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                margin-right: 1px;
                font-size: 11px;
            }
            QTabBar::tab:selected { background: #334155; color: #e2e8f0; border-color: #334155; }
            QTabBar::tab:hover:!selected { background: #2d3748; }
            QGroupBox {
                color: #e2e8f0;
                border: 1px solid #2d3748;
                border-radius: 6px;
                margin-top: 6px;
                padding-top: 14px;
                font-size: 12px;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; color: #94a3b8; }
            QProgressBar {
                border: 1px solid #334155;
                border-radius: 4px;
                text-align: center;
                color: #e2e8f0;
                background-color: #1e293b;
                max-height: 16px;
            }
            QProgressBar::chunk {
                background-color: #059669;
                border-radius: 3px;
            }
            QDockWidget { color: #e2e8f0; }
            QDockWidget::title {
                background: #1e293b;
                padding: 4px 8px;
                font-weight: bold;
                font-size: 11px;
                border-bottom: 1px solid #2d3748;
            }
            QListWidget {
                background-color: #020617;
                color: #e2e8f0;
                border: 1px solid #2d3748;
                border-radius: 4px;
                font-size: 11px;
            }
            QListWidget::item { padding: 4px 6px; }
            QListWidget::item:selected { background-color: #334155; }
            QListWidget::item:hover { background-color: #1e293b; }
            QSplitter::handle { background: #2d3748; height: 2px; }
        """)

    # ------------------------------------------------------------------
    # UI Build
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        # --- Top bar: title + mode toggle + env profile ---
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        title = QLabel("Diffi")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #f8fafc;")
        top_bar.addWidget(title)

        # Mode toggle
        self._mode_compare_btn = QPushButton("Compare")
        self._mode_compare_btn.setCheckable(True)
        self._mode_compare_btn.setChecked(True)
        self._mode_compare_btn.setFixedWidth(72)
        self._mode_compare_btn.setStyleSheet("""
            QPushButton { font-size: 11px; padding: 3px 8px; }
            QPushButton:checked { background: #3b82f6; color: white; border-color: #3b82f6; }
        """)
        self._mode_compare_btn.clicked.connect(lambda: self._set_mode("compare"))

        self._mode_single_btn = QPushButton("Single")
        self._mode_single_btn.setCheckable(True)
        self._mode_single_btn.setFixedWidth(60)
        self._mode_single_btn.setStyleSheet("""
            QPushButton { font-size: 11px; padding: 3px 8px; }
            QPushButton:checked { background: #3b82f6; color: white; border-color: #3b82f6; }
        """)
        self._mode_single_btn.clicked.connect(lambda: self._set_mode("single"))

        top_bar.addWidget(self._mode_compare_btn)
        top_bar.addWidget(self._mode_single_btn)
        top_bar.addSpacing(12)

        # Config buttons
        import_btn = QPushButton("\u21e7 Config")
        import_btn.setStyleSheet(BTN_STYLE)
        import_btn.setToolTip("Import configuration")
        import_btn.clicked.connect(self._on_import_config)
        top_bar.addWidget(import_btn)

        export_cfg_btn = QPushButton("\u21e9 Config")
        export_cfg_btn.setStyleSheet(BTN_STYLE)
        export_cfg_btn.setToolTip("Export configuration")
        export_cfg_btn.clicked.connect(self._on_export_config)
        top_bar.addWidget(export_cfg_btn)

        top_bar.addStretch()

        # Environment
        top_bar.addWidget(QLabel("Env:"))
        self._profile_combo = QComboBox()
        self._profile_combo.setFixedWidth(120)
        self._profile_combo.currentTextChanged.connect(self._on_profile_selected)
        top_bar.addWidget(self._profile_combo)

        save_profile_btn = QPushButton("\u2714")
        save_profile_btn.setFixedSize(24, 24)
        save_profile_btn.setStyleSheet(BTN_STYLE)
        save_profile_btn.setToolTip("Save profile")
        save_profile_btn.clicked.connect(self._on_save_profile)
        top_bar.addWidget(save_profile_btn)

        del_profile_btn = QPushButton("\u2715")
        del_profile_btn.setFixedSize(24, 24)
        del_profile_btn.setStyleSheet(BTN_DANGER)
        del_profile_btn.setToolTip("Delete profile")
        del_profile_btn.clicked.connect(self._on_delete_profile)
        top_bar.addWidget(del_profile_btn)

        layout.addLayout(top_bar)

        # --- Content area ---
        content_splitter = QSplitter(Qt.Orientation.Vertical)

        # Top: API config
        config_widget = QWidget()
        config_layout = QVBoxLayout(config_widget)
        config_layout.setContentsMargins(0, 0, 0, 0)
        config_layout.setSpacing(4)

        self._api_tabs = QTabWidget()
        self._api_tabs.setStyleSheet("QTabWidget::pane { padding: 4px; }")
        self._old_form = ApiFormWidget("Old API")
        self._new_form = ApiFormWidget("New API")
        self._single_form = ApiFormWidget("API")

        self._api_tabs.addTab(self._old_form, "Old API")
        self._api_tabs.addTab(self._new_form, "New API")
        config_layout.addWidget(self._api_tabs)

        # IDs row
        ids_row = QHBoxLayout()
        ids_row.setSpacing(6)
        ids_row.addWidget(QLabel("IDs"))
        self._ids_input = QLineEdit()
        self._ids_input.setPlaceholderText("1, 2, 3, 5, 10")
        ids_row.addWidget(self._ids_input, 1)

        import_ids_btn = QPushButton("\u21e7 Import")
        import_ids_btn.setStyleSheet(BTN_STYLE)
        import_ids_btn.setToolTip("Import IDs from file")
        import_ids_btn.clicked.connect(self._on_import_ids)
        ids_row.addWidget(import_ids_btn)

        ids_row.addWidget(QLabel("Threads"))
        self._concurrency_spin = QSpinBox()
        self._concurrency_spin.setRange(1, 20)
        self._concurrency_spin.setValue(1)
        self._concurrency_spin.setFixedWidth(44)
        ids_row.addWidget(self._concurrency_spin)

        ids_row.addSpacing(8)

        ids_row.addWidget(QLabel("Ignore"))
        self._ignore_input = QLineEdit()
        self._ignore_input.setPlaceholderText("timestamp, request_id, user.created_at")
        self._ignore_input.setToolTip(
            "Comma-separated field paths to exclude from comparison.\n"
            "Supports prefixes: 'user' ignores 'user.name', 'user.email', etc."
        )
        ids_row.addWidget(self._ignore_input, 1)

        config_layout.addLayout(ids_row)

        # Fetch button + progress
        fetch_row = QHBoxLayout()
        fetch_row.setSpacing(6)
        self._fetch_btn = QPushButton("\u25b6 Run")
        self._fetch_btn.setStyleSheet(BTN_PRIMARY)
        self._fetch_btn.setFixedWidth(80)
        self._fetch_btn.clicked.connect(self._on_fetch)
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setMaximum(0)
        self._progress.setFixedWidth(120)
        fetch_row.addWidget(self._fetch_btn)
        fetch_row.addWidget(self._progress)
        fetch_row.addStretch()
        config_layout.addLayout(fetch_row)

        content_splitter.addWidget(config_widget)

        # Bottom: Results
        results_widget = QWidget()
        results_layout = QVBoxLayout(results_widget)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(4)

        results_header = QHBoxLayout()
        results_header.setSpacing(6)
        results_title = QLabel("Results")
        results_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #94a3b8;")
        results_header.addWidget(results_title)
        results_header.addStretch()

        self._mapping_btn = QPushButton("Mappings")
        self._mapping_btn.setVisible(False)
        self._mapping_btn.setStyleSheet(BTN_STYLE)
        self._mapping_btn.clicked.connect(self._on_configure_mappings)
        results_header.addWidget(self._mapping_btn)

        toggle_hist_btn = QPushButton("History")
        toggle_hist_btn.setStyleSheet(BTN_STYLE)
        toggle_hist_btn.clicked.connect(
            lambda: self._history_dock.setVisible(not self._history_dock.isVisible())
        )
        results_header.addWidget(toggle_hist_btn)

        export_json_btn = QPushButton("JSON")
        export_json_btn.setStyleSheet(BTN_STYLE)
        export_json_btn.setFixedWidth(44)
        export_json_btn.clicked.connect(lambda: self._on_export_results("json"))
        results_header.addWidget(export_json_btn)

        export_csv_btn = QPushButton("CSV")
        export_csv_btn.setStyleSheet(BTN_STYLE)
        export_csv_btn.setFixedWidth(40)
        export_csv_btn.clicked.connect(lambda: self._on_export_results("csv"))
        results_header.addWidget(export_csv_btn)

        export_md_btn = QPushButton("MD")
        export_md_btn.setStyleSheet(BTN_STYLE)
        export_md_btn.setFixedWidth(36)
        export_md_btn.clicked.connect(lambda: self._on_export_results("markdown"))
        results_header.addWidget(export_md_btn)

        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        self._error_label.setStyleSheet(
            "color: #fca5a5; padding: 6px; background: #7f1d1d; border-radius: 4px; font-size: 11px;"
        )
        results_layout.addWidget(self._error_label)
        results_layout.addLayout(results_header)

        self._results_scroll = QScrollArea()
        self._results_scroll.setWidgetResizable(True)
        self._results_content = QWidget()
        self._results_layout_inner = QVBoxLayout(self._results_content)
        self._results_layout_inner.setSpacing(4)
        self._results_scroll.setWidget(self._results_content)
        results_layout.addWidget(self._results_scroll)

        content_splitter.addWidget(results_widget)
        content_splitter.setStretchFactor(0, 1)
        content_splitter.setStretchFactor(1, 2)

        layout.addWidget(content_splitter)

        # History dock
        self._history_dock = QDockWidget("History", self)
        self._history_dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        history_container = QWidget()
        history_layout = QVBoxLayout(history_container)
        history_layout.setContentsMargins(4, 4, 4, 4)
        history_layout.setSpacing(4)

        self._history_list = QListWidget()
        self._history_list.itemDoubleClicked.connect(self._on_history_selected)
        history_layout.addWidget(self._history_list)

        clear_hist_btn = QPushButton("Clear")
        clear_hist_btn.setStyleSheet(BTN_DANGER)
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
            self._fetch_btn.setText("\u25b6 Run")
        else:
            self._api_tabs.addTab(self._single_form, "API")
            self._fetch_btn.setText("\u25b6 Fetch")

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
        summary_group = QGroupBox("Summary")
        summary_layout = QVBoxLayout(summary_group)
        summary_layout.setSpacing(4)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(16)
        stats = [
            ("Total", str(total), "#f8fafc"),
            ("OK", str(success), "#6ee7b7"),
            ("Err", str(error_count), "#fca5a5"),
            (
                "Status",
                "Changed" if has_changes else "Identical",
                "#fcd34d" if has_changes else "#6ee7b7",
            ),
        ]
        for label, value, color in stats:
            col = QHBoxLayout()
            col.setSpacing(4)
            val = QLabel(value)
            val.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {color};")
            lbl = QLabel(label)
            lbl.setStyleSheet("color: #64748b; font-size: 11px;")
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
                    f"\u23f1 Avg: Old {avg_old:.3f}s | New {avg_new:.3f}s"
                )
                rt_label.setStyleSheet("font-size: 11px; color: #94a3b8;")
                summary_layout.addWidget(rt_label)

        # Status code summary
        sc_results = [r for r in results if not r.get("error") and r.get("statusCodeDiff")]
        if sc_results:
            sc_label = QLabel(
                f"\u26a0 {len(sc_results)} ID(s) have different status codes"
            )
            sc_label.setStyleSheet("color: #fca5a5; font-weight: bold; font-size: 11px;")
            summary_layout.addWidget(sc_label)

        if has_changes and self._mode == "compare":
            map_btn = QPushButton(
                f"Mappings ({len(self._field_mappings)})"
                if self._field_mappings
                else "Configure Mappings"
            )
            map_btn.setStyleSheet(BTN_STYLE)
            map_btn.clicked.connect(self._on_configure_mappings)
            summary_layout.addWidget(map_btn)

        if all_missing:
            lbl = QLabel(f"Missing Fields \u2014 {len(all_missing)}")
            lbl.setStyleSheet("font-weight: bold; font-size: 11px; color: #fca5a5;")
            summary_layout.addWidget(lbl)
            for item in all_missing:
                summary_layout.addWidget(
                    QLabel(
                        f"  {item['path']} \u2192 {', '.join(str(i) for i in item['ids'])}"
                    )
                )

        if all_extra:
            lbl = QLabel(f"Extra Fields \u2014 {len(all_extra)}")
            lbl.setStyleSheet("font-weight: bold; font-size: 11px; color: #fcd34d;")
            summary_layout.addWidget(lbl)
            for item in all_extra:
                summary_layout.addWidget(
                    QLabel(
                        f"  {item['path']} \u2192 {', '.join(str(i) for i in item['ids'])}"
                    )
                )

        if all_type_changes:
            lbl = QLabel(f"Type Changes \u2014 {len(all_type_changes)}")
            lbl.setStyleSheet("font-weight: bold; font-size: 11px; color: #93c5fd;")
            summary_layout.addWidget(lbl)
            for item in all_type_changes:
                summary_layout.addWidget(
                    QLabel(
                        f"  {item['path']}: {item['oldType']} \u2192 {item['newType']}"
                    )
                )

        self._results_layout_inner.addWidget(summary_group)

        # Per-ID breakdown
        per_id_group = QGroupBox("Per-ID Breakdown")
        per_id_layout = QVBoxLayout(per_id_group)
        per_id_layout.setSpacing(2)
        for r in results:
            card = ResultCard(r)
            per_id_layout.addWidget(card)
        per_id_layout.addStretch()
        self._results_layout_inner.addWidget(per_id_group)

        # Raw responses
        if results and not results[0].get("error"):
            raw_group = QGroupBox("Raw Responses")
            raw_layout = QVBoxLayout(raw_group)
            raw_layout.setSpacing(4)
            for r in results:
                id_label = QLabel(f"ID: {r.get('id', '?')}")
                id_label.setStyleSheet("font-weight: bold; font-size: 11px;")
                raw_layout.addWidget(id_label)
                raw_text = QPlainTextEdit()
                raw_text.setReadOnly(True)
                raw_text.setMaximumHeight(120)
                raw_text.setStyleSheet("font-size: 11px;")
                if self._mode == "compare":
                    display_data = r.get("rawNewData") or r.get("newData", {})
                    raw_text.setPlainText(json.dumps(display_data, indent=2))
                    raw_layout.addWidget(raw_text)
                else:
                    raw_text.setPlainText(json.dumps(r.get("data", {}), indent=2))
                    raw_layout.addWidget(raw_text)
            self._results_layout_inner.addWidget(raw_group)

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
                ts = dt.strftime("%m/%d %H:%M")
            except ValueError:
                pass
            mode = entry.get("mode", "?")
            n_ids = len(entry.get("ids", []))
            self._history_list.addItem(f"{ts} | {mode} | {n_ids} IDs")

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
