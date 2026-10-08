"""Arranca/detiene un hilo de consulta por drive y pasa sus avisos a la
interfaz por una señal de Qt (segura entre hilos)."""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal

from ..config import Settings
from ..db import Database
from ..poller import DrivePoller, DriveRef

log = logging.getLogger(__name__)


class PollerManager(QObject):
    message = pyqtSignal(str, dict)   # (tipo, datos) desde cualquier hilo

    def __init__(self, db: Database, settings: Settings) -> None:
        super().__init__()
        self.db = db
        self.settings = settings
        self.pollers: dict[int, DrivePoller] = {}
        self._stopping: dict[int, DrivePoller] = {}
        self.status: dict[int, tuple[str, str]] = {}

    def _emit(self, kind: str, payload: dict) -> None:
        if kind == "status":
            self.status[payload["drive_id"]] = (payload.get("state", ""), payload.get("message", ""))
        self.message.emit(kind, payload)

    def is_running(self, drive_id: int) -> bool:
        p = self.pollers.get(drive_id)
        return bool(p and p.is_alive())

    def start(self, drive_id: int) -> None:
        if self.is_running(drive_id):
            return
        old = self._stopping.pop(drive_id, None)
        if old is not None:
            old.join(10)  # nunca dos hilos (dos conexiones) sobre el mismo drive
        row = self.db.drive(drive_id)
        if row is None:
            return
        poller = DrivePoller(DriveRef(row["id"], row["name"], row["ip"]), self.db, self.settings, self._emit)
        self.pollers[drive_id] = poller
        self.db.update_drive(drive_id, enabled=True)
        poller.start()

    def stop(self, drive_id: int, remember: bool = True) -> None:
        p = self.pollers.pop(drive_id, None)
        if p:
            p.stop()
            self._stopping[drive_id] = p
        if remember:
            self.db.update_drive(drive_id, enabled=False)
        self.status[drive_id] = ("stopped", "Detenido")
        self.message.emit("status", {"drive_id": drive_id, "state": "stopped", "message": "Detenido"})

    def restart(self, drive_id: int) -> None:
        p = self.pollers.pop(drive_id, None)
        if p:
            p.stop()
            p.join(10)
        self.start(drive_id)

    def start_enabled(self) -> None:
        for r in self.db.drives():
            if r["enabled"]:
                self.start(r["id"])

    def stop_all(self, timeout: float = 8.0) -> None:
        pollers = list(self.pollers.values()) + list(self._stopping.values())
        self._stopping.clear()
        for p in pollers:
            p.stop()
        for p in pollers:
            p.join(timeout)
        self.pollers.clear()
