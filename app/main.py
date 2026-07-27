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
    proc = [dict(r) for r in conn.execute("SELECT * FROM process ORDER BY part,ord,name")]
    prod_total = conn.execute("SELECT COUNT(*) c FROM product").fetchone()["c"]
    prod = [dict(r) for r in conn.execute(
        "SELECT p.tm_no,p.name,p.part,COUNT(r.id) steps FROM product p "
        "LEFT JOIN product_route r ON r.tm_no=p.tm_no GROUP BY p.tm_no ORDER BY p.tm_no LIMIT 20")]
    dtypes = [dict(r) for r in conn.execute("SELECT * FROM defect_type ORDER BY kind,name")]
    conn.close()
    return render(request, "masters.html", u, active="masters", heading="마스터 업로드",
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


def _proc_options(conn):
    """라우팅 체크박스용: 공정명(고유) 순서·COPQ제외."""
    return [dict(r) for r in conn.execute(
        "SELECT name, MIN(ord) o, MAX(copq_exclude) excl FROM process GROUP BY name ORDER BY o, name")]


@app.get("/admin/products", response_class=HTMLResponse)
def products_list(request: Request, q: str = "", part: str = "", page: int = 1, msg: str = "", err: str = ""):
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
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) c FROM product p {wsql}", args).fetchone()["c"]
    page = max(1, page)
    off = (page - 1) * PAGE_SIZE
    rows = [dict(r) for r in conn.execute(
        f"SELECT p.tm_no,p.name,p.part,p.weight, "
        f"(SELECT GROUP_CONCAT(process,'→') FROM (SELECT process FROM product_route "
        f" WHERE tm_no=p.tm_no ORDER BY seq)) route "
        f"FROM product p {wsql} ORDER BY p.part,p.tm_no LIMIT ? OFFSET ?", args + [PAGE_SIZE, off])]
    conn.close()
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    return render(request, "products.html", u, active="products", heading="제품 마스터",
                  crumb="관리", pending=pending_count(), rows=rows, total=total,
                  q=q, part=part, page=page, pages=pages,
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
    procs = _proc_options(conn)
    conn.close()
    return render(request, "product_form.html", u, active="products", heading="제품 등록",
                  crumb="관리 / 제품 마스터", pending=pending_count(), mode="new",
                  p={"tm_no": "", "name": "", "part": "VMS PART", "weight": 0},
                  procs=procs, route={})


@app.get("/admin/products/{tm}/edit", response_class=HTMLResponse)
def product_edit(request: Request, tm: str):
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
    route = {r["process"]: dict(r) for r in conn.execute(
        "SELECT process,unit_price,op_code FROM product_route WHERE tm_no=? ORDER BY seq", (tm,))}
    procs = _proc_options(conn)
    conn.close()
    return render(request, "product_form.html", u, active="products", heading="제품 수정",
                  crumb="관리 / 제품 마스터", pending=pending_count(), mode="edit",
                  p=dict(p), procs=procs, route=route)


@app.post("/admin/products/save")
async def product_save(request: Request):
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
    try:
        weight = float(form.get("weight") or 0)
    except ValueError:
        weight = 0.0
    conn = db.connect()
    procs = _proc_options(conn)
    if orig and orig != tm:
        conn.execute("DELETE FROM product WHERE tm_no=?", (orig,))
        conn.execute("DELETE FROM product_route WHERE tm_no=?", (orig,))
    conn.execute("INSERT INTO product(tm_no,name,part,weight) VALUES(?,?,?,?) "
                 "ON CONFLICT(tm_no) DO UPDATE SET name=excluded.name, part=excluded.part, "
                 "weight=excluded.weight", (tm, name, part, weight))
    conn.execute("DELETE FROM product_route WHERE tm_no=?", (tm,))
    seq = 0
    for pr in procs:                          # 표준 공정 순서대로
        if form.get("use_" + pr["name"]):
            seq += 1
            try:
                price = float((form.get("price_" + pr["name"]) or "0").replace(",", ""))
            except ValueError:
                price = 0.0
            conn.execute("INSERT INTO product_route(tm_no,seq,process,unit_price,op_code) "
                         "VALUES(?,?,?,?,?)", (tm, seq, pr["name"], price,
                                              (form.get("code_" + pr["name"]) or "").strip()))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/products?msg=저장됨: {tm}", status_code=303)


@app.post("/admin/products/{tm}/delete")
def product_delete(request: Request, tm: str):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/admin/products", status_code=303)
    conn = db.connect()
    conn.execute("DELETE FROM product WHERE tm_no=?", (tm,))
    conn.execute("DELETE FROM product_route WHERE tm_no=?", (tm,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/products?msg=삭제됨", status_code=303)


# ── 공정별 단가 ─────────────────────────────────────────
def _price_map(conn, tm):
    """{집계공정: 단가|None}"""
    cur = {r["process"]: r["unit_price"]
           for r in conn.execute("SELECT process,unit_price FROM product_price WHERE tm_no=?", (tm,))}
    return {p: cur.get(p) for p in db.AGG_PROCESSES}


@app.get("/admin/prices", response_class=HTMLResponse)
def prices_list(request: Request, q: str = "", part: str = "", miss: str = "",
                page: int = 1, msg: str = "", err: str = ""):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    conn = db.connect()
    # 제품 마스터 ∪ 단가 보유 TM (단가만 있고 제품 미등록인 품목도 보이도록)
    base_sql = ("SELECT tm_no FROM product UNION SELECT tm_no FROM product_price")
    where, args = [], []
    if q:
        where.append("(p.tm_no LIKE ? OR IFNULL(pr.name,'') LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
    if part in ("VMS PART", "TM PART"):
        where.append("pr.part=?"); args.append(part)
    if miss:
        where.append("NOT EXISTS(SELECT 1 FROM product_price pp WHERE pp.tm_no=p.tm_no)")
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    from_sql = f"FROM ({base_sql}) p LEFT JOIN product pr ON pr.tm_no=p.tm_no {wsql}"
    total = conn.execute(f"SELECT COUNT(*) c {from_sql}", args).fetchone()["c"]
    page = max(1, page)
    off = (page - 1) * PAGE_SIZE
    rows = []
    for r in conn.execute(
            f"SELECT p.tm_no, IFNULL(pr.name,'') name, IFNULL(pr.part,'') part, "
            f"(SELECT GROUP_CONCAT(process,'→') FROM (SELECT process FROM product_route "
            f" WHERE tm_no=p.tm_no ORDER BY seq)) route "
            f"{from_sql} ORDER BY p.tm_no LIMIT ? OFFSET ?", args + [PAGE_SIZE, off]):
        d = dict(r)
        d["price"] = _price_map(conn, r["tm_no"])
        rows.append(d)
    conn.close()
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    return render(request, "prices.html", u, active="prices", heading="공정별 단가",
                  crumb="관리", pending=pending_count(), rows=rows, total=total,
                  procs=db.AGG_PROCESSES, q=q, part=part, miss=miss, page=page, pages=pages,
                  can_edit=(u["role"] == "admin"), msg=msg, err=err)


@app.post("/admin/prices/import")
async def prices_import(request: Request, file: UploadFile = File(...)):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/admin/prices", status_code=303)
    path = os.path.join("/tmp", "qms_price_" + secrets.token_hex(4) + ".xlsx")
    with open(path, "wb") as f:
        f.write(await file.read())
    conn = db.connect()
    msg = err = ""
    try:
        n, note = ingest.ingest_price_master(conn, path)
        if note.get("error"):
            err = note["error"]
        else:
            msg = (f"단가 {n}품목 반영 (정형없음→완제품단가 {note['정형없음(완제품단가)']}건, "
                   f"스킵 {note['완제품단가없어_스킵']}건)")
    except Exception as e:
        err = f"가져오기 실패: {e}"
    finally:
        conn.close()
        os.remove(path)
    return RedirectResponse(f"/admin/prices?{'msg='+msg if msg else 'err='+err}", status_code=303)


@app.get("/admin/prices/new", response_class=HTMLResponse)
def price_new(request: Request):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    if u["role"] != "admin":
        return RedirectResponse("/admin/prices", status_code=303)
    return render(request, "price_form.html", u, active="prices", heading="단가 등록",
                  crumb="관리 / 공정별 단가", pending=pending_count(), mode="new",
                  p={"tm_no": "", "name": "", "price": {p: None for p in db.AGG_PROCESSES}},
                  procs=db.AGG_PROCESSES)


@app.get("/admin/prices/{tm}/edit", response_class=HTMLResponse)
def price_edit(request: Request, tm: str):
    u = current_user(request)
    g = _guard(u)
    if g:
        return g
    if u["role"] != "admin":
        return RedirectResponse("/admin/prices", status_code=303)
    conn = db.connect()
    prow = conn.execute("SELECT tm_no,name FROM product WHERE tm_no=?", (tm,)).fetchone()
    p = {"tm_no": tm, "name": prow["name"] if prow else "", "price": _price_map(conn, tm)}
    conn.close()
    return render(request, "price_form.html", u, active="prices", heading="단가 수정",
                  crumb="관리 / 공정별 단가", pending=pending_count(), mode="edit",
                  p=p, procs=db.AGG_PROCESSES)


@app.post("/admin/prices/save")
async def price_save(request: Request):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/admin/prices", status_code=303)
    form = await request.form()
    tm = calc.base_tmno(str(form.get("tm_no", "")))
    if not tm:
        return RedirectResponse("/admin/prices?err=TM-NO 필수", status_code=303)
    conn = db.connect()
    for i, proc in enumerate(db.AGG_PROCESSES):
        raw = str(form.get(f"price_{i}", "")).strip()
        if raw == "":
            conn.execute("DELETE FROM product_price WHERE tm_no=? AND process=?", (tm, proc))
            continue
        try:
            v = float(raw)
        except ValueError:
            conn.close()
            return RedirectResponse(f"/admin/prices?err={proc} 단가가 숫자가 아님", status_code=303)
        conn.execute("INSERT INTO product_price(tm_no,process,unit_price) VALUES(?,?,?) "
                     "ON CONFLICT(tm_no,process) DO UPDATE SET unit_price=excluded.unit_price",
                     (tm, proc, v))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/prices?msg=저장됨: {tm}&q={tm}", status_code=303)


@app.post("/admin/prices/{tm}/delete")
def price_delete(request: Request, tm: str):
    u = current_user(request)
    if u is None or u["role"] != "admin":
        return RedirectResponse("/admin/prices", status_code=303)
    conn = db.connect()
    conn.execute("DELETE FROM product_price WHERE tm_no=?", (tm,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/prices?msg=단가 삭제됨", status_code=303)


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
# 사내불량·외주·폐기·생산은 '폴더 반영'(/admin/scan)으로 수집하므로 업로드 화면을 두지 않는다.
# 수기 입력만 존재하는 소스(SVP·Claim·Incident)만 남긴다.
INPUT_PAGES = {
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
