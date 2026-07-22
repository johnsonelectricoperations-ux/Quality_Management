# -*- coding: utf-8 -*-
"""SQLite 연결 및 스키마.

TM-NO는 전 구간 TEXT로 저장(날짜 자동변환 방지). 원본 입력은 보존하고
배분/KPI는 계산 시점에 산출한다.
"""
import os
import sqlite3
import hashlib

DB_PATH = os.environ.get("QMS_DB", os.path.join(os.path.dirname(__file__), "..", "qms.db"))
DB_PATH = os.path.abspath(DB_PATH)

SCHEMA = """
CREATE TABLE IF NOT EXISTS process (
  id INTEGER PRIMARY KEY, part TEXT NOT NULL, name TEXT NOT NULL,
  copq_exclude INTEGER NOT NULL DEFAULT 0,
  UNIQUE(part, name)
);
CREATE TABLE IF NOT EXISTS product (
  tm_no TEXT PRIMARY KEY, name TEXT NOT NULL, part TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_route (
  id INTEGER PRIMARY KEY, tm_no TEXT NOT NULL, seq INTEGER NOT NULL,
  process TEXT NOT NULL, unit_price REAL NOT NULL,
  UNIQUE(tm_no, seq)
);
CREATE TABLE IF NOT EXISTS defect_type (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, process TEXT NOT NULL,
  name TEXT NOT NULL, alloc_rule TEXT DEFAULT '',
  UNIQUE(name)
);
CREATE TABLE IF NOT EXISTS defect_entry (
  id INTEGER PRIMARY KEY, d TEXT NOT NULL, tm_no TEXT NOT NULL,
  defect_name TEXT NOT NULL, qty INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'direct',   -- direct|outsource|discard
  status TEXT NOT NULL DEFAULT 'confirmed', -- confirmed|pending
  reg_user TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS production (
  id INTEGER PRIMARY KEY, d TEXT NOT NULL, tm_no TEXT NOT NULL,
  qty INTEGER NOT NULL, amount REAL NOT NULL DEFAULT 0,  -- 생산금액(천원)
  UNIQUE(d, tm_no)
);
CREATE TABLE IF NOT EXISTS svp (
  id INTEGER PRIMARY KEY, ym TEXT NOT NULL, part TEXT NOT NULL,
  amount REAL NOT NULL,  -- 천원
  UNIQUE(ym, part)
);
CREATE TABLE IF NOT EXISTS claim (
  id INTEGER PRIMARY KEY, ym TEXT NOT NULL, part TEXT NOT NULL,
  item TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0,
  UNIQUE(ym, part, item)
);
CREATE TABLE IF NOT EXISTS incident (
  id INTEGER PRIMARY KEY, d TEXT NOT NULL, part TEXT NOT NULL,
  customer TEXT DEFAULT '', content TEXT DEFAULT '', reg_user TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS target (
  id INTEGER PRIMARY KEY, fy INTEGER NOT NULL, part TEXT NOT NULL,
  kpi TEXT NOT NULL, value REAL NOT NULL, unit TEXT DEFAULT '',
  UNIQUE(fy, part, kpi)
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  pw_hash TEXT NOT NULL, role TEXT NOT NULL  -- viewer|editor|admin
);
CREATE TABLE IF NOT EXISTS upload_log (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,
  filename TEXT NOT NULL, ok INTEGER NOT NULL, note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_defect_d ON defect_entry(d);
CREATE INDEX IF NOT EXISTS ix_prod_d ON production(d);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    _ensure_default_admin(conn)
    conn.close()


def hash_pw(pw: str) -> str:
    return hashlib.sha256(("qms$" + pw).encode()).hexdigest()


def _ensure_default_admin(conn):
    cur = conn.execute("SELECT COUNT(*) c FROM users")
    if cur.fetchone()["c"] == 0:
        conn.executemany(
            "INSERT INTO users(username,name,pw_hash,role) VALUES(?,?,?,?)",
            [
                ("admin", "품질관리자", hash_pw("admin"), "admin"),
                ("editor", "입력자", hash_pw("editor"), "editor"),
                ("viewer", "조회자", hash_pw("viewer"), "viewer"),
            ],
        )
        conn.commit()


if __name__ == "__main__":
    init_db()
    print("DB initialized:", DB_PATH)
