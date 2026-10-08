"""Parsers de las páginas del drive.

Todos parten de extract_rows() (genérico) y nunca suponen rutas: la URL de cada
página se descubre en el menú del drive (ver discovery.py).
"""

from .fault_log import FaultRecord, parse_fault_rows
from .generic import rows_to_kv
from .rows import Row, decode_body, extract_rows
from .values import format_duration, parse_cip_time, parse_duration, parse_number

__all__ = [
    "FaultRecord", "Row", "decode_body", "extract_rows", "format_duration",
    "parse_cip_time", "parse_duration", "parse_fault_rows", "parse_number", "rows_to_kv",
]
