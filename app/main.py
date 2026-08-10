# -*- coding: utf-8 -*-
"""통합품질관리시스템 웹 앱 (FastAPI). 포트 5003.

실행: uvicorn app.main:app --host 0.0.0.0 --port 5003
"""
import os
import json
import secrets
import sqlite3
import datetime as _dt
from collections import defaultdict

from fastapi import FastAPI, Request, Form, UploadFile, File, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, calc, ingest, scan, init_data, price_calc, report_kpi, report_monthly

BASE = os.path.dirname(__file__)
app = FastAPI(title="통합품질관리시스템")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
tpl = Jinja2Templates(directory=os.path.join(BASE, "templates"))
tpl.env.filters["cf"] = lambda v: f"{v:,.0f}" if isinstance(v, (int, float)) else v
tpl.env.filters["cf2"] = lambda v: f"{v:,.2f}" if isinstance(v, (int, float)) else v

# 화면 표기 통일: DB는 'VMS PART'/'TM PART'로 저장하고, 화면에는 1PART/2PART로 보여준다.
PART_LABEL = {"VMS PART": "1PART", "TM PART": "2PART"}
tpl.env.filters["pl"] = lambda v: PART_LABEL.get(str(v).strip(), v)
tpl.env.filters["is_pdf"] = lambda name: str(name or "").lower().endswith(".pdf")
# 동영상 첨부: 브라우저가 그대로 재생할 수 있는 형식만 플레이어로 띄운다(나머지는 변환 안내).
tpl.env.filters["is_playable_video"] = lambda name: str(name or "").lower().endswith(
    (".mp4", ".webm", ".ogg", ".ogv", ".m4v"))
tpl.env.globals["PARTS"] = [("VMS PART", "1PART"), ("TM PART", "2PART")]

# 발생원인·개선대책처럼 여러 줄로 입력받는 textarea 값을 보고서 한 줄에 이어붙일 때,
# 줄바꿈을 그냥 없애면 문장이 다 붙어버려 읽기 어렵다. 일반 공백은 HTML에서 겹치면
# 하나로 줄어드니, 시각적으로 확실히 벌어지도록 줄바꿈마다 줄임없는 공백(nbsp) 2개로
# 바꿔 한 줄 안에서도 항목 사이 간격이 보이게 한다(2026-07-31).
tpl.env.filters["oneline"] = lambda v: str(v).replace("\r\n", "\n").replace("\n", "  ") if v else "-"

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


def new_dtype_count():
    """사내불량 시트에서 발견된 미등록 불량유형 건수(불량유형 마스터 메뉴 알람용)."""
    conn = db.connect()
    n = len(ingest.pending_defect_types(conn))
    conn.close()
    return n


def menu_perms(role):
    """사이드바용 메뉴별 보기 권한 {menu_key: bool}. 권한 없는 메뉴는 흐리게 + 클릭 불가로 그린다.
    한 번의 조회로 만든다(메뉴마다 has_perm을 부르면 페이지당 십여 번 DB를 여는 셈이 된다)."""
    keys = tuple(db.PERM_MENU_KEYS) + ("users",)
    if role == "admin":
        return {k: True for k in keys}
    conn = db.connect()
    saved = {r["menu_key"]: bool(r["can_view"]) for r in conn.execute(
        "SELECT menu_key, can_view FROM permission WHERE role=?", (role,))}
    conn.close()
    out = {k: saved.get(k, True) for k in db.PERM_MENU_KEYS}
    out["dash"] = True        # 로그인 후 갈 곳이 없어지지 않게 항상 허용
    out["users"] = False      # 사용자 관리는 관리자 전용(권한 매트릭스 대상 밖)
    return out


def render(request, template, user, **ctx):
    ctx.setdefault("crumb", "")
    can = menu_perms(user["role"])
    ctx.setdefault("can_menu", can)
    ctx.setdefault("unreg_alert", unreg_alert_count() if can.get("data_check") else 0)
    ctx.setdefault("dtype_alert", new_dtype_count() if can.get("defect_types") else 0)
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
    ("monthly", "월마감 보고서"),
    ("svp", "SVP 입력"),
    ("claim", "Claim 입력"),
    ("incident", "Customer Incident 관리"),
    ("internal_issue", "내부품질 Issue 관리"),
    ("capa", "개선대책서 (현장품질회의)"),
    ("oreview", "불량 검토(100EA↑)"),
    ("data_check", "데이터 점검"),
    ("products", "제품 마스터"),
    ("processes", "공정 관리"),
    ("defect_types", "불량유형 마스터"),
    ("customers", "고객사 마스터"),
    ("scan", "폴더 반영"),
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
        """월별 실적과 나란히 놓는 목표선. warranty·incident는 FY 목표가 연간 합계라
        그대로 두면 월 실적과 자릿수가 안 맞으므로 12개월 균등배분값을 쓴다."""
        return [calc.monthly_target(target_val(conn, fy, part, kpi), kpi) for fy in fys]

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
    # 지난주 = 해당주와 같은 요일 범위를 7일 앞으로 옮긴 것. 월 경계를 넘나들 수 있어(예: 이번 달
    # 1주차의 지난주는 지난달 말) 'N월 M주차'로 표기하지 않고 날짜 범위로 보여준다.
    pw_s = (_dt.date.fromisoformat(ws) - _dt.timedelta(days=7)).isoformat()
    pw_e = (_dt.date.fromisoformat(we) - _dt.timedelta(days=7)).isoformat()
    prev_week_label = f"{int(pw_s[5:7])}/{int(pw_s[8:10])}~{int(pw_e[5:7])}/{int(pw_e[8:10])}"
    # 지난달 = 이번달의 바로 전 달(월 경계).
    pm_y, pm_m = (cy - 1, 12) if cm == 1 else (cy, cm - 1)
    pmstart = f"{pm_y:04d}-{pm_m:02d}-01"
    pmend = f"{pm_y:04d}-{pm_m:02d}-31"
    prev_month_label = f"{pm_y:04d}-{pm_m:02d}"
    top5 = {}
    for tp in ("VMS PART", "TM PART"):
        top5[tp] = {"month": calc.top5_defect(conn, m, mstart, mend, tp),
                    "prev_month": calc.top5_defect(conn, m, pmstart, pmend, tp),
                    "week": calc.top5_defect(conn, m, ws, we, tp),
                    "prev_week": calc.top5_defect(conn, m, pw_s, pw_e, tp)}
    top_part_default = "TM PART" if part == "TM PART" else "VMS PART"
    return {"cy": cy, "cm": cm, "cur_fy": calc.fy_label(cur_fy), "cards": cards,
            "charts": charts, "top5": top5, "top_part_default": top_part_default,
            "prev_week_label": prev_week_label, "prev_month_label": prev_month_label,
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
        # 불량유형 목록: 실제 발생한 공정불량 기준(셋팅 제외, 폐기는 '폐기불량' 하나로 묶임)
        dtypes = report_kpi.defect_type_options(conn, m)
        ctx.update({"sub": sub, "tpart": tpart, "defect_type_opts": dtypes})
        if sub == "item":
            d_unit = d_unit if d_unit in ("일", "주", "월") else "일"
            df, dt = d_from or f"{cy:04d}-{cm:02d}-01", d_to or f"{cy:04d}-{cm:02d}-28"
            trend = report_kpi.defect_trend(conn, m, tpart, d_tm, d_name, d_unit, df, dt)
            trend["chart_names"] = "|".join(trend["series"].keys())
            trend["chart_sets"] = "|".join(_join(v) for v in trend["series"].values())
            trend["labels_csv"] = ",".join(trend["labels"])
            trend["notes_json"] = json.dumps(
                [trend["notes"].get(k, []) for k in trend["series"]], ensure_ascii=False)
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
            trend2["notes_json"] = json.dumps(
                [trend2["notes"].get(k, []) for k in trend2["series"]], ensure_ascii=False)
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
    rows = report_kpi.defect_names_for_tm(conn, calc.Masters(conn), tm.strip())
    conn.close()
    return JSONResponse(rows)


# ── (구) 공정별 불량현황 → 세부지표현황으로 통합 (옛 링크 호환) ──
@app.get("/report/defect")
def report_defect_redirect(request: Request, part: str = "통합", kind: str = "공정"):
    tab = "process1" if part == "VMS PART" else "process2"
    return RedirectResponse(f"/report/detail?tab={tab}", status_code=303)


# ── 월마감 보고서 ───────────────────────────────────────
# 데이터로 뽑을 수 없는 서술 항목만 사람이 입력한다(그 외 모든 수치는 시스템이 계산).
# 보고서는 '해당 마감월'의 내용만 담는다(예: 7월마감 → 7월 이슈만) — 2026-07-30 확정.
REPORT_SECTIONS = [
    ("main_tasks", "주요 업무 진행 현황",
     "이번 달 주요 업무 진행 현황을 줄바꿈으로 구분해 입력하세요.\n"
     "보고서에는 입력한 줄바꿈이 그대로 유지됩니다."),
]


def _report_months(conn, n=13):
    """월마감 대상 후보 (년,월) 목록 — 생산량 데이터가 있는 마지막 달부터 과거 n개월."""
    cy, cm = latest_month(conn)
    return calc.trailing_months(cy, cm, n)[::-1]      # 최신월이 위로


@app.get("/report/monthly", response_class=HTMLResponse)
def monthly_close(request: Request, ym: str = "", msg: str = "", err: str = ""):
    """월마감 보고서 — 서술 항목 작성 화면. 마감월을 골라 텍스트를 입력/수정한다."""
    u, g = _perm_guard(request, "monthly", "view")
    if g:
        return g
    conn = db.connect()
    months = _report_months(conn)
    valid = {"%04d-%02d" % (y, mo) for (y, mo) in months}
    if ym not in valid:
        y0, m0 = months[0]
        ym = "%04d-%02d" % (y0, m0)
    saved = {r["section"]: dict(r) for r in conn.execute(
        "SELECT section, content, updated_at, updated_by FROM report_text WHERE ym=?", (ym,))}
    cache = conn.execute("SELECT built_at, built_by FROM report_cache WHERE ym=?", (ym,)).fetchone()
    cache = dict(cache) if cache else None
    conn.close()
    sections = [{"key": k, "name": n, "hint": h,
                 "content": saved.get(k, {}).get("content", ""),
                 "updated_at": saved.get(k, {}).get("updated_at", ""),
                 "updated_by": saved.get(k, {}).get("updated_by", "")}
                for k, n, h in REPORT_SECTIONS]
    return render(request, "monthly_close.html", u, active="monthly", heading="월마감 보고서",
                  crumb="리포트", pending=pending_count(), ym=ym, sections=sections,
                  ym_opts=["%04d-%02d" % (y, mo) for (y, mo) in months],
                  cache=cache, msg=msg, err=err,
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "monthly", "edit")))


@app.get("/report/monthly/view", response_class=HTMLResponse)
def monthly_view(request: Request, ym: str = "", rebuild: str = ""):
    """월마감 보고서 발표 화면(새창). 캐시된 결과를 즉시 렌더 — 발표 중 지연 방지.
    rebuild=1 이면 다시 계산해 캐시를 갱신한다."""
    u, g = _perm_guard(request, "monthly", "view")
    if g:
        return g
    conn = db.connect()
    months = _report_months(conn)
    valid = {"%04d-%02d" % (y, mo): (y, mo) for (y, mo) in months}
    if ym not in valid:
        y0, m0 = months[0]
        ym = "%04d-%02d" % (y0, m0)
    y, mth = valid[ym]
    m = calc.Masters(conn)
    if rebuild == "1":
        data = report_monthly.rebuild(conn, m, y, mth, u["name"])
    else:
        data = report_monthly.get_or_build(conn, m, y, mth, u["name"])
    conn.close()
    return tpl.TemplateResponse(request, "monthly_view.html",
                                {"user": u, "d": data, "ym": ym})


@app.post("/report/monthly/rebuild")
def monthly_rebuild(request: Request, ym: str = Form(...)):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "monthly", "edit")):
        return RedirectResponse("/report/monthly", status_code=303)
    conn = db.connect()
    months = _report_months(conn)
    valid = {"%04d-%02d" % (y, mo): (y, mo) for (y, mo) in months}
    if ym in valid:
        y, mth = valid[ym]
        report_monthly.rebuild(conn, calc.Masters(conn), y, mth, u["name"])
    conn.close()
    return RedirectResponse(f"/report/monthly?ym={ym}&msg=재계산 완료", status_code=303)


@app.post("/report/monthly/save")
async def monthly_close_save(request: Request):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "monthly", "edit")):
        return RedirectResponse("/report/monthly", status_code=303)
    form = await request.form()
    ym = (form.get("ym") or "").strip()
    if len(ym) != 7:
        return RedirectResponse("/report/monthly?err=마감월이 올바르지 않습니다", status_code=303)
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = db.connect()
    for key, _name, _hint in REPORT_SECTIONS:
        if key not in form:
            continue
        conn.execute(
            "INSERT INTO report_text(ym,section,content,updated_at,updated_by) VALUES(?,?,?,?,?) "
            "ON CONFLICT(ym,section) DO UPDATE SET content=excluded.content, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (ym, key, (form.get(key) or "").strip(), now, u["name"]))
    conn.commit()
    # 서술 내용은 발표용 캐시(report_cache)에 함께 구워져 들어간다. 저장만 하고 캐시를 그대로
    # 두면 보고서에는 옛 내용이 계속 보이므로, 저장 즉시 다시 계산해 둔다(약 0.3초).
    # 사용자가 '재계산'을 따로 누르지 않아도 되도록 한 것이다(2026-07-30).
    valid = {"%04d-%02d" % (y, mo): (y, mo) for (y, mo) in _report_months(conn)}
    if ym in valid:
        y, mth = valid[ym]
        report_monthly.rebuild(conn, calc.Masters(conn), y, mth, u["name"])
    conn.close()
    return RedirectResponse(f"/report/monthly?ym={ym}&msg=저장됨 (보고서에 바로 반영됨)",
                            status_code=303)


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


# ── 대표품명(품명 그룹) 관리 ────────────────────────────
# 월마감 p15 품명별 파레토가 TM-NO 단위로 쪼개져 의미가 없던 문제를 품명 단위 집계로 바꾸면서 신설.
# 자동 규칙(괄호·ASS'Y·구두점 제거)으로 대부분 묶이고, 규칙으로 못 잡는 것(오타 등)만 여기서 지정한다.
@app.get("/admin/products/alias", response_class=HTMLResponse)
def product_alias_list(request: Request, part: str = "VMS PART", q: str = "",
                       msg: str = "", err: str = ""):
    u, g = _perm_guard(request, "products", "view")
    if g:
        return g
    if part not in ("VMS PART", "TM PART"):
        part = "VMS PART"
    conn = db.connect()
    alias = calc.load_alias(conn)
    args = [part]
    wsql = "WHERE part=? AND name<>''"
    if q:
        wsql += " AND UPPER(name) LIKE ?"
        args.append("%" + q.upper() + "%")
    rows = []
    for r in conn.execute(f"SELECT name, COUNT(*) n FROM product {wsql} "
                          f"GROUP BY name ORDER BY name", args):
        rows.append({"name": r["name"], "cnt": r["n"],
                     "auto": calc.auto_group_name(r["name"]),
                     "group": calc.group_of(alias, part, r["name"]),
                     "saved": alias["exact"].get((part, r["name"]), "")})
    # 대표품명별로 몇 개 품명이 묶였는지 — 묶임 결과를 눈으로 확인하는 용도
    merged = {}
    for r in rows:
        merged.setdefault(r["group"], []).append(r["name"])
    groups = sorted(((g, ns) for g, ns in merged.items() if len(ns) > 1),
                    key=lambda kv: -len(kv[1]))
    # load_alias가 `*`를 떼어 보관하므로 화면에는 패턴 형태로 되돌려 보여준다.
    prules = [{"name": n + "*", "group": gp} for (p, n), gp in alias["prefix"] if p == part]
    conn.close()
    return render(request, "product_alias.html", u, active="products",
                  heading="대표품명 (품명 그룹) 관리", crumb="관리 · 제품 마스터",
                  pending=pending_count(), part=part, q=q, rows=rows, groups=groups,
                  prules=prules, msg=msg, err=err,
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "products", "edit")))


@app.post("/admin/products/alias")
async def product_alias_save(request: Request):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "products", "edit")):
        return RedirectResponse("/admin/products/alias", status_code=303)
    form = await request.form()
    part = form.get("part") or "VMS PART"
    q = (form.get("q") or "").strip()
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = db.connect()
    n = 0
    for key in form.keys():
        if not key.startswith("g_"):
            continue
        name = key[2:]
        val = (form.get(key) or "").strip()
        if val:
            conn.execute(
                "INSERT INTO product_alias(part,name,group_name,updated_at,updated_by) "
                "VALUES(?,?,?,?,?) ON CONFLICT(part,name) DO UPDATE SET "
                "group_name=excluded.group_name, updated_at=excluded.updated_at, "
                "updated_by=excluded.updated_by", (part, name, val, now, u["name"]))
        else:
            # 빈칸 = 지정 해제 → 자동 규칙으로 돌아간다
            conn.execute("DELETE FROM product_alias WHERE part=? AND name=?", (part, name))
        n += 1
    conn.commit()
    conn.close()
    # 대표품명이 바뀌면 월마감 파레토가 달라지므로 캐시를 버려 다음 조회 때 다시 계산되게 한다.
    _drop_report_cache()
    qs = f"?part={part}&msg={n}건 반영 (보고서는 다시 계산됩니다)"
    if q:
        qs += f"&q={q}"
    return RedirectResponse("/admin/products/alias" + qs, status_code=303)


def _drop_report_cache():
    conn = db.connect()
    conn.execute("DELETE FROM report_cache")
    conn.commit()
    conn.close()


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
    scrap_root = scan.scrap_root(conn)
    sources = []
    for key, label, sub in scan.SOURCES:
        if key == "scrap":
            # 폐기불량은 공유 루트가 아니라 별도 로컬 경로를 쓰고, 그 폴더 안 다른 파일과
            # 섞여 있어도 scrap_data.db 라는 이름의 파일 존재 여부만 본다(2026-07-31).
            folder = scrap_root
            target = os.path.join(scrap_root, scan.SCRAP_FILENAME)
            exists = os.path.isfile(target)
            cnt = 1 if exists else 0
            sub_display = scan.SCRAP_FILENAME
        else:
            folder = os.path.join(root, sub)
            exists = os.path.isdir(folder)
            cnt = 0
            if exists:
                for _r, _d, names in os.walk(folder):
                    cnt += sum(1 for n in names if not n.startswith("~$"))
            sub_display = sub
        sources.append({"key": key, "label": label, "sub": sub_display, "exists": exists, "count": cnt})
    logs = [dict(r) for r in conn.execute(
        "SELECT ts,kind,ok,note FROM upload_log WHERE kind LIKE 'scan:%' ORDER BY id DESC LIMIT 20")]
    last_scan = db.get_setting(conn, "last_scan_at", "")
    init_done = db.get_setting(conn, "init_data_done", "") == "1"
    conn.close()
    return render(request, "scan.html", u, active="scan", heading="폴더 반영",
                  crumb="관리", pending=pending_count(), root=root,
                  root_ok=os.path.isdir(root), scrap_root=scrap_root,
                  scrap_root_ok=os.path.isfile(os.path.join(scrap_root, scan.SCRAP_FILENAME)),
                  sources=sources, results=results,
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


@app.post("/admin/scan/scrap-root")
async def scan_set_scrap_root(request: Request, scrap_root: str = Form("")):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "scan", "edit")):
        return RedirectResponse("/admin/scan", status_code=303)
    conn = db.connect()
    db.set_setting(conn, scan.SETTING_SCRAP_ROOT, scrap_root.strip())
    conn.close()
    return RedirectResponse("/admin/scan?msg=폐기불량 경로 저장됨", status_code=303)


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
def defect_types_list(request: Request, edit: int = 0, msg: str = "", err: str = "",
                      new_name: str = "", new_part: str = "", new_kind: str = ""):
    u, g = _perm_guard(request, "defect_types", "view")
    if g:
        return g
    conn = db.connect()
    pend_types = ingest.pending_defect_types(conn)
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
    # 알람 목록에서 '등록'을 누르면 파트·구분·불량명이 채워진 상태로 추가 폼이 열린다
    prefill = ({"name": new_name, "part": new_part, "kind": new_kind}
               if new_name and not edit_row else None)
    return render(request, "defect_types.html", u, active="defect_types", heading="불량유형 마스터 관리",
                  crumb="관리", pending=pending_count(), rows=rows, edit_row=edit_row,
                  pend_types=pend_types, prefill=prefill,
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


@app.post("/admin/defect-types/pending/dismiss")
def defect_types_dismiss(request: Request, part: str = Form(...), kind: str = Form(...),
                         name: str = Form(...)):
    """알람 목록에서 '무시' — 마스터에 넣지 않고 알람만 끈다(집계에는 계속 반영되지 않음)."""
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "defect_types", "edit")):
        return RedirectResponse("/admin/defect-types", status_code=303)
    conn = db.connect()
    conn.execute("UPDATE defect_type_pending SET dismissed=1 WHERE part=? AND kind=? AND name=?",
                 (part, kind, name))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/defect-types?msg=알람에서 제외했습니다", status_code=303)


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
              "part_only": k in PART_ONLY_KPIS,
              # warranty·incident는 FY 목표가 연간 합계라 월별 비교에는 12로 균등배분한 값을 쓴다.
              # 입력은 연간 목표 하나만 받고, 균등배분값은 화면에 계산해서 보여준다.
              "fy_total": k in calc.FY_TOTAL_KPIS,
              "per_month": (round(rows[(k, 0)] / 12, 1)
                            if k in calc.FY_TOTAL_KPIS and rows.get((k, 0)) else None)}
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
    cols = ("id,d,part,customer,location,tm_no,product_name,content,defect_qty,cause,action,is_official")
    rows = [dict(r) for r in conn.execute(
        f"SELECT {cols} FROM incident ORDER BY d DESC,id DESC LIMIT 200")]
    # 첨부파일을 이슈별로 묶어 목록에 개수/링크를 보여준다
    files = defaultdict(list)
    for f in conn.execute("SELECT id,incident_id,kind,orig_name,size FROM incident_file ORDER BY id"):
        files[f["incident_id"]].append(dict(f))
    for r in rows:
        r["files"] = files.get(r["id"], [])
    edit_row = None
    if edit:
        r = conn.execute(f"SELECT {cols} FROM incident WHERE id=?", (edit,)).fetchone()
        if r:
            edit_row = dict(r)
            edit_row["files"] = files.get(edit_row["id"], [])
    # 고객명은 고객사 마스터에서 고른다(표기 통일). 목록에 없으면 마스터에 먼저 추가.
    cust_opts = [dict(r) for r in conn.execute(
        "SELECT name, short_name, part FROM customer WHERE active=1 ORDER BY part, name")]
    conn.close()
    return render(request, "incident.html", u, active="incident", heading="Customer Incident 관리",
                  crumb="데이터 입력", pending=pending_count(), rows=rows, edit_row=edit_row,
                  cust_opts=cust_opts,
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
    location = (form.get("location") or "").strip()
    tm_no = calc.base_tmno((form.get("tm_no") or "").strip())
    product_name = (form.get("product_name") or "").strip()
    content = (form.get("content") or "").strip()
    cause = (form.get("cause") or "").strip()
    action = (form.get("action") or "").strip()
    try:
        defect_qty = int((form.get("defect_qty") or "0").replace(",", "").strip() or 0)
    except ValueError:
        defect_qty = 0
    is_official = 1 if form.get("is_official") else 0
    if not d:
        eq = f"&edit={orig_id}" if orig_id else ""
        return RedirectResponse(f"/input/incident?err=날짜는 필수입니다{eq}", status_code=303)
    conn = db.connect()
    if orig_id:
        conn.execute(
            "UPDATE incident SET d=?,part=?,customer=?,location=?,tm_no=?,product_name=?,content=?,"
            "defect_qty=?,cause=?,action=?,is_official=? WHERE id=?",
            (d, part, customer, location, tm_no, product_name, content,
             defect_qty, cause, action, is_official, orig_id))
        iid = int(orig_id)
        msg = "수정됨"
    else:
        cur = conn.execute(
            "INSERT INTO incident(d,part,customer,location,tm_no,product_name,content,"
            "defect_qty,cause,action,is_official,reg_user) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (d, part, customer, location, tm_no, product_name, content,
             defect_qty, cause, action, is_official, u["name"]))
        iid = cur.lastrowid
        msg = "등록됨"
    # 첨부파일(불량사진 photo / 세부자료 doc)은 저장 후 그 이슈에 붙인다
    n_up = 0
    for field, kind in (("photo", "photo"), ("doc", "doc")):
        for up in form.getlist(field):
            if getattr(up, "filename", ""):
                _save_incident_file(conn, iid, kind, up, u["name"])
                n_up += 1
    conn.commit()
    conn.close()
    if n_up:
        msg += f" (첨부 {n_up}건)"
    return RedirectResponse(f"/input/incident?msg={msg}", status_code=303)


# ── 고객 품질이슈 첨부파일 ───────────────────────────────
# static이 아닌 uploads/ 에 두고 **로그인·권한 확인 후에만** 내려준다(세부자료가 섞여 있으므로).
UPLOAD_ROOT = os.path.abspath(os.path.join(BASE, "..", "uploads", "incident"))
MAX_UPLOAD_MB = 20
# 동영상은 사진보다 훨씬 커서 20MB로는 몇 초짜리도 안 들어간다(휴대폰 촬영분은 보통 수십 MB).
# 그래서 동영상 첨부만 한도를 따로 둔다(2026-08-10 추가).
MAX_VIDEO_MB = 150
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")
# 브라우저가 그대로 재생할 수 있는 형식만 '재생'으로 취급한다. avi·wmv·mkv는 파일로만 보관하고
# 재생은 안 되므로(코덱 문제) 화면에서 변환 안내를 띄운다.
VIDEO_EXTS = (".mp4", ".webm", ".ogg", ".ogv", ".m4v", ".mov")
VIDEO_PLAYABLE_EXTS = (".mp4", ".webm", ".ogg", ".ogv", ".m4v")


def _safe_name(name):
    """경로 조작·이상문자 제거. 확장자는 살린다."""
    base = os.path.basename(str(name or "")).replace("\\", "_").replace("/", "_")
    keep = [c for c in base if c.isalnum() or c in " ._-()[]가-힣" or ord(c) > 127]
    out = "".join(keep).strip() or "file"
    return out[:120]


def _save_incident_file(conn, incident_id, kind, upload, user_name):
    data = upload.file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        return None
    os.makedirs(UPLOAD_ROOT, exist_ok=True)
    orig = _safe_name(upload.filename)
    stored = f"{incident_id}_{secrets.token_hex(6)}_{orig}"
    with open(os.path.join(UPLOAD_ROOT, stored), "wb") as f:
        f.write(data)
    conn.execute(
        "INSERT INTO incident_file(incident_id,kind,orig_name,stored_name,size,uploaded_at,uploaded_by) "
        "VALUES(?,?,?,?,?,?,?)",
        (incident_id, kind, orig, stored, len(data),
         _dt.datetime.now().strftime("%Y-%m-%d %H:%M"), user_name))
    return stored


@app.get("/input/incident/file/{fid}")
def incident_file_get(request: Request, fid: int):
    """첨부파일 다운로드/표시. 보고서에서 <img src>로도 쓴다."""
    u, g = _perm_guard(request, "incident", "view")
    if g:
        return g
    conn = db.connect()
    r = conn.execute("SELECT orig_name, stored_name FROM incident_file WHERE id=?", (fid,)).fetchone()
    conn.close()
    if not r:
        return RedirectResponse("/input/incident?err=파일을 찾을 수 없습니다", status_code=303)
    path = os.path.join(UPLOAD_ROOT, r["stored_name"])
    if not os.path.isfile(path):
        return RedirectResponse("/input/incident?err=파일이 서버에 없습니다", status_code=303)
    # inline: 브라우저가 PDF·이미지 등을 다운로드 없이 새 탭에서 바로 열어 보여준다
    # (2026-07-31 — 발표 중 다운로드 기다리지 않고 바로 보이도록).
    return FileResponse(path, filename=r["orig_name"], content_disposition_type="inline")


@app.post("/input/incident/file/{fid}/delete")
def incident_file_delete(request: Request, fid: int):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "incident", "edit")):
        return RedirectResponse("/input/incident", status_code=303)
    conn = db.connect()
    r = conn.execute("SELECT incident_id, stored_name FROM incident_file WHERE id=?", (fid,)).fetchone()
    if r:
        try:
            os.remove(os.path.join(UPLOAD_ROOT, r["stored_name"]))
        except OSError:
            pass                        # 파일이 이미 없어도 DB 행은 지운다
        conn.execute("DELETE FROM incident_file WHERE id=?", (fid,))
        conn.commit()
        iid = r["incident_id"]
    else:
        iid = 0
    conn.close()
    return RedirectResponse(f"/input/incident?edit={iid}&msg=첨부 삭제됨", status_code=303)


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
    # 첨부파일도 함께 정리(디스크에 고아 파일이 남지 않게)
    for f in conn.execute("SELECT stored_name FROM incident_file WHERE incident_id=?", (iid,)):
        try:
            os.remove(os.path.join(UPLOAD_ROOT, f["stored_name"]))
        except OSError:
            pass
    conn.execute("DELETE FROM incident_file WHERE incident_id=?", (iid,))
    conn.execute("DELETE FROM incident WHERE id=?", (iid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/input/incident?msg=삭제됨", status_code=303)


# ── 내부품질 Issue 관리 (건별 직접 등록, Customer Incident와 동일 방식) ─────
@app.get("/input/internal-issue", response_class=HTMLResponse)
def internal_issue_page(request: Request, edit: int = 0, msg: str = "", err: str = ""):
    u, g = _perm_guard(request, "internal_issue", "view")
    if g:
        return g
    conn = db.connect()
    cols = ("id,d,part,process,process_etc,location,tm_no,product_name,content,defect_qty,cause,action,is_official")
    rows = [dict(r) for r in conn.execute(
        f"SELECT {cols} FROM internal_issue ORDER BY d DESC,id DESC LIMIT 200")]
    files = defaultdict(list)
    for f in conn.execute("SELECT id,internal_issue_id,kind,orig_name,size FROM internal_issue_file ORDER BY id"):
        files[f["internal_issue_id"]].append(dict(f))
    for r in rows:
        r["files"] = files.get(r["id"], [])
    edit_row = None
    if edit:
        r = conn.execute(f"SELECT {cols} FROM internal_issue WHERE id=?", (edit,)).fetchone()
        if r:
            edit_row = dict(r)
            edit_row["files"] = files.get(edit_row["id"], [])
    conn.close()
    return render(request, "internal_issue.html", u, active="internal_issue", heading="내부품질 Issue 관리",
                  crumb="데이터 입력", pending=pending_count(), rows=rows, edit_row=edit_row,
                  processes=db.AGG_PROCESSES,
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "internal_issue", "edit")), msg=msg, err=err)


@app.post("/input/internal-issue/save")
async def internal_issue_save(request: Request):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "internal_issue", "edit")):
        return RedirectResponse("/input/internal-issue", status_code=303)
    form = await request.form()
    orig_id = (form.get("orig_id") or "").strip()
    d = (form.get("d") or "").strip()
    part = form.get("part") or "VMS PART"
    process = (form.get("process") or "").strip()
    process_etc = (form.get("process_etc") or "").strip() if process == "기타" else ""
    location = (form.get("location") or "").strip()
    tm_no = calc.base_tmno((form.get("tm_no") or "").strip())
    product_name = (form.get("product_name") or "").strip()
    content = (form.get("content") or "").strip()
    cause = (form.get("cause") or "").strip()
    action = (form.get("action") or "").strip()
    try:
        defect_qty = int((form.get("defect_qty") or "0").replace(",", "").strip() or 0)
    except ValueError:
        defect_qty = 0
    is_official = 1 if form.get("is_official") else 0
    if not d:
        eq = f"&edit={orig_id}" if orig_id else ""
        return RedirectResponse(f"/input/internal-issue?err=날짜는 필수입니다{eq}", status_code=303)
    conn = db.connect()
    if orig_id:
        conn.execute(
            "UPDATE internal_issue SET d=?,part=?,process=?,process_etc=?,location=?,tm_no=?,product_name=?,content=?,"
            "defect_qty=?,cause=?,action=?,is_official=? WHERE id=?",
            (d, part, process, process_etc, location, tm_no, product_name, content,
             defect_qty, cause, action, is_official, orig_id))
        iid = int(orig_id)
        msg = "수정됨"
    else:
        cur = conn.execute(
            "INSERT INTO internal_issue(d,part,process,process_etc,location,tm_no,product_name,content,"
            "defect_qty,cause,action,is_official,reg_user) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d, part, process, process_etc, location, tm_no, product_name, content,
             defect_qty, cause, action, is_official, u["name"]))
        iid = cur.lastrowid
        msg = "등록됨"
    n_up = 0
    for field, kind in (("photo", "photo"), ("doc", "doc")):
        for up in form.getlist(field):
            if getattr(up, "filename", ""):
                _save_internal_issue_file(conn, iid, kind, up, u["name"])
                n_up += 1
    conn.commit()
    conn.close()
    if n_up:
        msg += f" (첨부 {n_up}건)"
    return RedirectResponse(f"/input/internal-issue?msg={msg}", status_code=303)


# ── 내부품질 issue 첨부파일 ───────────────────────────────
INTERNAL_ISSUE_UPLOAD_ROOT = os.path.abspath(os.path.join(BASE, "..", "uploads", "internal_issue"))


def _save_internal_issue_file(conn, internal_issue_id, kind, upload, user_name):
    data = upload.file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        return None
    os.makedirs(INTERNAL_ISSUE_UPLOAD_ROOT, exist_ok=True)
    orig = _safe_name(upload.filename)
    stored = f"{internal_issue_id}_{secrets.token_hex(6)}_{orig}"
    with open(os.path.join(INTERNAL_ISSUE_UPLOAD_ROOT, stored), "wb") as f:
        f.write(data)
    conn.execute(
        "INSERT INTO internal_issue_file(internal_issue_id,kind,orig_name,stored_name,size,uploaded_at,uploaded_by) "
        "VALUES(?,?,?,?,?,?,?)",
        (internal_issue_id, kind, orig, stored, len(data),
         _dt.datetime.now().strftime("%Y-%m-%d %H:%M"), user_name))
    return stored


@app.get("/input/internal-issue/file/{fid}")
def internal_issue_file_get(request: Request, fid: int):
    u, g = _perm_guard(request, "internal_issue", "view")
    if g:
        return g
    conn = db.connect()
    r = conn.execute("SELECT orig_name, stored_name FROM internal_issue_file WHERE id=?", (fid,)).fetchone()
    conn.close()
    if not r:
        return RedirectResponse("/input/internal-issue?err=파일을 찾을 수 없습니다", status_code=303)
    path = os.path.join(INTERNAL_ISSUE_UPLOAD_ROOT, r["stored_name"])
    if not os.path.isfile(path):
        return RedirectResponse("/input/internal-issue?err=파일이 서버에 없습니다", status_code=303)
    # inline: incident.html과 동일하게 다운로드 없이 새 탭에서 바로 보여준다(2026-07-31).
    return FileResponse(path, filename=r["orig_name"], content_disposition_type="inline")


# ── PDF 뷰어 (여러 장짜리 첨부를 한 장씩 화면 꽉 차게 넘겨보기) ──────────
# 브라우저 기본 PDF 뷰어는 계속 스크롤하는 방식이라, 월마감 보고서 슬라이드 뷰어와 같은
# "한 장씩 전체화면 전환" 느낌을 내려고 PDF.js를 내장했다(2026-07-31, 인터넷 안 되는
# 서버에서도 동작하도록 static/pdfjs/에 라이브러리 파일을 직접 받아 넣어둠).
_PDF_VIEW_SRC_PREFIXES = ("/input/incident/file/", "/input/internal-issue/file/")


@app.get("/view/pdf", response_class=HTMLResponse)
def pdf_view(request: Request, src: str = "", name: str = "문서"):
    u = current_user(request)
    if u is None:
        return RedirectResponse("/login", status_code=303)
    if not src.startswith(_PDF_VIEW_SRC_PREFIXES) or not src.rsplit("/", 1)[-1].isdigit():
        return RedirectResponse("/", status_code=303)
    return tpl.TemplateResponse(request, "pdf_view.html", {"src": src, "name": name})


@app.post("/input/internal-issue/file/{fid}/delete")
def internal_issue_file_delete(request: Request, fid: int):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "internal_issue", "edit")):
        return RedirectResponse("/input/internal-issue", status_code=303)
    conn = db.connect()
    r = conn.execute("SELECT internal_issue_id, stored_name FROM internal_issue_file WHERE id=?", (fid,)).fetchone()
    if r:
        try:
            os.remove(os.path.join(INTERNAL_ISSUE_UPLOAD_ROOT, r["stored_name"]))
        except OSError:
            pass
        conn.execute("DELETE FROM internal_issue_file WHERE id=?", (fid,))
        conn.commit()
        iid = r["internal_issue_id"]
    else:
        iid = 0
    conn.close()
    return RedirectResponse(f"/input/internal-issue?edit={iid}&msg=첨부 삭제됨", status_code=303)


@app.post("/input/internal-issue/{iid}/toggle-official")
def internal_issue_toggle_official(request: Request, iid: int):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "internal_issue", "edit")):
        return RedirectResponse("/input/internal-issue", status_code=303)
    conn = db.connect()
    conn.execute("UPDATE internal_issue SET is_official=1-is_official WHERE id=?", (iid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/input/internal-issue", status_code=303)


@app.post("/input/internal-issue/{iid}/delete")
def internal_issue_delete(request: Request, iid: int):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "internal_issue", "edit")):
        return RedirectResponse("/input/internal-issue", status_code=303)
    conn = db.connect()
    for f in conn.execute("SELECT stored_name FROM internal_issue_file WHERE internal_issue_id=?", (iid,)):
        try:
            os.remove(os.path.join(INTERNAL_ISSUE_UPLOAD_ROOT, f["stored_name"]))
        except OSError:
            pass
    conn.execute("DELETE FROM internal_issue_file WHERE internal_issue_id=?", (iid,))
    conn.execute("DELETE FROM internal_issue WHERE id=?", (iid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/input/internal-issue?msg=삭제됨", status_code=303)


# ── 개선대책서 (현장품질회의) ──────────────────────────────
# 사내에서 쓰던 1장짜리 대책서 양식을 그대로 담는다. 입력은 세로로 흐르는 폼(쓰기 편하게),
# 보기는 원래 양식 배치(발표용)로 화면을 나눴다(2026-08-10 확정).
CAPA_UPLOAD_ROOT = os.path.abspath(os.path.join(BASE, "..", "uploads", "capa"))


def _capa_num(v, default=0):
    try:
        return int(str(v or "").replace(",", "").strip() or default)
    except ValueError:
        return default


def _capa_load(conn, cid):
    """대책서 1건 + 하위(대책·표준·수평전개·첨부) 전부를 dict로 묶어 반환."""
    r = conn.execute("SELECT * FROM capa WHERE id=?", (cid,)).fetchone()
    if not r:
        return None
    row = dict(r)
    row["tms"] = [dict(x) for x in conn.execute(
        "SELECT * FROM capa_tm WHERE capa_id=? ORDER BY id", (cid,))]
    # 품명 그룹(대표품명)도 같이 계산해 둔다 — "Pulley 계열에 무슨 일이 있었나"를 보기 위함.
    alias = calc.load_alias(conn)
    groups = []
    for t in row["tms"]:
        g = calc.group_of(alias, row["part"], t["product_name"]) if t["product_name"] else ""
        t["group_name"] = g
        if g and g not in groups:
            groups.append(g)
    row["groups"] = groups
    row["actions"] = [dict(x) for x in conn.execute(
        "SELECT * FROM capa_action WHERE capa_id=? ORDER BY side,seq,id", (cid,))]
    std = {x["name"]: dict(x) for x in conn.execute(
        "SELECT * FROM capa_std WHERE capa_id=?", (cid,))}
    # 표준 5종은 항상 같은 순서로 보여준다(미입력 건도 빈 줄로 나와야 체크가 빠진 게 보인다).
    row["stds"] = [std.get(n, {"name": n, "revised": 0, "rev_date": ""}) for n in db.CAPA_STD_NAMES]
    row["spreads"] = [dict(x) for x in conn.execute(
        "SELECT * FROM capa_spread WHERE capa_id=? ORDER BY id", (cid,))]
    files = defaultdict(list)
    for f in conn.execute("SELECT * FROM capa_file WHERE capa_id=? ORDER BY id", (cid,)):
        files[f["section"]].append(dict(f))
    row["files"] = files
    return row


@app.get("/input/capa", response_class=HTMLResponse)
def capa_page(request: Request, edit: int = 0, msg: str = "", err: str = ""):
    u, g = _perm_guard(request, "capa", "view")
    if g:
        return g
    conn = db.connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT id,d,title,kind,part,cause_process,found_process,tm_no,product_name,defect_type,"
        "equipment,dept,presenter,status,lot_qty,defect_qty FROM capa ORDER BY d DESC,id DESC LIMIT 200")]
    # 목록에 '대책 진행률'을 같이 보여줘야 어느 건이 밀리고 있는지 한눈에 보인다.
    prog = {r["capa_id"]: (r["n"], r["done_n"]) for r in conn.execute(
        "SELECT capa_id, COUNT(*) n, SUM(done) done_n FROM capa_action GROUP BY capa_id")}
    today = _dt.date.today().isoformat()
    overdue = {r["capa_id"] for r in conn.execute(
        "SELECT DISTINCT capa_id FROM capa_action WHERE done=0 AND due<>'' AND due<?", (today,))}
    for r in rows:
        n, dn = prog.get(r["id"], (0, 0))
        r["act_n"], r["act_done"] = n, dn or 0
        r["overdue"] = r["id"] in overdue
    edit_row = _capa_load(conn, edit) if edit else None
    # 설비명은 마스터를 따로 두지 않고 이미 입력된 값을 자동완성으로 제안해 표기를 통일한다.
    equips = [r["equipment"] for r in conn.execute(
        "SELECT DISTINCT equipment FROM capa WHERE equipment<>'' ORDER BY equipment")]
    dtypes = [r["name"] for r in conn.execute(
        "SELECT DISTINCT name FROM defect_type ORDER BY name")]
    # 발견공정은 자유 입력이라(협력업체·고객 등) 기존 입력값을 자동완성으로 제안해 표기를 통일한다.
    found_opts = [r["found_process"] for r in conn.execute(
        "SELECT DISTINCT found_process FROM capa WHERE found_process<>'' ORDER BY found_process")]
    for p in db.AGG_PROCESSES:
        if p not in found_opts:
            found_opts.append(p)
    conn.close()
    return render(request, "capa.html", u, active="capa", heading="개선대책서 (현장품질회의)",
                  crumb="데이터 입력", pending=pending_count(), rows=rows, edit_row=edit_row,
                  processes=db.AGG_PROCESSES, kinds=db.CAPA_KINDS, lot_actions=db.CAPA_LOT_ACTIONS,
                  std_names=db.CAPA_STD_NAMES, m4=db.CAPA_4M, applied_opts=db.CAPA_APPLIED,
                  equips=equips, dtypes=dtypes, found_opts=found_opts, today=today,
                  can_edit=(u["role"] == "admin" or has_perm(u["role"], "capa", "edit")), msg=msg, err=err)


@app.post("/input/capa/save")
async def capa_save(request: Request):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "capa", "edit")):
        return RedirectResponse("/input/capa", status_code=303)
    form = await request.form()
    orig_id = (form.get("orig_id") or "").strip()
    d = (form.get("d") or "").strip()
    if not d:
        eq = f"&edit={orig_id}" if orig_id else ""
        return RedirectResponse(f"/input/capa?err=작성일은 필수입니다{eq}", status_code=303)
    kind = (form.get("kind") or "product").strip()
    # 대상 품번은 여러 개 받는다(대책서 1건이 여러 품번에 걸리는 일이 실제로 많다).
    # capa.tm_no에는 첫 줄(대표)만 복사해 목록·색인에 쓴다.
    tm_list = []
    if kind == "product":
        tm_nos, tm_names = form.getlist("tm_no"), form.getlist("tm_name")
        tm_qtys, tm_memos = form.getlist("tm_qty"), form.getlist("tm_memo")
        for i, t in enumerate(tm_nos):
            t = calc.base_tmno((t or "").strip())
            nm = (tm_names[i].strip() if i < len(tm_names) else "")
            if not t and not nm:
                continue
            tm_list.append((t, nm, _capa_num(tm_qtys[i] if i < len(tm_qtys) else 0),
                            tm_memos[i].strip() if i < len(tm_memos) else ""))
    tm_no = tm_list[0][0] if tm_list else ""
    vals = dict(
        d=d, title=(form.get("title") or "").strip(), kind=kind,
        part=form.get("part") or "VMS PART",
        # 원인공정은 체크박스 복수 선택 → '성형, 소결' 형태로 이어 저장한다.
        cause_process=", ".join(x for x in form.getlist("cause_process") if x.strip()),
        found_process=(form.get("found_process") or "").strip(),
        tm_no=tm_no,
        product_name=(tm_list[0][1] if tm_list else ""),
        defect_type=(form.get("defect_type") or "").strip() if kind == "product" else "",
        equipment=(form.get("equipment") or "").strip(),
        dept=(form.get("dept") or "").strip(), writer=(form.get("writer") or "").strip(),
        presenter=(form.get("presenter") or "").strip(),
        approver=(form.get("approver") or "").strip(),
        approved_at=(form.get("approved_at") or "").strip(),
        symptom=(form.get("symptom") or "").strip(),
        occur_date=(form.get("occur_date") or "").strip(),
        occur_ongoing=1 if form.get("occur_ongoing") else 0,
        lot_qty=_capa_num(form.get("lot_qty")), defect_qty=_capa_num(form.get("defect_qty")),
        defect_qty_note=(form.get("defect_qty_note") or "").strip(),
        rate_unit=(form.get("rate_unit") or "%").strip(),
        lot_action=(form.get("lot_action") or "").strip(),
        lot_action_etc=(form.get("lot_action_etc") or "").strip(),
        interim=(form.get("interim") or "").strip(),
        cause_occur=(form.get("cause_occur") or "").strip(),
        cause_flow=(form.get("cause_flow") or "").strip(),
        cause_4m=(form.get("cause_4m") or "").strip(),
        status=(form.get("status") or "open").strip(),
        updated_at=_dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    conn = db.connect()
    if orig_id:
        sets = ",".join(f"{k}=?" for k in vals)
        conn.execute(f"UPDATE capa SET {sets} WHERE id=?", (*vals.values(), orig_id))
        cid = int(orig_id)
        msg = "수정됨"
    else:
        cols = ",".join(vals) + ",reg_user"
        qs = ",".join("?" * (len(vals) + 1))
        cur = conn.execute(f"INSERT INTO capa({cols}) VALUES({qs})", (*vals.values(), u["name"]))
        cid = cur.lastrowid
        msg = "등록됨"

    conn.execute("DELETE FROM capa_tm WHERE capa_id=?", (cid,))
    for t, nm, q, mm in tm_list:
        conn.execute("INSERT INTO capa_tm(capa_id,tm_no,product_name,defect_qty,memo) VALUES(?,?,?,?,?)",
                     (cid, t, nm, q, mm))

    # 하위 3종(대책·표준·수평전개)은 행 수가 매번 달라져 부분 수정이 까다롭다.
    # 전부 지우고 다시 넣는 편이 단순하고, 한 건당 행이 수십 개 수준이라 성능도 문제없다.
    conn.execute("DELETE FROM capa_action WHERE capa_id=?", (cid,))
    for side in ("occur", "flow"):
        for i, txt in enumerate(form.getlist(f"act_{side}")):
            txt = (txt or "").strip()
            if not txt:
                continue
            dues = form.getlist(f"act_{side}_due")
            dones = form.getlist(f"act_{side}_done")
            conn.execute(
                "INSERT INTO capa_action(capa_id,side,seq,content,due,done,done_at) VALUES(?,?,?,?,?,?,?)",
                (cid, side, i, txt, dues[i] if i < len(dues) else "",
                 1 if (i < len(dones) and dones[i] == "1") else 0, ""))

    conn.execute("DELETE FROM capa_std WHERE capa_id=?", (cid,))
    for name in db.CAPA_STD_NAMES:
        revised = 1 if (form.get(f"std_{name}") == "1") else 0
        conn.execute("INSERT INTO capa_std(capa_id,name,revised,rev_date) VALUES(?,?,?,?)",
                     (cid, name, revised, (form.get(f"stddate_{name}") or "").strip() if revised else ""))

    conn.execute("DELETE FROM capa_spread WHERE capa_id=?", (cid,))
    targets = form.getlist("sp_target")
    sp_tm, sp_ap, sp_pd, sp_mm = (form.getlist(k) for k in ("sp_tm", "sp_applied", "sp_plan", "sp_memo"))
    for i, t in enumerate(targets):
        t = (t or "").strip()
        if not t and not (sp_tm[i].strip() if i < len(sp_tm) else ""):
            continue
        conn.execute(
            "INSERT INTO capa_spread(capa_id,tm_no,target,applied,plan_date,memo) VALUES(?,?,?,?,?,?)",
            (cid, calc.base_tmno(sp_tm[i].strip()) if i < len(sp_tm) else "", t,
             sp_ap[i] if i < len(sp_ap) else "", sp_pd[i] if i < len(sp_pd) else "",
             sp_mm[i].strip() if i < len(sp_mm) else ""))

    n_up = 0
    for field, section, kindf in (("f_photo", "photo", "photo"), ("f_cause", "cause", "doc"),
                                  ("f_action", "action", "doc"), ("f_etc", "etc", "doc")):
        for up in form.getlist(field):
            if getattr(up, "filename", ""):
                if _save_capa_file(conn, cid, section, kindf, up, u["name"]):
                    n_up += 1
    conn.commit()
    conn.close()
    if n_up:
        msg += f" (첨부 {n_up}건)"
    return RedirectResponse(f"/input/capa?edit={cid}&msg={msg}", status_code=303)


def _save_capa_file(conn, capa_id, section, kind, upload, user_name):
    data = upload.file.read()
    orig = _safe_name(upload.filename)
    low = orig.lower()
    is_video = low.endswith(VIDEO_EXTS)
    limit = MAX_VIDEO_MB if is_video else MAX_UPLOAD_MB
    if len(data) > limit * 1024 * 1024:
        return None
    os.makedirs(CAPA_UPLOAD_ROOT, exist_ok=True)
    # 유첨으로 올렸어도 이미지·동영상이면 보고서에서 그림/플레이어로 보여준다.
    if is_video:
        kind = "video"
    elif kind != "photo" and low.endswith(IMAGE_EXTS):
        kind = "photo"
    stored = f"{capa_id}_{secrets.token_hex(6)}_{orig}"
    with open(os.path.join(CAPA_UPLOAD_ROOT, stored), "wb") as f:
        f.write(data)
    conn.execute(
        "INSERT INTO capa_file(capa_id,section,kind,orig_name,stored_name,size,uploaded_at,uploaded_by) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (capa_id, section, kind, orig, stored, len(data),
         _dt.datetime.now().strftime("%Y-%m-%d %H:%M"), user_name))
    return stored


@app.get("/input/capa/file/{fid}")
def capa_file_get(request: Request, fid: int):
    u, g = _perm_guard(request, "capa", "view")
    if g:
        return g
    conn = db.connect()
    r = conn.execute("SELECT orig_name, stored_name FROM capa_file WHERE id=?", (fid,)).fetchone()
    conn.close()
    if not r:
        return RedirectResponse("/input/capa?err=파일을 찾을 수 없습니다", status_code=303)
    path = os.path.join(CAPA_UPLOAD_ROOT, r["stored_name"])
    if not os.path.isfile(path):
        return RedirectResponse("/input/capa?err=파일이 서버에 없습니다", status_code=303)
    return FileResponse(path, filename=r["orig_name"], content_disposition_type="inline")


@app.post("/input/capa/file/{fid}/delete")
def capa_file_delete(request: Request, fid: int):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "capa", "edit")):
        return RedirectResponse("/input/capa", status_code=303)
    conn = db.connect()
    r = conn.execute("SELECT capa_id, stored_name FROM capa_file WHERE id=?", (fid,)).fetchone()
    if not r:
        conn.close()
        return RedirectResponse("/input/capa?err=파일을 찾을 수 없습니다", status_code=303)
    try:
        os.remove(os.path.join(CAPA_UPLOAD_ROOT, r["stored_name"]))
    except OSError:
        pass
    conn.execute("DELETE FROM capa_file WHERE id=?", (fid,))
    conn.commit()
    cid = r["capa_id"]
    conn.close()
    return RedirectResponse(f"/input/capa?edit={cid}&msg=첨부 삭제됨", status_code=303)


@app.get("/input/capa/{cid}/view", response_class=HTMLResponse)
def capa_view(request: Request, cid: int):
    """발표용 보기 화면 — 사내 대책서 양식(주황 제목바 + 4분할) 배치를 그대로 그린다."""
    u, g = _perm_guard(request, "capa", "view")
    if g:
        return g
    conn = db.connect()
    row = _capa_load(conn, cid)
    conn.close()
    if not row:
        return RedirectResponse("/input/capa?err=대책서를 찾을 수 없습니다", status_code=303)
    if row["lot_qty"]:
        f = row["defect_qty"] / row["lot_qty"]
        row["rate_txt"] = (f"{round(f * 1000000):,} PPM" if row["rate_unit"] == "PPM"
                           else f"{f * 100:.1f}%")
    else:
        row["rate_txt"] = "-"
    return tpl.TemplateResponse(request, "capa_view.html", {
        "r": row, "today": _dt.date.today().isoformat(),
        "kind_ko": dict(db.CAPA_KINDS).get(row["kind"], row["kind"]),
        "part_ko": PART_LABEL.get(row["part"], row["part"])})


@app.post("/input/capa/{cid}/delete")
def capa_delete(request: Request, cid: int):
    u = current_user(request)
    if u is None or not (u["role"] == "admin" or has_perm(u["role"], "capa", "edit")):
        return RedirectResponse("/input/capa", status_code=303)
    conn = db.connect()
    for f in conn.execute("SELECT stored_name FROM capa_file WHERE capa_id=?", (cid,)):
        try:
            os.remove(os.path.join(CAPA_UPLOAD_ROOT, f["stored_name"]))
        except OSError:
            pass
    for t in ("capa_file", "capa_action", "capa_std", "capa_spread", "capa_tm"):
        conn.execute(f"DELETE FROM {t} WHERE capa_id=?", (cid,))
    conn.execute("DELETE FROM capa WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/input/capa?msg=삭제됨", status_code=303)


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
