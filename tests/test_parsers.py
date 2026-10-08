import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drivemonitor.parsers import (extract_rows, parse_cip_time, parse_duration, parse_fault_rows,
                                  parse_number, rows_to_kv)
from drivemonitor.parsers.rows import Row

FAULT_LINE = ("1. CipTime(GMT): Thu Oct 8 05:31:50 2026 | Uptime: 1 days, 12 h:52 m:44 s | "
              "CumulativeUptime: 1723 days, 13 h:15 m:3 s | FaultId: 55 | FaultSubCode: 0 | "
              "FLT S55 - VEL ERROR")
RAW = Path(__file__).parent / "fixtures" / "raw"


# --- valores -----------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("100", 100.0), ("100 %", 100.0), ("1,234.5 rpm", 1234.5), ("-3.25 A", -3.25),
    ("11.9", 11.9), ("2e3", 2000.0), ("No", None), ("", None), (None, None),
])
def test_parse_number(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("1 days, 12 h:52 m:44 s", 132764),
    ("1723 days, 13 h:15 m:3 s", 1723 * 86400 + 13 * 3600 + 15 * 60 + 3),
    ("0 days, 0 h:01 m:10 s", 70),
    ("12 h:00 m:00 s", 43200),
    ("Running", None),
])
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


def test_parse_cip_time_is_utc_and_locale_independent():
    assert parse_cip_time("Thu Oct 8 05:31:50 2026") == datetime(2026, 10, 8, 5, 31, 50, tzinfo=timezone.utc)
    assert parse_cip_time("Thu Oct  8 05:31:50 2026").day == 8
    assert parse_cip_time("basura") is None


# --- filas y clave/valor ---------------------------------------------------------

def test_html_table_with_header_columns():
    html = (b"<table><tr><th>Counter</th><th>Port 1</th><th>Port 2</th></tr>"
            b"<tr><td>CRC Errors</td><td>3</td><td>0</td></tr></table>")
    assert rows_to_kv(extract_rows("x.html", html, "text/html")) == [
        ("CRC Errors / Port 1", "3"), ("CRC Errors / Port 2", "0")]


def test_encoder_example_fields():
    html = ("<table><tr><td>Serial No</td><td>12345678</td></tr>"
            "<tr><td>Temperature (&deg;C)</td><td>41</td></tr>"
            "<tr><td>RSSI (%)</td><td>100</td></tr>"
            "<tr><td>Hiperface to DSL adapter detected</td><td>No</td></tr>"
            "<tr><td>FW REV</td><td>1.4</td></tr></table>").encode()
    kv = dict(rows_to_kv(extract_rows("enc.html", html, "text/html")))
    assert kv["Temperature (°C)"] == "41"
    assert kv["RSSI (%)"] == "100"
    assert kv["Hiperface to DSL adapter detected"] == "No"


def test_kv_keeps_everything_and_dedups_keys():
    rows = [Row(["A", "1"]), Row(["A", "2"]), Row(["solo texto"]), Row(["B:", "x", "y"])]
    assert rows_to_kv(rows) == [("A", "1"), ("A (2)", "2"), ("Texto 1", "solo texto"), ("B", "x | y")]


def test_xml_and_json():
    xml = (b"<?xml version='1.0'?><signals><sig name='vel' unit='rpm'>1500.2</sig>"
           b"<bus>650</bus><meta rev='2'/></signals>")
    assert rows_to_kv(extract_rows("d.xml", xml, "text/xml")) == [
        ("vel", "1500.2"), ("vel@unit", "rpm"), ("signals/bus", "650"), ("signals/meta@rev", "2")]
    assert rows_to_kv(extract_rows("d.json", b'{"a": {"b": 1}}', "application/json")) == [("a.b", "1")]


@pytest.mark.parametrize("body", [b"", b"\xff\xfe\x00<<<", b"<table><tr><td>sin cerrar", b"{malo",
                                  b"<?xml <x>", b"<html><script>var a='<td>';</script></html>"])
def test_garbage_never_raises(body):
    assert isinstance(extract_rows("x", body, ""), list)
    assert isinstance(rows_to_kv(extract_rows("x", body, "")), list)
    assert isinstance(parse_fault_rows(extract_rows("x", body, "")), list)


# --- Fault Log ---------------------------------------------------------------

def test_fault_line_example():
    rows = extract_rows("faultlog.html", f"<pre>{FAULT_LINE}</pre>".encode(), "text/html")
    [f] = parse_fault_rows(rows)
    assert f.index == 1
    assert f.cip_time_utc == datetime(2026, 10, 8, 5, 31, 50, tzinfo=timezone.utc)
    assert f.uptime_s == 132764
    assert f.cumulative_uptime_s == 148914903
    assert (f.fault_id, f.sub_code) == (55, 0)
    assert f.text == "FLT S55 - VEL ERROR"
    assert f.raw == FAULT_LINE
    assert f.dedup_key == "148914903|55|0"


def test_fault_log_as_table_with_headers():
    html = ("<table><tr><th>#</th><th>CipTime(GMT)</th><th>Uptime</th><th>CumulativeUptime</th>"
            "<th>FaultId</th><th>FaultSubCode</th><th>Text</th></tr>"
            "<tr><td>1.</td><td>Thu Oct 8 05:31:50 2026</td><td>1 days, 12 h:52 m:44 s</td>"
            "<td>1723 days, 13 h:15 m:3 s</td><td>55</td><td>0</td><td>FLT S55 - VEL ERROR</td></tr>"
            "</table>").encode()
    [f] = parse_fault_rows(extract_rows("f.html", html, "text/html"))
    assert (f.fault_id, f.sub_code, f.cumulative_uptime_s, f.index) == (55, 0, 148914903, 1)
    assert f.text == "FLT S55 - VEL ERROR"


def test_fault_split_in_cells():
    cells = [p for p in FAULT_LINE.split(" | ")]
    html = ("<table><tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr></table>").encode()
    [f] = parse_fault_rows(extract_rows("f.html", html, "text/html"))
    assert f.dedup_key == "148914903|55|0"


def test_non_fault_rows_ignored():
    rows = extract_rows("f.html", b"<h2>Fault Log</h2><p>No faults</p>", "text/html")
    assert parse_fault_rows(rows) == []


# --- capturas reales del drive (paso 1), si existen ----------------------------

def _raw_files():
    if not (RAW / "manifest.json").exists():
        return []
    manifest = json.loads((RAW / "manifest.json").read_text(encoding="utf-8"))
    return [f for f in manifest.get("fetched", []) if f.get("status") == 200]


@pytest.mark.skipif(not _raw_files(), reason="sin capturas reales en tests/fixtures/raw")
@pytest.mark.parametrize("entry", _raw_files(), ids=lambda e: e["url"])
def test_real_captures_parse_without_errors(entry):
    body = (RAW / entry["file"]).read_bytes()
    rows = extract_rows(entry["url"], body, entry.get("content_type", ""))
    rows_to_kv(rows)
    faults = parse_fault_rows(rows)
    if "fault log" in (entry.get("label") or "").lower() and b"FaultId" in body:
        assert faults, "El Fault Log real no se pudo interpretar"
