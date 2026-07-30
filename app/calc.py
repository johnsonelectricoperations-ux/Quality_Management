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

# Warranty·Customer Incident 는 목표가 'FY 연간 합계'다(비율이 아니라 누적 총량).
# 그래서 월별 실적과 나란히 놓을 목표는 FY 목표를 12개월로 균등배분한 값을 쓴다(2026-07-30 확정).
# 누계(FY) 비교에는 균등배분값이 아니라 FY 목표 원본을 그대로 쓴다.
FY_TOTAL_KPIS = ("warranty", "incident")


def monthly_target(annual, kpi):
    """월별 목표. FY 합계로 관리하는 지표(warranty·incident)만 12로 균등배분한다."""
    if annual is None:
        return None
    return annual / 12 if kpi in FY_TOTAL_KPIS else annual


# ── 대표품명(품명 그룹) ─────────────────────────────────
# 같은 품목이 사양·조립여부 표기 때문에 여러 품명으로 흩어져 있어 품명별 파레토가 쪼개진다.
# 아래 규칙으로 대부분 묶이고, 규칙으로 못 잡는 것(오타 등)만 product_alias 표에 사람이 등록한다.
#   HUB(FS20) → HUB   /   VALVE,PISTON → VALVE PISTON   /   ROD GUIDE ASS'Y → ROD GUIDE
# 괄호 밖의 `_반가공` 같은 접미는 **일부러 남긴다** — 다른 품목에서는 구분이 필요할 수 있어
# 규칙으로 지우면 위험하다. 그런 건은 별칭표에서 사람이 판단한다.
_ASSY_RE = re.compile(r"\b(?:ASS'?Y|ASSY|ASM)\b")
_PAREN_RE = re.compile(r"\([^)]*\)")
_PUNCT_RE = re.compile(r"[,\-/]")
_WS_RE = re.compile(r"\s+")


def auto_group_name(name):
    """품명 → 자동 대표품명. 별칭표에 등록이 없을 때 쓰는 기본 규칙."""
    s = (name or "").upper()
    s = _PAREN_RE.sub(" ", s)
    s = _ASSY_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s or (name or "").strip()


def load_alias(conn):
    """사람이 등록한 별칭. 품명 끝에 `*`를 붙이면 그 접두어로 시작하는 품명 전체에 적용된다.

    접두어 규칙이 필요한 이유: 'CARRIER 는 모두 한 그룹' 처럼 앞으로 새 품명이 생겨도 같이
    묶여야 하는 경우가 있다. 이름을 하나하나 등록해 두면 신규 품명이 조용히 빠진다.
    """
    exact, prefix = {}, []
    for r in conn.execute("SELECT part, name, group_name FROM product_alias"):
        nm = (r["name"] or "").strip()
        if nm.endswith("*"):
            prefix.append(((r["part"], nm[:-1].upper()), r["group_name"]))
        else:
            exact[(r["part"], nm)] = r["group_name"]
    prefix.sort(key=lambda kv: -len(kv[0][1]))      # 더 구체적인(긴) 접두어가 이긴다
    return {"exact": exact, "prefix": prefix}


def group_of(alias, part, name):
    """대표품명. 별칭 등록(정확일치 → 접두어)이 있으면 그것, 없으면 자동 규칙."""
    g = alias["exact"].get((part, name))
    if g:
        return g
    up = (name or "").upper()
    for (p, pre), g in alias["prefix"]:
        if p == part and up.startswith(pre):
            return g
    return auto_group_name(name)

# 외주소재불량 Scrap Cost 단가 = 완제품(기타)단가 × 이 배수 (2026-07-29 확정).
# 외주에서 받은 소재 상태의 불량이라 공정단가(성형 0.5배 등)가 아니라 별도 배수를 쓴다.
# 기존 엑셀 실적과 대조한 결과 파트별로 실제 배수가 달라 파트별로 둔다(1PART 0.794 → 0.8).
OUTSOURCE_PRICE_RATIO = {"VMS PART": 0.8, "TM PART": 0.9}
OUTSOURCE_PRICE_RATIO_DEFAULT = 0.8


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

def week_of_month(dstr):
    """해당 날짜가 그 달의 몇 주차인지(1~). 한 주는 **월요일~일요일**이 기준이지만,
    월 경계를 넘지 않도록 첫 주·마지막 주는 달 안쪽으로 잘라낸다.
    (예: 1일이 수요일이면 1주=수~일(5일), 마지막날이 화요일이면 마지막주=월~화.
    다음 달 1일이 수요일이면 그 달의 1주는 다시 수~일로 새로 시작한다 — 주가 월을 넘어가지 않음.)"""
    d = date.fromisoformat(dstr)
    first = d.replace(day=1)
    first_monday = first - timedelta(days=first.weekday())   # weekday(): 월=0 ... 일=6
    d_monday = d - timedelta(days=d.weekday())
    return (d_monday - first_monday).days // 7 + 1


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
        self.price = {}         # (tm, proc) -> 원 (오늘 기준 현재가, 화면 표시용)
        self.price_hist = {}    # (tm, proc) -> [(effective_from, 원), ...] 오름차순
        self.defect = {}        # (part, name) -> (kind, proc, rule_list)
        for r in conn.execute("SELECT * FROM product"):
            self.product[r["tm_no"]] = (r["name"], r["part"])
        for r in conn.execute("SELECT * FROM product_route ORDER BY tm_no, seq"):
            self.route.setdefault(r["tm_no"], []).append(r["process"])
            if r["unit_price"]:
                self.price_hist.setdefault((r["tm_no"], db.bucket_of(r["process"])), []).append(
                    ("2000-01-01", r["unit_price"]))
        # 단가 마스터(집계공정 기준, 효력시작일별 이력)가 있으면 우선 적용
        today = date.today().isoformat()
        for r in conn.execute(
                "SELECT tm_no,process,unit_price,effective_from FROM product_price ORDER BY effective_from"):
            key = (r["tm_no"], r["process"])
            self.price_hist[key] = [x for x in self.price_hist.get(key, [])
                                    if x[0] != r["effective_from"]]
            self.price_hist[key].append((r["effective_from"], r["unit_price"]))
        for key, hist in self.price_hist.items():
            hist.sort(key=lambda x: x[0])
            applicable = [p for ef, p in hist if ef <= today]
            if applicable:
                self.price[key] = applicable[-1]
        for r in conn.execute("SELECT * FROM defect_type"):
            self.defect[(r["part"], r["name"])] = (r["kind"], r["process"], parse_alloc_rule(r["alloc_rule"]))

    def price_on(self, tm_no, proc, d):
        """해당 날짜(d) 시점에 유효한 단가. 효력시작일이 d 이전인 것 중 가장 최근 값을 쓴다
        (미래에 새 단가가 등록돼도 과거 d의 원가는 바뀌지 않는다)."""
        hist = self.price_hist.get((tm_no, proc))
        if not hist:
            return 0
        val = 0
        for ef, p in hist:
            if ef > d:
                break
            val = p
        return val

    def scrap_price(self, tm_no, stored_process, source, d, part=""):
        """Scrap Cost용 단가 — **불량을 발견한 공정(입력공정)** 기준 (2026-07-29 확정).
        불량유형 마스터의 배분규칙은 '공정별 불량수량' 분석에만 쓰고, 비용은 재배분하지 않는다
        (재배분하면 후처리에서 발견된 불량이 성형 0.5배 단가로 계산돼 실패비용이 과소평가된다).
        외주소재불량은 공정단가가 아니라 **완제품(기타)단가 × 파트별 배수**로 계산한다."""
        if source == "outsource":
            ratio = OUTSOURCE_PRICE_RATIO.get(part, OUTSOURCE_PRICE_RATIO_DEFAULT)
            return self.price_on(tm_no, "기타", d) * ratio
        proc = db.bucket_of(stored_process) if stored_process else ""
        return self.price_on(tm_no, proc, d) if proc else 0

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

    def kind_from(self, part, defect_name, stored_kind=""):
        """공정|셋팅 판정만 (resolve()의 배분 계산 없이). 저장된 kind가 있으면 그대로,
        없으면 불량유형 마스터에서 유추한다."""
        return (stored_kind or "").strip() or self.kind_of(part, defect_name)

    def resolve(self, part, tm_no, defect_name, qty, stored_process="", stored_kind=""):
        """→ (kind, [(집계공정, qty)]).

        마스터(defect_type)에 발생공정 또는 배분기준이 **지정돼 있으면** 그 규칙대로 배분한다
        (여러 공정이면 largest_remainder로 정수 배분, 시트에 어느 공정이 기록했는지는 무시).
        마스터에 아무 지정이 없으면(공란="입력공정 100%") 시트에 기록된 공정(stored_process)을
        그대로 100% 사용한다.

        예외: **TM-NO가 없는 건**(폐기의 소결로_산화 등 품목을 특정할 수 없는 공정단위 불량)은
        품목 라우팅이 없어 마스터 배분기준을 적용할 수 없다. 특정 TM-NO에 넣지 않고
        입력에 지정된 공정(stored_process) 100%로만 집계한다."""
        sp = (stored_process or "").strip()
        dt = self.defect.get((part, defect_name))
        kind = (stored_kind or "").strip() or (dt[0] if dt else "공정")
        if not str(tm_no or "").strip() and sp:
            return kind, [(db.bucket_of(sp), qty)]
        has_master_rule = bool(dt and (dt[1] or dt[2]))      # 발생공정 또는 배분기준 지정됨
        if not has_master_rule and sp:
            return kind, [(db.bucket_of(sp), qty)]
        allocs = self.allocate(part, tm_no, defect_name, qty)
        return kind, allocs


# ── 일 단위 집계 (배분·비용 포함) ───────────────────────
def compute_daily(conn, m: Masters):
    """daily[d][part] = 지표 dict. proc[(part,proc)] 상세도 포함.

    파트별 생산량 데이터가 등록된 마지막 날짜(그 파트 production 테이블의 MAX(d))보다 이후 날짜의
    불량은 아직 분모(생산량)가 확정되지 않은 것이므로 집계에서 제외한다 — 생산량이 나중에 등록되면
    다음 계산 시 자동으로 반영된다. 생산량이 아예 없는 파트는 전부 제외(컷오프 없음=집계 안 함)."""
    prod_cutoff = {}
    for p in ("VMS PART", "TM PART"):
        row = conn.execute("SELECT MAX(d) m FROM production WHERE part=?", (p,)).fetchone()
        prod_cutoff[p] = row["m"] or "0000-00-00"

    daily = defaultdict(lambda: defaultdict(lambda: {
        "scrap_qty": 0, "scrap_cost": 0.0, "scrap_cost_copq": 0.0,
        "proc_qty": 0, "set_qty": 0, "prod_qty": 0, "prod_amount": 0.0,
    }))
    pdetail = defaultdict(lambda: {"qty": 0, "cost": 0.0, "excl": False})  # (part,proc) 누적(전체기간)

    for r in conn.execute(
            "SELECT d,tm_no,defect_name,qty,part,process,kind,source,exclude_cost "
            "FROM defect_entry WHERE status='confirmed'"):
        prod = m.product.get(r["tm_no"])
        part = prod[1] if prod else (r["part"] or "")   # 제품 파트 우선, 없으면 저장된 파트(제품 미지정 폐기)
        if not part:
            continue
        if r["d"] > prod_cutoff.get(part, "0000-00-00"):
            continue                            # 그 파트의 생산량이 아직 등록 안 된 날짜 → 지표 제외
        kind = m.kind_from(part, r["defect_name"], r["kind"])
        cell = daily[r["d"]][part]
        cell["scrap_qty"] += r["qty"]
        if kind == "셋팅":
            cell["set_qty"] += r["qty"]
        else:
            cell["proc_qty"] += r["qty"]
        if r["exclude_cost"]:
            continue                            # 성형 작성 셋팅불량: 불량율엔 포함, 비용은 제외
        # 비용은 배분규칙을 쓰지 않고 **발견(입력) 공정** 단가로 계산한다
        price = m.scrap_price(r["tm_no"], r["process"], r["source"], r["d"], part)
        cost = r["qty"] * price / 1000.0                   # 원 → 천원
        cell["scrap_cost"] += cost
        cell["scrap_cost_copq"] += cost                    # COPQ는 성형 등 별도 제외 없이 전체 반영

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
    """집계 대상(use_agg=1) 건만, 전표금액에서 협력사 Re-claim을 뺀 순금액으로 합산."""
    q = ("SELECT COALESCE(SUM(amount-reclaim),0) s FROM claim "
         "WHERE use_agg=1 AND d LIKE ? AND part IN (%s)") % ",".join("?" * len(parts))
    args = [ym + "%"] + list(parts)
    if items:
        q += " AND item IN (%s)" % ",".join("?" * len(items)); args += list(items)
    return conn.execute(q, args).fetchone()["s"]


def incident_count(conn, ym, parts):
    """공식(is_official=1) 건만 KPI 집계에 반영."""
    like = ym + "%"
    q = ("SELECT COUNT(*) c FROM incident WHERE is_official=1 AND d LIKE ? AND part IN (%s)"
         % ",".join("?" * len(parts)))
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
    """공정별 불량수량·생산·ppm·ScrapCost. part는 VMS/TM (통합 아님).

    **수량**은 불량유형 마스터의 배분규칙대로 원인공정에 배분하고,
    **비용**은 불량을 발견한 입력공정에 그대로 물린다(2026-07-29 확정, compute_daily와 동일 기준).
    두 열의 기준이 다르므로 화면에서 이 점을 안내한다."""
    parts = _parts_for(part)
    dates = set(_dates_in_month(y, mth))
    per = defaultdict(lambda: {"qty": 0, "cost": 0.0})
    for r in conn.execute(
            "SELECT d,tm_no,defect_name,qty,part,process,kind,source,exclude_cost "
            "FROM defect_entry WHERE status='confirmed'"):
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
            per[proc]["qty"] += q
        if r["exclude_cost"]:
            continue                    # 성형 작성 셋팅불량: 수량만 반영, 비용 제외
        cost_proc = "기타" if r["source"] == "outsource" else db.bucket_of(r["process"] or "")
        if cost_proc:
            price = m.scrap_price(r["tm_no"], r["process"], r["source"], r["d"], rpart)
            per[cost_proc]["cost"] += r["qty"] * price / 1000.0
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
        prod = prod_by_tm.get(tm, 0)
        ppm = round(a["defect"] / prod * 1_000_000) if prod else None
        rows.append({"tm": tm, "name": m.product.get(tm, ("?",))[0],
                     "prod": prod, "defect": a["defect"], "ppm": ppm, "by": by})
    rows.sort(key=lambda x: x["defect"], reverse=True)
    return rows[:limit]
