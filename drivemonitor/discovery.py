"""Descubre la URL real de cada página leyendo el menú del drive.

No supone rutas: recorre la página principal, sus frames y scripts (solo GET,
en serie) y empareja el texto de los enlaces del menú con los nombres de
config.PAGES ("Fault Log", "Encoder Diagnostics", ...).

Si una página no trae los datos en su HTML (los carga con JavaScript), se
buscan los recursos de datos a los que hace referencia (.xml, .json, .cgi,
.txt, otra .html) y se usa el que trae más campos.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable

from .config import PAGES, PageDef
from .http_client import DriveClient, UnsafeRequest, unsafe_reason
from .parsers import decode_body, extract_rows, rows_to_kv

log = logging.getLogger(__name__)

MAX_REQUESTS = 40
STATIC_EXT = (".css", ".gif", ".png", ".jpg", ".jpeg", ".ico", ".bmp", ".svg", ".woff", ".ttf")
DATA_EXT = (".xml", ".json", ".cgi", ".txt", ".csv", ".htm", ".html", ".shtml", ".asp")

PATH_IN_TEXT = re.compile(
    r"""["']((?:/|\.{0,2}/)?[A-Za-z0-9_\-./]+\.(?:s?html?|xml|json|js|cgi|txt|csv|asp))["']""")
# Menús armados en JavaScript: ("Fault Log", "faultlog.html")
JS_MENU_PAIR = re.compile(
    r"""["']([A-Za-z][A-Za-z0-9 /&()\-]{2,40})["']\s*,\s*["']([^"'\s]+\.(?:s?html?|cgi|xml|json|asp))["']""")


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


class _MenuParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.labeled: list[tuple[str, str]] = []  # (texto, href)
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        for attr in ("href", "src", "data"):
            if d.get(attr):
                self.links.append(d[attr])
        if tag == "a" and d.get("href"):
            self._href, self._text = d["href"], []
        if tag in ("option",) and d.get("value"):
            self.links.append(d["value"])

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._text).split())
            if text:
                self.labeled.append((text, self._href))
            self._href = None

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)


@dataclass
class DiscoveryResult:
    pages: dict[str, tuple[str, str]] = field(default_factory=dict)  # key -> (url_datos, url_menú)
    menu: list[tuple[str, str]] = field(default_factory=list)        # (texto, url) encontrados
    skipped: list[tuple[str, str]] = field(default_factory=list)     # (url, motivo)
    requests: int = 0

    @property
    def missing(self) -> list[PageDef]:
        return [p for p in PAGES if p.key not in self.pages]


def match_pages(menu: list[tuple[str, str]], pages: tuple[PageDef, ...] = PAGES) -> dict[str, str]:
    """Empareja textos del menú con las páginas conocidas.
    Primero coincidencia exacta (sin mayúsculas ni signos), luego 'contiene'."""
    by_norm: dict[str, str] = {}
    for text, url in menu:
        by_norm.setdefault(norm(text), url)
    out: dict[str, str] = {}
    used: set[str] = set()
    # 1) nombre principal exacto, 2) alias exacto, 3) coincidencia parcial.
    for p in pages:
        url = by_norm.get(norm(p.label))
        if url and url not in used:
            out[p.key] = url
            used.add(url)
    for p in pages:
        if p.key in out:
            continue
        for alias in p.aliases:
            url = by_norm.get(norm(alias))
            if url and url not in used:
                out[p.key] = url
                used.add(url)
                break
    for p in pages:
        if p.key in out:
            continue
        name = norm(p.label)
        for n, url in by_norm.items():
            if url not in used and (name in n or (len(n) > 5 and n in name)):
                out[p.key] = url
                used.add(url)
                break
    return out


class Discoverer:
    def __init__(self, client: DriveClient, wait: Callable[[float], bool], gap_s: float = 0.5) -> None:
        self.client = client
        self.wait = wait          # wait(seg) -> True si hay que detenerse
        self.gap_s = gap_s
        self.requests = 0

    def _get(self, url: str):
        if self.requests >= MAX_REQUESTS:
            return None
        if self.requests and self.wait(self.gap_s):
            raise InterruptedError
        self.requests += 1
        return self.client.get(url)

    def _same_host(self, url: str) -> bool:
        return urllib.parse.urlsplit(url).netloc == self.client.netloc

    def crawl_menu(self, result: DiscoveryResult) -> None:
        """Recorre página principal, frames y scripts buscando el menú."""
        start = self.client.absolute("/")
        queue = deque([(start, 0)])
        seen = {start}
        while queue and self.requests < MAX_REQUESTS:
            url, depth = queue.popleft()
            resp = self._get(url)
            if resp is None or not resp.ok:
                continue
            text = decode_body(resp.body, resp.content_type)
            is_js = url.lower().endswith(".js") or "javascript" in resp.content_type.lower()
            links: list[str] = []
            if not is_js:
                mp = _MenuParser()
                try:
                    mp.feed(text)
                    mp.close()
                except Exception:  # noqa: BLE001
                    pass
                links += mp.links
                for label, href in mp.labeled:
                    result.menu.append((label, urllib.parse.urljoin(url, href)))
            for label, href in JS_MENU_PAIR.findall(text):
                result.menu.append((label, urllib.parse.urljoin(url, href)))
            links += PATH_IN_TEXT.findall(text)
            if depth >= 3:
                continue
            for href in links:
                if href.startswith(("javascript:", "mailto:", "#", "data:")):
                    continue
                absu = urllib.parse.urljoin(url, href.split("#", 1)[0])
                if absu in seen or not self._same_host(absu) or absu.lower().endswith(STATIC_EXT):
                    continue
                seen.add(absu)
                reason = unsafe_reason(absu)
                if reason:
                    result.skipped.append((absu, reason))
                    continue
                # Solo se recorren páginas que pueden contener el menú.
                if absu.lower().endswith((".js", ".htm", ".html", ".shtml", "/")) or "." not in absu.rsplit("/", 1)[-1]:
                    queue.append((absu, depth + 1))

    def resolve_data_url(self, page_url: str) -> str:
        """Si la página no trae datos en el HTML, busca el recurso que los trae."""
        resp = self._get(page_url)
        if resp is None or not resp.ok:
            return page_url
        kv = rows_to_kv(extract_rows(page_url, resp.body, resp.content_type))
        real = [k for k, v in kv if not k.startswith("Texto ")]
        if len(real) >= 2:
            return page_url
        text = decode_body(resp.body, resp.content_type)
        candidates: list[str] = []
        scripts: list[str] = []
        for href in PATH_IN_TEXT.findall(text):
            absu = urllib.parse.urljoin(page_url, href)
            if not self._same_host(absu) or unsafe_reason(absu) or absu == page_url:
                continue
            if absu.lower().endswith(".js"):
                scripts.append(absu)
            elif absu.lower().endswith(DATA_EXT) and absu not in candidates:
                candidates.append(absu)
        for js in scripts[:3]:
            r = self._get(js)
            if r is not None and r.ok:
                for href in PATH_IN_TEXT.findall(decode_body(r.body, r.content_type)):
                    absu = urllib.parse.urljoin(js, href)
                    if (self._same_host(absu) and not unsafe_reason(absu) and absu != page_url
                            and absu.lower().endswith(DATA_EXT) and absu not in candidates):
                        candidates.append(absu)
        best, best_n = page_url, len(real)
        for cand in candidates[:6]:
            r = self._get(cand)
            if r is None or not r.ok:
                continue
            n = len([k for k, _ in rows_to_kv(extract_rows(cand, r.body, r.content_type))
                     if not k.startswith("Texto ")])
            if n > best_n:
                best, best_n = cand, n
        if best != page_url:
            log.info("Datos de %s se leen de %s (%d campos)", page_url, best, best_n)
        return best

    def run(self, overrides: dict[str, str] | None = None) -> DiscoveryResult:
        result = DiscoveryResult()
        overrides = overrides or {}
        try:
            self.crawl_menu(result)
            found = match_pages([(t, u) for t, u in result.menu if not unsafe_reason(u)])
            for p in PAGES:
                if p.key in overrides:
                    url = self.client.check(overrides[p.key])
                    result.pages[p.key] = (url, url)
                elif p.key in found:
                    menu_url = found[p.key]
                    try:
                        result.pages[p.key] = (self.resolve_data_url(menu_url), menu_url)
                    except UnsafeRequest as e:
                        result.skipped.append((menu_url, str(e)))
        except InterruptedError:
            pass
        result.requests = self.requests
        return result
