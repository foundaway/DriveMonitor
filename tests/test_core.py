from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from drivemonitor import analysis
from drivemonitor.analysis import FaultPoint
from drivemonitor.db import Database
from drivemonitor.discovery import match_pages
from drivemonitor.export import export_table
from drivemonitor.http_client import DriveClient, UnsafeRequest, unsafe_reason
from drivemonitor.parsers import extract_rows, parse_fault_rows

MX = ZoneInfo("America/Mexico_City")


def ts_local(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=MX).timestamp()


# --- seguridad del cliente HTTP -------------------------------------------------

def test_client_blocks_non_get_methods():
    c = DriveClient("127.0.0.1:9")
    with pytest.raises(UnsafeRequest):
        c._session.post("http://127.0.0.1:9/x")
    with pytest.raises(UnsafeRequest):
        c._session.request("PUT", "http://127.0.0.1:9/x")
    assert not hasattr(c, "post")


@pytest.mark.parametrize("url", [
    "http://h/clearfaultlog.html", "http://h/faults/ClearFaults.htm", "http://h/reset.cgi",
    "http://h/x.cgi?a=1", "http://h/set_ip.html", "http://h/reboot", "https://h/x.html",
])
def test_unsafe_urls(url):
    assert unsafe_reason(url)


def test_client_rejects_other_hosts_and_unsafe_urls():
    c = DriveClient("10.0.0.5")
    with pytest.raises(UnsafeRequest):
        c.check("http://10.0.0.6/home.html")
    with pytest.raises(UnsafeRequest):
        c.check("/faults/clearfaultlog.html")
    assert c.check("diag/encoder.html") == "http://10.0.0.5/diag/encoder.html"


def test_client_timeout_raises_request_exception():
    c = DriveClient("127.0.0.1:9", timeout_s=0.5)  # puerto sin servicio
    with pytest.raises(requests.RequestException):
        c.get("/")


# --- descubrimiento ---------------------------------------------------------------

def test_match_pages_by_menu_label():
    menu = [("Home", "h"), ("Fault Logs", "section"), ("Fault Log", "fl"), ("Network Settings", "ns"),
            ("Network Statistics", "nst"), ("ENCODER DIAGNOSTICS", "enc"), ("Clear Fault Log", "clr")]
    m = match_pages(menu)
    assert m["fault_log"] == "fl"
    assert m["network_settings"] == "ns"
    assert m["network_stats"] == "nst"
    assert m["encoder"] == "enc"
    assert "clr" not in m.values()


# --- base de datos ----------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


def test_fault_dedup(db):
    did = db.add_drive("1.2.3.4", "D1")
    line = ("1. CipTime(GMT): Thu Oct 8 05:31:50 2026 | Uptime: 1 days, 12 h:52 m:44 s | "
            "CumulativeUptime: 1723 days, 13 h:15 m:3 s | FaultId: 55 | FaultSubCode: 0 | FLT S55 - VEL ERROR")
    [rec] = parse_fault_rows(extract_rows("f.html", f"<pre>{line}</pre>".encode(), "text/html"))
    assert db.insert_fault(did, rec, 1000.0) is not None
    assert db.insert_fault(did, rec, 2000.0) is None
    row = db.faults(did)[0]
    assert db.fault_count(did) == 1
    assert row["raw"] == line
    assert row["cip_time_utc"] == datetime.fromisoformat("2026-10-08T05:31:50+00:00").timestamp()


def test_info_records_only_changes(db):
    did = db.add_drive("1.2.3.4")
    first = db.update_info(did, "drive_info", [("FW", "1.0"), ("SN", "X")], 1.0)
    assert first == [("FW", None, "1.0"), ("SN", None, "X")]
    assert db.update_info(did, "drive_info", [("FW", "1.0"), ("SN", "X")], 2.0) == []
    assert db.update_info(did, "drive_info", [("FW", "1.1"), ("SN", "X")], 3.0) == [("FW", "1.0", "1.1")]
    assert len(db.info_history(did)) == 3


def test_drives_persist(tmp_path):
    d1 = Database(tmp_path / "p.db")
    d1.add_drive("172.23.22.95", "Línea 1")
    d1.close()
    d2 = Database(tmp_path / "p.db")
    assert [(r["ip"], r["name"]) for r in d2.drives()] == [("172.23.22.95", "Línea 1")]


def test_samples_and_purge(db):
    did = db.add_drive("1.2.3.4")
    db.insert_samples(did, "monitor", 100.0, [("Vel", "1500 rpm", 1500.0)])
    db.insert_samples(did, "monitor", 200.0, [("Vel", "1490 rpm", 1490.0)])
    assert db.series(did, "monitor", "Vel", 0, 300) == [(100.0, 1500.0, "1500 rpm"), (200.0, 1490.0, "1490 rpm")]
    assert db.latest_values(did, "monitor")["Vel"][2] == 1490.0
    assert db.purge_samples(did, "monitor", 150.0) == 1


# --- análisis -------------------------------------------------------------------

def test_pareto_hour_shift_and_tbf():
    f = [FaultPoint(ts_local(2026, 10, 8, 8, 0), 55, 0, "VEL ERROR"),
         FaultPoint(ts_local(2026, 10, 8, 8, 30), 55, 0, "VEL ERROR"),
         FaultPoint(ts_local(2026, 10, 8, 16, 0), 33, 1, "BUS"),
         FaultPoint(ts_local(2026, 10, 9, 2, 0), 55, 0, "VEL ERROR")]
    p = analysis.pareto(f)
    assert p[0] == ("55 VEL ERROR", 3, 75.0)
    assert p[1] == ("33.1 BUS", 1, 100.0)
    hours = analysis.by_hour(f)
    assert hours[8] == 2 and hours[16] == 1 and hours[2] == 1
    shifts = [{"name": "T1", "start": "07:00"}, {"name": "T2", "start": "15:00"}, {"name": "T3", "start": "23:00"}]
    assert analysis.by_shift(f, shifts) == [("T1", 2), ("T2", 1), ("T3", 1)]
    assert analysis.shift_of(ts_local(2026, 10, 8, 6, 59), shifts) == "T3"
    tbf = analysis.time_between(f, now=ts_local(2026, 10, 9, 3, 0))
    assert tbf.intervals_s == [1800, 7.5 * 3600, 10 * 3600]
    assert tbf.min_s == 1800 and tbf.since_last_s == 3600


def test_local_time_display():
    # 05:31:50 GMT = 23:31:50 del día anterior en Ciudad de México (UTC-6)
    t = datetime.fromisoformat("2026-10-08T05:31:50+00:00").timestamp()
    assert analysis.fmt_local(t) == "2026-10-07 23:31:50"


def test_counter_increments_handle_reset():
    s = [(1, 10.0), (2, 12.0), (3, 12.0), (4, 3.0), (5, 5.0)]
    assert analysis.counter_increments(s) == [(2, 2.0), (3, 0.0), (4, 3.0), (5, 2.0)]


# --- exportación ------------------------------------------------------------------

def test_export_csv_and_xlsx(tmp_path):
    import openpyxl
    rows = [["2026-10-08 05:31:50", 55, "FLT S55 - VEL ERROR"], ["2026-10-08 06:00:00", 33, "ñ"]]
    assert export_table(tmp_path / "f.csv", ["Hora", "Código", "Texto"], rows) == 2
    assert (tmp_path / "f.csv").read_text(encoding="utf-8-sig").splitlines()[0] == "Hora,Código,Texto"
    assert export_table(tmp_path / "f.xlsx", ["Hora", "Código", "Texto"], rows) == 2
    ws = openpyxl.load_workbook(tmp_path / "f.xlsx").active
    assert [c.value for c in ws[2]] == rows[0]


# --- nombres reales del drive (EncData/EncRSSI, ...) ---------------------------

@pytest.mark.parametrize("key,rssi,quality,fw", [
    ("EncData/EncRSSI", True, False, False),
    ("RSSI (%)", True, False, False),
    ("EncData/EncQualityMonitor", False, True, False),
    ("EncData/HF2DSLfwrev", False, False, True),
    ("FW REV", False, False, True),
    ("Firmware Revision", False, False, True),
    ("EncData/EncRevolution", False, False, False),
])
def test_key_detection_with_real_names(key, rssi, quality, fw):
    from drivemonitor.poller import FIRMWARE_RE, QUALITY_RE, RSSI_RE
    assert bool(RSSI_RE.search(key)) == rssi
    assert bool(QUALITY_RE.search(key)) == quality
    assert bool(FIRMWARE_RE.search(key)) == fw


def test_uptime_and_volatile_keys():
    from drivemonitor.poller import is_uptime_key, is_volatile_key
    assert is_uptime_key("Uptime") and is_uptime_key("DriveData/Uptime")
    assert not is_uptime_key("CumulativeUptime") and not is_uptime_key("X/CumUptime")
    assert is_volatile_key("EncData/EncTemperature") and is_volatile_key("SystemTime")
    assert not is_volatile_key("IP Address") and not is_volatile_key("ConnectionTimeout")
