"""Captura de descubrimiento (PASO 1) del servidor web de un Kinetix 5500.

SOLO LECTURA:
  - Únicamente peticiones HTTP GET, una a la vez (una sola conexión).
  - Pausa entre peticiones y timeout corto.
  - No envía formularios.
  - NO sigue enlaces con parámetros (?...) ni rutas cuyo nombre sugiera una
    acción (clear, reset, set, write, ...). Esas URLs solo se anotan en el
    manifiesto como "omitidas" para revisarlas a mano.

Solo usa la biblioteca estándar de Python (3.8+); no requiere instalar nada.

Uso:
    python tools/capture_drive.py 172.23.22.95
    python tools/capture_drive.py 172.23.22.95 --out tests/fixtures/raw --delay 1.0

Resultado:
    <out>/files/...        respuestas crudas, una por URL
    <out>/manifest.json    URL, estado, Content-Type, cabeceras, tamaño, origen
    <out>/samples/...      segunda y tercera captura de páginas de datos, para
                           ver qué valores cambian y cada cuánto
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

DEFAULT_HOST = "172.23.22.95"
USER_AGENT = "DriveMonitor-Discovery/0.1 (read-only)"

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


class LinkParser(HTMLParser):
    """Extrae href/src/action de cualquier etiqueta."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []  # (atributo/etiqueta, valor)
        self.forms: list[dict] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        for attr in ("href", "src", "data"):
            if d.get(attr):
                self.links.append((f"{tag}.{attr}", d[attr]))
        if tag == "form":
            self.forms.append({"action": d.get("action"), "method": d.get("method", "GET")})


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


def extract_links(url: str, body: bytes, ctype: str) -> tuple[list[tuple[str, str]], list[dict], list[str]]:
    text = body.decode("latin-1", errors="replace")
    found: list[tuple[str, str]] = []
    forms: list[dict] = []
    if "html" in ctype or url.lower().endswith((".htm", ".html", ".shtml", "/")):
        lp = LinkParser()
        try:
            lp.feed(text)
        except Exception:  # noqa: BLE001
            pass
        found.extend(lp.links)
        forms = lp.forms
    for m in PATH_IN_TEXT.finditer(text):
        found.append(("texto", m.group(1)))
    hints = [m.group(0)[:120] for m in REFRESH_HINTS.finditer(text)]
    return found, forms, hints


def main() -> int:
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
    args = ap.parse_args()
    if not args.host:
        args.host = input(f"IP del drive [{DEFAULT_HOST}]: ").strip() or DEFAULT_HOST
    delay = max(0.5, args.delay)

    root = f"http://{args.host}/"
    out = Path(args.out)
    files_dir = out / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    queue: list[tuple[str, str]] = [(root, "(inicio)")]
    seen: set[str] = {root}
    manifest: dict = {
        "host": args.host,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "tool": USER_AGENT,
        "fetched": [],
        "skipped": [],
        "errors": [],
    }
    consecutive_fail = 0

    while queue and len(manifest["fetched"]) < args.max:
        url, origin = queue.pop(0)
        t0 = time.monotonic()
        status, headers, body, err = fetch(url, args.timeout)
        elapsed = round(time.monotonic() - t0, 3)
        if err:
            consecutive_fail += 1
            manifest["errors"].append({"url": url, "error": err, "origin": origin})
            print(f"[ERR ] {url}  {err}")
            if consecutive_fail >= 3:
                print("3 errores seguidos: se detiene la captura para no insistir al drive.")
                break
            time.sleep(delay * (2 ** consecutive_fail))  # backoff
            continue
        consecutive_fail = 0

        ctype = headers.get("Content-Type", "") or headers.get("content-type", "")
        dest = local_path(files_dir, url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        links, forms, hints = extract_links(url, body, ctype.lower())
        manifest["fetched"].append({
            "url": url, "origin": origin, "status": status, "content_type": ctype,
            "bytes": len(body), "elapsed_s": elapsed, "file": str(dest.relative_to(out)),
            "headers": headers, "forms": forms, "refresh_hints": hints,
            "fetched_utc": datetime.now(timezone.utc).isoformat(),
        })
        print(f"[{status}] {url}  ({len(body)} B, {elapsed}s)")

        for kind, raw in links:
            if raw.startswith(("javascript:", "mailto:", "#", "data:")):
                continue
            absu = urllib.parse.urljoin(url, raw.split("#", 1)[0])
            pu = urllib.parse.urlsplit(absu)
            if pu.scheme != "http" or pu.netloc != urllib.parse.urlsplit(root).netloc:
                continue
            if absu in seen:
                continue
            seen.add(absu)
            reason = is_unsafe(absu)
            if reason:
                manifest["skipped"].append({"url": absu, "reason": reason, "origin": url, "via": kind})
                print(f"[SKIP] {absu}  ({reason})")
                continue
            queue.append((absu, url))
        time.sleep(delay)

    # Muestras repetidas de páginas de datos para ver qué cambia.
    if args.samples > 0:
        sample_dir = out / "samples"
        data_pages = [f for f in manifest["fetched"]
                      if f["status"] == 200 and not f["url"].lower().endswith(STATIC_EXT)]
        manifest["samples"] = []
        for n in range(1, args.samples + 1):
            time.sleep(args.sample_gap)
            for f in data_pages:
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
                time.sleep(delay)

    manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nListo: {len(manifest['fetched'])} URLs, {len(manifest['skipped'])} omitidas, "
          f"{len(manifest['errors'])} errores. Manifiesto en {out / 'manifest.json'}")
    return 0


def run() -> int:
    # Con doble clic (sin argumentos) la consola se cerraría al terminar o al
    # fallar; en ese caso se muestra el error y se espera Enter.
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
