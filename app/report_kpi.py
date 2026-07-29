# -*- coding: utf-8 -*-
"""세부지표현황(신규, 2026-07-29) — 사용자 제공 `세부지표현황.xlsx` 양식과 동일한 FY(4월~3월)
월별 표를 계산한다.

- 총액(합계)·raw(종합) 구간: **불량을 발견한(입력) 공정 기준**(Scrap Cost 총액 계산과 동일).
- 성형/소결/정형/가공/기타 5개 행 배분(bucket): **단가는 발견공정 그대로 쓰되, 그 비용이
  집계되는 행은 불량유형 마스터가 정의한 귀책(원인)공정**을 따른다(2026-07-29 확정,
  `month_process()` 참고). 총액은 바뀌지 않고 5행 사이의 분배만 달라진다.
- 각 표의 월별 열은 **그 달 단독 실적**만 보여주고, 4월 왼쪽의 **"누계"** 열이 데이터가 있는
  월까지의 누적을 보여준다(2026-07-29 확정). 비율지표(Scrap Cost/Quantity/COPQ %,
  공정불량 ppm)의 누계는 각 월 %의 단순합이 아니라 **누적 분자 ÷ 누적 분모**로 재계산한다.
"""
import calendar
import datetime as _dt
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


def latest_actual_month(conn):
    """생산량 데이터가 존재하는 마지막 (년,월). 없으면 None."""
    row = conn.execute("SELECT MAX(d) m FROM production").fetchone()
    d = row["m"]
    if not d:
        return None
    return int(d[:4]), int(d[5:7])


def _months_so_far(fy, cutoff):
    """FY 12개월 중 cutoff(년,월) 이전(포함)까지만. cutoff가 None이면 전부 제외."""
    months = calc.fy_months(fy)
    if cutoff is None:
        return []
    return [(y, mo) for (y, mo) in months if (y, mo) <= cutoff]


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
    """Scrap Cost/Scrap Quantity 탭의 1개 블록(통합/1Part/2Part) 12개월치 데이터 + 누계.
    qty_mode=True면 단위가 EA(수량), False면 천원(비용)."""
    parts = calc._parts_for(part)
    months = calc.fy_months(fy)
    cutoff = latest_actual_month(conn)
    key = "qty" if qty_mode else "cost"
    gj = {b: [] for b in BUCKETS}
    gj_sub = []
    setting = []
    total = []
    denom = []
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

    idxs = [i for i, (y, mo) in enumerate(months) if (y, mo) in _months_so_far(fy, cutoff)]
    cum_gj = {b: sum(gj[b][i] for i in idxs) for b in BUCKETS}
    cum_gj_sub = sum(gj_sub[i] for i in idxs)
    cum_setting = sum(setting[i] for i in idxs)
    cum_total = sum(total[i] for i in idxs)
    cum_denom = sum(denom[i] for i in idxs)
    cum = {"gj": cum_gj, "gj_sub": cum_gj_sub, "setting": cum_setting, "total": cum_total,
          "denom": cum_denom, "target": target[0] if target else 0,
          "actual": _pct(cum_total, cum_denom)}
    return {"gj": gj, "gj_sub": gj_sub, "setting": setting, "total": total,
           "denom": denom, "target": target, "actual": actual, "cum": cum}


def scrap_cost_table(conn, m, fy):
    return {lbl: _block_cost(conn, m, fy, part, qty_mode=False)
            for lbl, part in (("Total", "통합"), ("1Part", "VMS PART"), ("2Part", "TM PART"))}


def scrap_qty_table(conn, m, fy):
    return {lbl: _block_cost(conn, m, fy, part, qty_mode=True)
            for lbl, part in (("Total", "통합"), ("1Part", "VMS PART"), ("2Part", "TM PART"))}


def _block_copq(conn, m, fy, part):
    parts = calc._parts_for(part)
    months = calc.fy_months(fy)
    cutoff = latest_actual_month(conn)
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

    idxs = [i for i, (y, mo) in enumerate(months) if (y, mo) in _months_so_far(fy, cutoff)]
    cum_items = {it: sum(items[it][i] for i in idxs) for it in COPQ_ITEM_ORDER}
    cum_total = sum(total[i] for i in idxs)
    cum_denom = sum(denom[i] for i in idxs)
    cum = {"copq_items": cum_items, "total": cum_total, "denom": cum_denom,
          "target": target[0] if target else 0, "actual": _pct(cum_total, cum_denom)}
    return {"copq_items": items, "total": total, "denom": denom, "target": target,
           "actual": actual, "cum": cum}


def copq_table(conn, m, fy):
    return {lbl: _block_copq(conn, m, fy, part)
            for lbl, part in (("Total", "통합"), ("1Part", "VMS PART"), ("2Part", "TM PART"))}


def _block_incident(conn, m, fy, part):
    """월별 열은 그 달 발생분만(누적 아님). 왼쪽 누계 열에서 데이터가 있는 달까지 합산."""
    parts = calc._parts_for(part)
    months = calc.fy_months(fy)
    cutoff = latest_actual_month(conn)
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
    target = _target_flat(conn, fy, part, "incident")

    idxs = [i for i, (y, mo) in enumerate(months) if (y, mo) in _months_so_far(fy, cutoff)]
    cum_official = sum(official[i] for i in idxs)
    cum = {"official": cum_official, "target": target[0] if target else 0}
    return {"sub": sub, "official": official, "unofficial": unofficial,
           "target": target, "cum": cum}


def incident_table(conn, m, fy):
    return {lbl: _block_incident(conn, m, fy, part)
            for lbl, part in (("Total", "통합"), ("1Part", "VMS PART"), ("2Part", "TM PART"))}


def _block_warranty(conn, m, fy, part):
    """월별 열은 그 달 발생분만(누적 아님). 왼쪽 누계 열에서 데이터가 있는 달까지 합산."""
    parts = calc._parts_for(part)
    months = calc.fy_months(fy)
    cutoff = latest_actual_month(conn)
    monthly = []
    for (y, mo) in months:
        ym = "%04d-%02d" % (y, mo)
        monthly.append(calc.claim_sum(conn, ym, parts, ["Warranty"]))
    target = _target_flat(conn, fy, part, "warranty")

    idxs = [i for i, (y, mo) in enumerate(months) if (y, mo) in _months_so_far(fy, cutoff)]
    cum_actual = sum(monthly[i] for i in idxs)
    cum = {"actual": cum_actual, "target": target[0] if target else 0}
    return {"actual": monthly, "target": target, "cum": cum}


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
    cutoff = latest_actual_month(conn)
    is1 = part == "VMS PART"
    raw_rows = RAW_ROWS_1PART if is1 else RAW_ROWS_2PART

    raw = {row: [] for row in raw_rows}
    raw_sub = []
    prod_qty = []
    bucket_ppm = {row: [] for row in BUCKET_ROWS}
    bucket_qty = {row: [] for row in BUCKET_ROWS}
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
            bucket_qty[row].append(v)
            bucket_ppm[row].append(round(v / pq * 1_000_000) if pq else 0)

    target = [calc_target_val(conn, fy, part, "proc_ppm", mo) for (_y, mo) in months]
    actual = [round(t / p * 1_000_000) if p else 0 for t, p in zip(raw_sub, prod_qty)]

    idxs = [i for i, (y, mo) in enumerate(months) if (y, mo) in _months_so_far(fy, cutoff)]
    cum_raw = {row: sum(raw[row][i] for i in idxs) for row in raw_rows}
    cum_raw_sub = sum(raw_sub[i] for i in idxs)
    cum_prod_qty = sum(prod_qty[i] for i in idxs)
    cum_bucket_qty = {row: sum(bucket_qty[row][i] for i in idxs) for row in BUCKET_ROWS}
    cum_bucket_ppm = {row: (round(cum_bucket_qty[row] / cum_prod_qty * 1_000_000) if cum_prod_qty else 0)
                     for row in BUCKET_ROWS}
    cum_target = round(sum(target[i] for i in idxs if target[i]) / len(idxs)) if idxs else 0
    cum = {"raw": cum_raw, "raw_sub": cum_raw_sub, "prod_qty": cum_prod_qty,
          "bucket_ppm": cum_bucket_ppm, "target": cum_target,
          "actual": round(cum_raw_sub / cum_prod_qty * 1_000_000) if cum_prod_qty else 0}
    return {"raw_rows": raw_rows, "raw": raw, "raw_sub": raw_sub, "prod_qty": prod_qty,
           "target": target, "actual": actual,
           "bucket_rows": BUCKET_ROWS, "bucket_ppm": bucket_ppm, "cum": cum}


# ── 불량유형별 추이 (탭 8) ──────────────────────────────
def tmno_search(conn, q, part=""):
    """TM-NO 자동완성: 앞자리(prefix) 일치, 최대 20개. defect_entry에 등록된 값(미등록 제품 포함)."""
    if len(q) < 2:
        return []
    sql = ("SELECT DISTINCT de.tm_no tm FROM defect_entry de LEFT JOIN product p ON p.tm_no=de.tm_no "
          "WHERE de.tm_no LIKE ? AND de.tm_no != ''")
    args = [q + "%"]
    if part in ("VMS PART", "TM PART"):
        sql += " AND COALESCE(p.part, de.part) = ?"
        args.append(part)
    sql += " ORDER BY de.tm_no LIMIT 20"
    return [r["tm"] for r in conn.execute(sql, args)]


def defect_names_for_tm(conn, tm):
    """그 TM-NO에 실제 발생한 불량유형명 목록."""
    return [r["defect_name"] for r in conn.execute(
        "SELECT DISTINCT defect_name FROM defect_entry WHERE tm_no=? ORDER BY defect_name", (tm,))]


def _period_setup(cy, cm, unit, date_from=None, date_to=None):
    """일/주/월 단위 기간·라벨·날짜인덱스를 만든다(ITEM·불량유형 두 추이 화면 공통).
    일=date_from~date_to 그대로. 월=최근 12개월. 주=최근 7개월 범위의 실제 주차(라벨 'N월M주',
    calc.week_of_month 기준 월~일). 반환: (d0, d1, labels, day_idx) —
    day_idx: {'YYYY-MM-DD': 그 날짜가 속하는 라벨의 인덱스}."""
    if unit == "월":
        months = calc.trailing_months(cy, cm, 12)
        d0 = f"{months[0][0]:04d}-{months[0][1]:02d}-01"
        y1, m1 = months[-1]
        d1 = f"{y1:04d}-{m1:02d}-{calendar.monthrange(y1, m1)[1]:02d}"
        labels = [f"{mo}월" for (_y, mo) in months]
        mi = {(y, mo): i for i, (y, mo) in enumerate(months)}
        day_idx = {}
        d = _dt.date.fromisoformat(d0)
        end = _dt.date.fromisoformat(d1)
        while d <= end:
            day_idx[d.isoformat()] = mi.get((d.year, d.month))
            d += _dt.timedelta(days=1)
        return d0, d1, labels, day_idx
    if unit == "주":
        months = calc.trailing_months(cy, cm, 7)
        d0 = f"{months[0][0]:04d}-{months[0][1]:02d}-01"
        y1, m1 = months[-1]
        d1 = f"{y1:04d}-{m1:02d}-{calendar.monthrange(y1, m1)[1]:02d}"
        seen, order = {}, []
        day_idx = {}
        d = _dt.date.fromisoformat(d0)
        end = _dt.date.fromisoformat(d1)
        while d <= end:
            wk = calc.week_of_month(d.isoformat())
            key = (d.year, d.month, wk)
            if key not in seen:
                seen[key] = f"{d.month}월{wk}주"
                order.append(key)
            day_idx[d.isoformat()] = order.index(key)
            d += _dt.timedelta(days=1)
        labels = [seen[k] for k in order]
        return d0, d1, labels, day_idx
    # 일
    d0, d1 = date_from, date_to
    labels, day_idx = [], {}
    d = _dt.date.fromisoformat(d0)
    end = _dt.date.fromisoformat(d1)
    while d <= end and len(labels) < 400:
        day_idx[d.isoformat()] = len(labels)
        labels.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return d0, d1, labels, day_idx


def defect_trend(conn, part, tm_q, defect_name, unit, date_from=None, date_to=None):
    """[ITEM] Part·TM-NO/품명·불량유형·집계단위(일/주/월)로 필터링한 불량 발생 추이.
    반환: {"labels":[...], "series":{불량명:[qty...]}, "qty_total":int, "tm_rows":[...]}"""
    parts = calc._parts_for(part) if part != "통합" else ["VMS PART", "TM PART"]
    cutoff = latest_actual_month(conn) or (_dt.date.today().year, _dt.date.today().month)
    cy, cm = cutoff
    d0, d1, labels, day_idx = _period_setup(cy, cm, unit, date_from, date_to)

    where = ["status='confirmed'", "d BETWEEN ? AND ?"]
    args = [d0, d1]
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

    by_type = defaultdict(lambda: [0] * len(labels))
    by_tm = defaultdict(int)
    total = 0
    for r in rows:
        by_tm[r["tm_no"] or "(미지정)"] += r["qty"]
        total += r["qty"]
        i = day_idx.get(r["d"])
        if i is not None:
            by_type[r["defect_name"]][i] += r["qty"]

    top_types = sorted(by_type, key=lambda k: -sum(by_type[k]))[:8]
    series = {t: by_type[t] for t in top_types}
    tm_rows = sorted(by_tm.items(), key=lambda kv: -kv[1])[:20]
    return {"labels": labels, "series": series, "qty_total": total, "tm_rows": tm_rows}


def defect_trend_types(conn, m, part, defect_names, unit, date_from=None, date_to=None):
    """[불량유형] Part·불량유형(최대 3개)·집계단위(일/주/월)로 필터링한 발생 추이.
    반환: {"labels":[...], "series":{불량명:[qty...]}}"""
    parts = calc._parts_for(part) if part != "통합" else ["VMS PART", "TM PART"]
    names = [n for n in defect_names if n][:3]
    if not names:
        return {"labels": [], "series": {}}

    cutoff = latest_actual_month(conn) or (_dt.date.today().year, _dt.date.today().month)
    cy, cm = cutoff
    d0, d1, labels, day_idx = _period_setup(cy, cm, unit, date_from, date_to)

    where = ["status='confirmed'", "d BETWEEN ? AND ?",
            "defect_name IN (%s)" % ",".join("?" * len(names))]
    args = [d0, d1] + names
    rows = []
    for r in conn.execute(
            f"SELECT d,tm_no,defect_name,qty,part FROM defect_entry WHERE {' AND '.join(where)}", args):
        prod = m.product.get(r["tm_no"])
        rpart = prod[1] if prod else (r["part"] or "")
        if part != "통합" and rpart and rpart not in parts:
            continue
        rows.append(r)

    series = {n: [0] * len(labels) for n in names}
    for r in rows:
        i = day_idx.get(r["d"])
        if i is not None:
            series[r["defect_name"]][i] += r["qty"]
    return {"labels": labels, "series": series}
