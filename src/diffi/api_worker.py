"""Background worker that calls APIs and compares responses."""
from __future__ import annotations

import base64
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests
from PySide6.QtCore import Signal, QThread

from .comparator import apply_field_mappings, collect_fields, deep_compare
from .utils import TIMEOUT


class ApiWorker(QThread):
    """Fetches data from one or two APIs for every ID, then compares."""

    finished = Signal(object)
    error_signal = Signal(str, str)

    # -- class-level OAuth2 token cache (keyed by (token_url, client_id)) --------
    def __init__(
        self,
        configs: list[dict],
        ids: list[str],
        mappings: list[dict],
        ignore_fields: set[str] | None = None,
        concurrency: int = 1,
        max_retries: int = 2,
        retry_delay: float = 1.0,
        verify_ssl: bool = True,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.configs = configs
        self.ids = ids
        self.mappings = mappings
        self.ignore_fields: set[str] = ignore_fields or set()
        self.concurrency: int = max(1, concurrency)
        self.max_retries: int = max_retries
        self.retry_delay: float = retry_delay
        self.verify_ssl: bool = verify_ssl
        self._cancel_event = threading.Event()

    # -- public ----------------------------------------------------------------

    def cancel(self) -> None:
        """Request the worker to stop as soon as possible."""
        self._cancel_event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def run(self) -> None:  # noqa: C901
        results: list[dict] = []
        if self.concurrency > 1:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                futures = {
                    pool.submit(self._process_id, id_): id_ for id_ in self.ids
                }
                for future in as_completed(futures):
                    if self._cancel_event.is_set():
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
                    results.append(future.result())
        else:
            for id_ in self.ids:
                if self._cancel_event.is_set():
                    break
                results.append(self._process_id(id_))
        id_order = {id_: i for i, id_ in enumerate(self.ids)}
        results.sort(key=lambda r: id_order.get(r["id"], 0))
        self.finished.emit(results)

    # -- per-id processing -----------------------------------------------------

    def _process_id(self, id_: str) -> dict[str, Any]:  # noqa: C901
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

    # -- HTTP ------------------------------------------------------------------

    def _call_api(  # noqa: C901
        self, config: dict[str, Any], id_: str
    ) -> tuple[Any, float, int]:
        """Return (parsed_json, elapsed_seconds, status_code)."""
        url = _render_template(config["url"], id_)
        method = config.get("method", "GET").upper()

        full_url = self._build_url(url, config.get("params", {}), id_)
        headers = dict(config.get("headers", {}))
        auth_config = config.get("auth", {"type": "none"})
        self._apply_auth(headers, auth_config, id_)

        body: str | None = None
        if method in ("POST", "PUT", "PATCH") and config.get("body"):
            body = _render_template(config["body"], id_)
            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"

        last_exc: Exception | None = None
        for attempt in range(1 + self.max_retries):
            if self._cancel_event.is_set():
                raise ValueError("Operation cancelled")
            start = time.time()
            try:
                res = requests.request(
                    method,
                    full_url,
                    headers=headers,
                    data=body,
                    timeout=TIMEOUT,
                    verify=self.verify_ssl,
                )
                elapsed = round(time.time() - start, 3)
                if not res.ok:
                    raise ValueError(f"HTTP {res.status_code}: {res.reason}")
                try:
                    parsed = res.json()
                except (ValueError, requests.exceptions.JSONDecodeError) as je:
                    raise ValueError(
                        f"Response is not valid JSON (HTTP {res.status_code}): "
                        f"{res.text[:200]}"
                    ) from je
                return parsed, elapsed, res.status_code
            except requests.exceptions.Timeout:
                last_exc = ValueError(
                    f"Request timed out after {TIMEOUT}s: {method} {full_url}"
                )
            except requests.exceptions.SSLError as e:
                raise ValueError(
                    f"SSL error: {e}\n\nTip: enable 'Skip SSL verification' "
                    f"in advanced options if the server uses a self-signed cert."
                ) from e
            except requests.exceptions.ConnectionError:
                last_exc = ValueError(
                    f"Network error: {method} {full_url}\n\n"
                    f"Possible causes:\n"
                    f"\u2022 The server is unreachable or DNS resolution failed\n"
                    f"\u2022 The URL is incorrect or missing a protocol\n"
                    f"\u2022 A firewall is blocking the connection"
                )
            except ValueError:
                raise
            except requests.RequestException as e:
                last_exc = ValueError(f"Request failed: {e}")
            if attempt < self.max_retries:
                time.sleep(self.retry_delay * (2 ** attempt))
        raise last_exc  # type: ignore[misc]

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _build_url(url: str, params: dict[str, str], id_: str) -> str:
        from urllib.parse import urlencode, urlparse, urlunparse

        parsed = list(urlparse(url))
        if params:
            existing = parsed[4]
            qs = urlencode(
                {k: _render_template(v, id_) for k, v in params.items()}
            )
            parsed[4] = (existing + "&" + qs) if existing else qs
        return urlunparse(parsed)

    @staticmethod
    def _apply_auth(
        headers: dict[str, str], auth_config: dict[str, Any], id_: str
    ) -> None:
        auth_type = auth_config.get("type", "none")
        if auth_type == "basic":
            user = _render_template(auth_config.get("username", ""), id_)
            pwd = _render_template(auth_config.get("password", ""), id_)
            if user:
                token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
                headers["Authorization"] = f"Basic {token}"
        elif auth_type == "bearer":
            token = _render_template(auth_config.get("token", ""), id_)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "oauth2":
            token = ApiWorker._get_oauth2_token(auth_config, id_)
            if token:
                headers["Authorization"] = f"Bearer {token}"

    @staticmethod
    def _get_oauth2_token(
        config: dict[str, Any], id_: str
    ) -> str | None:
        token_url = _render_template(config.get("token_url", ""), id_)
        client_id = _render_template(config.get("client_id", ""), id_)
        client_secret = _render_template(config.get("client_secret", ""), id_)
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
            return res.json().get("access_token", "")
        except Exception:
            return None


# -- template helper ----------------------------------------------------------


def _render_template(text: str, id_: str) -> str:
    """Replace ``{{id}}`` and arbitrary ``{{name}}`` placeholders."""
    result = text.replace("{{id}}", str(id_))

    def _replacer(m: re.Match[str]) -> str:
        return id_

    result = re.sub(r"\{\{(\w+)\}\}", _replacer, result)
    return result
