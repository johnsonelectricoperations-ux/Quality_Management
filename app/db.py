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
CREATE TABLE IF NOT EXISTS defect_type_pending (
  -- 사내불량 시트에서 발견됐지만 불량유형 마스터에 없는 불량명(2026-07-30 신설).
  -- 마스터에 없으면 적재가 그 열을 건너뛰므로 **수량이 조용히 유실**된다. 그래서 여기에 남겨
  -- 화면에 알람을 띄우고, 관리자가 배분율까지 정해 등록하도록 유도한다.
  -- 마스터에 등록되면 조회 시 자동으로 목록에서 사라진다(마스터와 대조하므로 삭제 불필요).
  id INTEGER PRIMARY KEY, part TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
  qty INTEGER NOT NULL DEFAULT 0,          -- 건너뛴 수량 누계(영향도 파악용)
  first_seen TEXT DEFAULT '', last_seen TEXT DEFAULT '',
  src_file TEXT DEFAULT '', dismissed INTEGER NOT NULL DEFAULT 0,
  UNIQUE(part, kind, name)
);
CREATE TABLE IF NOT EXISTS product_alias (
  -- 대표품명(품명 그룹) 별칭표 — 2026-07-30 신설.
  -- 같은 품목이 사양·조립여부·오타 때문에 여러 품명으로 흩어져 있어(HUB(FS20), ROD GUIDE ASS'Y,
  -- CARRIER PALNET_반가공 …) 품명별 파레토가 쪼개져 의미가 없었다. 그래서 품명 → 대표품명을 둔다.
  -- 비어 있으면 자동 정규화 규칙(괄호/ASS'Y/구두점 제거)을 쓰고, 규칙으로 못 잡는 것(오타 등)만
  -- 여기에 사람이 등록한다. **제품별이 아니라 품명 단위**라 한 줄이 그 품명의 모든 품목에 적용된다.
  id INTEGER PRIMARY KEY, part TEXT NOT NULL, name TEXT NOT NULL,
  group_name TEXT NOT NULL,
  updated_at TEXT DEFAULT '', updated_by TEXT DEFAULT '',
  UNIQUE(part, name)
);
CREATE TABLE IF NOT EXISTS report_cache (
  -- 월마감 보고서 조립 결과(JSON). 발표 중 화면 지연을 막기 위한 캐시일 뿐,
  -- 마감 후 수정불가 규칙은 없다(2026-07-30 확정) — '재계산'으로 언제든 갱신한다.
  ym TEXT PRIMARY KEY, payload TEXT NOT NULL,
  built_at TEXT DEFAULT '', built_by TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS incident_file (
  -- 고객 품질이슈 첨부파일. kind='photo'(불량사진, 보고서에 표시) | 'doc'(세부자료, 보관용).
  -- 나중에 TM-NO별 품질이슈 통계/분석에 쓰려고 incident_id로 묶어 이력을 남긴다.
  id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'doc', orig_name TEXT NOT NULL,
  stored_name TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
  uploaded_at TEXT DEFAULT '', uploaded_by TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_incfile ON incident_file(incident_id);
CREATE TABLE IF NOT EXISTS internal_issue (
  -- 내부품질 issue 건별 등록(원장). Customer Incident와 동일 구조이되 고객명 대신
  -- 문제가 발생한 내부 공정명(process, 5대 집계공정 중 하나)을 관리한다(2026-07-30 신설).
  id INTEGER PRIMARY KEY, d TEXT NOT NULL, part TEXT NOT NULL,
  process TEXT DEFAULT '', process_etc TEXT DEFAULT '', location TEXT DEFAULT '', tm_no TEXT DEFAULT '',
  product_name TEXT DEFAULT '', content TEXT DEFAULT '', defect_qty INTEGER NOT NULL DEFAULT 0,
  cause TEXT DEFAULT '', action TEXT DEFAULT '', is_official INTEGER NOT NULL DEFAULT 1,
  reg_user TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS internal_issue_file (
  id INTEGER PRIMARY KEY, internal_issue_id INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'doc', orig_name TEXT NOT NULL,
  stored_name TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
  uploaded_at TEXT DEFAULT '', uploaded_by TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_intissuefile ON internal_issue_file(internal_issue_id);
CREATE TABLE IF NOT EXISTS report_text (
  -- 월마감 보고서에 사람이 직접 쓰는 서술 항목(예: '주요 업무 진행 현황').
  -- 데이터로 뽑을 수 없는 내용만 여기 둔다. ym='YYYY-MM'(마감월), section=항목키.
  id INTEGER PRIMARY KEY, ym TEXT NOT NULL, section TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  updated_at TEXT DEFAULT '', updated_by TEXT DEFAULT '',
  UNIQUE(ym, section)
);
CREATE TABLE IF NOT EXISTS fy_actual (
  -- FY 연간 실적(월별로 쪼갤 수 없는 과거 집계값). 월마감 보고서의 'FY26' 열 전용.
  -- FY27 이후는 원천 데이터로 시스템이 직접 계산하므로 여기에 넣지 않는다(FY26 일회성 시드).
  -- 구조는 target 테이블과 대칭(fy, part, kpi, value, unit).
  id INTEGER PRIMARY KEY, fy INTEGER NOT NULL, part TEXT NOT NULL,
  kpi TEXT NOT NULL, value REAL NOT NULL, unit TEXT DEFAULT '',
  UNIQUE(fy, part, kpi)
);
CREATE TABLE IF NOT EXISTS fy_claim (
  -- FY 연간 고객 클레임 금액(업체별). 보고서 p14의 'FY26' 열 전용, 단위 만원.
  id INTEGER PRIMARY KEY, fy INTEGER NOT NULL, part TEXT NOT NULL DEFAULT '',
  customer TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0,
  UNIQUE(fy, part, customer)
);
CREATE TABLE IF NOT EXISTS customer (
  -- 고객사 마스터. name=정식명(생산량 파일 '주거래처' E열과 동일하게 유지),
  -- short_name=월마감 보고서 표기명(약칭, 예: 현대트랜시스 지곡→HTS). part는 생산량 파일이
  -- 1파트/2파트로 나뉘어 있어 고객사별로 유일하게 정해진다(2026-07-30 확인, 중복 0건).
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  short_name TEXT NOT NULL DEFAULT '', part TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1, memo TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS permission (
  -- editor/viewer 메뉴별 보기/편집 권한(관리자는 항상 전기능이라 여기 저장하지 않음)
  role TEXT NOT NULL, menu_key TEXT NOT NULL,
  can_view INTEGER NOT NULL DEFAULT 1, can_edit INTEGER NOT NULL DEFAULT 0,
  UNIQUE(role, menu_key)
);
CREATE TABLE IF NOT EXISTS capa (
  -- 개선대책서(현장품질회의 발표자료) 원장 — 2026-08-10 신설.
  -- 사내에서 쓰던 1장짜리 대책서 양식(주황 제목바 + 4분할)을 그대로 담는다. 항목 이름은
  -- 8D(D2 문제정의 / D3 임시조치 / D4 원인분석 / D5·D6 근본대책 / D7 표준화·수평전개)에
  -- 대응시켜 두었다 — 고객이 8D를 요구하면 이 데이터로 8D 서식을 뽑기 위함(2026-08-10 확정).
  --
  -- kind(이슈구분)로 입력칸이 갈린다: 설비 이슈("소결로 벨트 파손")처럼 특정 품번을 지정할 수
  -- 없는 건이 실제로 많아서 **tm_no는 필수가 아니다**. 대신 모든 이슈가 반드시 갖는
  -- cause_process(원인공정)를 공통 축으로 쓴다.
  -- cause_process(원인공정)와 found_process(발견공정)는 반드시 따로 둔다 — 성형에서 만들어져
  -- 정형에서 발견되는 식이라, 합치면 '왜 우리 검사가 못 걸렀나'(유출) 분석이 불가능해진다.
  id INTEGER PRIMARY KEY,
  d TEXT NOT NULL,                          -- 작성일(회의 발표일)
  title TEXT DEFAULT '',                    -- 제목 (예: Pulley 동심도 불량 개선 대책)
  kind TEXT NOT NULL DEFAULT 'product',     -- product(제품불량)|equip(설비)|process(공정·작업)|etc
  part TEXT NOT NULL DEFAULT '',
  cause_process TEXT DEFAULT '',            -- 원인공정
  found_process TEXT DEFAULT '',            -- 발견공정
  tm_no TEXT DEFAULT '', product_name TEXT DEFAULT '',   -- 제품불량일 때만
  defect_type TEXT DEFAULT '',              -- 불량유형(defect_type 마스터 name)
  equipment TEXT DEFAULT '',                -- 작업설비 (예: JHC100/6) — 마스터 없이 자동완성으로 표기 통일
  dept TEXT DEFAULT '',                     -- 작성부서
  writer TEXT DEFAULT '',                   -- 담당관리직
  presenter TEXT DEFAULT '',                -- 발표자
  approver TEXT DEFAULT '', approved_at TEXT DEFAULT '',  -- 결재(서명 대신 이름+일자로 기록)
  -- D2 문제 정의
  symptom TEXT DEFAULT '',                  -- 현상 (예: 동심도 불량)
  occur_date TEXT DEFAULT '',               -- 발생일
  occur_ongoing INTEGER NOT NULL DEFAULT 0, -- '지속 발생'이면 1 (양식에 날짜 대신 이렇게 적는 경우가 많다)
  lot_qty INTEGER NOT NULL DEFAULT 0,       -- 해당 Lot 수량
  defect_qty INTEGER NOT NULL DEFAULT 0,    -- 불량 수량 (불량률은 저장하지 않고 화면에서 계산)
  -- D3 임시 조치
  lot_action TEXT DEFAULT '',               -- 선별|재작업|특채|기타
  lot_action_etc TEXT DEFAULT '',
  interim TEXT DEFAULT '',                  -- 근본개선 완료 전 차기생산 임시조치 방법
  -- D4 원인 분석 (발생/유출을 나눠 적는 것이 이 양식의 핵심)
  cause_occur TEXT DEFAULT '',              -- 발생측면
  cause_flow TEXT DEFAULT '',               -- 유출측면
  cause_4m TEXT DEFAULT '',                 -- 사람|설비|자재|방법|설계 — 원인 유형별 집계용(양식엔 없던 항목)
  status TEXT NOT NULL DEFAULT 'open',      -- open(진행)|done(완료)|hold(보류)
  reg_user TEXT DEFAULT '', updated_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_capa_d ON capa(d);
CREATE INDEX IF NOT EXISTS ix_capa_tm ON capa(tm_no);
CREATE TABLE IF NOT EXISTS capa_tm (
  -- 대책서 1건이 걸리는 **대상 품번들**(2026-08-10 추가).
  -- 실제 대책서(sample-01.pdf 유첨#1)를 보니 한 건에 품번이 여러 개 나온다
  -- (2206·1785·1695·1360… 각각 불량수량이 따로 있음). 대표 품번 하나만 저장하면
  -- 나머지 품번이 TM-NO별 추적에서 통째로 빠지므로 목록으로 받는다.
  -- capa.tm_no에는 이 중 첫 줄(대표)을 복사해 둔다 — 목록·색인용이라 조회는 여기를 본다.
  id INTEGER PRIMARY KEY, capa_id INTEGER NOT NULL,
  tm_no TEXT NOT NULL DEFAULT '', product_name TEXT DEFAULT '',
  defect_qty INTEGER NOT NULL DEFAULT 0,
  memo TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_capa_tm ON capa_tm(capa_id);
CREATE INDEX IF NOT EXISTS ix_capa_tm_no ON capa_tm(tm_no);
CREATE TABLE IF NOT EXISTS capa_action (
  -- 근본대책 항목. 종이 양식에선 대책마다 오른쪽에 일정(8/30)을 형광펜으로 칠해뒀는데,
  -- 회의가 끝나면 아무도 다시 안 본다. 그래서 대책을 **한 덩어리 글이 아니라 항목별로** 쪼개
  -- 각각 기한·완료여부를 갖게 했다 — 기한이 지난 미완료 대책을 시스템이 찾아낼 수 있다.
  id INTEGER PRIMARY KEY, capa_id INTEGER NOT NULL,
  side TEXT NOT NULL DEFAULT 'occur',       -- occur(발생 방지)|flow(유출 방지)
  seq INTEGER NOT NULL DEFAULT 0,
  content TEXT DEFAULT '', due TEXT DEFAULT '',
  done INTEGER NOT NULL DEFAULT 0, done_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_capa_action ON capa_action(capa_id);
CREATE TABLE IF NOT EXISTS capa_std (
  -- 표준 반영 체크(양식의 5종 고정: PFMEA·관리계획서·작업표준·C/Sheet·작업요령서).
  -- '개정'을 골랐으면 일자를 받는다 — 종이로는 '불필요'인지 '안 채운 것'인지 구분이 안 됐다.
  id INTEGER PRIMARY KEY, capa_id INTEGER NOT NULL,
  name TEXT NOT NULL, revised INTEGER NOT NULL DEFAULT 0,   -- 1=개정, 0=불필요
  rev_date TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_capa_std ON capa_std(capa_id);
CREATE TABLE IF NOT EXISTS capa_spread (
  -- 수평전개(횡전개). 양식엔 '대상부품: Pulley 4종'처럼 자유 텍스트로 적혀 있었는데,
  -- tm_no를 같이 받아두면 "이 품번에 전개된 대책이 뭐가 있나"를 역으로 조회할 수 있다.
  id INTEGER PRIMARY KEY, capa_id INTEGER NOT NULL,
  tm_no TEXT DEFAULT '', target TEXT DEFAULT '',            -- 대상부품(표기용)
  applied TEXT DEFAULT '',                                  -- 완료|예정|해당없음
  plan_date TEXT DEFAULT '', memo TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_capa_spread ON capa_spread(capa_id);
CREATE TABLE IF NOT EXISTS capa_file (
  -- 항목별 첨부. 양식의 '유첨'(원인분석·근본개선 상세를 뒷장으로 빼는 것)에 해당한다.
  -- section으로 어느 항목의 첨부인지 구분해야 보고서에서 설명 옆에 그림이 붙는다.
  id INTEGER PRIMARY KEY, capa_id INTEGER NOT NULL,
  section TEXT NOT NULL DEFAULT 'etc',      -- photo(문제부위)|cause(원인분석)|action(근본개선)|etc
  kind TEXT NOT NULL DEFAULT 'doc',         -- photo|doc
  orig_name TEXT NOT NULL, stored_name TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
  uploaded_at TEXT DEFAULT '', uploaded_by TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_capa_file ON capa_file(capa_id);
CREATE INDEX IF NOT EXISTS ix_defect_d ON defect_entry(d);
CREATE INDEX IF NOT EXISTS ix_prod_d ON production(d);
"""

# 개선대책서 선택지 — 화면과 검증에서 같이 쓰려고 한 곳에 모아둔다(2026-08-10).
CAPA_KINDS = [("product", "제품불량"), ("equip", "설비"), ("process", "공정·작업"), ("etc", "기타")]
CAPA_LOT_ACTIONS = ["선별", "재작업", "특채", "기타"]
CAPA_STD_NAMES = ["PFMEA", "관리계획서", "작업표준", "C/Sheet", "작업요령서"]
CAPA_4M = ["사람", "설비", "자재", "방법", "설계"]
CAPA_APPLIED = ["완료", "예정", "해당없음"]


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
    # 월마감 보고서 '고객 품질 ISSUE' 장에 필요한 항목 (2026-07-30 추가)
    _add_col(conn, "incident", "location", "TEXT DEFAULT ''")       # 발생위치 (예: HTS 서산 조립라인)
    _add_col(conn, "incident", "defect_qty", "INTEGER NOT NULL DEFAULT 0")   # 불량수량
    _add_col(conn, "incident", "cause", "TEXT DEFAULT ''")          # 발생원인
    _add_col(conn, "incident", "action", "TEXT DEFAULT ''")         # 개선대책
    # 공정명이 '기타'일 때 실제 공정을 적어두는 상세 항목 (2026-07-30 추가)
    _add_col(conn, "internal_issue", "process_etc", "TEXT DEFAULT ''")
    _migrate_defect_type(conn)
    _migrate_product_price(conn)
    _migrate_claim(conn)
    _migrate_target(conn)
    _prune_noncanonical_processes(conn)
    conn.commit()
    _normalize_tmno(conn)
    _ensure_default_admin(conn)
    _ensure_default_permissions(conn)
    _ensure_default_aliases(conn)
    conn.close()


# 사용자가 확정한 대표품명 규칙(2026-07-30). 끝의 `*`는 접두어 규칙 — 새 품명이 생겨도 같이 묶인다.
# 나머지(HUB(FS20)·VALVE,PISTON·TELES BLOCK(L) 등)는 자동 규칙으로 묶이므로 등록하지 않는다.
_DEFAULT_ALIASES = [
    ("TM PART", "CARRIER*", "CARRIER"),      # CARRIER PALNET/PLANET/-INPUT… 전부 한 그룹
    ("VMS PART", "ROD GUIDE*", "ROD GUIDE"),  # ROD GUIDE, ROD GUIDE ASS'Y
    ("VMS PART", "R/GUIDE*", "ROD GUIDE"),    # R/GUIDE ASSY 도 같은 품목
]


def _ensure_default_aliases(conn):
    """확정 규칙을 없는 것만 채운다. 사람이 고친 값은 절대 덮어쓰지 않는다."""
    for part, name, group in _DEFAULT_ALIASES:
        conn.execute("INSERT OR IGNORE INTO product_alias(part,name,group_name,updated_by) "
                     "VALUES(?,?,?,'기본규칙')", (part, name, group))
    conn.commit()


# editor/viewer 권한 매트릭스에 올릴 메뉴 키(사용자 관리는 항상 관리자 전용이라 제외).
PERM_MENU_KEYS = ("dash", "rdetail", "monthly", "svp", "claim", "incident", "internal_issue", "capa", "oreview",
                  "data_check", "products", "processes", "defect_types",
                  "customers", "scan", "target")
# 기존 하드코딩 동작과 동일한 기본값: viewer는 전체 보기만, editor는 데이터입력 메뉴만 편집 가능.
# capa(개선대책서)는 대책서를 해당 부서가 직접 작성하므로 editor에 편집을 열어둔다(2026-08-10).
_EDITOR_DEFAULT_EDIT = {"svp", "claim", "incident", "internal_issue", "capa", "oreview"}


def _ensure_default_permissions(conn):
    """아직 행이 없는 (role, menu_key) 조합만 기본값으로 채운다.
    메뉴가 새로 추가돼도(PERM_MENU_KEYS에 항목 추가) 그 메뉴만 기본값이 들어가고,
    관리자가 이미 조정해 둔 기존 권한은 건드리지 않는다."""
    have = {(r["role"], r["menu_key"]) for r in
            conn.execute("SELECT role, menu_key FROM permission")}
    rows = []
    for key in PERM_MENU_KEYS:
        if ("viewer", key) not in have:
            rows.append(("viewer", key, 1, 0))
        if ("editor", key) not in have:
            rows.append(("editor", key, 1, 1 if key in _EDITOR_DEFAULT_EDIT else 0))
    if rows:
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
