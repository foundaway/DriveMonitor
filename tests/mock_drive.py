"""Servidor web SIMULADO de un Kinetix 5500, solo para pruebas.

NO reproduce el HTML real del drive (aún no se tiene); usa los nombres de menú
y los formatos de ejemplo conocidos. Cuando se capturen las páginas reales, las
pruebas de parsers deben usar tests/fixtures/raw/.

Registra cada petición (método, ruta) para verificar que la app solo hace GET.
"""

from __future__ import annotations

import http.server
import socket
import threading
from email.utils import formatdate

MENU = """<html><body><ul>
<li><a href="home.html">Home</a></li>
<li>Diagnostics<ul>
<li><a href="diag/drive_info.html">Drive Information</a></li>
<li><a href="diag/motor.html">Motor Diagnostics</a></li>
<li><a href="diag/encoder.html">Encoder Diagnostics</a></li>
<li><a href="diag/net_settings.html">Network Settings</a></li>
<li><a href="diag/eth_stats.html">Ethernet Statistics</a></li>
<li><a href="diag/net_stats.html">Network Statistics</a></li>
<li><a href="diag/monitor.html">Monitor Signals</a></li></ul></li>
<li>Fault Logs<ul><li><a href="faults/faultlog.html">Fault Log</a></li>
<li><a href="faults/clearfaultlog.html">Clear Fault Log</a></li></ul></li>
</ul></body></html>"""


def table(rows: list[tuple[str, object]]) -> str:
    return "<table>" + "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows) + "</table>"


class MockDrive:
    def __init__(self, port: int = 0) -> None:
        self.state = {
            "uptime": "1 days, 12 h:52 m:44 s",
            "firmware": "13.001",
            "ip_address": "172.23.22.95",
            "rssi": 100, "quality": 100, "temperature": 41,
            "crc": 0, "collisions": 0, "packets_in": 1000,
            "velocity": 1500.0,
            "faults": [
                "1. CipTime(GMT): Thu Oct 8 05:31:50 2026 | Uptime: 1 days, 12 h:52 m:44 s | "
                "CumulativeUptime: 1723 days, 13 h:15 m:3 s | FaultId: 55 | FaultSubCode: 0 | FLT S55 - VEL ERROR",
            ],
        }
        self.requests: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.port = port or self._free_port()
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def page(self, path: str) -> str | None:
        s = self.state
        pages = {
            "/": '<html><head><title>Kinetix 5500</title></head><frameset cols="220,*">'
                 '<frame src="menu.html"><frame src="home.html"></frameset></html>',
            "/menu.html": MENU,
            "/home.html": "<h1>Home</h1>" + table([("Product Name", "2198-H008-ERS"), ("Status", "Running"),
                                                   ("Uptime", s["uptime"])]),
            "/diag/drive_info.html": "<h2>Drive Information</h2>" + table([
                ("Catalog Number", "2198-H008-ERS"), ("Firmware Revision", s["firmware"]),
                ("Serial Number", "D0A1B2C3")]),
            "/diag/motor.html": table([("Motor Catalog", "VPL-B1003T"), ("Rated Current (A)", "2.9")]),
            "/diag/encoder.html": table([
                ("Serial No", "12345678"), ("Resolution (Counts)", "262144"), ("Revolutions", "4096"),
                ("Supply Voltage (V)", "11.9"), ("Temperature (&deg;C)", s["temperature"]),
                ("RSSI (%)", s["rssi"]), ("Quality Monitor (%)", s["quality"]),
                ("Hiperface to DSL adapter detected", "No"), ("FW REV", "1.4")]),
            "/diag/net_settings.html": table([("IP Address", s["ip_address"]), ("Subnet Mask", "255.255.255.0"),
                                              ("Gateway", "172.23.22.1")]),
            "/diag/eth_stats.html": "<table><tr><th>Counter</th><th>Port 1</th><th>Port 2</th></tr>"
                                    f"<tr><td>CRC Errors</td><td>{s['crc']}</td><td>0</td></tr>"
                                    f"<tr><td>Collisions</td><td>{s['collisions']}</td><td>0</td></tr>"
                                    f"<tr><td>Packets In</td><td>{s['packets_in']}</td><td>500</td></tr></table>",
            "/diag/net_stats.html": table([("Lost Packets", 0), ("Connections Open", 2)]),
            # Página que carga sus datos por JavaScript desde un XML.
            "/diag/monitor.html": "<h2>Monitor Signals</h2><div id='t'></div>"
                                  "<script>setInterval(function(){load('monitor_data.xml')},2000);</script>",
            "/diag/monitor_data.xml": f"<?xml version='1.0'?><signals><sig name='Velocity Feedback'>"
                                      f"{s['velocity']:.2f}</sig><sig name='DC Bus Voltage'>650.0</sig></signals>",
            "/faults/faultlog.html": "<h2>Fault Log</h2><pre>" + "\n".join(s["faults"]) + "</pre>",
            "/faults/clearfaultlog.html": "<p>FAULT LOG CLEARED</p>",
        }
        return pages.get(path)

    def start(self) -> "MockDrive":
        drive = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _record(self):
                with drive.lock:
                    drive.requests.append((self.command, self.path))

            def do_GET(self):
                self._record()
                with drive.lock:
                    body = drive.page(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = body.encode("utf-8")
                self.send_response(200)
                ctype = "text/xml" if self.path.endswith(".xml") else "text/html; charset=utf-8"
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Date", formatdate(usegmt=True))
                self.end_headers()
                self.wfile.write(data)

            def _deny(self):
                self._record()
                self.send_response(405)
                self.end_headers()

            do_POST = do_PUT = do_DELETE = do_PATCH = _deny

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.allow_reuse_address = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


if __name__ == "__main__":
    import sys
    import time

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    MockDrive(port).start()
    print(f"Drive simulado en http://127.0.0.1:{port}/ (Ctrl+C para salir)")
    while True:
        time.sleep(1)
