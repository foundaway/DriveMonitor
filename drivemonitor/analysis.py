"""Cálculos para las vistas: Pareto, distribución por hora y turno, tiempo
entre fallas e incrementos de contadores. Funciones puras, sin Qt."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean, median
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from .config import LOCAL_TZ_NAME

LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)


def to_local(ts: float | None) -> datetime | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(LOCAL_TZ)


def fmt_local(ts: float | None, with_seconds: bool = True) -> str:
    d = to_local(ts)
    if d is None:
        return ""
    return d.strftime("%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M")


def utc_offset_s(ts: float | None = None) -> int:
    d = to_local(ts if ts is not None else datetime.now(timezone.utc).timestamp())
    return int(d.utcoffset().total_seconds())


@dataclass
class FaultPoint:
    t: float                # epoch UTC de la falla
    code: int | None
    sub: int | None
    text: str


def fault_label(code: int | None, sub: int | None, text: str = "") -> str:
    base = "?" if code is None else str(code)
    if sub:
        base += f".{sub}"
    return f"{base} {text}".strip()


def pareto(faults: Iterable[FaultPoint]) -> list[tuple[str, int, float]]:
    """[(etiqueta, cantidad, % acumulado)] ordenado de mayor a menor."""
    texts: dict[tuple, str] = {}
    counts: Counter = Counter()
    for f in faults:
        key = (f.code, f.sub)
        counts[key] += 1
        texts.setdefault(key, f.text)
    total = sum(counts.values())
    out, acc = [], 0
    for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        acc += n
        out.append((fault_label(key[0], key[1], texts[key]), n, 100.0 * acc / total))
    return out


def by_hour(faults: Iterable[FaultPoint]) -> list[int]:
    hours = [0] * 24
    for f in faults:
        hours[to_local(f.t).hour] += 1
    return hours


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def shift_of(ts: float, shifts: Sequence[dict]) -> str:
    """Nombre del turno de una hora local. Los turnos se definen por su hora de
    inicio; cada uno dura hasta el inicio del siguiente (con vuelta a medianoche)."""
    if not shifts:
        return "Sin turno"
    d = to_local(ts)
    minute = d.hour * 60 + d.minute
    ordered = sorted(shifts, key=lambda s: _minutes(s["start"]))
    current = ordered[-1]["name"]  # antes del primer inicio sigue el último turno del día anterior
    for s in ordered:
        if minute >= _minutes(s["start"]):
            current = s["name"]
    return current


def by_shift(faults: Iterable[FaultPoint], shifts: Sequence[dict]) -> list[tuple[str, int]]:
    counts = Counter(shift_of(f.t, shifts) for f in faults)
    return [(s["name"], counts.get(s["name"], 0)) for s in shifts]


@dataclass
class TbfStats:
    intervals_s: list[float]
    mean_s: float | None
    median_s: float | None
    min_s: float | None
    max_s: float | None
    since_last_s: float | None


def time_between(faults: Iterable[FaultPoint], now: float) -> TbfStats:
    ts = sorted(f.t for f in faults)
    iv = [b - a for a, b in zip(ts, ts[1:])]
    return TbfStats(
        intervals_s=iv,
        mean_s=mean(iv) if iv else None,
        median_s=median(iv) if iv else None,
        min_s=min(iv) if iv else None,
        max_s=max(iv) if iv else None,
        since_last_s=(now - ts[-1]) if ts else None,
    )


def counter_increments(series: Sequence[tuple[float, float | None]]) -> list[tuple[float, float]]:
    """[(ts, valor acumulado)] -> [(ts, incremento desde la lectura anterior)].
    Si el contador baja (reinicio del drive o del contador) el incremento es el
    valor nuevo, que es lo contado desde el reinicio."""
    out = []
    prev = None
    for ts, v in series:
        if v is None:
            continue
        if prev is not None:
            out.append((ts, v - prev if v >= prev else v))
        prev = v
    return out
