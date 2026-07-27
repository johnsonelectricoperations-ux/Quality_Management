# -*- coding: utf-8 -*-
"""계산 엔진: 회계연도(FY), 공정 배분(정수), KPI 집계.

원칙
- FY = 4월~익년 3월. FY 이름은 종료연도 뒤 2자리. 2026-07 → FY27.
- 배분: 불량유형 배분기준 → 품목 라우팅과 교집합 → 정수 배분(최대잉여법).
- COPQ의 Scrap Cost는 '성형' 등 COPQ제외 공정 배분분을 제외.
- 분모(SVP): 월별은 SVP 입력값, 없으면 생산금액 합(추정).
"""
import re
from collections import defaultdict
from datetime import date, timedelta

from . import db


def base_tmno(tm):
    """TM-NO 정규화: 여러 파일의 표기 흔들림을 하나의 base 키로 통일.

    규칙
      1) 접미 알파벳 제거      598-10A / 598-10B → 598-10, 6017-01AJ → 6017-01
      2) 접미부 2자리 0채움    588-5  → 588-05
      3) 접미부 없으면 -00     1632 / 1632-0 / 1632-00 → 1632-00

    안전장치: 접미부가 숫자가 아니면(날짜로 깨진 값 등) 손대지 않고 그대로 둔다.
    """
    s = re.sub(r"[A-Za-z]+$", "", str(tm or "").strip()).strip()
    if not s:
        return ""
    s = s.rstrip("-")                       # '1632-' → '1632'
    head, sep, suf = s.partition("-")
    if not head.isdigit():                  # 숫자 코드가 아니면 원본 유지
        return s
    if sep and not suf.isdigit():           # 날짜 등으로 깨진 접미부는 유지
        return s
    return f"{head}-{int(suf or 0):02d}"    # 접미부 없으면 00

CLAIM_COPQ_ITEMS = ["Warranty", "3rd Party Containment", "Quality Special Freight",
                    "Customer Incident Cost", "Unplanned Inspection & Sorting", "Variance"]


# ── FY 유틸 ─────────────────────────────────────────────
def fy_of(y, m):
    return y + 1 if m >= 4 else y

def fy_label(fy):
    return "FY%02d" % (fy % 100)

def ym_to_fy(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return fy_of(y, m)

def fy_months(fy):
    """FY의 (y,m) 목록 (4월~익년 3월)."""
    out = []
    for m in range(4, 13):
        out.append((fy - 1, m))
    for m in range(1, 4):
        out.append((fy, m))
    return out

def trailing_months(end_y, end_m, n):
    """(end_y,end_m) 포함 직전 n개월 (오래된→최신)."""
    out = []
    y, m = end_y, end_m
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))

def iso_week_of_month(dstr):
    """해당 날짜가 그 달의 몇 주차인지(1~).  단순 (일-1)//7 + 1."""
    d = date.fromisoformat(dstr)
    return (d.day - 1) // 7 + 1


# ── 배분기준 파싱 ───────────────────────────────────────
# 배분기준 형식 D: 배분을 마스터에 고정하지 않고 입력 시트의 공정에 100% 귀속.
INPUT_PROCESS_RULE = "입력공정 100%"


def parse_alloc_rule(rule):
    """'소결,정형' → [(소결,1),(정형,1)] · '정형:70,성형:30' → [(정형,70),(성형,30)]
       '' 또는 '입력공정 100%' → [] (발생공정/입력공정 귀속)."""
    rule = (rule or "").strip()
    if not rule or rule.replace(" ", "") == INPUT_PROCESS_RULE.replace(" ", ""):
        return []
    out = []
    for tok in rule.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            name, r = tok.split(":", 1)
            out.append((name.strip(), float(r)))
        else:
            out.append((tok, 1.0))
    return out


def largest_remainder(qty, ratios):
    """비율대로 정수 배분. 합=qty 보장, 소수점 없음."""
    if not ratios:
        return []
    tot = sum(ratios)
    raw = [qty * r / tot for r in ratios]
    floors = [int(x) for x in raw]
    rem = qty - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in range(rem):
        floors[order[i % len(order)]] += 1
    return floors


# ── 마스터 로드 ─────────────────────────────────────────
class Masters:
    def __init__(self, conn):
        self.product = {}       # tm -> (name, part)
        self.route = {}         # tm -> [proc,...] (순서)
        self.price = {}         # (tm, proc) -> 원
        self.defect = {}        # (part, name) -> (kind, proc, rule_list)
        for r in conn.execute("SELECT * FROM product"):
            self.product[r["tm_no"]] = (r["name"], r["part"])
        for r in conn.execute("SELECT * FROM product_route ORDER BY tm_no, seq"):
            self.route.setdefault(r["tm_no"], []).append(r["process"])
            if r["unit_price"]:
                self.price[(r["tm_no"], db.bucket_of(r["process"]))] = r["unit_price"]
        # 단가 마스터(집계공정 기준)가 있으면 우선 적용
        for r in conn.execute("SELECT tm_no,process,unit_price FROM product_price"):
            self.price[(r["tm_no"], r["process"])] = r["unit_price"]
        for r in conn.execute("SELECT * FROM defect_type"):
            self.defect[(r["part"], r["name"])] = (r["kind"], r["process"], parse_alloc_rule(r["alloc_rule"]))

    def allocate(self, part, tm_no, defect_name, qty):
        """→ [(집계공정, alloc_qty)] 정수 배분(최대잉여법, 소수점 없음).
        마스터에 발생공정/배분기준이 없으면 제품 라우팅의 첫 공정으로 폴백."""
        dt = self.defect.get((part, defect_name))
        route = self.route.get(tm_no, [])
        bucket_route = {db.bucket_of(p) for p in route}      # 이 제품이 실제 거치는 집계공정
        if not dt:
            proc = db.bucket_of(route[0]) if route else "?"
            return [(proc, qty)]
        kind, gen_proc, rule = dt
        targets = rule if rule else [(gen_proc, 1.0)]
        filt = [(p, r) for p, r in targets if p in bucket_route]
        if not filt:
            fp = gen_proc if gen_proc in bucket_route else (db.bucket_of(route[0]) if route else gen_proc)
            filt = [(fp or "?", 1.0)]   # 제품 마스터에 없는 TM-NO는 공정을 특정할 수 없다
        allocs = largest_remainder(qty, [r for _, r in filt])
        return [(filt[i][0], allocs[i]) for i in range(len(filt)) if allocs[i] > 0]

    def kind_of(self, part, defect_name):
        dt = self.defect.get((part, defect_name))
        return dt[0] if dt else "공정"

    def resolve(self, part, tm_no, defect_name, qty, stored_process="", stored_kind=""):
        """→ (kind, [(집계공정, qty)]).

        마스터(defect_type)에 발생공정 또는 배분기준이 **지정돼 있으면** 그 규칙대로 배분한다
        (여러 공정이면 largest_remainder로 정수 배분, 시트에 어느 공정이 기록했는지는 무시).
        마스터에 아무 지정이 없으면(공란="입력공정 100%") 시트에 기록된 공정(stored_process)을
        그대로 100% 사용한다."""
        sp = (stored_process or "").strip()
        dt = self.defect.get((part, defect_name))
        kind = (stored_kind or "").strip() or (dt[0] if dt else "공정")
        has_master_rule = bool(dt and (dt[1] or dt[2]))      # 발생공정 또는 배분기준 지정됨
        if not has_master_rule and sp:
            return kind, [(db.bucket_of(sp), qty)]
        allocs = self.allocate(part, tm_no, defect_name, qty)
        return kind, allocs


# ── 일 단위 집계 (배분·비용 포함) ───────────────────────
def compute_daily(conn, m: Masters):
    """daily[d][part] = 지표 dict. proc[(part,proc)] 상세도 포함."""
    daily = defaultdict(lambda: defaultdict(lambda: {
        "scrap_qty": 0, "scrap_cost": 0.0, "scrap_cost_copq": 0.0,
        "proc_qty": 0, "set_qty": 0, "prod_qty": 0, "prod_amount": 0.0,
    }))
    pdetail = defaultdict(lambda: {"qty": 0, "cost": 0.0, "excl": False})  # (part,proc) 누적(전체기간)

    for r in conn.execute(
            "SELECT d,tm_no,defect_name,qty,part,process,kind FROM defect_entry WHERE status='confirmed'"):
        prod = m.product.get(r["tm_no"])
        part = prod[1] if prod else (r["part"] or "")   # 제품 파트 우선, 없으면 저장된 파트(제품 미지정 폐기)
        if not part:
            continue
        kind, allocs = m.resolve(part, r["tm_no"], r["defect_name"], r["qty"],
                                 r["process"], r["kind"])
        cell = daily[r["d"]][part]
        cell["scrap_qty"] += r["qty"]
        if kind == "셋팅":
            cell["set_qty"] += r["qty"]
        else:
            cell["proc_qty"] += r["qty"]
        for proc, q in allocs:
            price = m.price.get((r["tm_no"], proc), 0)
            cost = q * price / 1000.0                      # 원 → 천원
            cell["scrap_cost"] += cost
            cell["scrap_cost_copq"] += cost                # COPQ는 성형 등 별도 제외 없이 전체 반영

    for r in conn.execute("SELECT d,tm_no,qty,amount FROM production"):
        prod = m.product.get(r["tm_no"])
        if not prod:
            continue
        cell = daily[r["d"]][prod[1]]
        cell["prod_qty"] += r["qty"]
        cell["prod_amount"] += r["amount"]
    return daily


def _sum_cells(daily, dates, parts):
    agg = {"scrap_qty": 0, "scrap_cost": 0.0, "scrap_cost_copq": 0.0,
           "proc_qty": 0, "set_qty": 0, "prod_qty": 0, "prod_amount": 0.0}
    for d in dates:
        if d not in daily:
            continue
        for part in parts:
            c = daily[d].get(part)
            if not c:
                continue
            for k in agg:
                agg[k] += c[k]
    return agg


def _dates_in_month(y, mth):
    d = date(y, mth, 1)
    out = []
    while d.month == mth and d.year == y:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _parts_for(part):
    return ["VMS PART", "TM PART"] if part == "통합" else [part]


SVP_TOTAL_PART = "통합"     # 합계는 자동합산이 아니라 별도 입력값


def svp_of(conn, ym, parts):
    """월 분모(천원). SVP는 **원 단위로 저장**되므로 천원으로 환산해 돌려준다.
    없으면 None(생산금액으로 추정).

    통합 조회(파트 2개 이상)일 때는 별도 입력된 '합계(통합)' 값을 우선 사용하고,
    없을 때만 파트 합으로 계산한다."""
    def _amt(p):
        row = conn.execute("SELECT amount FROM svp WHERE ym=? AND part=?", (ym, p)).fetchone()
        return row["amount"] if row else None

    if len(parts) > 1:
        tot = _amt(SVP_TOTAL_PART)
        if tot is not None:
            return tot / 1000.0
    total, have = 0.0, False
    for p in parts:
        v = _amt(p)
        if v is not None:
            total += v; have = True
    return total / 1000.0 if have else None


def claim_sum(conn, ym, parts, items=None):
    q = "SELECT COALESCE(SUM(amount),0) s FROM claim WHERE ym=? AND part IN (%s)" % \
        ",".join("?" * len(parts))
    args = [ym] + list(parts)
    if items:
        q += " AND item IN (%s)" % ",".join("?" * len(items)); args += list(items)
    return conn.execute(q, args).fetchone()["s"]


def incident_count(conn, ym, parts):
    like = ym + "%"
    q = "SELECT COUNT(*) c FROM incident WHERE d LIKE ? AND part IN (%s)" % ",".join("?" * len(parts))
    return conn.execute(q, [like] + list(parts)).fetchone()["c"]


def kpi_actual_of(conn, ym, part):
    """그 달에 입력된 KPI 실적(공식 집계값) {kpi: value}. 없으면 {}."""
    return {r["kpi"]: r["value"] for r in conn.execute(
        "SELECT kpi,value FROM kpi_actual WHERE ym=? AND part=?", (ym, part))}


def month_kpi(conn, m, daily, y, mth, part):
    """한 달의 KPI dict. 분모/추정 여부 포함.

    입력된 KPI 실적(kpi_actual)이 있으면 그 값이 계산값보다 우선한다
    (과거 월은 원천 데이터가 없고, 보고된 공식 수치가 기준이므로).
    """
    ym = "%04d-%02d" % (y, mth)
    parts = _parts_for(part)
    agg = _sum_cells(daily, _dates_in_month(y, mth), parts)
    svp = svp_of(conn, ym, parts)
    denom = svp if svp is not None else agg["prod_amount"]
    est = svp is None
    claim_total = claim_sum(conn, ym, parts, CLAIM_COPQ_ITEMS)
    warranty = claim_sum(conn, ym, parts, ["Warranty"])
    copq_cost = agg["scrap_cost_copq"] + claim_total
    def pct(a, b):
        return round(a / b * 100, 2) if b else 0.0
    def ppm(a, b):
        return round(a / b * 1_000_000) if b else 0
    # Scrap Quantity = 공정불량 + 셋팅불량, 분모는 입고수량(생산수량).
    scrap_qty = agg["proc_qty"] + agg["set_qty"]
    out = {
        "ym": ym,
        "scrap_cost": round(agg["scrap_cost"]),
        "scrap_cost_pct": pct(agg["scrap_cost"], denom),
        "scrap_qty": scrap_qty,
        "scrap_qty_pct": pct(scrap_qty, agg["prod_qty"]),
        "copq_cost": round(copq_cost),
        "copq_pct": pct(copq_cost, denom),
        "warranty": round(warranty),
        "incident": incident_count(conn, ym, parts),
        "proc_ppm": ppm(agg["proc_qty"], agg["prod_qty"]),
        "set_ppm": ppm(agg["set_qty"], agg["prod_qty"]),
        "proc_qty": agg["proc_qty"],
        "set_qty": agg["set_qty"],
        "prod_qty": agg["prod_qty"],
        "denom": round(denom),
        "denom_est": est,
    }
    actual = kpi_actual_of(conn, ym, part)
    if actual:
        out.update(actual)                 # 입력된 실적이 계산값보다 우선
        out["from_actual"] = sorted(actual)
    return out


def target_map(conn, fy, part):
    out = {}
    for r in conn.execute("SELECT kpi,value,unit FROM target WHERE fy=? AND part=?", (fy, part)):
        out[r["kpi"]] = (r["value"], r["unit"])
    return out


# ── 공정별 집계 (한 달) ─────────────────────────────────
def process_breakdown(conn, m, y, mth, part, kind):
    """공정별 불량수량·생산·ppm·ScrapCost. part는 VMS/TM (통합 아님)."""
    parts = _parts_for(part)
    dates = set(_dates_in_month(y, mth))
    per = defaultdict(lambda: {"qty": 0, "cost": 0.0})
    for r in conn.execute(
            "SELECT d,tm_no,defect_name,qty,part,process,kind FROM defect_entry WHERE status='confirmed'"):
        if r["d"] not in dates:
            continue
        prod = m.product.get(r["tm_no"])
        rpart = prod[1] if prod else (r["part"] or "")
        if rpart not in parts:
            continue
        rkind, allocs = m.resolve(rpart, r["tm_no"], r["defect_name"], r["qty"],
                                  r["process"], r["kind"])
        if rkind != kind:
            continue
        for proc, q in allocs:
            price = m.price.get((r["tm_no"], proc), 0)
            per[proc]["qty"] += q
            per[proc]["cost"] += q * price / 1000.0
    # 생산수량(파트 합, 월)
    prod_qty = 0
    for r in conn.execute("SELECT tm_no, SUM(qty) s FROM production WHERE substr(d,1,7)=? GROUP BY tm_no",
                          ("%04d-%02d" % (y, mth),)):
        pr = m.product.get(r["tm_no"])
        if pr and pr[1] in parts:
            prod_qty += r["s"]
    # 공정별 불량현황은 집계공정 5종만 표시(성형·소결·정형·가공·기타)
    proc_order = list(db.AGG_PROCESSES)
    rows = []
    for proc in proc_order:
        d = per.get(proc, {"qty": 0, "cost": 0.0})
        ppm = round(d["qty"] / prod_qty * 1_000_000) if prod_qty else 0
        rows.append({"process": proc, "qty": d["qty"], "prod": prod_qty,
                     "ppm": ppm, "cost": round(d["cost"])})
    return rows


# ── 세부지표현황 (다축 분석) ────────────────────────────
PERIOD_UNITS = ("월", "분기", "년")     # 주별은 현 데이터(월말 1일 몰림)에서 왜곡되어 제외


def period_key(dstr, unit):
    """일자 → 집계 기간 키. 년(FY)은 4월~익년 3월 기준."""
    y, mth = int(dstr[:4]), int(dstr[5:7])
    if unit == "월":
        return f"{y:04d}-{mth:02d}"
    if unit == "분기":
        fy = fy_of(y, mth)
        q = ((mth - 4) % 12) // 3 + 1        # 4~6월=Q1 … 1~3월=Q4
        return f"FY{fy % 100:02d} Q{q}"
    return fy_label(fy_of(y, mth))            # 년 = FY


def _period_sort_key(k):
    """기간 키 정렬용(문자열 사전순이 시간순과 어긋나지 않게)."""
    if k.startswith("FY") and " Q" in k:
        return (int(k[2:4]), int(k[-1]))
    if k.startswith("FY"):
        return (int(k[2:4]), 0)
    return (int(k[:4]), int(k[5:7]))


def _defect_rows(conn, m, date_from, date_to, part, kind=None, procs=None, sources=None):
    """조건에 맞는 불량을 (기간키 계산 전) 정규화해 돌려준다.
    반환: [(d, part, tm_no, defect_name, 집계공정, qty, source)]"""
    parts = _parts_for(part)
    out = []
    for r in conn.execute(
            "SELECT d,tm_no,defect_name,qty,part,process,kind,source FROM defect_entry "
            "WHERE status='confirmed' AND d BETWEEN ? AND ?", (date_from, date_to)):
        prod = m.product.get(r["tm_no"])
        rpart = prod[1] if prod else (r["part"] or "")
        if rpart not in parts:
            continue
        if sources and r["source"] not in sources:
            continue
        rkind, allocs = m.resolve(rpart, r["tm_no"], r["defect_name"], r["qty"],
                                  r["process"], r["kind"])
        if kind and rkind != kind:
            continue
        for proc, q in allocs:
            if procs and proc not in procs:
                continue
            out.append((r["d"], rpart, r["tm_no"], r["defect_name"], proc, q, r["source"]))
    return out


def _prod_by_period(conn, m, date_from, date_to, part, unit, by_tm=False):
    """기간별 생산수량(불량율 분모). by_tm=True면 {(기간,tm): qty}."""
    parts = _parts_for(part)
    agg = defaultdict(int)
    for r in conn.execute("SELECT d,tm_no,qty FROM production WHERE d BETWEEN ? AND ?",
                          (date_from, date_to)):
        prod = m.product.get(r["tm_no"])
        if not prod or prod[1] not in parts:
            continue
        k = period_key(r["d"], unit)
        agg[(k, r["tm_no"]) if by_tm else k] += r["qty"]
    return agg


def analyze(conn, m, kind_of_chart, date_from, date_to, part, unit,
            kind=None, procs=None, sources=None, measure="ppm", topn=10):
    """세부지표현황 공용 분석기.

    kind_of_chart: part | process | defect_type | tm | product_name
    measure: ppm(불량율) | qty(수량)
    반환: {"periods":[기간...], "series":[{"name":..,"values":[..]}], "unit":..., "note":...}
    """
    rows = _defect_rows(conn, m, date_from, date_to, part, kind, procs, sources)
    periods = sorted({period_key(d, unit) for d, *_ in rows}, key=_period_sort_key)
    if not periods:
        return {"periods": [], "series": [], "unit": "", "note": "해당 조건의 데이터가 없습니다."}

    # 분류 축 결정
    def group_of(row):
        d, rpart, tm, dname, proc, q, src = row
        if kind_of_chart == "part":
            return PART_DISPLAY.get(rpart, rpart)
        if kind_of_chart == "process":
            return proc
        if kind_of_chart == "defect_type":
            return dname
        if kind_of_chart == "tm":
            return tm or "(미지정)"
        if kind_of_chart == "product_name":
            p = m.product.get(tm)
            return p[0] if p else "(미등록)"
        return "전체"

    cell = defaultdict(int)          # (group, period) -> qty
    gtotal = defaultdict(int)
    for row in rows:
        g = group_of(row)
        k = period_key(row[0], unit)
        cell[(g, k)] += row[5]
        gtotal[g] += row[5]

    # 상위 N개 + 나머지 합계
    ordered = sorted(gtotal, key=lambda g: gtotal[g], reverse=True)
    keep, rest = ordered[:topn], ordered[topn:]
    note = ""
    if rest:
        # '기타'는 실제 불량유형 이름으로도 쓰이므로 합산 항목은 이름을 분리한다
        other = f"그 외 {len(rest)}개"
        note = f"상위 {topn}개만 표시(나머지 {len(rest)}개는 '{other}'로 합산)"
        for g in rest:
            for k in periods:
                cell[(other, k)] += cell.get((g, k), 0)
        keep = keep + [other]

    # 분모(불량율) 준비
    unit_label = "ppm" if measure == "ppm" else "EA"
    per_tm = kind_of_chart in ("tm", "product_name")
    prod = _prod_by_period(conn, m, date_from, date_to, part, unit, by_tm=per_tm)

    # 그룹별 분모 TM-NO 집합: 불량이 없던 품번도 분모에는 들어가야 한다
    denom_tms = {}
    if per_tm:
        by_name = defaultdict(set)
        if kind_of_chart == "product_name":
            for tmk, pr in m.product.items():
                by_name[pr[0]].add(tmk)
        tms_of = (lambda g: {g}) if kind_of_chart == "tm" else (lambda g: by_name.get(g, set()))
        for g in ordered:                        # 상위 N + 나머지 원본 그룹 전부
            denom_tms[g] = tms_of(g)
        if rest:                                 # 합산 그룹은 포함된 그룹들의 합집합
            denom_tms[other] = set().union(*(denom_tms[g] for g in rest))

    def denom_of(g, k):
        if per_tm:
            return sum(prod.get((k, tmk), 0) for tmk in denom_tms.get(g, ()))
        return prod.get(k, 0)

    series = []
    for g in keep:
        vals, qtys, denoms = [], [], []
        for k in periods:
            q = cell.get((g, k), 0)
            qtys.append(q)
            denom = denom_of(g, k)
            denoms.append(denom)
            if measure == "qty":
                vals.append(q)
            else:
                vals.append(round(q / denom * 1_000_000) if denom else None)
        total_q = sum(qtys)
        series.append({"name": g, "values": vals, "qty": qtys, "denom": denoms, "total": total_q})
    # 표시용 분모 시리즈: per_tm 축은 그룹마다 다르므로 그룹별로, 아니면 공통 1개
    denom_series = ({s["name"]: s["denom"] for s in series} if per_tm
                    else (series[0]["denom"] if series else []))
    return {"periods": periods, "series": series, "unit": unit_label, "note": note,
            "per_tm": per_tm, "denom_series": denom_series}


PART_DISPLAY = {"VMS PART": "1PART", "TM PART": "2PART"}


# ── TOP5 (공정불량, 기간 누적, 파트) ────────────────────
def top5_defect(conn, m, date_from, date_to, part, limit=5):
    parts = _parts_for(part)
    agg = defaultdict(lambda: {"defect": 0, "by": defaultdict(int)})
    for r in conn.execute(
            "SELECT tm_no,defect_name,SUM(qty) s FROM defect_entry "
            "WHERE status='confirmed' AND d BETWEEN ? AND ? GROUP BY tm_no,defect_name",
            (date_from, date_to)):
        prod = m.product.get(r["tm_no"])
        if not prod or prod[1] not in parts:
            continue
        if m.kind_of(prod[1], r["defect_name"]) != "공정":
            continue
        agg[r["tm_no"]]["defect"] += r["s"]
        agg[r["tm_no"]]["by"][r["defect_name"]] += r["s"]
    # 생산량(기간)
    prod_by_tm = defaultdict(int)
    for r in conn.execute("SELECT tm_no,SUM(qty) s FROM production WHERE d BETWEEN ? AND ? GROUP BY tm_no",
                          (date_from, date_to)):
        prod_by_tm[r["tm_no"]] += r["s"]
    rows = []
    for tm, a in agg.items():
        by = sorted(a["by"].items(), key=lambda x: x[1], reverse=True)[:3]
        rows.append({"tm": tm, "name": m.product.get(tm, ("?",))[0],
                     "prod": prod_by_tm.get(tm, 0), "defect": a["defect"], "by": by})
    rows.sort(key=lambda x: x["defect"], reverse=True)
    return rows[:limit]
