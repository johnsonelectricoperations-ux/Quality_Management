# -*- coding: utf-8 -*-
"""서버 공유 폴더 스캔 → DB 적재.

폴더 구조 (루트 = setting 'data_root', 기본 \\carp130001\\...\\Quality_Data):
    01_사내불량\\{YYYY-MM}\\{1PART|2PART}\\{공정}\\   (재귀, 파일명으로 판단)
    02_생산량\\{YYYY-MM}\\
    03_외주소재\\00_sintering_defect.xlsm            (고정 파일 덮어쓰기)
    04_폐기불량\\scrap_data.db                        (고정 파일 덮어쓰기)
    05_단가마스터\\제품별 단가 Master_*.xlsx

원칙
- 폴더의 파일은 **읽기만** 한다(수정·이동·삭제 없음).
- 재적재는 멱등: 사내불량=batch_key, 외주/폐기=source 전체 교체, 생산/단가=upsert.
- 화면(수동 버튼)과 향후 스케줄러가 **같은 scan_all()** 을 호출한다.
"""
import os
import datetime

from . import db, ingest

DEFAULT_ROOT = r"\\carp130001\TheEyesHaveIt\QC_Data\Quality_Data"
SETTING_ROOT = "data_root"

SUB_DEFECT = "01_사내불량"
SUB_PRODUCTION = "02_생산량"
SUB_OUTSOURCE = "03_외주소재"
SUB_SCRAP = "04_폐기불량"
SUB_PRICE = "05_단가마스터"

SOURCES = [
    ("defect", "사내불량", SUB_DEFECT),
    ("production", "생산량", SUB_PRODUCTION),
    ("outsource", "외주소재", SUB_OUTSOURCE),
    ("scrap", "폐기불량", SUB_SCRAP),
    ("price", "단가마스터", SUB_PRICE),
]


def data_root(conn):
    return db.get_setting(conn, SETTING_ROOT, "") or os.environ.get("QMS_DATA_ROOT", DEFAULT_ROOT)


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
    files, rows, detail = ingest.ingest_daily_defect_folder(conn, folder)
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
        n, pend, errs = ingest.ingest_outsource_xlsm(conn, p)
    except Exception as e:
        return {"ok": False, "note": str(e), "files": 1, "rows": 0, "errors": [str(e)]}
    return {"ok": not errs, "files": 1, "rows": n, "errors": errs,
            "note": f"{os.path.basename(p)} / {n}건 적재" + (f" (검토대기 {pend})" if pend else "")}


def scan_scrap(conn, root):
    paths = _find_files(os.path.join(root, SUB_SCRAP), (".db",), recursive=False)
    p = _newest(paths)
    if not p:
        return {"ok": False, "note": "대상 파일 없음", "files": 0, "rows": 0, "errors": []}
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
    return {"ok": True, "files": 1, "rows": n, "errors": [], "note": f"{os.path.basename(p)} / {n}건 적재{extra}"}


def scan_price(conn, root):
    paths = _find_files(os.path.join(root, SUB_PRICE), (".xlsx", ".xlsm"), recursive=False)
    p = _newest(paths)
    if not p:
        return {"ok": False, "note": "대상 파일 없음", "files": 0, "rows": 0, "errors": []}
    try:
        n, note = ingest.ingest_price_master(conn, p)
    except Exception as e:
        return {"ok": False, "note": str(e), "files": 1, "rows": 0, "errors": [str(e)]}
    if note.get("error"):
        return {"ok": False, "files": 1, "rows": 0, "errors": [note["error"]], "note": note["error"]}
    return {"ok": True, "files": 1, "rows": n, "errors": [],
            "note": f"{os.path.basename(p)} / {n}품목 적재"}


SCANNERS = {"defect": scan_defect, "production": scan_production, "outsource": scan_outsource,
            "scrap": scan_scrap, "price": scan_price}


def scan_one(conn, key, root=None):
    """소스 1개 스캔. 반환: result dict(+ key/label)."""
    root = root or data_root(conn)
    label = dict((k, lb) for k, lb, _s in SOURCES).get(key, key)
    fn = SCANNERS.get(key)
    if fn is None:
        return {"key": key, "label": label, "ok": False, "note": "알 수 없는 소스",
                "files": 0, "rows": 0, "errors": []}
    res = fn(conn, root)
    res.update(key=key, label=label)
    _log(conn, f"scan:{key}", root, res["ok"], res["note"])
    return res


def scan_all(conn, root=None):
    """전체 소스 스캔. 화면 버튼·스케줄러 공용 진입점."""
    root = root or data_root(conn)
    results = [scan_one(conn, key, root) for key, _lb, _s in SOURCES]
    db.set_setting(conn, "last_scan_at", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return results
