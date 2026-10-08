"""Log de la aplicación a archivo con rotación."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def setup_logging(log_dir: Path, level: int = logging.INFO) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "drivemonitor.log"
    root = logging.getLogger()
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        return path
    root.setLevel(level)
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"))
    root.addHandler(fh)
    # Evita que urllib3 llene el log con cada conexión.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return path
