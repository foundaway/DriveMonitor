"""Historial en SQLite.

Tiempos: segundos epoch UTC (REAL). La interfaz los muestra en hora local.
Cada hilo usa su propia conexión (modo WAL), así cada drive escribe sin
bloquear a la interfaz.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

from .parsers.fault_log import FaultRecord

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS drives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    ip TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_utc REAL NOT NULL
);
-- URL real de cada página, encontrada en el menú del drive.
CREATE TABLE IF NOT EXISTS page_urls (
    drive_id INTEGER NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
    page TEXT NOT NULL,
    url TEXT NOT NULL,
    menu_url TEXT,
    found_utc REAL NOT NULL,
    PRIMARY KEY (drive_id, page)
);
-- Todos los campos de las páginas periódicas, en formato clave/valor.
CREATE TABLE IF NOT EXISTS samples (
    drive_id INTEGER NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
    page TEXT NOT NULL,
    ts REAL NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    num REAL
);
CREATE INDEX IF NOT EXISTS ix_samples ON samples(drive_id, page, key, ts);
CREATE INDEX IF NOT EXISTS ix_samples_ts ON samples(drive_id, ts);
-- Fallas, una sola vez cada una.
CREATE TABLE IF NOT EXISTS faults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_id INTEGER NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
    dedup_key TEXT NOT NULL,
    first_seen_utc REAL NOT NULL,
    cip_time_utc REAL,
    cip_time_text TEXT,
    uptime_s INTEGER,
    cumulative_uptime_s INTEGER,
    fault_id INTEGER,
    sub_code INTEGER,
    text TEXT,
    raw TEXT,
    UNIQUE (drive_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS ix_faults_time ON faults(drive_id, cip_time_utc);
-- Valor vigente de páginas que solo se registran al cambiar.
CREATE TABLE IF NOT EXISTS info_current (
    drive_id INTEGER NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
    page TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    ts REAL NOT NULL,
    PRIMARY KEY (drive_id, page, key)
);
CREATE TABLE IF NOT EXISTS info_history (
    drive_id INTEGER NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
    page TEXT NOT NULL,
    key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_info_history ON info_history(drive_id, ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_id INTEGER REFERENCES drives(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT
);
CREATE INDEX IF NOT EXISTS ix_events ON events(drive_id, ts);
CREATE TABLE IF NOT EXISTS parse_errors (
    drive_id INTEGER REFERENCES drives(id) ON DELETE CASCADE,
    page TEXT,
    url TEXT,
    ts REAL NOT NULL,
    error TEXT,
    excerpt TEXT
);
CREATE TABLE IF NOT EXISTS drive_state (
    drive_id INTEGER NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (drive_id, key)
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.conn.executescript(SCHEMA)  # executescript maneja su propia transacción
        self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                          (str(SCHEMA_VERSION),))

    # --- conexión ---------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=15, isolation_level=None,
                                check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    class _Tx:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def __enter__(self) -> sqlite3.Connection:
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn

        def __exit__(self, exc_type, exc, tb) -> None:
            self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")

    def tx(self) -> "_Tx":
        return Database._Tx(self.conn)

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # --- drives -----------------------------------------------------------
    def add_drive(self, ip: str, name: str = "") -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO drives(name, ip, enabled, created_utc) VALUES (?, ?, 1, ?)",
                            (name.strip() or ip, ip.strip(), time.time()))
            return int(cur.lastrowid)

    def update_drive(self, drive_id: int, *, name: str | None = None, enabled: bool | None = None) -> None:
        with self.tx() as c:
            if name is not None:
                c.execute("UPDATE drives SET name=? WHERE id=?", (name, drive_id))
            if enabled is not None:
                c.execute("UPDATE drives SET enabled=? WHERE id=?", (1 if enabled else 0, drive_id))

    def remove_drive(self, drive_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM drives WHERE id=?", (drive_id,))

    def drives(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM drives ORDER BY id").fetchall()

    def drive(self, drive_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM drives WHERE id=?", (drive_id,)).fetchone()

    # --- estado por drive -------------------------------------------------
    def get_state(self, drive_id: int, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM drive_state WHERE drive_id=? AND key=?",
                              (drive_id, key)).fetchone()
        return r[0] if r else None

    def set_state(self, drive_id: int, key: str, value: str | None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO drive_state(drive_id, key, value) VALUES (?, ?, ?) "
                      "ON CONFLICT(drive_id, key) DO UPDATE SET value=excluded.value",
                      (drive_id, key, value))

    # --- URLs -------------------------------------------------------------
    def page_urls(self, drive_id: int) -> dict[str, str]:
        rows = self.conn.execute("SELECT page, url FROM page_urls WHERE drive_id=?", (drive_id,))
        return {r["page"]: r["url"] for r in rows}

    def set_page_urls(self, drive_id: int, urls: dict[str, tuple[str, str]]) -> None:
        """urls: {page: (url_de_datos, url_del_menú)}"""
        now = time.time()
        with self.tx() as c:
            c.execute("DELETE FROM page_urls WHERE drive_id=?", (drive_id,))
            c.executemany("INSERT INTO page_urls(drive_id, page, url, menu_url, found_utc) VALUES (?,?,?,?,?)",
                          [(drive_id, p, u, m, now) for p, (u, m) in urls.items()])

    # --- muestras ---------------------------------------------------------
    def insert_samples(self, drive_id: int, page: str, ts: float,
                       kv: Iterable[tuple[str, str, float | None]]) -> int:
        rows = [(drive_id, page, ts, k, v, n) for k, v, n in kv]
        if rows:
            with self.tx() as c:
                c.executemany("INSERT INTO samples(drive_id, page, ts, key, value, num) VALUES (?,?,?,?,?,?)", rows)
        return len(rows)

    def sample_keys(self, drive_id: int, page: str, numeric_only: bool = False) -> list[str]:
        sql = "SELECT DISTINCT key FROM samples WHERE drive_id=? AND page=?"
        if numeric_only:
            sql += " AND num IS NOT NULL"
        return [r[0] for r in self.conn.execute(sql + " ORDER BY key", (drive_id, page))]

    def series(self, drive_id: int, page: str, key: str, t0: float, t1: float) -> list[tuple[float, float | None, str]]:
        return [(r[0], r[1], r[2]) for r in self.conn.execute(
            "SELECT ts, num, value FROM samples WHERE drive_id=? AND page=? AND key=? AND ts BETWEEN ? AND ? "
            "ORDER BY ts", (drive_id, page, key, t0, t1))]

    def samples_range(self, drive_id: int, page: str, t0: float, t1: float,
                      keys: list[str] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT ts, key, value, num FROM samples WHERE drive_id=? AND page=? AND ts BETWEEN ? AND ?"
        args: list = [drive_id, page, t0, t1]
        if keys:
            sql += f" AND key IN ({','.join('?' * len(keys))})"
            args += keys
        return self.conn.execute(sql + " ORDER BY ts, key", args).fetchall()

    def latest_values(self, drive_id: int, page: str) -> dict[str, tuple[float, str, float | None]]:
        r = self.conn.execute("SELECT MAX(ts) FROM samples WHERE drive_id=? AND page=?",
                              (drive_id, page)).fetchone()
        if not r or r[0] is None:
            return {}
        rows = self.conn.execute("SELECT key, ts, value, num FROM samples WHERE drive_id=? AND page=? AND ts=?",
                                 (drive_id, page, r[0]))
        return {x["key"]: (x["ts"], x["value"], x["num"]) for x in rows}

    def purge_samples(self, drive_id: int | None, page: str, older_than: float) -> int:
        with self.tx() as c:
            if drive_id is None:
                cur = c.execute("DELETE FROM samples WHERE page=? AND ts<?", (page, older_than))
            else:
                cur = c.execute("DELETE FROM samples WHERE drive_id=? AND page=? AND ts<?",
                                (drive_id, page, older_than))
            return cur.rowcount

    # --- fallas -----------------------------------------------------------
    def insert_fault(self, drive_id: int, rec: FaultRecord, first_seen: float) -> int | None:
        """Devuelve el id si la falla es nueva; None si ya estaba registrada."""
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO faults(drive_id, dedup_key, first_seen_utc, cip_time_utc, cip_time_text, "
                "uptime_s, cumulative_uptime_s, fault_id, sub_code, text, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (drive_id, rec.dedup_key, first_seen,
                 rec.cip_time_utc.timestamp() if rec.cip_time_utc else None, rec.cip_time_text,
                 rec.uptime_s, rec.cumulative_uptime_s, rec.fault_id, rec.sub_code, rec.text, rec.raw))
            return int(cur.lastrowid) if cur.rowcount else None

    def faults(self, drive_id: int, t0: float | None = None, t1: float | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT *, COALESCE(cip_time_utc, first_seen_utc) AS t FROM faults WHERE drive_id=?")
        args: list = [drive_id]
        if t0 is not None:
            sql += " AND COALESCE(cip_time_utc, first_seen_utc) >= ?"
            args.append(t0)
        if t1 is not None:
            sql += " AND COALESCE(cip_time_utc, first_seen_utc) <= ?"
            args.append(t1)
        return self.conn.execute(sql + " ORDER BY t DESC, id DESC", args).fetchall()

    def fault_count(self, drive_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM faults WHERE drive_id=?", (drive_id,)).fetchone()[0]

    # --- info (registrar solo cambios) ------------------------------------
    def update_info(self, drive_id: int, page: str, kv: list[tuple[str, str]], ts: float,
                    remove_missing: bool = True) -> list[tuple[str, str | None, str | None]]:
        """Guarda solo lo que cambió. Devuelve [(clave, anterior, nuevo)].
        La primera vez cada clave aparece con anterior = None."""
        changes: list[tuple[str, str | None, str | None]] = []
        with self.tx() as c:
            cur = {r["key"]: r["value"] for r in c.execute(
                "SELECT key, value FROM info_current WHERE drive_id=? AND page=?", (drive_id, page))}
            new = dict(kv)
            for k, v in new.items():
                if k not in cur or cur[k] != v:
                    changes.append((k, cur.get(k), v))
            if remove_missing:
                for k in cur.keys() - new.keys():
                    changes.append((k, cur[k], None))
            for k, old, v in changes:
                if v is None:
                    c.execute("DELETE FROM info_current WHERE drive_id=? AND page=? AND key=?", (drive_id, page, k))
                else:
                    c.execute("INSERT INTO info_current(drive_id, page, key, value, ts) VALUES (?,?,?,?,?) "
                              "ON CONFLICT(drive_id, page, key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                              (drive_id, page, k, v, ts))
                c.execute("INSERT INTO info_history(drive_id, page, key, old_value, new_value, ts) VALUES (?,?,?,?,?,?)",
                          (drive_id, page, k, old, v, ts))
        return changes

    def info_current(self, drive_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT page, key, value, ts FROM info_current WHERE drive_id=? "
                                 "ORDER BY page, rowid", (drive_id,)).fetchall()

    def info_history(self, drive_id: int, limit: int = 5000) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT ts, page, key, old_value, new_value FROM info_history WHERE drive_id=? "
                                 "ORDER BY ts DESC, rowid DESC LIMIT ?", (drive_id, limit)).fetchall()

    # --- eventos y errores ------------------------------------------------
    def add_event(self, drive_id: int | None, kind: str, message: str, details: str = "",
                  ts: float | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO events(drive_id, ts, kind, message, details) VALUES (?,?,?,?,?)",
                      (drive_id, ts or time.time(), kind, message, details))

    def events(self, drive_id: int, limit: int = 2000) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT ts, kind, message, details FROM events WHERE drive_id=? "
                                 "ORDER BY ts DESC, id DESC LIMIT ?", (drive_id, limit)).fetchall()

    def add_parse_error(self, drive_id: int, page: str, url: str, error: str, excerpt: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO parse_errors(drive_id, page, url, ts, error, excerpt) VALUES (?,?,?,?,?,?)",
                      (drive_id, page, url, time.time(), error, excerpt[:2000]))
