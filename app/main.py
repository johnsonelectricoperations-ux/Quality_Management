# -*- coding: utf-8 -*-
"""통합품질관리시스템 웹 앱 (FastAPI). 포트 5003.

실행: uvicorn app.main:app --host 0.0.0.0 --port 5003
"""
import os
import secrets
from collections import defaultdict

from fastapi import FastAPI, Request, Form, UploadFile, File, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, calc, ingest, scan, init_data

BASE = os.path.dirname(__file__)
app = FastAPI(title="통합품질관리시스템")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
tpl = Jinja2Templates(directory=os.path.join(BASE, "templates"))
tpl.env.filters["cf"] = lambda v: f"{v:,.0f}" if isinstance(v, (int, float)) else v
tpl.env.filters["cf2"] = lambda v: f"{v:,.2f}" if isinstance(v, (int, float)) else v

# 화면 표기 통일: DB는 'VMS PART'/'TM PART'로 저장하고, 화면에는 1PART/2PART로 보여준다.
PART_LABEL = {"VMS PART": "1PART", "TM PART": "2PART"}
tpl.env.filters["pl"] = lambda v: PART_LABEL.get(str(v).strip(), v)
tpl.env.globals["PARTS"] = [("VMS PART", "1PART"), ("TM PART", "2PART")]

# 집계공정 '기타' 표시명(압입·밴딩·선별 등 후공정 묶음이라는 의미를 화면에 드러냄)
PROC_LABEL = {"기타": "기타후공정", "?": "(공정미상)"}

ROLE_KO = {"viewer": "조회자", "editor": "입력자", "admin": "관리자"}
ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}
SESSIONS = {}   # token -> username

db.init_db()


# ── 인증 ────────────────────────────────────────────────
def current_user(request: Request):
    tok = request.cookies.get("qms_sid")
    uname = SESSIONS.get(tok)
    if not uname:
        return None
    conn = db.connect()
    row = conn.execute("SELECT username,name,role FROM users WHERE username=?", (uname,)).fetchone()
    conn.close()
    return dict(row) if row else None


def require(role="viewer"):
    def dep(request: Request):
        u = current_user(request)
        if not u:
            return None
        if ROLE_RANK[u["role"]] < ROLE_RANK[role]:
            return {"_forbidden": True, **u}
        return u
    return dep


def render(request, template, user, **ctx):
    ctx.setdefault("crumb", "")
    return tpl.TemplateResponse(request, template, {
        "user": user, "role_ko": ROLE_KO.get(user["role"], ""), **ctx})


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, err: str = ""):
    return tpl.TemplateResponse(request, "login.html", {"err": err})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = db.connect()
    row = conn.execute("SELECT username,pw_hash FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if not row or row["pw_hash"] != db.hash_pw(password):
        return tpl.TemplateResponse(request, "login.html",
                                    {"err": "아이디 또는 비밀번호가 올바르지 않습니다."})
    tok = secrets.token_hex(16)
    SESSIONS[tok] = username
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("qms_sid", tok, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout(request: Request):
    tok = request.cookies.get("qms_sid")
    SESSIONS.pop(tok, None)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("qms_sid")
    return resp


def _guard(u):
    """로그인/권한 체크 → 리다이렉트 or None."""
    if u is None:
        return RedirectResponse("/login", status_code=303)
    if u.get("_forbidden"):
        return RedirectResponse("/", status_code=303)
    return None


def pending_count():
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) c FROM defect_entry WHERE status='pending'").fetchone()["c"]
    conn.close()
    return n


# ── 대시보드 데이터 조립 ────────────────────────────────
def latest_month(conn):
    row = conn.execute("SELECT MAX(d) m FROM production").fetchone()
    d = row["m"] or "2026-07-01"
    return int(d[:4]), int(d[5:7])


# 일 단위로 쌓이는 소스(사내불량/외주소재불량/폐기불량/생산량). 서로 같은 날짜까지
# 채워져 있어야 정상이므로, 이 중 가장 늦은 날짜(ref_date)보다 뒤처진 소스를 '지연'으로 표시한다.
FRESHNESS_DAILY = [
    ("사내불량", "direct"), ("외주소재불량", "outsource"), ("폐기불량", "discard"),
]


def data_freshness(conn):
    """소스별 DB 반영 최신 일자. 일별 소스(사내/외주/폐기/생산)는 서로 비교해 지연 여부를 표시하고,
    월별 수기 입력(SVP·Claim)과 비정기 입력(Customer Incident)은 참고용으로만 보여준다."""
    daily = []
    for label, src in FRESHNESS_DAILY:
        row = conn.execute(
            "SELECT MAX(d) m FROM defect_entry WHERE source=? AND status!='rejected'", (src,)).fetchone()
        daily.append({"label": label, "date": row["m"]})
    row = conn.execute("SELECT MAX(d) m FROM production").fetchone()
    daily.append({"label": "생산량", "date": row["m"]})
    ref = max((r["date"] for r in daily if r["date"]), default=None)
    for r in daily:
        r["stale"] = r["date"] != ref if ref else False

    monthly = []
    for label, table in [("SVP", "svp"), ("Claim", "claim")]:
        row = conn.execute(f"SELECT MAX(ym) m FROM {table}").fetchone()
        monthly.append({"label": label, "date": row["m"]})
    inc = conn.execute("SELECT MAX(d) m FROM incident").fetchone()
    return {"daily": daily, "ref_date": ref, "monthly": monthly, "incident": inc["m"]}


MONTHLY_KPIS = ("proc_ppm", "set_ppm")      # 월별 목표를 둘 수 있는 지표


def target_val(conn, fy, part, kpi, mon=0):
    """목표값. mon(1~12)을 주면 그 달의 월별 목표를 우선 사용하고,
    없으면 연간 목표(mon=0)로 폴백한다."""
    fy = fy % 100 if fy >= 100 else fy      # 2027 → 27 정규화 (목표는 2자리 FY)
    if mon:
        row = conn.execute(
            "SELECT value FROM target WHERE fy=? AND part=? AND kpi=? AND mon=?",
            (fy, part, kpi, mon)).fetchone()
        if row:
            return row["value"]
    row = conn.execute("SELECT value FROM target WHERE fy=? AND part=? AND kpi=? AND mon=0",
                       (fy, part, kpi)).fetchone()
    return row["value"] if row else None


def _join(vals):
    """차트 data-* 용 CSV. 큰 수가 지수표기(1.7e+07)로 나가면 JS가 못 읽으므로 고정소수로."""
    def one(v):
        if v is None:
            return ""
        if isinstance(v, float) and not v.is_integer():
            return f"{v:.6f}".rstrip("0").rstrip(".")
        return str(int(v))
    return ",".join(one(v) for v in vals)


def build_dashboard(conn, m, daily, part):
    cy, cm = latest_month(conn)
    months = calc.trailing_months(cy, cm, 7)
    series = [calc.month_kpi(conn, m, daily, y, mm, part) for (y, mm) in months]
    labels = [f"{mm}월" for (y, mm) in months]
    fys = [calc.fy_of(y, mm) for (y, mm) in months]
    fydiv = next((i for i in range(1, len(fys)) if fys[i] != fys[i - 1]), None)
    fy_lbls = None
    if fydiv is not None:
        fy_lbls = f"{calc.fy_label(fys[0])},{calc.fy_label(fys[-1])}"

    def targets(kpi):
        return [target_val(conn, fy, part, kpi) for fy in fys]

    cur = series[-1]
    prev = series[-2] if len(series) > 1 else cur
    cur_fy = fys[-1]

    def card(kpi, value, unit, tkey, delta, better_down=True):
        tv = target_val(conn, cur_fy, part, tkey)
        return {"value": value, "unit": unit, "target": tv, "delta": delta,
                "good": (delta <= 0) if better_down else (delta >= 0)}

    ppm_part = part if part != "통합" else "VMS PART"     # 불량율 목표는 파트별만 존재
    cards = {
        "scrap_cost": {"amt": cur["scrap_cost"], "pct": cur["scrap_cost_pct"],
                       "target": target_val(conn, cur_fy, part, "scrap_cost"),
                       "delta": round(cur["scrap_cost_pct"] - prev["scrap_cost_pct"], 2)},
        "scrap_qty": {"amt": cur["scrap_qty"], "pct": cur["scrap_qty_pct"],
                      "target": target_val(conn, cur_fy, part, "scrap_qty"),
                      "delta": round(cur["scrap_qty_pct"] - prev["scrap_qty_pct"], 2)},
        "copq": {"amt": cur["copq_cost"], "pct": cur["copq_pct"],
                 "target": target_val(conn, cur_fy, part, "copq"),
                 "delta": round(cur["copq_pct"] - prev["copq_pct"], 2)},
        "incident": {"val": cur["incident"], "target": target_val(conn, cur_fy, part, "incident"),
                     "delta": cur["incident"] - prev["incident"]},
        "warranty": {"val": cur["warranty"], "target": target_val(conn, cur_fy, part, "warranty"),
                     "delta": cur["warranty"] - prev["warranty"]},
        "proc_ppm": {"val": cur["proc_ppm"],
                     "target": target_val(conn, cur_fy, ppm_part, "proc_ppm", cm),
                     "delta": cur["proc_ppm"] - prev["proc_ppm"]},
        "set_ppm": {"val": cur["set_ppm"],
                    "target": target_val(conn, cur_fy, ppm_part, "set_ppm", cm),
                    "delta": cur["set_ppm"] - prev["set_ppm"]},
        "prod_qty": {"val": cur["prod_qty"], "delta": cur["prod_qty"] - prev["prod_qty"]},
        "denom_est": cur["denom_est"],
    }

    charts = {
        "labels": ",".join(labels), "cur": len(months) - 1,
        "fydiv": fydiv if fydiv is not None else "", "fylabels": fy_lbls or "",
        "copq_values": _join([s["copq_pct"] for s in series]),
        "copq_targets": _join(targets("copq")),
    }

    # KPI 카드 클릭 시 아래 차트를 그 지표로 전환하기 위한 월별 시리즈 (지표별)
    def mtargets(kpi, tpart=None, monthly=False):
        return _join([target_val(conn, calc.fy_of(y, mm), tpart or part, kpi, mm if monthly else 0)
                      for (y, mm) in months])

    metrics = [
        {"key": "copq", "label": "COPQ", "unit": "%", "dec": 2, "color": "--p-500",
         "vseries": _join([s["copq_pct"] for s in series]), "targets": targets("copq")},
        {"key": "scrap_cost", "label": "Scrap Cost", "unit": "%", "dec": 2, "color": "--p-500",
         "vseries": _join([s["scrap_cost_pct"] for s in series]), "targets": targets("scrap_cost")},
        {"key": "scrap_qty", "label": "Scrap Quantity", "unit": "%", "dec": 2, "color": "--sec",
         "vseries": _join([s["scrap_qty_pct"] for s in series]), "targets": targets("scrap_qty")},
        {"key": "incident", "label": "Customer Incident", "unit": "건", "dec": 0, "color": "--sec",
         "vseries": _join([s["incident"] for s in series]), "targets": targets("incident")},
        {"key": "warranty", "label": "Warranty", "unit": "천원", "dec": 0, "color": "--sec",
         "vseries": _join([s["warranty"] for s in series]), "targets": targets("warranty")},
        {"key": "proc_ppm", "label": "공정불량율", "unit": "ppm", "dec": 0, "color": "--p-500",
         "vseries": _join([s["proc_ppm"] for s in series]),
         "targets": [target_val(conn, calc.fy_of(y, mm), ppm_part, "proc_ppm", mm) for (y, mm) in months]},
        {"key": "set_ppm", "label": "셋팅불량율", "unit": "ppm", "dec": 0, "color": "--p-500",
         "vseries": _join([s["set_ppm"] for s in series]),
         "targets": [target_val(conn, calc.fy_of(y, mm), ppm_part, "set_ppm", mm) for (y, mm) in months]},
        {"key": "prod_qty", "label": "생산수량", "unit": "EA", "dec": 0, "color": "--sec",
         "vseries": _join([s["prod_qty"] for s in series]), "targets": [None] * len(months)},
    ]
    for mt in metrics:
        mt["targets"] = _join(mt["targets"]) if isinstance(mt["targets"], list) else mt["targets"]
    charts["metrics"] = metrics

    # 당월 주별 COPQ (추정 SVP, claim 생산액 비례배분)
    weekly = build_weekly(conn, m, daily, cy, cm, part)
    charts["wk_labels"] = ",".join(f"{w['week']}주" for w in weekly)
    charts["wk_values"] = _join([w["copq_pct"] for w in weekly])
    charts["wk_target"] = target_val(conn, cur_fy, part, "copq")
    charts["wk_cur"] = len(weekly) - 1

    # TOP5
    from datetime import date, timedelta
    mstart = f"{cy:04d}-{cm:02d}-01"
    mend = f"{cy:04d}-{cm:02d}-31"
    top_parts = "VMS PART" if part == "통합" else part
    top_month = calc.top5_defect(conn, m, mstart, mend, top_parts)
    cur_week = weekly[-1]["week"] if weekly else 1
    wk_lo = (cur_week - 1) * 7 + 1
    ws = f"{cy:04d}-{cm:02d}-{wk_lo:02d}"
    we = f"{cy:04d}-{cm:02d}-{min(wk_lo+6,31):02d}"
    top_week = calc.top5_defect(conn, m, ws, we, top_parts)
    return {"cy": cy, "cm": cm, "cur_fy": calc.fy_label(cur_fy), "cards": cards,
            "charts": charts, "top_month": top_month, "top_week": top_week,
            "top_part": top_parts, "cur_week": cur_week}


def build_weekly(conn, m, daily, cy, cm, part):
    parts = calc._parts_for(part)
    ym = f"{cy:04d}-{cm:02d}"
    claim_total = calc.claim_sum(conn, ym, parts, calc.CLAIM_COPQ_ITEMS)
    wk = defaultdict(lambda: {"copq_cost": 0.0, "prod_amt": 0.0})
    from datetime import date, timedelta
    d = date(cy, cm, 1)
    month_prod = 0.0
    while d.month == cm and d.year == cy:
        ds = d.isoformat()
        w = calc.iso_week_of_month(ds)
        for p in parts:
            c = daily.get(ds, {}).get(p)
            if c:
                wk[w]["copq_cost"] += c["scrap_cost_copq"]
                wk[w]["prod_amt"] += c["prod_amount"]
                month_prod += c["prod_amount"]
        d += timedelta(days=1)
    out = []
    for w in sorted(wk):
        pa = wk[w]["prod_amt"]
        claim_w = claim_total * (pa / month_prod) if month_prod else 0
        cost = wk[w]["copq_cost"] + claim_w
        pct = round(cost / pa * 100, 2) if pa else 0
        out.append({"week": w, "copq_pct": pct})
    return out


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, part: str = "통합"):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    if part not in ("통합", "VMS PART", "TM PART"):
        part = "통합"
    conn = db.connect()
    m = calc.Masters(conn)
    daily = calc.compute_daily(conn, m)
    data = build_dashboard(conn, m, daily, part)
    fresh = data_freshness(conn)
    conn.close()
    return render(request, "dashboard.html", u, active="dash", heading="대시보드",
                  crumb="개요", pending=pending_count(), part=part, d=data, fresh=fresh)


# ── (구) KPI 현황 → 세부지표현황으로 통합 (옛 링크 호환) ──
@app.get("/report/kpi")
def report_kpi_redirect(request: Request, part: str = "통합"):
    return RedirectResponse(f"/report/detail?view=kpi&part={part}", status_code=303)


# ── 리포트: 세부지표현황 (KPI현황 + 공정별현황 통합) ────
VIEWS = [
    ("kpi", "KPI 종합 추이", "Scrap/COPQ/불량율 6개 지표를 한눈에"),
    ("part", "파트별 불량율 추이", "1PART vs 2PART 비교"),
    ("process", "공정별 불량율 추이", "성형~기타후공정"),
    ("defect_type", "불량유형별 추이", "어떤 불량이 늘고 있나 (상위 N)"),
    ("defect_share", "불량유형별 비중", "상위 유형 파레토"),
    ("tm", "TM-NO별 불량율 추이", "품번별 문제 추적 (상위 N)"),
    ("product_name", "품명별 비중", "품명 기준 집중도"),
]
SOURCE_OPTS = [("direct", "사내"), ("outsource", "외주"), ("discard", "폐기")]


def _default_range(conn):
    cy, cm = latest_month(conn)
    months = calc.trailing_months(cy, cm, 7)
    y0, m0 = months[0]
    return f"{y0:04d}-{m0:02d}", f"{cy:04d}-{cm:02d}"


@app.get("/report/detail", response_class=HTMLResponse)
def report_detail(request: Request, view: str = "kpi", part: str = "통합", unit: str = "월",
                  f: str = "", t: str = "", kind: str = "", proc: str = "",
                  src: str = "", measure: str = "ppm", topn: int = 10):
    """세부지표현황: 검색조건(파트·기간·구분·공정·소스) + 분석유형 선택 + 집계단위(월/분기/년)."""
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    if view not in {v for v, _n, _d in VIEWS}:
        view = "kpi"
    if part not in ("통합", "VMS PART", "TM PART"):
        part = "통합"
    if unit not in calc.PERIOD_UNITS:
        unit = "월"
    if measure not in ("ppm", "qty"):
        measure = "ppm"
    conn = db.connect()
    df, dt = _default_range(conn)
    ym_from = f or df
    ym_to = t or dt
    date_from = ym_from + "-01"
    date_to = ym_to + "-31"
    procs = [p for p in proc.split(",") if p] or None
    sources = [s for s in src.split(",") if s] or None
    kind_f = kind if kind in ("공정", "셋팅") else None

    m = calc.Masters(conn)
    ctx = {"view": view, "part": part, "unit": unit, "ym_from": ym_from, "ym_to": ym_to,
           "kind": kind, "proc": proc, "src": src, "measure": measure, "topn": topn,
           "views": VIEWS, "units": calc.PERIOD_UNITS, "agg_procs": db.AGG_PROCESSES,
           "source_opts": SOURCE_OPTS, "proc_label": PROC_LABEL,
           "chart": None, "table": None, "kpi_charts": None}

    if view == "kpi":
        # 기존 KPI 현황: 선택 기간의 월별 6개 지표 (집계단위는 월 고정이 자연스러움)
        daily = calc.compute_daily(conn, m)
        y0, m0 = int(ym_from[:4]), int(ym_from[5:7])
        y1, m1 = int(ym_to[:4]), int(ym_to[5:7])
        months = []
        yy, mm = y0, m0
        while (yy, mm) <= (y1, m1) and len(months) < 36:
            months.append((yy, mm))
            mm += 1
            if mm == 13:
                mm, yy = 1, yy + 1
        series = [calc.month_kpi(conn, m, daily, y, mo, part) for (y, mo) in months]
        labels = [f"{mo}월" for (y, mo) in months]
        fys = [calc.fy_of(y, mo) for (y, mo) in months]
        fydiv = next((i for i in range(1, len(fys)) if fys[i] != fys[i - 1]), None)
        fylabels = f"{calc.fy_label(fys[0])},{calc.fy_label(fys[-1])}" if fydiv is not None else ""
        ppm_part = part if part != "통합" else "VMS PART"

        def tgt(kpi, tpart=None):
            mon = kpi in MONTHLY_KPIS
            return _join([target_val(conn, calc.fy_of(y, mo), tpart or part, kpi, mo if mon else 0)
                          for (y, mo) in months])

        specs = [("Scrap Cost", "--p-500", "%", 2, "scrap_cost_pct", "scrap_cost", part),
                 ("Scrap Quantity", "--sec", "%", 2, "scrap_qty_pct", "scrap_qty", part),
                 ("COPQ", "--p-500", "%", 2, "copq_pct", "copq", part),
                 ("Warranty", "--sec", "", 0, "warranty", "warranty", part),
                 ("공정불량율 (ppm)", "--p-500", "", 0, "proc_ppm", "proc_ppm", ppm_part),
                 ("셋팅불량율 (ppm)", "--sec", "", 0, "set_ppm", "set_ppm", ppm_part)]
        # 카드별 월별 분자·분모(소스 데이터): vk(값 키)로 매핑
        KPI_SRC = {
            "scrap_cost_pct": ("scrap_cost", "Scrap Cost(천원)", "denom", "분모(SVP·천원)"),
            "scrap_qty_pct": ("scrap_qty", "Scrap 수량(EA)", "prod_qty", "생산수량(EA)"),
            "copq_pct": ("copq_cost", "COPQ 비용(천원)", "denom", "분모(SVP·천원)"),
            "warranty": ("warranty", "Warranty(천원)", None, None),
            "proc_ppm": ("proc_qty", "공정불량 수량(EA)", "prod_qty", "생산수량(EA)"),
            "set_ppm": ("set_qty", "셋팅불량 수량(EA)", "prod_qty", "생산수량(EA)"),
        }

        def src_table(vk):
            num_key, num_label, den_key, den_label = KPI_SRC[vk]
            rows = [[num_label] + [f"{s[num_key]:,}" for s in series]]
            if den_key:
                rows.append([den_label] + [f"{s[den_key]:,}" for s in series])
            return {"head": ["구분"] + labels, "rows": rows}

        ctx["kpi_charts"] = [{
            "name": nm, "color": col, "unit": un, "dec": dc,
            "cur_val": series[-1][vk] if series else 0,
            "vseries": _join([s[vk] for s in series]), "targets": tgt(tk, tp),
            "src_table": src_table(vk),
        } for nm, col, un, dc, vk, tk, tp in specs]
        ctx["labels"] = ",".join(labels)
        ctx["cur"] = len(months) - 1
        ctx["fydiv"] = fydiv if fydiv is not None else ""
        ctx["fylabels"] = fylabels
    elif view in ("defect_share", "product_name"):
        axis, label, color = ("defect_type", "불량유형", "--p-500") if view == "defect_share" \
                             else ("product_name", "품명", "--sec")
        res = calc.analyze(conn, m, axis, date_from, date_to, part, unit,
                           kind_f, procs, sources, measure="qty", topn=topn)
        rows = sorted(res["series"], key=lambda s: s["total"], reverse=True)
        grand = sum(r["total"] for r in rows) or 1
        ctx["chart"] = {"type": "hbar", "names": "|".join(r["name"] for r in rows),
                        "vseries": _join([r["total"] for r in rows]),
                        "unit": "EA", "dec": 0, "color": color, "note": res["note"]}
        # 월별 소스 데이터: 분자(항목별 월 수량) + 분모(그 달 전체 수량, 열 합계)
        by_period_total = [sum(r["qty"][i] for r in rows) for i in range(len(res["periods"]))]
        ctx["table"] = {"head": [label] + res["periods"] + ["합계(EA)", "비중"],
                        "rows": [[r["name"]] + [f"{v:,}" for v in r["qty"]]
                                 + [f"{r['total']:,}", f"{r['total']/grand*100:.1f}%"] for r in rows],
                        "denom_row": ["합계(분모)"] + [f"{v:,}" for v in by_period_total]
                                     + [f"{grand:,}", "100.0%"]}
    else:
        res = calc.analyze(conn, m, view, date_from, date_to, part, unit,
                           kind_f, procs, sources, measure=measure, topn=topn)
        names = [PROC_LABEL.get(s["name"], s["name"]) for s in res["series"]]
        ctx["chart"] = {"type": "multi", "labels": ",".join(res["periods"]),
                        "names": "|".join(names),
                        "sets": "|".join(_join(s["values"]) for s in res["series"]),
                        "unit": res["unit"], "dec": 0, "color": "--p-500", "note": res["note"],
                        "legend": list(zip(names, range(len(names))))}
        # 상세 표는 항상 원본 수량(분자)로 표시하고, 생산수량(분모)은 별도 표로 보여준다
        ctx["table"] = {"head": ["구분(분자·EA)"] + res["periods"] + ["합계(EA)"],
                        "rows": [[names[i]] + [f"{v:,}" for v in s["qty"]]
                                 + [f"{s['total']:,}"] for i, s in enumerate(res["series"])]}
        if res["per_tm"]:
            ctx["denom_table"] = {"head": ["구분(분모·생산수량 EA)"] + res["periods"] + ["합계(EA)"],
                                  "rows": [[names[i]] + [f"{v:,}" for v in res["denom_series"][s["name"]]]
                                           + [f"{sum(res['denom_series'][s['name']]):,}"]
                                           for i, s in enumerate(res["series"])]}
        else:
            dv = res["denom_series"]
            ctx["denom_table"] = {"head": ["구분(분모)"] + res["periods"] + ["합계(EA)"],
                                  "rows": [["생산수량(EA)"] + [f"{v:,}" for v in dv] + [f"{sum(dv):,}"]]}
        ctx["measure_unit"] = "ppm" if measure == "ppm" else "EA"
    conn.close()
    return render(request, "report_detail.html", u, active="rdetail", heading="세부지표현황",
                  crumb="집계/리포트", pending=pending_count(), **ctx)


# ── (구) 공정별 불량현황 → 세부지표현황으로 통합 (옛 링크 호환) ──
@app.get("/report/defect")
def report_defect_redirect(request: Request, part: str = "통합", kind: str = "공정"):
    return RedirectResponse(f"/report/detail?view=process&part={part}&kind={kind}",
                            status_code=303)


# ── 마스터 ──────────────────────────────────────────────
@app.get("/masters", response_class=HTMLResponse)
def masters(request: Request):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    conn = db.connect()
    proc = [dict(r) for r in conn.execute("SELECT * FROM process ORDER BY part,ord,name")]
    prod_total = conn.execute("SELECT COUNT(*) c FROM product").fetchone()["c"]
    prod = [dict(r) for r in conn.execute(
        "SELECT p.tm_no,p.name,p.part,COUNT(r.id) steps FROM product p "
        "LEFT JOIN product_route r ON r.tm_no=p.tm_no GROUP BY p.tm_no ORDER BY p.tm_no LIMIT 20")]
    dtypes = [dict(r) for r in conn.execute("SELECT * FROM defect_type ORDER BY part,kind,name")]
    conn.close()
    return render(request, "masters.html", u, active="masters", heading="마스터 조회",
                  crumb="관리", pending=pending_count(), proc=proc, prod=prod, prod_total=prod_total,
                  dtypes=dtypes, can_edit=(u["role"] == "admin"))


@app.post("/masters/upload")
async def masters_upload(request: Request, kind: str = Form(...), file: UploadFile = File(...)):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/masters", status_code=303)
    path = os.path.join("/tmp", "qms_up_" + secrets.token_hex(4) + ".xlsx")
    with open(path, "wb") as f:
        f.write(await file.read())
    conn = db.connect()
    msg = err = ""
    try:
        if kind == "process":
            n = ingest.ingest_process_master(conn, path)
            msg = f"공정명 마스터 {n}건 반영"
        elif kind == "product":
            n = ingest.ingest_product_master(conn, path)
            msg = f"제품 마스터 {n}건 반영"
        elif kind == "defect":
            n, errs = ingest.ingest_defect_master(conn, path)
            if errs:
                err = "불량유형 마스터 오류: " + " / ".join(errs[:6])
            else:
                msg = f"불량유형 마스터 {n}건 반영"
    except Exception as e:
        err = f"업로드 실패: {e}"
    finally:
        conn.close()
        os.remove(path)
    q = ("?msg=" + msg) if msg else ("?err=" + err)
    return RedirectResponse("/masters" + q, status_code=303)


# ── 제품 마스터 관리 (CSV 가져오기 + 등록/수정/삭제) ────
PAGE_SIZE = 50

# 제품마스터 화면(라우팅·단가) 표시용 4개 공정. 집계는 계속 5종(성형/소결/정형/가공/기타)을
# 쓰지만(리포트·대시보드·불량배분규칙), 가공·기타는 단가가 항상 같아(price_calc.py 규칙)
# 제품마스터 화면에서는 '기타' 하나로 묶어 보여준다. 저장 시 내부적으로 가공·기타 둘 다 갱신한다.
DISPLAY_PROCESSES = ["성형", "소결", "정형", "기타"]
_OTHER_UNDERLYING = {"성형": ["성형"], "소결": ["소결"], "정형": ["정형"], "기타": ["가공", "기타"]}


def _route_display(route_str):
    """'성형→소결→정형→가공→밴딩→후처리' 같은 원본(물리공정) 라우팅 문자열을 화면표시 4종
    (성형·소결·정형·기타)으로 접고, 연속 중복은 하나로 합친다."""
    if not route_str:
        return route_str
    out = []
    for proc in route_str.split("→"):
        b = db.bucket_of(proc)
        b = "기타" if b in ("가공", "기타") else b
        if not out or out[-1] != b:
            out.append(b)
    return "→".join(out)


def _price_map_display(conn, tm):
    """{성형·소결·정형·기타: 단가|None}. '기타'는 기타 단가(없으면 가공 단가)를 보여준다."""
    full = _price_map(conn, tm)
    other = full.get("기타")
    if other is None:
        other = full.get("가공")
    return {"성형": full.get("성형"), "소결": full.get("소결"), "정형": full.get("정형"), "기타": other}


def _proc_options(conn):
    """라우팅 체크박스용: 공정명(고유) 순서·COPQ제외."""
    return [dict(r) for r in conn.execute(
        "SELECT name, MIN(ord) o, MAX(copq_exclude) excl FROM process GROUP BY name ORDER BY o, name")]


def _copq_excluded(conn):
    """COPQ 제외 공정명 집합 (성형 등)."""
    return {r["name"] for r in conn.execute("SELECT DISTINCT name FROM process WHERE copq_exclude=1")}


def _price_map(conn, tm):
    """{집계공정: 오늘 기준 현재 단가|None}. 효력시작일이 오늘 이전인 것 중 가장 최근 값을 쓴다."""
    cur = {}
    for r in conn.execute(
            "SELECT process,unit_price FROM product_price WHERE tm_no=? AND effective_from<=date('now') "
            "ORDER BY effective_from", (tm,)):
        cur[r["process"]] = r["unit_price"]      # 나중에 읽는(더 최근) 값이 이전 값을 덮어씀
    return {p: cur.get(p) for p in db.AGG_PROCESSES}


@app.get("/admin/products", response_class=HTMLResponse)
def products_list(request: Request, q: str = "", part: str = "", miss: str = "",
                  page: int = 1, msg: str = "", err: str = ""):
    """제품 마스터 + 공정별 단가 통합 목록."""
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    conn = db.connect()
    where, args = [], []
    if q:
        where.append("(p.tm_no LIKE ? OR p.name LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
    if part in ("VMS PART", "TM PART"):
        where.append("p.part=?"); args.append(part)
    if miss:
        where.append("NOT EXISTS(SELECT 1 FROM product_price pp WHERE pp.tm_no=p.tm_no)")
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) c FROM product p {wsql}", args).fetchone()["c"]
    page = max(1, page)
    off = (page - 1) * PAGE_SIZE
    rows = []
    for r in conn.execute(
            f"SELECT p.tm_no,p.name,p.part, "
            f"(SELECT GROUP_CONCAT(process,'→') FROM (SELECT process FROM product_route "
            f" WHERE tm_no=p.tm_no ORDER BY seq)) route "
            f"FROM product p {wsql} ORDER BY p.tm_no LIMIT ? OFFSET ?", args + [PAGE_SIZE, off]):
        d = dict(r)
        d["price"] = _price_map_display(conn, r["tm_no"])
        d["route"] = _route_display(r["route"])
        rows.append(d)
    conn.close()
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    return render(request, "products.html", u, active="products", heading="제품 마스터",
                  crumb="관리", pending=pending_count(), rows=rows, total=total,
                  agg=DISPLAY_PROCESSES, q=q, part=part, miss=miss, page=page, pages=pages,
                  can_edit=(u["role"] == "admin"), msg=msg, err=err)


@app.post("/admin/products/import")
async def products_import(request: Request, part: str = Form(...), file: UploadFile = File(...)):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/admin/products", status_code=303)
    path = os.path.join("/tmp", "qms_csv_" + secrets.token_hex(4))
    with open(path, "wb") as f:
        f.write(await file.read())
    conn = db.connect()
    msg = err = ""
    try:
        n, new = ingest.ingest_product_csv(conn, path, part)
        msg = f"{part} 제품 {n}건 반영 (신규 {new})"
    except Exception as e:
        err = f"가져오기 실패: {e}"
    finally:
        conn.close()
        os.remove(path)
    return RedirectResponse(f"/admin/products?{'msg='+msg if msg else 'err='+err}", status_code=303)


@app.get("/admin/products/new", response_class=HTMLResponse)
def product_new(request: Request):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    if u["role"] != "admin":
        return RedirectResponse("/admin/products", status_code=303)
    conn = db.connect()
    excl = _copq_excluded(conn)
    conn.close()
    return render(request, "product_form.html", u, active="products", heading="제품 등록",
                  crumb="관리 / 제품 마스터", pending=pending_count(), mode="new",
                  p={"tm_no": "", "name": "", "part": "VMS PART"},
                  agg=DISPLAY_PROCESSES, excl=excl,
                  route=set(), price={pr: None for pr in DISPLAY_PROCESSES})


@app.get("/admin/products/{tm}/edit", response_class=HTMLResponse)
def product_edit(request: Request, tm: str):
    """수정 폼: 제품 정보 + 공정 라우팅 + 공정별 단가를 현재 값으로 채워 보여준다."""
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    if u["role"] != "admin":
        return RedirectResponse("/admin/products", status_code=303)
    conn = db.connect()
    p = conn.execute("SELECT * FROM product WHERE tm_no=?", (tm,)).fetchone()
    if not p:
        conn.close()
        return RedirectResponse("/admin/products?err=제품 없음", status_code=303)
    # 라우팅은 화면표시 4종 기준 — 압입·밴딩·후처리·가공은 전부 '기타'로 모임
    route_raw = {db.bucket_of(r["process"]) for r in conn.execute(
        "SELECT process FROM product_route WHERE tm_no=? ORDER BY seq", (tm,))}
    route = {("기타" if b == "가공" else b) for b in route_raw}
    price = _price_map_display(conn, tm)
    excl = _copq_excluded(conn)
    conn.close()
    return render(request, "product_form.html", u, active="products", heading="제품 수정",
                  crumb="관리 / 제품 마스터", pending=pending_count(), mode="edit",
                  p=dict(p), agg=DISPLAY_PROCESSES, excl=excl, route=route, price=price)


@app.post("/admin/products/save")
async def product_save(request: Request):
    """제품 정보 + 라우팅 + 공정별 단가를 한 번에 저장."""
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/admin/products", status_code=303)
    form = await request.form()
    tm = calc.base_tmno(form.get("tm_no") or "")   # 변형 접미 알파벳 제거(base로 합침)
    name = (form.get("name") or "").strip()
    part = form.get("part") or "VMS PART"
    orig = form.get("orig_tm") or ""
    if not tm:
        return RedirectResponse("/admin/products?err=TM-NO 필수", status_code=303)
    conn = db.connect()
    if orig and orig != tm:                        # TM-NO 변경 시 옛 레코드 정리
        for t in ("product", "product_route", "product_price"):
            conn.execute(f"DELETE FROM {t} WHERE tm_no=?", (orig,))
    conn.execute("INSERT INTO product(tm_no,name,part) VALUES(?,?,?) "
                 "ON CONFLICT(tm_no) DO UPDATE SET name=excluded.name, part=excluded.part",
                 (tm, name, part))
    # 라우팅(화면 4종 → 내부 집계공정 5종으로 전개). 기존 op_code는 공정별로 보존한다.
    # '기타' 체크 시 원래 '가공' 버킷이 있던 제품만 가공+기타 둘 다 유지하고(불량배분규칙이
    # 그대로 동작하도록), 원래 없던 제품에 이 화면 저장만으로 '가공'을 새로 만들지는 않는다
    # (그러면 "찍힘" 같이 가공을 포함하는 배분규칙이 조용히 적용 대상에 끼어드는 부작용이 생김).
    codes = {db.bucket_of(r["process"]): (r["op_code"] or "") for r in conn.execute(
        "SELECT process,op_code FROM product_route WHERE tm_no=? ORDER BY seq", (tm,))}
    had_gagong = "가공" in codes
    conn.execute("DELETE FROM product_route WHERE tm_no=?", (tm,))
    seq = 0
    for pr in DISPLAY_PROCESSES:
        if form.get("use_" + pr):
            real_list = (["가공", "기타"] if had_gagong else ["기타"]) if pr == "기타" else [pr]
            for real_pr in real_list:
                seq += 1
                conn.execute("INSERT INTO product_route(tm_no,seq,process,unit_price,op_code) "
                             "VALUES(?,?,?,0,?)", (tm, seq, real_pr, codes.get(real_pr, "")))
    # 공정별 단가 — 화면 '기타' 입력값을 가공·기타 둘 다에 동일하게 저장(빈칸이면 둘 다 삭제)
    for proc in DISPLAY_PROCESSES:
        raw = (form.get("price_" + proc) or "").replace(",", "").strip()
        real_procs = _OTHER_UNDERLYING[proc]
        if raw == "":
            for real_pr in real_procs:
                conn.execute("DELETE FROM product_price WHERE tm_no=? AND process=?", (tm, real_pr))
            continue
        try:
            v = float(raw)
        except ValueError:
            conn.close()
            return RedirectResponse(f"/admin/products?err={proc} 단가가 숫자가 아님", status_code=303)
        for real_pr in real_procs:
            conn.execute(
                "INSERT INTO product_price(tm_no,process,unit_price,effective_from) VALUES(?,?,?,'2000-01-01') "
                "ON CONFLICT(tm_no,process,effective_from) DO UPDATE SET unit_price=excluded.unit_price",
                (tm, real_pr, v))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/products?msg=저장됨: {tm}&q={tm}", status_code=303)


@app.post("/admin/products/{tm}/delete")
def product_delete(request: Request, tm: str):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/admin/products", status_code=303)
    conn = db.connect()
    for t in ("product", "product_route", "product_price"):
        conn.execute(f"DELETE FROM {t} WHERE tm_no=?", (tm,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/products?msg=삭제됨", status_code=303)


# ── 공정별 단가 ─────────────────────────────────────────
@app.get("/admin/prices")
def prices_redirect(request: Request):
    """공정별 단가는 제품 마스터 화면에 통합됨(옛 링크 호환)."""
    return RedirectResponse("/admin/products", status_code=303)


# ── 폴더 반영(스캔) ─────────────────────────────────────
def _scan_page(request, u, results=None, msg="", err=""):
    conn = db.connect()
    root = scan.data_root(conn)
    sources = []
    for key, label, sub in scan.SOURCES:
        folder = os.path.join(root, sub)
        exists = os.path.isdir(folder)
        cnt = 0
        if exists:
            for _r, _d, names in os.walk(folder):
                cnt += sum(1 for n in names if not n.startswith("~$"))
        sources.append({"key": key, "label": label, "sub": sub, "exists": exists, "count": cnt})
    logs = [dict(r) for r in conn.execute(
        "SELECT ts,kind,ok,note FROM upload_log WHERE kind LIKE 'scan:%' ORDER BY id DESC LIMIT 20")]
    last_scan = db.get_setting(conn, "last_scan_at", "")
    init_done = db.get_setting(conn, "init_data_done", "") == "1"
    conn.close()
    return render(request, "scan.html", u, active="scan", heading="폴더 반영",
                  crumb="관리", pending=pending_count(), root=root,
                  root_ok=os.path.isdir(root), sources=sources, results=results,
                  logs=logs, last_scan=last_scan, init_done=init_done,
                  can_edit=(u["role"] == "admin"), msg=msg, err=err)


@app.get("/admin/scan", response_class=HTMLResponse)
def scan_page(request: Request, msg: str = "", err: str = ""):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    return _scan_page(request, u, msg=msg, err=err)


@app.post("/admin/scan/root")
async def scan_set_root(request: Request, root: str = Form("")):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/admin/scan", status_code=303)
    conn = db.connect()
    db.set_setting(conn, scan.SETTING_ROOT, root.strip())
    conn.close()
    return RedirectResponse("/admin/scan?msg=경로 저장됨", status_code=303)


@app.post("/admin/scan/init", response_class=HTMLResponse)
async def scan_init_data(request: Request):
    """templates/ 실데이터로 DB 초기 구축 (최초 1회). 적재는 멱등이라 재실행해도 중복 없음."""
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    if u["role"] != "admin":
        return RedirectResponse("/admin/scan", status_code=303)
    conn = db.connect()
    try:
        steps = init_data.run(conn, echo=lambda *_a: None)
        db.set_setting(conn, "init_data_done", "1")
    except Exception as e:
        conn.close()
        return _scan_page(request, u, err=f"초기 구축 실패: {e}")
    results = [{"key": "init", "label": lb, "ok": not str(out).startswith("실패"),
                "files": 1, "rows": 0, "note": out, "errors": []} for lb, out in steps]
    scan._log(conn, "init:templates", "templates/", True,
              " / ".join(f"{lb}:{out}" for lb, out in steps))
    conn.close()
    return _scan_page(request, u, results=results, msg="초기 구축 완료")


@app.post("/admin/scan/purge-demo", response_class=HTMLResponse)
async def scan_purge_demo(request: Request):
    """데모(샘플) 품목과 그 실적을 삭제. 실데이터로 전환할 때 1회 사용."""
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    if u["role"] != "admin":
        return RedirectResponse("/admin/scan", status_code=303)
    conn = db.connect()
    try:
        out = init_data.purge_demo(conn)
    except Exception as e:
        conn.close()
        return _scan_page(request, u, err=f"데모 삭제 실패: {e}")
    note = " · ".join(f"{k} {v}행" for k, v in out.items() if v)
    scan._log(conn, "purge:demo", "데모 데이터", True, note or "삭제 대상 없음")
    conn.close()
    results = [{"key": "demo", "label": "데모 데이터 삭제", "ok": True,
                "files": 0, "rows": sum(out.values()),
                "note": note or "삭제할 데모 데이터가 없습니다", "errors": []}]
    return _scan_page(request, u, results=results)


@app.post("/admin/scan/run", response_class=HTMLResponse)
async def scan_run(request: Request, key: str = Form("all")):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    if u["role"] != "admin":
        return RedirectResponse("/admin/scan", status_code=303)
    conn = db.connect()
    try:
        results = scan.scan_all(conn) if key == "all" else [scan.scan_one(conn, key)]
    except Exception as e:
        conn.close()
        return _scan_page(request, u, err=f"반영 실패: {e}")
    conn.close()
    return _scan_page(request, u, results=results)


# ── 공정 관리 ───────────────────────────────────────────
@app.get("/admin/processes", response_class=HTMLResponse)
def processes_list(request: Request, msg: str = "", err: str = ""):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    conn = db.connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM process ORDER BY part,ord,name")]
    conn.close()
    return render(request, "processes.html", u, active="processes", heading="공정 관리",
                  crumb="관리", pending=pending_count(), rows=rows,
                  can_edit=(u["role"] == "admin"), msg=msg, err=err)


@app.post("/admin/processes/save")
async def processes_save(request: Request):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/admin/processes", status_code=303)
    form = await request.form()
    part = (form.get("part") or "").strip()
    name = (form.get("name") or "").strip()
    if not part or not name:
        return RedirectResponse("/admin/processes?err=파트/공정명 필수", status_code=303)
    try:
        ordn = int(form.get("ord") or 0)
    except ValueError:
        ordn = 0
    excl = 1 if form.get("copq_exclude") else 0
    conn = db.connect()
    conn.execute("INSERT INTO process(part,name,copq_exclude,ord) VALUES(?,?,?,?) "
                 "ON CONFLICT(part,name) DO UPDATE SET copq_exclude=excluded.copq_exclude, "
                 "ord=excluded.ord", (part, name, excl, ordn))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/processes?msg=저장됨", status_code=303)


@app.post("/admin/processes/{pid}/delete")
def processes_delete(request: Request, pid: int):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/admin/processes", status_code=303)
    conn = db.connect()
    conn.execute("DELETE FROM process WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/processes?msg=삭제됨", status_code=303)


# ── 불량유형 마스터 관리 (배분기준: 공정 체크박스 + 비율 표준입력) ──
DEFECT_KINDS = ["공정", "셋팅"]


def _decompose_alloc_rule(process, alloc_rule):
    """저장된 alloc_rule(+발생공정)을 편집폼 미리채움용으로 분해.
    반환: (선택된 공정 set, {공정: 비율문자열}) — 비율 dict가 비어있으면 균등/미지정."""
    rule = (alloc_rule or "").strip()
    if not rule or rule.replace(" ", "") == calc.INPUT_PROCESS_RULE.replace(" ", ""):
        return ({process} if process else set()), {}
    selected, ratios = set(), {}
    has_ratio = ":" in rule
    for tok in rule.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            name, r = tok.split(":", 1)
            name = name.strip()
            selected.add(name)
            ratios[name] = r.strip()
        else:
            selected.add(tok)
    return selected, (ratios if has_ratio else {})


def _compose_alloc_rule(form):
    """체크박스(use_공정)+비율(ratio_공정) 입력 → alloc_rule 문자열.
    아무 공정도 안 고르면 '' (입력공정 100%). 비율을 하나라도 채우면 전부 채워야 한다."""
    selected = [p for p in db.AGG_PROCESSES if form.get("use_" + p)]
    if not selected:
        return "", None
    ratios = {p: (form.get("ratio_" + p) or "").strip() for p in selected}
    filled = {p: v for p, v in ratios.items() if v != ""}
    if not filled:
        return ",".join(selected), None
    if len(filled) != len(selected):
        return None, "비율은 선택한 공정 전부에 입력하거나, 전부 비워 균등배분으로 두세요"
    try:
        for v in filled.values():
            float(v)
    except ValueError:
        return None, "비율은 숫자로 입력하세요"
    return ",".join(f"{p}:{filled[p]}" for p in selected), None


@app.get("/admin/defect-types", response_class=HTMLResponse)
def defect_types_list(request: Request, edit: int = 0, msg: str = "", err: str = ""):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    conn = db.connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM defect_type ORDER BY part,kind,name")]
    edit_row = None
    if edit:
        r = conn.execute("SELECT * FROM defect_type WHERE id=?", (edit,)).fetchone()
        if r:
            selected, ratios = _decompose_alloc_rule(r["process"], r["alloc_rule"])
            edit_row = dict(r)
            edit_row["selected"] = selected
            edit_row["ratios"] = ratios
    conn.close()
    return render(request, "defect_types.html", u, active="defect_types", heading="불량유형 마스터 관리",
                  crumb="관리", pending=pending_count(), rows=rows, edit_row=edit_row,
                  agg=db.AGG_PROCESSES, kinds=DEFECT_KINDS,
                  can_edit=(u["role"] == "admin"), msg=msg, err=err)


@app.post("/admin/defect-types/save")
async def defect_types_save(request: Request):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/admin/defect-types", status_code=303)
    form = await request.form()
    orig_id = (form.get("orig_id") or "").strip()
    part = form.get("part") or "VMS PART"
    kind = form.get("kind") or "공정"
    name = (form.get("name") or "").strip()
    if not name:
        return RedirectResponse("/admin/defect-types?err=불량명 필수", status_code=303)
    alloc_rule, err = _compose_alloc_rule(form)
    if err:
        eq = f"&edit={orig_id}" if orig_id else ""
        return RedirectResponse(f"/admin/defect-types?err={err}{eq}", status_code=303)
    conn = db.connect()
    if orig_id:
        conn.execute("DELETE FROM defect_type WHERE id=?", (orig_id,))
    conn.execute(
        "INSERT INTO defect_type(part,kind,process,name,alloc_rule) VALUES(?,?,?,?,?) "
        "ON CONFLICT(part,kind,name) DO UPDATE SET process=excluded.process, alloc_rule=excluded.alloc_rule",
        (part, kind, "", name, alloc_rule))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/defect-types?msg=저장됨: {name}", status_code=303)


@app.post("/admin/defect-types/{did}/delete")
def defect_types_delete(request: Request, did: int):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/admin/defect-types", status_code=303)
    conn = db.connect()
    conn.execute("DELETE FROM defect_type WHERE id=?", (did,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/defect-types?msg=삭제됨", status_code=303)


# ── 목표 관리 ───────────────────────────────────────────
@app.get("/admin/target", response_class=HTMLResponse)
def admin_target(request: Request, fy: int = 27, part: str = "통합"):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    conn = db.connect()
    rows = {}
    for r in conn.execute("SELECT * FROM target WHERE fy=? AND part=?", (fy, part)):
        rows[(r["kpi"], r["mon"])] = r["value"]
    conn.close()
    order = [("scrap_cost", "Scrap Cost", "%"), ("scrap_qty", "Scrap Quantity", "%"),
             ("copq", "COPQ", "%"), ("incident", "Customer Incident", "건"),
             ("warranty", "Warranty", "천원"), ("proc_ppm", "공정불량율", "ppm"),
             ("set_ppm", "셋팅불량율", "ppm")]
    items = [{"kpi": k, "name": n, "unit": un,
              "value": rows.get((k, 0), ""), "monthly": k in MONTHLY_KPIS}
             for k, n, un in order]
    # 월별 목표(FY 순서: 4월~익년 3월)
    fy_months = [m for m in range(4, 13)] + [m for m in range(1, 4)]
    monthly = [{"kpi": k, "name": n, "unit": un,
                "vals": [{"mon": mm, "value": rows.get((k, mm), "")} for mm in fy_months]}
               for k, n, un in order if k in MONTHLY_KPIS]
    return render(request, "target.html", u, active="target", heading="목표 관리",
                  crumb="관리", pending=pending_count(), fy=fy, part=part, items=items,
                  monthly=monthly, fy_months=fy_months,
                  can_edit=(u["role"] == "admin"))


@app.post("/admin/target")
async def admin_target_save(request: Request):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/admin/target", status_code=303)
    form = await request.form()
    fy = int(form.get("fy")); part = form.get("part")
    units = {"scrap_cost": "%", "scrap_qty": "%", "copq": "%", "incident": "건",
             "warranty": "천원", "proc_ppm": "ppm", "set_ppm": "ppm"}
    conn = db.connect()

    def put(kpi, unit, mon, raw):
        """빈칸이면 해당 목표 삭제, 값이 있으면 저장."""
        v = (raw or "").replace(",", "").strip()
        if v == "":
            conn.execute("DELETE FROM target WHERE fy=? AND part=? AND kpi=? AND mon=?",
                         (fy, part, kpi, mon))
            return
        conn.execute("INSERT INTO target(fy,part,kpi,value,unit,mon) VALUES(?,?,?,?,?,?) "
                     "ON CONFLICT(fy,part,kpi,mon) DO UPDATE SET value=excluded.value",
                     (fy, part, kpi, float(v), unit, mon))

    for kpi, unit in units.items():
        if kpi in MONTHLY_KPIS and part == "통합":
            continue
        raw = form.get("t_" + kpi, "")
        if raw != "" or form.get("t_" + kpi) is not None:
            put(kpi, unit, 0, raw)                     # 연간 목표
        if kpi in MONTHLY_KPIS:                        # 월별 목표 (4~3월)
            for mm in list(range(4, 13)) + list(range(1, 4)):
                key = f"m_{kpi}_{mm}"
                if key in form:
                    put(kpi, unit, mm, form.get(key, ""))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/target?fy={fy}&part={part}&msg=저장됨", status_code=303)


# ── 사용자 관리 ─────────────────────────────────────────
@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    conn = db.connect()
    users = [dict(r) for r in conn.execute("SELECT username,name,role FROM users ORDER BY role")]
    conn.close()
    return render(request, "users.html", u, active="users", heading="사용자 관리",
                  crumb="관리", pending=pending_count(), users=users,
                  can_edit=(u["role"] == "admin"))


# ── 데이터 입력/업로드 (공통 트랜잭션 업로드) ───────────
# 사내불량·외주·폐기·생산은 '폴더 반영'(/admin/scan)으로 수집하므로 업로드 화면을 두지 않는다.
# 수기 입력만 존재하는 소스(SVP·Claim·Incident)만 남긴다.
INPUT_PAGES = {
    "claim": ("Claim 입력", "Claim Excel — 년월·파트·항목·금액(천원)"),
    "incident": ("Customer Incident 관리", "Incident Excel — 일자·파트·고객·내용"),
}


# ── SVP 입력 (FY 가로 표) ───────────────────────────────
SVP_PARTS = ["VMS PART", "TM PART"]
SVP_ROWS = SVP_PARTS + [calc.SVP_TOTAL_PART]     # 합계(통합)도 직접 입력


def _fy_cols(fy):
    """FY의 12개월 → [(ym, 라벨)] (4월~익년 3월)."""
    out = []
    for m in range(4, 13):
        out.append((f"{fy-1:04d}-{m:02d}", f"{m}월"))
    for m in range(1, 4):
        out.append((f"{fy:04d}-{m:02d}", f"{m}월"))
    return out


def _fy_options(conn):
    """선택 가능한 FY 목록 (입력된 SVP + 생산 데이터 + 당해 FY)."""
    fys = set()
    for r in conn.execute("SELECT DISTINCT ym FROM svp"):
        fys.add(calc.ym_to_fy(r["ym"]))
    cy, cm = latest_month(conn)
    fys.add(calc.fy_of(cy, cm))
    fys.add(calc.fy_of(cy, cm) - 1)
    return sorted(f % 100 for f in fys)


@app.get("/input/svp", response_class=HTMLResponse)
def svp_page(request: Request, fy: int = 0, msg: str = "", err: str = ""):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    conn = db.connect()
    fy_list = _fy_options(conn)
    if not fy:
        fy = fy_list[-1] if fy_list else 27
    cols = _fy_cols(2000 + fy)
    cur = {(r["part"], r["ym"]): r["amount"] for r in conn.execute("SELECT part,ym,amount FROM svp")}
    conn.close()
    rows = []
    for part in SVP_ROWS:
        vals = {ym: cur.get((part, ym)) for ym, _l in cols}
        tot = sum(v for v in vals.values() if v)
        rows.append({"part": part, "vals": vals, "total": round(tot),
                     "is_total": part == calc.SVP_TOTAL_PART})
    return render(request, "svp.html", u, active="svp", heading="SVP 입력",
                  crumb="데이터 입력", pending=pending_count(), fy=fy, fy_list=fy_list,
                  cols=cols, rows=rows, msg=msg, err=err,
                  can_edit=(u["role"] in ("editor", "admin")))


@app.post("/input/svp/save")
async def svp_save(request: Request):
    """FY 표 저장. 빈칸은 삭제(그 달은 생산금액 추정으로 계산)."""
    u = current_user(request)
    if u is None or u["role"] not in ("editor", "admin"):
        return RedirectResponse("/input/svp", status_code=303)
    form = await request.form()
    fy = int(form.get("fy") or 27)
    conn = db.connect()
    for part in SVP_ROWS:
        for ym, _lbl in _fy_cols(2000 + fy):
            key = f"v_{part}_{ym}"
            if key not in form:
                continue
            raw = str(form.get(key) or "").replace(",", "").strip()
            if raw == "":
                conn.execute("DELETE FROM svp WHERE ym=? AND part=?", (ym, part))
                continue
            try:
                v = float(raw)
            except ValueError:
                conn.close()
                return RedirectResponse(f"/input/svp?fy={fy}&err={ym} 값이 숫자가 아님",
                                        status_code=303)
            conn.execute("INSERT INTO svp(ym,part,amount) VALUES(?,?,?) "
                         "ON CONFLICT(ym,part) DO UPDATE SET amount=excluded.amount",
                         (ym, part, v))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/input/svp?fy={fy}&msg=저장됨", status_code=303)


@app.get("/input/{page}", response_class=HTMLResponse)
def input_page(request: Request, page: str, msg: str = "", err: str = ""):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    if page == "outsource-review":
        return outsource_review(request, u)
    if page not in INPUT_PAGES:
        return RedirectResponse("/", status_code=303)
    title, desc = INPUT_PAGES[page]
    conn = db.connect()
    recent = _recent_rows(conn, page)
    conn.close()
    return render(request, "input.html", u, active=page, heading=title, crumb="데이터 입력",
                  pending=pending_count(), page=page, desc=desc, recent=recent, msg=msg, err=err,
                  can_edit=(u["role"] in ("editor", "admin")))


def _recent_rows(conn, page):
    if page in ("defect", "outsource", "discard"):
        src = {"defect": "direct", "outsource": "outsource", "discard": "discard"}[page]
        return [dict(r) for r in conn.execute(
            "SELECT d,tm_no,defect_name,qty,status FROM defect_entry WHERE source=? "
            "ORDER BY id DESC LIMIT 12", (src,))]
    if page == "production":
        return [dict(r) for r in conn.execute(
            "SELECT d,tm_no,qty,amount FROM production ORDER BY d DESC,tm_no LIMIT 12")]
    if page == "claim":
        return [dict(r) for r in conn.execute(
            "SELECT ym,part,item,amount FROM claim ORDER BY ym DESC LIMIT 12")]
    if page == "incident":
        return [dict(r) for r in conn.execute(
            "SELECT d,part,customer,content FROM incident ORDER BY d DESC LIMIT 12")]
    return []


@app.post("/input/{page}/upload")
async def input_upload(request: Request, page: str, file: UploadFile = File(...)):
    u = current_user(request)
    if u is None or u["role"] not in ("editor", "admin"):
        return RedirectResponse(f"/input/{page}", status_code=303)
    path = os.path.join("/tmp", "qms_tx_" + secrets.token_hex(4) + ".xlsx")
    with open(path, "wb") as f:
        f.write(await file.read())
    conn = db.connect()
    msg = err = ""
    try:
        if page == "defect":
            n, _ = ingest.ingest_defect_entries(conn, path, "direct", u["name"])
            msg = f"불량입력 {n}건"
        elif page == "outsource":
            n, pend = ingest.ingest_defect_entries(conn, path, "outsource", u["name"], quarantine_100=True)
            msg = f"외주소재불량 {n}건 (검토 격리 {pend}건)"
        elif page == "discard":
            n, _ = ingest.ingest_defect_entries(conn, path, "discard", u["name"])
            msg = f"폐기불량 {n}건"
        elif page == "production":
            msg = f"생산실적 {ingest.ingest_production(conn, path)}건"
        elif page == "claim":
            msg = f"Claim {ingest.ingest_claim(conn, path)}건"
        elif page == "incident":
            msg = f"Incident {ingest.ingest_incident(conn, path)}건"
    except Exception as e:
        err = f"업로드 실패: {e}"
    finally:
        conn.close()
        os.remove(path)
    q = ("?msg=" + msg) if msg else ("?err=" + err)
    return RedirectResponse(f"/input/{page}{q}", status_code=303)


SOURCE_LABEL = {"outsource": "외주소재불량", "discard": "폐기불량"}


def outsource_review(request, u):
    """외주소재불량·폐기불량 100EA 이상 검토 대기 목록(공통 화면)."""
    conn = db.connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT id,d,tm_no,defect_name,qty,source,process FROM defect_entry WHERE status='pending' ORDER BY id")]
    for r in rows:
        r["source_label"] = SOURCE_LABEL.get(r["source"], r["source"])
        r["no_tm"] = not (r["tm_no"] or "").strip()      # TM-NO 없는 건 = 공정 단위로만 집계
    conn.close()
    return render(request, "review.html", u, active="oreview", heading="불량 검토 (100EA 이상)",
                  crumb="데이터 입력", pending=len(rows), rows=rows,
                  can_edit=(u["role"] in ("editor", "admin")))


@app.post("/input/outsource-review/{eid}")
async def review_action(request: Request, eid: int, action: str = Form(...), qty: int = Form(None)):
    """검토 대기 건 처리: approve(그대로/수정 반영)·reject(삭제).
    사람이 결정한 건은 reviewed=1로 표시해 다음 폴더 반영(재스캔) 때도 덮어써지지 않는다."""
    u = current_user(request)
    if u is None or u["role"] not in ("editor", "admin"):
        return RedirectResponse("/input/outsource-review", status_code=303)
    conn = db.connect()
    if action == "approve":
        if qty is not None:
            conn.execute("UPDATE defect_entry SET qty=?, status='confirmed', reviewed=1 WHERE id=?", (qty, eid))
        else:
            conn.execute("UPDATE defect_entry SET status='confirmed', reviewed=1 WHERE id=?", (eid,))
    elif action == "reject":
        # 하드 삭제 대신 상태만 'rejected'로 바꿔 집계에서 제외(status='confirmed' 조건에 안 걸림)하고,
        # reviewed=1로 표시해 다음 폴더 재스캔에도 이 건이 다시 나타나지 않게 한다.
        conn.execute("UPDATE defect_entry SET status='rejected', reviewed=1 WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/input/outsource-review", status_code=303)
