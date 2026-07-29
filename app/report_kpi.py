# -*- coding: utf-8 -*-
"""세부지표현황(신규, 2026-07-29) — 사용자 제공 `세부지표현황.xlsx` 양식과 동일한 FY(4월~3월)
월별 표를 계산한다.

- 총액(합계)·raw(종합) 구간: **불량을 발견한(입력) 공정 기준**(Scrap Cost 총액 계산과 동일).
- 성형/소결/정형/가공/기타 5개 행 배분(bucket): **단가는 발견공정 그대로 쓰되, 그 비용이
  집계되는 행은 불량유형 마스터가 정의한 귀책(원인)공정**을 따른다(2026-07-29 확정,
  `month_process()` 참고). 총액은 바뀌지 않고 5행 사이의 분배만 달라진다.
"""
from collections import defaultdict

from . import db, calc

BUCKETS = ["성형", "소결", "정형", "가공", "기타"]
COPQ_ITEM_ORDER = ["Scrap cost"] + calc.CLAIM_COPQ_ITEMS + ["기타"]


def month_process(conn, m, y, mth, parts):
    """해당 월 + 파트(들)의 불량을 공정/셋팅으로 나눠 raw(입력 그대로)·bucket(5종 집계공정,
    귀책공정 기준) 두 기준으로 동시 집계한다.
    반환: {"공정": {"raw":{cat:{"qty","cost"}}, "bucket":{buck:{"qty","cost"}}}, "셋팅": {...}}

    raw: 사내불량은 저장된 물리공정(성형/소결/정형/가공/압입/후처리) 그대로, 외주소재불량은
    '외주소재', 폐기불량은 '폐기불량'으로 묶는다 — **불량이 어디서 발견됐는지**를 그대로 보여준다.

    bucket(2026-07-29 수정): **단가는 발견(입력)공정 기준 그대로**(Scrap Cost 총액과 동일하게
    계산)이지만, 그 비용이 집계되는 **행(성형/소결/정형/가공/기타)은 불량유형 마스터에 정의된
    귀책(원인)공정**을 따른다(`Masters.resolve()`가 마스터 배분규칙+제품 라우팅으로 판단, 마스터에
    규칙이 없으면 발견공정 그대로). 즉 "후처리 시트에서 발견된 깨짐(귀책=성형, 후처리단가 500원)"
    이면 수량×500원의 비용 그대로 '성형' 행에 집계된다. 배분(수량 쪼개짐)이 있어도 단가를 공정별로
    다시 매기지 않고 발견공정 단가로 비례배분하므로, 이 재배정은 **총 수량·총 비용에는 영향이 없고
    5개 행 사이의 분배만 달라진다**. 마스터에 없는 TM-NO 등으로 배분이 '?'가 되면 '기타'로 접는다."""
    ym = "%04d-%02d" % (y, mth)
    out = {k: {"raw": defaultdict(lambda: {"qty": 0, "cost": 0.0}),
               "bucket": defaultdict(lambda: {"qty": 0, "cost": 0.0})}
           for k in ("공정", "셋팅")}
    for r in conn.execute(
            "SELECT d,tm_no,defect_name,qty,part,process,kind,source,exclude_cost "
            "FROM defect_entry WHERE status='confirmed' AND d LIKE ?", (ym + "%",)):
        prod = m.product.get(r["tm_no"])
        part = prod[1] if prod else (r["part"] or "")
        if part not in parts:
            continue
        kind, allocs = m.resolve(part, r["tm_no"], r["defect_name"], r["qty"], r["process"], r["kind"])
        if kind not in out:
            continue
        qty = r["qty"]
        cost = 0.0
        if not r["exclude_cost"]:
            price = m.scrap_price(r["tm_no"], r["process"], r["source"], r["d"], part)
            cost = qty * price / 1000.0
        slot = out[kind]
        # raw: 발견(입력) 그대로 — 귀책 배분과 무관, 그대로 둔다
        if r["source"] == "outsource":
            slot["raw"]["외주소재"]["qty"] += qty
            slot["raw"]["외주소재"]["cost"] += cost
        elif r["source"] == "discard":
            slot["raw"]["폐기불량"]["qty"] += qty
            slot["raw"]["폐기불량"]["cost"] += cost
        else:
            cat = r["process"] or "후처리"
            slot["raw"][cat]["qty"] += qty
            slot["raw"][cat]["cost"] += cost
        # bucket: 귀책공정으로 수량 배분, 비용은 발견공정 단가로 계산한 총액을 그 비율대로 배분
        for b, q in allocs:
            if b not in BUCKETS:
                b = "기타"
            slot["bucket"][b]["qty"] += q
            if qty:
                slot["bucket"][b]["cost"] += cost * (q / qty)
    return out


def month_prod(conn, m, y, mth, parts):
    """월 + 파트(들)의 생산수량 합·생산금액 합(천원)."""
    ym = "%04d-%02d" % (y, mth)
    qty, amt = 0, 0.0
    for r in conn.execute("SELECT tm_no,qty,amount,part FROM production WHERE d LIKE ?", (ym + "%",)):
        prod = m.product.get(r["tm_no"])
        part = prod[1] if prod else (r["part"] or "")
        if part not in parts:
            continue
        qty += r["qty"]
        amt += r["amount"]
    return qty, amt


def _target_flat(conn, fy, part, kpi):
    """MONTHLY_KPIS가 아닌 지표(scrap_cost/scrap_qty/copq)의 FY 단일 목표를 12개월에 반복."""
    v = calc_target_val(conn, fy, part, kpi)
    return [v] * 12


# main.py의 target_val을 이 모듈에서도 쓰기 위한 얇은 래퍼(순환 임포트 방지용으로 지연 임포트)
def calc_target_val(conn, fy, part, kpi, mon=0):
    fy = fy % 100 if fy >= 100 else fy
    if mon:
        row = conn.execute(
            "SELECT value FROM target WHERE fy=? AND part=? AND kpi=? AND mon=?",
            (fy, part, kpi, mon)).fetchone()
        if row:
            return row["value"]
    row = conn.execute("SELECT value FROM target WHERE fy=? AND part=? AND kpi=? AND mon=0",
                       (fy, part, kpi)).fetchone()
    return row["value"] if row else None


def _pct(a, b):
    return round(a / b * 100, 3) if b else 0.0


def _block_cost(conn, m, fy, part, qty_mode=False):
    """Scrap Cost/Scrap Quantity 탭의 1개 블록(통합/1Part/2Part) 12개월치 데이터.
    qty_mode=True면 단위가 EA(수량), False면 천원(비용)."""
    parts = calc._parts_for(part)
    months = calc.fy_months(fy)
    key = "qty" if qty_mode else "cost"
    gj = {b: [] for b in BUCKETS}
    gj_sub = []
    setting = []
    total = []
    denom = []
    target = []
    for (y, mo) in months:
        mp = month_process(conn, m, y, mo, parts)
        gj_month = 0.0
        for b in BUCKETS:
            v = mp["공정"]["bucket"].get(b, {}).get(key, 0)
            gj[b].append(v)
            gj_month += v
        gj_sub.append(gj_month)
        set_v = sum(x[key] for x in mp["셋팅"]["bucket"].values())
        setting.append(set_v)
        total.append(gj_month + set_v)
        if qty_mode:
            pq, _pa = month_prod(conn, m, y, mo, parts)
            denom.append(pq)
        else:
            s = calc.svp_of(conn, "%04d-%02d" % (y, mo), parts)
            if s is None:
                _pq, pa = month_prod(conn, m, y, mo, parts)
                s = pa
            denom.append(s)
    tkpi = "scrap_qty" if qty_mode else "scrap_cost"
    target = _target_flat(conn, fy, part, tkpi)
    actual = [_pct(t, d) for t, d in zip(total, denom)]
    return {"gj": gj, "gj_sub": gj_sub, "setting": setting, "total": total,
           "denom": denom, "target": target, "actual": actual}


def scrap_cost_table(conn, m, fy):
    return {lbl: _block_cost(conn, m, fy, part, qty_mode=False)
            for lbl, part in (("Total", "통합"), ("1Part", "VMS PART"), ("2Part", "TM PART"))}


def scrap_qty_table(conn, m, fy):
    return {lbl: _block_cost(conn, m, fy, part, qty_mode=True)
            for lbl, part in (("Total", "통합"), ("1Part", "VMS PART"), ("2Part", "TM PART"))}


def _block_copq(conn, m, fy, part):
    parts = calc._parts_for(part)
    months = calc.fy_months(fy)
    items = {it: [] for it in COPQ_ITEM_ORDER}
    total = []
    denom = []
    for (y, mo) in months:
        ym = "%04d-%02d" % (y, mo)
        mp = month_process(conn, m, y, mo, parts)
        scrap_cost = sum(x["cost"] for k in ("공정", "셋팅") for x in mp[k]["bucket"].values())
        items["Scrap cost"].append(scrap_cost)
        item_sum6 = 0.0
        for it in calc.CLAIM_COPQ_ITEMS:
            v = calc.claim_sum(conn, ym, parts, [it])
            items[it].append(v)
            item_sum6 += v
        all_claim = calc.claim_sum(conn, ym, parts)
        etc = all_claim - item_sum6
        items["기타"].append(etc)
        tot = scrap_cost + item_sum6 + etc
        total.append(tot)
        s = calc.svp_of(conn, ym, parts)
        if s is None:
            _pq, pa = month_prod(conn, m, y, mo, parts)
            s = pa
        denom.append(s)
    target = _target_flat(conn, fy, part, "copq")
    actual = [_pct(t, d) for t, d in zip(total, denom)]
    return {"copq_items": items, "total": total, "denom": denom, "target": target, "actual": actual}


def copq_table(conn, m, fy):
    return {lbl: _block_copq(conn, m, fy, part)
            for lbl, part in (("Total", "통합"), ("1Part", "VMS PART"), ("2Part", "TM PART"))}


def _block_incident(conn, m, fy, part):
    parts = calc._parts_for(part)
    months = calc.fy_months(fy)
    official, unofficial, sub = [], [], []
    for (y, mo) in months:
        ym = "%04d-%02d" % (y, mo)
        like = ym + "%"
        off = conn.execute(
            ("SELECT COUNT(*) c FROM incident WHERE is_official=1 AND d LIKE ? AND part IN (%s)"
             % ",".join("?" * len(parts))), [like] + list(parts)).fetchone()["c"]
        unoff = conn.execute(
            ("SELECT COUNT(*) c FROM incident WHERE is_official=0 AND d LIKE ? AND part IN (%s)"
             % ",".join("?" * len(parts))), [like] + list(parts)).fetchone()["c"]
        official.append(off)
        unofficial.append(unoff)
        sub.append(off + unoff)
    cum_official = []
    run = 0
    for v in official:
        run += v
        cum_official.append(run)
    target = _target_flat(conn, fy, part, "incident")
    return {"sub": sub, "official": official, "unofficial": unofficial,
           "target": target, "actual": cum_official}


def incident_table(conn, m, fy):
    return {lbl: _block_incident(conn, m, fy, part)
            for lbl, part in (("Total", "통합"), ("1Part", "VMS PART"), ("2Part", "TM PART"))}


def _block_warranty(conn, m, fy, part):
    parts = calc._parts_for(part)
    months = calc.fy_months(fy)
    monthly = []
    for (y, mo) in months:
        ym = "%04d-%02d" % (y, mo)
        monthly.append(calc.claim_sum(conn, ym, parts, ["Warranty"]))
    cum = []
    run = 0.0
    for v in monthly:
        run += v
        cum.append(run)
    target = _target_flat(conn, fy, part, "warranty")
    return {"actual": cum, "target": target}


def warranty_table(conn, m, fy):
    return {lbl: _block_warranty(conn, m, fy, part)
            for lbl, part in (("Total", "통합"), ("1Part", "VMS PART"), ("2Part", "TM PART"))}


# 공정불량-1PART/2PART 탭: raw(종합, 발견 그대로) 표시용 행 순서. 압입은 1PART에만 존재.
RAW_ROWS_1PART = ["성형", "소결", "정형", "가공", "압입", "후처리", "외주소재", "폐기불량"]
RAW_ROWS_2PART = ["성형", "소결", "정형", "가공", "후처리", "외주소재", "폐기불량"]
# 공정별(발생=귀책기준) 행: 표준 5버킷. 압입은 그 자체가 집계공정이 아니라(귀책 배분 결과 성형 등
# 실제 원인 공정으로 흩어짐), 여기서는 별도로 떼어 보여주지 않는다.
BUCKET_ROWS = BUCKETS


def process_table(conn, m, fy, part):
    """공정불량-1PART/2PART 탭 데이터. part는 'VMS PART' 또는 'TM PART' (단일 파트만)."""
    parts = [part]
    months = calc.fy_months(fy)
    is1 = part == "VMS PART"
    raw_rows = RAW_ROWS_1PART if is1 else RAW_ROWS_2PART

    raw = {row: [] for row in raw_rows}
    raw_sub = []
    prod_qty = []
    bucket_ppm = {row: [] for row in BUCKET_ROWS}
    for (y, mo) in months:
        mp = month_process(conn, m, y, mo, parts)["공정"]
        month_total = 0
        for row in raw_rows:
            v = mp["raw"].get(row, {}).get("qty", 0)
            raw[row].append(v)
            month_total += v
        raw_sub.append(month_total)
        pq, _pa = month_prod(conn, m, y, mo, parts)
        prod_qty.append(pq)

        for row in BUCKET_ROWS:
            v = mp["bucket"].get(row, {"qty": 0}).get("qty", 0)
            bucket_ppm[row].append(round(v / pq * 1_000_000) if pq else 0)

    target = [calc_target_val(conn, fy, part, "proc_ppm", mo) for (_y, mo) in months]
    actual = [round(t / p * 1_000_000) if p else 0 for t, p in zip(raw_sub, prod_qty)]
    return {"raw_rows": raw_rows, "raw": raw, "raw_sub": raw_sub, "prod_qty": prod_qty,
           "target": target, "actual": actual,
           "bucket_rows": BUCKET_ROWS, "bucket_ppm": bucket_ppm}


# ── 불량유형별 추이 (탭 8, 일 단위) ────────────────────────
def defect_trend(conn, part, tm_q, defect_name, date_from, date_to):
    """Part·TM-NO/품명·불량유형·기간(일 단위)으로 필터링한 불량 발생 추이.
    반환: {"days":[...], "series":{불량명:[qty...]}, "qty_total":int, "tm_rows":[...]}"""
    parts = calc._parts_for(part) if part != "통합" else ["VMS PART", "TM PART"]
    where = ["status='confirmed'", "d BETWEEN ? AND ?"]
    args = [date_from, date_to]
    if defect_name:
        where.append("defect_name=?")
        args.append(defect_name)
    if tm_q:
        where.append("(tm_no LIKE ? OR tm_no=?)")
        args += [f"%{tm_q}%", tm_q]
    q = f"SELECT d,tm_no,defect_name,qty,part FROM defect_entry WHERE {' AND '.join(where)}"
    rows = []
    for r in conn.execute(q, args):
        if r["part"] and r["part"] not in parts and part != "통합":
            continue
        rows.append(r)

    days = []
    d0 = date_from
    import datetime as _dt
    cur = _dt.date.fromisoformat(date_from)
    end = _dt.date.fromisoformat(date_to)
    while cur <= end and len(days) < 400:
        days.append(cur.isoformat())
        cur += _dt.timedelta(days=1)

    by_type = defaultdict(lambda: defaultdict(int))
    by_tm = defaultdict(int)
    total = 0
    for r in rows:
        by_type[r["defect_name"]][r["d"]] += r["qty"]
        by_tm[r["tm_no"] or "(미지정)"] += r["qty"]
        total += r["qty"]

    top_types = sorted(by_type, key=lambda k: -sum(by_type[k].values()))[:8]
    series = {t: [by_type[t].get(d, 0) for d in days] for t in top_types}
    tm_rows = sorted(by_tm.items(), key=lambda kv: -kv[1])[:20]
    return {"days": days, "series": series, "qty_total": total, "tm_rows": tm_rows}
