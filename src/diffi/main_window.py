"""Main application window — orchestrates all widgets and features."""
from __future__ import annotations

import csv
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFrame,
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
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QMessageBox,
)

from .api_form import ApiFormWidget
from .api_worker import ApiWorker
from .comparator import apply_field_mappings
from .field_mapping_dialog import FieldMappingDialog
from .result_card import ResultCard
from .utils import (
    AUTH_TYPES,
    HISTORY_FILE,
    MAX_HISTORY,
    PROFILES_FILE,
    load_json,
    save_json,
)


class DiffiWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Diffi \u2014 API Comparator")
        self.setMinimumSize(1000, 740)
        self._mode: str = "compare"
        self._results: list[dict] | None = None
        self._field_mappings: list[dict] = []
        self._history: list[dict] = load_json(HISTORY_FILE) or []
        self._profiles: dict[str, dict] = load_json(PROFILES_FILE) or {}
        self._worker: ApiWorker | None = None
        self._unsaved_changes: bool = False
        self._build_ui()
        self._apply_styles()
        self._setup_shortcuts()
        self._refresh_history_list()
        self._refresh_profile_combo()
        self._update_empty_state()

    # ======================================================================
    # Styles
    # ======================================================================

    def _apply_styles(self) -> None:
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
            QLabel#emptyState { font-size: 14px; color: #585b70; padding: 40px; }

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

            QPushButton#cancelBtn {
                background: #f38ba8;
                color: #1e1e2e;
                border: none;
                font-weight: 600;
                padding: 0;
            }
            QPushButton#cancelBtn:hover { background: #f5a0bc; }

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

            QPlainTextEdit#diffView {
                background: #11111b;
                border: 1px solid #313244;
                border-radius: 6px;
                font-family: "Cascadia Code", "Fira Code", "Consolas", monospace;
                font-size: 11px;
                padding: 8px;
            }

            QLineEdit#searchInput {
                background: #11111b;
                border: 1px solid #313244;
                border-radius: 8px;
                padding: 6px 12px;
                font-size: 13px;
            }
            QLineEdit#searchInput:focus { border-color: #89b4fa; }
        """)

    # ======================================================================
    # Build UI
    # ======================================================================

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(0)

        # -- Header --
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        t = QLabel("Diffi")
        t.setObjectName("appTitle")
        hdr.addWidget(t)
        s = QLabel("API Comparator")
        s.setObjectName("appSub")
        hdr.addWidget(s)
        hdr.addStretch()

        ic = QPushButton("\u2b07")
        ic.setObjectName("iconBtn")
        ic.setFixedSize(34, 34)
        ic.setToolTip("Import Configuration (Ctrl+O)")
        ic.clicked.connect(self._on_import_config)
        hdr.addWidget(ic)
        ec = QPushButton("\u2b06")
        ec.setObjectName("iconBtn")
        ec.setFixedSize(34, 34)
        ec.setToolTip("Export Configuration (Ctrl+Shift+S)")
        ec.clicked.connect(self._on_export_config)
        hdr.addWidget(ec)
        hdr.addSpacing(4)
        envl = QLabel("Environment")
        envl.setObjectName("fieldLabel")
        hdr.addWidget(envl)
        self._profile_combo = QComboBox()
        self._profile_combo.setFixedWidth(130)
        self._profile_combo.currentTextChanged.connect(self._on_profile_selected)
        hdr.addWidget(self._profile_combo)
        sp = QPushButton("\u2713")
        sp.setObjectName("removeBtn")
        sp.setFixedSize(30, 30)
        sp.setToolTip("Save profile")
        sp.clicked.connect(self._on_save_profile)
        hdr.addWidget(sp)
        dp = QPushButton("\u00d7")
        dp.setObjectName("removeBtn")
        dp.setFixedSize(30, 30)
        dp.setToolTip("Delete profile")
        dp.clicked.connect(self._on_delete_profile)
        hdr.addWidget(dp)
        layout.addLayout(hdr)
        layout.addSpacing(20)

        # -- Mode dropdown --
        mrow = QHBoxLayout()
        mrow.setSpacing(8)
        ml = QLabel("Mode")
        ml.setObjectName("fieldLabel")
        mrow.addWidget(ml)
        self._mode_combo = QComboBox()
        self._mode_combo.setFixedWidth(200)
        self._mode_combo.setFixedHeight(34)
        self._mode_combo.addItems(["Compare (Old vs New)", "Single API"])
        self._mode_combo.currentIndexChanged.connect(
            lambda i: self._set_mode("compare" if i == 0 else "single")
        )
        mrow.addWidget(self._mode_combo)
        mrow.addStretch()
        layout.addLayout(mrow)
        layout.addSpacing(16)

        # -- Content splitter --
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Top config
        cfg = QWidget()
        cfg.setObjectName("central")
        cl = QVBoxLayout(cfg)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(12)

        self._api_tabs = QTabWidget()
        self._old_form = ApiFormWidget("Old API")
        self._new_form = ApiFormWidget("New API")
        self._single_form = ApiFormWidget("API")
        self._api_tabs.addTab(self._old_form, "Old API")
        self._api_tabs.addTab(self._new_form, "New API")
        cl.addWidget(self._api_tabs)

        # IDs card
        ids_card = QFrame()
        ids_card.setObjectName("card")
        il = QVBoxLayout(ids_card)
        il.setContentsMargins(20, 14, 20, 14)
        il.setSpacing(10)

        r1 = QHBoxLayout()
        r1.setSpacing(8)
        self._ids_input = QLineEdit()
        self._ids_input.setPlaceholderText("e.g. 1, 2, 3, 5, 10")
        self._ids_input.textChanged.connect(self._mark_unsaved)
        r1.addWidget(self._ids_input, 1)
        imb = QPushButton("\u2b07")
        imb.setObjectName("iconBtn")
        imb.setFixedSize(34, 34)
        imb.setToolTip("Import IDs from File")
        imb.clicked.connect(self._on_import_ids)
        r1.addWidget(imb)
        il.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(20)
        r2.addWidget(QLabel("Threads"))
        self._concurrency_spin = QSpinBox()
        self._concurrency_spin.setRange(1, 20)
        self._concurrency_spin.setValue(1)
        self._concurrency_spin.setFixedWidth(52)
        r2.addWidget(self._concurrency_spin)
        r2.addSpacing(4)
        il2 = QLabel("Ignore Fields")
        il2.setObjectName("fieldLabel")
        r2.addWidget(il2)
        self._ignore_input = QLineEdit()
        self._ignore_input.setPlaceholderText(
            "timestamp, user.*, re:^_internal"
        )
        self._ignore_input.setToolTip(
            "Comma-separated field paths to exclude.\n"
            "Supports prefixes ('user' ignores 'user.name'),\n"
            "globs ('user.*'), and regex ('re:^_internal')."
        )
        r2.addWidget(self._ignore_input, 1)
        il.addLayout(r2)

        # Advanced options row
        r3 = QHBoxLayout()
        r3.setSpacing(16)
        self._ssl_check = QCheckBox("Skip SSL verification")
        self._ssl_check.setChecked(False)
        r3.addWidget(self._ssl_check)
        r3.addSpacing(4)
        r3.addWidget(QLabel("Proxy"))
        self._proxy_input = QLineEdit()
        self._proxy_input.setPlaceholderText("http://proxy:8080")
        self._proxy_input.setFixedWidth(200)
        r3.addWidget(self._proxy_input)
        r3.addSpacing(4)
        r3.addWidget(QLabel("Rate limit (s)"))
        self._rate_spin = QSpinBox()
        self._rate_spin.setRange(0, 60)
        self._rate_spin.setValue(0)
        self._rate_spin.setFixedWidth(52)
        self._rate_spin.setToolTip("Minimum seconds between requests (0 = none)")
        r3.addWidget(self._rate_spin)
        r3.addStretch()
        il.addLayout(r3)
        cl.addWidget(ids_card)

        # Fetch / Cancel
        fr = QHBoxLayout()
        fr.setSpacing(12)
        self._fetch_btn = QPushButton("\u25b6  Run Comparison")
        self._fetch_btn.setObjectName("greenBtn")
        self._fetch_btn.setFixedHeight(40)
        self._fetch_btn.setFixedWidth(180)
        self._fetch_btn.clicked.connect(self._on_fetch)
        self._cancel_btn = QPushButton("\u2716  Cancel")
        self._cancel_btn.setObjectName("cancelBtn")
        self._cancel_btn.setFixedHeight(40)
        self._cancel_btn.setFixedWidth(120)
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setMaximum(0)
        self._progress.setFixedWidth(160)
        self._progress.setFixedHeight(4)
        fr.addWidget(self._fetch_btn)
        fr.addWidget(self._cancel_btn)
        fr.addWidget(self._progress)
        fr.addStretch()
        cl.addLayout(fr)

        splitter.addWidget(cfg)

        # Results
        res = QWidget()
        res.setObjectName("central")
        rl = QVBoxLayout(res)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)

        # Search bar (#3)
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self._search_input = QLineEdit()
        self._search_input.setObjectName("searchInput")
        self._search_input.setPlaceholderText(
            "\U0001f50d  Filter results by ID or field path..."
        )
        self._search_input.setFixedWidth(300)
        self._search_input.textChanged.connect(self._on_search_changed)
        search_row.addWidget(self._search_input)
        search_row.addStretch()

        rlbl = QHBoxLayout()
        rlbl.setSpacing(8)
        rt = QLabel("Results")
        rt.setObjectName("sectionTitle")
        rlbl.addWidget(rt)
        rlbl.addStretch()
        self._mapping_btn = QPushButton("Field Mappings")
        self._mapping_btn.setVisible(False)
        self._mapping_btn.clicked.connect(self._on_configure_mappings)
        rlbl.addWidget(self._mapping_btn)
        hb = QPushButton("\u21bb")
        hb.setObjectName("iconBtn")
        hb.setFixedSize(34, 34)
        hb.setToolTip("Toggle History")
        hb.clicked.connect(
            lambda: self._history_dock.setVisible(
                not self._history_dock.isVisible()
            )
        )
        rlbl.addWidget(hb)
        ej = QPushButton("{}")
        ej.setObjectName("iconBtn")
        ej.setFixedSize(34, 34)
        ej.setToolTip("Export as JSON")
        ej.clicked.connect(lambda: self._on_export_results("json"))
        rlbl.addWidget(ej)
        ec2 = QPushButton("\u2630")
        ec2.setObjectName("iconBtn")
        ec2.setFixedSize(34, 34)
        ec2.setToolTip("Export as CSV")
        ec2.clicked.connect(lambda: self._on_export_results("csv"))
        rlbl.addWidget(ec2)
        em = QPushButton("\u270e")
        em.setObjectName("iconBtn")
        em.setFixedSize(34, 34)
        em.setToolTip("Export as Markdown")
        em.clicked.connect(lambda: self._on_export_results("markdown"))
        rlbl.addWidget(em)

        # cURL export button (#13)
        curl_btn = QPushButton("\u2318")
        curl_btn.setObjectName("iconBtn")
        curl_btn.setFixedSize(34, 34)
        curl_btn.setToolTip("Copy as cURL command")
        curl_btn.clicked.connect(self._on_export_curl)
        rlbl.addWidget(curl_btn)

        rl.addLayout(search_row)
        rl.addLayout(rlbl)

        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        self._error_label.setObjectName("errorBox")
        rl.addWidget(self._error_label)

        self._results_scroll = QScrollArea()
        self._results_scroll.setWidgetResizable(True)
        self._results_scroll.setStyleSheet(
            "border: none; background: transparent;"
        )
        self._results_content = QWidget()
        self._results_content.setObjectName("central")
        self._results_layout_inner = QVBoxLayout(self._results_content)
        self._results_layout_inner.setSpacing(8)
        self._results_scroll.setWidget(self._results_content)
        rl.addWidget(self._results_scroll)

        # empty state placeholder (#9)
        self._empty_label = QLabel(
            "Configure two API endpoints above and click "
            "\u25b6 Run Comparison to see differences."
        )
        self._empty_label.setObjectName("emptyState")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._results_layout_inner.addWidget(self._empty_label)

        splitter.addWidget(res)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        # History dock
        self._history_dock = QDockWidget("History", self)
        self._history_dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        hc = QWidget()
        hc.setObjectName("card")
        hl = QVBoxLayout(hc)
        hl.setContentsMargins(12, 12, 12, 12)
        hl.setSpacing(8)
        self._history_list = QListWidget()
        self._history_list.itemDoubleClicked.connect(
            self._on_history_selected
        )
        hl.addWidget(self._history_list, 1)
        chb = QPushButton("Clear History")
        chb.setObjectName("linkBtn")
        chb.clicked.connect(self._on_clear_history)
        hl.addWidget(chb)
        self._history_dock.setWidget(hc)
        self.addDockWidget(
            Qt.DockWidgetArea.RightDockWidgetArea, self._history_dock
        )
        self._history_dock.setVisible(False)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready")

    # ======================================================================
    # Keyboard shortcuts (#2)
    # ======================================================================

    def _setup_shortcuts(self) -> None:
        def _bind(key: str, slot: Any) -> None:
            action = QAction(self)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(slot)
            self.addAction(action)

        _bind("Ctrl+Return", self._on_fetch)
        _bind("Ctrl+Enter", self._on_fetch)
        _bind("Ctrl+O", self._on_import_config)
        _bind("Ctrl+S", self._on_export_config)
        _bind("Ctrl+Shift+S", self._on_export_config)
        _bind("Ctrl+F", lambda: self._search_input.setFocus())
        _bind("Escape", self._on_cancel)

    # ======================================================================
    # Mode
    # ======================================================================

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        idx = 0 if mode == "compare" else 1
        if self._mode_combo.currentIndex() != idx:
            self._mode_combo.blockSignals(True)
            self._mode_combo.setCurrentIndex(idx)
            self._mode_combo.blockSignals(False)
        self._api_tabs.clear()
        if mode == "compare":
            self._api_tabs.addTab(self._old_form, "Old API")
            self._api_tabs.addTab(self._new_form, "New API")
            self._fetch_btn.setText("\u25b6  Run Comparison")
        else:
            self._api_tabs.addTab(self._single_form, "API")
            self._fetch_btn.setText("\u25b6  Fetch Data")
        self._mark_unsaved()

    # ======================================================================
    # Config import / export
    # ======================================================================

    def _on_import_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Configuration", "", "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text())
        except (json.JSONDecodeError, OSError) as e:
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
        self._unsaved_changes = False
        self._update_title()

    def _on_export_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Configuration",
            "diffi_config.json",
            "JSON Files (*.json)",
        )
        if not path:
            return
        data: dict[str, Any] = {"mode": self._mode}
        if self._mode == "compare":
            data["old_api"] = self._old_form.get_config()
            data["new_api"] = self._new_form.get_config()
        else:
            data["api"] = self._single_form.get_config()
        data["ids"] = _parse_ids(self._ids_input.text())
        data["field_mappings"] = self._field_mappings
        data["ignore_fields"] = self._ignore_input.text()
        data["concurrency"] = self._concurrency_spin.value()
        try:
            Path(path).write_text(json.dumps(data, indent=2))
        except OSError as e:
            QMessageBox.warning(self, "Export Error", str(e))

    # ======================================================================
    # Export results
    # ======================================================================

    def _on_export_results(self, fmt: str) -> None:
        if not self._results:
            QMessageBox.information(
                self, "No Results", "Run a comparison first."
            )
            return
        ext = {"json": "json", "csv": "csv", "markdown": "md"}[fmt]
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Results",
            f"diffi_results.{ext}",
            f"Files (*.{ext})",
        )
        if not path:
            return
        try:
            if fmt == "json":
                content = json.dumps(self._results, indent=2, default=str)
            elif fmt == "csv":
                content = self._to_csv()
            else:
                content = self._to_md()
            Path(path).write_text(content)
        except OSError as e:
            QMessageBox.warning(self, "Export Error", str(e))

    def _to_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "id", "status", "error",
            "missing_fields", "extra_fields", "type_changes",
            "rt_old", "rt_new", "sc_old", "sc_new",
            "size_old", "size_new",
        ])
        for r in self._results:
            rt = r.get("responseTimes", {})
            sc = r.get("statusCodes", {})
            sz = r.get("responseSizes", {})
            st = (
                "error"
                if r.get("error")
                else (
                    "changed"
                    if (
                        r.get("missing")
                        or r.get("extra")
                        or r.get("typeChanges")
                    )
                    else "identical"
                )
            )
            w.writerow([
                r.get("id", ""),
                st,
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
                sz.get("old", sz.get("single", "")),
                sz.get("new", ""),
            ])
        return buf.getvalue()

    def _to_md(self) -> str:
        L = [
            "# Diffi Comparison Results",
            f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"**Mode:** {self._mode.title()}",
        ]
        t = len(self._results)
        e = sum(1 for r in self._results if r.get("error"))
        L += [
            f"**Total IDs:** {t}",
            f"**Successful:** {t - e}",
            f"**Errors:** {e}",
            "",
        ]
        if self._mode == "compare":
            am: dict[str, list[str]] = {}
            ae: dict[str, list[str]] = {}
            at: dict[str, str] = {}
            for r in self._results:
                if r.get("error"):
                    continue
                for i in r.get("missing", []):
                    am.setdefault(i["path"], []).append(r["id"])
                for i in r.get("extra", []):
                    ae.setdefault(i["path"], []).append(r["id"])
                for i in r.get("typeChanges", []):
                    at.setdefault(
                        f"{i['path']}: {i['oldType']} \u2192 {i['newType']}",
                        [],
                    ).append(r["id"])
            for title, d in [
                ("Missing Fields", am),
                ("Extra Fields", ae),
                ("Type Changes", at),
            ]:
                if d:
                    L += [
                        f"## {title}",
                        "| Field | Affected IDs |",
                        "|-------|-------------|",
                    ] + [
                        f"| `{k}` | {', '.join(v)} |"
                        for k, v in d.items()
                    ] + [""]
        L.append("## Per-ID Results")
        for r in self._results:
            rid = r.get("id", "?")
            if r.get("error"):
                L += [
                    f"### ID {rid} \u2014 Error",
                    f"> {r['error']}",
                    "",
                ]
            else:
                rt = r.get("responseTimes", {})
                sc = r.get("statusCodes", {})
                rs = ""
                if "old" in rt and "new" in rt:
                    rs += (
                        f" | Time: {rt['old']:.3f}s "
                        f"\u2192 {rt['new']:.3f}s"
                    )
                elif "single" in rt:
                    rs += f" | Time: {rt['single']:.3f}s"
                ss = ""
                if "old" in sc and "new" in sc:
                    ss += (
                        f" | Status: {sc['old']} \u2192 {sc['new']}"
                    )
                elif "single" in sc:
                    ss += f" | Status: {sc['single']}"
                L.append(f"### ID {rid}{rs}{ss}")
                for title, items in [
                    ("Missing", r.get("missing", [])),
                    ("Extra", r.get("extra", [])),
                    (
                        "Type changes",
                        r.get("typeChanges", []),
                    ),
                ]:
                    if items:
                        L.append(
                            f"- {title}: "
                            f"{', '.join(i['path'] for i in items)}"
                        )
                if (
                    not r.get("missing")
                    and not r.get("extra")
                    and not r.get("typeChanges")
                ):
                    L.append("- Identical")
                L.append("")
        return "\n".join(L)

    # ======================================================================
    # cURL export (#13)
    # ======================================================================

    def _on_export_curl(self) -> None:
        config = (
            self._old_form.get_config()
            if self._mode == "compare"
            else self._single_form.get_config()
        )
        url = config["url"].replace("{{id}}", "ID")
        method = config.get("method", "GET")
        parts = [f"curl -X {method} '{url}'"]
        for k, v in config.get("headers", {}).items():
            parts.append(f"  -H '{k}: {v}'")
        auth = config.get("auth", {})
        if auth.get("type") == "bearer":
            parts.append(f"  -H 'Authorization: Bearer {auth.get('token', '')}'")
        elif auth.get("type") == "basic":
            parts.append(f"  -u '{auth.get('username', '')}:{auth.get('password', '')}'")
        body = config.get("body", "")
        if body and method in ("POST", "PUT", "PATCH"):
            parts.append(f"  -d '{body.replace(chr(39), chr(39) + chr(92) + chr(39))}'")
        cmd = " \\\n".join(parts)
        cb = QApplication.clipboard()
        if cb:
            cb.setText(cmd)
        self._status_bar.showMessage("cURL command copied to clipboard", 3000)

    # ======================================================================
    # Bulk ID import
    # ======================================================================

    def _on_import_ids(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import IDs", "", "Text/CSV (*.txt *.csv);;All (*)"
        )
        if not path:
            return
        try:
            content = Path(path).read_text()
        except OSError as e:
            QMessageBox.warning(self, "Import Error", str(e))
            return
        ids: list[str] = []
        if path.endswith(".csv"):
            for row in csv.reader(io.StringIO(content)):
                if row:
                    v = row[0].strip().strip('"').strip("'")
                    if v:
                        ids.append(v)
        else:
            for line in content.splitlines():
                v = line.strip().strip(",").strip(";")
                if v:
                    ids.append(v)
        if ids:
            ex = self._ids_input.text().strip()
            self._ids_input.setText(
                (ex + ", " + ", ".join(ids)) if ex else ", ".join(ids)
            )

    # ======================================================================
    # Fetch / Cancel
    # ======================================================================

    def _on_fetch(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self._clear_results()
        self._error_label.setVisible(False)
        ids = _parse_ids(self._ids_input.text())
        if not ids:
            self._show_error("Provide at least one ID")
            return
        if self._mode == "compare":
            configs = [
                self._old_form.get_config(),
                self._new_form.get_config(),
            ]
            if not configs[0]["url"] or not configs[1]["url"]:
                self._show_error("Both old and new API URLs are required")
                return
        else:
            configs = [self._single_form.get_config()]
            if not configs[0]["url"]:
                self._show_error("API URL is required")
                return

        ig = {
            s.strip()
            for s in re.split(r"[,]+", self._ignore_input.text())
            if s.strip()
        }
        self._fetch_btn.setEnabled(False)
        self._fetch_btn.setVisible(False)
        self._cancel_btn.setVisible(True)
        self._progress.setVisible(True)
        self._status_bar.showMessage("Running...")

        self._worker = ApiWorker(
            configs,
            ids,
            self._field_mappings,
            ignore_fields=ig,
            concurrency=self._concurrency_spin.value(),
            verify_ssl=not self._ssl_check.isChecked(),
            proxy=self._proxy_input.text().strip() or None,
            rate_limit=float(self._rate_spin.value()),
        )
        self._worker.finished.connect(self._on_results)
        self._worker.start()

    def _on_cancel(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._status_bar.showMessage("Cancelling...")
            QTimer.singleShot(3000, self._reset_fetch_ui)

    def _reset_fetch_ui(self) -> None:
        self._fetch_btn.setEnabled(True)
        self._fetch_btn.setVisible(True)
        self._cancel_btn.setVisible(False)
        self._progress.setVisible(False)

    # ======================================================================
    # Results
    # ======================================================================

    def _on_results(self, results: list[dict]) -> None:
        self._results = results
        self._reset_fetch_ui()
        self._save_to_history(results)
        self._status_bar.showMessage(
            f"Completed \u2014 {len(results)} ID(s) processed", 5000
        )
        total = len(results)
        success = sum(1 for r in results if not r.get("error"))
        ec = total - success
        am: list[dict] = []
        ae: list[dict] = []
        atc: list[dict] = []
        if self._mode == "compare":
            _am: dict[str, dict] = {}
            _ae: dict[str, dict] = {}
            _at: dict[str, dict] = {}
            for r in results:
                if r.get("error"):
                    continue
                for i in r.get("missing", []):
                    p = i["path"]
                    if p not in _am:
                        _am[p] = {"path": p, "value": i["value"], "ids": []}
                    _am[p]["ids"].append(r["id"])
                for i in r.get("extra", []):
                    p = i["path"]
                    if p not in _ae:
                        _ae[p] = {"path": p, "value": i["value"], "ids": []}
                    _ae[p]["ids"].append(r["id"])
                for i in r.get("typeChanges", []):
                    p = i["path"]
                    if p not in _at:
                        _at[p] = {**i, "ids": []}
                    _at[p]["ids"].append(r["id"])
            am = list(_am.values())
            ae = list(_ae.values())
            atc = list(_at.values())
        has = bool(am or ae or atc)

        # Summary card
        sc = QFrame()
        sc.setObjectName("card")
        sl = QVBoxLayout(sc)
        sl.setContentsMargins(20, 16, 20, 16)
        sl.setSpacing(10)
        srow = QHBoxLayout()
        srow.setSpacing(32)
        for lbl, val, col in [
            ("Total IDs", str(total), "#cdd6f4"),
            ("Successful", str(success), "#a6e3a1"),
            ("Errors", str(ec), "#f38ba8"),
            (
                "Status",
                "Changes Detected" if has else "All Identical",
                "#f9e2af" if has else "#a6e3a1",
            ),
        ]:
            c = QVBoxLayout()
            c.setSpacing(0)
            v = QLabel(val)
            v.setStyleSheet(
                f"font-size: 24px; font-weight: 700; "
                f"color: {col}; background: transparent;"
            )
            l = QLabel(lbl)
            l.setStyleSheet(
                "font-size: 11px; color: #6c7086; background: transparent;"
            )
            c.addWidget(v)
            c.addWidget(l)
            srow.addLayout(c)
        srow.addStretch()
        sl.addLayout(srow)

        all_rt = [r.get("responseTimes", {}) for r in results if not r.get("error")]
        if all_rt and "old" in all_rt[0]:
            ot = [t["old"] for t in all_rt if "old" in t]
            nt = [t["new"] for t in all_rt if "new" in t]
            if ot and nt:
                rl = QLabel(
                    f"Average Response Time  \u2014  "
                    f"Old: {sum(ot) / len(ot):.3f}s    "
                    f"New: {sum(nt) / len(nt):.3f}s"
                )
                rl.setStyleSheet(
                    "font-size: 12px; color: #6c7086; background: transparent;"
                )
                sl.addWidget(rl)

        # response size summary
        all_sz = [r.get("responseSizes", {}) for r in results if not r.get("error")]
        if all_sz and "old" in all_sz[0]:
            os_ = [s["old"] for s in all_sz if "old" in s]
            ns = [s["new"] for s in all_sz if "new" in s]
            if os_ and ns:
                from .result_card import _fmt_size
                sl2 = QLabel(
                    f"Average Response Size  \u2014  "
                    f"Old: {_fmt_size(int(sum(os_) / len(os_)))}    "
                    f"New: {_fmt_size(int(sum(ns) / len(ns)))}"
                )
                sl2.setStyleSheet(
                    "font-size: 12px; color: #6c7086; background: transparent;"
                )
                sl.addWidget(sl2)

        scr = [
            r
            for r in results
            if not r.get("error") and r.get("statusCodeDiff")
        ]
        if scr:
            scl = QLabel(
                f"\u26a0  {len(scr)} ID(s) returned different status codes"
            )
            scl.setStyleSheet(
                "color: #f38ba8; font-weight: 600; font-size: 12px; "
                "background: transparent;"
            )
            sl.addWidget(scl)

        if has and self._mode == "compare":
            mb = QPushButton(
                f"Configure Field Mappings ({len(self._field_mappings)})"
                if self._field_mappings
                else "Configure Field Mappings"
            )
            mb.clicked.connect(self._on_configure_mappings)
            sl.addWidget(mb)

        for title, items, col in [
            ("Missing Fields", am, "#f38ba8"),
            ("Extra Fields", ae, "#f9e2af"),
            ("Type Changes", atc, "#89b4fa"),
        ]:
            if items:
                l = QLabel(f"{title} \u2014 {len(items)}")
                l.setStyleSheet(
                    f"font-weight: 600; font-size: 12px; color: {col}; "
                    f"background: transparent; margin-top: 4px;"
                )
                sl.addWidget(l)
                for i in items:
                    sl.addWidget(
                        QLabel(
                            f"  {i['path']} \u2192 IDs: "
                            f"{', '.join(str(x) for x in i['ids'])}"
                        )
                    )
        self._results_layout_inner.addWidget(sc)

        # Per-ID
        pl = QLabel("Per-ID Breakdown")
        pl.setStyleSheet(
            "font-size: 14px; font-weight: 600; color: #6c7086; "
            "background: transparent; margin-top: 12px;"
        )
        self._results_layout_inner.addWidget(pl)
        self._result_cards: list[ResultCard] = []
        for r in results:
            card = ResultCard(r)
            self._result_cards.append(card)
            self._results_layout_inner.addWidget(card)

        self._results_layout_inner.addStretch()
        self._update_empty_state()

    def _show_error(self, msg: str) -> None:
        self._error_label.setText(msg)
        self._error_label.setVisible(True)

    def _clear_results(self) -> None:
        while self._results_layout_inner.count():
            it = self._results_layout_inner.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._result_cards = []
        self._results = None
        self._empty_label = None

    # ======================================================================
    # Search / Filter (#3)
    # ======================================================================

    def _on_search_changed(self, text: str) -> None:
        query = text.strip().lower()
        if not hasattr(self, "_result_cards"):
            return
        for card in self._result_cards:
            if not query:
                card.setVisible(True)
                continue
            r = card._result
            searchable = json.dumps(r, default=str).lower()
            card.setVisible(query in searchable)

    # ======================================================================
    # Field Mappings
    # ======================================================================

    def _on_configure_mappings(self) -> None:
        if not self._results:
            return
        m, e = [], []
        for r in self._results:
            if r.get("error"):
                continue
            m.extend(r.get("missing", []))
            e.extend(r.get("extra", []))
        d = FieldMappingDialog(self._field_mappings, m, e)
        d.applied.connect(self._on_mappings_applied)
        d.show()

    def _on_mappings_applied(self, mappings: list) -> None:
        self._field_mappings = mappings
        self._on_fetch()

    # ======================================================================
    # History
    # ======================================================================

    def _save_to_history(self, results: list[dict]) -> None:
        entry: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "mode": self._mode,
            "ids": _parse_ids(self._ids_input.text()),
            "config": {
                "old_api": (
                    self._old_form.get_config()
                    if self._mode == "compare"
                    else None
                ),
                "new_api": (
                    self._new_form.get_config()
                    if self._mode == "compare"
                    else None
                ),
                "api": (
                    self._single_form.get_config()
                    if self._mode != "compare"
                    else None
                ),
                "ignore_fields": self._ignore_input.text(),
                "concurrency": self._concurrency_spin.value(),
            },
            "results_summary": [],
        }
        for r in results:
            s: dict[str, Any] = {
                "id": r.get("id"),
                "error": r.get("error"),
                "missing_count": len(r.get("missing", [])),
                "extra_count": len(r.get("extra", [])),
                "type_changes_count": len(r.get("typeChanges", [])),
            }
            rt = r.get("responseTimes", {})
            if "old" in rt:
                s["response_time_old"] = rt["old"]
                s["response_time_new"] = rt["new"]
            elif "single" in rt:
                s["response_time"] = rt["single"]
            sc = r.get("statusCodes", {})
            if "old" in sc:
                s["status_code_old"] = sc["old"]
                s["status_code_new"] = sc["new"]
            elif "single" in sc:
                s["status_code"] = sc["single"]
            entry["results_summary"].append(s)
        self._history.insert(0, entry)
        self._history = self._history[:MAX_HISTORY]
        save_json(HISTORY_FILE, self._history)
        self._refresh_history_list()

    def _refresh_history_list(self) -> None:
        self._history_list.clear()
        for e in self._history:
            ts = e.get("timestamp", "?")
            try:
                ts = datetime.fromisoformat(ts).strftime("%b %d, %H:%M")
            except ValueError:
                pass
            self._history_list.addItem(
                f"{ts}  \u00b7  {e.get('mode', '?')}  \u00b7  "
                f"{len(e.get('ids', []))} IDs"
            )

    def _on_history_selected(self, item: Any) -> None:
        row = self._history_list.row(item)
        if row < 0 or row >= len(self._history):
            return
        e = self._history[row]
        self._ids_input.setText(", ".join(str(i) for i in e.get("ids", [])))
        mode = e.get("mode", "compare")
        self._set_mode(mode)
        # restore full config from history (#5)
        cfg = e.get("config", {})
        if cfg:
            if mode == "compare":
                if cfg.get("old_api"):
                    self._old_form.set_config(cfg["old_api"])
                if cfg.get("new_api"):
                    self._new_form.set_config(cfg["new_api"])
            else:
                if cfg.get("api"):
                    self._single_form.set_config(cfg["api"])
            if cfg.get("ignore_fields"):
                self._ignore_input.setText(cfg["ignore_fields"])
            if cfg.get("concurrency"):
                self._concurrency_spin.setValue(cfg["concurrency"])
        self._status_bar.showMessage(
            f"Restored run from {e.get('timestamp', '?')}", 3000
        )

    def _on_clear_history(self) -> None:
        # confirmation dialog (#6)
        if self._history:
            reply = QMessageBox.question(
                self,
                "Clear History",
                f"Delete all {len(self._history)} history entries?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._history = []
        save_json(HISTORY_FILE, self._history)
        self._refresh_history_list()

    # ======================================================================
    # Profiles
    # ======================================================================

    def _refresh_profile_combo(self) -> None:
        self._profile_combo.clear()
        self._profile_combo.addItem("")
        for n in sorted(self._profiles.keys()):
            self._profile_combo.addItem(n)

    def _on_profile_selected(self, name: str) -> None:
        if not name or name not in self._profiles:
            return
        p = self._profiles[name]
        m = p.get("mode", "compare")
        self._set_mode(m)
        if m == "compare":
            if "old_api" in p:
                self._old_form.set_config(p["old_api"])
            if "new_api" in p:
                self._new_form.set_config(p["new_api"])
        else:
            if "api" in p:
                self._single_form.set_config(p["api"])
        self._ids_input.setText(", ".join(p.get("ids", [])))
        self._field_mappings = p.get("field_mappings", [])
        self._ignore_input.setText(p.get("ignore_fields", ""))
        self._concurrency_spin.setValue(p.get("concurrency", 1))
        self._unsaved_changes = False
        self._update_title()

    def _on_save_profile(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(
            self, "Save Profile", "Profile name:"
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        p: dict[str, Any] = {"mode": self._mode}
        if self._mode == "compare":
            p["old_api"] = self._old_form.get_config()
            p["new_api"] = self._new_form.get_config()
        else:
            p["api"] = self._single_form.get_config()
        p["ids"] = _parse_ids(self._ids_input.text())
        p["field_mappings"] = self._field_mappings
        p["ignore_fields"] = self._ignore_input.text()
        p["concurrency"] = self._concurrency_spin.value()
        self._profiles[name] = p
        save_json(PROFILES_FILE, self._profiles)
        self._refresh_profile_combo()
        self._profile_combo.setCurrentText(name)
        self._unsaved_changes = False
        self._update_title()

    def _on_delete_profile(self) -> None:
        name = self._profile_combo.currentText()
        if not name or name not in self._profiles:
            return
        if (
            QMessageBox.question(
                self,
                "Delete Profile",
                f"Delete '{name}'?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            del self._profiles[name]
            save_json(PROFILES_FILE, self._profiles)
            self._refresh_profile_combo()

    # ======================================================================
    # Unsaved changes tracking (#7)
    # ======================================================================

    def _mark_unsaved(self) -> None:
        if not self._unsaved_changes:
            self._unsaved_changes = True
            self._update_title()

    def _update_title(self) -> None:
        base = "Diffi \u2014 API Comparator"
        if self._unsaved_changes:
            self.setWindowTitle(f"*{base}")
        else:
            self.setWindowTitle(base)

    # ======================================================================
    # Empty state (#9)
    # ======================================================================

    def _update_empty_state(self) -> None:
        has_results = bool(self._result_cards) if hasattr(self, "_result_cards") else False
        if hasattr(self, "_empty_label") and self._empty_label is not None:
            self._empty_label.setVisible(not has_results)


# ============================================================================
# Helpers
# ============================================================================


def _parse_ids(text: str) -> list[str]:
    """Split comma/semicolon/space-separated IDs from a text field."""
    return [s.strip() for s in re.split(r"[,;\s]+", text) if s.strip()]


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = DiffiWindow()
    window.show()
    sys.exit(app.exec())
