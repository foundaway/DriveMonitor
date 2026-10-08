"""Detalle de un drive: pestañas Fallas, Tendencias, Red, Info y Eventos."""

from __future__ import annotations

import logging
import time
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QDateTime, Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDateTimeEdit, QHBoxLayout, QLabel, QListWidget,
                             QListWidgetItem, QPushButton, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from .. import analysis
from ..analysis import LOCAL_TZ, FaultPoint
from ..config import PAGE_BY_KEY, Settings
from ..db import Database
from ..parsers import format_duration
from ..poller import NET_ERROR_RE, QUALITY_RE, RSSI_RE
from . import theme
from .widgets import DataTable, color, do_export, time_plot

log = logging.getLogger(__name__)

EVENT_TEXT = {
    "comm_lost": "Pérdida de comunicación", "comm_restored": "Comunicación restablecida",
    "reboot": "Reinicio del drive", "firmware_change": "Cambio de firmware",
    "network_config_change": "Cambio de red", "info_change": "Cambio de información",
    "net_errors": "Errores de red", "encoder_low": "Encoder bajo", "encoder_ok": "Encoder normal",
    "discovery": "Descubrimiento", "faultlog_initial": "Fault Log inicial", "internal_error": "Error interno",
}
EVENT_COLORS = {"comm_lost": theme.BAD, "reboot": theme.BAD, "firmware_change": theme.WARN,
                "network_config_change": theme.WARN, "net_errors": theme.WARN, "encoder_low": theme.WARN,
                "internal_error": theme.BAD, "comm_restored": theme.OK, "encoder_ok": theme.OK}


def page_label(key: str) -> str:
    return PAGE_BY_KEY[key].label if key in PAGE_BY_KEY else key


# --------------------------------------------------------------------------
class RangeSelector(QWidget):
    """Rango de fechas en hora local (America/Mexico_City)."""

    changed = pyqtSignal()
    PRESETS = [("Última hora", 3600), ("Últimas 8 h", 8 * 3600), ("Últimas 24 h", 86400),
               ("Últimos 7 días", 7 * 86400), ("Últimos 30 días", 30 * 86400), ("Todo", None),
               ("Personalizado", -1)]

    def __init__(self, default_index: int = 2) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        for name, _ in self.PRESETS:
            self.combo.addItem(name)
        self.start = QDateTimeEdit(calendarPopup=True, displayFormat="yyyy-MM-dd HH:mm")
        self.end = QDateTimeEdit(calendarPopup=True, displayFormat="yyyy-MM-dd HH:mm")
        lay.addWidget(QLabel("Rango:"))
        lay.addWidget(self.combo)
        lay.addWidget(QLabel("de"))
        lay.addWidget(self.start)
        lay.addWidget(QLabel("a"))
        lay.addWidget(self.end)
        self.combo.setCurrentIndex(default_index)
        self._sync_edits()
        self.combo.currentIndexChanged.connect(self._preset_changed)
        self.start.dateTimeChanged.connect(self._custom_changed)
        self.end.dateTimeChanged.connect(self._custom_changed)

    @staticmethod
    def _to_qdt(ts: float) -> QDateTime:
        d = analysis.to_local(ts).replace(tzinfo=None)
        return QDateTime(d.year, d.month, d.day, d.hour, d.minute, d.second)

    @staticmethod
    def _from_qdt(q: QDateTime) -> float:
        d = q.toPyDateTime().replace(tzinfo=LOCAL_TZ, microsecond=0)
        return d.timestamp()

    def _sync_edits(self) -> None:
        t0, t1 = self.range()
        for w, t in ((self.start, t0), (self.end, t1)):
            w.blockSignals(True)
            w.setDateTime(self._to_qdt(max(t, 0)))
            w.blockSignals(False)

    def _preset_changed(self) -> None:
        if self.PRESETS[self.combo.currentIndex()][1] != -1:
            self._sync_edits()
        self.changed.emit()

    def _custom_changed(self) -> None:
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(len(self.PRESETS) - 1)
        self.combo.blockSignals(False)
        self.changed.emit()

    def is_relative(self) -> bool:
        return self.PRESETS[self.combo.currentIndex()][1] not in (-1,)

    def range(self) -> tuple[float, float]:
        now = time.time()
        span = self.PRESETS[self.combo.currentIndex()][1]
        if span is None:
            return 0.0, now + 86400
        if span == -1:
            return self._from_qdt(self.start.dateTime()), self._from_qdt(self.end.dateTime())
        return now - span, now + 60

    def set_window(self, t0: float, t1: float) -> None:
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(len(self.PRESETS) - 1)
        self.combo.blockSignals(False)
        for w, t in ((self.start, t0), (self.end, t1)):
            w.blockSignals(True)
            w.setDateTime(self._to_qdt(t))
            w.blockSignals(False)
        self.changed.emit()


def bar_plot(title: str) -> pg.PlotWidget:
    w = pg.PlotWidget(title=title)
    w.showGrid(x=False, y=True, alpha=0.15)
    w.setMouseEnabled(x=True, y=False)
    return w


def set_category_ticks(plot: pg.PlotWidget, labels: list[str], max_len: int = 18) -> None:
    ticks = [(i, (l if len(l) <= max_len else l[:max_len - 1] + "…")) for i, l in enumerate(labels)]
    plot.getAxis("bottom").setTicks([ticks])


# --------------------------------------------------------------------------
class FaultsTab(QWidget):
    def __init__(self, db: Database, settings: Settings, drive_id: int) -> None:
        super().__init__()
        self.db, self.settings, self.drive_id = db, settings, drive_id
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.range = RangeSelector(default_index=3)
        self.code = QComboBox()
        self.code.addItem("Todos los códigos", None)
        bar.addWidget(self.range)
        bar.addWidget(QLabel("Código:"))
        bar.addWidget(self.code)
        bar.addStretch(1)
        lay.addLayout(bar)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = DataTable(["Hora local", "Código", "Subcódigo", "Texto del drive", "Uptime",
                                "CumulativeUptime", "Detectada (PC)"], export_name="fallas")
        self.table.table.itemSelectionChanged.connect(self._fault_selected)
        split.addWidget(self.table)

        self.charts = QTabWidget()
        self.pareto = bar_plot("Pareto por código")
        self.hours = bar_plot("Fallas por hora del día (hora local)")
        self.shifts = bar_plot("Fallas por turno")
        tbf_w = QWidget()
        tl = QVBoxLayout(tbf_w)
        self.tbf_lbl = QLabel()
        self.tbf_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.tbf_plot = bar_plot("Distribución del tiempo entre fallas (horas)")
        tl.addWidget(self.tbf_lbl)
        tl.addWidget(self.tbf_plot, 1)
        win_w = QWidget()
        wl = QVBoxLayout(win_w)
        self.win_lbl = QLabel("Selecciona una falla en la tabla para ver las señales de 60 s antes a 60 s después.",
                              objectName="muted")
        self.win_lbl.setWordWrap(True)
        self.win_plot = time_plot()
        wexp = QPushButton("Exportar ventana…")
        wexp.clicked.connect(self._export_window)
        whead = QHBoxLayout()
        whead.addWidget(self.win_lbl, 1)
        whead.addWidget(wexp)
        wl.addLayout(whead)
        wl.addWidget(self.win_plot, 1)
        self.charts.addTab(win_w, "Señales ±60 s")
        self.charts.addTab(self.pareto, "Pareto")
        self.charts.addTab(self.hours, "Por hora")
        self.charts.addTab(self.shifts, "Por turno")
        self.charts.addTab(tbf_w, "Tiempo entre fallas")
        split.addWidget(self.charts)
        split.setSizes([380, 320])
        lay.addWidget(split, 1)

        self._pareto_vb2 = None
        self._window_rows: list = []
        self.range.changed.connect(self.refresh)
        self.code.currentIndexChanged.connect(self.refresh)

    def refresh(self) -> None:
        t0, t1 = self.range.range()
        rows = self.db.faults(self.drive_id, t0, t1)
        codes = sorted({(r["fault_id"], r["sub_code"], r["text"]) for r in rows}, key=lambda c: (c[0] or 0, c[1] or 0))
        current = self.code.currentData()
        self.code.blockSignals(True)
        self.code.clear()
        self.code.addItem("Todos los códigos", None)
        for fid, sub, text in codes:
            self.code.addItem(analysis.fault_label(fid, sub, text), (fid, sub))
        idx = self.code.findData(current) if current is not None else 0
        self.code.setCurrentIndex(max(0, idx))
        self.code.blockSignals(False)
        sel = self.code.currentData()
        if sel is not None:
            rows = [r for r in rows if (r["fault_id"], r["sub_code"]) == tuple(sel)]

        self.table.set_rows(
            [[(analysis.fmt_local(r["t"]), r["t"]), (r["fault_id"], r["fault_id"]), (r["sub_code"], r["sub_code"]),
              r["text"] or "", (format_duration(r["uptime_s"]), r["uptime_s"]),
              (format_duration(r["cumulative_uptime_s"]), r["cumulative_uptime_s"]),
              (analysis.fmt_local(r["first_seen_utc"]), r["first_seen_utc"])] for r in rows],
            data=[r["id"] for r in rows])
        points = [FaultPoint(r["t"], r["fault_id"], r["sub_code"], r["text"] or "") for r in rows if r["t"]]
        self._draw_pareto(points)
        self._draw_hours(points)
        self._draw_shifts(points)
        self._draw_tbf(points)

    def _draw_pareto(self, points: list[FaultPoint]) -> None:
        p = self.pareto
        p.clear()
        data = analysis.pareto(points)[:15]
        pi = p.getPlotItem()
        if self._pareto_vb2 is None:
            pi.showAxis("right")
            vb2 = pg.ViewBox()
            pi.scene().addItem(vb2)
            pi.getAxis("right").linkToView(vb2)
            pi.getAxis("right").setLabel("% acumulado")
            vb2.setXLink(pi)

            def upd():
                vb2.setGeometry(pi.vb.sceneBoundingRect())
                vb2.linkedViewChanged(pi.vb, vb2.XAxis)

            pi.vb.sigResized.connect(upd)
            vb2.setZValue(pi.vb.zValue() + 10)  # la línea acumulada encima de las barras
            self._pareto_vb2 = vb2
        vb2 = self._pareto_vb2
        vb2.clear()
        if not data:
            return
        x = np.arange(len(data))
        p.addItem(pg.BarGraphItem(x=x, height=[d[1] for d in data], width=0.7, brush=theme.ACCENT))
        vb2.addItem(pg.PlotDataItem(x, [d[2] for d in data], pen=pg.mkPen(theme.WARN, width=2),
                                    symbol="o", symbolBrush=theme.WARN, symbolSize=6))
        vb2.setYRange(0, 105)
        set_category_ticks(p, [d[0] for d in data])
        p.setXRange(-0.6, len(data) - 0.4)

    def _draw_hours(self, points: list[FaultPoint]) -> None:
        self.hours.clear()
        h = analysis.by_hour(points)
        self.hours.addItem(pg.BarGraphItem(x=np.arange(24), height=h, width=0.8, brush=theme.ACCENT))
        self.hours.getAxis("bottom").setTicks([[(i, f"{i:02d}") for i in range(24)]])

    def _draw_shifts(self, points: list[FaultPoint]) -> None:
        self.shifts.clear()
        data = analysis.by_shift(points, self.settings.data.get("shifts", []))
        if not data:
            return
        self.shifts.addItem(pg.BarGraphItem(x=np.arange(len(data)), height=[d[1] for d in data],
                                            width=0.6, brush=theme.ACCENT))
        set_category_ticks(self.shifts, [f"{d[0]} ({d[1]})" for d in data], 30)

    def _draw_tbf(self, points: list[FaultPoint]) -> None:
        st = analysis.time_between(points, time.time())
        self.tbf_plot.clear()
        if not st.intervals_s:
            self.tbf_lbl.setText("Se necesitan al menos dos fallas en el rango." +
                                 (f" Desde la última falla: <b>{format_duration(st.since_last_s)}</b>"
                                  if st.since_last_s is not None else ""))
            return
        self.tbf_lbl.setText(
            f"Fallas: <b>{len(points)}</b> &nbsp; Promedio: <b>{format_duration(st.mean_s)}</b> &nbsp; "
            f"Mediana: <b>{format_duration(st.median_s)}</b> &nbsp; Mínimo: <b>{format_duration(st.min_s)}</b>"
            f" &nbsp; Máximo: <b>{format_duration(st.max_s)}</b> &nbsp; Desde la última: "
            f"<b>{format_duration(st.since_last_s)}</b>")
        hours = np.array(st.intervals_s) / 3600.0
        counts, edges = np.histogram(hours, bins=min(20, max(5, len(hours) // 2)))
        self.tbf_plot.addItem(pg.BarGraphItem(x0=edges[:-1], x1=edges[1:], height=counts, brush=theme.ACCENT))

    def _fault_selected(self) -> None:
        fid = self.table.selected_data()
        if fid is None:
            return
        row = next((r for r in self.db.faults(self.drive_id) if r["id"] == fid), None)
        if row is None or not row["t"]:
            return
        t = row["t"]
        self.win_plot.clear()
        self.win_plot.addLegend(offset=(10, 10))
        self._window_rows = []
        keys = self.db.sample_keys(self.drive_id, "monitor", numeric_only=True)
        n = 0
        for i, k in enumerate(keys):
            s = self.db.series(self.drive_id, "monitor", k, t - 60, t + 60)
            pts = [(ts, v) for ts, v, _ in s if v is not None]
            self._window_rows += [(analysis.fmt_local(ts), k, txt, v) for ts, v, txt in s]
            if pts:
                xs, ys = zip(*pts)
                self.win_plot.plot(xs, ys, pen=pg.mkPen(color(i), width=2), name=k, symbol="o", symbolSize=4,
                                   symbolBrush=color(i))
                n += 1
        self.win_plot.addItem(pg.InfiniteLine(t, angle=90, pen=pg.mkPen(theme.BAD, width=2, style=Qt.PenStyle.DashLine)))
        self.win_plot.setXRange(t - 60, t + 60)
        label = f"Falla {analysis.fault_label(row['fault_id'], row['sub_code'], row['text'] or '')} — " \
                f"{analysis.fmt_local(t)} (hora del drive)."
        self.win_lbl.setText(label + ("" if n else " No hay señales registradas en ese intervalo."))
        self.charts.setCurrentIndex(0)

    def _export_window(self) -> None:
        do_export(self, "senales_falla.xlsx", ["Hora local", "Señal", "Valor", "Número"], self._window_rows)


# --------------------------------------------------------------------------
class TrendsTab(QWidget):
    DEFAULTS = {"encoder": ("temperature", "voltage", "rssi", "quality")}

    def __init__(self, db: Database, drive_id: int) -> None:
        super().__init__()
        self.db, self.drive_id = db, drive_id
        lay = QHBoxLayout(self)
        side = QVBoxLayout()
        self.page = QComboBox()
        for k in ("monitor", "encoder", "ethernet_stats", "network_stats", "home", "drive_info", "motor"):
            self.page.addItem(page_label(k), k)
        side.addWidget(QLabel("Página"))
        side.addWidget(self.page)
        side.addWidget(QLabel("Señales (marca las que quieras ver)"))
        self.keys = QListWidget()
        side.addWidget(self.keys, 1)
        self.live = QCheckBox("Actualizar en vivo")
        self.live.setChecked(True)
        side.addWidget(self.live)
        exp = QPushButton("Exportar rango…")
        exp.clicked.connect(self.export)
        side.addWidget(exp)
        holder = QWidget()
        holder.setLayout(side)
        holder.setMaximumWidth(300)
        lay.addWidget(holder)

        right = QVBoxLayout()
        self.range = RangeSelector(default_index=0)
        top = QHBoxLayout()
        top.addWidget(self.range)
        top.addStretch(1)
        right.addLayout(top)
        self.plot = time_plot()
        self.plot.getPlotItem().setDownsampling(auto=True, mode="peak")
        self.plot.getPlotItem().setClipToView(True)
        right.addWidget(self.plot, 1)
        hint = QLabel("Rueda del mouse: zoom · arrastrar: mover · clic derecho: ver todo. "
                     "Para hacer zoom sin que se reinicie, desmarca «Actualizar en vivo».", objectName="muted")
        right.addWidget(hint)
        lay.addLayout(right, 1)

        self._checked: dict[str, set[str]] = {}
        self.page.currentIndexChanged.connect(self._load_keys)
        self.keys.itemChanged.connect(self._key_toggled)
        self.range.changed.connect(self.refresh)

    def _load_keys(self) -> None:
        page = self.page.currentData()
        keys = self.db.sample_keys(self.drive_id, page, numeric_only=True)
        if page not in self._checked:
            pats = self.DEFAULTS.get(page)
            self._checked[page] = ({k for k in keys if any(p in k.lower() for p in pats)} if pats
                                   else set(keys[:4]))
        self.keys.blockSignals(True)
        self.keys.clear()
        for k in keys:
            it = QListWidgetItem(k)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if k in self._checked[page] else Qt.CheckState.Unchecked)
            self.keys.addItem(it)
        self.keys.blockSignals(False)
        self.refresh()

    def _key_toggled(self, it: QListWidgetItem) -> None:
        sel = self._checked.setdefault(self.page.currentData(), set())
        (sel.add if it.checkState() == Qt.CheckState.Checked else sel.discard)(it.text())
        self.refresh()

    def selected_keys(self) -> list[str]:
        return [self.keys.item(i).text() for i in range(self.keys.count())
                if self.keys.item(i).checkState() == Qt.CheckState.Checked]

    def refresh(self) -> None:
        if self.keys.count() == 0:
            self._load_keys_once()
        page = self.page.currentData()
        t0, t1 = self.range.range()
        vr = self.plot.getPlotItem().vb.viewRange()
        keep_zoom = not self.live.isChecked() and getattr(self, "_drawn", False)
        self.plot.clear()
        self.plot.getPlotItem().legend.clear() if self.plot.getPlotItem().legend else None
        for i, k in enumerate(self.selected_keys()):
            pts = [(ts, v) for ts, v, _ in self.db.series(self.drive_id, page, k, t0, t1) if v is not None]
            if pts:
                xs, ys = zip(*pts)
                self.plot.plot(xs, ys, pen=pg.mkPen(color(i), width=2), name=k)
        if keep_zoom:
            self.plot.setRange(xRange=vr[0], yRange=vr[1], padding=0)
        else:
            self.plot.enableAutoRange()
        self._drawn = True

    def _load_keys_once(self) -> None:
        if not getattr(self, "_loading", False):
            self._loading = True
            try:
                self._load_keys()
            finally:
                self._loading = False

    def refresh_live(self) -> None:
        if self.live.isChecked() and self.range.is_relative():
            self.refresh()

    def export(self) -> None:
        page = self.page.currentData()
        t0, t1 = self.range.range()
        rows = self.db.samples_range(self.drive_id, page, t0, t1, self.selected_keys() or None)
        do_export(self, f"tendencia_{page}.xlsx", ["Hora local", "Página", "Señal", "Valor", "Número"],
                  [(analysis.fmt_local(r["ts"]), page_label(page), r["key"], r["value"], r["num"]) for r in rows])


# --------------------------------------------------------------------------
class NetworkTab(QWidget):
    def __init__(self, db: Database, drive_id: int) -> None:
        super().__init__()
        self.db, self.drive_id = db, drive_id
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.page = QComboBox()
        self.page.addItem(page_label("ethernet_stats"), "ethernet_stats")
        self.page.addItem(page_label("network_stats"), "network_stats")
        self.range = RangeSelector(default_index=2)
        self.only_err = QCheckBox("Solo contadores de error")
        bar.addWidget(self.page)
        bar.addWidget(self.range)
        bar.addWidget(self.only_err)
        bar.addStretch(1)
        lay.addLayout(bar)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = DataTable(["Contador", "Valor actual", "Incremento último intervalo",
                                "Incremento en el rango", "Última lectura"], export_name="red")
        self.table.table.itemSelectionChanged.connect(self._draw_selected)
        split.addWidget(self.table)
        self.plot = time_plot()
        split.addWidget(self.plot)
        split.setSizes([360, 260])
        lay.addWidget(split, 1)
        lay.addWidget(QLabel("Los incrementos se calculan entre lecturas consecutivas; si un contador se "
                             "reinicia se toma el valor nuevo como incremento.", objectName="muted"))
        self.page.currentIndexChanged.connect(self.refresh)
        self.range.changed.connect(self.refresh)
        self.only_err.toggled.connect(self.refresh)

    def refresh(self) -> None:
        page = self.page.currentData()
        t0, t1 = self.range.range()
        latest = self.db.latest_values(self.drive_id, page)
        rows, colors, data = [], [], []
        for key, (ts, value, num) in latest.items():
            is_err = bool(NET_ERROR_RE.search(key))
            if self.only_err.isChecked() and not is_err:
                continue
            if num is None:
                rows.append([key, value, "", "", analysis.fmt_local(ts)])
                colors.append(None)
                data.append(key)
                continue
            inc = analysis.counter_increments([(a, b) for a, b, _ in self.db.series(self.drive_id, page, key, t0, t1)])
            last_inc = inc[-1][1] if inc else None
            total = sum(v for _, v in inc) if inc else None
            rows.append([key, (value, num), (f"{last_inc:g}" if last_inc is not None else "", last_inc),
                         (f"{total:g}" if total is not None else "", total), analysis.fmt_local(ts)])
            colors.append(theme.BAD if is_err and (total or 0) > 0 else None)
            data.append(key)
        self.table.set_rows(rows, colors, data)
        self._draw_selected()

    def _draw_selected(self) -> None:
        key = self.table.selected_data()
        self.plot.clear()
        if key is None:
            self.plot.setTitle("Selecciona un contador para ver su incremento por intervalo")
            return
        page = self.page.currentData()
        t0, t1 = self.range.range()
        inc = analysis.counter_increments([(a, b) for a, b, _ in self.db.series(self.drive_id, page, key, t0, t1)])
        self.plot.setTitle(f"{key}: incremento por intervalo")
        if not inc:
            return
        xs = np.array([a for a, _ in inc])
        ys = np.array([b for _, b in inc])
        width = max(1.0, float(np.median(np.diff(xs))) * 0.8) if len(xs) > 1 else 30.0
        brush = theme.BAD if NET_ERROR_RE.search(key) else theme.ACCENT
        self.plot.addItem(pg.BarGraphItem(x=xs, height=ys, width=width, brush=brush))
        self.plot.enableAutoRange()


# --------------------------------------------------------------------------
class InfoTab(QWidget):
    def __init__(self, db: Database, drive_id: int) -> None:
        super().__init__()
        self.db, self.drive_id = db, drive_id
        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Vertical)
        a = QWidget()
        al = QVBoxLayout(a)
        al.setContentsMargins(0, 0, 0, 0)
        al.addWidget(QLabel("Datos actuales del drive, motor y red", objectName="title"))
        self.current = DataTable(["Página", "Campo", "Valor", "Desde"], export_name="info_actual")
        al.addWidget(self.current)
        b = QWidget()
        bl = QVBoxLayout(b)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(QLabel("Historial de cambios", objectName="title"))
        self.history = DataTable(["Hora local", "Página", "Campo", "Antes", "Después"], export_name="info_historial")
        bl.addWidget(self.history)
        split.addWidget(a)
        split.addWidget(b)
        lay.addWidget(split)

    def refresh(self) -> None:
        self.current.set_rows([[page_label(r["page"]), r["key"], r["value"], (analysis.fmt_local(r["ts"]), r["ts"])]
                               for r in self.db.info_current(self.drive_id)])
        hist = self.db.info_history(self.drive_id)
        self.history.set_rows(
            [[(analysis.fmt_local(r["ts"]), r["ts"]), page_label(r["page"]), r["key"],
              "(nuevo)" if r["old_value"] is None else r["old_value"],
              "(ya no aparece)" if r["new_value"] is None else r["new_value"]] for r in hist],
            [theme.WARN if r["old_value"] is not None else None for r in hist])


class EventsTab(QWidget):
    def __init__(self, db: Database, drive_id: int) -> None:
        super().__init__()
        self.db, self.drive_id = db, drive_id
        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Vertical)
        a = QWidget()
        al = QVBoxLayout(a)
        al.setContentsMargins(0, 0, 0, 0)
        al.addWidget(QLabel("Eventos", objectName="title"))
        self.events = DataTable(["Hora local", "Tipo", "Mensaje", "Detalle"], export_name="eventos")
        al.addWidget(self.events)
        b = QWidget()
        bl = QVBoxLayout(b)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(QLabel("Errores de lectura o de formato (la app sigue con las demás páginas)", objectName="title"))
        self.errors = DataTable(["Hora local", "Página", "URL", "Error"], export_name="errores_lectura")
        bl.addWidget(self.errors)
        split.addWidget(a)
        split.addWidget(b)
        split.setSizes([450, 200])
        lay.addWidget(split)

    def refresh(self) -> None:
        ev = self.db.events(self.drive_id)
        self.events.set_rows([[(analysis.fmt_local(r["ts"]), r["ts"]), EVENT_TEXT.get(r["kind"], r["kind"]),
                               r["message"], r["details"] or ""] for r in ev],
                             [EVENT_COLORS.get(r["kind"]) for r in ev])
        self.errors.set_rows([[(analysis.fmt_local(r["ts"]), r["ts"]), page_label(r["page"] or ""), r["url"] or "",
                               r["error"] or ""] for r in self.db.parse_errors(self.drive_id)])


# --------------------------------------------------------------------------
class DriveDetail(QWidget):
    def __init__(self, db: Database, settings: Settings, drive_id: int) -> None:
        super().__init__()
        self.db, self.drive_id = db, drive_id
        lay = QVBoxLayout(self)
        head = QHBoxLayout()
        self.title = QLabel(objectName="title")
        self.status = QLabel(objectName="muted")
        self.enc = QLabel()
        head.addWidget(self.title)
        head.addSpacing(16)
        head.addWidget(self.status)
        head.addStretch(1)
        head.addWidget(self.enc)
        lay.addLayout(head)
        self.tabs = QTabWidget()
        self.faults = FaultsTab(db, settings, drive_id)
        self.trends = TrendsTab(db, drive_id)
        self.network = NetworkTab(db, drive_id)
        self.info = InfoTab(db, drive_id)
        self.events = EventsTab(db, drive_id)
        self.tabs.addTab(self.faults, "Fallas")
        self.tabs.addTab(self.trends, "Tendencias")
        self.tabs.addTab(self.network, "Red")
        self.tabs.addTab(self.info, "Info")
        self.tabs.addTab(self.events, "Eventos")
        lay.addWidget(self.tabs, 1)
        self._dirty = {"faults": True, "trends": True, "network": True, "info": True, "events": True}
        self._last_full = 0.0
        self.tabs.currentChanged.connect(lambda _i: self.refresh(force=True))

    def mark_dirty(self, what: str | None = None) -> None:
        for k in ([what] if what else self._dirty):
            self._dirty[k] = True

    def set_header(self, name: str, ip: str, state: str, message: str) -> None:
        self.title.setText(f"{name}  ·  {ip}")
        self.status.setText(message or theme.STATE_TEXT.get(state, state))
        self.status.setStyleSheet(f"color:{theme.STATE_COLORS.get(state, theme.MUTED)};")
        enc = self.db.latest_values(self.drive_id, "encoder")
        parts = []
        for k, (_ts, _v, n) in enc.items():
            if n is not None and (RSSI_RE.search(k) or QUALITY_RE.search(k)):
                c = theme.BAD if n < 100 else theme.OK
                parts.append(f"<span style='color:{theme.MUTED}'>{k}</span> <b style='color:{c}'>{n:g} %</b>")
        self.enc.setText(" &nbsp; ".join(parts))

    def refresh(self, force: bool = False) -> None:
        w = self.tabs.currentWidget()
        name = {self.faults: "faults", self.trends: "trends", self.network: "network",
                self.info: "info", self.events: "events"}[w]
        try:
            if name == "trends" and not force and not self._dirty["trends"]:
                self.trends.refresh_live()
                return
            if force or self._dirty[name]:
                self._dirty[name] = False
                w.refresh()
        except Exception:  # noqa: BLE001 - un dato raro no debe cerrar la ventana
            log.exception("Error al refrescar la pestaña %s", name)
