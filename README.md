# 통합품질관리시스템 (Quality Management System)

Johnson Electric Operations 품질 KPI(Scrap·COPQ·불량율·Claim·Incident)를
Excel 데이터로부터 집계·시각화하는 사내 웹 시스템.

- 스택: Python + FastAPI + SQLite + Jinja2, 포트 **5003**
- 회계연도(FY) 기준: **4월~익년 3월** (FY27 = 2026.4~2027.3)
- 권한 3단계: 조회자 / 입력자 / 관리자
- 설계 문서: `plan.md` · 디자인: `docs/design/` · 진행: `task.md`, `status.md`

## 설치 · 실행 (개발/데모)

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

python -m app.init_data           # templates/ 실데이터 → DB(qms.db) 최초 구축 (1회)

uvicorn app.main:app --host 0.0.0.0 --port 5003
# 브라우저: http://<서버IP>:5003
```

로그인: `admin`/`admin`, `editor`/`editor`, `viewer`/`viewer` (운영 시 반드시 변경).

> 코드를 고친 뒤에는 **서버를 재시작**해야 반영된다(`--reload` 미사용 시).

### 데이터 수집 2단계

| | ① 초기 구축 | ② 운영(신규 수집) |
|---|---|---|
| 입력 위치 | 리포지토리 `templates/` | 서버 공유 폴더 `\\carp130001\...\Quality_Data` |
| 실행 | `python -m app.init_data` 또는 `/admin/scan` 의 **초기 구축** | `/admin/scan` 의 **폴더 반영** (향후 자동 주기) |
| 횟수 | 최초 1회 | 반복 (멱등) |

공유 폴더의 파일은 **읽기만** 한다(수정·이동·삭제 없음).

## 화면

| 구분 | 경로 | 내용 |
|---|---|---|
| 개요 | `/` | 대시보드 — KPI 카드 클릭 시 그래프 전환(막대+목표선) |
| 입력 | `/input/svp` · `/input/claim` · `/input/incident` | SVP·Claim·Customer Incident |
| 입력 | `/input/outsource-review` | 불량 검토(100EA↑) — 승인/수정/삭제 |
| 리포트 | `/report/detail` | **세부지표현황** — 검색조건 8개 × 분석유형 7종, 그래프 + 분자·분모 소스표 |
| 관리 | `/admin/products` · `/admin/processes` · `/masters` | 제품 마스터(단가 포함) · 공정 · 마스터 조회 |
| 관리 | `/admin/scan` · `/admin/target` · `/admin/users` | 폴더 반영 · 목표 · 사용자 (관리자) |

## 구조

```
app/
  db.py         SQLite 스키마·연결·마이그레이션·공정 정의(bucket_of)
  ingest.py     원천 파일 → DB 적재 (TM-NO 정규화·배분기준 파싱·멱등 적재)
  scan.py       서버 공유 폴더 순회 → ingest 호출 (운영 수집 진입점)
  init_data.py  templates/ 실데이터로 최초 1회 DB 구축
  calc.py       ★ 계산 엔진 — FY 매핑·정수 배분·KPI·세부지표 분석
  main.py       ★ FastAPI 라우트·인증·화면 조립
  seed.py       (데모용) 샘플 일괄 적재
  setpw.py      비밀번호 변경 CLI
  templates/    Jinja2 화면
  static/       qms.css·charts.js(SVG 차트 렌더러)·theme.js
scripts/        일일 불량 입력시트 양식 생성기 · (데모용) 샘플 Excel 생성기
templates/      초기 구축용 실데이터 + 업로드 양식(.xlsx)
deploy/         Windows 상시구동·백업 배치
docs/           ARCHITECTURE.md(시스템 구조) · design/(디자인 토큰·시안)
```

> **전체 구조·계산규칙·DB·화면은 `docs/ARCHITECTURE.md` 에 정리돼 있다.** 먼저 읽는 것을 권장.

## 핵심 규칙 (요약)

- **TM-NO**: 전 구간 문자열. `598-10A`→`598-10`, `588-5`→`588-05`, `1632`→`1632-00` 로 정규화.
- **공정 2층 구조**: 물리공정 7종(라우팅용) → `bucket_of()` → **집계공정 5종**(성형·소결·정형·가공·기타).
  화면·KPI는 집계공정 5종만 쓴다. '기타'는 화면에 "기타후공정"으로 표기.
- **배분**: 불량유형 배분기준(`소결,정형` = 균등 / `정형:70,성형:30` = 비율 / 빈칸 = 입력공정 100%)
  → 품목 라우팅 교집합 → 정수 배분(최대잉여법, 합 보존, 소수점 없음).
- **Scrap**: 수량·비용 모두 **공정불량 + 셋팅불량** 합산. 분모는 생산수량(수량) / SVP(비용).
- **COPQ**: Scrap Cost + Claim 6항목, 분모 = SVP(없으면 생산금액 추정).
  ※ **COPQ/Scrap에서 특정 공정을 제외하는 규칙은 없다.**
- **외주·폐기**: 별도 불량율을 계산하지 않고 해당 공정의 공정불량 수량에 **합산만** 한다.
- **추이 차트**: 최근 7개월 롤링(당월 우측), FY 경계에서 목표선 계단식.
- **집계 단위**: 월/분기/년(FY). 주별은 원천이 월 단위 수집이라 제공하지 않는다.

## 배포 (사내 서버 PC · Windows)

상시 구동·백업·업데이트 절차는 **`deploy/DEPLOY.md`** 참고. 요약:

```bat
deploy\setup.bat            REM venv + 의존성 + 빈 DB
deploy\register_tasks.bat   REM (관리자) 부팅 자동시작 + 매일 백업  ← 상시 구동
REM 수동 실행: deploy\run_server.bat  /  백업: deploy\backup_db.bat
python -m app.setpw admin <새비밀번호>   REM 기본 비밀번호 변경(필수)
```

방화벽 5003 인바운드 허용 후 사내망에서 `http://<서버IP>:5003` 접속. 기존 서버 프로그램과 독립 venv/포트로 공존.

## 2차 (예정)

외주소재불량 폴더 자동수집 · 폐기불량 DB연동 · 생산실적/SVP/Claim ERP 자동입력(RPA).
