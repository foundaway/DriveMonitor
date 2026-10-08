"""Pruebas de la extracción genérica de tools/capture_drive.py (solo biblioteca estándar).

Ejecutar:  python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import capture_drive as cd  # noqa: E402

FAULT_ROW = ("1. CipTime(GMT): Thu Oct 8 05:31:50 2026 | Uptime: 1 days, 12 h:52 m:44 s | "
             "CumulativeUptime: 1723 days, 13 h:15 m:3 s | FaultId: 55 | FaultSubCode: 0 | "
             "FLT S55 - VEL ERROR")


class ExtractRowsTest(unittest.TestCase):
    def test_html_table_rows_and_cells(self):
        html = (b"<html><body><h1>Encoder</h1><table>"
                b"<tr><th>Campo</th><th>Valor</th></tr>"
                b"<tr><td>RSSI (%)</td><td>100</td></tr>"
                b"<tr><td>Temperature (&deg;C)</td><td> 41 </td></tr>"
                b"</table><script>var x = '<td>no</td>';</script></body></html>")
        rows = cd.extract_rows("http://d/enc.html", html, "text/html")
        self.assertEqual(rows, [["Encoder"], ["Campo", "Valor"], ["RSSI (%)", "100"],
                                ["Temperature (°C)", "41"]])

    def test_pre_fault_log_one_row_per_fault_split_by_pipes(self):
        body = ("<pre>" + FAULT_ROW + "\n2. CipTime(GMT): Wed Oct 7 22:10:05 2026 | FaultId: 33</pre>").encode()
        rows = cd.extract_rows("http://d/faultlog.html", body, "text/html")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "1. CipTime(GMT): Thu Oct 8 05:31:50 2026")
        self.assertEqual(rows[0][-1], "FLT S55 - VEL ERROR")
        self.assertIn("FaultId: 55", rows[0])

    def test_key_value_text(self):
        rows = cd.extract_rows("http://d/home.html", b"<p>Status: Running</p><p>Hola</p>", "text/html")
        self.assertEqual(rows, [["Status", "Running"], ["Hola"]])

    def test_nested_tables(self):
        html = b"<table><tr><td><table><tr><td>A</td><td>1</td></tr></table></td></tr><tr><td>B</td></tr></table>"
        rows = cd.extract_rows("http://d/x.html", html, "text/html")
        self.assertIn(["A", "1"], rows)
        self.assertIn(["B"], rows)

    def test_xml(self):
        xml = b"<?xml version='1.0'?><signals><sig name='vel'>1500.2</sig><bus>650</bus></signals>"
        rows = cd.extract_rows("http://d/data.xml", xml, "text/xml")
        self.assertIn(["signals/sig@name", "vel"], rows)
        self.assertIn(["signals/sig", "1500.2"], rows)
        self.assertIn(["signals/bus", "650"], rows)

    def test_json(self):
        rows = cd.extract_rows("http://d/data.json", b'{"a": {"b": 1}, "c": [true, null]}', "application/json")
        self.assertEqual(rows, [["a.b", "1"], ["c[0]", "True"], ["c[1]", ""]])

    def test_garbage_never_raises(self):
        for body in (b"", b"\xff\xfe\x00<<<", b"<table><tr><td>sin cerrar", b"{malo", b"<?xml <x>"):
            self.assertIsInstance(cd.extract_rows("http://d/x", body, ""), list)


class SafetyTest(unittest.TestCase):
    def test_unsafe_urls_are_skipped(self):
        for url in ("http://d/clearfaultlog.html", "http://d/ClearFaults.htm", "http://d/reset.cgi",
                    "http://d/page.cgi?x=1", "http://d/set_ip.html", "http://d/reboot"):
            self.assertIsNotNone(cd.is_unsafe(url), url)

    def test_normal_pages_allowed(self):
        for url in ("http://d/faultlog.html", "http://d/diag/encoder.html", "http://d/"):
            self.assertIsNone(cd.is_unsafe(url), url)

    def test_fetch_uses_get_only(self):
        import inspect
        src = inspect.getsource(cd)
        self.assertNotIn('method="POST"', src)
        self.assertIn('method="GET"', inspect.getsource(cd.fetch))


if __name__ == "__main__":
    unittest.main()
