from __future__ import annotations

import json
import re
import threading
from typing import Any

import requests
from PySide6.QtCore import QSize, Qt, Signal, QThread
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QHeaderView,
)

from .comparator import apply_field_mappings, collect_fields, deep_compare


METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
TIMEOUT = 30


class ApiWorker(QThread):
    finished = Signal(object)
    error_signal = Signal(str, str)  # id, error message

    def __init__(self, configs: list[dict], ids: list[str], mappings: list[dict], parent=None):
        super().__init__(parent)
        self.configs = configs
        self.ids = ids
        self.mappings = mappings

    def run(self):
        results = []
        for id_ in self.ids:
            id_results = {"id": id_}
            try:
                datas = []
                for config in self.configs:
                    data = self._call_api(config, id_)
                    datas.append(data)
                if len(datas) == 2:
                    old_data, raw_new = datas
                    new_data = (
                        apply_field_mappings(raw_new, self.mappings)
                        if self.mappings
                        else raw_new
                    )
                    comparison = deep_compare(old_data, new_data)
                    id_results["oldData"] = old_data
                    id_results["newData"] = new_data
                    id_results["rawNewData"] = raw_new if self.mappings else None
                    id_results["oldFields"] = collect_fields(old_data)
                    id_results["newFields"] = collect_fields(new_data)
                    id_results.update(comparison)
                else:
                    data = datas[0]
                    id_results["data"] = data
                    id_results["fields"] = collect_fields(data)
                id_results["error"] = None
            except Exception as e:
                id_results["error"] = str(e)
                id_results.setdefault("missing", [])
                id_results.setdefault("extra", [])
                id_results.setdefault("typeChanges", [])
            results.append(id_results)
        self.finished.emit(results)

    def _call_api(self, config: dict, id_: str) -> Any:
        url = config["url"].replace("{{id}}", str(id_))
        method = config.get("method", "GET").upper()

        try:
            from urllib.parse import urlencode, urlparse, urlunstable

            parsed = list(urlparse(url))
            params = config.get("params", {})
            if params:
                existing = parsed[4]
                qs = urlencode({k: v.replace("{{id}}", str(id_)) for k, v in params.items()})
                parsed[4] = (existing + "&" + qs) if existing else qs
            full_url = urlunstable(parsed)
        except Exception:
            raise ValueError(f'Invalid URL: "{url}". Must start with http:// or https://')

        headers = dict(config.get("headers", {}))
        fetch_options: dict[str, Any] = {"method": method, "headers": headers}

        if method in ("POST", "PUT", "PATCH") and config.get("body"):
            body = config["body"].replace("{{id}}", str(id_))
            fetch_options["data"] = body
            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"

        try:
            res = requests.request(method, full_url, headers=headers, data=fetch_options.get("data"), timeout=TIMEOUT)
            if not res.ok:
                raise ValueError(f"HTTP {res.status_code}: {res.reason}")
            return res.json()
        except requests.exceptions.Timeout:
            raise ValueError(f"Request timed out after {TIMEOUT}s: {method} {full_url}")
        except requests.exceptions.ConnectionError:
            raise ValueError(
                f"Network error: {method} {full_url}\n\n"
                f"Possible causes:\n"
                f"• The server is unreachable or DNS resolution failed\n"
                f"• The URL is incorrect or missing a protocol\n"
                f"• A firewall is blocking the connection"
            )


class ApiFormWidget(QWidget):
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QGroupBox { font-weight: bold; border: 1px solid #444; border-radius: 8px; margin-top: 8px; padding-top: 16px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox(label)
        glayout = QGridLayout(group)

        # URL
        glayout.addWidget(QLabel("URL:"), 0, 0)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://api.example.com/v1/{{id}}")
        glayout.addWidget(self.url_input, 0, 1)

        # Method
        glayout.addWidget(QLabel("Method:"), 0, 2)
        self.method_combo = QComboBox()
        self.method_combo.addItems(METHODS)
        glayout.addWidget(self.method_combo, 0, 3)

        # Headers
        self.header_rows: list[dict] = []
        headers_widget = QWidget()
        headers_layout = QVBoxLayout(headers_widget)
        headers_layout.setContentsMargins(0, 0, 0, 0)

        headers_label_row = QHBoxLayout()
        headers_label_row.addWidget(QLabel("Headers"))
        headers_label_row.addStretch()
        add_header_btn = QPushButton("+ Add")
        add_header_btn.setFixedWidth(60)
        add_header_btn.clicked.connect(lambda: self._add_header_row())
        headers_label_row.addWidget(add_header_btn)
        headers_layout.addLayout(headers_label_row)

        self.headers_container = QVBoxLayout()
        headers_layout.addLayout(self.headers_container)
        glayout.addWidget(headers_widget, 1, 0, 1, 4)

        # Params
        self.param_rows: list[dict] = []
        params_widget = QWidget()
        params_layout = QVBoxLayout(params_widget)
        params_layout.setContentsMargins(0, 0, 0, 0)

        params_label_row = QHBoxLayout()
        params_label_row.addWidget(QLabel("Query Params"))
        params_label_row.addStretch()
        add_param_btn = QPushButton("+ Add")
        add_param_btn.setFixedWidth(60)
        add_param_btn.clicked.connect(lambda: self._add_param_row())
        params_label_row.addWidget(add_param_btn)
        params_layout.addLayout(params_label_row)

        self.params_container = QVBoxLayout()
        params_layout.addLayout(self.params_container)
        glayout.addWidget(params_widget, 2, 0, 1, 4)

        # Body
        glayout.addWidget(QLabel("Request Body:"), 3, 0)
        self.body_input = QPlainTextEdit()
        self.body_input.setPlaceholderText('{"title": "foo", "body": "bar", "userId": {{id}} }')
        self.body_input.setMaximumHeight(80)
        self.body_input.setVisible(False)
        glayout.addWidget(self.body_input, 3, 1, 1, 3)

        self.method_combo.currentTextChanged.connect(self._on_method_changed)

        layout.addWidget(group)

        # Add default rows
        self._add_header_row("Content-Type", "application/json")
        self._add_header_row("Accept", "application/json")

    def _on_method_changed(self, method: str):
        self.body_input.setVisible(method in ("POST", "PUT", "PATCH"))

    def _add_header_row(self, key="", value=""):
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
        row_layout.setContentsMargins(0, 2, 0, 2)

        enabled_cb = QCheckBox()
        enabled_cb.setChecked(True)
        key_input = QLineEdit()
        key_input.setPlaceholderText("Key")
        key_input.setFixedWidth(150)
        value_input = QLineEdit()
        value_input.setPlaceholderText("Value")
        remove_btn = QPushButton("✕")
        remove_btn.setFixedWidth(24)

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
        row_layout.addWidget(value_input)
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

        return {
            "url": self.url_input.text(),
            "method": self.method_combo.currentText(),
            "headers": headers,
            "params": params,
            "body": self.body_input.toPlainText(),
        }

    def set_config(self, config: dict):
        self.url_input.setText(config.get("url", ""))
        idx = self.method_combo.findText(config.get("method", "GET"))
        if idx >= 0:
            self.method_combo.setCurrentIndex(idx)


class FieldMappingDialog(QWidget):
    applied = Signal(list)

    def __init__(self, mappings: list[dict], missing: list[dict], extra: list[dict]):
        super().__init__()
        self.setWindowTitle("Field Mappings")
        self.setMinimumSize(500, 300)

        layout = QVBoxLayout(self)

        title = QLabel("Map old API field paths to new API field paths")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(title)

        self.rows: list[dict] = []

        # Pre-populate suggestions
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
        self.scroll.setWidget(scroll_content)
        layout.addWidget(self.scroll)

        add_btn = QPushButton("+ Add mapping")
        add_btn.clicked.connect(lambda: self._add_row("", ""))
        layout.addWidget(add_btn)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.close)
        apply_btn = QPushButton("Apply & Re-run")
        apply_btn.setStyleSheet(
            "background-color: #059669; color: white; padding: 6px 16px; border-radius: 4px;"
        )
        apply_btn.clicked.connect(self._on_apply)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(apply_btn)
        layout.addLayout(btn_row)

    def _add_row(self, old_path="", new_path=""):
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 2, 0, 2)

        old_input = QLineEdit(old_path)
        old_input.setPlaceholderText("old.field.path")
        arrow = QLabel("→")
        new_input = QLineEdit(new_path)
        new_input.setPlaceholderText("new.field.path")
        remove_btn = QPushButton("✕")
        remove_btn.setFixedWidth(24)

        row_data = {"widget": row_widget, "oldPath": old_input, "newPath": new_input}
        self.rows.append(row_data)
        remove_btn.clicked.connect(lambda: self._remove_row(row_widget))

        row_layout.addWidget(old_input)
        row_layout.addWidget(arrow)
        row_layout.addWidget(new_input)
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


class ResultCard(QWidget):
    def __init__(self, result: dict):
        super().__init__()
        self._expanded = False
        self._result = result
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        r = self._result
        error = r.get("error")
        has_diff = bool(r.get("missing") or r.get("extra") or r.get("typeChanges"))

        # Header
        header = QHBoxLayout()
        id_label = QLabel(f"ID: {r.get('id', '?')}")
        id_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        header.addWidget(id_label)

        if error:
            badge = QLabel("Error")
            badge.setStyleSheet("background: #7f1d1d; color: #fca5a5; padding: 2px 8px; border-radius: 4px;")
        elif has_diff:
            badge = QLabel(
                f"{len(r.get('missing', []))} missing, "
                f"{len(r.get('extra', []))} extra, "
                f"{len(r.get('typeChanges', []))} type changes"
            )
            badge.setStyleSheet("background: #78350f; color: #fcd34d; padding: 2px 8px; border-radius: 4px;")
        else:
            badge = QLabel("Identical")
            badge.setStyleSheet("background: #064e3b; color: #6ee7b7; padding: 2px 8px; border-radius: 4px;")
        header.addWidget(badge)
        header.addStretch()

        self._toggle_btn = QPushButton("▼" if self._expanded else "▶")
        self._toggle_btn.setFixedWidth(24)
        self._toggle_btn.clicked.connect(self._toggle)
        header.addWidget(self._toggle_btn)
        layout.addLayout(header)

        # Body (hidden by default)
        self._body = QWidget()
        self._body.setVisible(False)
        body_layout = QVBoxLayout(self._body)

        if error:
            err_label = QLabel(error)
            err_label.setWordWrap(True)
            err_label.setStyleSheet("color: #fca5a5; padding: 8px; background: #7f1d1d; border-radius: 4px;")
            body_layout.addWidget(err_label)
        else:
            if r.get("missing"):
                body_layout.addWidget(QLabel("Missing Fields:"))
                for item in r["missing"]:
                    body_layout.addWidget(QLabel(f"  {item['path']} → {json.dumps(item.get('value'))}"))
            if r.get("extra"):
                body_layout.addWidget(QLabel("Extra Fields:"))
                for item in r["extra"]:
                    body_layout.addWidget(QLabel(f"  {item['path']} → {json.dumps(item.get('value'))}"))
            if r.get("typeChanges"):
                body_layout.addWidget(QLabel("Type Changes:"))
                for item in r["typeChanges"]:
                    body_layout.addWidget(
                        QLabel(f"  {item['path']}: {item['oldType']} → {item['newType']}")
                    )

        layout.addWidget(self._body)

        # Border color
        border = "#7f1d1d" if error else ("#78350f" if has_diff else "#1e293b")
        self.setStyleSheet(
            f"ResultCard {{ border: 1px solid {border}; border-radius: 8px; margin: 4px 0; }}"
        )

    def _toggle(self):
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._toggle_btn.setText("▼" if self._expanded else "▶")


class DiffiWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Diffi — API Comparator")
        self.setMinimumSize(900, 700)

        self._mode = "compare"  # "compare" | "single"
        self._results: list[dict] | None = None
        self._field_mappings: list[dict] = []

        self._build_ui()
        self._apply_styles()

    def _apply_styles(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #0f172a; }
            QLabel { color: #e2e8f0; }
            QLineEdit, QPlainTextEdit, QTextEdit {
                background-color: #020617;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QComboBox {
                background-color: #020617;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #1e293b;
                color: #e2e8f0;
                selection-background-color: #334155;
            }
            QPushButton {
                background-color: #334155;
                color: #e2e8f0;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
            }
            QPushButton:hover { background-color: #475569; }
            QCheckBox { color: #e2e8f0; }
            QScrollBar:vertical {
                background-color: #0f172a;
                width: 10px;
            }
            QScrollBar::handle:vertical {
                background-color: #334155;
                border-radius: 5px;
                min-height: 30px;
            }
            QTabWidget::pane { border: 1px solid #334155; border-radius: 8px; background: transparent; }
            QTabBar::tab {
                background: #1e293b;
                color: #94a3b8;
                padding: 8px 16px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                margin-right: 2px;
            }
            QTabBar::tab:selected { background: #334155; color: #e2e8f0; }
            QGroupBox { color: #e2e8f0; border: 1px solid #334155; border-radius: 8px; margin-top: 8px; padding-top: 16px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #e2e8f0; }
            QProgressBar {
                border: 1px solid #334155;
                border-radius: 4px;
                text-align: center;
                color: #e2e8f0;
                background-color: #1e293b;
            }
            QProgressBar::chunk {
                background-color: #059669;
                border-radius: 3px;
            }
        """)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)

        # Title
        title = QLabel("Diffi — API Comparator")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #f8fafc;")
        layout.addWidget(title)

        # Mode toggle
        mode_row = QHBoxLayout()
        self._mode_compare_btn = QPushButton("Compare (Old vs New)")
        self._mode_compare_btn.setCheckable(True)
        self._mode_compare_btn.setChecked(True)
        self._mode_single_btn = QPushButton("Single API")
        self._mode_single_btn.setCheckable(True)

        self._mode_compare_btn.clicked.connect(lambda: self._set_mode("compare"))
        self._mode_single_btn.clicked.connect(lambda: self._set_mode("single"))

        mode_row.addWidget(self._mode_compare_btn)
        mode_row.addWidget(self._mode_single_btn)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        # Content area
        content_splitter = QSplitter(Qt.Orientation.Vertical)

        # Top: API config
        config_widget = QWidget()
        config_layout = QVBoxLayout(config_widget)

        self._api_tabs = QTabWidget()
        self._old_form = ApiFormWidget("Old API")
        self._new_form = ApiFormWidget("New API")
        self._single_form = ApiFormWidget("API")

        self._api_tabs.addTab(self._old_form, "Old API")
        self._api_tabs.addTab(self._new_form, "New API")
        config_layout.addWidget(self._api_tabs)

        # IDs
        ids_widget = QWidget()
        ids_layout = QHBoxLayout(ids_widget)
        ids_layout.addWidget(QLabel("IDs (comma/space/semicolon separated):"))
        self._ids_input = QLineEdit()
        self._ids_input.setPlaceholderText("1, 2, 3, 5, 10")
        ids_layout.addWidget(self._ids_input)
        config_layout.addWidget(ids_widget)

        # Fetch button + progress
        fetch_row = QHBoxLayout()
        self._fetch_btn = QPushButton("▶ Run Comparison")
        self._fetch_btn.setStyleSheet(
            "background-color: #059669; color: white; font-weight: bold; padding: 10px 24px; border-radius: 6px;"
            "QPushButton:hover { background-color: #047857; }"
            "QPushButton:disabled { background-color: #065f46; color: #94a3b8; }"
        )
        self._fetch_btn.clicked.connect(self._on_fetch)
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setMaximum(0)  # indeterminate
        fetch_row.addWidget(self._fetch_btn)
        fetch_row.addWidget(self._progress)
        fetch_row.addStretch()
        config_layout.addLayout(fetch_row)

        content_splitter.addWidget(config_widget)

        # Bottom: Results
        results_widget = QWidget()
        results_layout = QVBoxLayout(results_widget)

        results_header = QHBoxLayout()
        results_title = QLabel("Results")
        results_title.setStyleSheet("font-size: 18px; font-weight: bold;")
        results_header.addWidget(results_title)
        results_header.addStretch()

        self._mapping_btn = QPushButton("Configure Field Mappings")
        self._mapping_btn.setVisible(False)
        self._mapping_btn.clicked.connect(self._on_configure_mappings)
        results_header.addWidget(self._mapping_btn)

        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        self._error_label.setStyleSheet("color: #fca5a5; padding: 8px; background: #7f1d1d; border-radius: 4px;")
        results_layout.addWidget(self._error_label)

        results_layout.addLayout(results_header)

        self._results_scroll = QScrollArea()
        self._results_scroll.setWidgetResizable(True)
        self._results_content = QWidget()
        self._results_layout_inner = QVBoxLayout(self._results_content)
        self._results_scroll.setWidget(self._results_content)
        results_layout.addWidget(self._results_scroll)

        content_splitter.addWidget(results_widget)
        content_splitter.setStretchFactor(0, 1)
        content_splitter.setStretchFactor(1, 2)

        layout.addWidget(content_splitter)

    def _set_mode(self, mode: str):
        self._mode = mode
        self._mode_compare_btn.setChecked(mode == "compare")
        self._mode_single_btn.setChecked(mode == "single")
        self._api_tabs.clear()
        if mode == "compare":
            self._api_tabs.addTab(self._old_form, "Old API")
            self._api_tabs.addTab(self._new_form, "New API")
            self._fetch_btn.setText("▶ Run Comparison")
        else:
            self._api_tabs.addTab(self._single_form, "API")
            self._fetch_btn.setText("▶ Fetch Data")

    def _on_fetch(self):
        # Clear previous results
        self._clear_results()
        self._error_label.setVisible(False)

        ids = [s.strip() for s in re.split(r"[,;\s]+", self._ids_input.text()) if s.strip()]
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

        self._fetch_btn.setEnabled(False)
        self._progress.setVisible(True)

        self._worker = ApiWorker(configs, ids, self._field_mappings)
        self._worker.finished.connect(self._on_results)
        self._worker.error_signal.connect(self._on_worker_error)
        self._worker.start()

    def _on_results(self, results: list[dict]):
        self._results = results
        self._fetch_btn.setEnabled(True)
        self._progress.setVisible(False)

        # Summary
        total = len(results)
        success = sum(1 for r in results if not r.get("error"))
        error_count = total - success

        all_missing = []
        all_extra = []
        all_type_changes = []

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
        summary_group = QGroupBox("Summary Report")
        summary_layout = QVBoxLayout(summary_group)

        stats_row = QHBoxLayout()
        stats = [
            ("Total IDs", str(total), "#f8fafc"),
            ("Successful", str(success), "#6ee7b7"),
            ("Errors", str(error_count), "#fca5a5"),
            ("Status", "Changes" if has_changes else "No Changes", "#fcd34d" if has_changes else "#6ee7b7"),
        ]
        for label, value, color in stats:
            col = QVBoxLayout()
            val = QLabel(value)
            val.setStyleSheet(f"font-size: 24px; font-weight: bold; color: {color};")
            lbl = QLabel(label)
            lbl.setStyleSheet("color: #94a3b8;")
            col.addWidget(val)
            col.addWidget(lbl)
            stats_row.addLayout(col)
        summary_layout.addLayout(stats_row)

        # Mapping button in summary
        if has_changes and self._mode == "compare":
            map_btn = QPushButton(
                f"Configure Field Mappings ({len(self._field_mappings)})"
                if self._field_mappings
                else "Configure Field Mappings"
            )
            map_btn.clicked.connect(self._on_configure_mappings)
            summary_layout.addWidget(map_btn)

        # Tables for missing/extra/type changes
        if all_missing:
            summary_layout.addWidget(QLabel(f"Missing Fields — {len(all_missing)}"))
            for item in all_missing:
                summary_layout.addWidget(
                    QLabel(f"  {item['path']} → IDs: {', '.join(str(i) for i in item['ids'])}")
                )

        if all_extra:
            summary_layout.addWidget(QLabel(f"Extra Fields — {len(all_extra)}"))
            for item in all_extra:
                summary_layout.addWidget(
                    QLabel(f"  {item['path']} → IDs: {', '.join(str(i) for i in item['ids'])}")
                )

        if all_type_changes:
            summary_layout.addWidget(QLabel(f"Type Changes — {len(all_type_changes)}"))
            for item in all_type_changes:
                summary_layout.addWidget(
                    QLabel(f"  {item['path']}: {item['oldType']} → {item['newType']}")
                )

        self._results_layout_inner.addWidget(summary_group)

        # Per-ID breakdown
        per_id_group = QGroupBox("Per-ID Breakdown")
        per_id_layout = QVBoxLayout(per_id_group)
        for r in results:
            card = ResultCard(r)
            per_id_layout.addWidget(card)
        per_id_layout.addStretch()
        self._results_layout_inner.addWidget(per_id_group)

        # Raw responses
        if results and not results[0].get("error"):
            raw_group = QGroupBox("Raw Responses")
            raw_layout = QVBoxLayout(raw_group)
            for r in results:
                id_label = QLabel(f"ID: {r.get('id', '?')}")
                id_label.setStyleSheet("font-weight: bold;")
                raw_layout.addWidget(id_label)
                if self._mode == "compare":
                    raw_text = QPlainTextEdit()
                    raw_text.setReadOnly(True)
                    raw_text.setMaximumHeight(150)
                    display_data = r.get("rawNewData") or r.get("newData", {})
                    raw_text.setPlainText(json.dumps(display_data, indent=2))
                    raw_layout.addWidget(QLabel("New API Response:"))
                    raw_layout.addWidget(raw_text)
                else:
                    raw_text = QPlainTextEdit()
                    raw_text.setReadOnly(True)
                    raw_text.setMaximumHeight(150)
                    raw_text.setPlainText(json.dumps(r.get("data", {}), indent=2))
                    raw_layout.addWidget(raw_text)
            self._results_layout_inner.addWidget(raw_group)

    def _on_worker_error(self, id_: str, msg: str):
        self._show_error(f"ID {id_}: {msg}")

    def _show_error(self, msg: str):
        self._error_label.setText(msg)
        self._error_label.setVisible(True)

    def _clear_results(self):
        while self._results_layout_inner.count():
            item = self._results_layout_inner.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._results = None

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


def main():
    import sys
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = DiffiWindow()
    window.show()
    sys.exit(app.exec())
