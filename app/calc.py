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
        self.defect = {}        # name -> (kind, proc, rule_list)
        self.exclude = set()    # (part, proc) COPQ 제외
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
            self.defect[r["name"]] = (r["kind"], r["process"], parse_alloc_rule(r["alloc_rule"]))
        for r in conn.execute("SELECT * FROM process WHERE copq_exclude=1"):
            self.exclude.add((r["part"], r["name"]))

    def allocate(self, tm_no, defect_name, qty):
        """→ [(process, alloc_qty)] 정수 배분. 알 수 없으면 발생공정 100%."""
        dt = self.defect.get(defect_name)
        route = self.route.get(tm_no, [])
        if not dt:
            proc = route[0] if route else "?"
            return [(proc, qty)]
        kind, gen_proc, rule = dt
        targets = rule if rule else [(gen_proc, 1.0)]
        filt = [(p, r) for p, r in targets if p in route]
        if not filt:
            fp = gen_proc if gen_proc in route else (route[0] if route else gen_proc)
            filt = [(fp, 1.0)]
        allocs = largest_remainder(qty, [r for _, r in filt])
        return [(filt[i][0], allocs[i]) for i in range(len(filt)) if allocs[i] > 0]

    def kind_of(self, defect_name):
        dt = self.defect.get(defect_name)
        return dt[0] if dt else "공정"

    def resolve(self, tm_no, defect_name, qty, stored_process="", stored_kind=""):
        """→ (kind, [(집계공정, qty)]).
        저장된 공정·구분(사내/외주/폐기)이 있으면 그대로 집계공정 버킷에 100%,
        없으면 기존 배분(allocate)+kind_of 폴백. 결과 공정은 항상 집계버킷으로 정규화."""
        sp = (stored_process or "").strip()
        sk = (stored_kind or "").strip()
        kind = sk or self.kind_of(defect_name)
        if sp:
            return kind, [(db.bucket_of(sp), qty)]
        allocs = [(db.bucket_of(p), q) for p, q in self.allocate(tm_no, defect_name, qty)]
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
        kind, allocs = m.resolve(r["tm_no"], r["defect_name"], r["qty"],
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
            excl = (part, proc) in m.exclude
            if not excl:
                cell["scrap_cost_copq"] += cost

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
    per = defaultdict(lambda: {"qty": 0, "cost": 0.0, "excl": False})
    for r in conn.execute(
            "SELECT d,tm_no,defect_name,qty,part,process,kind FROM defect_entry WHERE status='confirmed'"):
        if r["d"] not in dates:
            continue
        prod = m.product.get(r["tm_no"])
        part = prod[1] if prod else (r["part"] or "")
        if part not in parts:
            continue
        rkind, allocs = m.resolve(r["tm_no"], r["defect_name"], r["qty"],
                                  r["process"], r["kind"])
        if rkind != kind:
            continue
        for proc, q in allocs:
            price = m.price.get((r["tm_no"], proc), 0)
            per[proc]["qty"] += q
            per[proc]["cost"] += q * price / 1000.0
            per[proc]["excl"] = any((p, proc) in m.exclude for p in parts)
    # 생산수량(파트 합, 월)
    prod_qty = 0
    for r in conn.execute("SELECT tm_no, SUM(qty) s FROM production WHERE substr(d,1,7)=? GROUP BY tm_no",
                          ("%04d-%02d" % (y, mth),)):
        pr = m.product.get(r["tm_no"])
        if pr and pr[1] in parts:
            prod_qty += r["s"]
    # 공정 순서는 process 마스터(ord) 기준
    proc_order = [r["name"] for r in conn.execute(
        "SELECT name FROM process WHERE part=? ORDER BY ord, name", (parts[0],))]
    if not proc_order:
        proc_order = sorted(per.keys())
    rows = []
    for proc in proc_order:
        excl_default = any((p, proc) in m.exclude for p in parts)
        d = per.get(proc, {"qty": 0, "cost": 0.0, "excl": excl_default})
        ppm = round(d["qty"] / prod_qty * 1_000_000) if prod_qty else 0
        rows.append({"process": proc, "qty": d["qty"], "prod": prod_qty,
                     "ppm": ppm, "cost": round(d["cost"]), "excl": d["excl"]})
    return rows


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
        if m.kind_of(r["defect_name"]) != "공정":
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
