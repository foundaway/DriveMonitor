"""Extracción genérica de filas desde HTML, XML, JSON o texto.

No supone el formato de ninguna página: convierte lo que llegue en filas de
celdas de texto. Los parsers específicos y el almacenamiento clave/valor parten
de aquí.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser

BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
              "table", "tr", "hr", "form", "fieldset", "legend", "dt", "dd", "pre", "center"}


@dataclass
class Row:
    cells: list[str]
    header: bool = False          # fila hecha solo de <th>
    source: str = "table"         # "table" | "text" | "xml" | "json"
    extra: dict = field(default_factory=dict)

    def text(self) -> str:
        return " | ".join(self.cells)


def split_key_value(text: str) -> list[str]:
    """'a | b | c' -> ['a', 'b', 'c']; 'Clave: valor' -> ['Clave', 'valor'];
    cualquier otro texto queda en una celda."""
    if " | " in text:
        return [part.strip() for part in text.split(" | ")]
    m = re.match(r"^([^:|]{1,60}?)\s*[:=]\s+(.+)$", text)
    return [m.group(1), m.group(2)] if m else [text]


class _RowExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[Row] = []
        self._skip = 0
        self._row_stack: list[tuple[list[str], list[bool]] | None] = []
        self._cell: list[str] | None = None
        self._cell_is_th = False
        self._loose: list[str] = []

    @staticmethod
    def _clean(parts: list[str]) -> str:
        return " ".join("".join(parts).split())

    def _flush_loose(self) -> None:
        lines = "".join(self._loose).splitlines()
        self._loose = []
        for line in lines:
            text = " ".join(line.split())
            if text:
                self.rows.append(Row(split_key_value(text), source="text"))

    def _close_cell(self) -> None:
        if self._cell is not None and self._row_stack and self._row_stack[-1] is not None:
            cells, kinds = self._row_stack[-1]
            cells.append(self._clean(self._cell))
            kinds.append(self._cell_is_th)
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row_stack and self._row_stack[-1] is not None:
            cells, kinds = self._row_stack[-1]
            while cells and not cells[-1]:
                cells.pop()
                kinds.pop()
            if any(cells):
                self.rows.append(Row(cells, header=bool(kinds) and all(kinds)))
            self._row_stack[-1] = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
            return
        if tag == "table":
            self._close_cell()
            self._flush_loose()
            self._row_stack.append(None)
        elif tag == "tr" and self._row_stack:
            self._close_row()
            self._row_stack[-1] = ([], [])
        elif tag in ("td", "th") and self._row_stack:
            self._close_cell()
            if self._row_stack[-1] is None:
                self._row_stack[-1] = ([], [])
            self._cell = []
            self._cell_is_th = tag == "th"
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")
        elif tag in BLOCK_TAGS and self._cell is None:
            self._flush_loose()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_row()
        elif tag == "table" and self._row_stack:
            self._close_row()
            self._row_stack.pop()
        elif tag in BLOCK_TAGS and self._cell is None:
            self._flush_loose()

    def handle_data(self, data):
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        else:
            self._loose.append(data)

    def close(self):
        super().close()
        while self._row_stack:
            self._close_row()
            self._row_stack.pop()
        self._flush_loose()


def decode_body(body: bytes, content_type: str = "") -> str:
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8"]:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    return body.decode("latin-1", errors="replace")


def _flatten_json(obj, prefix: str, rows: list[Row]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten_json(v, f"{prefix}.{k}" if prefix else str(k), rows)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten_json(v, f"{prefix}[{i}]", rows)
    else:
        rows.append(Row([prefix or "valor", "" if obj is None else str(obj)], source="json"))


def _is_html(url: str, content_type: str) -> bool:
    return "html" in (content_type or "").lower() or url.lower().endswith((".htm", ".html", ".shtml", "/"))


def extract_rows(url: str, body: bytes, content_type: str = "") -> list[Row]:
    """Filas de una respuesta HTML, XML, JSON o texto. Nunca lanza excepción."""
    text = decode_body(body, content_type)
    low_ct = (content_type or "").lower()
    stripped = text.lstrip()
    try:
        if "json" in low_ct or url.lower().endswith(".json") or stripped[:1] in ("{", "["):
            rows: list[Row] = []
            _flatten_json(json.loads(text), "", rows)
            return rows
    except ValueError:
        pass
    try:
        if "xml" in low_ct or url.lower().endswith(".xml") or (
                stripped.startswith("<?xml") and "<html" not in stripped[:500].lower()):
            rows = []
            root = ET.fromstring(text.encode("utf-8"))

            def walk(el, path):
                tag = el.tag.split("}")[-1]
                p = f"{path}/{tag}" if path else tag
                text = " ".join((el.text or "").split())
                # <sig name="Velocity">1500</sig> -> clave = nombre del elemento
                name_attr = next((a for a in ("name", "label", "id", "key") if el.attrib.get(a)), None)
                if text and name_attr:
                    label = el.attrib[name_attr]
                    rows.append(Row([label, text], source="xml"))
                    for k, v in el.attrib.items():
                        if k != name_attr:
                            rows.append(Row([f"{label}@{k}", v], source="xml"))
                else:
                    for k, v in el.attrib.items():
                        rows.append(Row([f"{p}@{k}", v], source="xml"))
                    if text:
                        rows.append(Row([p, text], source="xml"))
                for ch in el:
                    walk(ch, p)

            walk(root, "")
            return rows
    except ET.ParseError:
        pass
    try:
        if _is_html(url, content_type) or "<" in stripped[:200]:
            ex = _RowExtractor()
            ex.feed(text)
            ex.close()
            return ex.rows
    except Exception:  # noqa: BLE001 - HTML raro: seguir con texto plano
        pass
    return [Row(split_key_value(" ".join(line.split())), source="text")
            for line in text.splitlines() if line.strip()]
