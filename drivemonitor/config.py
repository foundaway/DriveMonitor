"""Rutas de datos y configuración de la aplicación (settings.json)."""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import APP_NAME

log = logging.getLogger(__name__)

LOCAL_TZ_NAME = "America/Mexico_City"


@dataclass(frozen=True)
class PageDef:
    """Una página del servidor web del drive.

    mode "periodic": se lee cada interval_s.
    mode "on_change": se lee al conectar y cada interval_s, pero solo se
    registra si algo cambió.
    """

    key: str
    label: str          # texto del menú del drive con el que se busca la página
    mode: str
    interval_s: float
    aliases: tuple[str, ...] = ()


PAGES: tuple[PageDef, ...] = (
    PageDef("home", "Home", "on_change", 600),
    PageDef("drive_info", "Drive Information", "on_change", 600),
    PageDef("motor", "Motor Diagnostics", "on_change", 600),
    PageDef("encoder", "Encoder Diagnostics", "periodic", 30),
    PageDef("network_settings", "Network Settings", "on_change", 600),
    PageDef("ethernet_stats", "Ethernet Statistics", "periodic", 60),
    PageDef("network_stats", "Network Statistics", "periodic", 60),
    PageDef("monitor", "Monitor Signals", "periodic", 2),
    PageDef("fault_log", "Fault Log", "periodic", 15, ("Fault Logs",)),
)
PAGE_BY_KEY = {p.key: p for p in PAGES}

DEFAULT_SETTINGS: dict = {
    "intervals": {p.key: p.interval_s for p in PAGES},
    "request_timeout_s": 4.0,
    "min_gap_s": 0.5,             # pausa mínima entre dos peticiones al mismo drive
    "max_backoff_s": 60.0,
    "shifts": [                   # turnos (hora local, inicio inclusive)
        {"name": "Turno 1", "start": "07:00"},
        {"name": "Turno 2", "start": "15:00"},
        {"name": "Turno 3", "start": "23:00"},
    ],
    "retention_days": {"monitor": 60},  # 0 = sin límite
    "notifications": True,
    "url_overrides": {},          # {"<ip>": {"fault_log": "http://.../x.html"}}
}

MIN_INTERVAL_S = 2.0


def data_dir() -> Path:
    """%LOCALAPPDATA%\\DriveMonitor en Windows; ~/.local/share/DriveMonitor en otros.
    Se puede forzar con la variable DRIVEMONITOR_HOME."""
    if os.environ.get("DRIVEMONITOR_HOME"):
        d = Path(os.environ["DRIVEMONITOR_HOME"])
    elif sys.platform == "win32":
        d = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    else:
        d = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "settings.json"
        self.data = copy.deepcopy(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.data = _merge(DEFAULT_SETTINGS, json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            log.error("No se pudo leer %s (%s); se usan valores por defecto", self.path, e)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def interval(self, page_key: str) -> float:
        v = self.data["intervals"].get(page_key, PAGE_BY_KEY[page_key].interval_s)
        try:
            return max(MIN_INTERVAL_S, float(v))
        except (TypeError, ValueError):
            return PAGE_BY_KEY[page_key].interval_s

    def url_overrides(self, ip: str) -> dict[str, str]:
        return dict(self.data.get("url_overrides", {}).get(ip, {}))
