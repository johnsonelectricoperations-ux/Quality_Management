# -*- coding: utf-8 -*-
"""초기 데이터 구축 — templates/ 의 실데이터로 DB를 처음 채운다.

운영 흐름
    ① (최초 1회) 이 스크립트로 templates/ 데이터를 DB에 적재
    ② (이후 계속) 공유 폴더(\\carp130001\\...\\Quality_Data)에서 '폴더 반영'으로 신규 수집

templates/ 는 초기 구축에만 쓰고, 이후에는 보지 않는다.
겹치는 데이터는 폴더 쪽이 최신이므로 폴더 반영이 덮어쓴다(외주·폐기는 전체 교체, 나머지는 upsert).

사용:
    python -m app.init_data          # 이미 데이터가 있으면 중단
    python -m app.init_data --force  # 강제 재실행
"""
import os
import sys

from . import db, ingest

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "templates")

# (라벨, 파일명, 적재함수) — 순서 중요: 마스터 → 단가 → 실적
STEPS = [
    ("불량유형 마스터", "불량유형마스터_양식.xlsx",
     lambda conn, p: _defect_master(conn, p)),
    ("제품 목록 (VMS)", "TM-NO_List_VMS Part.CSV",
     lambda conn, p: "%d품목" % ingest.ingest_product_csv(conn, p, "VMS PART")[0]),
    ("제품 목록 (TM)", "TM-NO_List_TM Part.CSV",
     lambda conn, p: "%d품목" % ingest.ingest_product_csv(conn, p, "TM PART")[0]),
    ("공정별 단가", "제품별 단가 Master_Rev23(FY25).xlsx",
     lambda conn, p: "%d품목" % ingest.ingest_price_master(conn, p)[0]),
    ("생산수량 (1part)", "1part_20260723_20260723.xlsx",
     lambda conn, p: "%d품목" % ingest.ingest_production_xlsx(conn, p)[0]),
    ("생산수량 (2part)", "2part_20260723_20260723.xlsx",
     lambda conn, p: "%d품목" % ingest.ingest_production_xlsx(conn, p)[0]),
    ("외주소재불량", "00_sintering_defect.xlsm",
     lambda conn, p: "%d건" % ingest.ingest_outsource_xlsm(conn, p)[0]),
    ("폐기불량", "scrap_data.db",
     lambda conn, p: "%d건" % ingest.ingest_scrap_db(conn, p)[0]),
]


def _defect_master(conn, path):
    n, errs = ingest.ingest_defect_master(conn, path)
    if errs:
        raise RuntimeError("; ".join(errs[:3]))
    return "%d건" % n


def has_data(conn):
    """이미 운영 데이터가 있는가."""
    for t in ("defect_entry", "production", "product"):
        if conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]:
            return True
    return False


def run(conn, templates_dir=None, echo=print):
    """templates/ 데이터를 순서대로 적재. 반환: [(라벨, 결과문자열 또는 오류)]"""
    base = templates_dir or TEMPLATES
    results = []
    for label, fname, fn in STEPS:
        path = os.path.join(base, fname)
        if not os.path.exists(path):
            results.append((label, "건너뜀 (파일 없음)"))
            echo(f"  - {label}: 건너뜀 (파일 없음: {fname})")
            continue
        try:
            out = fn(conn, path)
            results.append((label, out))
            echo(f"  ✓ {label}: {out}")
        except Exception as e:
            results.append((label, f"실패: {e}"))
            echo(f"  ! {label}: 실패 - {e}")
    return results


def main():
    force = "--force" in sys.argv
    db.init_db()
    conn = db.connect()
    if has_data(conn) and not force:
        print("이미 데이터가 있습니다. 초기 구축을 건너뜁니다.")
        print("강제로 다시 실행하려면: python -m app.init_data --force")
        conn.close()
        return
    print("초기 데이터 구축 시작 (templates/)")
    run(conn, echo=print)
    db.set_setting(conn, "init_data_done", "1")
    conn.close()
    print("\n완료. 이후 신규 데이터는 '폴더 반영' 화면에서 공유 폴더로 수집하세요.")


if __name__ == "__main__":
    main()
