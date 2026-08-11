# -*- coding: utf-8 -*-
"""주마감 보고서(월요일 아침 보고용) — 2026-08-10 신설.

**주차 = 금요일 ~ 목요일 (7일).** 월마감·대시보드의 주차(`calc.week_of_month`, 월~일이며
월 경계를 넘지 않음)와 **다른 기준**이므로 그 함수는 건드리지 않고 여기서 따로 계산한다.

왜 목요일에 끊나: 금·토·일 특근분 사내불량 데이터가 **차주 월요일**에야 정리된다.
월요일 아침 보고 시점에 금~일 데이터가 없으므로, 그 주를 목요일까지로 끊어야 보고서에
빈 날이 생기지 않는다(2026-08-10 사용자 확정). 시작을 금요일로 두면 주가 빈틈없이 이어진다.

    8/10(월) 보고 → 7/31(금)~8/6(목)   (금·토·일분은 8/3 월요일에 정리 완료)
    8/17(월) 보고 → 8/7(금)~8/13(목)

주간 금액비율(COPQ·Scrap Cost)의 분모는 SVP인데 SVP는 월 단위 값이라 주간 값이 없다.
그래서 **그 주가 걸친 달의 SVP를 생산금액 비중으로 안분한 추정치**를 쓴다 — 화면에 반드시
'추정'으로 표시한다(2026-08-10 사용자 확정: 넣되 추정치로).
"""
import datetime as _dt
import json
from collections import defaultdict

from . import calc, db, report_kpi

# 캐시 구조·계산이 바뀌면 이 숫자를 올린다 → 저장된 캐시가 자동으로 버려지고 다시 계산된다.
CACHE_VERSION = 5

WEEK_END_WEEKDAY = 3      # 목요일 (월=0 … 일=6)
WEEK_DAYS = 7

CONTENTS = [
    "(1) 주간 KPI 현황 (1PART / 2PART)",
    "(2) 공정별 불량 현황",
    "(3) 주요 품질 이슈",
    "(4) 개선대책 진행 현황",
    "(5) 품질 주요 업무현황",
]

# 사람이 직접 쓰는 서술 항목(주차별). report_text 테이블을 그대로 쓰되 ym 자리에 주차키를 넣는다.
WEEK_SECTIONS = [
    ("last_week_tasks", "지난주 품질 주요 업무현황",
     "지난주에 진행한 주요 업무를 줄바꿈으로 구분해 입력하세요. 비워두면 보고서에서 생략됩니다."),
    ("this_week_tasks", "금주 품질 주요 업무현황",
     "이번 주 예정된 주요 업무를 줄바꿈으로 구분해 입력하세요. 비워두면 보고서에서 생략됩니다."),
]


# ── 주차 계산 ──────────────────────────────────────────
def week_end_of(d):
    """그 날짜가 속한 주(금~목)의 **목요일** 날짜. d가 금요일이면 그 주는 다음 목요일에 끝난다."""
    if isinstance(d, str):
        d = _dt.date.fromisoformat(d)
    # 목요일까지 며칠 남았나 (목=3). 오늘이 목요일이면 0, 금요일이면 6.
    ahead = (WEEK_END_WEEKDAY - d.weekday()) % WEEK_DAYS
    return d + _dt.timedelta(days=ahead)


def week_bounds(wk):
    """주차키(목요일 YYYY-MM-DD) → (금요일, 목요일) 날짜 문자열."""
    end = _dt.date.fromisoformat(wk) if isinstance(wk, str) else wk
    start = end - _dt.timedelta(days=WEEK_DAYS - 1)
    return start.isoformat(), end.isoformat()


def week_label(wk):
    """'8월 2주차 (7/31~8/6)' 형태. 몇 주차인지는 **목요일이 속한 달** 기준으로 센다
    (주가 월을 넘나들기 때문에 기준 날짜를 하나로 정해야 한다)."""
    s, e = week_bounds(wk)
    ed = _dt.date.fromisoformat(e)
    first = ed.replace(day=1)
    first_end = week_end_of(first)               # 그 달의 첫 번째 '목요일 마감' 주
    nth = (ed - first_end).days // WEEK_DAYS + 1
    sd = _dt.date.fromisoformat(s)
    return f"{ed.month}월 {nth}주차 ({sd.month}/{sd.day}~{ed.month}/{ed.day})"


def recent_weeks(conn, n=13):
    """주마감 대상 후보 주차(목요일 키) 목록 — 최신 주가 위로.

    생산량이 등록된 마지막 날짜를 기준으로, **이미 끝난 주**만 후보로 올린다
    (진행 중인 주를 마감하면 반쪽짜리 숫자가 보고되므로)."""
    row = conn.execute("SELECT MAX(d) m FROM production").fetchone()
    last = row["m"] if row and row["m"] else _dt.date.today().isoformat()
    last_d = _dt.date.fromisoformat(last)
    end = week_end_of(last_d)
    if end > last_d:                 # 아직 안 끝난 주 → 직전 주로 내린다
        end -= _dt.timedelta(days=WEEK_DAYS)
    return [(end - _dt.timedelta(days=WEEK_DAYS * i)).isoformat() for i in range(n)]


def _dates_between(d0, d1):
    a, b = _dt.date.fromisoformat(d0), _dt.date.fromisoformat(d1)
    out = []
    while a <= b:
        out.append(a.isoformat())
        a += _dt.timedelta(days=1)
    return out


# ── 주간 KPI ───────────────────────────────────────────
def _svp_estimate(conn, daily, d0, d1, parts):
    """주간 분모(천원) 추정. 주가 걸친 **달마다** 그 달 SVP × (그 주 그 달 생산금액 / 그 달 전체
    생산금액)으로 안분해 더한다. SVP가 없는 달은 그 기간 생산금액을 그대로 쓴다.
    반환: (분모, 추정여부)"""
    by_month = defaultdict(float)                     # 그 주에 속한 날의 달별 생산금액
    for d in _dates_between(d0, d1):
        cell = daily.get(d)
        if not cell:
            continue
        for p in parts:
            c = cell.get(p)
            if c:
                by_month[d[:7]] += c["prod_amount"]
    denom, est = 0.0, False
    for ym, wk_amt in by_month.items():
        svp = calc.svp_of(conn, ym, parts)
        if svp is None:
            denom += wk_amt                            # SVP 미입력 → 생산금액으로 대체
            est = True
            continue
        m_amt = 0.0
        for d in calc._dates_in_month(int(ym[:4]), int(ym[5:7])):
            cell = daily.get(d)
            if not cell:
                continue
            for p in parts:
                c = cell.get(p)
                if c:
                    m_amt += c["prod_amount"]
        denom += svp * (wk_amt / m_amt) if m_amt else 0.0
        est = True          # SVP 자체가 월값이라 주간으로 쪼갠 이상 언제나 추정치다
    return denom, est


def week_kpi(conn, m, daily, wk, part):
    """한 주(금~목)의 KPI dict. 월 KPI(`calc.month_kpi`)와 같은 키를 쓰되 분모는 추정이다.

    클레임·Warranty는 **실제 발생일자로** 그 주에 든 건만 더한다(월 금액을 주별로 안분하지
    않는다 — 주간 보고에서는 언제 터졌는지가 중요하므로)."""
    d0, d1 = week_bounds(wk)
    parts = calc._parts_for(part)
    agg = calc._sum_cells(daily, _dates_between(d0, d1), parts)
    denom, est = _svp_estimate(conn, daily, d0, d1, parts)

    ph = ",".join("?" * len(parts))
    claim_items = ",".join("?" * len(calc.CLAIM_COPQ_ITEMS))
    claim_total = conn.execute(
        f"SELECT COALESCE(SUM(amount-reclaim),0) s FROM claim WHERE use_agg=1 "
        f"AND d BETWEEN ? AND ? AND part IN ({ph}) AND item IN ({claim_items})",
        [d0, d1] + list(parts) + list(calc.CLAIM_COPQ_ITEMS)).fetchone()["s"]
    warranty = conn.execute(
        f"SELECT COALESCE(SUM(amount-reclaim),0) s FROM claim WHERE use_agg=1 "
        f"AND d BETWEEN ? AND ? AND part IN ({ph}) AND item='Warranty'",
        [d0, d1] + list(parts)).fetchone()["s"]
    incident = conn.execute(
        f"SELECT COUNT(*) c FROM incident WHERE is_official=1 AND d BETWEEN ? AND ? "
        f"AND part IN ({ph})", [d0, d1] + list(parts)).fetchone()["c"]

    copq_cost = agg["scrap_cost_copq"] + claim_total
    scrap_cost_r, copq_cost_r, denom_r = round(agg["scrap_cost"]), round(copq_cost), round(denom)

    def pct(a, b):
        return round(a / b * 100, 3) if b else 0.0

    def ppm(a, b):
        return round(a / b * 1_000_000) if b else 0

    scrap_qty = agg["proc_qty"] + agg["set_qty"]
    return {
        "wk": wk, "start": d0, "end": d1,
        "scrap_cost": scrap_cost_r, "scrap_cost_pct": pct(scrap_cost_r, denom_r),
        "scrap_qty": scrap_qty, "scrap_qty_pct": pct(scrap_qty, agg["prod_qty"]),
        "copq_cost": copq_cost_r, "copq_pct": pct(copq_cost_r, denom_r),
        "warranty": round(warranty), "incident": incident,
        "proc_ppm": ppm(agg["proc_qty"], agg["prod_qty"]),
        "set_ppm": ppm(agg["set_qty"], agg["prod_qty"]),
        "proc_qty": agg["proc_qty"], "set_qty": agg["set_qty"], "prod_qty": agg["prod_qty"],
        "denom": denom_r, "denom_est": est,
    }


# 주간 보고에 올릴 지표 — (키, 표시명, 단위, 소수자리, 낮을수록 좋은지, 추정치 여부)
WEEK_METRICS = [
    ("proc_ppm", "공정불량", "PPM", 0, True, False),
    ("set_ppm", "셋팅불량", "PPM", 0, True, False),
    ("scrap_qty_pct", "Scrap Quantity", "%", 3, True, False),
    ("scrap_cost_pct", "Scrap Cost", "%", 3, True, True),
    ("copq_pct", "COPQ", "%", 3, True, True),
    ("incident", "Customer Incident", "건", 0, True, False),
]


def _kpi_block(conn, m, daily, wk, part):
    """지표별 (금주 / 전주 / 증감). 주간 보고의 핵심은 절대값보다 **전주 대비 추세**다."""
    prev = (_dt.date.fromisoformat(wk) - _dt.timedelta(days=WEEK_DAYS)).isoformat()
    cur_k = week_kpi(conn, m, daily, wk, part)
    prv_k = week_kpi(conn, m, daily, prev, part)
    rows = []
    for key, name, unit, dec, lower_better, est in WEEK_METRICS:
        c, p = cur_k.get(key), prv_k.get(key)
        delta = None if (c is None or p is None) else round(c - p, dec) if dec else (c - p)
        rows.append({"key": key, "name": name, "unit": unit, "dec": dec,
                     "cur": c, "prev": p, "delta": delta,
                     "worse": (delta is not None and delta > 0) if lower_better else None,
                     "est": est})
    return {"rows": rows, "cur": cur_k, "prev": prv_k}


TREND_WEEKS = 5       # 주간 KPI 타일에 함께 그리는 최근 주차 수(5주 — 8주는 너무 빽빽하다)

# 지표 → 목표 테이블의 kpi 키. PPM·비율 목표는 월 기준이지만 주간도 같은 '율'이라 그대로 비교한다.
TARGET_KEY = {"proc_ppm": "proc_ppm", "set_ppm": "set_ppm", "scrap_qty_pct": "scrap_qty",
              "scrap_cost_pct": "scrap_cost", "copq_pct": "copq"}


def _trend(conn, m, daily, wk, part, n=TREND_WEEKS):
    """최근 n주 추이 — 타일마다 작은 막대그래프를 그리기 위한 값.

    주간 보고는 절대값보다 흐름이 중요해서(2026-08-10 요청) 숫자 옆에 최근 몇 주를 같이 보여준다.
    맨 오른쪽 막대가 이번 주다."""
    end = _dt.date.fromisoformat(wk)
    wks = [(end - _dt.timedelta(days=WEEK_DAYS * i)).isoformat() for i in range(n - 1, -1, -1)]
    ks = [week_kpi(conn, m, daily, w, part) for w in wks]
    labels = []
    for w in wks:
        s, _e = week_bounds(w)
        sd = _dt.date.fromisoformat(s)
        labels.append(f"{sd.month}/{sd.day}")
    # 목표는 FY 단위로 등록돼 있다. 주가 월을 넘나들 수 있으므로 **목요일(마감일)** 기준으로 잡는다.
    fy = calc.fy_of(int(wk[:4]), int(wk[5:7]))
    out = {}
    for key, _name, _unit, dec, _lb, _est in WEEK_METRICS:
        # 생산 데이터가 아예 없는 주는 **0이 아니라 빈칸**으로 둔다. 0으로 그리면 보고서에서
        # '그 주는 불량 0'으로 읽혀 정반대로 오해된다(2026-08-10).
        vals = [(k.get(key) or 0) if k["prod_qty"] else None for k in ks]
        real = [v for v in vals if v is not None]
        vmax = max(real) if real else 0
        tgt = None
        tk = TARGET_KEY.get(key)
        if tk and fy:
            tgt = _target_val(conn, fy, part, tk)
            if tgt:
                vmax = max(vmax, tgt)
        out[key] = {"labels": labels, "values": vals, "vmax": vmax or 1, "target": tgt, "dec": dec}
    return out


def _target_val(conn, fy, part, kpi):
    fy = fy % 100 if fy >= 100 else fy
    row = conn.execute("SELECT value FROM target WHERE fy=? AND part=? AND kpi=? AND mon=0",
                       (fy, part, kpi)).fetchone()
    return row["value"] if row else None


def _process_block(conn, m, daily, wk, part):
    """공정별(성형·소결·정형·가공·기타) **불량율(PPM)** — 금주/전주 비교(2026-08-10 요청).

    불량율 = 그 공정 불량수량 / 그 파트 **생산수량** × 100만. 분모가 공정별 투입량이 아니라
    파트 생산수량인 것은 KPI(공정불량 PPM)와 같은 기준을 쓰기 위함이다 — 공정별 값을 모두
    더하면 그 파트의 공정불량 PPM이 된다."""
    prev_wk = (_dt.date.fromisoformat(wk) - _dt.timedelta(days=WEEK_DAYS)).isoformat()
    d0, d1 = week_bounds(wk)
    p0, p1 = week_bounds(prev_wk)

    def by_proc(a, b):
        out = defaultdict(int)
        # 공정불량만 센다(셋팅불량 제외) — 월마감 '공정불량 현황'과 같은 기준.
        for (_d, _rp, _tm, _dn, proc, q, _src) in calc._defect_rows(conn, m, a, b, part, kind="공정"):
            out[proc] += q
        return out

    cur, prv = by_proc(d0, d1), by_proc(p0, p1)
    cur_prod = week_kpi(conn, m, daily, wk, part)["prod_qty"]
    prv_prod = week_kpi(conn, m, daily, prev_wk, part)["prod_qty"]

    def ppm(a, b):
        return round(a / b * 1_000_000) if b else 0

    rows = []
    for proc in db.AGG_PROCESSES:
        c, p = cur.get(proc, 0), prv.get(proc, 0)
        cr, pr = ppm(c, cur_prod), ppm(p, prv_prod)
        rows.append({"name": proc, "qty": c, "qty_prev": p,
                     "cur": cr, "prev": pr, "delta": cr - pr})
    tot_c, tot_p = sum(r["qty"] for r in rows), sum(r["qty_prev"] for r in rows)
    return {"rows": rows, "qty": tot_c, "qty_prev": tot_p,
            "cur": ppm(tot_c, cur_prod), "prev": ppm(tot_p, prv_prod),
            "delta": ppm(tot_c, cur_prod) - ppm(tot_p, prv_prod),
            "prod": cur_prod, "prod_prev": prv_prod}


def _top_items(conn, m, wk, part, limit=5):
    """그 주 불량수량 상위 TM-NO + 주요 불량유형(통합 반영)."""
    d0, d1 = week_bounds(wk)
    return calc.top5_defect(conn, m, d0, d1, part, limit=limit)


def _issue_block(conn, wk):
    """그 주에 등록된 내부품질 Issue / 고객 Incident 목록."""
    d0, d1 = week_bounds(wk)
    internal = [dict(r) for r in conn.execute(
        "SELECT d,part,process,tm_no,product_name,content,defect_qty,cause,action "
        "FROM internal_issue WHERE is_official=1 AND d BETWEEN ? AND ? ORDER BY d,id", (d0, d1))]
    customer = [dict(r) for r in conn.execute(
        "SELECT d,part,customer,tm_no,product_name,content,defect_qty,cause,action "
        "FROM incident WHERE is_official=1 AND d BETWEEN ? AND ? ORDER BY d,id", (d0, d1))]
    return {"internal": internal, "customer": customer}


def _capa_block(conn, wk):
    """개선대책 진행 현황 — 월요일 아침에 '지난주에 하기로 한 게 됐나'를 확인하는 장.
    기한초과 / 금주 마감예정 / 지난주 완료 세 덩어리로 나눈다."""
    d0, d1 = week_bounds(wk)
    nxt0 = (_dt.date.fromisoformat(d1) + _dt.timedelta(days=1)).isoformat()
    nxt1 = (_dt.date.fromisoformat(d1) + _dt.timedelta(days=WEEK_DAYS)).isoformat()
    sql = ("SELECT a.id,a.capa_id,a.side,a.content,a.due,a.done,a.done_at, "
           "c.title,c.dept,c.cause_process,c.tm_no,c.equipment "
           "FROM capa_action a JOIN capa c ON c.id=a.capa_id WHERE ")
    over = [dict(r) for r in conn.execute(
        sql + "a.done=0 AND a.due<>'' AND a.due<? ORDER BY a.due", (nxt0,))]
    soon = [dict(r) for r in conn.execute(
        sql + "a.done=0 AND a.due BETWEEN ? AND ? ORDER BY a.due", (nxt0, nxt1))]
    done = [dict(r) for r in conn.execute(
        sql + "a.done=1 AND a.due BETWEEN ? AND ? ORDER BY a.due", (d0, d1))]
    return {"overdue": over, "soon": soon, "done": done}


def _texts(conn, wk):
    saved = {r["section"]: r["content"] for r in conn.execute(
        "SELECT section, content FROM report_text WHERE ym=?", (wk,))}
    return {k: saved.get(k, "") for k, _n, _h in WEEK_SECTIONS}


def build(conn, m, wk):
    """주마감 보고서 payload 조립."""
    daily = calc.compute_daily(conn, m)
    d0, d1 = week_bounds(wk)
    # KPI는 1PART/2PART로만 본다 — 통합은 보지 않는다(2026-08-10 사용자 확정).
    parts = [("VMS PART", "1PART"), ("TM PART", "2PART")]
    return {
        "v": CACHE_VERSION, "wk": wk, "start": d0, "end": d1, "label": week_label(wk),
        "contents": CONTENTS,
        "kpi": {lbl: _kpi_block(conn, m, daily, wk, p) for p, lbl in parts},
        "trend": {lbl: _trend(conn, m, daily, wk, p) for p, lbl in parts},
        "proc": {lbl: _process_block(conn, m, daily, wk, p) for p, lbl in parts},
        "top": {lbl: _top_items(conn, m, wk, p) for p, lbl in parts},
        # 품번이 없는 불량(설비 단위 폐기 등)은 TOP5에 못 들어가므로 건별로 따로 싣는다.
        "unassigned": {lbl: calc.unassigned_defects(conn, m, d0, d1, p, limit=4)
                       for p, lbl in parts},
        "issues": _issue_block(conn, wk),
        "capa": _capa_block(conn, wk),
        "texts": _texts(conn, wk),
        "built_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ── 캐시 ───────────────────────────────────────────────
def get_or_build(conn, m, wk, user=""):
    row = conn.execute("SELECT payload FROM week_cache WHERE wk=?", (wk,)).fetchone()
    if row:
        try:
            p = json.loads(row["payload"])
            if p.get("v") == CACHE_VERSION:
                return p
        except (ValueError, TypeError):
            pass                     # 캐시가 깨졌으면 조용히 다시 만든다
    return rebuild(conn, m, wk, user)


def rebuild(conn, m, wk, user=""):
    p = build(conn, m, wk)
    conn.execute(
        "INSERT INTO week_cache(wk,payload,built_at,built_by) VALUES(?,?,?,?) "
        "ON CONFLICT(wk) DO UPDATE SET payload=excluded.payload, built_at=excluded.built_at, "
        "built_by=excluded.built_by",
        (wk, json.dumps(p, ensure_ascii=False), p["built_at"], user))
    conn.commit()
    return p
