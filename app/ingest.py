# -*- coding: utf-8 -*-
"""Excel 적재 및 검증.

공통 규칙
- 'DATA' 시트를 읽고 1행을 헤더로 사용.
- TM-NO는 문자열로만 취급. 셀이 날짜(datetime)로 변환되어 있으면 오류로 거부.
- 마스터는 전체 교체(idempotent). 트랜잭션은 삽입(UNIQUE는 UPSERT).
"""
import csv
import datetime
import io
from openpyxl import load_workbook

from .calc import parse_alloc_rule, base_tmno
from . import db

# CSV 제품목록의 공정 컬럼 (좌→우 = 공정 순서)
CSV_PROC_COLS = ["성형", "소결", "정형", "가공", "압입", "밴딩", "후처리"]


class IngestError(Exception):
    pass


def _rows(path, tm_col=None):
    """DATA 시트를 [dict,...] 로. tm_col(헤더명) 지정 시 문자열 강제/날짜검증."""
    wb = load_workbook(path, data_only=True)
    ws = wb["DATA"] if "DATA" in wb.sheetnames else wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    headers = [str(h).strip() if h is not None else "" for h in next(it)]
    out = []
    for i, raw in enumerate(it, start=2):
        if all(c is None or str(c).strip() == "" for c in raw):
            continue
        row = {headers[j]: raw[j] for j in range(len(headers))}
        if tm_col is not None:
            v = row.get(tm_col)
            if isinstance(v, (datetime.date, datetime.datetime)):
                raise IngestError(
                    f"{i}행 TM-NO가 날짜로 변환됨({v}). 원본 셀을 텍스트로 고쳐 다시 올려주세요.")
            row[tm_col] = "" if v is None else str(v).strip()
        out.append((i, row))
    return headers, out


def _d(v):
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.strftime("%Y-%m-%d")
    return str(v).strip()


def _ym(v):
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.strftime("%Y-%m")
    return str(v).strip()[:7]


def _int(v):
    return int(round(float(v))) if v not in (None, "") else 0


# ── 마스터 ─────────────────────────────────────────────
def ingest_process_master(conn, path):
    _, rows = _rows(path)
    data = []
    for i, r in rows:
        part = str(r.get("파트구분", "")).strip()
        name = str(r.get("공정명", "")).strip()
        excl = 1 if str(r.get("COPQ제외여부", "")).strip().upper() == "Y" else 0
        if not part or not name:
            raise IngestError(f"{i}행: 파트구분/공정명 필수")
        data.append((part, name, excl))
    conn.execute("DELETE FROM process")
    conn.executemany("INSERT INTO process(part,name,copq_exclude) VALUES(?,?,?)", data)
    conn.commit()
    return len(data)


def ingest_product_master(conn, path):
    _, rows = _rows(path, tm_col="TM-NO")
    prods, routes = {}, []
    for i, r in rows:
        tm = r["TM-NO"]
        name = str(r.get("품명", "")).strip()
        part = str(r.get("파트구분", "")).strip()
        seq = _int(r.get("공정순서"))
        proc = str(r.get("공정명", "")).strip()
        price = float(r.get("기준단가(원)") or 0)
        if not tm or not proc:
            raise IngestError(f"{i}행: TM-NO/공정명 필수")
        if not part:
            raise IngestError(f"{i}행: 파트구분 필수 (VMS PART/TM PART)")
        prods[tm] = (name, part)               # 파트는 제품 마스터가 직접 명시
        routes.append((tm, seq, proc, price))
    conn.execute("DELETE FROM product_route")
    conn.execute("DELETE FROM product")
    for tm, (name, part) in prods.items():
        conn.execute("INSERT INTO product(tm_no,name,part) VALUES(?,?,?)", (tm, name, part))
    conn.executemany("INSERT INTO product_route(tm_no,seq,process,unit_price) VALUES(?,?,?,?)", routes)
    conn.commit()
    return len(prods)


def ingest_product_csv(conn, path, part):
    """제품목록 CSV(wide, cp949) 적재. part = 'VMS PART' | 'TM PART'.
    컬럼: TM-NO, 품명, 중량, 성형, 소결, 정형, 가공, 압입, 밴딩, 후처리
    공정 칸에 값이 있으면 라우팅에 포함(좌→우 순서), 값은 op_code로 보존.
    반환: (제품수, 신규수)."""
    raw = open(path, "rb").read()
    text = None
    for enc in ("cp949", "euc-kr", "utf-8-sig", "utf-8"):
        try:
            text = raw.decode(enc); break
        except Exception:
            continue
    if text is None:
        raise IngestError("CSV 인코딩을 인식할 수 없습니다 (cp949/utf-8).")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise IngestError("빈 CSV 파일")
    header = [h.strip() for h in rows[0]]
    idx = {h: i for i, h in enumerate(header)}
    if "TM-NO" not in idx or "품명" not in idx:
        raise IngestError("헤더에 TM-NO / 품명 이 필요합니다.")
    db.ensure_processes(conn, part)

    existing = {r["tm_no"] for r in conn.execute("SELECT tm_no FROM product WHERE part=?", (part,))}
    n = new = 0
    for r in rows[1:]:
        if not r or not str(r[idx["TM-NO"]]).strip():
            continue
        tm = base_tmno(r[idx["TM-NO"]])       # 변형(598-10A)→base(598-10) 합침
        name = str(r[idx["품명"]]).strip() if idx.get("품명", -1) < len(r) else ""
        weight = 0.0
        if "중량" in idx and idx["중량"] < len(r):
            try:
                weight = float(str(r[idx["중량"]]).strip() or 0)
            except ValueError:
                weight = 0.0
        route = []
        for proc in CSV_PROC_COLS:
            if proc in idx and idx[proc] < len(r):
                code = str(r[idx[proc]]).strip()
                if code:
                    route.append((proc, code))
        conn.execute("INSERT INTO product(tm_no,name,part,weight) VALUES(?,?,?,?) "
                     "ON CONFLICT(tm_no) DO UPDATE SET name=excluded.name, part=excluded.part, "
                     "weight=excluded.weight", (tm, name, part, weight))
        conn.execute("DELETE FROM product_route WHERE tm_no=?", (tm,))
        for seq, (proc, code) in enumerate(route, start=1):
            conn.execute("INSERT INTO product_route(tm_no,seq,process,unit_price,op_code) "
                         "VALUES(?,?,?,?,?)", (tm, seq, proc, 0, code))
        n += 1
        if tm not in existing:
            new += 1
    conn.commit()
    return n, new


def _clean_name(v):
    """헤더/불량명 정규화: 개행 제거·공백 정리."""
    return " ".join(str(v or "").replace("\n", " ").split())


# 불량유형 마스터 시트 → 파트 매핑
DEFECT_MASTER_SHEETS = {"DATA_1PART": "VMS PART", "DATA_2PART": "TM PART"}


def ingest_defect_master(conn, path):
    """불량유형 마스터(DATA_1PART/DATA_2PART 2시트) 적재.
    - 구분(공정/셋팅), 발생공정(공란 허용=입력공정 귀속), 불량명, 배분기준.
    - 발생공정 지정 시 집계공정(성형·소결·정형·가공·기타)만 허용.
    반환: (건수, errors[list]). errors 있으면 반영하지 않음."""
    wb = load_workbook(path, data_only=True)
    agg = set(db.AGG_PROCESSES)
    data, errors, seen = [], [], set()
    sheets = [s for s in DEFECT_MASTER_SHEETS if s in wb.sheetnames]
    if not sheets:
        return 0, ["DATA_1PART/DATA_2PART 시트를 찾을 수 없습니다."]
    for sheet in sheets:
        part = DEFECT_MASTER_SHEETS[sheet]
        ws = wb[sheet]
        it = ws.iter_rows(values_only=True)
        headers = [_clean_name(h) for h in next(it)]
        idx = {h: j for j, h in enumerate(headers)}
        need = ["구분", "공정명(발생공정)", "불량명", "배분기준"]
        miss = [h for h in need if h not in idx]
        if miss:
            errors.append(f"[{sheet}] 헤더 누락: {', '.join(miss)}")
            continue
        for i, raw in enumerate(it, start=2):
            if all(c is None or str(c).strip() == "" for c in raw):
                continue
            kind = str(raw[idx["구분"]] or "").strip()
            proc = _clean_name(raw[idx["공정명(발생공정)"]])
            name = _clean_name(raw[idx["불량명"]])
            rule = str(raw[idx["배분기준"]] or "").strip()
            if kind not in ("공정", "셋팅"):
                errors.append(f"[{sheet}] {i}행: 구분은 공정/셋팅만 ('{kind}')")
            if not name:
                errors.append(f"[{sheet}] {i}행: 불량명 필수")
                continue
            key = (part, kind, name)
            if key in seen:
                errors.append(f"[{sheet}] {i}행: 불량명 중복 ('{name}')")
            seen.add(key)
            if proc and proc not in agg:
                errors.append(f"[{sheet}] {i}행: 발생공정 '{proc}'은 집계공정(성형·소결·정형·가공·기타)만 허용")
            for p, _rt in parse_alloc_rule(rule):
                if p not in agg:
                    errors.append(f"[{sheet}] {i}행: 배분기준 공정 '{p}'은 집계공정만 허용")
            data.append((part, kind, proc, name, rule))
    if errors:
        return 0, errors
    conn.execute("DELETE FROM defect_type")
    conn.executemany(
        "INSERT INTO defect_type(part,kind,process,name,alloc_rule) VALUES(?,?,?,?,?)", data)
    conn.commit()
    return len(data), []


# ── 사내불량 일일 양식(폴더 스캔) ──────────────────────
import os
import re

# 파일명: {PART}_{공정}_{구분}불량_{YYYYMMDD}.xlsx
_DAILY_RE = re.compile(r"^(1PART|2PART)_(.+?)_(공정|셋팅)불량_(\d{8})$")
_PART_TOKEN = {"1PART": "VMS PART", "2PART": "TM PART"}
_DAILY_PROCS = {
    "VMS PART": {"성형", "소결", "정형", "가공", "압입", "밴딩", "선별공정"},
    "TM PART": {"성형", "소결", "정형", "가공", "선별공정"},
}


def parse_daily_filename(fname):
    """'1PART_성형_공정불량_20260724.xlsx' → (part, proc, kind, date, batch_key) 또는 None."""
    stem = os.path.splitext(os.path.basename(fname))[0]
    mo = _DAILY_RE.match(stem)
    if not mo:
        return None
    part = _PART_TOKEN[mo.group(1)]
    proc, kind, ymd = mo.group(2), mo.group(3), mo.group(4)
    d = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    return part, proc, kind, d, f"{part}|{proc}|{kind}|{d}"


def _find_header_row(ws, key="TM-NO"):
    for r in range(1, min(ws.max_row, 15) + 1):
        for c in range(1, min(ws.max_column, 10) + 1):
            if str(ws.cell(row=r, column=c).value or "").strip() == key:
                return r
    return None


def ingest_daily_defect_file(conn, path, user=""):
    """사내불량 일일 양식 1개 적재. 행=TM-NO, 열=불량유형, 셀=수량.
    발생공정이 지정된 유형은 그 공정, 공란이면 파일의 공정(시트)에 귀속.
    같은 batch_key(파트|공정|구분|일자)는 삭제 후 재적재(idempotent).
    반환: (적재건수, errors[list])."""
    parsed = parse_daily_filename(path)
    if not parsed:
        return 0, [f"파일명 형식 오류: {os.path.basename(path)} "
                   f"(예: 1PART_성형_공정불량_20260724.xlsx)"]
    part, sheet_proc, kind, d, batch_key = parsed
    if sheet_proc not in _DAILY_PROCS.get(part, set()):
        return 0, [f"{os.path.basename(path)}: '{sheet_proc}'은 {part}의 공정이 아님"]

    # 마스터: (part,kind,name) → 발생공정
    dmap = {(r["part"], r["kind"], r["name"]): (r["process"] or "").strip()
            for r in conn.execute("SELECT part,kind,name,process FROM defect_type")}

    wb = load_workbook(path, data_only=True)
    ws = wb["DATA"] if "DATA" in wb.sheetnames else wb.worksheets[0]
    hr = _find_header_row(ws)
    if hr is None:
        return 0, [f"{os.path.basename(path)}: 헤더행(TM-NO)을 찾을 수 없음"]
    headers = [str(ws.cell(row=hr, column=c).value or "").strip()
               for c in range(1, ws.max_column + 1)]
    tm_col = headers.index("TM-NO") + 1
    # 불량유형 열 = TM-NO/품명/No 이후의 헤더
    fixed = {"No", "TM-NO", "품명", ""}
    dcols = [(c + 1, headers[c]) for c in range(len(headers)) if headers[c] not in fixed]

    rows, errors = [], []
    for r in range(hr + 1, ws.max_row + 1):
        tmv = ws.cell(row=r, column=tm_col).value
        if isinstance(tmv, (datetime.date, datetime.datetime)):
            errors.append(f"{os.path.basename(path)} {r}행: TM-NO가 날짜로 변환됨({tmv})")
            continue
        tmno = base_tmno(tmv)
        if not tmno or tmno == "합계":
            continue
        for c, name in dcols:
            qty = _int(ws.cell(row=r, column=c).value)
            if qty <= 0:
                continue
            key = (part, kind, name)
            if key not in dmap:
                errors.append(f"{os.path.basename(path)}: 불량유형 '{name}'이 {part} {kind} 마스터에 없음")
                continue
            proc = dmap[key] or sheet_proc          # 발생공정 지정 우선, 없으면 시트 공정
            rows.append((d, tmno, name, qty, proc, kind, "direct", user, batch_key))
    if errors:
        return 0, errors
    conn.execute("DELETE FROM defect_entry WHERE batch_key=?", (batch_key,))
    conn.executemany(
        "INSERT INTO defect_entry(d,tm_no,defect_name,qty,process,kind,source,status,reg_user,batch_key) "
        "VALUES(?,?,?,?,?,?,?, 'confirmed', ?,?)", rows)
    conn.commit()
    return len(rows), []


def ingest_daily_defect_folder(conn, folder, user=""):
    """폴더 내 사내불량 일일 양식 전체 스캔 적재. 반환: (파일수, 총건수, 상세[list])."""
    detail, files, total = [], 0, 0
    for fn in sorted(os.listdir(folder)):
        if not fn.lower().endswith((".xlsx", ".xlsm")):
            continue
        if not _DAILY_RE.match(os.path.splitext(fn)[0]):
            continue
        n, errs = ingest_daily_defect_file(conn, os.path.join(folder, fn), user)
        files += 1
        total += n
        detail.append((fn, n, errs))
    return files, total, detail


# ── 트랜잭션 ───────────────────────────────────────────
def ingest_defect_entries(conn, path, source="direct", user="", quarantine_100=False):
    """불량입력/외주소재불량/폐기불량. quarantine_100: 단일TM×단일불량 ≥100 → pending."""
    _, rows = _rows(path, tm_col="TM-NO")
    n, pend = 0, 0
    for i, r in rows:
        tm = r["TM-NO"]; name = str(r.get("불량명", "")).strip(); qty = _int(r.get("수량"))
        if not tm or not name or qty <= 0:
            continue
        status = "pending" if (quarantine_100 and qty >= 100) else "confirmed"
        if status == "pending":
            pend += 1
        conn.execute("INSERT INTO defect_entry(d,tm_no,defect_name,qty,source,status,reg_user)"
                     " VALUES(?,?,?,?,?,?,?)",
                     (_d(r.get("일자")), tm, name, qty, source, status, user))
        n += 1
    conn.commit()
    return n, pend


def ingest_production(conn, path):
    _, rows = _rows(path, tm_col="TM-NO")
    n = 0
    for i, r in rows:
        tm = r["TM-NO"]; qty = _int(r.get("생산수량(EA)"))
        amt = float(r.get("생산금액(천원)") or 0)
        if not tm:
            continue
        conn.execute("INSERT INTO production(d,tm_no,qty,amount) VALUES(?,?,?,?) "
                     "ON CONFLICT(d,tm_no) DO UPDATE SET qty=excluded.qty, amount=excluded.amount",
                     (_d(r.get("일자")), tm, qty, amt))
        n += 1
    conn.commit()
    return n


_PROD_RE = re.compile(r"^(1part|2part)_(\d{8})_(\d{8})$", re.IGNORECASE)


def ingest_production_xlsx(conn, path):
    """생산수량 xlsx(ERP 제품창고재고금액조회) 적재.
    파일명 {1part|2part}_{시작}_{종료}.xlsx → 파트·일자(시작일 기준).
    C열=규격=TM-NO, I열=입고수량=생산수량, J열=입고금액(원)→÷1000=천원.
    변형 TM-NO는 base로 합산. 반환: (건수, errors)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    mo = _PROD_RE.match(stem)
    if not mo:
        return 0, [f"파일명 형식 오류: {os.path.basename(path)} (예: 1part_20260723_20260723.xlsx)"]
    part = "VMS PART" if mo.group(1).lower() == "1part" else "TM PART"
    d = f"{mo.group(2)[:4]}-{mo.group(2)[4:6]}-{mo.group(2)[6:8]}"

    wb = load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    # 헤더행 탐색(규격·입고수량·입고금액 포함)
    hr = None
    for r in range(1, min(ws.max_row, 8) + 1):
        vals = [str(ws.cell(row=r, column=c).value or "").strip() for c in range(1, ws.max_column + 1)]
        if "규격" in vals and "입고수량" in vals:
            hr = r; headers = vals; break
    if hr is None:
        return 0, [f"{os.path.basename(path)}: 헤더행(규격/입고수량)을 찾을 수 없음"]
    c_tm = headers.index("규격") + 1
    c_qty = headers.index("입고수량") + 1
    c_amt = headers.index("입고금액") + 1

    agg = {}  # base_tm -> [qty, amt천원]
    for r in range(hr + 1, ws.max_row + 1):
        first = str(ws.cell(row=r, column=1).value or "").strip()
        if first == "TOTAL":
            continue
        raw = ws.cell(row=r, column=c_tm).value
        if isinstance(raw, (datetime.date, datetime.datetime)):
            continue  # 규격이 날짜면 TM-NO 아님 → 스킵
        tm = base_tmno(raw)
        if not tm:
            continue
        qty = _int(ws.cell(row=r, column=c_qty).value)
        if qty <= 0:
            continue
        amt = float(ws.cell(row=r, column=c_amt).value or 0) / 1000.0  # 원 → 천원
        cur = agg.setdefault(tm, [0, 0.0])
        cur[0] += qty; cur[1] += amt
    for tm, (qty, amt) in agg.items():
        conn.execute(
            "INSERT INTO production(d,tm_no,qty,amount,part) VALUES(?,?,?,?,?) "
            "ON CONFLICT(d,tm_no) DO UPDATE SET qty=excluded.qty, amount=excluded.amount, part=excluded.part",
            (d, tm, qty, amt, part))
    conn.commit()
    return len(agg), []


def ingest_svp(conn, path):
    _, rows = _rows(path)
    n = 0
    for i, r in rows:
        conn.execute("INSERT INTO svp(ym,part,amount) VALUES(?,?,?) "
                     "ON CONFLICT(ym,part) DO UPDATE SET amount=excluded.amount",
                     (_ym(r.get("년월")), str(r.get("파트", "")).strip(), float(r.get("금액(천원)") or 0)))
        n += 1
    conn.commit()
    return n


def ingest_claim(conn, path):
    _, rows = _rows(path)
    n = 0
    for i, r in rows:
        conn.execute("INSERT INTO claim(ym,part,item,amount) VALUES(?,?,?,?) "
                     "ON CONFLICT(ym,part,item) DO UPDATE SET amount=excluded.amount",
                     (_ym(r.get("년월")), str(r.get("파트", "")).strip(),
                      str(r.get("항목", "")).strip(), float(r.get("금액(천원)") or 0)))
        n += 1
    conn.commit()
    return n


def ingest_incident(conn, path):
    _, rows = _rows(path)
    n = 0
    for i, r in rows:
        conn.execute("INSERT INTO incident(d,part,customer,content) VALUES(?,?,?,?)",
                     (_d(r.get("일자")), str(r.get("파트", "")).strip(),
                      str(r.get("고객", "")).strip(), str(r.get("내용", "")).strip()))
        n += 1
    conn.commit()
    return n
