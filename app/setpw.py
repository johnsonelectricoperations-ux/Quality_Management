# -*- coding: utf-8 -*-
"""사용자 비밀번호/계정 관리 CLI (운영용).

비밀번호 변경 : python -m app.setpw <아이디> <새비밀번호>
계정 추가     : python -m app.setpw add <아이디> <이름> <비밀번호> <권한>
                권한 = viewer | editor | admin
목록          : python -m app.setpw list
"""
import sys

from . import db


def main(argv):
    db.init_db()
    conn = db.connect()
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "list":
        for r in conn.execute("SELECT username,name,role FROM users ORDER BY role"):
            print(f"  {r['username']:12s} {r['name']:10s} {r['role']}")
    elif cmd == "add" and len(argv) == 5:
        _, username, name, pw, role = argv
        if role not in ("viewer", "editor", "admin"):
            print("권한은 viewer/editor/admin"); return 1
        conn.execute("INSERT INTO users(username,name,pw_hash,role) VALUES(?,?,?,?)",
                     (username, name, db.hash_pw(pw), role))
        conn.commit()
        print(f"계정 추가: {username} ({role})")
    elif len(argv) == 2:
        username, pw = argv
        cur = conn.execute("UPDATE users SET pw_hash=? WHERE username=?", (db.hash_pw(pw), username))
        conn.commit()
        print(f"비밀번호 변경: {username}" if cur.rowcount else f"계정 없음: {username}")
    else:
        print(__doc__); return 1
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
