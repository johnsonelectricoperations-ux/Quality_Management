# -*- coding: utf-8 -*-
"""서버 공유 폴더 스캔 → DB 적재.

폴더 구조 (루트 = setting 'data_root', 기본 \\10.80.12.103\\...\\Quality_Data):
    01_사내불량\\{연도}\\{1파트|2파트}\\{월}\\{YYMM}_{공정}_{공정불량|셋팅불량}_생산{1|2}파트.xlsx
        (하위폴더는 사람이 찾기용, 시스템은 재귀 탐색 + 파일명으로 판단. 월별 1파일, 시트=일자 1~31)
    02_생산량\\{YYYY-MM}\\
    03_외주소재\\00_sintering_defect.xlsm            (고정 파일 덮어쓰기)

폐기불량은 위 공유 루트와 별개의 로컬 경로(설정 'scrap_root', 기본 C:\\PJT\\Scrap_management)를
쓴다 — 그 폴더 안에 여러 파일이 섞여 있어도 **scrap_data.db 라는 이름의 파일만** 읽는다
(2026-07-31 확정, 서버PC 이전하며 소스 위치 변경).

단가마스터는 폴더 반영 대상에서 제외한다(2026-07-31, 필요 없어져 삭제 — 단가는
/admin/products 화면에서 직접 관리).

원칙
- 폴더의 파일은 **읽기만** 한다(수정·이동·삭제 없음).
- 재적재는 멱등: 사내불량=batch_key, 외주/폐기=source 전체 교체, 생산=upsert.
- 화면(수동 버튼)과 Windows 작업 스케줄러(`QMS_DataScan`, 매일 02:10, `deploy/scan_data.bat`
  → `python -m app.scan`)가 **같은 scan_all()** 을 호출한다(2026-07-31 스케줄러 연결).
"""
import os
import datetime

from . import db, ingest

DEFAULT_ROOT = r"\\10.80.12.103\TheEyesHaveIt\QC_Data\Quality_Data"
SETTING_ROOT = "data_root"

DEFAULT_SCRAP_ROOT = r"C:\PJT\Scrap_management"
SETTING_SCRAP_ROOT = "scrap_root"
SCRAP_FILENAME = "scrap_data.db"

SUB_DEFECT = "01_사내불량"
SUB_PRODUCTION = "02_생산량"
SUB_OUTSOURCE = "03_외주소재"

SOURCES = [
    ("defect", "사내불량", SUB_DEFECT),
    ("production", "생산량", SUB_PRODUCTION),
    ("outsource", "외주소재", SUB_OUTSOURCE),
    ("scrap", "폐기불량", ""),
]


def data_root(conn):
    return db.get_setting(conn, SETTING_ROOT, "") or os.environ.get("QMS_DATA_ROOT", DEFAULT_ROOT)


def scrap_root(conn):
    return db.get_setting(conn, SETTING_SCRAP_ROOT, "") or os.environ.get("QMS_SCRAP_ROOT", DEFAULT_SCRAP_ROOT)


def root_for(conn, key):
    """소스별 루트 — 폐기불량만 공유 루트와 별개 경로를 쓴다."""
    return scrap_root(conn) if key == "scrap" else data_root(conn)


def _log(conn, kind, filename, ok, note):
    conn.execute("INSERT INTO upload_log(ts,kind,filename,ok,note) VALUES(?,?,?,?,?)",
                 (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind, filename,
                  1 if ok else 0, note[:500]))
    conn.commit()


def _find_files(folder, exts, recursive=True):
    """폴더(하위 포함) 내 대상 파일 경로 목록. Excel 임시파일 제외."""
    out = []
    if not os.path.isdir(folder):
        return out
    if recursive:
        for root, _d, names in os.walk(folder):
            for fn in sorted(names):
                if not fn.startswith("~$") and fn.lower().endswith(exts):
                    out.append(os.path.join(root, fn))
    else:
        for fn in sorted(os.listdir(folder)):
            p = os.path.join(folder, fn)
            if os.path.isfile(p) and not fn.startswith("~$") and fn.lower().endswith(exts):
                out.append(p)
    return out


def _newest(paths):
    return max(paths, key=os.path.getmtime) if paths else None


# ── 소스별 스캔 ────────────────────────────────────────
def scan_defect(conn, root):
    folder = os.path.join(root, SUB_DEFECT)
    if not os.path.isdir(folder):
        return {"ok": False, "note": "폴더 없음", "files": 0, "rows": 0, "errors": []}
    # 다른 소스(생산량·외주·폐기·단가)는 이미 이 try/except가 있는데 사내불량만 빠져 있었다.
    # 그래서 사내불량 파일 하나의 오류가 전체(scan_all) 반영을 통째로 실패시켰다(2026-07-30 수정).
    try:
        files, rows, detail = ingest.ingest_monthly_defect_folder(conn, folder)
    except Exception as e:
        return {"ok": False, "note": str(e), "files": 0, "rows": 0, "errors": [str(e)]}
    errors = [f"{rel}: {'; '.join(errs)}" for rel, _n, errs in detail if errs]
    return {"ok": not errors, "files": files, "rows": rows, "errors": errors,
            "note": f"{files}개 파일 / {rows}건 적재"}


def scan_production(conn, root):
    folder = os.path.join(root, SUB_PRODUCTION)
    paths = _find_files(folder, (".xlsx", ".xlsm"))
    if not paths:
        return {"ok": False, "note": "대상 파일 없음", "files": 0, "rows": 0, "errors": []}
    total, errors = 0, []
    for p in paths:
        try:
            n, errs = ingest.ingest_production_xlsx(conn, p)
            total += n
            errors += [f"{os.path.basename(p)}: {e}" for e in errs]
        except Exception as e:
            errors.append(f"{os.path.basename(p)}: {e}")
    return {"ok": not errors, "files": len(paths), "rows": total, "errors": errors,
            "note": f"{len(paths)}개 파일 / {total}품목 적재"}


def scan_outsource(conn, root):
    paths = _find_files(os.path.join(root, SUB_OUTSOURCE), (".xlsm", ".xlsx"), recursive=False)
    p = _newest(paths)
    if not p:
        return {"ok": False, "note": "대상 파일 없음", "files": 0, "rows": 0, "errors": []}
    try:
        n, pend, skip_no_tm, errs = ingest.ingest_outsource_xlsm(conn, p)
    except Exception as e:
        return {"ok": False, "note": str(e), "files": 1, "rows": 0, "errors": [str(e)]}
    note = f"{os.path.basename(p)} / {n}건 적재"
    if pend:
        note += f" (검토대기 {pend})"
    if skip_no_tm:
        note += f" (TM-NO없음 {skip_no_tm}건 제외)"
    return {"ok": not errs, "files": 1, "rows": n, "errors": errs, "note": note}


def scan_scrap(conn, root):
    """폐기불량은 root(=scrap_root) 폴더 안에 다른 파일이 섞여 있어도 scrap_data.db 라는
    이름의 파일만 골라서 읽는다(2026-07-31 확정 — 예전엔 폴더 내 가장 최근 .db를 아무거나
    썼는데, 이제 그 폴더에 관련 없는 다른 .db 파일도 있을 수 있어 이름으로 특정한다)."""
    p = os.path.join(root, SCRAP_FILENAME)
    if not os.path.isfile(p):
        return {"ok": False, "note": f"{SCRAP_FILENAME} 파일을 찾을 수 없음", "files": 0, "rows": 0, "errors": []}
    try:
        n, note = ingest.ingest_scrap_db(conn, p)
    except Exception as e:
        return {"ok": False, "note": str(e), "files": 1, "rows": 0, "errors": [str(e)]}
    skip = note.get("수량없음_스킵", 0)
    pend = note.get("검토대기", 0)
    extra = ""
    if skip:
        extra += f" (수량없음 {skip}건 제외)"
    if pend:
        extra += f" (검토대기 {pend})"
    return {"ok": True, "files": 1, "rows": n, "errors": [], "note": f"{SCRAP_FILENAME} / {n}건 적재{extra}"}


SCANNERS = {"defect": scan_defect, "production": scan_production, "outsource": scan_outsource,
            "scrap": scan_scrap}


def scan_one(conn, key, root=None):
    """소스 1개 스캔. 반환: result dict(+ key/label)."""
    root = root or root_for(conn, key)
    label = dict((k, lb) for k, lb, _s in SOURCES).get(key, key)
    fn = SCANNERS.get(key)
    if fn is None:
        return {"key": key, "label": label, "ok": False, "note": "알 수 없는 소스",
                "files": 0, "rows": 0, "errors": []}
    res = fn(conn, root)
    res.update(key=key, label=label)
    _log(conn, f"scan:{key}", root, res["ok"], res["note"])
    return res


def scan_all(conn):
    """전체 소스 스캔. 화면 버튼·스케줄러 공용 진입점. 소스별로 root_for()가 알맞은
    루트(공유 루트 또는 폐기불량 전용 로컬 경로)를 골라 쓴다."""
    results = [scan_one(conn, key) for key, _lb, _s in SOURCES]
    db.set_setting(conn, "last_scan_at", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return results


def main():
    """CLI 진입점(`python -m app.scan`) — Windows 작업 스케줄러에서 새벽 시간에 호출해
    공유 폴더·폐기불량 폴더의 최신 데이터를 DB에 반영한다(2026-07-31 신설)."""
    conn = db.connect()
    try:
        results = scan_all(conn)
        conn.commit()
    finally:
        conn.close()
    ok = True
    for r in results:
        status = "OK" if r["ok"] else "확인필요"
        print(f"[{status}] {r['label']}: {r['note']}")
        if not r["ok"]:
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
