"""Conversión de textos del drive: números, duraciones y fechas."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_NUM_RE = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?")
_DUR_PARTS = {
    "d": re.compile(r"(\d+)\s*d(?:ays?|ías?|ias?)?\b", re.I),
    "h": re.compile(r"(\d+)\s*h", re.I),
    "m": re.compile(r"(\d+)\s*m(?:in)?\b|(\d+)\s*m\s*:", re.I),
    "s": re.compile(r"(\d+)\s*s(?:ec)?\b", re.I),
}
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE_RE = re.compile(
    r"(?:[A-Za-z]{3}\s+)?([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})\s+(\d{4})")


def parse_number(text: str | None) -> float | None:
    """Primer número del texto: '1,234.5 rpm' -> 1234.5; '100 %' -> 100.0."""
    if not text:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_duration(text: str | None) -> int | None:
    """'1 days, 12 h:52 m:44 s' -> segundos. None si no parece una duración."""
    if not text:
        return None
    total, found = 0, False
    for unit, mult in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        m = _DUR_PARTS[unit].search(text)
        if m:
            total += int(next(g for g in m.groups() if g)) * mult
            found = True
    return total if found else None


def parse_cip_time(text: str | None) -> datetime | None:
    """'Thu Oct 8 05:31:50 2026' (GMT) -> datetime con zona UTC.
    No depende del idioma de Windows."""
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    try:
        return datetime(int(m.group(6)), mon, int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)), tzinfo=timezone.utc)
    except ValueError:
        return None


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(round(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d} d {h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"
