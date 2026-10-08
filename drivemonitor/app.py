"""Punto de entrada de la aplicación de escritorio."""

from __future__ import annotations

import logging
import sys
import threading
import traceback

from . import APP_NAME, __version__
from .config import Settings, data_dir
from .db import Database
from .logging_setup import setup_logging

log = logging.getLogger(__name__)


def _install_excepthooks() -> None:
    """Un error inesperado se registra en el log y no cierra la app."""

    def hook(exc_type, exc, tb):
        log.error("Excepción no controlada:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))

    def thread_hook(args):
        log.error("Excepción en hilo %s:\n%s", args.thread.name if args.thread else "?",
                  "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = hook
    threading.excepthook = thread_hook


def _load_spanish(app) -> None:
    """Traducción de los textos propios de Qt (Sí/No, Cancelar...)."""
    from PyQt6.QtCore import QLibraryInfo, QLocale, QTranslator

    tr = QTranslator(app)
    path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if tr.load(QLocale(QLocale.Language.Spanish), "qtbase", "_", path):
        app.installTranslator(tr)
        app._qt_translator = tr  # mantener viva la referencia
    else:
        log.info("No se encontró la traducción de Qt al español en %s", path)


def main(argv: list[str] | None = None) -> int:
    base = data_dir()
    log_path = setup_logging(base / "logs")
    _install_excepthooks()
    log.info("%s %s iniciando; datos en %s", APP_NAME, __version__, base)

    from PyQt6.QtWidgets import QApplication

    from .ui import theme
    from .ui.main_window import MainWindow

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    _load_spanish(app)
    app.setQuitOnLastWindowClosed(True)
    theme.apply(app)
    settings = Settings()
    db = Database(base / "drivemonitor.db")
    win = MainWindow(db, settings)
    win.show()
    code = app.exec()
    log.info("%s cerrado (código %s). Log: %s", APP_NAME, code, log_path)
    return code


if __name__ == "__main__":
    sys.exit(main())
