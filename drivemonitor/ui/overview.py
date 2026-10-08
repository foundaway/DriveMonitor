"""Vista general: una tarjeta por drive."""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                             QSizePolicy, QVBoxLayout, QWidget)

from .. import analysis
from ..db import Database
from ..parsers import format_duration
from ..poller import QUALITY_RE, RSSI_RE
from . import theme


class Dot(QLabel):
    def __init__(self, size: int = 12) -> None:
        super().__init__()
        self._size = size
        self.setFixedSize(size, size)
        self.set_color(theme.IDLE)

    def set_color(self, c: str) -> None:
        self.setStyleSheet(f"background:{c}; border-radius:{self._size // 2}px;")


class DriveCard(QFrame):
    open_requested = pyqtSignal(int)
    toggle_requested = pyqtSignal(int)

    def __init__(self, drive_id: int) -> None:
        super().__init__(objectName="card")
        self.drive_id = drive_id
        self.setMinimumWidth(300)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)

        top = QHBoxLayout()
        self.name = QLabel(objectName="title")
        self.ip = QLabel(objectName="muted")
        top.addWidget(self.name)
        top.addStretch(1)
        top.addWidget(self.ip)
        lay.addLayout(top)

        st = QHBoxLayout()
        self.dot = Dot()
        self.state = QLabel()
        st.addWidget(self.dot)
        st.addWidget(self.state, 1)
        lay.addLayout(st)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        self.faults24 = QLabel(objectName="big")
        self.rssi = QLabel(objectName="big")
        self.quality = QLabel(objectName="big")
        for col, (lbl, w) in enumerate((("Fallas 24 h", self.faults24), ("RSSI", self.rssi),
                                        ("Quality", self.quality))):
            cap = QLabel(lbl, objectName="muted")
            grid.addWidget(cap, 0, col)
            grid.addWidget(w, 1, col)
        lay.addLayout(grid)

        lay.addWidget(QLabel("Última falla", objectName="muted"))
        self.last_fault = QLabel()
        self.last_fault.setWordWrap(True)
        lay.addWidget(self.last_fault)
        self.updated = QLabel(objectName="muted")
        lay.addWidget(self.updated)

        btns = QHBoxLayout()
        self.open_btn = QPushButton("Abrir detalle", objectName="primary")
        self.toggle_btn = QPushButton()
        self.open_btn.clicked.connect(lambda: self.open_requested.emit(self.drive_id))
        self.toggle_btn.clicked.connect(lambda: self.toggle_requested.emit(self.drive_id))
        btns.addWidget(self.open_btn)
        btns.addWidget(self.toggle_btn)
        lay.addLayout(btns)

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        self.open_requested.emit(self.drive_id)

    @staticmethod
    def _pct(label: QLabel, value: float | None) -> bool:
        if value is None:
            label.setText("—")
            label.setStyleSheet("")
            return False
        label.setText(f"{value:g} %")
        low = value < 100
        label.setStyleSheet(f"color:{theme.BAD if low else theme.OK};")
        return low

    def refresh(self, db: Database, row, state: str, message: str, running: bool) -> None:
        did = self.drive_id
        self.name.setText(row["name"])
        self.ip.setText(row["ip"])
        self.dot.set_color(theme.STATE_COLORS.get(state, theme.IDLE))
        self.state.setText(message or theme.STATE_TEXT.get(state, state))
        self.toggle_btn.setText("Detener" if running else "Iniciar")

        now = time.time()
        n24 = db.fault_count_since(did, now - 86400)
        self.faults24.setText(str(n24))
        self.faults24.setStyleSheet(f"color:{theme.BAD};" if n24 else "")

        enc = db.latest_values(did, "encoder")
        rssi = next((v[2] for k, v in enc.items() if RSSI_RE.search(k)), None)
        qual = next((v[2] for k, v in enc.items() if QUALITY_RE.search(k)), None)
        low = self._pct(self.rssi, rssi) | self._pct(self.quality, qual)

        lf = db.last_fault(did)
        if lf:
            t = lf["t"]
            ago = format_duration(now - t) if t else ""
            self.last_fault.setText(f"<b>{analysis.fault_label(lf['fault_id'], lf['sub_code'])}</b> "
                                    f"{lf['text'] or ''}<br><span style='color:{theme.MUTED}'>"
                                    f"{analysis.fmt_local(t)} (hace {ago})</span>")
        else:
            self.last_fault.setText(f"<span style='color:{theme.MUTED}'>Sin fallas registradas</span>")

        last = db.last_sample_ts(did)
        self.updated.setText(f"Última lectura: {analysis.fmt_local(last)}" if last else "Sin lecturas todavía")

        recent_fault = bool(lf and lf["t"] and now - lf["t"] < 3600)
        alert = state == "offline" or low or recent_fault
        if self.property("alert") != alert:
            self.setProperty("alert", alert)
            self.style().unpolish(self)
            self.style().polish(self)


class OverviewPage(QScrollArea):
    open_requested = pyqtSignal(int)
    toggle_requested = pyqtSignal(int)

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.host = QWidget()
        self.grid = QGridLayout(self.host)
        self.grid.setContentsMargins(16, 16, 16, 16)
        self.grid.setSpacing(14)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.setWidget(self.host)
        self.cards: dict[int, DriveCard] = {}
        self.empty = QLabel("No hay drives. Usa «Agregar drive» y escribe solo la IP.", objectName="muted")
        self.grid.addWidget(self.empty, 0, 0)
        self._cols = 0

    def _relayout(self) -> None:
        cols = max(1, self.viewport().width() // 340)
        if cols == self._cols and self.grid.count() == len(self.cards) + (0 if self.cards else 1):
            return
        self._cols = cols
        for i in reversed(range(self.grid.count())):
            self.grid.takeAt(i)
        if not self.cards:
            self.grid.addWidget(self.empty, 0, 0)
            self.empty.show()
            return
        self.empty.hide()
        for i, card in enumerate(self.cards.values()):
            self.grid.addWidget(card, i // cols, i % cols)

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._relayout()

    def sync(self, drive_ids: list[int]) -> None:
        for did in list(self.cards):
            if did not in drive_ids:
                card = self.cards.pop(did)
                card.setParent(None)
                card.deleteLater()
        for did in drive_ids:
            if did not in self.cards:
                card = DriveCard(did)
                card.open_requested.connect(self.open_requested)
                card.toggle_requested.connect(self.toggle_requested)
                self.cards[did] = card
        self._cols = 0
        self._relayout()
