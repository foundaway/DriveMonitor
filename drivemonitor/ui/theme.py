"""Tema oscuro de estilo industrial."""

from __future__ import annotations

BG = "#16191d"
PANEL = "#1f2328"
PANEL_2 = "#272c33"
BORDER = "#363c45"
FG = "#e6e8eb"
MUTED = "#8b949e"
ACCENT = "#3d8bd9"
OK = "#3fb950"
WARN = "#d29922"
BAD = "#f85149"
IDLE = "#6e7681"

SERIES_COLORS = ["#4ea1ff", "#f0883e", "#3fb950", "#d2a8ff", "#ff7b72", "#e3b341",
                 "#39c5cf", "#bc8cff", "#ffa198", "#7ee787"]

STATE_COLORS = {"online": OK, "offline": BAD, "connecting": WARN, "discovering": WARN, "stopped": IDLE}
STATE_TEXT = {"online": "En línea", "offline": "Sin comunicación", "connecting": "Conectando…",
              "discovering": "Buscando páginas…", "stopped": "Detenido"}

QSS = f"""
* {{ font-family: "Segoe UI", "DejaVu Sans", sans-serif; font-size: 10pt; }}
QMainWindow, QDialog, QWidget {{ background: {BG}; color: {FG}; }}
QToolBar {{ background: {PANEL}; border: none; border-bottom: 1px solid {BORDER}; spacing: 6px; padding: 4px; }}
QToolButton {{ background: transparent; color: {FG}; padding: 5px 10px; border-radius: 4px; }}
QToolButton:hover {{ background: {PANEL_2}; }}
QPushButton {{ background: {PANEL_2}; color: {FG}; border: 1px solid {BORDER}; border-radius: 4px; padding: 5px 12px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {IDLE}; border-color: {PANEL_2}; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateTimeEdit, QTimeEdit, QPlainTextEdit {{
    background: {PANEL}; color: {FG}; border: 1px solid {BORDER}; border-radius: 4px; padding: 4px; }}
QComboBox QAbstractItemView {{ background: {PANEL}; color: {FG}; selection-background-color: {ACCENT}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; top: -1px; }}
QTabBar::tab {{ background: {PANEL}; color: {MUTED}; padding: 7px 16px; border: 1px solid {BORDER};
    border-bottom: none; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {PANEL_2}; color: {FG}; border-top: 2px solid {ACCENT}; }}
QTableWidget, QTableView, QListWidget, QTreeWidget {{ background: {PANEL}; alternate-background-color: {PANEL_2};
    color: {FG}; gridline-color: {BORDER}; border: 1px solid {BORDER}; selection-background-color: #2f5d8a; }}
QHeaderView::section {{ background: {PANEL_2}; color: {FG}; padding: 4px; border: none;
    border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER}; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 6px; margin-top: 12px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; color: {MUTED}; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; }}
QFrame#card[alert="true"] {{ border: 2px solid {BAD}; }}
QFrame#card QLabel {{ background: transparent; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#title {{ font-size: 13pt; font-weight: 600; }}
QLabel#big {{ font-size: 16pt; font-weight: 600; }}
QScrollArea {{ border: none; }}
QSplitter::handle {{ background: {BORDER}; }}
QStatusBar {{ background: {PANEL}; color: {MUTED}; }}
QCheckBox {{ spacing: 6px; }}
QToolTip {{ background: {PANEL_2}; color: {FG}; border: 1px solid {BORDER}; }}
"""


def apply(app) -> None:
    import pyqtgraph as pg

    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    pg.setConfigOptions(antialias=True, background=PANEL, foreground=FG)
