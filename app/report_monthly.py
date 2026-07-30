# -*- coding: utf-8 -*-
"""월마감 보고서(품질경영 회의록) 데이터 조립 — 2026-07-30 신설.

원본 양식: `templates/26년 7월 품질경영 회의록_6월마감.pdf` (참고용, 숫자 대조 대상 아님).
구성(사용자 확정): 목차에서 (2)KPI 현황·(4)협력업체 품질실적은 **제외**하고 6개 장으로 재번호.

성능
    발표 중 화면이 지연되면 안 되므로 두 가지를 쓴다.
    ① `_collect_fy()` 가 FY 12개월치 불량·생산을 **단 한 번만 스캔**해 필요한 모든 집계를 만든다
       (월×파트×공정마다 다시 조회하지 않는다 — 옛 방식이면 수십 번 재스캔했다).
    ② 조립 결과는 `report_cache` 에 JSON으로 저장해 두고 화면은 그것만 읽는다.
       데이터가 바뀌면 '재계산' 버튼으로 갱신한다(마감 후 수정불가 규칙은 없음 — 2026-07-30 확정).

달성율
    `2 - 실적/목표` (낮을수록 좋은 지표 기준), 0~200%로 자른다. 원본 PDF의 달성율 값과 일치하는
    식임을 6월마감 자료로 역산 확인했다(예: 실적 461 / 목표 900 → 149%).
"""
import calendar
import datetime as _dt
import json
from collections import defaultdict

from . import db, calc

BUCKETS = ["성형", "소결", "정형", "가공", "기타"]
BAN_PROCS = ["성형", "소결", "정형", "가공"]        # 반별목표제 대상(기타후공정 제외)
PART_LABEL = {"VMS PART": "생산1P", "TM PART": "생산2P", "통합": "합계"}

# KOI 5개 지표: (target kpi, 표시명, 단위, 소수자리, month_kpi 키)
KOI_METRICS = [
    ("warranty", "Customer Warranty Cost", "천원", 0, "warranty"),
    ("copq", "Cost of Poor Quality", "%", 3, "copq_pct"),
    ("incident", "Customer Incidents", "건수", 0, "incident"),
    ("scrap_qty", "Total Internal Scrap/Reject Qty", "%", 3, "scrap_qty_pct"),
    ("scrap_cost", "Total Internal Scrap Cost", "%", 3, "scrap_cost_pct"),
]

CONTENTS = [
    "(1) KOI 현황",
    "(2) 공정불량 현황",
    "(3) 고객 품질 ISSUE",
    "(4) 고객 Claim 현황",
    "(5) 품질 COST : Scrap Cost, COPQ",
    "(6) 주요 업무 진행 현황",
]


def achieve(actual, target):
    """달성율(%) = 2 - 실적/목표, 0~200% clamp. 목표 없거나 0이면 None."""
    if target in (None, 0) or actual is None:
        return None
    return round(max(0.0, min(2.0, 2 - (actual / target))) * 100)


def _fy_ym(fy):
    return ["%04d-%02d" % (y, mo) for (y, mo) in calc.fy_months(fy)]


def _collect_fy(conn, m, fy):
    """FY 12개월 불량·생산을 **1회 스캔**해 월×파트별 집계를 모두 만든다.

    반환 dict:
      pq[(ym,part)]  생산수량      pa[(ym,part)]  생산금액(천원)
      bq[(ym,part,kind,bucket)] 귀책공정 기준 수량   bc[...] 비용(천원)
      rq[(ym,part,kind)] 발견기준 총수량(공정/셋팅 소계용)
      tmc[(ym,part,tm)] TM-NO별 비용(천원, 공정+셋팅)
      tmd[(ym,part,bucket,tm)] = {"qty","dates":set,"by":{불량명:수량}}  (반별 서술용)
    """
    ymset = set(_fy_ym(fy))
    d0, d1 = min(ymset) + "-01", max(ymset) + "-31"
    pq, pa = defaultdict(int), defaultdict(float)
    bq, bc = defaultdict(int), defaultdict(float)
    rq = defaultdict(int)
    tmc = defaultdict(float)
    tmd = {}

    for r in conn.execute(
            "SELECT d,tm_no,qty,amount,part FROM production WHERE d BETWEEN ? AND ?", (d0, d1)):
        ym = r["d"][:7]
        if ym not in ymset:
            continue
        prod = m.product.get(r["tm_no"])
        part = prod[1] if prod else (r["part"] or "")
        if not part:
            continue
        pq[(ym, part)] += r["qty"]
        pa[(ym, part)] += r["amount"]

    for r in conn.execute(
            "SELECT d,tm_no,defect_name,qty,part,process,kind,source,exclude_cost "
            "FROM defect_entry WHERE status='confirmed' AND d BETWEEN ? AND ?", (d0, d1)):
        ym = r["d"][:7]
        if ym not in ymset:
            continue
        prod = m.product.get(r["tm_no"])
        part = prod[1] if prod else (r["part"] or "")
        if not part:
            continue
        kind, allocs = m.resolve(part, r["tm_no"], r["defect_name"], r["qty"],
                                 r["process"], r["kind"])
        if kind not in ("공정", "셋팅"):
            continue
        qty = r["qty"]
        cost = 0.0
        if not r["exclude_cost"]:
            cost = qty * m.scrap_price(r["tm_no"], r["process"], r["source"], r["d"], part) / 1000.0
        rq[(ym, part, kind)] += qty
        tmc[(ym, part, (r["tm_no"] or "").strip())] += cost
        for b, q in allocs:
            if b not in BUCKETS:
                b = "기타"
            bq[(ym, part, kind, b)] += q
            if qty:
                bc[(ym, part, kind, b)] += cost * (q / qty)
            if kind == "공정":
                key = (ym, part, b, (r["tm_no"] or "").strip())
                cur = tmd.get(key)
                if cur is None:
                    cur = tmd[key] = {"qty": 0, "dates": set(), "by": defaultdict(int)}
                cur["qty"] += q
                cur["dates"].add(r["d"])
                cur["by"][r["defect_name"]] += q
    return {"pq": pq, "pa": pa, "bq": bq, "bc": bc, "rq": rq, "tmc": tmc, "tmd": tmd}


def _fy_actual(conn, fy, part, kpi):
    r = conn.execute("SELECT value FROM fy_actual WHERE fy=? AND part=? AND kpi=?",
                     (fy % 100, part, kpi)).fetchone()
    return r["value"] if r else None


def _target(conn, fy, part, kpi, mon=0):
    fy = fy % 100 if fy >= 100 else fy
    if mon:
        r = conn.execute("SELECT value FROM target WHERE fy=? AND part=? AND kpi=? AND mon=?",
                         (fy, part, kpi, mon)).fetchone()
        if r:
            return r["value"]
    r = conn.execute("SELECT value FROM target WHERE fy=? AND part=? AND kpi=? AND mon=0",
                     (fy, part, kpi)).fetchone()
    return r["value"] if r else None


def _ratio(a, b, dec=3):
    return round(a / b * 100, dec) if b else 0.0


def _ppm(a, b):
    return round(a / b * 1_000_000) if b else 0


# ── (1) KOI 현황 ────────────────────────────────────────
def _koi_block(conn, m, daily, fy, part, upto):
    """지표 5종 × (FY 연간목표 + 4월~마감월 실적/목표/달성율)."""
    months = [mo for (_y, mo) in calc.fy_months(fy)]
    idx = months.index(upto) + 1
    use = calc.fy_months(fy)[:idx]
    ppm_part = part if part != "통합" else "VMS PART"
    out = []
    for tkpi, name, unit, dec, mkey in KOI_METRICS:
        acts, tgts, achs = [], [], []
        for (y, mo) in use:
            k = calc.month_kpi(conn, m, daily, y, mo, part)
            v = k.get(mkey)
            t = _target(conn, fy, ppm_part if tkpi in ("proc_ppm", "set_ppm") else part, tkpi)
            acts.append(v)
            tgts.append(t)
            achs.append(achieve(v, t))
        out.append({"name": name, "unit": unit, "dec": dec,
                    "fy_target": _target(conn, fy, part, tkpi),
                    "actual": acts, "target": tgts, "achieve": achs,
                    "vmax": max([a for a in acts if a is not None] + [t for t in tgts if t] + [0])})
    return {"months": [mo for (_y, mo) in use], "metrics": out}


# ── (2) 공정불량 현황 + 반별목표제 ──────────────────────
def _proc_block(conn, m, agg, fy, part, upto):
    ymlist = _fy_ym(fy)
    months = [mo for (_y, mo) in calc.fy_months(fy)]
    upto_i = months.index(upto)
    qty, prod, ppm, tgt, ach = [], [], [], [], []
    for i, ym in enumerate(ymlist):
        q = agg["rq"].get((ym, part, "공정"), 0)
        p = agg["pq"].get((ym, part), 0)
        t = _target(conn, fy, part, "proc_ppm", months[i])
        v = _ppm(q, p) if i <= upto_i else None
        qty.append(q if i <= upto_i else None)
        prod.append(p if i <= upto_i else None)
        ppm.append(v)
        tgt.append(t)
        ach.append(achieve(v, t) if i <= upto_i else None)
    cq = sum(q for q in qty[:upto_i + 1] if q)
    cp = sum(p for p in prod[:upto_i + 1] if p)
    cum = _ppm(cq, cp)
    ft = _target(conn, fy, part, "proc_ppm")
    return {"months": months, "upto_i": upto_i, "ppm": ppm, "target": tgt, "achieve": ach,
            "fy_cum": cum, "fy_cum_ach": achieve(cum, ft), "fy_target": ft,
            "prev_actual": _fy_actual(conn, fy - 1, part, "proc_ppm"),
            "prev_target": _target(conn, fy - 1, part, "proc_ppm"),
            "vmax": max([v for v in ppm if v] + [t for t in tgt if t] + [0])}


def _ban_block(conn, m, agg, fy, part, upto, top=2):
    """반별목표제: 공정별 ppm 추이 + 그 달 수량 상위 TM-NO 서술."""
    ymlist = _fy_ym(fy)
    months = [mo for (_y, mo) in calc.fy_months(fy)]
    upto_i = months.index(upto)
    upto_ym = ymlist[upto_i]
    out = {}
    for proc in BAN_PROCS:
        ppm, tgt, ach = [], [], []
        for i, ym in enumerate(ymlist):
            q = agg["bq"].get((ym, part, "공정", proc), 0)
            p = agg["pq"].get((ym, part), 0)
            t = _target(conn, fy, part, "ban_ppm_" + proc)
            v = _ppm(q, p) if i <= upto_i else None
            ppm.append(v)
            tgt.append(t)
            ach.append(achieve(v, t) if i <= upto_i else None)
        cq = sum(agg["bq"].get((ymlist[i], part, "공정", proc), 0) for i in range(upto_i + 1))
        cp = sum(agg["pq"].get((ymlist[i], part), 0) for i in range(upto_i + 1))
        cum = _ppm(cq, cp)
        ft = _target(conn, fy, part, "ban_ppm_" + proc)
        # 그 달 서술: 수량 상위 TM-NO
        items = []
        for (ym, p2, b, tm), v in agg["tmd"].items():
            if ym == upto_ym and p2 == part and b == proc:
                items.append((tm, v))
        items.sort(key=lambda kv: -kv[1]["qty"])
        rows = []
        for tm, v in items[:top]:
            prod = m.product.get(tm) if tm else None
            days = sorted(v["dates"])
            shown = days[:6]
            rows.append({
                "tm_no": tm, "no_tm": not tm,
                "name": prod[0] if prod else "",
                "qty": v["qty"],
                "dates": ", ".join("%d/%d" % (int(d[5:7]), int(d[8:10])) for d in shown)
                         + (" 외 %d일" % (len(days) - len(shown)) if len(days) > len(shown) else ""),
                "defects": " / ".join("%s %d" % (n, q) for n, q in
                                      sorted(v["by"].items(), key=lambda kv: -kv[1])[:3]),
            })
        out[proc] = {"months": months, "upto_i": upto_i, "ppm": ppm, "target": tgt,
                     "achieve": ach, "fy_cum": cum, "fy_cum_ach": achieve(cum, ft),
                     "fy_target": ft,
                     "prev_actual": _fy_actual(conn, fy - 1, part, "ban_ppm_" + proc),
                     "prev_target": _target(conn, fy - 1, part, "ban_ppm_" + proc),
                     "top_items": rows,
                     "vmax": max([v for v in ppm if v] + [t for t in tgt if t] + [0])}
    return out


def _top5(conn, m, y, mth, part):
    d0 = "%04d-%02d-01" % (y, mth)
    d1 = "%04d-%02d-%02d" % (y, mth, calendar.monthrange(y, mth)[1])
    rows = calc.top5_defect(conn, m, d0, d1, part)
    tot = sum(r["defect"] for r in rows) or 1
    for r in rows:
        r["share"] = round(r["defect"] / tot * 100, 1)
        r["by_txt"] = " / ".join("%s %d" % (n, q) for n, q in r["by"][:3])
    return rows


# ── (3) 고객 품질 ISSUE ─────────────────────────────────
def _issue_block(conn, fy, part, upto):
    months = [mo for (_y, mo) in calc.fy_months(fy)]
    ymlist = _fy_ym(fy)
    upto_i = months.index(upto)
    off, unoff = [], []
    for i, ym in enumerate(ymlist):
        if i > upto_i:
            off.append(None); unoff.append(None); continue
        o = conn.execute("SELECT COUNT(*) c FROM incident WHERE is_official=1 AND d LIKE ? AND part=?",
                         (ym + "%", part)).fetchone()["c"]
        u = conn.execute("SELECT COUNT(*) c FROM incident WHERE is_official=0 AND d LIKE ? AND part=?",
                         (ym + "%", part)).fetchone()["c"]
        off.append(o); unoff.append(u)
    by_cust = defaultdict(lambda: [0, 0])
    for r in conn.execute(
            "SELECT customer, is_official, COUNT(*) c FROM incident "
            "WHERE part=? AND d BETWEEN ? AND ? GROUP BY customer, is_official",
            (part, ymlist[0] + "-01", ymlist[upto_i] + "-31")):
        by_cust[r["customer"] or "(미지정)"][0 if r["is_official"] else 1] += r["c"]
    # 그 달 상세(문제사항/사진/원인/대책)
    upto_ym = ymlist[upto_i]
    details = []
    for r in conn.execute(
            "SELECT id,d,customer,location,tm_no,product_name,content,defect_qty,cause,action,is_official "
            "FROM incident WHERE part=? AND d LIKE ? ORDER BY d", (part, upto_ym + "%")):
        row = dict(r)
        row["photos"] = [f["id"] for f in conn.execute(
            "SELECT id FROM incident_file WHERE incident_id=? AND kind='photo' ORDER BY id",
            (r["id"],))]
        details.append(row)
    return {"months": months, "upto_i": upto_i, "official": off, "unofficial": unoff,
            "prev_official": _fy_actual(conn, fy - 1, part, "incident_official"),
            "prev_unofficial": _fy_actual(conn, fy - 1, part, "incident_unofficial"),
            "fy_official": sum(v for v in off if v), "fy_unofficial": sum(v for v in unoff if v),
            "by_cust": sorted(([k] + v for k, v in by_cust.items()), key=lambda x: -(x[1] + x[2])),
            "details": details,
            "vmax": max([v for v in off if v] + [1])}


# ── (4) 고객 Claim 현황 ─────────────────────────────────
def _claim_block(conn, fy, upto):
    """업체별 클레임 금액(만원) — FY26 시드 + FY 월별 실적. claim.amount는 천원이라 /10."""
    months = [mo for (_y, mo) in calc.fy_months(fy)]
    ymlist = _fy_ym(fy)
    upto_i = months.index(upto)
    monthly = []
    for i, ym in enumerate(ymlist):
        if i > upto_i:
            monthly.append(None); continue
        s = conn.execute("SELECT COALESCE(SUM(amount-reclaim),0) s FROM claim "
                         "WHERE use_agg=1 AND d LIKE ?", (ym + "%",)).fetchone()["s"]
        monthly.append(round(s / 10))
    by_cust = defaultdict(lambda: [0] * (len(months) + 1))   # [FY이전, 월별...]
    for r in conn.execute(
            "SELECT customer, d, SUM(amount-reclaim) s FROM claim WHERE use_agg=1 "
            "AND d BETWEEN ? AND ? GROUP BY customer, d",
            (ymlist[0] + "-01", ymlist[upto_i] + "-31")):
        i = ymlist.index(r["d"][:7]) if r["d"][:7] in ymlist else None
        if i is not None:
            by_cust[r["customer"] or "(미지정)"][i + 1] += round(r["s"] / 10)
    for r in conn.execute("SELECT customer, amount FROM fy_claim WHERE fy=?", ((fy - 1) % 100,)):
        by_cust[r["customer"]][0] += round(r["amount"])
    details = []
    for r in conn.execute(
            "SELECT d,part,customer,tm_no,product_name,item,amount,reclaim,content FROM claim "
            "WHERE d BETWEEN ? AND ? ORDER BY d",
            (ymlist[0] + "-01", ymlist[upto_i] + "-31")):
        details.append({"ym": r["d"][:7], "part": PART_LABEL.get(r["part"], r["part"]),
                        "customer": r["customer"], "tm_no": r["tm_no"],
                        "product_name": r["product_name"], "item": r["item"],
                        "amount": round(r["amount"] / 10), "reclaim": round(r["reclaim"] / 10),
                        "net": round((r["amount"] - r["reclaim"]) / 10),
                        "content": r["content"]})
    prev_total = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM fy_claim WHERE fy=?",
                              ((fy - 1) % 100,)).fetchone()["s"]
    return {"months": months, "upto_i": upto_i, "monthly": monthly,
            "fy_total": sum(v for v in monthly if v), "prev_total": round(prev_total),
            "by_cust": sorted(([k] + v for k, v in by_cust.items()), key=lambda x: -sum(x[1:])),
            "details": details,
            "vmax": max([v for v in monthly if v] + [round(prev_total), 1])}


# ── (5) 품질 COST ───────────────────────────────────────
SCRAP_ROWS = BUCKETS + ["셋팅"]


def _scrap_block(conn, m, agg, fy, y, mth):
    """공정별 전월 vs 당월 Scrap Cost(만원) + 증감률, 품명별 파레토, 파트별 TOP3."""
    ymlist = _fy_ym(fy)
    cur = "%04d-%02d" % (y, mth)
    pm = (y, mth - 1) if mth > 1 else (y - 1, 12)
    prev = "%04d-%02d" % pm
    parts = ["VMS PART", "TM PART"]

    def cost(ym, part, row):
        if row == "셋팅":
            return sum(agg["bc"].get((ym, part, "셋팅", b), 0.0) for b in BUCKETS)
        return agg["bc"].get((ym, part, "공정", row), 0.0)

    table = []
    for row in SCRAP_ROWS:
        cells = {}
        for part in parts + ["통합"]:
            ps = parts if part == "통합" else [part]
            a = sum(cost(prev, p, row) for p in ps) / 10.0    # 천원 → 만원
            b = sum(cost(cur, p, row) for p in ps) / 10.0
            cells[part] = {"prev": round(a), "cur": round(b),
                           "rate": (round((b - a) / a * 100) if a else None),
                           "up": b > a}
        table.append({"name": row, "cells": cells})
    total = {}
    for part in parts + ["통합"]:
        ps = parts if part == "통합" else [part]
        a = sum(cost(prev, p, r) for p in ps for r in SCRAP_ROWS) / 10.0
        b = sum(cost(cur, p, r) for p in ps for r in SCRAP_ROWS) / 10.0
        total[part] = {"prev": round(a), "cur": round(b),
                       "rate": (round((b - a) / a * 100) if a else None), "up": b > a}

    pareto, top3 = {}, {}
    for part in parts:
        items = [(tm, c / 10.0) for (ym, p, tm), c in agg["tmc"].items()
                 if ym == cur and p == part and c > 0]
        items.sort(key=lambda kv: -kv[1])
        tot = sum(c for _t, c in items) or 1
        bars, acc = [], 0.0
        for tm, c in items[:5]:
            acc += c
            prod = m.product.get(tm)
            bars.append({"label": (prod[0] if prod else (tm or "(미지정)"))[:14],
                         "value": round(c), "cum": round(acc / tot * 100), "etc": False})
        etc = tot - sum(c for _t, c in items[:5])
        if etc > 0.5:
            # 품목이 분산된 파트는 '기타'가 상위5보다 클 수 있다 — 회색으로 구분해 오해를 막는다.
            bars.append({"label": "기타(%d품목)" % max(0, len(items) - 5),
                         "value": round(etc), "cum": 100, "etc": True})
        pareto[part] = {"bars": bars, "vmax": max([b["value"] for b in bars] + [1])}
        t3 = []
        for tm, c in items[:3]:
            prod = m.product.get(tm)
            best = max(((b, agg["tmd"].get((cur, part, b, tm), {}).get("qty", 0)) for b in BUCKETS),
                       key=lambda kv: kv[1])
            names = agg["tmd"].get((cur, part, best[0], tm), {}).get("by", {})
            t3.append({"tm_no": tm, "name": prod[0] if prod else "",
                       "cost": round(c), "proc": best[0] if best[1] else "-",
                       "defect": " / ".join(list(dict(sorted(names.items(),
                                                             key=lambda kv: -kv[1])).keys())[:2])})
        top3[part] = t3
    return {"prev_ym": prev, "cur_ym": cur, "rows": table, "total": total,
            "pareto": pareto, "top3": top3}


def _copq_block(conn, m, agg, fy, part, upto):
    """공정불량/클레임/Q-COST(만원) + COPQ% + 목표 + 달성율. FY26은 시드값."""
    ymlist = _fy_ym(fy)
    months = [mo for (_y, mo) in calc.fy_months(fy)]
    upto_i = months.index(upto)
    parts = [part]
    defect, claim, qcost, pct, tgt, ach = [], [], [], [], [], []
    for i, ym in enumerate(ymlist):
        if i > upto_i:
            for L in (defect, claim, qcost, pct, tgt, ach):
                L.append(None)
            continue
        dcost = sum(agg["bc"].get((ym, part, k, b), 0.0) for k in ("공정", "셋팅") for b in BUCKETS)
        cl = calc.claim_sum(conn, ym, parts, calc.CLAIM_COPQ_ITEMS)
        svp = calc.svp_of(conn, ym, parts)
        denom = svp if svp is not None else agg["pa"].get((ym, part), 0.0)
        t = _target(conn, fy, part, "copq")
        p = _ratio(dcost + cl, denom)
        defect.append(round(dcost / 10)); claim.append(round(cl / 10))
        qcost.append(round((dcost + cl) / 10)); pct.append(p)
        tgt.append(t); ach.append(achieve(p, t))
    cd = sum(v for v in defect if v); cc = sum(v for v in claim if v)
    cum_denom = 0.0
    for i in range(upto_i + 1):
        svp = calc.svp_of(conn, ymlist[i], parts)
        cum_denom += svp if svp is not None else agg["pa"].get((ymlist[i], part), 0.0)
    cum_pct = _ratio((cd + cc) * 10, cum_denom)
    ft = _target(conn, fy, part, "copq")
    return {"months": months, "upto_i": upto_i,
            "defect": defect, "claim": claim, "qcost": qcost, "pct": pct,
            "target": tgt, "achieve": ach,
            "fy_defect": cd, "fy_claim": cc, "fy_qcost": cd + cc,
            "fy_pct": cum_pct, "fy_target": ft, "fy_ach": achieve(cum_pct, ft),
            "prev_defect": _fy_actual(conn, fy - 1, part, "copq_defect_amt"),
            "prev_claim": _fy_actual(conn, fy - 1, part, "copq_claim_amt"),
            "prev_pct": _fy_actual(conn, fy - 1, part, "copq_pct"),
            "prev_target": _target(conn, fy - 1, part, "copq"),
            "vmax": max([v for v in qcost if v] + [1])}


# ── 전체 조립 ───────────────────────────────────────────
def build(conn, m, y, mth):
    """월마감 보고서 전체 데이터. 화면은 이 결과(캐시)만 읽는다."""
    fy = calc.fy_of(y, mth)
    daily = calc.compute_daily(conn, m)
    agg = _collect_fy(conn, m, fy)
    txt = conn.execute("SELECT content FROM report_text WHERE ym=? AND section='main_tasks'",
                       ("%04d-%02d" % (y, mth),)).fetchone()
    return {
        "ym": "%04d-%02d" % (y, mth), "y": y, "m": mth,
        "fy_label": calc.fy_label(fy), "prev_fy_label": calc.fy_label(fy - 1),
        "contents": CONTENTS,
        "koi": {PART_LABEL[p]: _koi_block(conn, m, daily, fy, p, mth)
                for p in ("통합", "VMS PART", "TM PART")},
        "proc": {PART_LABEL[p]: _proc_block(conn, m, agg, fy, p, mth)
                 for p in ("VMS PART", "TM PART")},
        "top5": {PART_LABEL[p]: _top5(conn, m, y, mth, p) for p in ("VMS PART", "TM PART")},
        "ban": {PART_LABEL[p]: _ban_block(conn, m, agg, fy, p, mth)
                for p in ("VMS PART", "TM PART")},
        "issue": {PART_LABEL[p]: _issue_block(conn, fy, p, mth) for p in ("VMS PART", "TM PART")},
        "claim": _claim_block(conn, fy, mth),
        "scrap": _scrap_block(conn, m, agg, fy, y, mth),
        "copq": {PART_LABEL[p]: _copq_block(conn, m, agg, fy, p, mth)
                 for p in ("VMS PART", "TM PART")},
        "main_tasks": (txt["content"] if txt else ""),
        "built_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ── 캐시 (발표 중 지연 방지) ────────────────────────────
def get_cached(conn, ym):
    r = conn.execute("SELECT payload, built_at, built_by FROM report_cache WHERE ym=?",
                     (ym,)).fetchone()
    if not r:
        return None
    try:
        data = json.loads(r["payload"])
    except ValueError:
        return None
    data["built_at"] = r["built_at"]
    data["built_by"] = r["built_by"]
    return data


def rebuild(conn, m, y, mth, user_name=""):
    data = build(conn, m, y, mth)
    conn.execute(
        "INSERT INTO report_cache(ym,payload,built_at,built_by) VALUES(?,?,?,?) "
        "ON CONFLICT(ym) DO UPDATE SET payload=excluded.payload, built_at=excluded.built_at, "
        "built_by=excluded.built_by",
        (data["ym"], json.dumps(data, ensure_ascii=False), data["built_at"], user_name))
    conn.commit()
    data["built_by"] = user_name
    return data


def get_or_build(conn, m, y, mth, user_name=""):
    ym = "%04d-%02d" % (y, mth)
    return get_cached(conn, ym) or rebuild(conn, m, y, mth, user_name)
