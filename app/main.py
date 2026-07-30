# -*- coding: utf-8 -*-
"""통합품질관리시스템 웹 앱 (FastAPI). 포트 5003.

실행: uvicorn app.main:app --host 0.0.0.0 --port 5003
"""
import os
import secrets
import sqlite3
from collections import defaultdict

from fastapi import FastAPI, Request, Form, UploadFile, File, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, calc, ingest, scan, init_data, price_calc, report_kpi

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
    if has_perm(user["role"], "data_check", "view"):
        ctx.setdefault("unreg_alert", unreg_alert_count())
    else:
        ctx.setdefault("unreg_alert", 0)
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


FORBIDDEN_REDIRECT = "/?perm_denied=1"   # 대시보드로 보내되 팝업으로 사유를 알림(base.html의 JS가 처리)


def _guard(u):
    """로그인/권한 체크 → 리다이렉트 or None."""
    if u is None:
        return RedirectResponse("/login", status_code=303)
    if u.get("_forbidden"):
        return RedirectResponse(FORBIDDEN_REDIRECT, status_code=303)
    return None


# ── 메뉴별 권한 매트릭스(사용자 관리 화면) ──────────────────────
# 관리자는 항상 전기능(하드코딩, 매트릭스에 없음). editor/viewer는 메뉴별 보기/편집을
# permission 테이블에서 조회(db._ensure_default_permissions가 기본값 시딩).
PERM_MENUS = [
    ("dash", "대시보드"),
    ("rdetail", "세부지표현황"),
    ("svp", "SVP 입력"),
    ("claim", "Claim 입력"),
    ("incident", "Customer Incident 관리"),
    ("oreview", "불량 검토(100EA↑)"),
    ("data_check", "데이터 점검"),
    ("products", "제품 마스터"),
    ("processes", "공정 관리"),
    ("defect_types", "불량유형 마스터"),
    ("customers", "고객사 마스터"),
    ("scan", "폴더 반영"),
    ("masters", "마스터 조회"),
    ("target", "목표 관리"),
]


def load_permissions(conn):
    """{(role, menu_key): (can_view, can_edit)}. admin은 포함 안 함(항상 True)."""
    d = {}
    for r in conn.execute("SELECT role,menu_key,can_view,can_edit FROM permission"):
        d[(r["role"], r["menu_key"])] = (bool(r["can_view"]), bool(r["can_edit"]))
    return d


def has_perm(role, menu_key, level="view"):
    """관리자는 항상 True. dash(대시보드)는 로그인 후 반드시 갈 곳이 있어야 하므로 항상 보기 허용."""
    if role == "admin" or menu_key == "dash":
        return True
    conn = db.connect()
    row = conn.execute("SELECT can_view,can_edit FROM permission WHERE role=? AND menu_key=?",
                       (role, menu_key)).fetchone()
    conn.close()
    if not row:
        return level == "view"
    return bool(row["can_view"]) if level == "view" else bool(row["can_edit"])


def _perm_guard(request, menu_key, level="view"):
    """로그인 + 메뉴 권한 체크. (user, 막힘응답or None) 반환 — 막히면 user가 None일 수 있음."""
    u = current_user(request)
    g = _guard(u)
    if g:
        return u, g
    if not has_perm(u["role"], menu_key, level):
        return u, RedirectResponse(FORBIDDEN_REDIRECT, status_code=303)
    return u, None


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
    prod_date = db.get_setting(conn, ingest.PROD_FRESHNESS_KEY, "") or None
    if not prod_date:
        row = conn.execute("SELECT MAX(d) m FROM production").fetchone()
        prod_date = row["m"]                    # 이 설정이 생기기 전 과거 데이터용 폴백
    daily.append({"label": "생산량", "date": prod_date})
    ref = max((r["date"] for r in daily if r["date"]), default=None)
    for r in daily:
        r["stale"] = r["date"] != ref if ref else False

    monthly = []
    row = conn.execute("SELECT MAX(ym) m FROM svp").fetchone()
    monthly.append({"label": "SVP", "date": row["m"]})
    row = conn.execute("SELECT MAX(d) m FROM claim").fetchone()
    monthly.append({"label": "Claim", "date": row["m"]})
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

    # Warranty·Incident는 목표 자체가 FY 누적합계라, 카드 값도 FY 시작(4월)부터 당월까지
    # 누적으로 계산해야 목표와 비교가 된다(전월대비는 그대로 당월 단독 증감).
    fy_all_months = calc.fy_months(cur_fy)
    ytd_months = fy_all_months[:fy_all_months.index((cy, cm)) + 1] if (cy, cm) in fy_all_months else fy_all_months
    ytd_parts = calc._parts_for(part)
    warranty_ytd = round(sum(calc.claim_sum(conn, "%04d-%02d" % (y, mm), ytd_parts, ["Warranty"])
                             for y, mm in ytd_months))
    incident_ytd = sum(calc.incident_count(conn, "%04d-%02d" % (y, mm), ytd_parts) for y, mm in ytd_months)

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
        "incident": {"val": incident_ytd, "target": target_val(conn, cur_fy, part, "incident"),
                     "delta": cur["incident"] - prev["incident"]},
        "warranty": {"val": warranty_ytd, "target": target_val(conn, cur_fy, part, "warranty"),
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

    # 당월 주별 지표 (추정 SVP, claim/Warranty 생산액 비례배분) — 카드 클릭 전환 시 이 차트도 함께 바뀌도록
    # 월별 metrics와 동일한 key 세트로 준비해둔다.
    weekly = build_weekly(conn, m, daily, cy, cm, part)
    wk_field = {"copq": "copq_pct", "scrap_cost": "scrap_cost_pct", "scrap_qty": "scrap_qty_pct",
               "incident": "incident", "warranty": "warranty", "proc_ppm": "proc_ppm",
               "set_ppm": "set_ppm", "prod_qty": "prod_qty"}
    wk_target = {"copq": target_val(conn, cur_fy, part, "copq"),
                "scrap_cost": target_val(conn, cur_fy, part, "scrap_cost"),
                "scrap_qty": target_val(conn, cur_fy, part, "scrap_qty"),
                "incident": None, "warranty": None,
                "proc_ppm": target_val(conn, cur_fy, ppm_part, "proc_ppm", cm),
                "set_ppm": target_val(conn, cur_fy, ppm_part, "set_ppm", cm),
                "prod_qty": None}

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
        mt["wk_values"] = _join([w[wk_field[mt["key"]]] for w in weekly])
        mt["wk_target"] = wk_target[mt["key"]]
    charts["metrics"] = metrics

    charts["wk_labels"] = ",".join(f"{w['week']}주" for w in weekly)
    charts["wk_values"] = _join([w["copq_pct"] for w in weekly])
    charts["wk_target"] = wk_target["copq"]
    charts["wk_cur"] = len(weekly) - 1

    # TOP5 — 1PART/2PART 각각 항상 계산해두고, 화면에서 선택바로 전환한다(페이지 전체 파트 필터와 무관).
    mstart = f"{cy:04d}-{cm:02d}-01"
    mend = f"{cy:04d}-{cm:02d}-31"
    cur_week = weekly[-1]["week"] if weekly else 1
    ws = weekly[-1]["start"] if weekly else mstart
    we = weekly[-1]["end"] if weekly else mend
    top5 = {}
    for tp in ("VMS PART", "TM PART"):
        top5[tp] = {"month": calc.top5_defect(conn, m, mstart, mend, tp),
                    "week": calc.top5_defect(conn, m, ws, we, tp)}
    top_part_default = "TM PART" if part == "TM PART" else "VMS PART"
    return {"cy": cy, "cm": cm, "cur_fy": calc.fy_label(cur_fy), "cards": cards,
            "charts": charts, "top5": top5, "top_part_default": top_part_default,
            "cur_week": cur_week}


def build_weekly(conn, m, daily, cy, cm, part):
    """당월 주별(월~일) 지표. COPQ 외 카드 클릭 전환에 맞춰 전 지표를 계산한다.
    SVP·Warranty·claim은 월 단위로만 존재하므로, 그 주의 생산금액 비중만큼 월값을 비례배분한다
    (기존 COPQ 주별 계산과 동일한 방식을 전 지표로 확장)."""
    parts = calc._parts_for(part)
    ym = f"{cy:04d}-{cm:02d}"
    svp_month = calc.svp_of(conn, ym, parts)
    copq_claim_total = calc.claim_sum(conn, ym, parts, calc.CLAIM_COPQ_ITEMS)
    warranty_total = calc.claim_sum(conn, ym, parts, ["Warranty"])

    wk = defaultdict(lambda: {"scrap_qty": 0, "scrap_cost": 0.0, "scrap_cost_copq": 0.0,
                              "proc_qty": 0, "set_qty": 0, "prod_qty": 0, "prod_amount": 0.0,
                              "start": None, "end": None})
    from datetime import date, timedelta
    d = date(cy, cm, 1)
    month_prod_amt = 0.0
    while d.month == cm and d.year == cy:
        ds = d.isoformat()
        w = calc.week_of_month(ds)
        cell = wk[w]
        if cell["start"] is None:
            cell["start"] = ds
        cell["end"] = ds                             # 월~일 기준, 월 경계에서 잘린 첫/마지막 주 포함
        for p in parts:
            c = daily.get(ds, {}).get(p)
            if c:
                cell["scrap_qty"] += c["scrap_qty"]
                cell["scrap_cost"] += c["scrap_cost"]
                cell["scrap_cost_copq"] += c["scrap_cost_copq"]
                cell["proc_qty"] += c["proc_qty"]
                cell["set_qty"] += c["set_qty"]
                cell["prod_qty"] += c["prod_qty"]
                cell["prod_amount"] += c["prod_amount"]
                month_prod_amt += c["prod_amount"]
        d += timedelta(days=1)

    def pct(a, b):
        return round(a / b * 100, 2) if b else 0.0

    def ppm(a, b):
        return round(a / b * 1_000_000) if b else 0

    out = []
    for w in sorted(wk):
        cell = wk[w]
        pa = cell["prod_amount"]
        share = (pa / month_prod_amt) if month_prod_amt else 0
        svp_w = (svp_month * share) if svp_month else pa
        claim_w = copq_claim_total * share
        warranty_w = warranty_total * share
        incident_w = conn.execute(
            ("SELECT COUNT(*) c FROM incident WHERE is_official=1 AND d BETWEEN ? AND ? AND part IN (%s)"
             % ",".join("?" * len(parts))), [cell["start"], cell["end"]] + list(parts)).fetchone()["c"]
        copq_cost = cell["scrap_cost_copq"] + claim_w
        out.append({
            "week": w, "start": cell["start"], "end": cell["end"],
            "copq_pct": pct(copq_cost, svp_w),
            "scrap_cost_pct": pct(cell["scrap_cost"], svp_w),
            "scrap_qty_pct": pct(cell["scrap_qty"], cell["prod_qty"]),
            "proc_ppm": ppm(cell["proc_qty"], cell["prod_qty"]),
            "set_ppm": ppm(cell["set_qty"], cell["prod_qty"]),
            "incident": incident_w,
            "warranty": round(warranty_w),
            "prod_qty": cell["prod_qty"],
        })
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
    return RedirectResponse("/report/detail?tab=scrap_cost", status_code=303)


# ── 리포트: 세부지표현황 (사용자 제공 세부지표현황.xlsx 양식, 탭 구성) ──
TABS = [
    ("scrap_cost", "Scrap Cost"),
    ("scrap_qty", "Scrap Quantity"),
    ("copq", "COPQ"),
    ("incident", "Customer Incident"),
    ("warranty", "Warranty"),
    ("process1", "공정불량-1PART"),
    ("process2", "공정불량-2PART"),
    ("trend", "불량유형별"),
]
FY_MONTH_LABELS = [f"{mo}월" for mo in list(range(4, 13)) + list(range(1, 4))]


@app.get("/report/detail", response_class=HTMLResponse)
def report_detail(request: Request, tab: str = "scrap_cost", fy: int = 0, sub: str = "item",
                  tpart: str = "통합", d_tm: str = "", d_name: str = "",
                  d_from: str = "", d_to: str = "", d_unit: str = "일",
                  t1: str = "", t2: str = "", t3: str = "", t_unit: str = "일"):
    """세부지표현황: 사용자 제공 세부지표현황.xlsx 양식과 동일한 탭 구성.
    Scrap Cost/Quantity/COPQ/Customer Incident/Warranty는 Total·1Part·2Part 3블록×FY 12개월
    (+4월 왼쪽 누계 열), 공정불량-1PART/2PART는 파트별 종합(집계기준 EA)+공정별(발생기준 ppm),
    불량유형별은 [ITEM](Part·TM-NO·불량유형·집계단위 일/주/월) / [불량유형](Part·불량유형 최대 3개·
    집계단위 일/주/월) 두 서브탭."""
    u, g = _perm_guard(request, "rdetail", "view")
    if g:
        return g
    if tab not in {t for t, _n in TABS}:
        tab = "scrap_cost"
    conn = db.connect()
    m = calc.Masters(conn)
    cy, cm = latest_month(conn)
    cur_fy = calc.fy_of(cy, cm)
    fy = fy or cur_fy
    fy_opts = sorted({cur_fy, cur_fy - 1, cur_fy + 1})

    ctx = {"tab": tab, "tabs": TABS, "fy": fy, "fy_opts": fy_opts, "fy_label": calc.fy_label(fy),
           "months": FY_MONTH_LABELS, "PROC_LABEL": PROC_LABEL}

    if tab == "scrap_cost":
        ctx["data"] = report_kpi.scrap_cost_table(conn, m, fy)
        ctx["buckets"] = report_kpi.BUCKETS
    elif tab == "scrap_qty":
        ctx["data"] = report_kpi.scrap_qty_table(conn, m, fy)
        ctx["buckets"] = report_kpi.BUCKETS
    elif tab == "copq":
        ctx["data"] = report_kpi.copq_table(conn, m, fy)
        ctx["items"] = report_kpi.COPQ_ITEM_ORDER
    elif tab == "incident":
        ctx["data"] = report_kpi.incident_table(conn, m, fy)
    elif tab == "warranty":
        ctx["data"] = report_kpi.warranty_table(conn, m, fy)
    elif tab in ("process1", "process2"):
        part = "VMS PART" if tab == "process1" else "TM PART"
        ctx["data"] = report_kpi.process_table(conn, m, fy, part)
    else:  # trend
        if tpart not in ("통합", "VMS PART", "TM PART"):
            tpart = "통합"
        sub = sub if sub in ("item", "type") else "item"
        dtypes = [r["name"] for r in conn.execute(
            "SELECT DISTINCT name FROM defect_type ORDER BY name")]
        ctx.update({"sub": sub, "tpart": tpart, "defect_type_opts": dtypes})
        if sub == "item":
            d_unit = d_unit if d_unit in ("일", "주", "월") else "일"
            df, dt = d_from or f"{cy:04d}-{cm:02d}-01", d_to or f"{cy:04d}-{cm:02d}-28"
            trend = report_kpi.defect_trend(conn, tpart, d_tm, d_name, d_unit, df, dt)
            trend["chart_names"] = "|".join(trend["series"].keys())
            trend["chart_sets"] = "|".join(_join(v) for v in trend["series"].values())
            trend["labels_csv"] = ",".join(trend["labels"])
            ctx.update({"d_tm": d_tm, "d_name": d_name, "d_from": df, "d_to": dt,
                       "d_unit": d_unit, "trend": trend})
        else:
            t_unit = t_unit if t_unit in ("일", "주", "월") else "일"
            df, dt = d_from or f"{cy:04d}-{cm:02d}-01", d_to or f"{cy:04d}-{cm:02d}-28"
            types = [t1, t2, t3]
            trend2 = report_kpi.defect_trend_types(conn, m, tpart, types, t_unit, df, dt)
            trend2["chart_names"] = "|".join(trend2["series"].keys())
            trend2["chart_sets"] = "|".join(_join(v) for v in trend2["series"].values())
            trend2["labels_csv"] = ",".join(trend2["labels"])
            ctx.update({"t1": t1, "t2": t2, "t3": t3, "t_unit": t_unit,
                       "d_from": df, "d_to": dt, "trend2": trend2})
    conn.close()
    return render(request, "report_detail.html", u, active="rdetail", heading="세부지표현황",
                  crumb="개요", pending=pending_count(), **ctx)


@app.get("/report/tmno-search")
def report_tmno_search(request: Request, q: str = "", part: str = ""):
    """불량유형별[item별] TM-NO 자동완성용 JSON."""
    u = current_user(request)
    if u is None:
        return JSONResponse([])
    conn = db.connect()
    rows = report_kpi.tmno_search(conn, q.strip().upper(), part)
    conn.close()
    return JSONResponse(rows)


@app.get("/report/tmno-defects")
def report_tmno_defects(request: Request, tm: str = ""):
    """특정 TM-NO에 실제 발생한 불량유형 목록 JSON (불량유형 드롭다운 좁히기용)."""
    u = current_user(request)
    if u is None:
        return JSONResponse([])
    conn = db.connect()
    rows = report_kpi.defect_names_for_tm(conn, tm.strip())
    conn.close()
    return JSONResponse(rows)


# ── (구) 공정별 불량현황 → 세부지표현황으로 통합 (옛 링크 호환) ──
@app.get("/report/defect")
def report_defect_redirect(request: Request, part: str = "통합", kind: str = "공정"):
    tab = "process1" if part == "VMS PART" else "process2"
    return RedirectResponse(f"/report/detail?tab={tab}", status_code=303)


# ── 마스터 ──────────────────────────────────────────────
@app.get("/masters", response_class=HTMLResponse)
def masters(request: Request):
    u, g = _perm_guard(request, "masters", "view")
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
                  dtypes=dtypes, can_edit=(u["role"] == "admin" or has_perm(u["role"], "masters", "edit")))


@app.post("/masters/upload")
async def masters_upload(request: Request, kind: str = Form(...), file: UploadFile = File(...)):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "masters", "edit")):
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


def _hidden_tmnos(conn):
    return {r["tm_no"] for r in conn.execute("SELECT tm_no FROM data_check_hidden")}


def unreg_alert_count():
    """메뉴 경고 아이콘용: 숨김 처리 안 한 TM-NO 미등록 건수."""
    conn = db.connect()
    unreg_rows, _missing = _data_check(conn)
    conn.close()
    return len([r for r in unreg_rows if not r["hidden"]])


def _data_check(conn):
    """TM-NO 미등록 / 공정별 단가 누락 점검. 반환: (미등록 목록, 단가누락 목록)."""
    unreg = {}
    for r in conn.execute(
            "SELECT tm_no,d,part FROM defect_entry WHERE tm_no!='' AND tm_no NOT IN (SELECT tm_no FROM product)"):
        cur = unreg.setdefault(r["tm_no"], {"last_d": r["d"], "part": r["part"] or "", "src": set()})
        if r["d"] > cur["last_d"]:
            cur["last_d"] = r["d"]
        if r["part"] and not cur["part"]:
            cur["part"] = r["part"]
        cur["src"].add("사내/외주/폐기불량")
    for r in conn.execute(
            "SELECT tm_no,d,part FROM production WHERE tm_no NOT IN (SELECT tm_no FROM product)"):
        cur = unreg.setdefault(r["tm_no"], {"last_d": r["d"], "part": r["part"] or "", "src": set()})
        if r["d"] > cur["last_d"]:
            cur["last_d"] = r["d"]
        if r["part"] and not cur["part"]:
            cur["part"] = r["part"]
        cur["src"].add("생산량")
    hidden = _hidden_tmnos(conn)
    unreg_rows = [{"tm_no": tm, "part": v["part"], "last_d": v["last_d"], "src": "·".join(sorted(v["src"])),
                   "hidden": tm in hidden}
                  for tm, v in unreg.items()]
    unreg_rows.sort(key=lambda r: r["last_d"], reverse=True)

    routes = {}
    for r in conn.execute("SELECT tm_no,process FROM product_route"):
        b = db.bucket_of(r["process"])
        b = "기타" if b in ("가공", "기타") else b
        routes.setdefault(r["tm_no"], set()).add(b)
    prices = {}
    for r in conn.execute(
            "SELECT tm_no,process,unit_price FROM product_price WHERE effective_from<=date('now') "
            "ORDER BY effective_from"):
        prices.setdefault(r["tm_no"], {})[r["process"]] = r["unit_price"]

    def other_price(pmap):
        v = pmap.get("기타")
        return v if v is not None else pmap.get("가공")

    missing_rows = []
    for r in conn.execute("SELECT tm_no,name,part FROM product ORDER BY tm_no"):
        tm = r["tm_no"]
        route = routes.get(tm, set())
        pmap = prices.get(tm, {})
        if not route:
            missing_rows.append({"tm_no": tm, "name": r["name"], "part": r["part"], "missing": "(라우팅 없음)"})
            continue
        missing = [pr for pr in DISPLAY_PROCESSES if pr in route
                  and (other_price(pmap) if pr == "기타" else pmap.get(pr)) is None]
        if missing:
            missing_rows.append({"tm_no": tm, "name": r["name"], "part": r["part"], "missing": ", ".join(missing)})
    return unreg_rows, missing_rows


@app.get("/admin/data-check", response_class=HTMLResponse)
def data_check_page(request: Request, show_hidden: str = ""):
    """TM-NO 미등록·공정별 단가 누락 점검 화면. 관리자가 바로 등록 화면으로 이동해 채울 수 있다."""
    u, g = _perm_guard(request, "data_check", "view")
    if g:
        return g
    conn = db.connect()
    unreg_rows, missing_rows = _data_check(conn)
    conn.close()
    show_hidden = show_hidden == "1"
    hidden_count = len([r for r in unreg_rows if r["hidden"]])
    if not show_hidden:
        unreg_rows = [r for r in unreg_rows if not r["hidden"]]
    can_edit = u["role"] == "admin" or has_perm(u["role"], "data_check", "edit")
    return render(request, "data_check.html", u, active="data_check", heading="데이터 점검",
                  crumb="관리", pending=pending_count(), unreg_rows=unreg_rows, missing_rows=missing_rows,
                  hidden_count=hidden_count, show_hidden=show_hidden, can_edit=can_edit)


@app.post("/admin/data-check/hide")
def data_check_hide(request: Request, tm_no: str = Form(...)):
    u, g = _perm_guard(request, "data_check", "edit")
    if g:
        return g
    conn = db.connect()
    conn.execute(
        "INSERT INTO data_check_hidden(tm_no, hidden_at) VALUES(?, datetime('now')) "
        "ON CONFLICT(tm_no) DO NOTHING", (tm_no,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/data-check", status_code=303)


@app.post("/admin/data-check/unhide")
def data_check_unhide(request: Request, tm_no: str = Form(...)):
    u, g = _perm_guard(request, "data_check", "edit")
    if g:
        return g
    conn = db.connect()
    conn.execute("DELETE FROM data_check_hidden WHERE tm_no=?", (tm_no,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/data-check?show_hidden=1", status_code=303)


@app.get("/admin/products", response_class=HTMLResponse)
def products_list(request: Request, q: str = "", part: str = "", miss: str = "",
                  page: int = 1, msg: str = "", err: str = ""):
    """제품 마스터 + 공정별 단가 통합 목록."""
    u, g = _perm_guard(request, "products", "view")
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
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "products", "edit")), msg=msg, err=err)


@app.post("/admin/products/import")
async def products_import(request: Request, part: str = Form(...), file: UploadFile = File(...)):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "products", "edit")):
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
def product_new(request: Request, tm: str = ""):
    u, g = _perm_guard(request, "products", "edit")
    if g:
        return g
    conn = db.connect()
    excl = _copq_excluded(conn)
    tm = calc.base_tmno(tm) if tm else ""
    part_guess = "VMS PART"
    suggest = {"rest": None, "with_jh": None, "no_jh": None}
    if tm:
        r = conn.execute("SELECT part FROM defect_entry WHERE tm_no=? AND part!='' LIMIT 1", (tm,)).fetchone()
        if not r:
            r = conn.execute("SELECT part FROM production WHERE tm_no=? AND part!='' LIMIT 1", (tm,)).fetchone()
        if r:
            part_guess = r["part"]
        suggest = price_calc.suggest_prices(conn, tm)
    conn.close()
    return render(request, "product_form.html", u, active="products", heading="제품 등록",
                  crumb="관리 / 제품 마스터", pending=pending_count(), mode="new",
                  p={"tm_no": tm, "name": "", "part": part_guess},
                  agg=DISPLAY_PROCESSES, excl=excl, suggest=suggest,
                  route=set(), price={pr: None for pr in DISPLAY_PROCESSES})


@app.get("/admin/products/{tm}/edit", response_class=HTMLResponse)
def product_edit(request: Request, tm: str):
    """수정 폼: 제품 정보 + 공정 라우팅 + 공정별 단가를 현재 값으로 채워 보여준다."""
    u, g = _perm_guard(request, "products", "edit")
    if g:
        return g
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
    suggest = price_calc.suggest_prices(conn, tm)
    conn.close()
    return render(request, "product_form.html", u, active="products", heading="제품 수정",
                  crumb="관리 / 제품 마스터", pending=pending_count(), mode="edit",
                  p=dict(p), agg=DISPLAY_PROCESSES, excl=excl, route=route, price=price,
                  suggest=suggest)


@app.post("/admin/products/save")
async def product_save(request: Request):
    """제품 정보 + 라우팅 + 공정별 단가를 한 번에 저장."""
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "products", "edit")):
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
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "products", "edit")):
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
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "scan", "edit")), msg=msg, err=err)


@app.get("/admin/scan", response_class=HTMLResponse)
def scan_page(request: Request, msg: str = "", err: str = ""):
    u, g = _perm_guard(request, "scan", "view")
    if g:
        return g
    return _scan_page(request, u, msg=msg, err=err)


@app.post("/admin/scan/root")
async def scan_set_root(request: Request, root: str = Form("")):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "scan", "edit")):
        return RedirectResponse("/admin/scan", status_code=303)
    conn = db.connect()
    db.set_setting(conn, scan.SETTING_ROOT, root.strip())
    conn.close()
    return RedirectResponse("/admin/scan?msg=경로 저장됨", status_code=303)


@app.post("/admin/scan/init", response_class=HTMLResponse)
async def scan_init_data(request: Request):
    """templates/ 실데이터로 DB 초기 구축 (최초 1회). 적재는 멱등이라 재실행해도 중복 없음."""
    u, g = _perm_guard(request, "scan", "edit")
    if g:
        return g
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
    u, g = _perm_guard(request, "scan", "edit")
    if g:
        return g
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
    u, g = _perm_guard(request, "scan", "edit")
    if g:
        return g
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
    u, g = _perm_guard(request, "processes", "view")
    if g:
        return g
    conn = db.connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM process ORDER BY part,ord,name")]
    conn.close()
    return render(request, "processes.html", u, active="processes", heading="공정 관리",
                  crumb="관리", pending=pending_count(), rows=rows,
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "processes", "edit")), msg=msg, err=err)


@app.post("/admin/processes/save")
async def processes_save(request: Request):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "processes", "edit")):
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
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "processes", "edit")):
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
    u, g = _perm_guard(request, "defect_types", "view")
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
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "defect_types", "edit")), msg=msg, err=err)


@app.post("/admin/defect-types/save")
async def defect_types_save(request: Request):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "defect_types", "edit")):
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
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "defect_types", "edit")):
        return RedirectResponse("/admin/defect-types", status_code=303)
    conn = db.connect()
    conn.execute("DELETE FROM defect_type WHERE id=?", (did,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/defect-types?msg=삭제됨", status_code=303)


# ── 고객사 마스터 ───────────────────────────────────────
# 정식명(name)은 생산량 파일의 '주거래처'(E열)와 같게 유지한다 — 그 파일이 파트별로 나뉘어 있어
# 고객사→파트가 유일하게 결정되고, Claim·Customer Incident의 파트 판정 근거가 된다.
# short_name은 월마감 보고서 표기명(약칭)으로, 보고서에서 업체별 행 이름으로 쓴다.
@app.get("/admin/customers", response_class=HTMLResponse)
def customers_list(request: Request, q: str = "", part: str = "", edit: int = 0,
                   msg: str = "", err: str = ""):
    u, g = _perm_guard(request, "customers", "view")
    if g:
        return g
    conn = db.connect()
    where, args = [], []
    if q:
        where.append("(name LIKE ? OR short_name LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if part in ("VMS PART", "TM PART"):
        where.append("part=?")
        args.append(part)
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM customer {wsql} ORDER BY part, name", args)]
    edit_row = None
    if edit:
        r = conn.execute("SELECT * FROM customer WHERE id=?", (edit,)).fetchone()
        edit_row = dict(r) if r else None
    conn.close()
    return render(request, "customers.html", u, active="customers", heading="고객사 마스터",
                  crumb="관리", pending=pending_count(), rows=rows, edit_row=edit_row,
                  q=q, part=part, msg=msg, err=err,
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "customers", "edit")))


@app.post("/admin/customers/save")
async def customers_save(request: Request, cid: str = Form(""), name: str = Form(...),
                         short_name: str = Form(""), part: str = Form(""),
                         active: str = Form(""), memo: str = Form("")):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "customers", "edit")):
        return RedirectResponse("/admin/customers", status_code=303)
    name = name.strip()
    if not name:
        return RedirectResponse("/admin/customers?err=고객사명은 필수입니다", status_code=303)
    if part not in ("VMS PART", "TM PART", ""):
        part = ""
    act = 1 if active else 0
    conn = db.connect()
    try:
        if cid:
            conn.execute("UPDATE customer SET name=?,short_name=?,part=?,active=?,memo=? WHERE id=?",
                        (name, short_name.strip(), part, act, memo.strip(), int(cid)))
        else:
            conn.execute("INSERT INTO customer(name,short_name,part,active,memo) VALUES(?,?,?,?,?)",
                        (name, short_name.strip(), part, act, memo.strip()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return RedirectResponse(f"/admin/customers?err=이미 등록된 고객사명입니다: {name}", status_code=303)
    finally:
        conn.close()
    return RedirectResponse(f"/admin/customers?msg=저장됨: {name}", status_code=303)


@app.post("/admin/customers/{cid}/delete")
def customers_delete(request: Request, cid: int):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "customers", "edit")):
        return RedirectResponse("/admin/customers", status_code=303)
    conn = db.connect()
    conn.execute("DELETE FROM customer WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/customers?msg=삭제됨", status_code=303)


# ── 목표 관리 ───────────────────────────────────────────
# 반별목표제(월마감 보고서 §3) 공정별 공정불량 ppm 목표.
# 파트별로만 존재(통합 없음), 월별 변동 없이 FY 연간 목표 하나만 쓴다(2026-07-30 확정).
BAN_PROCESSES = ["성형", "소결", "정형", "가공"]


def ban_kpi(proc):
    return "ban_ppm_" + proc


BAN_KPIS = [ban_kpi(p) for p in BAN_PROCESSES]
# 통합에는 목표를 두지 않는 지표(파트별만 의미가 있음)
PART_ONLY_KPIS = set(MONTHLY_KPIS) | set(BAN_KPIS)


@app.get("/admin/target", response_class=HTMLResponse)
def admin_target(request: Request, fy: int = 27, part: str = "통합"):
    u, g = _perm_guard(request, "target", "view")
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
    ban_order = [(ban_kpi(p), f"반별목표제 · {p}", "ppm") for p in BAN_PROCESSES]
    items = [{"kpi": k, "name": n, "unit": un,
              "value": rows.get((k, 0), ""), "monthly": k in MONTHLY_KPIS,
              "part_only": k in PART_ONLY_KPIS}
             for k, n, un in order + ban_order]
    # 월별 목표(FY 순서: 4월~익년 3월)
    fy_months = [m for m in range(4, 13)] + [m for m in range(1, 4)]
    monthly = [{"kpi": k, "name": n, "unit": un,
                "vals": [{"mon": mm, "value": rows.get((k, mm), "")} for mm in fy_months]}
               for k, n, un in order if k in MONTHLY_KPIS]
    return render(request, "target.html", u, active="target", heading="목표 관리",
                  crumb="관리", pending=pending_count(), fy=fy, part=part, items=items,
                  monthly=monthly, fy_months=fy_months,
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "target", "edit")))


@app.post("/admin/target")
async def admin_target_save(request: Request):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "target", "edit")):
        return RedirectResponse("/admin/target", status_code=303)
    form = await request.form()
    fy = int(form.get("fy")); part = form.get("part")
    units = {"scrap_cost": "%", "scrap_qty": "%", "copq": "%", "incident": "건",
             "warranty": "천원", "proc_ppm": "ppm", "set_ppm": "ppm"}
    units.update({k: "ppm" for k in BAN_KPIS})
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
        if kpi in PART_ONLY_KPIS and part == "통합":
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
# 관리자만 editor·viewer 계정을 추가/수정/삭제할 수 있다(admin 계정 자체는 이 화면에서 다루지 않음).
@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, edit: str = "", msg: str = "", err: str = ""):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    if u["role"] != "admin":
        return RedirectResponse(FORBIDDEN_REDIRECT, status_code=303)
    conn = db.connect()
    users = [dict(r) for r in conn.execute("SELECT username,name,role FROM users ORDER BY role")]
    edit_row = None
    if edit:
        row = conn.execute("SELECT username,name,role FROM users WHERE username=? AND role IN ('editor','viewer')",
                           (edit,)).fetchone()
        edit_row = dict(row) if row else None
    perm_data = load_permissions(conn)
    conn.close()
    return render(request, "users.html", u, active="users", heading="사용자 관리",
                  crumb="관리", pending=pending_count(), users=users, edit_row=edit_row,
                  can_edit=(u["role"] == "admin"), msg=msg, err=err,
                  perm_matrix=PERM_MENUS, perm_data=perm_data)


@app.post("/admin/users/save")
def admin_users_save(request: Request, orig_username: str = Form(""), username: str = Form(...),
                     name: str = Form(...), role: str = Form(...), password: str = Form("")):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse(FORBIDDEN_REDIRECT, status_code=303)
    username = username.strip()
    if role not in ("editor", "viewer"):
        return RedirectResponse("/admin/users?err=권한은 editor 또는 viewer만 지정할 수 있습니다.", status_code=303)
    if not username or not name.strip():
        return RedirectResponse("/admin/users?err=아이디와 이름을 입력하세요.", status_code=303)
    conn = db.connect()
    try:
        if orig_username:
            existing = conn.execute("SELECT role FROM users WHERE username=?", (orig_username,)).fetchone()
            if not existing or existing["role"] not in ("editor", "viewer"):
                conn.close()
                return RedirectResponse("/admin/users?err=admin 계정은 이 화면에서 수정할 수 없습니다.", status_code=303)
            if username != orig_username:
                dup = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
                if dup:
                    conn.close()
                    return RedirectResponse("/admin/users?err=이미 사용 중인 아이디입니다.", status_code=303)
            if password:
                conn.execute("UPDATE users SET username=?,name=?,role=?,pw_hash=? WHERE username=?",
                            (username, name.strip(), role, db.hash_pw(password), orig_username))
            else:
                conn.execute("UPDATE users SET username=?,name=?,role=? WHERE username=?",
                            (username, name.strip(), role, orig_username))
        else:
            dup = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
            if dup:
                conn.close()
                return RedirectResponse("/admin/users?err=이미 사용 중인 아이디입니다.", status_code=303)
            if not password:
                conn.close()
                return RedirectResponse("/admin/users?err=새 계정은 비밀번호를 입력하세요.", status_code=303)
            conn.execute("INSERT INTO users(username,name,pw_hash,role) VALUES(?,?,?,?)",
                        (username, name.strip(), db.hash_pw(password), role))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/users?msg=저장했습니다.", status_code=303)


@app.post("/admin/users/{username}/delete")
def admin_users_delete(request: Request, username: str):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse(FORBIDDEN_REDIRECT, status_code=303)
    conn = db.connect()
    row = conn.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
    if row and row["role"] in ("editor", "viewer"):
        conn.execute("DELETE FROM users WHERE username=?", (username,))
        conn.commit()
    conn.close()
    return RedirectResponse("/admin/users?msg=삭제했습니다.", status_code=303)


@app.post("/admin/permissions/save")
async def admin_permissions_save(request: Request):
    """editor/viewer 메뉴별 보기·편집 권한 매트릭스 저장(관리자 전용, 관리자 자신은 매트릭스 대상 아님)."""
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse(FORBIDDEN_REDIRECT, status_code=303)
    form = await request.form()
    conn = db.connect()
    for role in ("editor", "viewer"):
        for key, _label in PERM_MENUS:
            can_view = 1 if form.get(f"p_{role}_{key}_view") else 0
            can_edit = 1 if form.get(f"p_{role}_{key}_edit") else 0
            conn.execute(
                "INSERT INTO permission(role,menu_key,can_view,can_edit) VALUES(?,?,?,?) "
                "ON CONFLICT(role,menu_key) DO UPDATE SET can_view=excluded.can_view, can_edit=excluded.can_edit",
                (role, key, can_view, can_edit))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/users?msg=권한을 저장했습니다.", status_code=303)


# ── 데이터 입력 ─────────────────────────────────────────
# 사내불량·외주·폐기·생산은 '폴더 반영'(/admin/scan)으로 수집하므로 업로드 화면을 두지 않는다.
# SVP는 FY 표(아래), Claim·Customer Incident는 건별 직접 등록(원장) 화면.
@app.get("/input/outsource-review", response_class=HTMLResponse)
def outsource_review_page(request: Request):
    u, g = _perm_guard(request, "oreview", "view")
    if g:
        return g
    return outsource_review(request, u)


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
    u, g = _perm_guard(request, "svp", "view")
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
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "svp", "edit")))


@app.post("/input/svp/save")
async def svp_save(request: Request):
    """FY 표 저장. 빈칸은 삭제(그 달은 생산금액 추정으로 계산)."""
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "svp", "edit")):
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


# ── Claim 입력 (건별 직접 등록) ──────────────────────────
CLAIM_ITEM_OPTIONS = calc.CLAIM_COPQ_ITEMS + ["기타"]


@app.get("/input/claim", response_class=HTMLResponse)
def claim_page(request: Request, edit: int = 0, msg: str = "", err: str = ""):
    u, g = _perm_guard(request, "claim", "view")
    if g:
        return g
    conn = db.connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT id,d,part,customer,tm_no,product_name,item,amount,reclaim,use_agg,content FROM claim "
        "ORDER BY d DESC,id DESC LIMIT 200")]
    edit_row = None
    if edit:
        r = conn.execute(
            "SELECT id,d,part,customer,tm_no,product_name,item,amount,reclaim,content FROM claim WHERE id=?",
            (edit,)).fetchone()
        if r:
            edit_row = dict(r)
            # 원 단위 화면 입력에 맞춰 천원 저장값을 다시 ×1000 해서 채운다
            edit_row["amount_won"] = edit_row["amount"] * 1000
            edit_row["reclaim_won"] = edit_row["reclaim"] * 1000
            edit_row["item_is_custom"] = edit_row["item"] not in CLAIM_ITEM_OPTIONS
    conn.close()
    return render(request, "claim.html", u, active="claim", heading="Claim 입력",
                  crumb="데이터 입력", pending=pending_count(), rows=rows,
                  item_options=CLAIM_ITEM_OPTIONS, edit_row=edit_row,
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "claim", "edit")), msg=msg, err=err)


@app.post("/input/claim/save")
async def claim_save(request: Request):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "claim", "edit")):
        return RedirectResponse("/input/claim", status_code=303)
    form = await request.form()
    orig_id = (form.get("orig_id") or "").strip()
    d = (form.get("d") or "").strip()
    part = form.get("part") or "VMS PART"
    customer = (form.get("customer") or "").strip()
    tm_no = calc.base_tmno((form.get("tm_no") or "").strip())
    product_name = (form.get("product_name") or "").strip()
    item = form.get("item") or ""
    if item == "기타":
        item = (form.get("item_etc") or "").strip() or "기타"
    content = (form.get("content") or "").strip()
    raw = (form.get("amount") or "").replace(",", "").strip()
    raw_reclaim = (form.get("reclaim") or "").replace(",", "").strip()
    if not d or not item or raw == "":
        eq = f"&edit={orig_id}" if orig_id else ""
        return RedirectResponse(f"/input/claim?err=날짜·항목·전표금액은 필수입니다{eq}", status_code=303)
    try:
        amount_won = float(raw)
        reclaim_won = float(raw_reclaim) if raw_reclaim else 0.0
    except ValueError:
        eq = f"&edit={orig_id}" if orig_id else ""
        return RedirectResponse(f"/input/claim?err=금액이 숫자가 아닙니다{eq}", status_code=303)
    # 입력은 원 단위, 저장은 다른 COPQ 계산과 맞춰 천원 단위
    amount = amount_won / 1000.0
    reclaim = reclaim_won / 1000.0
    conn = db.connect()
    if orig_id:
        conn.execute(
            "UPDATE claim SET d=?,part=?,customer=?,tm_no=?,product_name=?,item=?,amount=?,reclaim=?,content=? "
            "WHERE id=?",
            (d, part, customer, tm_no, product_name, item, amount, reclaim, content, orig_id))
        msg = "수정됨"
    else:
        conn.execute(
            "INSERT INTO claim(d,part,customer,tm_no,product_name,item,amount,reclaim,content,reg_user) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (d, part, customer, tm_no, product_name, item, amount, reclaim, content, u["name"]))
        msg = "등록됨"
    conn.commit()
    conn.close()
    return RedirectResponse(f"/input/claim?msg={msg}", status_code=303)


@app.post("/input/claim/{cid}/toggle-agg")
def claim_toggle_agg(request: Request, cid: int):
    """이력표의 '집계 포함' 체크박스: 체크된 건만 COPQ 등 집계에 반영."""
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "claim", "edit")):
        return RedirectResponse("/input/claim", status_code=303)
    conn = db.connect()
    conn.execute("UPDATE claim SET use_agg=1-use_agg WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/input/claim", status_code=303)


@app.post("/input/claim/{cid}/delete")
def claim_delete(request: Request, cid: int):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "claim", "edit")):
        return RedirectResponse("/input/claim", status_code=303)
    conn = db.connect()
    conn.execute("DELETE FROM claim WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/input/claim?msg=삭제됨", status_code=303)


# ── Customer Incident 관리 (건별 직접 등록) ──────────────
@app.get("/input/incident", response_class=HTMLResponse)
def incident_page(request: Request, edit: int = 0, msg: str = "", err: str = ""):
    u, g = _perm_guard(request, "incident", "view")
    if g:
        return g
    conn = db.connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT id,d,part,customer,tm_no,product_name,content,is_official FROM incident "
        "ORDER BY d DESC,id DESC LIMIT 200")]
    edit_row = None
    if edit:
        r = conn.execute(
            "SELECT id,d,part,customer,tm_no,product_name,content,is_official FROM incident WHERE id=?",
            (edit,)).fetchone()
        if r:
            edit_row = dict(r)
    conn.close()
    return render(request, "incident.html", u, active="incident", heading="Customer Incident 관리",
                  crumb="데이터 입력", pending=pending_count(), rows=rows, edit_row=edit_row,
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "incident", "edit")), msg=msg, err=err)


@app.post("/input/incident/save")
async def incident_save(request: Request):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "incident", "edit")):
        return RedirectResponse("/input/incident", status_code=303)
    form = await request.form()
    orig_id = (form.get("orig_id") or "").strip()
    d = (form.get("d") or "").strip()
    part = form.get("part") or "VMS PART"
    customer = (form.get("customer") or "").strip()
    tm_no = calc.base_tmno((form.get("tm_no") or "").strip())
    product_name = (form.get("product_name") or "").strip()
    content = (form.get("content") or "").strip()
    is_official = 1 if form.get("is_official") else 0
    if not d:
        eq = f"&edit={orig_id}" if orig_id else ""
        return RedirectResponse(f"/input/incident?err=날짜는 필수입니다{eq}", status_code=303)
    conn = db.connect()
    if orig_id:
        conn.execute(
            "UPDATE incident SET d=?,part=?,customer=?,tm_no=?,product_name=?,content=?,is_official=? "
            "WHERE id=?",
            (d, part, customer, tm_no, product_name, content, is_official, orig_id))
        msg = "수정됨"
    else:
        conn.execute(
            "INSERT INTO incident(d,part,customer,tm_no,product_name,content,is_official,reg_user) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (d, part, customer, tm_no, product_name, content, is_official, u["name"]))
        msg = "등록됨"
    conn.commit()
    conn.close()
    return RedirectResponse(f"/input/incident?msg={msg}", status_code=303)


@app.post("/input/incident/{iid}/toggle-official")
def incident_toggle_official(request: Request, iid: int):
    """이력표의 '공식' 체크박스: 공식(is_official=1) 건만 Customer Incident KPI에 반영."""
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "incident", "edit")):
        return RedirectResponse("/input/incident", status_code=303)
    conn = db.connect()
    conn.execute("UPDATE incident SET is_official=1-is_official WHERE id=?", (iid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/input/incident", status_code=303)


@app.post("/input/incident/{iid}/delete")
def incident_delete(request: Request, iid: int):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "incident", "edit")):
        return RedirectResponse("/input/incident", status_code=303)
    conn = db.connect()
    conn.execute("DELETE FROM incident WHERE id=?", (iid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/input/incident?msg=삭제됨", status_code=303)


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
                  crumb="데이터 입력", pending=len(rows), rows=rows, agg_processes=db.AGG_PROCESSES,
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "oreview", "edit")))


@app.post("/input/outsource-review/{eid}")
async def review_action(request: Request, eid: int, action: str = Form(...), qty: int = Form(None),
                        process: str = Form("")):
    """검토 대기 건 처리: approve(수량·집계공정 그대로/수정 반영)·reject(삭제).
    집계공정을 수정해도 원본 파일(외주소재 xlsm·폐기 scrap_data.db)은 건드리지 않는다 —
    defect_entry.process(이 시스템의 사본)만 바뀌고, 이후 집계는 이 값을 기준으로 계산된다.
    사람이 결정한 건은 reviewed=1로 표시해 다음 폴더 반영(재스캔) 때도 덮어써지지 않는다."""
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "oreview", "edit")):
        return RedirectResponse("/input/outsource-review", status_code=303)
    conn = db.connect()
    if action == "approve":
        proc = process.strip() if process and process.strip() in db.AGG_PROCESSES else None
        if qty is not None and proc:
            conn.execute("UPDATE defect_entry SET qty=?, process=?, status='confirmed', reviewed=1 WHERE id=?",
                        (qty, proc, eid))
        elif qty is not None:
            conn.execute("UPDATE defect_entry SET qty=?, status='confirmed', reviewed=1 WHERE id=?", (qty, eid))
        elif proc:
            conn.execute("UPDATE defect_entry SET process=?, status='confirmed', reviewed=1 WHERE id=?", (proc, eid))
        else:
            conn.execute("UPDATE defect_entry SET status='confirmed', reviewed=1 WHERE id=?", (eid,))
    elif action == "reject":
        # 하드 삭제 대신 상태만 'rejected'로 바꿔 집계에서 제외(status='confirmed' 조건에 안 걸림)하고,
        # reviewed=1로 표시해 다음 폴더 재스캔에도 이 건이 다시 나타나지 않게 한다.
        conn.execute("UPDATE defect_entry SET status='rejected', reviewed=1 WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/input/outsource-review", status_code=303)
