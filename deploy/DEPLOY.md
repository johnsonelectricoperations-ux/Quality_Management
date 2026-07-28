# 배포 가이드 (Windows 서버 PC · 포트 5003)

기존 서버 프로그램이 돌고 있는 사내 서버 PC에, **독립 가상환경(venv) + 전용 포트 5003**으로
통합품질관리시스템을 상시 구동한다. 사용자는 사내망에서 브라우저로 `http://<서버IP>:5003` 접속.

> 대상: Windows 10/11 또는 Windows Server. Python 3.10+ 설치 필요.
> 모든 명령은 프로젝트 최상위 폴더(이 저장소를 받은 폴더)에서 실행한다.

---

## 0. 사전 준비

1. **Python 설치 확인**: 명령 프롬프트에서 `python --version` → 3.10 이상.
   - 없으면 python.org에서 설치 (설치 시 "Add Python to PATH" 체크).
2. **소스 배치**: 이 저장소를 서버 PC의 원하는 폴더에 복사 (예: `C:\apps\qms`).
3. **포트 확인**: 5003이 비어 있는지 확인 (기존 서버와 충돌 방지).
   ```
   netstat -ano | findstr :5003
   ```
   결과가 없으면 사용 가능. 이미 쓰고 있으면 `run_server.bat`/`_service.bat`/`.env`의 5003을 다른 포트로 변경.

---

## 1. 설치 (최초 1회)

`deploy\setup.bat`를 **더블클릭**(또는 명령 프롬프트에서 실행).
- venv 생성 → 의존성 설치 → 빈 DB 초기화(기본 계정 생성).

수동으로 하려면:
```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python -m app.db            REM 빈 DB + 기본 계정(admin/editor/viewer)
```

> **PowerShell에서 `activate` 오류가 나면**(ExecutionPolicy 차단): 활성화하지 말고
> venv의 python을 직접 호출하세요 — `venv\Scripts\python.exe scripts\gen_sample.py` 처럼.
> 또는 `cmd`를 쓰거나, 이번 세션만 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` 후 활성화.
> `.bat` 파일들은 PowerShell 정책과 무관하게 실행됩니다(`.\deploy\run_server.bat`).

### 1-1. 데이터 채우기 (운영 흐름)

데이터는 **2단계**로 들어갑니다.

**① 최초 1회 — `templates/` 실데이터로 초기 구축**

저장소의 `templates/` 폴더에 실제 데이터(제품목록·불량유형·단가·생산·외주·폐기)가 들어있습니다.
설치 직후 한 번만 실행해 DB를 채웁니다.

```bat
python -m app.init_data          REM 이미 데이터가 있으면 자동 중단
python -m app.init_data --force  REM 강제 재실행
```

또는 서버 실행 후 웹에서 **관리 → 🔄 폴더 반영 → [⤓ templates 데이터로 초기 구축]** 버튼으로도 됩니다.

**② 이후 계속 — 공유 폴더에서 신규 수집**

이후 신규 데이터는 아래 공유 폴더에 규칙적 파일명으로 저장하고,
웹에서 **관리 → 🔄 폴더 반영 → [▶ 전체 폴더 반영]** 을 누르면 DB에 들어갑니다.

```
\\carp130001\TheEyesHaveIt\QC_Data\Quality_Data\
  01_사내불량\{YYYY-MM}\{1PART|2PART}\{공정}\   ← 일일 불량 양식
  02_생산량\{YYYY-MM}\                          ← ERP 생산수량 xlsx
  03_외주소재\00_sintering_defect.xlsm          ← 덮어쓰기
  04_폐기불량\scrap_data.db                     ← 덮어쓰기
  05_단가마스터\제품별 단가 Master_*.xlsx
```

- 폴더의 파일은 **읽기만** 합니다(수정·이동·삭제 없음).
- 여러 번 반영해도 **중복되지 않습니다**(사내불량=일자·공정 단위 교체, 외주·폐기=전체 교체).
- 초기 구축과 기간이 겹치면 **폴더 쪽이 최신으로 덮어씁니다**.
- 루트 경로는 폴더 반영 화면에서 변경할 수 있습니다(환경변수 `QMS_DATA_ROOT` 도 가능).
- 서비스 계정에 공유 폴더 **읽기 권한**이 필요합니다.

> 수기 입력 소스(SVP·Claim·Customer Incident)만 웹 화면에서 직접 입력/업로드합니다.
> 데모 데이터로 먼저 보고 싶으면: `python scripts\gen_sample.py` → `python -m app.seed`

### 1-2. 관리자 비밀번호 변경 (필수)
```bat
python -m app.setpw admin <새비밀번호>
python -m app.setpw editor <새비밀번호>
python -m app.setpw viewer <새비밀번호>
```
계정 추가: `python -m app.setpw add <아이디> <이름> <비밀번호> <viewer|editor|admin>`

---

## 2. 서버 실행

### 방법 A. 수동 실행 (테스트/임시)
`deploy\run_server.bat` 실행 → 창을 열어둔 동안 구동. 닫으면 중지.

### 방법 B. 상시 구동 — 작업 스케줄러 (권장·간단)
`deploy\register_tasks.bat`를 **관리자 권한으로 실행**:
- `QMS_Server` : 부팅 시 자동 시작 (로그온 없이 SYSTEM 계정으로 실행)
- `QMS_Backup` : 매일 02:00 DB 백업
- `QMS_PriceUpdate` : 매년 4월 1일 03:00, 공정별 단가(성형·소결·정형·가공·기타) 재계산.
  최근 3개월 입고실적 기준으로 다시 산출하고 **그 해 4월 1일부터만 적용**한다(과거 원가는 불변).
  자세한 계산 규칙은 `app/price_calc.py` 상단 주석 참고.

지금 바로 시작:  `schtasks /Run /TN "QMS_Server"`
상태 확인:      `schtasks /Query /TN "QMS_Server"`
중지:           `schtasks /End /TN "QMS_Server"`
제거:           `schtasks /Delete /TN "QMS_Server" /F`

### 방법 C. 상시 구동 — NSSM 서비스 (더 견고, 자동 재시작)
[NSSM](https://nssm.cc) 다운로드 후:
```bat
nssm install QMS "C:\apps\qms\venv\Scripts\python.exe" "-m uvicorn app.main:app --host 0.0.0.0 --port 5003 --workers 1"
nssm set QMS AppDirectory "C:\apps\qms"
nssm set QMS Start SERVICE_AUTO_START
nssm start QMS
```
로그: `nssm set QMS AppStdout C:\apps\qms\logs\server.log` (logs 폴더 미리 생성).

---

## 3. 방화벽 (사내망 접속 허용)

다른 PC에서 접속하려면 5003 인바운드 허용 (관리자 명령 프롬프트):
```bat
netsh advfirewall firewall add rule name="QMS 5003" dir=in action=allow protocol=TCP localport=5003
```
접속 주소: `http://<서버IP>:5003`  (서버 IP는 `ipconfig`로 확인)

---

## 4. 백업 · 복원

- **자동 백업**: `register_tasks.bat`가 매일 02:00에 `deploy\backup_db.bat` 실행 →
  `backups\qms_YYYYMMDD_HHMMSS.db` 생성, 최근 30개 보관.
- **수동 백업**: `deploy\backup_db.bat` (또는 `python deploy\backup_db.py 30`).
  구동 중에도 안전(SQLite 온라인 백업).
- **복원**: 서버 중지 → `backups\`의 원하는 파일을 `qms.db`로 복사 → 서버 시작.
  ```bat
  copy /Y backups\qms_20260722_020000.db qms.db
  ```

> DB(`qms.db`)와 `backups\`는 git에 올리지 않는다(.gitignore). 별도 드라이브 백업 병행 권장.

---

## 5. 업데이트 (새 버전 반영)

```bat
REM 서버 중지 후
git pull                        REM 또는 새 소스 덮어쓰기 (qms.db는 보존!)
venv\Scripts\activate
pip install -r requirements.txt REM 의존성 변동 시
REM 서버 재시작 (작업 스케줄러: schtasks /End 후 /Run, NSSM: nssm restart QMS)
```
DB 스키마는 실행 시 자동 생성/유지(`CREATE TABLE IF NOT EXISTS`)되어 기존 데이터는 보존된다.
업데이트 전 백업을 먼저 받아둔다.

---

## 6. 점검 · 문제 해결

| 증상 | 확인 |
|---|---|
| 접속 안 됨 | 서버 구동 여부(`schtasks /Query`), 포트(`netstat -ano \| findstr :5003`), 방화벽 |
| 포트 충돌 | 5003을 다른 값으로 변경(`_service.bat`/`run_server.bat`/방화벽 규칙 동일하게) |
| 업로드 실패 "TM-NO 날짜" | 원본 Excel의 TM-NO 셀을 텍스트 서식으로 고쳐 재업로드 |
| 배분기준 오류 | 불량유형 마스터의 배분 공정명이 공정명 마스터에 있는지 확인 |
| 디스크 부족 | `backups\` 오래된 파일 정리(보관 개수 축소: `backup_db.py <개수>`) |

헬스체크: 브라우저에서 `http://localhost:5003/login` 이 뜨면 정상.

---

## 7. 운영 체크리스트

- [ ] 기본 비밀번호(admin/editor/viewer) 모두 변경
- [ ] 실제 마스터 3종 업로드 완료
- [ ] FY 목표(목표 관리) 입력
- [ ] 방화벽 5003 허용, 사내 PC에서 접속 확인
- [ ] 자동 백업 동작 확인(`backups\`에 파일 생성)
- [ ] 서버 재부팅 후 자동 시작 확인
