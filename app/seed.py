# -*- coding: utf-8 -*-
"""개발용: sample_data/*.xlsx → DB 일괄 적재 + 기본 목표 세팅.

사용: python -m app.seed   (기존 qms.db 초기화 후 재적재)
"""
import os

from . import db, ingest, calc

SD = os.path.join(os.path.dirname(__file__), "..", "sample_data")
SD = os.path.abspath(SD)


def _p(name):
    return os.path.join(SD, name)


def seed_targets(conn):
    """FY26/FY27 목표 샘플 (통합/VMS/TM). % 지표는 %, 불량율은 ppm."""
    conn.execute("DELETE FROM target")
    base = {
        "FY26": {"scrap_cost": 1.10, "scrap_qty": 1.60, "copq": 1.30, "warranty": 1100,
                 "incident": 0, "proc_ppm": 2800, "set_ppm": 1600},
        "FY27": {"scrap_cost": 1.00, "scrap_qty": 1.50, "copq": 1.20, "warranty": 1000,
                 "incident": 0, "proc_ppm": 2500, "set_ppm": 1500},
    }
    units = {"scrap_cost": "%", "scrap_qty": "%", "copq": "%", "warranty": "천원",
             "incident": "건", "proc_ppm": "ppm", "set_ppm": "ppm"}
    rows = []
    for lbl, fy in (("FY26", 26), ("FY27", 27)):
        for part in ("통합", "VMS PART", "TM PART"):
            for kpi, val in base[lbl].items():
                # 공정/셋팅 불량율은 통합 목표 없음
                if kpi in ("proc_ppm", "set_ppm") and part == "통합":
                    continue
                rows.append((fy, part, kpi, val, units[kpi]))
    conn.executemany("INSERT INTO target(fy,part,kpi,value,unit) VALUES(?,?,?,?,?)", rows)
    conn.commit()


def main():
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init_db()
    conn = db.connect()
    print("적재 시작:", db.DB_PATH)
    print("  공정명 :", ingest.ingest_process_master(conn, _p("공정명마스터.xlsx")))
    print("  제품   :", ingest.ingest_product_master(conn, _p("제품마스터.xlsx")))
    n, errs = ingest.ingest_defect_master(conn, _p("불량유형마스터.xlsx"))
    print("  불량유형:", n, "오류:", errs)
    print("  불량입력:", ingest.ingest_defect_entries(conn, _p("불량입력.xlsx"), "direct"))
    print("  외주소재:", ingest.ingest_defect_entries(conn, _p("외주소재불량.xlsx"), "outsource",
                                                   quarantine_100=True))
    print("  폐기   :", ingest.ingest_defect_entries(conn, _p("폐기불량.xlsx"), "discard"))
    print("  생산실적:", ingest.ingest_production(conn, _p("생산실적.xlsx")))
    print("  SVP    :", ingest.ingest_svp(conn, _p("SVP.xlsx")))
    print("  Claim  :", ingest.ingest_claim(conn, _p("Claim.xlsx")))
    print("  Incident:", ingest.ingest_incident(conn, _p("Incident.xlsx")))
    seed_targets(conn)
    print("  목표   : seeded")
    conn.close()
    print("완료.")


if __name__ == "__main__":
    main()
