"""
http_util.py - Shared HTTP/JSON transport for vendor adapters whose devices
speak HTTP directly (Appear, Haivision). Stdlib only: urllib, ssl, json.

Not a vendor adapter itself - vendor modules compose a JsonHttpClient and
translate its results into Driver methods (ping/discover/etc), adding
whatever auth scheme and endpoint knowledge is specific to that driver.
"""

from __future__ import annotations

import http.cookiejar
import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from drivers.base import DiscoveryResult, DriverError, DriverAuthError, DriverUnreachableError

DEFAULT_TIMEOUT_SECONDS = 2.0


@dataclass
class JsonHttpClient:
    host: str
    port: int = 443
    scheme: str = "https"
    verify_tls: bool = False
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    # Pre-built auth/other headers, e.g. {"Authorization": "Basic ..."} or
    # {"X-Api-Key": "..."} - vendor modules build this however that vendor
    # authenticates; this class doesn't assume a scheme.
    extra_headers: dict = field(default_factory=dict)
    # Haivision (Makito X4 1.8) uses POST /apis/authentication + a session
    # cookie, not HTTP Basic. Opt in per client so Appear/others stay stateless.
    use_cookies: bool = False

    def __post_init__(self) -> None:
        self._cookie_jar = http.cookiejar.CookieJar() if self.use_cookies else None

    def _base_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    def _ssl_context(self) -> Optional[ssl.SSLContext]:
        if self.scheme != "https":
            return None
        ctx = ssl.create_default_context()
        if not self.verify_tls:
            # Broadcast appliances commonly run self-signed certs out of the
            # box. Explicitly opted out per-device via api_verify_tls in the
            # DB, not a silent global default.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _opener(self) -> urllib.request.OpenerDirector:
        handlers: list = []
        ctx = self._ssl_context()
        if ctx is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        if self._cookie_jar is not None:
            handlers.append(urllib.request.HTTPCookieProcessor(self._cookie_jar))
        return urllib.request.build_opener(*handlers)

    def _request(self, method: str, path: str, json_body: Optional[dict] = None) -> tuple[int, str, dict]:
        url = self._base_url() + path
        headers = {"Accept": "application/json, text/plain, */*", **self.extra_headers}
        data = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(json_body).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        start = time.monotonic()
        try:
            with self._opener().open(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return resp.status, body, {"elapsed_ms": elapsed_ms, "content_type": resp.headers.get("Content-Type")}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            if exc.code in (401, 403):
                raise DriverAuthError(f"{method} {url} -> HTTP {exc.code}") from exc
            return exc.code, body, {"elapsed_ms": int((time.monotonic() - start) * 1000), "content_type": None}
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise DriverUnreachableError(f"{method} {url} -> {exc}") from exc

    def get_json(self, path: str) -> Any:
        status, body, _meta = self._request("GET", path)
        if status >= 400:
            raise DriverError(f"GET {path} -> HTTP {status}: {body[:300]}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise DriverError(f"GET {path} returned non-JSON body: {body[:300]}") from exc

    def post_json(self, path: str, body: dict) -> Any:
        status, resp_body, _meta = self._request("POST", path, json_body=body)
        if status >= 400:
            raise DriverError(f"POST {path} -> HTTP {status}: {resp_body[:300]}")
        if not resp_body:
            return None
        try:
            return json.loads(resp_body)
        except json.JSONDecodeError as exc:
            raise DriverError(f"POST {path} returned non-JSON body: {resp_body[:300]}") from exc

    def get_text(self, path: str) -> str:
        """GET a non-JSON body (Prometheus text, HTML). Raises on HTTP >= 400."""
        status, body, _meta = self._request("GET", path)
        if status >= 400:
            raise DriverError(f"GET {path} -> HTTP {status}: {body[:300]}")
        return body

    def ping(self, probe_path: str = "/") -> bool:
        """Cheapest possible reachability check: TCP+TLS handshake and one GET.
        Does NOT confirm the device's real API is usable - just that something
        answers on the configured port."""
        try:
            self._request("GET", probe_path)
            return True
        except DriverUnreachableError:
            return False
        except DriverError:
            # Reached the host and got an HTTP response, even if not 2xx -
            # that still counts as "reachable" for health-check purposes.
            return True

    def discover(self, candidates: list[str]) -> list[DiscoveryResult]:
        """Probe candidate paths and report what responds."""
        results = []
        for path in candidates:
            try:
                status, body, meta = self._request("GET", path)
                results.append(DiscoveryResult(
                    path=path,
                    status_code=status,
                    ok=status < 400,
                    content_type=meta.get("content_type"),
                    body_preview=body[:200] if body else None,
                ))
            except DriverError as exc:
                results.append(DiscoveryResult(path=path, status_code=None, ok=False, error=str(exc)))
            except DriverUnreachableError as exc:
                results.append(DiscoveryResult(path=path, status_code=None, ok=False, error=str(exc)))
                break  # host isn't reachable at all - no point trying more paths
        return results
