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

from . import db, calc, ingest

BASE = os.path.dirname(__file__)
app = FastAPI(title="통합품질관리시스템")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
tpl = Jinja2Templates(directory=os.path.join(BASE, "templates"))
tpl.env.filters["cf"] = lambda v: f"{v:,.0f}" if isinstance(v, (int, float)) else v
tpl.env.filters["cf2"] = lambda v: f"{v:,.2f}" if isinstance(v, (int, float)) else v

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


def target_val(conn, fy, part, kpi):
    fy = fy % 100 if fy >= 100 else fy      # 2027 → 27 정규화 (목표는 2자리 FY)
    row = conn.execute("SELECT value FROM target WHERE fy=? AND part=? AND kpi=?", (fy, part, kpi)).fetchone()
    return row["value"] if row else None


def _join(vals):
    return ",".join("" if v is None else (f"{v:g}") for v in vals)


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
        "incident": {"val": cur["incident"], "target": target_val(conn, cur_fy, part, "incident")},
        "warranty": {"val": cur["warranty"], "target": target_val(conn, cur_fy, part, "warranty"),
                     "delta": cur["warranty"] - prev["warranty"]},
        "proc_ppm": {"val": cur["proc_ppm"], "target": target_val(conn, cur_fy, "VMS PART", "proc_ppm"),
                     "delta": cur["proc_ppm"] - prev["proc_ppm"]},
        "set_ppm": {"val": cur["set_ppm"], "target": target_val(conn, cur_fy, "VMS PART", "set_ppm"),
                    "delta": cur["set_ppm"] - prev["set_ppm"]},
        "prod_qty": {"val": cur["prod_qty"]},
        "denom_est": cur["denom_est"],
    }

    charts = {
        "labels": ",".join(labels), "cur": len(months) - 1,
        "fydiv": fydiv if fydiv is not None else "", "fylabels": fy_lbls or "",
        "copq_values": _join([s["copq_pct"] for s in series]),
        "copq_targets": _join(targets("copq")),
    }

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
    conn.close()
    return render(request, "dashboard.html", u, active="dash", heading="대시보드",
                  crumb="개요", pending=pending_count(), part=part, d=data)


# ── 리포트: KPI 현황 ────────────────────────────────────
@app.get("/report/kpi", response_class=HTMLResponse)
def report_kpi(request: Request, part: str = "통합"):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    if part not in ("통합", "VMS PART", "TM PART"):
        part = "통합"
    conn = db.connect()
    m = calc.Masters(conn)
    daily = calc.compute_daily(conn, m)
    cy, cm = latest_month(conn)
    months = calc.trailing_months(cy, cm, 7)
    series = [calc.month_kpi(conn, m, daily, y, mm, part) for (y, mm) in months]
    labels = [f"{mm}월" for (y, mm) in months]
    fys = [calc.fy_of(y, mm) for (y, mm) in months]
    fydiv = next((i for i in range(1, len(fys)) if fys[i] != fys[i - 1]), None)
    fylabels = f"{calc.fy_label(fys[0])},{calc.fy_label(fys[-1])}" if fydiv is not None else ""

    def tgt(kpi, tpart=None):
        return _join([target_val(conn, fy, tpart or part, kpi) for fy in fys])

    def vals(key):
        return _join([s[key] for s in series])

    charts = []
    charts.append(("Scrap Cost", "--p-500", "%", 2, "scrap_cost_pct", "scrap_cost", part))
    charts.append(("Scrap Quantity", "--sec", "%", 2, "scrap_qty_pct", "scrap_qty", part))
    charts.append(("COPQ", "--p-500", "%", 2, "copq_pct", "copq", part))
    charts.append(("Warranty", "--sec", "", 0, "warranty", "warranty", part))
    charts.append(("공정불량율 (ppm)", "--p-500", "", 0, "proc_ppm", "proc_ppm", "VMS PART"))
    charts.append(("셋팅불량율 (ppm)", "--sec", "", 0, "set_ppm", "set_ppm", "VMS PART"))
    chart_ctx = []
    for name, color, unit, dec, vkey, tkey, tpart in charts:
        chart_ctx.append({
            "name": name, "color": color, "unit": unit, "dec": dec,
            "type": "bar" if vkey == "warranty" else "area",
            "cur_val": series[-1][vkey], "vseries": vals(vkey), "targets": tgt(tkey, tpart),
        })
    # COPQ 구성 (당월)
    ym = f"{cy:04d}-{cm:02d}"
    parts = calc._parts_for(part)
    copq_items = []
    scrap_copq = round(_sum_scrap_copq(daily, cy, cm, parts))
    copq_items.append(("Scrap Cost (성형공정 제외분)", scrap_copq, "자동 산출"))
    for it in calc.CLAIM_COPQ_ITEMS:
        amt = calc.claim_sum(conn, ym, parts, [it])
        copq_items.append((it, round(amt), "Claim 입력"))
    denom = calc.svp_of(conn, ym, parts)
    est = denom is None
    if est:
        denom = _sum_prod_amount(daily, cy, cm, parts)
    total = sum(x[1] for x in copq_items)
    copq_pct = round(total / denom * 100, 2) if denom else 0
    conn.close()
    return render(request, "report_kpi.html", u, active="rkpi", heading="KPI 현황",
                  crumb="집계/리포트", pending=pending_count(), part=part,
                  fy=calc.fy_label(fys[-1]), labels=",".join(labels), cur=len(months) - 1,
                  fydiv=fydiv if fydiv is not None else "", fylabels=fylabels,
                  charts=chart_ctx, copq_items=copq_items, copq_total=round(total),
                  copq_pct=copq_pct, denom=round(denom), est=est, ym=ym)


def _sum_scrap_copq(daily, cy, cm, parts):
    tot = 0.0
    for ds, byp in daily.items():
        if ds[:7] != f"{cy:04d}-{cm:02d}":
            continue
        for p in parts:
            c = byp.get(p)
            if c:
                tot += c["scrap_cost_copq"]
    return tot


def _sum_prod_amount(daily, cy, cm, parts):
    tot = 0.0
    for ds, byp in daily.items():
        if ds[:7] != f"{cy:04d}-{cm:02d}":
            continue
        for p in parts:
            c = byp.get(p)
            if c:
                tot += c["prod_amount"]
    return tot


# ── 리포트: 공정별 불량현황 ─────────────────────────────
@app.get("/report/defect", response_class=HTMLResponse)
def report_defect(request: Request, part: str = "VMS PART", kind: str = "공정"):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    if part not in ("VMS PART", "TM PART"):
        part = "VMS PART"
    if kind not in ("공정", "셋팅"):
        kind = "공정"
    conn = db.connect()
    m = calc.Masters(conn)
    cy, cm = latest_month(conn)
    charts = []
    for p in ("VMS PART", "TM PART"):
        pb = calc.process_breakdown(conn, m, cy, cm, p, kind)
        procs = [r["process"] for r in pb]
        charts.append({
            "title": f"공정별 불량율 (ppm) · {p}", "color": "--p-500",
            "labels": ",".join(procs), "vseries": _join([r["ppm"] for r in pb]),
            "over": 1, "target": target_val(conn, calc.fy_of(cy, cm), p, "proc_ppm" if kind == "공정" else "set_ppm"),
        })
    for p in ("VMS PART", "TM PART"):
        pb = calc.process_breakdown(conn, m, cy, cm, p, kind)
        procs = [r["process"] for r in pb]
        mark = ",".join(str(i) for i, r in enumerate(pb) if r["excl"])
        charts.append({
            "title": f"공정별 Scrap Cost (천원) · {p}", "color": "--sec",
            "labels": ",".join(procs), "vseries": _join([r["cost"] for r in pb]),
            "mark": mark, "target": None,
        })
    detail = calc.process_breakdown(conn, m, cy, cm, part, kind)
    conn.close()
    return render(request, "report_defect.html", u, active="rdefect", heading="공정별 불량현황",
                  crumb="집계/리포트", pending=pending_count(), part=part, kind=kind,
                  ym=f"{cy:04d}-{cm:02d}", charts=charts, detail=detail)


# ── 마스터 ──────────────────────────────────────────────
@app.get("/masters", response_class=HTMLResponse)
def masters(request: Request):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    conn = db.connect()
    proc = [dict(r) for r in conn.execute("SELECT * FROM process ORDER BY part,name")]
    prod = [dict(r) for r in conn.execute(
        "SELECT p.tm_no,p.name,p.part,COUNT(r.id) steps FROM product p "
        "LEFT JOIN product_route r ON r.tm_no=p.tm_no GROUP BY p.tm_no ORDER BY p.tm_no")]
    dtypes = [dict(r) for r in conn.execute("SELECT * FROM defect_type ORDER BY kind,name")]
    conn.close()
    return render(request, "masters.html", u, active=None, heading="마스터 관리",
                  crumb="관리", pending=pending_count(), proc=proc, prod=prod, dtypes=dtypes,
                  can_edit=(u["role"] == "admin"))


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


# ── 목표 관리 ───────────────────────────────────────────
@app.get("/admin/target", response_class=HTMLResponse)
def admin_target(request: Request, fy: int = 27, part: str = "통합"):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    conn = db.connect()
    rows = {r["kpi"]: dict(r) for r in conn.execute(
        "SELECT * FROM target WHERE fy=? AND part=?", (fy, part))}
    conn.close()
    order = [("scrap_cost", "Scrap Cost", "%"), ("scrap_qty", "Scrap Quantity", "%"),
             ("copq", "COPQ", "%"), ("incident", "Customer Incident", "건"),
             ("warranty", "Warranty", "천원"), ("proc_ppm", "공정불량율", "ppm"),
             ("set_ppm", "셋팅불량율", "ppm")]
    items = [{"kpi": k, "name": n, "unit": un,
              "value": rows.get(k, {}).get("value", "")} for k, n, un in order]
    return render(request, "target.html", u, active="target", heading="목표 관리",
                  crumb="관리", pending=pending_count(), fy=fy, part=part, items=items,
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
    for kpi, unit in units.items():
        if kpi in ("proc_ppm", "set_ppm") and part == "통합":
            continue
        v = form.get("t_" + kpi, "").replace(",", "").strip()
        if v == "":
            continue
        conn.execute("INSERT INTO target(fy,part,kpi,value,unit) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(fy,part,kpi) DO UPDATE SET value=excluded.value",
                     (fy, part, kpi, float(v), unit))
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
INPUT_PAGES = {
    "defect": ("불량 입력", "불량입력(직접) Excel — 일자·TM-NO·불량명·수량"),
    "outsource": ("외주소재불량 가져오기", "외주소재불량 Excel — 100EA 이상은 검토 격리"),
    "discard": ("폐기불량 가져오기", "폐기불량 Excel"),
    "production": ("생산실적 입력", "생산실적 Excel — 일자·TM-NO·생산수량·생산금액(천원)"),
    "svp": ("SVP 입력", "SVP Excel — 년월·파트·금액(천원)"),
    "claim": ("Claim 입력", "Claim Excel — 년월·파트·항목·금액(천원)"),
    "incident": ("Customer Incident 관리", "Incident Excel — 일자·파트·고객·내용"),
}


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
    if page == "svp":
        return [dict(r) for r in conn.execute("SELECT ym,part,amount FROM svp ORDER BY ym DESC LIMIT 12")]
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
        elif page == "svp":
            msg = f"SVP {ingest.ingest_svp(conn, path)}건"
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


def outsource_review(request, u):
    conn = db.connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT id,d,tm_no,defect_name,qty FROM defect_entry WHERE status='pending' ORDER BY id")]
    conn.close()
    return render(request, "review.html", u, active="oreview", heading="외주소재불량 검토",
                  crumb="데이터 입력", pending=len(rows), rows=rows,
                  can_edit=(u["role"] in ("editor", "admin")))


@app.post("/input/outsource-review/{eid}")
async def review_action(request: Request, eid: int, action: str = Form(...), qty: int = Form(None)):
    u = current_user(request)
    if u is None or u["role"] not in ("editor", "admin"):
        return RedirectResponse("/input/outsource-review", status_code=303)
    conn = db.connect()
    if action == "approve":
        if qty is not None:
            conn.execute("UPDATE defect_entry SET qty=?, status='confirmed' WHERE id=?", (qty, eid))
        else:
            conn.execute("UPDATE defect_entry SET status='confirmed' WHERE id=?", (eid,))
    elif action == "reject":
        conn.execute("DELETE FROM defect_entry WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    return RedirectResponse("/input/outsource-review", status_code=303)
