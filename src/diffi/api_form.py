"""API configuration form widget."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QFrame,
)

from .utils import AUTH_TYPES, METHODS


class ApiFormWidget(QWidget):
    """A card containing URL, method, auth, headers, params, and body fields."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
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
        self.url_input.setPlaceholderText(
            "https://api.example.com/v1/{{id}}"
        )
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
        self._auth_oauth2_client_secret.setEchoMode(
            QLineEdit.EchoMode.Password
        )
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
        self.header_rows: list[dict[str, Any]] = []
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
        self.param_rows: list[dict[str, Any]] = []
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
        self.body_input.setPlaceholderText(
            '{"title": "foo", "body": "bar", "userId": {{id}} }'
        )
        self.body_input.setMaximumHeight(72)
        self.body_input.setVisible(False)
        blbl.setVisible(False)
        wl.addWidget(self.body_input)

        self.method_combo.currentTextChanged.connect(self._on_method_changed)
        root.addWidget(wrapper)

        self._add_header_row("Content-Type", "application/json")
        self._add_header_row("Accept", "application/json")

    # -- signals ---------------------------------------------------------------

    def _on_method_changed(self, method: str) -> None:
        vis = method in ("POST", "PUT", "PATCH")
        self.body_input.setVisible(vis)
        self._body_label.setVisible(vis)

    def _on_auth_changed(self, auth_type: str) -> None:
        self._auth_wrap.setVisible(auth_type != "None")
        for w in sum(self._auth_widgets.values(), []):
            w.setVisible(False)
        for w in self._auth_widgets.get(auth_type, []):
            w.setVisible(True)

    # -- header / param rows ---------------------------------------------------

    def _add_header_row(self, key: str = "", value: str = "") -> None:
        row = self._make_kv(self.header_rows, self.headers_container)
        row["key"].setText(key)
        row["value"].setText(value)

    def _add_param_row(self) -> None:
        self._make_kv(self.param_rows, self.params_container)

    def _make_kv(
        self, rows: list[dict[str, Any]], container: QVBoxLayout
    ) -> dict[str, Any]:
        w = QWidget()
        w.setObjectName("cardInner")
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(6)
        cb = QCheckBox()
        cb.setChecked(True)
        ki = QLineEdit()
        ki.setPlaceholderText("Key")
        ki.setFixedWidth(140)
        vi = QLineEdit()
        vi.setPlaceholderText("Value")
        rb = QPushButton("\u00d7")
        rb.setObjectName("removeBtn")
        rb.setFixedSize(26, 26)
        d: dict[str, Any] = {
            "widget": w,
            "enabled": cb,
            "key": ki,
            "value": vi,
        }
        rows.append(d)
        rb.clicked.connect(lambda: self._rm(rows, w))
        l.addWidget(cb)
        l.addWidget(ki)
        l.addWidget(vi, 1)
        l.addWidget(rb)
        container.addWidget(w)
        return d

    def _rm(self, rows: list[dict[str, Any]], w: QWidget) -> None:
        rows[:] = [r for r in rows if r["widget"] is not w]
        w.deleteLater()

    # -- serialisation ---------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        headers = {
            r["key"].text().strip(): r["value"].text()
            for r in self.header_rows
            if r["enabled"].isChecked() and r["key"].text().strip()
        }
        params = {
            r["key"].text().strip(): r["value"].text()
            for r in self.param_rows
            if r["enabled"].isChecked() and r["key"].text().strip()
        }
        at = self.auth_combo.currentText()
        auth: dict[str, str] = {"type": at.lower().replace(" ", "_")}
        if at == "Basic Auth":
            auth["username"] = self._auth_basic_user.text()
            auth["password"] = self._auth_basic_pwd.text()
        elif at == "Bearer Token":
            auth["token"] = self._auth_bearer_token.text()
        elif at == "OAuth2":
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

    def set_config(self, config: dict[str, Any]) -> None:
        self.url_input.setText(config.get("url", ""))
        idx = self.method_combo.findText(config.get("method", "GET"))
        if idx >= 0:
            self.method_combo.setCurrentIndex(idx)
        auth = config.get("auth", {})
        am = {
            "none": "None",
            "basic_auth": "Basic Auth",
            "bearer_token": "Bearer Token",
            "oauth2": "OAuth2",
        }
        at = am.get(auth.get("type", "none"), "None")
        idx = self.auth_combo.findText(at)
        if idx >= 0:
            self.auth_combo.setCurrentIndex(idx)
        if at == "Basic Auth":
            self._auth_basic_user.setText(auth.get("username", ""))
            self._auth_basic_pwd.setText(auth.get("password", ""))
        elif at == "Bearer Token":
            self._auth_bearer_token.setText(auth.get("token", ""))
        elif at == "OAuth2":
            self._auth_oauth2_client_id.setText(auth.get("client_id", ""))
            self._auth_oauth2_client_secret.setText(
                auth.get("client_secret", "")
            )
            self._auth_oauth2_token_url.setText(auth.get("token_url", ""))
        # restore headers
        existing_keys = {
            r["key"].text().strip() for r in self.header_rows
        }
        for k, v in config.get("headers", {}).items():
            if k and k not in existing_keys:
                self._add_header_row(k, v)
        # restore params
        for k, v in config.get("params", {}).items():
            if k:
                row = self._make_kv(self.param_rows, self.params_container)
                row["key"].setText(k)
                row["value"].setText(v)
        # restore body
        body = config.get("body", "")
        if body:
            self.body_input.setPlainText(body)
            method = config.get("method", "GET")
            if method in ("POST", "PUT", "PATCH"):
                self.body_input.setVisible(True)
                self._body_label.setVisible(True)
