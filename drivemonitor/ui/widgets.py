"""Componentes reutilizables: tabla con filtro y exportación, gráfica de tiempo."""

from __future__ import annotations

import logging
from typing import Sequence

import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import analysis
from ..export import export_table
from . import theme

log = logging.getLogger(__name__)


def ask_export_path(parent: QWidget, suggested: str) -> str | None:
    path, _ = QFileDialog.getSaveFileName(parent, "Exportar", suggested,
                                          "Excel (*.xlsx);;CSV (*.csv)")
    if not path:
        return None
    if not path.lower().endswith((".xlsx", ".csv")):
        path += ".xlsx"
    return path


def do_export(parent: QWidget, suggested: str, headers: Sequence[str], rows) -> None:
    path = ask_export_path(parent, suggested)
    if not path:
        return
    try:
        n = export_table(path, headers, rows)
        QMessageBox.information(parent, "Exportar", f"Se exportaron {n} filas a:\n{path}")
    except Exception as e:  # noqa: BLE001
        log.exception("Error al exportar")
        QMessageBox.critical(parent, "Exportar", f"No se pudo exportar:\n{e}")


class NumItem(QTableWidgetItem):
    """Celda que ordena por número aunque muestre texto."""

    def __init__(self, text: str, sort_value) -> None:
        super().__init__(text)
        self.sort_value = sort_value

    def __lt__(self, other):
        a, b = self.sort_value, getattr(other, "sort_value", None)
        if a is None or b is None:
            return (a is None) and (b is not None)
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


class DataTable(QWidget):
    """Tabla con búsqueda y botón de exportar (exporta lo que se ve filtrado)."""

    def __init__(self, headers: Sequence[str], export_name: str = "tabla", parent: QWidget | None = None,
                 searchable: bool = True) -> None:
        super().__init__(parent)
        self.headers = list(headers)
        self.export_name = export_name
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.search = QLineEdit(placeholderText="Buscar…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        self.count_lbl = QLabel("", objectName="muted")
        self.export_btn = QPushButton("Exportar…")
        self.export_btn.clicked.connect(self.export)
        if searchable:
            bar.addWidget(self.search, 1)
        else:
            self.search.hide()
            bar.addStretch(1)
        bar.addWidget(self.count_lbl)
        bar.addWidget(self.export_btn)
        lay.addLayout(bar)
        self.table = QTableWidget(0, len(self.headers))
        self.table.setHorizontalHeaderLabels(self.headers)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSortingEnabled(True)
        lay.addWidget(self.table)

    def set_rows(self, rows: list[Sequence], colors: list[str | None] | None = None,
                 data: list | None = None) -> None:
        """rows: celdas (texto o (texto, valor_para_ordenar)). data: objeto por fila (UserRole)."""
        sel = self.selected_data()
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                if isinstance(cell, tuple):
                    item = NumItem(str(cell[0]), cell[1])
                else:
                    item = NumItem("" if cell is None else str(cell), cell)
                if c == 0 and data is not None:
                    item.setData(Qt.ItemDataRole.UserRole, data[r])
                if colors and colors[r]:
                    item.setForeground(QColor(colors[r]))
                t.setItem(r, c, item)
        t.setSortingEnabled(True)
        if len(rows) and t.columnCount() > 1 and not getattr(self, "_sized", False):
            t.resizeColumnsToContents()
            for c in range(t.columnCount() - 1):
                t.setColumnWidth(c, min(t.columnWidth(c) + 12, 380))
            self._sized = True
        self._apply_filter()
        if sel is not None:
            self.select_data(sel)

    def selected_data(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        it = self.table.item(rows[0].row(), 0)
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def select_data(self, value) -> None:
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.data(Qt.ItemDataRole.UserRole) == value:
                self.table.blockSignals(True)
                self.table.selectRow(r)
                self.table.blockSignals(False)
                return

    def _apply_filter(self) -> None:
        q = self.search.text().strip().lower()
        shown = 0
        for r in range(self.table.rowCount()):
            hide = bool(q) and not any(
                q in (self.table.item(r, c).text().lower() if self.table.item(r, c) else "")
                for c in range(self.table.columnCount()))
            self.table.setRowHidden(r, hide)
            shown += not hide
        self.count_lbl.setText(f"{shown} filas")

    def visible_rows(self) -> list[list[str]]:
        out = []
        for r in range(self.table.rowCount()):
            if self.table.isRowHidden(r):
                continue
            out.append([self.table.item(r, c).text() if self.table.item(r, c) else ""
                        for c in range(self.table.columnCount())])
        return out

    def export(self) -> None:
        do_export(self, f"{self.export_name}.xlsx", self.headers, self.visible_rows())


class LocalDateAxis(pg.DateAxisItem):
    """Eje de fechas en hora local de America/Mexico_City (no la del PC)."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.utcOffset = -analysis.utc_offset_s()


def time_plot(title: str = "") -> pg.PlotWidget:
    w = pg.PlotWidget(axisItems={"bottom": LocalDateAxis(orientation="bottom")}, title=title)
    w.showGrid(x=True, y=True, alpha=0.15)
    w.addLegend(offset=(10, 10))
    w.setMouseEnabled(x=True, y=True)
    return w


def color(i: int) -> str:
    return theme.SERIES_COLORS[i % len(theme.SERIES_COLORS)]
