# -*- coding: utf-8 -*-
"""통합품질관리시스템 샘플 Excel 데이터 생성기 (시드 고정).

생성물(sample_data/):
  마스터  : 공정명마스터.xlsx, 제품마스터.xlsx, 불량유형마스터.xlsx
  트랜잭션: 불량입력.xlsx, 외주소재불량.xlsx, 폐기불량.xlsx,
           생산실적.xlsx, SVP.xlsx, Claim.xlsx, Incident.xlsx
기간: FY26(2025-04~2026-03) 전체 + FY27(2026-04~2026-07-22)
"""
import os
import random
from datetime import date, timedelta
from openpyxl import Workbook

random.seed(20260722)
OUT = os.path.join(os.path.dirname(__file__), "..", "sample_data")
OUT = os.path.abspath(OUT)
os.makedirs(OUT, exist_ok=True)

START = date(2025, 4, 1)      # FY26 시작
END = date(2026, 7, 22)       # 당일(FY27 진행)

# ── 마스터 정의 ──────────────────────────────────────────
PARTS = ["VMS PART", "TM PART"]
PROCESSES = ["성형", "소결", "정형", "선별"]           # 순서 = 공정 순서
COPQ_EXCLUDE = {"성형"}                                # COPQ 제외 공정

# 제품: TM-NO → (품명, 파트, [ (공정, 누적단가원) ])
def route(prices):
    return [(p, pr) for p, pr in zip(PROCESSES, prices)]

PRODUCTS = {
    "1545-01": ("VVT 스프로킷",   "VMS PART", route([120, 260, 310, 335])),
    "2233-02": ("오일펌프 로터",  "VMS PART", route([95, 210, 250, 268])),
    "3010-04": ("캠 로브",        "VMS PART", route([140, 300, 360, 390])),
    "4820-07": ("베인",          "VMS PART", route([80, 175, 210, 226])),
    "5501-03": ("부싱",          "VMS PART", route([60, 130, 158, 170])),
    "6120-01": ("스테이터 코어",  "TM PART",  route([150, 320, 390, 420])),
    "6640-05": ("샤프트",        "TM PART",  route([110, 240, 285, 305])),
    "7233-02": ("커뮤테이터",     "TM PART",  route([175, 360, 430, 465])),
    "8410-03": ("하우징",        "TM PART",  route([90, 195, 235, 252])),
    "9051-06": ("마그넷 요크",    "TM PART",  route([130, 280, 335, 360])),
}
VMS_TM = {tm: v[1] for tm, v in PRODUCTS.items()}

# 불량유형: 불량명(고유) → (구분, 발생공정, 배분기준)
DEFECT_TYPES = {
    "찍힘":     ("공정", "선별", "소결,정형"),
    "크랙":     ("공정", "소결", ""),
    "치수불량":  ("공정", "정형", "정형:70,성형:30"),
    "이물혼입":  ("공정", "선별", "성형"),
    "밀도불량":  ("공정", "성형", ""),
    "변형":     ("공정", "소결", ""),
    "소재크랙":  ("공정", "소결", "소결"),          # 외주소재불량용
    "초물셋팅":  ("셋팅", "성형", ""),
    "치수셋팅":  ("셋팅", "정형", ""),
}
PROC_DEFECTS = [d for d, v in DEFECT_TYPES.items() if v[0] == "공정" and d != "소재크랙"]
SET_DEFECTS = [d for d, v in DEFECT_TYPES.items() if v[0] == "셋팅"]

CLAIM_ITEMS = ["Warranty", "3rd Party Containment", "Quality Special Freight",
               "Customer Incident Cost", "Unplanned Inspection & Sorting", "Variance"]


# ── 유틸 ────────────────────────────────────────────────
def weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)

def months(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            m, y = 1, y + 1

def save(name, headers, rows, text_cols=()):
    wb = Workbook()
    ws = wb.active
    ws.title = "DATA"
    ws.append(headers)
    for r in rows:
        ws.append(list(r))
    for ci in text_cols:                       # TM-NO 등 텍스트 서식 강제
        for row in range(2, len(rows) + 2):
            ws.cell(row=row, column=ci).number_format = "@"
    path = os.path.join(OUT, name)
    wb.save(path)
    print(f"  {name}: {len(rows)} rows")


# ── 1) 마스터 ───────────────────────────────────────────
def gen_masters():
    proc_rows = []
    for part in PARTS:
        for p in PROCESSES:
            proc_rows.append((part, p, "Y" if p in COPQ_EXCLUDE else "N"))
    save("공정명마스터.xlsx", ["파트구분", "공정명", "COPQ제외여부"], proc_rows)

    prod_rows = []
    for tm, (name, part, rt) in PRODUCTS.items():
        for seq, (proc, price) in enumerate(rt, start=1):
            prod_rows.append((tm, name, part, seq, proc, price))
    save("제품마스터.xlsx", ["TM-NO", "품명", "파트구분", "공정순서", "공정명", "기준단가(원)"],
         prod_rows, text_cols=(1,))

    dt_rows = [(v[0], v[1], name, v[2]) for name, v in DEFECT_TYPES.items()]
    save("불량유형마스터.xlsx", ["구분", "공정명(발생공정)", "불량명", "배분기준"], dt_rows)


# ── 2) 생산실적 (일자·TM-NO·수량·금액천원) ───────────────
prod_qty_by_daytm = {}   # (date,tm) -> qty  (불량율 분모/타당성용)
def gen_production():
    rows = []
    for d in weekdays(START, END):
        for tm in PRODUCTS:
            base = 120 + (hash(tm) % 120)
            qty = max(20, int(random.gauss(base, base * 0.18)))
            amount = round(qty * random.uniform(3.2, 5.4))     # 천원
            prod_qty_by_daytm[(d, tm)] = qty
            rows.append((d.isoformat(), tm, qty, amount))
    save("생산실적.xlsx", ["일자", "TM-NO", "생산수량(EA)", "생산금액(천원)"],
         rows, text_cols=(2,))


# ── 3) 불량입력 (직접, 공정+셋팅) ────────────────────────
def gen_defect_direct():
    rows = []
    for d in weekdays(START, END):
        for tm in PRODUCTS:
            if random.random() < 0.30:                    # 공정불량 (과다 방지)
                name = random.choice(PROC_DEFECTS)
                rows.append((d.isoformat(), tm, name, random.randint(1, 4)))
            if random.random() < 0.05:                    # 셋팅불량 가끔
                name = random.choice(SET_DEFECTS)
                rows.append((d.isoformat(), tm, name, random.randint(1, 3)))
    save("불량입력.xlsx", ["일자", "TM-NO", "불량명", "수량"], rows, text_cols=(2,))


# ── 4) 외주소재불량 (100EA↑ 격리 데모 포함) ──────────────
def gen_outsource():
    rows = []
    for d in weekdays(START, END):
        if random.random() < 0.12:
            tm = random.choice(list(PRODUCTS))
            name = random.choice(["소재크랙", "이물혼입"])
            qty = random.randint(3, 15)
            rows.append((d.isoformat(), tm, name, qty))
    # 최근 몇 건은 100EA 이상 (검토 격리 시연)
    for tm, name, qty in [("1545-01", "소재크랙", 240), ("3010-04", "이물혼입", 155),
                          ("2233-02", "소재크랙", 108)]:
        rows.append((END.isoformat(), tm, name, qty))
    save("외주소재불량.xlsx", ["일자", "TM-NO", "불량명", "수량"], rows, text_cols=(2,))


# ── 5) 폐기불량 ─────────────────────────────────────────
def gen_discard():
    rows = []
    for d in weekdays(START, END):
        if random.random() < 0.10:
            tm = random.choice(list(PRODUCTS))
            name = random.choice(PROC_DEFECTS)
            rows.append((d.isoformat(), tm, name, random.randint(2, 10)))
    save("폐기불량.xlsx", ["일자", "TM-NO", "불량명", "수량"], rows, text_cols=(2,))


# ── 6) SVP (월·파트·천원) ───────────────────────────────
def gen_svp():
    rows = []
    for y, m in months(START, END):
        # FY27 당월(2026-07)은 미입력으로 두어 '추정 SVP' 시연
        if (y, m) == (2026, 7):
            continue
        for part in PARTS:
            base = 72_000 if part == "VMS PART" else 68_000   # 월 생산금액과 정합
            rows.append((f"{y:04d}-{m:02d}", part, int(base * random.uniform(0.92, 1.08))))
    save("SVP.xlsx", ["년월", "파트", "금액(천원)"], rows)


# ── 7) Claim (월·파트·항목·천원) ────────────────────────
def gen_claim():
    rows = []
    for y, m in months(START, END):
        for part in PARTS:
            for item in CLAIM_ITEMS:
                base = {"Warranty": 1000, "Quality Special Freight": 300,
                        "Customer Incident Cost": 400,
                        "Unplanned Inspection & Sorting": 150}.get(item, 0)
                val = int(base * random.uniform(0.4, 1.4)) if base else \
                    (random.choice([0, 0, 0, 120]) if item != "Variance" else 0)
                rows.append((f"{y:04d}-{m:02d}", part, item, val))
    save("Claim.xlsx", ["년월", "파트", "항목", "금액(천원)"], rows)


# ── 8) Customer Incident (건) ──────────────────────────
def gen_incident():
    rows = []
    customers = ["H사", "K사", "D사", "S사"]
    texts = ["납품 로트 표면 결함 통보", "치수 산포 이슈 제기", "조립 간섭 클레임",
             "소음 이상 보고", "포장 불량 접수"]
    for y, m in months(START, END):
        for _ in range(random.randint(0, 3)):
            day = random.randint(1, 27)
            part = random.choice(PARTS)
            rows.append((date(y, m, day).isoformat(), part,
                         random.choice(customers), random.choice(texts)))
    save("Incident.xlsx", ["일자", "파트", "고객", "내용"], rows)


if __name__ == "__main__":
    print(f"샘플 데이터 생성 → {OUT}")
    gen_masters()
    gen_production()
    gen_defect_direct()
    gen_outsource()
    gen_discard()
    gen_svp()
    gen_claim()
    gen_incident()
    print("완료.")
