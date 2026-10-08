"""Ciclo de consulta de un drive (un hilo por drive).

- Una sola petición a la vez, con pausa mínima entre peticiones.
- Cada página tiene su intervalo; las de "on_change" se leen al conectar y
  luego cada tanto, y solo se registran si algo cambió.
- Si el drive no responde: evento de pérdida de comunicación y reintentos con
  espera creciente (hasta max_backoff_s). Al volver: evento y relectura de las
  páginas de información.
- Cualquier error de parseo se registra y el ciclo sigue con las demás páginas.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests

from .config import PAGE_BY_KEY, PAGES, Settings
from .db import Database
from .discovery import Discoverer
from .http_client import DriveClient, UnsafeRequest
from .parsers import extract_rows, parse_duration, parse_fault_rows, parse_number, rows_to_kv

log = logging.getLogger(__name__)

RSSI_RE = re.compile(r"\brssi\b", re.I)
QUALITY_RE = re.compile(r"quality", re.I)
FIRMWARE_RE = re.compile(r"firmware|\bfw\b|fw\s*rev|revision|\brev\b", re.I)
UPTIME_RE = re.compile(r"^\s*uptime\b", re.I)
NET_ERROR_RE = re.compile(r"crc|collision|error|discard|drop|lost|late|align|overrun|underrun|"
                          r"\bfcs\b|miss|fail|reject|timeout|\bbad\b|invalid", re.I)
VOLATILE_RE = re.compile(r"uptime|\btime\b|date|clock|temperature|\btemp\b", re.I)
NETWORK_PAGES = ("ethernet_stats", "network_stats")
REDISCOVER_AFTER_S = 600
PURGE_EVERY_S = 3600

Emit = Callable[[str, dict], None]


@dataclass
class DriveRef:
    id: int
    name: str
    ip: str


class DrivePoller(threading.Thread):
    def __init__(self, drive: DriveRef, db: Database, settings: Settings,
                 emit: Emit | None = None) -> None:
        super().__init__(name=f"drive-{drive.ip}", daemon=True)
        self.drive = drive
        self.db = db
        self.settings = settings
        self.emit_cb = emit or (lambda kind, payload: None)
        self._halt = threading.Event()
        self.client = DriveClient(drive.ip, timeout_s=float(settings.data["request_timeout_s"]))
        self.urls: dict[str, str] = {}
        self.state = "stopped"
        self.fails = 0
        self.online: bool | None = None
        self._last_num: dict[tuple[str, str], float] = {}
        self._last_uptime: float | None = None
        self._alerted_low: set[str] = set()
        self._fault_hash: str | None = None
        self._last_discovery = 0.0
        self._last_purge = 0.0

    # --- utilidades -------------------------------------------------------
    def stop(self) -> None:
        self._halt.set()

    def stopped(self) -> bool:
        return self._halt.is_set()

    def wait(self, seconds: float) -> bool:
        return self._halt.wait(max(0.0, seconds))

    def emit(self, msg_type: str, /, **payload) -> None:
        payload["drive_id"] = self.drive.id
        try:
            self.emit_cb(msg_type, payload)
        except Exception:  # noqa: BLE001 - la interfaz nunca debe tumbar el ciclo
            log.exception("Error al notificar %s", msg_type)

    def event(self, kind: str, message: str, details: str = "", alert: bool = False,
              level: str = "warning") -> None:
        self.db.add_event(self.drive.id, kind, message, details)
        log.info("[%s] %s: %s %s", self.drive.ip, kind, message, details)
        self.emit("event", kind=kind, message=message, details=details)
        if alert:
            self.emit("alert", kind=kind, level=level, title=f"{self.drive.name}: {message}", message=details)

    def set_status(self, state: str, message: str = "") -> None:
        self.state = state
        self.emit("status", state=state, message=message)

    # --- ciclo principal --------------------------------------------------
    def run(self) -> None:
        log.info("Inicia monitoreo de %s (%s)", self.drive.name, self.drive.ip)
        self.set_status("connecting", "Conectando…")
        self._load_baselines()
        next_due: dict[str, float] = {}
        try:
            while not self.stopped():
                try:
                    if not self.urls or (self._needs_rediscovery() and
                                         time.time() - self._last_discovery > REDISCOVER_AFTER_S):
                        self._discover()
                        next_due = {k: 0.0 for k in self.urls}
                        if not self.urls:
                            self.set_status("online" if self.online else "offline",
                                            "No se encontraron páginas conocidas en el menú del drive")
                            self.wait(REDISCOVER_AFTER_S)
                            continue
                    # Páginas que dejaron de existir salen de la agenda; las nuevas entran.
                    next_due = {k: next_due.get(k, 0.0) for k in self.urls}
                    if not next_due:
                        continue
                    page = min(next_due, key=next_due.get)
                    if self.wait(next_due[page] - time.monotonic()):
                        break
                    ok = self._poll_page(page)
                    if ok is None:  # URL ya no existe: buscar de nuevo más adelante
                        next_due[page] = time.monotonic() + REDISCOVER_AFTER_S
                    else:
                        next_due[page] = time.monotonic() + self.settings.interval(page)
                    self._comm_ok()
                    self._maybe_purge()
                except (requests.RequestException, OSError) as e:
                    pause = self._comm_failed(e)
                    if self.wait(pause):
                        break
                    if self.online is False:
                        # Al recuperar la comunicación se releen todas las páginas.
                        next_due = {k: 0.0 for k in self.urls}
                    continue
                if self.wait(float(self.settings.data["min_gap_s"])):
                    break
        except Exception:  # noqa: BLE001
            log.exception("Error inesperado en el ciclo de %s", self.drive.ip)
            self.event("internal_error", "Error interno en el monitoreo", "Ver log de la aplicación")
        finally:
            self.client.close()
            self.db.close()
            self.set_status("stopped", "Detenido")
            log.info("Termina monitoreo de %s", self.drive.ip)

    def _needs_rediscovery(self) -> bool:
        return any(p.key not in self.urls for p in PAGES)

    def _discover(self) -> None:
        self._last_discovery = time.time()
        self.set_status("discovering", "Buscando páginas en el menú del drive…")
        overrides = self.settings.url_overrides(self.drive.ip)
        known = self.db.page_urls(self.drive.id)
        if known and not self.urls:
            self.urls = known  # primero lo ya conocido; se re-descubre si algo falla
            return
        res = Discoverer(self.client, self.wait, float(self.settings.data["min_gap_s"])).run(overrides)
        if res.pages:
            self.db.set_page_urls(self.drive.id, res.pages)
            self.urls = {k: v[0] for k, v in res.pages.items()}
        missing = ", ".join(p.label for p in res.missing)
        self.event("discovery", f"Páginas encontradas: {len(res.pages)} de {len(PAGES)}",
                   (f"No encontradas: {missing}. " if missing else "") +
                   f"Peticiones: {res.requests}. Menú: " + "; ".join(f"{t} -> {u}" for t, u in res.menu[:40]),
                   level="info")
        for url, reason in res.skipped:
            log.info("Omitida %s (%s)", url, reason)

    def _load_baselines(self) -> None:
        for page in NETWORK_PAGES:
            for key, (_ts, _v, num) in self.db.latest_values(self.drive.id, page).items():
                if num is not None:
                    self._last_num[(page, key)] = num
        up = self.db.get_state(self.drive.id, "last_uptime_s")
        self._last_uptime = float(up) if up else None

    def _comm_ok(self) -> None:
        if self.online is not True:
            if self.online is False:
                self.event("comm_restored", "Comunicación restablecida", alert=True, level="info")
            self.online = True
            self.set_status("online", "En línea")
        self.fails = 0

    def _comm_failed(self, err: Exception) -> float:
        self.fails += 1
        msg = f"{type(err).__name__}: {err}"
        log.warning("[%s] sin respuesta (%d): %s", self.drive.ip, self.fails, msg)
        if self.fails >= 2 and self.online is not False:
            self.online = False
            self.event("comm_lost", "Pérdida de comunicación con el drive", msg, alert=True, level="error")
        if self.online is False:
            self.set_status("offline", f"Sin comunicación ({self.fails} intentos)")
        return min(float(self.settings.data["max_backoff_s"]), 2.0 * (2 ** (self.fails - 1)))

    def _maybe_purge(self) -> None:
        now = time.time()
        if now - self._last_purge < PURGE_EVERY_S:
            return
        self._last_purge = now
        for page, days in self.settings.data.get("retention_days", {}).items():
            try:
                if days and float(days) > 0:
                    n = self.db.purge_samples(self.drive.id, page, now - float(days) * 86400)
                    if n:
                        log.info("[%s] depuradas %d muestras antiguas de %s", self.drive.ip, n, page)
            except Exception:  # noqa: BLE001
                log.exception("Error al depurar muestras")

    # --- lectura de una página ---------------------------------------------
    def _poll_page(self, page: str) -> bool | None:
        url = self.urls[page]
        try:
            resp = self.client.get(url)
        except UnsafeRequest as e:
            log.error("[%s] %s", self.drive.ip, e)
            self.urls.pop(page, None)
            return None
        if resp.status == 404:
            log.warning("[%s] %s devolvió 404; se buscará otra vez en el menú", self.drive.ip, url)
            self.db.add_parse_error(self.drive.id, page, url, "HTTP 404", "")
            self.urls.pop(page, None)
            self.db.set_page_urls(self.drive.id, {k: (v, v) for k, v in self.urls.items()})
            return None
        if not resp.ok:
            self.db.add_parse_error(self.drive.id, page, url, f"HTTP {resp.status}", resp.body[:500].decode("latin-1"))
            return False
        ts = time.time()
        try:
            self._process(page, resp.url, resp.body, resp.content_type, ts)
        except Exception as e:  # noqa: BLE001 - un formato inesperado no detiene nada
            log.exception("[%s] error al procesar %s", self.drive.ip, page)
            self.db.add_parse_error(self.drive.id, page, url, f"{type(e).__name__}: {e}",
                                    resp.body[:2000].decode("latin-1"))
            self.emit("parse_error", page=page, error=str(e))
        return True

    def _process(self, page: str, url: str, body: bytes, ctype: str, ts: float) -> None:
        rows = extract_rows(url, body, ctype)
        kv = rows_to_kv(rows)
        if not kv:
            self.db.add_parse_error(self.drive.id, page, url, "La página no trajo datos reconocibles",
                                    body[:2000].decode("latin-1"))
            return
        pdef = PAGE_BY_KEY[page]
        triples = [(k, v, parse_number(v)) for k, v in kv]

        if page == "fault_log":
            self._process_faults(rows, triples, body, ts)
        elif pdef.mode == "periodic":
            self.db.insert_samples(self.drive.id, page, ts, triples)
        else:
            stable = [(k, v) for k, v in kv if not VOLATILE_RE.search(k)]
            volatile = [(k, v, n) for k, v, n in triples if VOLATILE_RE.search(k)]
            if volatile:
                self.db.insert_samples(self.drive.id, page, ts, volatile)
            changes = self.db.update_info(self.drive.id, page, stable, ts)
            self._report_info_changes(page, changes)

        if page != "fault_log" and pdef.mode == "periodic":
            # Firmware que aparezca en páginas periódicas (p. ej. FW REV del encoder).
            fw = [(k, v) for k, v in kv if FIRMWARE_RE.search(k)]
            if fw:
                changes = self.db.update_info(self.drive.id, page, fw, ts, remove_missing=False)
                self._report_info_changes(page, changes)

        self._check_uptime(triples)
        if page == "encoder":
            self._check_encoder(triples)
        if page in NETWORK_PAGES:
            self._check_network(page, triples)
        self.emit("page", page=page, ts=ts, kv=kv)

    def _report_info_changes(self, page: str, changes: list) -> None:
        real = [(k, o, n) for k, o, n in changes if o is not None]
        if not real:
            return
        label = PAGE_BY_KEY[page].label
        detail = "; ".join(f"{k}: {o!r} -> {n!r}" for k, o, n in real[:20])
        fw = [c for c in real if FIRMWARE_RE.search(c[0])]
        if fw:
            self.event("firmware_change", f"Cambio de firmware ({label})",
                       "; ".join(f"{k}: {o!r} -> {n!r}" for k, o, n in fw), alert=True)
        if page == "network_settings":
            self.event("network_config_change", "Cambio en la configuración de red", detail, alert=True)
        elif not fw or len(fw) < len(real):
            self.event("info_change", f"Cambio en {label}", detail)

    def _process_faults(self, rows, triples, body: bytes, ts: float) -> None:
        h = hashlib.sha1(body).hexdigest()
        if h != self._fault_hash:
            # El contenido genérico del log solo se guarda cuando cambia.
            self.db.insert_samples(self.drive.id, "fault_log", ts, triples)
            self._fault_hash = h
        faults = parse_fault_rows(rows)
        if not faults and any("fault" in k.lower() for k, _v, _n in triples):
            self.db.add_parse_error(self.drive.id, "fault_log", self.urls.get("fault_log", ""),
                                    "No se reconoció ninguna falla en el Fault Log", body[:2000].decode("latin-1"))
        baseline_done = self.db.get_state(self.drive.id, "faultlog_baseline") == "1"
        new = []
        for rec in reversed(faults):  # de la más antigua a la más nueva
            fid = self.db.insert_fault(self.drive.id, rec, ts)
            if fid is not None:
                new.append(rec)
        if not baseline_done:
            self.db.set_state(self.drive.id, "faultlog_baseline", "1")
            if new:
                self.event("faultlog_initial", f"Fault Log inicial: {len(new)} " + ("falla registrada" if len(new) == 1 else "fallas registradas"), level="info")
            self.emit("faults", new=len(new), initial=True)
            return
        for rec in new:
            title = f"Falla {rec.fault_id}" + (f".{rec.sub_code}" if rec.sub_code else "")
            self.emit("alert", kind="new_fault", level="error",
                      title=f"{self.drive.name}: {title}", message=rec.text or rec.raw)
        if new:
            self.emit("faults", new=len(new), initial=False)

    def _check_uptime(self, triples) -> None:
        for k, v, _n in triples:
            if UPTIME_RE.search(k) and "cumulative" not in k.lower():
                secs = parse_duration(v)
                if secs is None:
                    continue
                if self._last_uptime is not None and secs + 5 < self._last_uptime:
                    from .parsers import format_duration
                    self.event("reboot", "Reinicio del drive detectado",
                               f"Uptime bajó de {format_duration(self._last_uptime)} a {format_duration(secs)}",
                               alert=True)
                self._last_uptime = float(secs)
                self.db.set_state(self.drive.id, "last_uptime_s", str(secs))
                return

    def _check_encoder(self, triples) -> None:
        for k, _v, n in triples:
            if n is None or not (RSSI_RE.search(k) or QUALITY_RE.search(k)):
                continue
            name = "RSSI" if RSSI_RE.search(k) else "Quality"
            if n < 100:
                if k not in self._alerted_low:
                    self._alerted_low.add(k)
                    self.event("encoder_low", f"{name} del encoder bajo: {n:g} %", k, alert=True)
            elif k in self._alerted_low:
                self._alerted_low.discard(k)
                self.event("encoder_ok", f"{name} del encoder volvió a {n:g} %", k)

    def _check_network(self, page: str, triples) -> None:
        increments = []
        for k, _v, n in triples:
            if n is None:
                continue
            prev = self._last_num.get((page, k))
            self._last_num[(page, k)] = n
            if prev is None or not NET_ERROR_RE.search(k):
                continue
            if n > prev:
                increments.append(f"{k}: +{n - prev:g} (total {n:g})")
            elif n < prev:
                log.info("[%s] contador %s reiniciado (%g -> %g)", self.drive.ip, k, prev, n)
        if increments:
            self.event("net_errors", f"Errores de red en {PAGE_BY_KEY[page].label}",
                       "; ".join(increments), alert=True)
