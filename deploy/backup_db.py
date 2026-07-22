# -*- coding: utf-8 -*-
"""DB 온라인 백업 (구동 중에도 안전). 보관 개수 초과분 자동 삭제.

사용: python deploy/backup_db.py [보관개수(기본 30)]
백업 위치: backups/qms_YYYYMMDD_HHMMSS.db
"""
import os
import sys
import glob
import sqlite3
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("QMS_DB", os.path.join(ROOT, "qms.db"))
BACKUP_DIR = os.path.join(ROOT, "backups")
KEEP = int(sys.argv[1]) if len(sys.argv) > 1 else 30


def main():
    if not os.path.exists(DB):
        print(f"[backup] DB 없음: {DB}")
        return 1
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"qms_{stamp}.db")
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)          # 온라인 백업 (락 없이 일관성 보장)
    dst.close()
    src.close()
    size = os.path.getsize(dest) / 1024
    print(f"[backup] 생성: {dest} ({size:,.0f} KB)")

    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "qms_*.db")))
    for old in files[:-KEEP] if len(files) > KEEP else []:
        os.remove(old)
        print(f"[backup] 오래된 백업 삭제: {os.path.basename(old)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
