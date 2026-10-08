"""Ventana principal."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (QDockWidget, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMainWindow,
                             QMessageBox, QSystemTrayIcon, QTabWidget, QToolBar)

from .. import APP_NAME, __version__, analysis
from ..config import Settings, data_dir
from ..db import Database
from . import theme
from .detail import DriveDetail
from .dialogs import AddDriveDialog, SettingsDialog
from .manager import PollerManager
from .overview import OverviewPage

log = logging.getLogger(__name__)

LEVEL_COLORS = {"error": theme.BAD, "warning": theme.WARN, "info": theme.OK}


def resource_path(rel: str) -> Path:
    """Ruta a un archivo de assets/, tanto desde el código como dentro del .exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / rel


def app_icon() -> QIcon:
    path = resource_path("assets/drivemonitor.png")
    if path.exists():
        icon = QIcon(str(path))
        if not icon.isNull():
            return icon
    log.warning("No se encontró el icono %s; se usa el de respaldo", path)
    return make_icon()


def make_icon(c: str = theme.ACCENT) -> QIcon:
    """Icono de respaldo si no se encuentra assets/drivemonitor.png."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(c))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 12, 12)
    p.setPen(QColor("white"))
    f = p.font()
    f.setPixelSize(30)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "K")
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self, db: Database, settings: Settings) -> None:
        super().__init__()
        self.db, self.settings = db, settings
        self.setWindowTitle(f"{APP_NAME} · Kinetix 5500 (solo lectura)")
        self.setWindowIcon(app_icon())
        self.resize(1360, 860)

        self.manager = PollerManager(db, settings)
        self.manager.message.connect(self._on_message)

        tb = QToolBar("Principal")
        tb.setIconSize(QSize(16, 16))
        tb.setMovable(False)
        self.addToolBar(tb)
        for text, slot in (("Agregar drive", self.add_drive), ("Renombrar", self.rename_drive),
                           ("Quitar drive", self.remove_drive), (None, None),
                           ("Iniciar todos", self.start_all), ("Detener todos", self.stop_all),
                           (None, None), ("Configuración", self.open_settings),
                           ("Carpeta de datos", self.show_data_dir)):
            if text is None:
                tb.addSeparator()
                continue
            a = QAction(text, self)
            a.triggered.connect(slot)
            tb.addAction(a)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.overview = OverviewPage()
        self.overview.open_requested.connect(self.open_detail)
        self.overview.toggle_requested.connect(self.toggle_drive)
        self.tabs.addTab(self.overview, "Vista general")
        self.tabs.tabBar().setTabButton(0, self.tabs.tabBar().ButtonPosition.RightSide, None)
        self.tabs.currentChanged.connect(lambda _i: self._refresh_current(force=True))
        self.setCentralWidget(self.tabs)
        self.details: dict[int, DriveDetail] = {}

        self.alerts = QListWidget()
        self.alerts.itemDoubleClicked.connect(lambda it: self.open_detail(it.data(Qt.ItemDataRole.UserRole)))
        dock = QDockWidget("Alertas", self)
        dock.setObjectName("alerts")
        dock.setWidget(self.alerts)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable |
                         QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.alerts_dock = dock
        dock.setMaximumHeight(170)

        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_NAME)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self.tray.messageClicked.connect(self._bring_front)

        self.status_lbl = QLabel()
        self.statusBar().addPermanentWidget(self.status_lbl)
        self.statusBar().showMessage(f"v{__version__} · Datos en {data_dir()} · Hora local: {analysis.LOCAL_TZ.key}")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(2000)
        self._dirty_cards = True

        self.overview.sync([r["id"] for r in db.drives()])
        self.manager.start_enabled()
        self._tick()

    # --- drives -----------------------------------------------------------
    def _selected_drive(self) -> int | None:
        w = self.tabs.currentWidget()
        if isinstance(w, DriveDetail):
            return w.drive_id
        drives = self.db.drives()
        if not drives:
            return None
        names = [f"{r['name']} ({r['ip']})" for r in drives]
        name, ok = QInputDialog.getItem(self, "Elegir drive", "Drive:", names, 0, False)
        return drives[names.index(name)]["id"] if ok else None

    def add_drive(self) -> None:
        dlg = AddDriveDialog(self)
        if not dlg.exec():
            return
        ip, name = dlg.values()
        if any(r["ip"] == ip for r in self.db.drives()):
            QMessageBox.information(self, "Agregar drive", f"El drive {ip} ya está en la lista.")
            return
        did = self.db.add_drive(ip, name)
        log.info("Drive agregado: %s (%s)", name or ip, ip)
        self.overview.sync([r["id"] for r in self.db.drives()])
        self.manager.start(did)
        self._dirty_cards = True
        self._tick()

    def rename_drive(self) -> None:
        did = self._selected_drive()
        if did is None:
            return
        row = self.db.drive(did)
        name, ok = QInputDialog.getText(self, "Renombrar", "Nombre:", text=row["name"])
        if ok and name.strip():
            self.db.update_drive(did, name=name.strip())
            self.manager.restart(did) if self.manager.is_running(did) else None
            self._dirty_cards = True
            self._tick()

    def remove_drive(self) -> None:
        did = self._selected_drive()
        if did is None:
            return
        row = self.db.drive(did)
        if QMessageBox.question(self, "Quitar drive",
                                f"¿Quitar {row['name']} ({row['ip']}) y borrar su historial?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.manager.stop(did, remember=False)
        d = self.details.pop(did, None)
        if d:
            self.tabs.removeTab(self.tabs.indexOf(d))
        self.db.remove_drive(did)
        self.overview.sync([r["id"] for r in self.db.drives()])

    def toggle_drive(self, did: int) -> None:
        if self.manager.is_running(did):
            self.manager.stop(did)
        else:
            self.manager.start(did)
        self._dirty_cards = True
        self._tick()

    def start_all(self) -> None:
        for r in self.db.drives():
            self.manager.start(r["id"])

    def stop_all(self) -> None:
        for r in self.db.drives():
            self.manager.stop(r["id"])

    def open_detail(self, did: int | None) -> None:
        if did is None or self.db.drive(did) is None:
            return
        d = self.details.get(did)
        if d is None:
            d = DriveDetail(self.db, self.settings, did)
            self.details[did] = d
            self.tabs.addTab(d, self.db.drive(did)["name"])
        self.tabs.setCurrentWidget(d)
        self._refresh_current(force=True)

    def _close_tab(self, i: int) -> None:
        w = self.tabs.widget(i)
        if isinstance(w, DriveDetail):
            self.details.pop(w.drive_id, None)
            self.tabs.removeTab(i)
            w.deleteLater()

    def open_settings(self) -> None:
        old = {k: self.settings.data.get(k) for k in ("request_timeout_s", "url_overrides")}
        dlg = SettingsDialog(self.settings, self.db.drives(), self)
        if dlg.exec():
            if any(self.settings.data.get(k) != v for k, v in old.items()):
                for did in list(self.manager.pollers):
                    self.manager.restart(did)
            for d in self.details.values():
                d.mark_dirty()
            self._refresh_current(force=True)

    def show_data_dir(self) -> None:
        QMessageBox.information(self, "Carpeta de datos",
                                f"Base de datos, configuración y log:\n{data_dir()}")

    # --- avisos de los hilos ----------------------------------------------
    def _on_message(self, kind: str, p: dict) -> None:
        did = p.get("drive_id")
        d = self.details.get(did)
        if kind == "status":
            self._dirty_cards = True
        elif kind == "page":
            if d:
                d.mark_dirty("trends")
                d.mark_dirty("network" if p.get("page") in ("ethernet_stats", "network_stats") else "info")
            self._dirty_cards = True
        elif kind == "faults":
            if d:
                d.mark_dirty("faults")
            self._dirty_cards = True
        elif kind in ("event", "parse_error"):
            if d:
                d.mark_dirty("events")
                d.mark_dirty("info")
            self._dirty_cards = True
        elif kind == "alert":
            self._alert(did, p.get("level", "warning"), p.get("title", ""), p.get("message", ""))

    def _alert(self, did: int | None, level: str, title: str, message: str) -> None:
        stamp = analysis.fmt_local(time.time())
        it = QListWidgetItem(f"{stamp}  {title}" + (f" — {message}" if message else ""))
        it.setForeground(QColor(LEVEL_COLORS.get(level, theme.FG)))
        it.setData(Qt.ItemDataRole.UserRole, did)
        self.alerts.insertItem(0, it)
        while self.alerts.count() > 500:
            self.alerts.takeItem(self.alerts.count() - 1)
        self.alerts_dock.show()
        if self.settings.data.get("notifications", True) and self.tray.isVisible():
            icon = {"error": QSystemTrayIcon.MessageIcon.Critical,
                    "warning": QSystemTrayIcon.MessageIcon.Warning}.get(level, QSystemTrayIcon.MessageIcon.Information)
            self.tray.showMessage(title, message or title, icon, 8000)
        QApplication_alert(self)

    def _bring_front(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # --- refresco periódico -------------------------------------------------
    def _tick(self) -> None:
        try:
            running = sum(1 for did in self.manager.pollers if self.manager.is_running(did))
            offline = sum(1 for s, _ in self.manager.status.values() if s == "offline")
            self.status_lbl.setText(f"Drives activos: {running} · sin comunicación: {offline}")
            self._refresh_current()
        except Exception:  # noqa: BLE001
            log.exception("Error en el refresco periódico")

    def _refresh_current(self, force: bool = False) -> None:
        w = self.tabs.currentWidget()
        if w is self.overview:
            if force or self._dirty_cards:
                self._dirty_cards = False
                for r in self.db.drives():
                    card = self.overview.cards.get(r["id"])
                    if card:
                        state, msg = self.manager.status.get(r["id"], ("stopped", ""))
                        if not self.manager.is_running(r["id"]):
                            state, msg = "stopped", "Detenido"
                        card.refresh(self.db, r, state, msg, self.manager.is_running(r["id"]))
        elif isinstance(w, DriveDetail):
            r = self.db.drive(w.drive_id)
            if r:
                state, msg = self.manager.status.get(r["id"], ("stopped", ""))
                if not self.manager.is_running(r["id"]):
                    state, msg = "stopped", "Detenido"
                w.set_header(r["name"], r["ip"], state, msg)
                self.tabs.setTabText(self.tabs.indexOf(w), r["name"])
            w.refresh(force=force)

    def closeEvent(self, e) -> None:  # noqa: N802
        self.timer.stop()
        self.statusBar().showMessage("Deteniendo consultas…")
        self.manager.stop_all()
        self.tray.hide()
        super().closeEvent(e)


def QApplication_alert(w) -> None:  # noqa: N802
    from PyQt6.QtWidgets import QApplication
    QApplication.alert(w, 3000)
