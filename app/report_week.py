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
import calendar
import datetime as _dt
import json
from collections import defaultdict

from . import calc, db, report_kpi

# 캐시 구조·계산이 바뀌면 이 숫자를 올린다 → 저장된 캐시가 자동으로 버려지고 다시 계산된다.
CACHE_VERSION = 17

WEEK_END_WEEKDAY = 3      # 목요일 (월=0 … 일=6)
WEEK_DAYS = 7

CONTENTS = [
    "(1) 주요 KPI 현황 (Scrap · COPQ · Incident)",
    "(2) 생산1P 공정불량율 현황",
    "(3) 생산2P 공정불량율 현황",
    "(4) 주요 품질 이슈",
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


# 추세선(선형회귀) 상승폭이 구간 평균의 이 비율 이상이면 "증가추세"로 본다 — 미세한 흔들림과
# 진짜 증가를 구분하기 위한 최소 기준(2026-08-11 사용자 확정).
TREND_RISE_RATIO = 0.20


def _trend_stats(vals):
    """PPM 시계열(None=생산량 없어 계산불가, 계산에서 제외)의 추세선(선형회귀).
    반환: (증가추세 여부, 각 지점의 추세선 적합값 리스트 — 그래프에 선으로 그리는 용도).
    유효 점이 2개 미만이면 적합값도 전부 None(추세선을 그릴 수 없음)."""
    pts = [(i, v) for i, v in enumerate(vals) if v is not None]
    fit = [None] * len(vals)
    if len(pts) < 2:
        return False, fit
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    if den == 0:
        return False, fit
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / den
    intercept = my - slope * mx
    fit = [slope * i + intercept for i in range(len(vals))]
    if slope <= 0 or my == 0:
        return False, fit
    rise = slope * (pts[-1][0] - pts[0][0])
    up = (rise / my) >= TREND_RISE_RATIO
    return up, fit


# 추이 그래프에 보여줄 개수(2026-08-11: 5→7로 확대). 생산이 아예 없던 달/주는 점을 만들 수
# 없으므로(불량율 자체가 정의 안 됨) 건너뛰고 그 이전 기간에서 채운다 — 그래서 "최근 7개"가
# 아니라 "생산이 있었던 최근 7개"가 된다.
TREND_POINTS = 7
TREND_LOOKBACK_MONTHS = 36     # 이 안에서도 7개를 못 채우면(신제품 등) 있는 만큼만 보여준다.
TREND_LOOKBACK_WEEKS = 104


def defect_trend(conn, m, tm, defect_group, asof):
    """특정 TM-NO·주요유형(대표명)의 최근 7개월/최근 7주(생산이 있었던 기간만) **불량율(PPM)
    추이** + 증가추세 판정 — TOP5 '주요유형' 클릭 팝업 및 글자색 표시용(2026-08-11 신설).
    수량이 아니라 불량율로 보는 이유는 생산량이 많고 적음에 따른 착시를 없애기 위함(사용자 확정).
    생산이 아예 없던 달/주는 불량율이 정의되지 않으므로 그래프에서 제외한다(사용자 확정).
    asof: 기준일(YYYY-MM-DD). 월마감이면 마감월 말일, 주마감이면 그 주 목요일(주차키)을 넘긴다.
    "YYYY-MM"(월마감 ym)을 바로 넘겨도 그 달 말일로 변환해 처리한다."""
    if isinstance(asof, str) and len(asof) == 7:
        ay, am = int(asof[:4]), int(asof[5:7])
        asof = "%04d-%02d-%02d" % (ay, am, calendar.monthrange(ay, am)[1])
    asof_d = _dt.date.fromisoformat(asof) if isinstance(asof, str) else asof

    month_pts = []
    y, mo = asof_d.year, asof_d.month
    for _ in range(TREND_LOOKBACK_MONTHS):
        if len(month_pts) >= TREND_POINTS:
            break
        d0 = "%04d-%02d-01" % (y, mo)
        d1 = "%04d-%02d-%02d" % (y, mo, calendar.monthrange(y, mo)[1])
        r = calc.defect_rate(conn, m, tm, defect_group, d0, d1)
        if r["prod"]:                                    # 생산이 있었던 달만 점으로 남긴다.
            month_pts.append({"label": "%d월" % mo, "ppm": r["ppm"], "qty": r["qty"]})
        mo -= 1
        if mo == 0:
            mo, y = 12, y - 1
    month_pts.reverse()                                   # 오래된→최신

    week_pts = []
    we = week_end_of(asof_d)
    for _ in range(TREND_LOOKBACK_WEEKS):
        if len(week_pts) >= TREND_POINTS:
            break
        d0, d1 = week_bounds(we)
        r = calc.defect_rate(conn, m, tm, defect_group, d0, d1)
        if r["prod"]:
            sd = _dt.date.fromisoformat(d0)
            week_pts.append({"label": "%d/%d" % (sd.month, sd.day), "ppm": r["ppm"], "qty": r["qty"]})
        we -= _dt.timedelta(days=WEEK_DAYS)
    week_pts.reverse()

    # 증가추세 판정은 불량율(PPM) 기준 그대로 유지한다(생산량 착시 배제, 기존 확정 사항).
    month_up, month_fit = _trend_stats([p["ppm"] for p in month_pts])
    week_up, week_fit = _trend_stats([p["ppm"] for p in week_pts])
    for p, f in zip(month_pts, month_fit):
        p["fit"] = round(f, 1) if f is not None else None
    for p, f in zip(week_pts, week_fit):
        p["fit"] = round(f, 1) if f is not None else None
    # 팝업 아래쪽 그래프용 — 같은 7개월 데이터의 수량(EA) 추세선(2026-08-13: 주간 그래프를
    # 월간 불량수량 그래프로 교체하면서 추가. 증가추세 판정에는 쓰지 않고 그래프 표시 전용).
    _, month_qty_fit = _trend_stats([p["qty"] for p in month_pts])
    for p, f in zip(month_pts, month_qty_fit):
        p["fit_qty"] = round(f, 1) if f is not None else None
    cls = "up-both" if (month_up and week_up) else ("up-month" if month_up else ("up-week" if week_up else ""))
    name = m.product.get(tm, ("?",))[0]
    return {"tm": tm, "name": name, "defect": defect_group,
            "months": month_pts, "weeks": week_pts,
            "month_up": month_up, "week_up": week_up, "cls": cls}


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


def _range_kpi(conn, m, daily, d0, d1, part):
    """임의 날짜 구간(d0~d1)의 KPI dict. 월 KPI(`calc.month_kpi`)와 같은 키를 쓰되 분모는 추정이다.

    클레임·Warranty는 **실제 발생일자로** 그 구간에 든 건만 더한다(월 금액을 안분하지 않는다 —
    언제 터졌는지가 중요하므로). `week_kpi`(그 주 금~목)와 "해당월 누적"(그 달 1일~누적종료일)이
    같은 계산을 공유한다(2026-08-21, 페이지2 KPI 타일에도 월누적을 추가하며 공통화)."""
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
        "start": d0, "end": d1,
        "scrap_cost": scrap_cost_r, "scrap_cost_pct": pct(scrap_cost_r, denom_r),
        "scrap_qty": scrap_qty, "scrap_qty_pct": pct(scrap_qty, agg["prod_qty"]),
        "copq_cost": copq_cost_r, "copq_pct": pct(copq_cost_r, denom_r),
        "warranty": round(warranty), "incident": incident,
        "proc_ppm": ppm(agg["proc_qty"], agg["prod_qty"]),
        "set_ppm": ppm(agg["set_qty"], agg["prod_qty"]),
        "proc_qty": agg["proc_qty"], "set_qty": agg["set_qty"], "prod_qty": agg["prod_qty"],
        "denom": denom_r, "denom_est": est,
    }


def week_kpi(conn, m, daily, wk, part):
    """한 주(금~목)의 KPI dict — `_range_kpi`를 그 주 범위로 호출."""
    d0, d1 = week_bounds(wk)
    out = _range_kpi(conn, m, daily, d0, d1, part)
    out["wk"] = wk
    return out


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

def _month_range_for_week(wk):
    """그 주(금~목) 중 **더 많은 날이 속한 달**을 '해당월'로 판단한다.

    처음엔 마감일(목요일) 기준으로 하려 했으나, 목요일이 월초(예: 9/1)면 주의 나머지 6일이
    전부 지난달인데도 "이번 달 누적"이 하루치만 표시되는 문제가 있었다(2026-08-21 사용자 지적).
    그래서 요일수로 다수결을 내고, 누적 종료일은 그 주 중 그 달에 속하는 마지막 날(=주가
    다음 달로 안 넘어갔으면 목요일 그대로, 넘어갔으면 그 달 말일)로 잡는다 — 월말 근처 주는
    자연스럽게 그 달 전체(=월마감에 가까운 값)를 보여주게 된다.
    반환: (그 달 1일, 누적종료일, 연, 월) — 날짜는 ISO 문자열."""
    d0, d1 = week_bounds(wk)
    cnt = defaultdict(int)
    for ds in _dates_between(d0, d1):
        d = _dt.date.fromisoformat(ds)
        cnt[(d.year, d.month)] += 1
    (y, mo), _n = max(cnt.items(), key=lambda kv: kv[1])
    month_start = _dt.date(y, mo, 1)
    month_last = _dt.date(y, mo, calendar.monthrange(y, mo)[1])
    cum_end = min(_dt.date.fromisoformat(d1), month_last)
    return month_start.isoformat(), cum_end.isoformat(), y, mo


def _month_cum_kpi(conn, m, daily, wk, part):
    """해당월(위 판단 기준) 1일 ~ 누적종료일까지 KPI(주간과 같은 키 전부). 페이지2(주요 KPI
    현황)·페이지3·4(공정불량율 현황) 타일의 미니그래프 맨 왼쪽 막대와 대표수치에 공통으로 쓴다
    (2026-08-21, `_range_kpi` 재사용 — Scrap Cost·COPQ 추정치 안분 로직도 그대로 적용됨)."""
    ms, me, _y, mo = _month_range_for_week(wk)
    out = _range_kpi(conn, m, daily, ms, me, part)
    out["label"] = f"{mo}월누적"
    return out


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
    mcum = _month_cum_kpi(conn, m, daily, wk, part)
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
        # 모든 지표(공정/셋팅불량 PPM·Scrap Qty/Cost·COPQ·Incident)에 해당월 누적을 같이 준다
        # (2026-08-21, 페이지2 요약 KPI에도 확대 적용).
        mv = (mcum.get(key) or 0) if mcum["prod_qty"] else None
        if mv is not None:
            vmax = max(vmax, mv)
        month = {"value": mv, "label": mcum["label"]}
        out[key] = {"labels": labels, "values": vals, "vmax": vmax or 1, "target": tgt, "dec": dec,
                    "month": month}
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
    # 공정별 막대그래프의 축 높이 — 금주·전주 값 중 가장 큰 것에 맞춘다(2026-08-10, 그래프 추가).
    vmax = max([r["cur"] for r in rows] + [r["prev"] for r in rows] + [0])
    return {"rows": rows, "qty": tot_c, "qty_prev": tot_p,
            "cur": ppm(tot_c, cur_prod), "prev": ppm(tot_p, prv_prod),
            "delta": ppm(tot_c, cur_prod) - ppm(tot_p, prv_prod),
            "prod": cur_prod, "prod_prev": prv_prod, "vmax": vmax}


def _top_items(conn, m, wk, part, limit=5):
    """그 주 불량수량 상위 TM-NO + 주요 불량유형(통합 반영)."""
    d0, d1 = week_bounds(wk)
    rows = calc.top5_defect(conn, m, d0, d1, part, limit=limit)
    for r in rows:
        # 주요유형별 월별/주별 불량율 증가추세 판정 → 글자색(2026-08-11 신설). 클릭 팝업과 동일 계산.
        r["by_info"] = [{"name": n, "qty": q,
                         "cls": defect_trend(conn, m, r["tm"], n, wk)["cls"]}
                        for n, q in r["by"][:2]]
    return rows


def _paginate_items(items, cost_fn, budget):
    """items를 순서대로 쌓다가, 다음 항목을 더하면 budget을 넘는 지점에서 페이지를 끊는다.

    **항목 하나는 절대 쪼개지 않는다** — 페이지 경계에서 한 이슈의 내용이 반토막 나지 않게
    하기 위함이다(2026-08-21 사용자 확정). 그래서 어떤 항목 하나가 budget보다 커도 통째로
    한 페이지를 차지하며, 그 페이지는 `tight=True`로 표시해 화면단에서 폰트를 줄이도록 한다.
    cost_fn은 그 항목이 차지할 '줄(line) 수'를 어림잡아 반환하는 함수 — 실제 렌더링 폭·글자
    수를 정확히 재는 게 아니라 대략적인 추정치라, budget과 함께 실사용 보며 조정할 수 있다.
    반환: [{"rows": [...], "tight": bool}, ...] (rows가 비어도 빈 페이지 1개는 항상 반환
    — "등록된 건이 없습니다" 안내를 보여줄 자리가 있어야 하므로)."""
    pages, cur, cur_cost = [], [], 0
    for it in items:
        c = cost_fn(it)
        if cur and cur_cost + c > budget:
            pages.append(cur)
            cur, cur_cost = [], 0
        cur.append(it)
        cur_cost += c
    if cur or not pages:
        pages.append(cur)
    # 키 이름은 "items"가 아니라 "rows"다 — Jinja는 obj.items를 dict.items() 메서드로 먼저
    # 해석해버려서(속성 접근이 dict.items() 내장 메서드와 충돌) "items"를 키로 쓰면 템플릿에서
    # 못 꺼내 쓴다(2026-08-21 실제로 겪음).
    return [{"rows": p, "tight": sum(cost_fn(i) for i in p) > budget} for p in pages]


# 주마감 "주요 품질 이슈" 카드(폭이 좁다) 한 줄에 대략 들어가는 글자수 — 11.5px 폰트,
# .lst 컬럼 폭 기준 눈대중(2026-08-21). 정확한 렌더 폭 측정이 아니라 대략치라 실사용 보며
# WEEK_ISSUE_CHARS_PER_LINE·WEEK_ISSUE_PAGE_LINES 둘 다 조정 가능.
WEEK_ISSUE_CHARS_PER_LINE = 44
WEEK_ISSUE_PAGE_LINES = 26          # 카드 안에 들어가는 대략적인 줄 수


def _week_issue_cost(it):
    def wrap(s, cpl=WEEK_ISSUE_CHARS_PER_LINE):
        s = str(s or "").strip()
        if not s:
            return 0
        # 사람이 직접 줄바꿈을 넣은 줄(문제사항·발생원인·개선대책도 2026-08-22부터 여러 줄
        # 입력 가능)은 그 줄바꿈 수만큼은 최소한 확보한다 — 글자수만 보면 짧은 줄 여러 개를
        # 한 줄로 오판해 예산을 실제보다 적게 잡는다.
        lines = s.splitlines()
        return max(len(lines), -(-len(s) // cpl))
    c = 1.4                          # 항목 사이 여백·구분선 몫
    c += wrap(it.get("content")) or 1
    c += 1                           # 공정/품번/품명 요약줄
    if it.get("action"):
        c += wrap(it["action"])
    if it.get("photos"):
        c += 6                       # 사진 썸네일(96px) ≈ 텍스트 6줄 분량
    if it.get("docs"):
        c += 1
    return c


def _issue_block(conn, wk):
    """그 주에 등록된 내부품질 Issue / 고객 Incident 목록 — 첨부(사진/문서)도 함께 싣는다
    (2026-08-13, 월마감 report_monthly._issue_block/_internal_issue_block과 동일 패턴).

    항목이 많아 카드 한 페이지에 다 안 들어가면 자동으로 페이지를 나눈다(2026-08-21) — 한
    이슈가 페이지 경계에서 잘리지 않도록 이슈 단위로만 나누고, 내부/고객 이슈 페이지 수가
    다를 수 있어 더 긴 쪽 기준으로 짝을 맞춰(`pages`) 슬라이드마다 나란히 보여준다."""
    d0, d1 = week_bounds(wk)
    internal = [dict(r) for r in conn.execute(
        "SELECT id,d,part,process,tm_no,product_name,content,defect_qty,cause,action "
        "FROM internal_issue WHERE is_official=1 AND d BETWEEN ? AND ? ORDER BY d,id", (d0, d1))]
    for row in internal:
        row["photos"] = [f["id"] for f in conn.execute(
            "SELECT id FROM internal_issue_file WHERE internal_issue_id=? AND kind='photo' ORDER BY id",
            (row["id"],))]
        row["docs"] = [{"id": f["id"], "name": f["orig_name"]} for f in conn.execute(
            "SELECT id, orig_name FROM internal_issue_file WHERE internal_issue_id=? AND kind='doc' ORDER BY id",
            (row["id"],))]
    customer = [dict(r) for r in conn.execute(
        "SELECT id,d,part,customer,tm_no,product_name,content,defect_qty,cause,action "
        "FROM incident WHERE is_official=1 AND d BETWEEN ? AND ? ORDER BY d,id", (d0, d1))]
    for row in customer:
        row["photos"] = [f["id"] for f in conn.execute(
            "SELECT id FROM incident_file WHERE incident_id=? AND kind='photo' ORDER BY id",
            (row["id"],))]
        row["docs"] = [{"id": f["id"], "name": f["orig_name"]} for f in conn.execute(
            "SELECT id, orig_name FROM incident_file WHERE incident_id=? AND kind='doc' ORDER BY id",
            (row["id"],))]
    internal_pages = _paginate_items(internal, _week_issue_cost, WEEK_ISSUE_PAGE_LINES)
    customer_pages = _paginate_items(customer, _week_issue_cost, WEEK_ISSUE_PAGE_LINES)
    empty_page = {"rows": [], "tight": False}
    n = max(len(internal_pages), len(customer_pages))
    pages = [{"internal": internal_pages[i] if i < len(internal_pages) else empty_page,
              "customer": customer_pages[i] if i < len(customer_pages) else empty_page}
             for i in range(n)]
    return {"internal": internal, "customer": customer, "pages": pages}


def _texts(conn, wk):
    saved = {r["section"]: r["content"] for r in conn.execute(
        "SELECT section, content FROM report_text WHERE ym=?", (wk,))}
    # 리치 에디터(2026-08-22) 이전 순수 텍스트도 줄바꿈이 유지되게 HTML로 변환 — 화면에서는
    # |safe로 그대로 찍는다.
    return {k: calc.text_to_html(saved.get(k, "")) for k, _n, _h in WEEK_SECTIONS}


def build(conn, m, wk):
    """주마감 보고서 payload 조립."""
    daily = calc.compute_daily(conn, m)
    d0, d1 = week_bounds(wk)
    parts = [("VMS PART", "1PART"), ("TM PART", "2PART")]
    # 공정불량율·셋팅불량율·TOP5는 1PART/2PART로만 본다(파트마다 원인공정이 달라 통합은 의미가
    # 옅다). 다만 (1) 요약 슬라이드의 Scrap·COPQ·Incident는 **통합만** 본다(2026-08-10 확정) —
    # 파트별 상세는 (2)(3) 슬라이드에서 이미 보므로 요약 장은 전사 숫자 하나로 충분하다.
    kpi_parts = parts + [("통합", "통합")]
    return {
        "v": CACHE_VERSION, "wk": wk, "start": d0, "end": d1, "label": week_label(wk),
        "contents": CONTENTS,
        "kpi": {lbl: _kpi_block(conn, m, daily, wk, p) for p, lbl in kpi_parts},
        "trend": {lbl: _trend(conn, m, daily, wk, p) for p, lbl in kpi_parts},
        "proc": {lbl: _process_block(conn, m, daily, wk, p) for p, lbl in parts},
        "top": {lbl: _top_items(conn, m, wk, p) for p, lbl in parts},
        # 품번이 없는 불량(설비 단위 폐기 등)은 TOP5에 못 들어가므로 건별로 따로 싣는다.
        # 최대 2건만(2026-08-10 확정, 월마감과 동일 기준).
        "unassigned": {lbl: calc.unassigned_defects(conn, m, d0, d1, p, limit=2)
                       for p, lbl in parts},
        "issues": _issue_block(conn, wk),
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
