"""Filas -> pares clave/valor, para guardar TODOS los campos de cualquier página."""

from __future__ import annotations

from .rows import Row


def _clean_key(text: str) -> str:
    return text.strip().rstrip(":").strip() or "(sin nombre)"


def rows_to_kv(rows: list[Row]) -> list[tuple[str, str]]:
    """Reglas:
    - 2 celdas: clave = 1ª, valor = 2ª.
    - más celdas con una fila de encabezados (<th>) del mismo ancho:
      una clave por columna, 'Fila / Columna'.
    - más celdas sin encabezado: clave = 1ª, valor = resto unido con ' | '.
    - 1 celda: se guarda como 'Texto N' para no perderla.
    Claves repetidas reciben sufijo ' (2)', ' (3)'...
    """
    out: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    header: list[str] | None = None
    text_n = 0

    def add(key: str, value: str) -> None:
        n = seen.get(key, 0) + 1
        seen[key] = n
        out.append((key if n == 1 else f"{key} ({n})", value))

    for row in rows:
        cells = row.cells
        if row.header:
            header = cells if len(cells) > 2 else None
            continue
        if len(cells) == 1:
            text_n += 1
            add(f"Texto {text_n}", cells[0])
        elif len(cells) == 2:
            add(_clean_key(cells[0]), cells[1])
        elif header and len(header) == len(cells):
            base = _clean_key(cells[0])
            for h, v in zip(header[1:], cells[1:]):
                add(f"{base} / {_clean_key(h)}", v)
        else:
            add(_clean_key(cells[0]), " | ".join(cells[1:]))
    return out
