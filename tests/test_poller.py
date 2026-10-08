"""Prueba de integración del ciclo de consulta contra el drive SIMULADO."""

import threading
import time

import pytest

import drivemonitor.config as config
from drivemonitor.config import PAGES, Settings
from drivemonitor.db import Database
from drivemonitor.poller import DrivePoller, DriveRef
from mock_drive import MockDrive

NEW_FAULT = ("1. CipTime(GMT): Thu Oct 8 09:00:01 2026 | Uptime: 1 days, 16 h:20 m:55 s | "
             "CumulativeUptime: 1723 days, 16 h:43 m:14 s | FaultId: 33 | FaultSubCode: 1 | FLT S33 - BUS UNDERVOLT")


def wait_for(cond, timeout=15.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MIN_INTERVAL_S", 0.05)
    mock = MockDrive().start()
    settings = Settings(tmp_path / "settings.json")
    settings.data["intervals"] = {p.key: 0.2 for p in PAGES}
    settings.data.update(min_gap_s=0.02, max_backoff_s=0.4, request_timeout_s=1.0)
    db = Database(tmp_path / "test.db")
    drive_id = db.add_drive(mock.host, "Prueba")
    events: list[tuple[str, dict]] = []
    lock = threading.Lock()

    def emit(kind, payload):
        with lock:
            events.append((kind, payload))

    poller = DrivePoller(DriveRef(drive_id, "Prueba", mock.host), db, settings, emit)
    yield mock, db, drive_id, poller, events
    poller.stop()
    poller.join(5)
    mock.stop()


def kinds(events, kind):
    return [p for k, p in list(events) if k == kind]


def event_kinds(db, drive_id):
    return [r["kind"] for r in db.events(drive_id)]


def test_full_cycle(setup):
    mock, db, drive_id, poller, events = setup
    poller.start()

    # Descubrimiento: todas las páginas, y Monitor Signals resuelto a su XML.
    assert wait_for(lambda: len(db.page_urls(drive_id)) == len(PAGES))
    urls = db.page_urls(drive_id)
    assert urls["monitor"].endswith("/diag/monitor_data.xml")
    assert urls["fault_log"].endswith("/faults/faultlog.html")

    # Fault Log inicial: se guarda sin alertar.
    assert wait_for(lambda: db.fault_count(drive_id) == 1)
    f = db.faults(drive_id)[0]
    assert (f["fault_id"], f["sub_code"], f["cumulative_uptime_s"]) == (55, 0, 148914903)
    assert f["text"] == "FLT S55 - VEL ERROR"
    assert f["cip_time_text"] == "Thu Oct 8 05:31:50 2026"
    assert not [a for a in kinds(events, "alert") if a["kind"] == "new_fault"]

    # Falla nueva: se guarda una sola vez y alerta.
    with mock.lock:
        mock.state["faults"].insert(0, NEW_FAULT)
    assert wait_for(lambda: db.fault_count(drive_id) == 2)
    time.sleep(1.0)  # varias lecturas más del mismo log
    assert db.fault_count(drive_id) == 2
    assert len([a for a in kinds(events, "alert") if a["kind"] == "new_fault"]) == 1

    # Encoder y señales se guardan como muestras clave/valor.
    assert wait_for(lambda: db.latest_values(drive_id, "encoder").get("RSSI (%)"))
    assert db.latest_values(drive_id, "encoder")["RSSI (%)"][2] == 100.0
    assert db.latest_values(drive_id, "monitor")["Velocity Feedback"][2] == 1500.0

    # RSSI baja de 100 % -> evento y alerta (una sola vez mientras siga bajo).
    with mock.lock:
        mock.state["rssi"] = 92
    assert wait_for(lambda: "encoder_low" in event_kinds(db, drive_id))
    time.sleep(0.8)
    assert event_kinds(db, drive_id).count("encoder_low") == 1

    # Contador de errores de red se incrementa.
    with mock.lock:
        mock.state["crc"] = 3
    assert wait_for(lambda: "net_errors" in event_kinds(db, drive_id))
    ev = [r for r in db.events(drive_id) if r["kind"] == "net_errors"][0]
    assert "CRC Errors / Port 1: +3" in ev["details"]

    # Reinicio: el Uptime baja.
    with mock.lock:
        mock.state["uptime"] = "0 days, 0 h:01 m:10 s"
    assert wait_for(lambda: "reboot" in event_kinds(db, drive_id))

    # Cambio de firmware y de configuración de red.
    with mock.lock:
        mock.state["firmware"] = "13.002"
        mock.state["ip_address"] = "172.23.22.96"
    assert wait_for(lambda: "firmware_change" in event_kinds(db, drive_id))
    assert wait_for(lambda: "network_config_change" in event_kinds(db, drive_id))
    hist = [(r["key"], r["old_value"], r["new_value"]) for r in db.info_history(drive_id)]
    assert ("Firmware Revision", "13.001", "13.002") in hist

    # Pérdida y recuperación de comunicación.
    port = mock.port
    mock.stop()
    assert wait_for(lambda: "comm_lost" in event_kinds(db, drive_id), timeout=20)
    mock2 = MockDrive(port).start()
    try:
        assert wait_for(lambda: "comm_restored" in event_kinds(db, drive_id), timeout=20)
    finally:
        poller.stop()
        poller.join(5)
        mock2.stop()

    # Seguridad: solo GET y nunca la página de borrar el log.
    all_requests = mock.requests + mock2.requests
    assert all_requests
    assert {m for m, _ in all_requests} == {"GET"}
    assert not [p for _, p in all_requests if "clear" in p.lower()]


def test_unreachable_drive_never_crashes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MIN_INTERVAL_S", 0.05)
    settings = Settings(tmp_path / "s.json")
    settings.data.update(min_gap_s=0.02, max_backoff_s=0.3, request_timeout_s=0.3)
    db = Database(tmp_path / "t.db")
    port = MockDrive._free_port()  # nadie escucha aquí
    did = db.add_drive(f"127.0.0.1:{port}")
    states = []
    p = DrivePoller(DriveRef(did, "x", f"127.0.0.1:{port}"), db, settings,
                    lambda k, pl: states.append(pl.get("state")) if k == "status" else None)
    p.start()
    assert wait_for(lambda: "comm_lost" in event_kinds(db, did), timeout=10)
    assert "offline" in states
    p.stop()
    p.join(5)
    assert not p.is_alive()
