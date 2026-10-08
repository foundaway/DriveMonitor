"""Visor en vivo y captura de descubrimiento (PASO 1) de un Kinetix 5500.

SOLO LECTURA:
  - Únicamente peticiones HTTP GET, una a la vez (una sola conexión).
  - Pausa entre peticiones, timeout corto y espera creciente si el drive
    no responde.
  - No envía formularios.
  - NO consulta enlaces con parámetros (?...) ni rutas cuyo nombre sugiera una
    acción (clear, reset, set, write, ...). Esas URLs solo se anotan en el
    manifiesto como "omitidas" para revisarlas a mano.

Solo usa la biblioteca estándar de Python (3.8+); no requiere instalar nada.

Uso:
    python tools/capture_drive.py          ventana: descubre las páginas del
                                           drive y muestra sus datos en vivo
    python tools/capture_drive.py 172.23.22.95          captura por consola
    python tools/capture_drive.py 172.23.22.95 --out tests/fixtures/raw --delay 1.0

Resultado (en la carpeta de salida):
    files/...        respuestas crudas, una por URL
    manifest.json    URL, estado, Content-Type, cabeceras, tamaño, origen
    samples/...      (consola) capturas extra para ver qué valores cambian
    live/...         (ventana) copia de cada página cada vez que su contenido
                     cambia, con la hora UTC en el nombre
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

DEFAULT_HOST = "172.23.22.95"
USER_AGENT = "DriveMonitor-Discovery/0.2 (read-only)"

# Palabras que, si aparecen en la ruta, hacen que la URL NO se consulte.
UNSAFE_WORDS = (
    "clear", "reset", "set", "write", "save", "reboot", "restart", "update",
    "upgrade", "flash", "firmware_upload", "upload", "submit", "delete",
    "apply", "config_post", "login", "logout", "cmd", "command", "action",
)

# Extensiones que se tratan como recursos estáticos (no se re-muestrean).
STATIC_EXT = (".js", ".css", ".gif", ".png", ".jpg", ".jpeg", ".ico", ".bmp", ".svg")

# Cadenas dentro de JS/HTML que parecen rutas del servidor.
PATH_IN_TEXT = re.compile(
    r"""["']((?:/|\.{0,2}/)?[A-Za-z0-9_\-./]+\.(?:html?|shtml|xml|json|js|css|cgi|txt|csv|asp|htm|xsl|gif|png))["']"""
)
REFRESH_HINTS = re.compile(
    r"(setInterval\s*\([^;]*?,\s*(\d+)\s*\)|setTimeout\s*\([^;]*?,\s*(\d+)\s*\)|"
    r"http-equiv\s*=\s*[\"']?refresh[\"']?[^>]*content\s*=\s*[\"']?(\d+))",
    re.IGNORECASE | re.DOTALL,
)

# Límites del modo en vivo (para no cargar el drive).
LIVE_MIN_REFRESH_S = 2.0      # mínimo entre dos consultas de la misma página
LIVE_MIN_GAP_S = 0.5          # mínimo entre dos peticiones cualesquiera
LIVE_MAX_SAVED_PER_PAGE = 50  # copias guardadas por página en live/
LIVE_MAX_BACKOFF_S = 60.0

Log = Callable[[str], None]


# --------------------------------------------------------------------------
# Descubrimiento
# --------------------------------------------------------------------------

class LinkParser(HTMLParser):
    """Extrae href/src/data de cualquier etiqueta, el texto de los <a> y el <title>."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []  # (etiqueta.atributo, valor)
        self.labels: dict[str, str] = {}        # href -> texto del enlace
        self.forms: list[dict] = []
        self.title = ""
        self._a_href: str | None = None
        self._a_text: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        for attr in ("href", "src", "data"):
            if d.get(attr):
                self.links.append((f"{tag}.{attr}", d[attr]))
        if tag == "form":
            self.forms.append({"action": d.get("action"), "method": d.get("method", "GET")})
        elif tag == "a" and d.get("href"):
            self._a_href, self._a_text = d["href"], []
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "a" and self._a_href is not None:
            text = " ".join("".join(self._a_text).split())
            if text:
                self.labels.setdefault(self._a_href, text)
            self._a_href = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._a_href is not None:
            self._a_text.append(data)
        if self._in_title:
            self.title += data


def is_unsafe(url: str) -> str | None:
    parts = urllib.parse.urlsplit(url)
    if parts.query:
        return "tiene parámetros (?...)"
    low = parts.path.lower()
    name = low.rsplit("/", 1)[-1]
    for w in UNSAFE_WORDS:
        if re.search(rf"(^|[^a-z]){w}([^a-z]|$)", name) or name.startswith(w):
            return f"nombre sugiere acción ('{w}')"
    return None


def local_path(base: Path, url: str) -> Path:
    parts = urllib.parse.urlsplit(url)
    p = parts.path or "/"
    if p.endswith("/"):
        p += "index"
    safe = re.sub(r"[^A-Za-z0-9_.\-/]", "_", p).lstrip("/")
    if parts.query:
        safe += "__" + hashlib.sha1(parts.query.encode()).hexdigest()[:8]
    return base / safe


def fetch(url: str, timeout: float) -> tuple[int, dict, bytes, str | None]:
    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": USER_AGENT,
        "Connection": "close",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read(), None
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() if e.fp else b"", None
    except Exception as e:  # noqa: BLE001 - registrar y seguir
        return 0, {}, b"", f"{type(e).__name__}: {e}"


def is_html(url: str, ctype: str) -> bool:
    return "html" in ctype.lower() or url.lower().endswith((".htm", ".html", ".shtml", "/"))


def extract_links(url: str, body: bytes, ctype: str):
    """Devuelve (enlaces, formularios, pistas de refresco, textos de enlace, título)."""
    text = body.decode("latin-1", errors="replace")
    found: list[tuple[str, str]] = []
    forms: list[dict] = []
    labels: dict[str, str] = {}
    title = ""
    if is_html(url, ctype):
        lp = LinkParser()
        try:
            lp.feed(text)
        except Exception:  # noqa: BLE001
            pass
        found.extend(lp.links)
        forms, labels, title = lp.forms, lp.labels, " ".join(lp.title.split())
    for m in PATH_IN_TEXT.finditer(text):
        found.append(("texto", m.group(1)))
    hints = [m.group(0)[:120] for m in REFRESH_HINTS.finditer(text)]
    return found, forms, hints, labels, title


def crawl(host: str, out: Path, *, delay: float = 1.0, timeout: float = 5.0,
          max_urls: int = 150, log: Log = print, stop: threading.Event | None = None) -> dict:
    """Recorre el sitio del drive (solo GET) y guarda cada respuesta en out/files."""
    stop = stop or threading.Event()
    delay = max(0.5, delay)
    root = f"http://{host}/"
    netloc = urllib.parse.urlsplit(root).netloc
    files_dir = out / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    queue: list[tuple[str, str]] = [(root, "(inicio)")]
    seen: set[str] = {root}
    labels: dict[str, str] = {root: "Inicio"}
    manifest: dict = {
        "host": host,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "tool": USER_AGENT,
        "fetched": [],
        "skipped": [],
        "errors": [],
    }
    consecutive_fail = 0

    while queue and len(manifest["fetched"]) < max_urls and not stop.is_set():
        url, origin = queue.pop(0)
        t0 = time.monotonic()
        status, headers, body, err = fetch(url, timeout)
        elapsed = round(time.monotonic() - t0, 3)
        if err:
            consecutive_fail += 1
            manifest["errors"].append({"url": url, "error": err, "origin": origin})
            log(f"[ERR ] {url}  {err}")
            if consecutive_fail >= 3:
                log("3 errores seguidos: se detiene la búsqueda para no insistir al drive.")
                break
            if stop.wait(delay * (2 ** consecutive_fail)):  # backoff
                break
            continue
        consecutive_fail = 0

        ctype = headers.get("Content-Type", "") or headers.get("content-type", "")
        dest = local_path(files_dir, url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        links, forms, hints, link_labels, title = extract_links(url, body, ctype)
        if title and url not in labels:
            labels[url] = title
        manifest["fetched"].append({
            "url": url, "origin": origin, "status": status, "content_type": ctype,
            "bytes": len(body), "elapsed_s": elapsed, "file": str(dest.relative_to(out)),
            "headers": headers, "forms": forms, "refresh_hints": hints,
            "fetched_utc": datetime.now(timezone.utc).isoformat(),
        })
        log(f"[{status}] {url}  ({len(body)} B, {elapsed}s)")

        for kind, raw in links:
            if raw.startswith(("javascript:", "mailto:", "#", "data:")):
                continue
            absu = urllib.parse.urljoin(url, raw.split("#", 1)[0])
            pu = urllib.parse.urlsplit(absu)
            if pu.scheme != "http" or pu.netloc != netloc:
                continue
            if raw in link_labels and absu not in labels:
                labels[absu] = link_labels[raw]
            if absu in seen:
                continue
            seen.add(absu)
            reason = is_unsafe(absu)
            if reason:
                manifest["skipped"].append({"url": absu, "reason": reason, "origin": url, "via": kind})
                log(f"[SKIP] {absu}  ({reason})")
                continue
            queue.append((absu, url))
        stop.wait(delay)

    for f in manifest["fetched"]:
        f["label"] = labels.get(f["url"], "")
    if stop.is_set():
        manifest["cancelled"] = True
        log("Búsqueda detenida por el usuario; se guarda lo obtenido hasta ahora.")
    write_manifest(out, manifest)
    return manifest


def write_manifest(out: Path, manifest: dict) -> None:
    manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def data_pages(manifest: dict) -> list[dict]:
    """Páginas que vale la pena seguir en vivo (respuestas 200 que no son estáticas)."""
    return [f for f in manifest["fetched"]
            if f["status"] == 200 and not f["url"].lower().endswith(STATIC_EXT)]


def page_label(page: dict) -> str:
    if page.get("label"):
        return page["label"]
    path = urllib.parse.urlsplit(page["url"]).path
    return path.rsplit("/", 1)[-1] or path or "/"


# --------------------------------------------------------------------------
# Extracción genérica de datos (sin suponer el formato de cada página)
# --------------------------------------------------------------------------

BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
              "table", "tr", "hr", "form", "fieldset", "legend", "dt", "dd", "pre", "center"}


class RowExtractor(HTMLParser):
    """Convierte HTML en filas: cada <tr> es una fila con sus celdas y el texto
    suelto se separa por bloques. Ignora <script> y <style>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._skip = 0
        self._row_stack: list[list[str] | None] = []  # una fila abierta por tabla
        self._cell: list[str] | None = None
        self._loose: list[str] = []

    @staticmethod
    def _clean(parts: list[str]) -> str:
        return " ".join("".join(parts).split())

    def _flush_loose(self) -> None:
        # Texto suelto: una fila por renglón (respeta <pre> y listas en texto).
        lines = "".join(self._loose).splitlines()
        self._loose = []
        for line in lines:
            text = " ".join(line.split())
            if text:
                self.rows.append(split_key_value(text))

    def _close_cell(self) -> None:
        if self._cell is not None and self._row_stack and self._row_stack[-1] is not None:
            self._row_stack[-1].append(self._clean(self._cell))
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row_stack and self._row_stack[-1] is not None:
            row = self._row_stack[-1]
            while row and not row[-1]:
                row.pop()
            if any(row):
                self.rows.append(row)
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
            self._row_stack[-1] = []
        elif tag in ("td", "th") and self._row_stack:
            self._close_cell()
            if self._row_stack[-1] is None:
                self._row_stack[-1] = []
            self._cell = []
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


def split_key_value(text: str) -> list[str]:
    """'a | b | c' -> ['a', 'b', 'c']; 'Clave: valor' -> ['Clave', 'valor'];
    cualquier otro texto queda en una celda."""
    if " | " in text:
        return [part.strip() for part in text.split(" | ")]
    m = re.match(r"^([^:|]{1,60}?)\s*[:=]\s+(.+)$", text)
    return [m.group(1), m.group(2)] if m else [text]


def decode_body(body: bytes, ctype: str = "") -> str:
    m = re.search(r"charset=([\w\-]+)", ctype or "", re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8"]:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    return body.decode("latin-1", errors="replace")


def _flatten_json(obj, prefix: str, rows: list[list[str]]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten_json(v, f"{prefix}.{k}" if prefix else str(k), rows)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten_json(v, f"{prefix}[{i}]", rows)
    else:
        rows.append([prefix or "valor", "" if obj is None else str(obj)])


def extract_rows(url: str, body: bytes, ctype: str = "") -> list[list[str]]:
    """Filas de texto con los datos de una respuesta, sea HTML, XML, JSON o texto.
    Nunca lanza excepción: si algo falla, devuelve el texto plano."""
    text = decode_body(body, ctype)
    low_ct = (ctype or "").lower()
    stripped = text.lstrip()
    try:
        if "json" in low_ct or url.lower().endswith(".json") or stripped[:1] in ("{", "["):
            rows: list[list[str]] = []
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
                for k, v in el.attrib.items():
                    rows.append([f"{p}@{k}", v])
                if el.text and el.text.strip():
                    rows.append([p, " ".join(el.text.split())])
                for ch in el:
                    walk(ch, p)

            walk(root, "")
            return rows
    except ET.ParseError:
        pass
    try:
        if is_html(url, ctype) or "<" in stripped[:200]:
            ex = RowExtractor()
            ex.feed(text)
            ex.close()
            return ex.rows
    except Exception:  # noqa: BLE001 - HTML raro: seguir con texto plano
        pass
    return [split_key_value(" ".join(line.split())) for line in text.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Monitoreo en vivo
# --------------------------------------------------------------------------

class LiveMonitor:
    """Consulta las páginas una por una, sin peticiones simultáneas.

    on_update(url, rows, raw_text, error, when_utc) se llama desde el hilo de
    trabajo; la ventana lo pasa a su propio hilo por una cola.
    """

    def __init__(self, pages: list[dict], out: Path, *, refresh_s: float, gap_s: float = 1.0,
                 timeout: float = 5.0, log: Log = print,
                 on_update: Callable[..., None] | None = None,
                 stop: threading.Event | None = None) -> None:
        self.pages = pages
        self.out = out
        self.refresh_s = max(LIVE_MIN_REFRESH_S, refresh_s)
        self.gap_s = max(LIVE_MIN_GAP_S, gap_s)
        self.timeout = timeout
        self.log = log
        self.on_update = on_update or (lambda *a: None)
        self.stop = stop or threading.Event()
        self._last_hash: dict[str, str] = {}
        self._saved: dict[str, int] = {}

    def set_refresh(self, seconds: float) -> None:
        self.refresh_s = max(LIVE_MIN_REFRESH_S, seconds)

    def _save_if_changed(self, url: str, body: bytes) -> bool:
        h = hashlib.sha1(body).hexdigest()
        changed = self._last_hash.get(url) != h
        self._last_hash[url] = h
        if changed and self._saved.get(url, 0) < LIVE_MAX_SAVED_PER_PAGE:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
            dest = local_path(self.out / "live", url)
            dest = dest.parent / dest.name / f"{stamp}{Path(dest.name).suffix or '.txt'}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
            self._saved[url] = self._saved.get(url, 0) + 1
        return changed

    def run(self) -> None:
        if not self.pages:
            self.log("No hay páginas de datos para seguir en vivo.")
            return
        next_due = {p["url"]: 0.0 for p in self.pages}
        fails = 0
        offline = False
        while not self.stop.is_set():
            url = min(next_due, key=next_due.get)
            wait = next_due[url] - time.monotonic()
            if wait > 0 and self.stop.wait(wait):
                break
            status, headers, body, err = fetch(url, self.timeout)
            when = datetime.now(timezone.utc)
            if err or status != 200:
                msg = err or f"HTTP {status}"
                self.on_update(url, None, None, msg, when)
                fails = fails + 1 if err else fails
                if err and fails >= 3:
                    pause = min(LIVE_MAX_BACKOFF_S, self.refresh_s * (2 ** (fails - 3)))
                    if not offline:
                        self.log(f"Sin comunicación con el drive ({msg}). Reintentos con espera creciente, hasta cada {LIVE_MAX_BACKOFF_S:.0f} s.")
                        offline = True
                    next_due = {u: time.monotonic() + pause for u in next_due}
                    continue
            else:
                if offline:
                    self.log("Comunicación restablecida.")
                    offline = False
                fails = 0
                ctype = headers.get("Content-Type", "") or headers.get("content-type", "")
                try:
                    self._save_if_changed(url, body)
                except OSError as e:
                    self.log(f"No se pudo guardar {url}: {e}")
                rows = extract_rows(url, body, ctype)
                self.on_update(url, rows, decode_body(body, ctype), None, when)
            next_due[url] = time.monotonic() + self.refresh_s
            self.stop.wait(self.gap_s)


# --------------------------------------------------------------------------
# Consola
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None, log: Log = print, stop: threading.Event | None = None) -> int:
    stop = stop or threading.Event()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", nargs="?", help="IP del drive, p. ej. 172.23.22.95 (si falta, se pregunta)")
    ap.add_argument("--out", default="tests/fixtures/raw", help="carpeta de salida")
    ap.add_argument("--delay", type=float, default=1.0, help="segundos entre peticiones (mín. 0.5)")
    ap.add_argument("--timeout", type=float, default=5.0, help="timeout por petición (s)")
    ap.add_argument("--max", type=int, default=150, help="máximo de URLs a consultar")
    ap.add_argument("--samples", type=int, default=2,
                    help="capturas extra de cada página de datos (0 = ninguna)")
    ap.add_argument("--sample-gap", type=float, default=3.0,
                    help="segundos entre capturas extra de la misma página")
    args = ap.parse_args(argv)
    if not args.host:
        args.host = input(f"IP del drive [{DEFAULT_HOST}]: ").strip() or DEFAULT_HOST
    delay = max(0.5, args.delay)
    out = Path(args.out)

    manifest = crawl(args.host, out, delay=delay, timeout=args.timeout, max_urls=args.max,
                     log=log, stop=stop)

    # Muestras repetidas de páginas de datos para ver qué cambia.
    if args.samples > 0 and not stop.is_set():
        sample_dir = out / "samples"
        pages = data_pages(manifest)
        manifest["samples"] = []
        for n in range(1, args.samples + 1):
            if stop.wait(args.sample_gap):
                break
            for f in pages:
                if stop.is_set():
                    break
                status, headers, body, err = fetch(f["url"], args.timeout)
                if err:
                    manifest["errors"].append({"url": f["url"], "error": err, "origin": "muestra"})
                    continue
                dest = local_path(sample_dir / f"s{n}", f["url"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(body)
                manifest["samples"].append({
                    "url": f["url"], "sample": n, "status": status, "bytes": len(body),
                    "file": str(dest.relative_to(out)),
                    "fetched_utc": datetime.now(timezone.utc).isoformat(),
                })
                stop.wait(delay)
        if stop.is_set():
            manifest["cancelled"] = True
            log("Captura detenida por el usuario; se guarda lo obtenido hasta ahora.")
        write_manifest(out, manifest)

    log(f"\nListo: {len(manifest['fetched'])} URLs, {len(manifest['skipped'])} omitidas, "
        f"{len(manifest['errors'])} errores. Manifiesto en {out / 'manifest.json'}")
    return 0


# --------------------------------------------------------------------------
# Ventana
# --------------------------------------------------------------------------

def default_out_dir() -> Path:
    # Junto al .exe (PyInstaller) o en la carpeta actual si se corre con Python.
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return base / "tests" / "fixtures" / "raw"


def gui() -> int:
    import queue as queue_mod
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    BG, PANEL, FG, MUTED = "#1e2227", "#262b31", "#e6e6e6", "#9aa3ad"
    ACCENT, OK, WARN, BAD = "#3d8bd9", "#3fb950", "#d29922", "#f85149"
    CHANGED_BG = "#4a3b12"
    MAX_COLS = 12

    win = tk.Tk()
    win.title("Kinetix 5500 en vivo (solo lectura)")
    win.geometry("1150x720")
    win.minsize(800, 500)
    win.configure(bg=BG)

    style = ttk.Style(win)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, bordercolor="#3a4048")
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Title.TLabel", font=("Segoe UI", 12, "bold"))
    style.configure("TButton", background=PANEL, foreground=FG, padding=(10, 4))
    style.map("TButton", background=[("active", "#323840"), ("disabled", BG)],
              foreground=[("disabled", "#5c636b")])
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
    style.map("Accent.TButton", background=[("active", "#5299e0"), ("disabled", "#2a3a4a")])
    style.configure("TEntry", fieldbackground=PANEL, foreground=FG, insertcolor=FG)
    style.configure("TSpinbox", fieldbackground=PANEL, foreground=FG, arrowcolor=FG)
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                    rowheight=22, font=("Consolas", 10))
    style.configure("Treeview.Heading", background="#2f353c", foreground=FG, relief="flat")
    style.map("Treeview", background=[("selected", "#2f5d8a")])
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(12, 4))
    style.map("TNotebook.Tab", background=[("selected", "#323840")], foreground=[("selected", FG)])
    style.configure("TPanedwindow", background=BG)

    events: queue_mod.Queue = queue_mod.Queue()
    stop = threading.Event()
    state: dict = {"monitor": None, "thread": None, "pages": [], "data": {}, "selected": None}

    # --- barra superior ---
    top = ttk.Frame(win, padding=(10, 8))
    top.pack(fill="x")
    ttk.Label(top, text="IP del drive").pack(side="left")
    ip_var = tk.StringVar(value=DEFAULT_HOST)
    ip_entry = ttk.Entry(top, textvariable=ip_var, width=16)
    ip_entry.pack(side="left", padx=(6, 12))
    ttk.Label(top, text="Refresco por página (s)").pack(side="left")
    refresh_var = tk.StringVar(value="5")
    refresh_spin = ttk.Spinbox(top, from_=LIVE_MIN_REFRESH_S, to=600, increment=1,
                               textvariable=refresh_var, width=5)
    refresh_spin.pack(side="left", padx=(6, 12))
    start_btn = ttk.Button(top, text="Conectar", style="Accent.TButton")
    start_btn.pack(side="left")
    stop_btn = ttk.Button(top, text="Detener", state="disabled")
    stop_btn.pack(side="left", padx=6)
    dot = tk.Canvas(top, width=14, height=14, bg=BG, highlightthickness=0)
    dot_item = dot.create_oval(2, 2, 12, 12, fill="#5c636b", outline="")
    dot.pack(side="left", padx=(14, 4))
    status_var = tk.StringVar(value="Desconectado. Solo se hacen peticiones GET; nada se modifica en el drive.")
    ttk.Label(top, textvariable=status_var).pack(side="left")

    out_bar = ttk.Frame(win, padding=(10, 0, 10, 6))
    out_bar.pack(fill="x")
    ttk.Label(out_bar, text="Guardar en", style="Muted.TLabel").pack(side="left")
    out_var = tk.StringVar(value=str(default_out_dir()))
    ttk.Entry(out_bar, textvariable=out_var).pack(side="left", fill="x", expand=True, padx=6)

    def browse() -> None:
        d = filedialog.askdirectory(initialdir=out_var.get())
        if d:
            out_var.set(d)

    browse_btn = ttk.Button(out_bar, text="Examinar…", command=browse)
    browse_btn.pack(side="left")

    # --- cuerpo: páginas | datos ---
    vpane = ttk.PanedWindow(win, orient="vertical")
    vpane.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    hpane = ttk.PanedWindow(vpane, orient="horizontal")
    vpane.add(hpane, weight=4)

    left = ttk.Frame(hpane)
    hpane.add(left, weight=1)
    ttk.Label(left, text="Páginas", style="Title.TLabel").pack(anchor="w", pady=(0, 4))
    pages_tv = ttk.Treeview(left, columns=("upd", "st"), show="tree headings", selectmode="browse")
    pages_tv.heading("#0", text="Página")
    pages_tv.heading("upd", text="Actualizada")
    pages_tv.heading("st", text="Estado")
    pages_tv.column("#0", width=190)
    pages_tv.column("upd", width=80, anchor="center")
    pages_tv.column("st", width=70, anchor="center")
    pages_tv.tag_configure("err", foreground=BAD)
    pages_tv.tag_configure("changed", foreground=WARN)
    pages_tv.pack(fill="both", expand=True)

    right = ttk.Frame(hpane)
    hpane.add(right, weight=3)
    head_var = tk.StringVar(value="Presiona Conectar para buscar las páginas del drive.")
    url_var = tk.StringVar(value="")
    ttk.Label(right, textvariable=head_var, style="Title.TLabel").pack(anchor="w")
    ttk.Label(right, textvariable=url_var, style="Muted.TLabel").pack(anchor="w", pady=(0, 4))
    nb = ttk.Notebook(right)
    nb.pack(fill="both", expand=True)

    data_frame = ttk.Frame(nb)
    nb.add(data_frame, text="Datos")
    cols = [f"c{i}" for i in range(MAX_COLS)]
    data_tv = ttk.Treeview(data_frame, columns=cols, show="headings")
    for i, c in enumerate(cols):
        data_tv.heading(c, text="")
        data_tv.column(c, width=160 if i else 220, stretch=True, anchor="w")
    data_tv.tag_configure("changed", background=CHANGED_BG)
    dsy = ttk.Scrollbar(data_frame, orient="vertical", command=data_tv.yview)
    dsx = ttk.Scrollbar(data_frame, orient="horizontal", command=data_tv.xview)
    data_tv.configure(yscrollcommand=dsy.set, xscrollcommand=dsx.set)
    data_tv.grid(row=0, column=0, sticky="nsew")
    dsy.grid(row=0, column=1, sticky="ns")
    dsx.grid(row=1, column=0, sticky="ew")
    data_frame.rowconfigure(0, weight=1)
    data_frame.columnconfigure(0, weight=1)

    raw_frame = ttk.Frame(nb)
    nb.add(raw_frame, text="Crudo")
    raw_txt = tk.Text(raw_frame, bg=PANEL, fg=FG, insertbackground=FG, font=("Consolas", 9),
                      wrap="none", relief="flat", state="disabled")
    rsy = ttk.Scrollbar(raw_frame, orient="vertical", command=raw_txt.yview)
    rsx = ttk.Scrollbar(raw_frame, orient="horizontal", command=raw_txt.xview)
    raw_txt.configure(yscrollcommand=rsy.set, xscrollcommand=rsx.set)
    raw_txt.grid(row=0, column=0, sticky="nsew")
    rsy.grid(row=0, column=1, sticky="ns")
    rsx.grid(row=1, column=0, sticky="ew")
    raw_frame.rowconfigure(0, weight=1)
    raw_frame.columnconfigure(0, weight=1)

    log_frame = ttk.Frame(vpane)
    vpane.add(log_frame, weight=1)
    ttk.Label(log_frame, text="Registro", style="Muted.TLabel").pack(anchor="w")
    log_txt = tk.Text(log_frame, height=7, bg=PANEL, fg=MUTED, font=("Consolas", 9),
                      relief="flat", state="disabled")
    log_txt.pack(fill="both", expand=True)

    # --- lógica de la ventana ---
    def append_log(msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        log_txt.config(state="normal")
        for line in msg.strip("\n").splitlines() or [""]:
            log_txt.insert("end", f"{stamp}  {line}\n")
        if int(log_txt.index("end-1c").split(".")[0]) > 3000:
            log_txt.delete("1.0", "500.0")
        log_txt.see("end")
        log_txt.config(state="disabled")

    def set_dot(color: str) -> None:
        dot.itemconfig(dot_item, fill=color)

    def set_running(running: bool) -> None:
        start_btn.config(state="disabled" if running else "normal")
        stop_btn.config(state="normal" if running else "disabled")
        ip_entry.config(state="disabled" if running else "normal")
        browse_btn.config(state="disabled" if running else "normal")

    def show_page(url: str | None) -> None:
        state["selected"] = url
        data_tv.delete(*data_tv.get_children())
        if not url:
            return
        page = next((p for p in state["pages"] if p["url"] == url), None)
        head_var.set(page_label(page) if page else url)
        url_var.set(url)
        d = state["data"].get(url)
        if not d:
            for c in cols:
                data_tv.heading(c, text="")
            data_tv.insert("", "end", values=["(esperando la primera lectura…)"])
            return
        rows, changed = d["rows"] or [], d["changed"]
        ncols = max((len(r) for r in rows), default=1)
        for i, c in enumerate(cols):
            longest = max((len(r[i]) for r in rows[:300] if i < len(r)), default=0)
            data_tv.heading(c, text=f"Col {i + 1}" if i < ncols else "")
            data_tv.column(c, width=min(420, max(90, 8 * longest + 16)) if i < ncols else 0,
                           minwidth=0, stretch=i < ncols)
        for i, r in enumerate(rows):
            vals = list(r[:MAX_COLS - 1]) + ([" | ".join(r[MAX_COLS - 1:])] if len(r) >= MAX_COLS else [])
            data_tv.insert("", "end", values=vals, tags=("changed",) if i in changed else ())
        if d["error"]:
            data_tv.insert("", 0, values=[f"Última lectura falló: {d['error']}"])
        raw_txt.config(state="normal")
        raw_txt.delete("1.0", "end")
        raw_txt.insert("1.0", (d["raw"] or "")[:200_000])
        raw_txt.config(state="disabled")

    def on_select(_e=None) -> None:
        sel = pages_tv.selection()
        show_page(sel[0] if sel else None)

    pages_tv.bind("<<TreeviewSelect>>", on_select)

    def apply_refresh(*_a) -> None:
        try:
            v = float(refresh_var.get())
        except ValueError:
            return
        if state["monitor"]:
            state["monitor"].set_refresh(v)

    refresh_var.trace_add("write", apply_refresh)

    def handle_update(url, rows, raw, error, when) -> None:
        d = state["data"].setdefault(url, {"rows": None, "raw": None, "changed": set(), "error": None})
        local = when.astimezone().strftime("%H:%M:%S")
        if error:
            d["error"] = error
            if pages_tv.exists(url):
                pages_tv.set(url, "st", "error")
                pages_tv.item(url, tags=("err",))
        else:
            prev = d["rows"] or []
            changed = {i for i, r in enumerate(rows) if i >= len(prev) or prev[i] != r} if d["rows"] else set()
            d.update(rows=rows, raw=raw, changed=changed, error=None)
            if pages_tv.exists(url):
                pages_tv.set(url, "upd", local)
                pages_tv.set(url, "st", f"{len(changed)} camb." if changed else "ok")
                pages_tv.item(url, tags=("changed",) if changed else ())
            set_dot(OK)
            status_var.set(f"En vivo · {len(state['pages'])} páginas · última lectura {local}")
        if url == state["selected"]:
            show_page(url)

    def worker(host: str, out: Path) -> None:
        try:
            events.put(("status", f"Buscando páginas en {host}…"))
            manifest = crawl(host, out, log=lambda m: events.put(("log", m)), stop=stop)
            pages = data_pages(manifest)
            events.put(("pages", pages))
            if stop.is_set():
                return
            if not pages:
                events.put(("log", "No se encontraron páginas con datos. Revisa la IP y la conexión."))
                return
            mon = LiveMonitor(pages, out, refresh_s=float(refresh_var.get() or 5),
                              log=lambda m: events.put(("log", m)),
                              on_update=lambda *a: events.put(("update", a)), stop=stop)
            state["monitor"] = mon
            events.put(("log", f"Monitoreo en vivo de {len(pages)} páginas, una petición a la vez."))
            mon.run()
        except Exception:  # noqa: BLE001 - mostrar en la ventana, nunca cerrar
            import traceback
            events.put(("log", traceback.format_exc()))
        finally:
            events.put(("done", None))

    def start() -> None:
        host = ip_var.get().strip()
        if not host or not re.fullmatch(r"[A-Za-z0-9.\-]+(:\d+)?", host):
            messagebox.showerror("IP inválida", "Escribe la IP del drive, p. ej. 172.23.22.95")
            return
        stop.clear()
        state.update(monitor=None, pages=[], data={}, selected=None)
        pages_tv.delete(*pages_tv.get_children())
        show_page(None)
        head_var.set("Buscando páginas…")
        url_var.set("")
        set_running(True)
        set_dot(WARN)
        t = threading.Thread(target=worker, args=(host, Path(out_var.get())), daemon=True)
        state["thread"] = t
        t.start()

    def request_stop() -> None:
        stop.set()
        stop_btn.config(state="disabled")
        status_var.set("Deteniendo…")

    start_btn.config(command=start)
    stop_btn.config(command=request_stop)
    ip_entry.bind("<Return>", lambda _e: start() if str(start_btn["state"]) == "normal" else None)

    def pump() -> None:
        for _ in range(200):
            try:
                kind, payload = events.get_nowait()
            except queue_mod.Empty:
                break
            try:
                if kind == "log":
                    append_log(payload)
                    if payload.startswith("Sin comunicación"):
                        set_dot(BAD)
                        status_var.set(payload)
                elif kind == "status":
                    status_var.set(payload)
                elif kind == "pages":
                    state["pages"] = payload
                    for p in payload:
                        pages_tv.insert("", "end", iid=p["url"], text=page_label(p), values=("", "…"))
                    if payload:
                        pages_tv.selection_set(payload[0]["url"])
                    else:
                        head_var.set("No se encontraron páginas.")
                elif kind == "update":
                    handle_update(*payload)
                elif kind == "done":
                    set_running(False)
                    set_dot("#5c636b")
                    status_var.set(f"Desconectado. Datos guardados en {out_var.get()}")
            except Exception:  # noqa: BLE001 - un dato raro no debe tumbar la ventana
                import traceback
                append_log(traceback.format_exc())
        win.after(100, pump)

    def on_close() -> None:
        t = state["thread"]
        if t and t.is_alive():
            if not messagebox.askyesno("Salir", "El monitoreo sigue activo. ¿Detener y salir?"):
                return
            stop.set()
            t.join(timeout=10)
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", on_close)
    win.update_idletasks()
    try:
        hpane.sashpos(0, 360)
        vpane.sashpos(0, max(300, win.winfo_height() - 220))
    except tk.TclError:
        pass
    pump()
    ip_entry.focus_set()
    ip_entry.select_range(0, "end")
    win.mainloop()
    return 0


def run() -> int:
    # Sin argumentos (doble clic) se abre la ventana; con argumentos, modo consola.
    if len(sys.argv) == 1:
        try:
            return gui()
        except ImportError:
            pass  # Python sin tkinter: seguir en consola
    interactive = len(sys.argv) == 1
    code = 1
    try:
        code = main()
    except KeyboardInterrupt:
        print("\nCancelado por el usuario.")
    except Exception:  # noqa: BLE001 - mostrar el error antes de cerrar
        import traceback
        traceback.print_exc()
    if interactive:
        try:
            input("\nPresiona Enter para cerrar...")
        except EOFError:
            pass
    return code


if __name__ == "__main__":
    sys.exit(run())
