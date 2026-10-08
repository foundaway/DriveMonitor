"""Cliente HTTP de SOLO LECTURA para el servidor web del drive.

- Solo existe el método get(); cualquier otro verbo se rechaza aunque alguien
  llame a la sesión interna.
- Una sola conexión por drive (pool de tamaño 1) y peticiones en serie.
- Timeouts cortos, sin reintentos automáticos (el ciclo de consulta decide el
  backoff).
- Rechaza URLs de otro host, con parámetros o con nombre de acción.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.parse
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter

from . import __version__

USER_AGENT = f"DriveMonitor/{__version__} (read-only)"

# Palabras que, en el nombre de la ruta, sugieren una acción sobre el drive.
UNSAFE_WORDS = (
    "clear", "reset", "set", "write", "save", "reboot", "restart", "update",
    "upgrade", "flash", "upload", "submit", "delete", "apply", "login", "logout",
    "cmd", "command", "action", "config_post",
)


class UnsafeRequest(Exception):
    """Se intentó una petición que podría modificar el drive."""


def unsafe_reason(url: str) -> str | None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", ""):
        return f"esquema no permitido ({parts.scheme})"
    if parts.query:
        return "tiene parámetros (?...)"
    name = parts.path.lower().rsplit("/", 1)[-1]
    for w in UNSAFE_WORDS:
        if re.search(rf"(^|[^a-z]){w}([^a-z]|$)", name) or name.startswith(w):
            return f"nombre sugiere acción ('{w}')"
    return None


@dataclass
class Response:
    url: str
    status: int
    content_type: str
    body: bytes
    elapsed_s: float

    @property
    def ok(self) -> bool:
        return self.status == 200


class _GetOnlySession(requests.Session):
    def request(self, method, url, *args, **kwargs):  # noqa: D401
        if str(method).upper() != "GET":
            raise UnsafeRequest(f"Método {method} bloqueado: la aplicación es de solo lectura")
        return super().request(method, url, *args, **kwargs)


class DriveClient:
    def __init__(self, host: str, timeout_s: float = 4.0) -> None:
        self.host = host.strip()
        self.base = f"http://{self.host}/"
        self.netloc = urllib.parse.urlsplit(self.base).netloc
        self.timeout_s = timeout_s
        self._lock = threading.Lock()  # garantiza una petición a la vez
        self._session = _GetOnlySession()
        adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0, pool_block=True)
        self._session.mount("http://", adapter)
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        self._session.trust_env = False  # nunca pasar por un proxy del sistema

    def absolute(self, url_or_path: str) -> str:
        return urllib.parse.urljoin(self.base, url_or_path)

    def check(self, url: str) -> str:
        url = self.absolute(url)
        if urllib.parse.urlsplit(url).netloc != self.netloc:
            raise UnsafeRequest(f"URL fuera del drive: {url}")
        reason = unsafe_reason(url)
        if reason:
            raise UnsafeRequest(f"URL bloqueada ({reason}): {url}")
        return url

    def get(self, url_or_path: str) -> Response:
        """GET de una página del drive. Lanza requests.RequestException si no
        hay comunicación y UnsafeRequest si la URL no es segura."""
        url = self.check(url_or_path)
        with self._lock:
            t0 = time.monotonic()
            r = self._session.get(url, timeout=self.timeout_s, allow_redirects=False)
            body = r.content
            elapsed = time.monotonic() - t0
        return Response(url=url, status=r.status_code,
                        content_type=r.headers.get("Content-Type", ""), body=body,
                        elapsed_s=elapsed)

    def close(self) -> None:
        self._session.close()
