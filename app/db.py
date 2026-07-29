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
  ord INTEGER NOT NULL DEFAULT 0,          -- 공정 순서(성형1..후처리7)
  UNIQUE(part, name)
);
CREATE TABLE IF NOT EXISTS product (
  tm_no TEXT PRIMARY KEY, name TEXT NOT NULL, part TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 0           -- 중량
);
CREATE TABLE IF NOT EXISTS product_route (
  id INTEGER PRIMARY KEY, tm_no TEXT NOT NULL, seq INTEGER NOT NULL,
  process TEXT NOT NULL, unit_price REAL NOT NULL DEFAULT 0,
  op_code TEXT DEFAULT '',                 -- CSV 공정 칸의 원본 코드 보존
  UNIQUE(tm_no, seq)
);
CREATE TABLE IF NOT EXISTS product_price (
  id INTEGER PRIMARY KEY, tm_no TEXT NOT NULL,
  process TEXT NOT NULL,                   -- 집계공정(성형·소결·정형·가공·기타)
  unit_price REAL NOT NULL DEFAULT 0,      -- 누적단가(원)
  effective_from TEXT NOT NULL DEFAULT '2000-01-01',  -- 이 날짜부터 적용(그 이전 실적은 이전 값 유지)
  UNIQUE(tm_no, process, effective_from)
);
CREATE TABLE IF NOT EXISTS defect_type (
  id INTEGER PRIMARY KEY, part TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL, process TEXT NOT NULL DEFAULT '',   -- 발생공정(공란 허용=입력공정 귀속)
  name TEXT NOT NULL, alloc_rule TEXT DEFAULT '',
  UNIQUE(part, kind, name)
);
CREATE TABLE IF NOT EXISTS defect_entry (
  id INTEGER PRIMARY KEY, d TEXT NOT NULL, tm_no TEXT NOT NULL,
  defect_name TEXT NOT NULL, qty INTEGER NOT NULL,
  part TEXT NOT NULL DEFAULT '',            -- 파트(제품 미지정 폐기 등, 제품 있으면 제품 파트 우선)
  process TEXT NOT NULL DEFAULT '',         -- 집계공정(사내/외주/폐기는 명시, 공란=배분 폴백)
  kind TEXT NOT NULL DEFAULT '',            -- 공정|셋팅 (공란=불량유형 마스터에서 유추)
  source TEXT NOT NULL DEFAULT 'direct',    -- direct|outsource|discard
  status TEXT NOT NULL DEFAULT 'confirmed', -- confirmed|pending
  reg_user TEXT DEFAULT '',
  batch_key TEXT DEFAULT '',                -- 폴더 재적재 idempotent 키(파트|공정|구분|일자)
  reviewed INTEGER NOT NULL DEFAULT 0,      -- 100EA 검토화면에서 사람이 승인/수정/반려했으면 1
  exclude_cost INTEGER NOT NULL DEFAULT 0   -- 1=셋팅불량율에는 포함, Scrap Cost·COPQ 계산에서는 제외
);                                           -- (재스캔 시 reviewed=1 행은 덮어쓰지 않고 보존)
CREATE TABLE IF NOT EXISTS production (
  id INTEGER PRIMARY KEY, d TEXT NOT NULL, tm_no TEXT NOT NULL,
  qty INTEGER NOT NULL, amount REAL NOT NULL DEFAULT 0,  -- 생산금액(천원)
  part TEXT NOT NULL DEFAULT '',                         -- 파일명 파트(마스터 미등록 대비)
  UNIQUE(d, tm_no)
);
CREATE TABLE IF NOT EXISTS svp (
  id INTEGER PRIMARY KEY, ym TEXT NOT NULL, part TEXT NOT NULL,
  amount REAL NOT NULL,  -- 천원
  UNIQUE(ym, part)
);
CREATE TABLE IF NOT EXISTS claim (
  -- 건별 등록(원장). 같은 달에 같은 항목이 여러 건 있어도 서로 덮어쓰지 않는다(incident와 동일 방식).
  -- amount=전표금액, reclaim=협력사 Re-claim(공제분). 집계(COPQ 등)는 amount-reclaim, use_agg=1인 건만.
  id INTEGER PRIMARY KEY, d TEXT NOT NULL, part TEXT NOT NULL,
  customer TEXT DEFAULT '', item TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0,
  reclaim REAL NOT NULL DEFAULT 0, use_agg INTEGER NOT NULL DEFAULT 1,
  content TEXT DEFAULT '', reg_user TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS incident (
  id INTEGER PRIMARY KEY, d TEXT NOT NULL, part TEXT NOT NULL,
  customer TEXT DEFAULT '', content TEXT DEFAULT '', reg_user TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kpi_actual (
  -- 과거 월 KPI 실적(집계 완료값). 원천 데이터가 없는 기간을 채우고,
  -- 있는 기간에도 '보고된 공식 수치'가 계산값보다 우선한다.
  id INTEGER PRIMARY KEY, ym TEXT NOT NULL, part TEXT NOT NULL,
  kpi TEXT NOT NULL, value REAL NOT NULL, unit TEXT DEFAULT '',
  UNIQUE(ym, part, kpi)
);
CREATE TABLE IF NOT EXISTS target (
  id INTEGER PRIMARY KEY, fy INTEGER NOT NULL, part TEXT NOT NULL,
  kpi TEXT NOT NULL, value REAL NOT NULL, unit TEXT DEFAULT '',
  mon INTEGER NOT NULL DEFAULT 0,          -- 0=FY 연간, 1~12=월별(공정/셋팅 불량율)
  UNIQUE(fy, part, kpi, mon)
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  pw_hash TEXT NOT NULL, role TEXT NOT NULL  -- viewer|editor|admin
);
CREATE TABLE IF NOT EXISTS setting (
  k TEXT PRIMARY KEY, v TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS upload_log (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,
  filename TEXT NOT NULL, ok INTEGER NOT NULL, note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS data_check_hidden (
  tm_no TEXT PRIMARY KEY, hidden_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS permission (
  -- editor/viewer 메뉴별 보기/편집 권한(관리자는 항상 전기능이라 여기 저장하지 않음)
  role TEXT NOT NULL, menu_key TEXT NOT NULL,
  can_view INTEGER NOT NULL DEFAULT 1, can_edit INTEGER NOT NULL DEFAULT 0,
  UNIQUE(role, menu_key)
);
CREATE INDEX IF NOT EXISTS ix_defect_d ON defect_entry(d);
CREATE INDEX IF NOT EXISTS ix_prod_d ON production(d);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# 불량율 집계공정 5종 (순서 고정). copq_exclude 기본: 성형=제외.
# 기타 = 후처리 = 압입·밴딩·선별공정을 묶은 버킷.
CANON_PROCESSES = [
    ("성형", 1, 0), ("소결", 2, 0), ("정형", 3, 0), ("가공", 4, 0), ("기타", 5, 0),
]
AGG_PROCESSES = [name for name, _o, _e in CANON_PROCESSES]     # 집계공정 이름

# 제품 라우팅에 쓰이는 물리공정(제품목록 CSV 컬럼 순서). 집계는 bucket_of()로 5종에 매핑.
PHYS_PROCESSES = ["성형", "소결", "정형", "가공", "압입", "밴딩", "후처리"]

# 물리공정(제품 라우팅·일일시트 공정) → 집계버킷 매핑.
# 성형·소결·정형·가공은 그대로, 나머지 후처리 계열은 전부 '기타'.
PHYS_TO_BUCKET = {
    "성형": "성형", "소결": "소결", "정형": "정형", "가공": "가공",
    "압입": "기타", "밴딩": "기타", "선별공정": "기타", "선별": "기타",
    "후처리": "기타", "기타": "기타",
}


def bucket_of(proc):
    """물리공정명 → 집계공정(버킷). 미정의는 그대로 반환."""
    return PHYS_TO_BUCKET.get(str(proc).strip(), str(proc).strip())


def _add_col(conn, table, col, decl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _migrate_target(conn):
    """target UNIQUE(fy,part,kpi) → UNIQUE(fy,part,kpi,mon).
    mon 컬럼이 없으면 테이블을 재생성하고 기존 목표를 연간(mon=0)으로 이관한다."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(target)")]
    if not cols or "mon" in cols:
        return
    old = list(conn.execute("SELECT fy,part,kpi,value,unit FROM target"))
    conn.execute("DROP TABLE target")
    conn.executescript("""
    CREATE TABLE target (
      id INTEGER PRIMARY KEY, fy INTEGER NOT NULL, part TEXT NOT NULL,
      kpi TEXT NOT NULL, value REAL NOT NULL, unit TEXT DEFAULT '',
      mon INTEGER NOT NULL DEFAULT 0,
      UNIQUE(fy, part, kpi, mon)
    );""")
    conn.executemany(
        "INSERT INTO target(fy,part,kpi,value,unit,mon) VALUES(?,?,?,?,?,0)",
        [(r["fy"], r["part"], r["kpi"], r["value"], r["unit"]) for r in old])


def _migrate_product_price(conn):
    """product_price UNIQUE(tm_no,process) → UNIQUE(tm_no,process,effective_from) 로 변경.
    effective_from 컬럼이 없으면 테이블을 재생성하고 기존 단가를 '2000-01-01'(항상 적용) 기준으로 이관한다."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(product_price)")]
    if not cols or "effective_from" in cols:
        return
    old = list(conn.execute("SELECT tm_no,process,unit_price FROM product_price"))
    conn.execute("DROP TABLE product_price")
    conn.executescript("""
    CREATE TABLE product_price (
      id INTEGER PRIMARY KEY, tm_no TEXT NOT NULL, process TEXT NOT NULL,
      unit_price REAL NOT NULL DEFAULT 0,
      effective_from TEXT NOT NULL DEFAULT '2000-01-01',
      UNIQUE(tm_no, process, effective_from)
    );""")
    conn.executemany(
        "INSERT INTO product_price(tm_no,process,unit_price,effective_from) VALUES(?,?,?,'2000-01-01')",
        [(r["tm_no"], r["process"], r["unit_price"]) for r in old])


def _migrate_claim(conn):
    """claim: 월단위 엑셀 집계(ym,part,item,amount, UNIQUE) → 건별 직접입력 원장(d,part,customer,
    item,amount,content)으로 변경. d 컬럼이 없으면 테이블을 재생성하고, 기존 월별 집계는
    그 달 1일(d=ym-01)로, 고객명·내용은 빈칸으로 이관한다."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(claim)")]
    if not cols or "d" in cols:
        return
    old = list(conn.execute("SELECT ym,part,item,amount FROM claim"))
    conn.execute("DROP TABLE claim")
    conn.executescript("""
    CREATE TABLE claim (
      id INTEGER PRIMARY KEY, d TEXT NOT NULL, part TEXT NOT NULL,
      customer TEXT DEFAULT '', item TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0,
      content TEXT DEFAULT '', reg_user TEXT DEFAULT ''
    );""")
    conn.executemany(
        "INSERT INTO claim(d,part,item,amount) VALUES(?,?,?,?)",
        [(f"{r['ym']}-01", r["part"], r["item"], r["amount"]) for r in old])


def _migrate_defect_type(conn):
    """defect_type UNIQUE(name) → UNIQUE(part,kind,name) 로 변경.
    ALTER로 UNIQUE를 못 바꾸므로 part 컬럼이 없으면 테이블을 재생성한다.
    (defect_type은 마스터 Excel에서 재적재되는 데이터라 삭제 안전)."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(defect_type)")]
    if cols and "part" not in cols:
        conn.execute("DROP TABLE defect_type")
        conn.executescript("""
        CREATE TABLE defect_type (
          id INTEGER PRIMARY KEY, part TEXT NOT NULL DEFAULT '',
          kind TEXT NOT NULL, process TEXT NOT NULL DEFAULT '',
          name TEXT NOT NULL, alloc_rule TEXT DEFAULT '',
          UNIQUE(part, kind, name)
        );""")


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    # 기존 DB 마이그레이션 (컬럼 추가)
    _add_col(conn, "process", "ord", "INTEGER NOT NULL DEFAULT 0")
    _add_col(conn, "product", "weight", "REAL NOT NULL DEFAULT 0")
    _add_col(conn, "product_route", "op_code", "TEXT DEFAULT ''")
    _add_col(conn, "defect_entry", "process", "TEXT NOT NULL DEFAULT ''")
    _add_col(conn, "defect_entry", "kind", "TEXT NOT NULL DEFAULT ''")
    _add_col(conn, "defect_entry", "batch_key", "TEXT DEFAULT ''")
    _add_col(conn, "defect_entry", "part", "TEXT NOT NULL DEFAULT ''")
    _add_col(conn, "defect_entry", "reviewed", "INTEGER NOT NULL DEFAULT 0")
    # 성형 공정 작성 셋팅불량: 셋팅불량율에는 포함하되 Scrap Cost·COPQ 계산에서는 제외.
    _add_col(conn, "defect_entry", "exclude_cost", "INTEGER NOT NULL DEFAULT 0")
    _add_col(conn, "production", "part", "TEXT NOT NULL DEFAULT ''")
    _add_col(conn, "claim", "reclaim", "REAL NOT NULL DEFAULT 0")
    _add_col(conn, "claim", "use_agg", "INTEGER NOT NULL DEFAULT 1")
    _add_col(conn, "claim", "tm_no", "TEXT DEFAULT ''")
    _add_col(conn, "claim", "product_name", "TEXT DEFAULT ''")
    _add_col(conn, "incident", "tm_no", "TEXT DEFAULT ''")
    _add_col(conn, "incident", "product_name", "TEXT DEFAULT ''")
    _add_col(conn, "incident", "is_official", "INTEGER NOT NULL DEFAULT 1")
    _migrate_defect_type(conn)
    _migrate_product_price(conn)
    _migrate_claim(conn)
    _migrate_target(conn)
    _prune_noncanonical_processes(conn)
    conn.commit()
    _normalize_tmno(conn)
    _ensure_default_admin(conn)
    _ensure_default_permissions(conn)
    conn.close()


# editor/viewer 권한 매트릭스에 올릴 메뉴 키(사용자 관리는 항상 관리자 전용이라 제외).
PERM_MENU_KEYS = ("dash", "rdetail", "svp", "claim", "incident", "oreview",
                  "data_check", "products", "processes", "defect_types",
                  "scan", "masters", "target")
# 기존 하드코딩 동작과 동일한 기본값: viewer는 전체 보기만, editor는 데이터입력 4개 메뉴만 편집 가능.
_EDITOR_DEFAULT_EDIT = {"svp", "claim", "incident", "oreview"}


def _ensure_default_permissions(conn):
    if conn.execute("SELECT COUNT(*) c FROM permission").fetchone()["c"] > 0:
        return
    rows = []
    for key in PERM_MENU_KEYS:
        rows.append(("viewer", key, 1, 0))
        rows.append(("editor", key, 1, 1 if key in _EDITOR_DEFAULT_EDIT else 0))
    conn.executemany("INSERT INTO permission(role,menu_key,can_view,can_edit) VALUES(?,?,?,?)", rows)
    conn.commit()


def _prune_noncanonical_processes(conn):
    """process 테이블에 집계공정 5종 외의 잔여 항목(예: 과거 테스트로 남은 '선별')이 있으면 정리하고,
    COPQ 제외 플래그를 CANON_PROCESSES 기준으로 맞춘다(현재는 전부 0=제외 없음)."""
    canon = {name: excl for name, _o, excl in CANON_PROCESSES}
    names = ",".join("?" * len(canon))
    conn.execute(f"DELETE FROM process WHERE name NOT IN ({names})", list(canon))
    for name, excl in canon.items():
        conn.execute("UPDATE process SET copq_exclude=? WHERE name=?", (excl, name))


def ensure_processes(conn, part):
    """해당 파트에 표준 공정 7종이 없으면 생성 (CSV 가져오기 시 사용)."""
    for name, ordn, excl in CANON_PROCESSES:
        conn.execute(
            "INSERT INTO process(part,name,copq_exclude,ord) VALUES(?,?,?,?) "
            "ON CONFLICT(part,name) DO UPDATE SET ord=excluded.ord",
            (part, name, excl, ordn))
    conn.commit()


TMNO_TABLES = ("product", "product_route", "product_price", "production", "defect_entry")


def _normalize_tmno(conn):
    """TM-NO 정규화 규칙이 바뀌어도 기존 행이 옛 키로 남지 않게 재정규화한다.
    (588-5 → 588-05, 1632 → 1632-00 규칙 추가분). 이미 정규형이면 아무 것도 하지 않음."""
    from .calc import base_tmno
    for t in TMNO_TABLES:
        rows = [r["tm_no"] for r in conn.execute(
            f"SELECT DISTINCT tm_no FROM {t} WHERE tm_no IS NOT NULL AND tm_no!=''")]
        for old in rows:
            new = base_tmno(old)
            if new == old:
                continue
            try:
                conn.execute(f"UPDATE {t} SET tm_no=? WHERE tm_no=?", (new, old))
            except sqlite3.IntegrityError:
                # 정규화하면 기존 행과 UNIQUE 충돌 → 옛 행은 버린다(정규형이 최신)
                conn.execute(f"DELETE FROM {t} WHERE tm_no=?", (old,))
    conn.commit()


def get_setting(conn, key, default=""):
    r = conn.execute("SELECT v FROM setting WHERE k=?", (key,)).fetchone()
    return r["v"] if r else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO setting(k,v) VALUES(?,?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))
    conn.commit()


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
