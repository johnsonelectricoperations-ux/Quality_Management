# -*- coding: utf-8 -*-
"""TM-NO별 공정단가(성형/소결/정형/가공/기타) 규칙 산출 (2026-07-28 확정).

규칙(사용자 확정)
- "나머지공정단가" = 최근 3개월 production 실적의 (입고금액 합 ÷ 입고수량 합).
  같은 TM-NO가 여러 고객사로 출하돼 단가가 섞여 있어도, 수량 합계·금액 합계로 나누므로
  자동으로 수량가중평균이 된다.
- 정형공정이 있는 품목: 성형=나머지×0.5, 소결=나머지×0.6, 정형=나머지×0.8
  정형공정이 없는 품목: 성형=나머지×0.6, 소결=나머지×0.8
  가공·기타 = 나머지공정단가 그대로(항상 동일 단가).
- 정형 유무는 product_route(제품 라우팅)에 '정형' 버킷이 있는지로 판단한다.
- 최근 3개월 입고실적이 없는 TM-NO는 계산하지 않는다(기존 product_price 값을 그대로 둔다).

effective_from(효력시작일)
- product_price는 (tm_no, process, effective_from) 단위로 이력을 쌓는다.
- 지금 이 규칙을 처음 적용할 때는 effective_from='2000-01-01'(기존 기준값)을 갱신한다.
- 매년 4월 자동 재계산(yearly_update)은 그 시점의 4월 1일을 effective_from으로 새로 추가한다.
  과거(그 이전) 원가 계산은 이전 단가를 그대로 쓰고, 4월 1일 이후 발생분부터만 새 단가가 적용된다
  (calc.Masters.price_on()이 발생일 기준으로 유효한 단가를 고른다).
"""
from datetime import date

from . import db
from . import calc

RATIO_WITH_JEONGHYEONG = {"성형": 0.5, "소결": 0.6, "정형": 0.8}
RATIO_NO_JEONGHYEONG = {"성형": 0.6, "소결": 0.8}
# 2PART(TM PART)는 정형 유무와 무관하게 성형 0.5 · 소결 0.8 고정 (2026-07-29 확정).
# 기존 엑셀 실적과 대조한 결과 2PART는 이 배수가 실제와 맞는다.
RATIO_TM_PART = {"성형": 0.5, "소결": 0.8, "정형": 0.8}
REST_PROCESSES = ("가공", "기타")   # 나머지공정단가 그대로 적용


def ratios_for(part, has_jeonghyeong):
    """파트·정형유무에 따른 공정단가 배수(기타=1.0 기준)."""
    if part == "TM PART":
        r = dict(RATIO_TM_PART)
        if not has_jeonghyeong:
            r.pop("정형", None)
        return r
    return RATIO_WITH_JEONGHYEONG if has_jeonghyeong else RATIO_NO_JEONGHYEONG


def compute_process_prices(conn, months=3):
    """최근 N개월 production 실적 기준으로 TM-NO별 공정단가를 계산.
    반환: {(tm_no, process): unit_price}. 최근 실적이 없는 TM-NO는 결과에 없음
    (호출측에서 기존 product_price 값을 그대로 두게 된다)."""
    cy, cm = _latest_month(conn)
    ym_set = {f"{y:04d}-{m:02d}" for y, m in calc.trailing_months(cy, cm, months)}

    agg = {}  # tm_no -> [qty_sum, amount_sum(천원)]
    for r in conn.execute("SELECT d,tm_no,qty,amount FROM production"):
        if r["d"][:7] not in ym_set:
            continue
        cur = agg.setdefault(r["tm_no"], [0, 0.0])
        cur[0] += r["qty"]; cur[1] += r["amount"]

    routes = {}  # tm_no -> {버킷공정 집합}
    for r in conn.execute("SELECT tm_no,process FROM product_route"):
        routes.setdefault(r["tm_no"], set()).add(db.bucket_of(r["process"]))
    parts = {r["tm_no"]: r["part"] for r in conn.execute("SELECT tm_no,part FROM product")}

    out = {}
    for tm, (qty, amt) in agg.items():
        if qty <= 0:
            continue
        rest = amt * 1000.0 / qty                     # 천원 → 원, 개당
        has_jh = "정형" in routes.get(tm, set())
        for proc, ratio in ratios_for(parts.get(tm, ""), has_jh).items():
            out[(tm, proc)] = rest * ratio
        for proc in REST_PROCESSES:
            out[(tm, proc)] = rest
    return out


def suggest_prices(conn, tm, months=3):
    """제품마스터 화면(단가 입력 보조)용: 특정 TM-NO 하나에 대해 최근 N개월 production 실적으로
    '나머지공정단가'를 추정하고, 정형 유무 두 경우 모두의 화면표시 4종(성형/소결/정형/기타) 제안값을
    함께 반환한다(실제 라우팅 저장 여부와 무관 — 화면에서 정형 체크박스 상태에 맞는 쪽을 쓰면 됨).
    반환: {"rest": 나머지공정단가|None, "with_jh": {...}|None, "no_jh": {...}}.
    최근 실적이 없으면 rest=None, with_jh/no_jh도 None."""
    cy, cm = _latest_month(conn)
    ym_set = {f"{y:04d}-{m:02d}" for y, m in calc.trailing_months(cy, cm, months)}
    qty_sum, amt_sum = 0, 0.0
    for r in conn.execute("SELECT d,qty,amount FROM production WHERE tm_no=?", (tm,)):
        if r["d"][:7] not in ym_set:
            continue
        qty_sum += r["qty"]; amt_sum += r["amount"]
    if qty_sum <= 0:
        return {"rest": None, "with_jh": None, "no_jh": None}
    rest = amt_sum * 1000.0 / qty_sum                  # 천원 → 원, 개당
    prow = conn.execute("SELECT part FROM product WHERE tm_no=?", (tm,)).fetchone()
    part = prow["part"] if prow else ""
    with_jh = {p: round(rest * r) for p, r in ratios_for(part, True).items()}
    with_jh["기타"] = round(rest)
    no_jh = {p: round(rest * r) for p, r in ratios_for(part, False).items()}
    no_jh["기타"] = round(rest)
    return {"rest": round(rest), "with_jh": with_jh, "no_jh": no_jh}


def _latest_month(conn):
    row = conn.execute("SELECT MAX(d) m FROM production").fetchone()
    d = row["m"] or date.today().isoformat()
    return int(d[:4]), int(d[5:7])


def apply_prices(conn, effective_from, months=3):
    """계산한 단가를 product_price에 저장(해당 effective_from 값만 갱신, 다른 시점 이력은 보존).
    반환: (반영건수, TM-NO수)."""
    prices = compute_process_prices(conn, months)
    rows = [(tm, proc, price, effective_from) for (tm, proc), price in prices.items()]
    conn.executemany(
        "INSERT INTO product_price(tm_no,process,unit_price,effective_from) VALUES(?,?,?,?) "
        "ON CONFLICT(tm_no,process,effective_from) DO UPDATE SET unit_price=excluded.unit_price",
        rows)
    conn.commit()
    tm_count = len({tm for tm, _p, _v, _e in rows})
    return len(rows), tm_count


def yearly_update(conn=None):
    """매년 4월 1일 실행용(Windows 작업 스케줄러 등록). 최근 3개월 실적으로 재계산하고,
    그 해 4월 1일부터만 적용되도록 새 effective_from을 추가한다(과거 원가는 불변)."""
    own = conn is None
    if own:
        conn = db.connect()
    eff = f"{date.today().year:04d}-04-01"
    n, tm_count = apply_prices(conn, eff)
    if own:
        conn.close()
    return n, tm_count, eff


if __name__ == "__main__":
    n, tm_count, eff = yearly_update()
    print(f"공정단가 재계산: {tm_count}개 TM-NO, {n}건 반영 (effective_from={eff})")
