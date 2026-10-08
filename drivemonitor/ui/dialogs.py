"""Diálogos: agregar drive y configuración."""

from __future__ import annotations

import ipaddress
import re

from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..config import MIN_INTERVAL_S, PAGES, Settings
from ..http_client import unsafe_reason

HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$")


def valid_host(text: str) -> bool:
    text = text.strip()
    if not HOST_RE.match(text):
        return False
    host = text.split(":")[0]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(re.match(r"^[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*$", host))


class AddDriveDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Agregar drive")
        lay = QFormLayout(self)
        self.ip = QLineEdit(placeholderText="172.23.22.95")
        self.name = QLineEdit(placeholderText="(opcional) p. ej. Línea 3 - Eje X")
        lay.addRow("IP del drive:", self.ip)
        lay.addRow("Nombre:", self.name)
        lay.addRow(QLabel("La app solo lee el servidor web del drive (HTTP GET); no cambia nada.",
                          objectName="muted"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Agregar")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def _ok(self) -> None:
        if not valid_host(self.ip.text()):
            QMessageBox.warning(self, "IP inválida", "Escribe una IP válida, p. ej. 172.23.22.95")
            return
        self.accept()

    def values(self) -> tuple[str, str]:
        return self.ip.text().strip(), self.name.text().strip()


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, drives: list, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Configuración")
        self.resize(640, 640)
        lay = QVBoxLayout(self)

        g = QGroupBox("Intervalos de consulta (segundos)")
        f = QFormLayout(g)
        self.intervals: dict[str, QDoubleSpinBox] = {}
        for p in PAGES:
            sb = QDoubleSpinBox(minimum=MIN_INTERVAL_S, maximum=86400, decimals=0)
            sb.setValue(settings.interval(p.key))
            note = "  (se registra solo si cambia)" if p.mode == "on_change" else ""
            f.addRow(f"{p.label}{note}:", sb)
            self.intervals[p.key] = sb
        lay.addWidget(g)

        g2 = QGroupBox("Comunicación")
        f2 = QFormLayout(g2)
        self.timeout = QDoubleSpinBox(minimum=1, maximum=30, decimals=1)
        self.timeout.setValue(float(settings.data["request_timeout_s"]))
        self.gap = QDoubleSpinBox(minimum=0.2, maximum=10, decimals=1)
        self.gap.setValue(float(settings.data["min_gap_s"]))
        self.backoff = QDoubleSpinBox(minimum=5, maximum=600, decimals=0)
        self.backoff.setValue(float(settings.data["max_backoff_s"]))
        f2.addRow("Timeout por petición:", self.timeout)
        f2.addRow("Pausa mínima entre peticiones:", self.gap)
        f2.addRow("Espera máxima al reintentar:", self.backoff)
        lay.addWidget(g2)

        g3 = QGroupBox("Turnos (hora local de inicio)")
        v3 = QVBoxLayout(g3)
        self.shifts = QTableWidget(0, 2)
        self.shifts.setHorizontalHeaderLabels(["Nombre", "Inicio (HH:MM)"])
        self.shifts.horizontalHeader().setStretchLastSection(True)
        self.shifts.setMinimumHeight(130)
        for s in settings.data.get("shifts", []):
            self._add_shift(s["name"], s["start"])
        hb = QHBoxLayout()
        add = QPushButton("Agregar turno")
        rem = QPushButton("Quitar turno")
        add.clicked.connect(lambda: self._add_shift(f"Turno {self.shifts.rowCount() + 1}", "00:00"))
        rem.clicked.connect(lambda: self.shifts.removeRow(self.shifts.currentRow()))
        hb.addWidget(add)
        hb.addWidget(rem)
        hb.addStretch(1)
        v3.addWidget(self.shifts)
        v3.addLayout(hb)
        lay.addWidget(g3)

        g4 = QGroupBox("Otros")
        f4 = QFormLayout(g4)
        self.retention = QSpinBox(minimum=0, maximum=3650)
        self.retention.setValue(int(settings.data.get("retention_days", {}).get("monitor", 60)))
        self.retention.setSuffix(" días (0 = sin límite)")
        self.notify = QCheckBox("Mostrar notificaciones de Windows")
        self.notify.setChecked(bool(settings.data.get("notifications", True)))
        f4.addRow("Guardar Monitor Signals:", self.retention)
        f4.addRow(self.notify)
        lay.addWidget(g4)

        g5 = QGroupBox("URL manual de una página (solo si el menú del drive no la muestra)")
        f5 = QFormLayout(g5)
        self.ov_drive = QComboBox()
        for d in drives:
            self.ov_drive.addItem(f"{d['name']} ({d['ip']})", d["ip"])
        self.ov_page = QComboBox()
        for p in PAGES:
            self.ov_page.addItem(p.label, p.key)
        self.ov_url = QLineEdit(placeholderText="vacío = buscar en el menú; p. ej. /diag/encoder.html")
        self._overrides = {ip: dict(v) for ip, v in settings.data.get("url_overrides", {}).items()}
        self.ov_drive.currentIndexChanged.connect(self._load_ov)
        self.ov_page.currentIndexChanged.connect(self._load_ov)
        self.ov_url.editingFinished.connect(self._store_ov)
        f5.addRow("Drive:", self.ov_drive)
        f5.addRow("Página:", self.ov_page)
        f5.addRow("URL:", self.ov_url)
        if not drives:
            g5.setEnabled(False)
        lay.addWidget(g5)
        self._load_ov()

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Save).setText("Guardar")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _add_shift(self, name: str, start: str) -> None:
        r = self.shifts.rowCount()
        self.shifts.insertRow(r)
        self.shifts.setItem(r, 0, QTableWidgetItem(name))
        self.shifts.setItem(r, 1, QTableWidgetItem(start))

    def _load_ov(self) -> None:
        ip, page = self.ov_drive.currentData(), self.ov_page.currentData()
        self.ov_url.setText(self._overrides.get(ip, {}).get(page, ""))

    def _store_ov(self) -> None:
        ip, page = self.ov_drive.currentData(), self.ov_page.currentData()
        if ip is None:
            return
        url = self.ov_url.text().strip()
        if url and unsafe_reason(url if "://" in url else f"http://x/{url.lstrip('/')}"):
            QMessageBox.warning(self, "URL no permitida",
                                "Esa URL tiene parámetros o un nombre de acción; la app es de solo lectura.")
            self.ov_url.setText("")
            return
        d = self._overrides.setdefault(ip, {})
        if url:
            d[page] = url
        else:
            d.pop(page, None)

    def _save(self) -> None:
        self._store_ov()
        shifts = []
        for r in range(self.shifts.rowCount()):
            name = (self.shifts.item(r, 0).text() if self.shifts.item(r, 0) else "").strip()
            start = (self.shifts.item(r, 1).text() if self.shifts.item(r, 1) else "").strip()
            if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", start):
                QMessageBox.warning(self, "Turnos", f"Hora inválida en el turno «{name}»: {start}")
                return
            shifts.append({"name": name or f"Turno {r + 1}", "start": start})
        d = self.settings.data
        d["intervals"] = {k: sb.value() for k, sb in self.intervals.items()}
        d["request_timeout_s"] = self.timeout.value()
        d["min_gap_s"] = self.gap.value()
        d["max_backoff_s"] = self.backoff.value()
        d["shifts"] = shifts
        d.setdefault("retention_days", {})["monitor"] = self.retention.value()
        d["notifications"] = self.notify.isChecked()
        d["url_overrides"] = {ip: v for ip, v in self._overrides.items() if v}
        self.settings.save()
        self.accept()
