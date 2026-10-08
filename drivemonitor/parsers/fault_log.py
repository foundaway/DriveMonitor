"""Parser del Fault Log.

Formato conocido (una falla por renglón o fila):
  1. CipTime(GMT): Thu Oct 8 05:31:50 2026 | Uptime: 1 days, 12 h:52 m:44 s |
  CumulativeUptime: 1723 days, 13 h:15 m:3 s | FaultId: 55 | FaultSubCode: 0 |
  FLT S55 - VEL ERROR

También acepta una tabla con encabezados (CipTime, Uptime, FaultId, ...).
Los campos se reconocen por nombre, no por posición.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .rows import Row
from .values import parse_cip_time, parse_duration, parse_number

_INDEX_RE = re.compile(r"^\s*(\d+)\s*[.)]\s*")
_KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z ()_]{1,40}?)\s*:\s*(.*)$")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


FIELD_ALIASES = {
    "ciptimegmt": "cip_time", "ciptime": "cip_time", "time": "cip_time", "timestamp": "cip_time",
    "uptime": "uptime",
    "cumulativeuptime": "cumulative_uptime", "cumuptime": "cumulative_uptime",
    "faultid": "fault_id", "faultcode": "fault_id", "code": "fault_id",
    "faultsubcode": "sub_code", "subcode": "sub_code",
    "text": "text", "description": "text", "fault": "text", "faulttext": "text",
}


@dataclass
class FaultRecord:
    index: int | None
    cip_time_text: str
    cip_time_utc: datetime | None
    uptime_s: int | None
    cumulative_uptime_s: int | None
    fault_id: int | None
    sub_code: int | None
    text: str
    raw: str

    @property
    def dedup_key(self) -> str:
        """CumulativeUptime + FaultId + SubCode (si falta el uptime, la hora)."""
        anchor = (str(self.cumulative_uptime_s) if self.cumulative_uptime_s is not None
                  else f"t:{self.cip_time_text}")
        return f"{anchor}|{self.fault_id}|{self.sub_code}"


def _to_int(v: str | None) -> int | None:
    n = parse_number(v)
    return int(n) if n is not None else None


def _record(fields: dict[str, str], extra_text: list[str], index: int | None, raw: str) -> FaultRecord | None:
    if "fault_id" not in fields and "cip_time" not in fields:
        return None
    text = fields.get("text") or " | ".join(t for t in extra_text if t)
    return FaultRecord(
        index=index,
        cip_time_text=fields.get("cip_time", "").strip(),
        cip_time_utc=parse_cip_time(fields.get("cip_time")),
        uptime_s=parse_duration(fields.get("uptime")),
        cumulative_uptime_s=parse_duration(fields.get("cumulative_uptime")),
        fault_id=_to_int(fields.get("fault_id")),
        sub_code=_to_int(fields.get("sub_code")),
        text=text.strip(),
        raw=raw,
    )


def _parse_line(parts: list[str]) -> FaultRecord | None:
    raw = " | ".join(parts)
    fields: dict[str, str] = {}
    extra: list[str] = []
    index = None
    for i, part in enumerate(parts):
        p = part.strip()
        if i == 0:
            m = _INDEX_RE.match(p)
            if m:
                index = int(m.group(1))
                p = p[m.end():]
        m = _KV_RE.match(p)
        name = FIELD_ALIASES.get(_norm(m.group(1))) if m else None
        if name and name not in fields:
            fields[name] = m.group(2).strip()
        elif p:
            extra.append(p)
    return _record(fields, extra, index, raw)


def parse_fault_rows(rows: list[Row]) -> list[FaultRecord]:
    """Devuelve las fallas encontradas, en el orden en que las lista el drive."""
    out: list[FaultRecord] = []
    header: list[str | None] | None = None
    for row in rows:
        if row.header:
            mapped = [FIELD_ALIASES.get(_norm(c)) for c in row.cells]
            header = mapped if sum(1 for m in mapped if m) >= 2 else None
            continue
        rec = None
        if header and len(row.cells) == len(header):
            fields = {h: v for h, v in zip(header, row.cells) if h}
            extra = [v for h, v in zip(header, row.cells) if not h]
            index = None
            if extra and re.fullmatch(r"\d+\.?", extra[0].strip()):
                index = int(extra.pop(0).strip(". "))
            rec = _record(fields, extra, index, row.text())
        if rec is None:
            # Una falla puede venir partida en celdas o en un solo texto con ' | '.
            parts = [p for c in row.cells for p in c.split(" | ")]
            rec = _parse_line(parts)
        if rec is not None:
            out.append(rec)
    return out
