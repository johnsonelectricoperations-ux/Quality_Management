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

python scripts/gen_sample.py      # 샘플 Excel 생성 → sample_data/
python -m app.seed                # 샘플 Excel → DB(qms.db) 적재 + FY 목표

uvicorn app.main:app --host 0.0.0.0 --port 5003
# 브라우저: http://<서버IP>:5003
```

샘플 로그인: `admin`/`admin`, `editor`/`editor`, `viewer`/`viewer` (운영 시 반드시 변경).

## 구조

```
app/
  db.py        SQLite 스키마·연결·기본계정
  ingest.py    Excel 적재/검증 (TM-NO 문자 강제·배분기준 파싱)
  calc.py      FY 매핑·정수 배분(최대잉여법)·KPI 집계
  seed.py      샘플 일괄 적재 + FY 목표
  main.py      FastAPI 라우트·인증·대시보드/리포트 조립
  templates/   base·login·dashboard·report_*·masters·target·users·input·review
  static/      qms.css·charts.js(차트 렌더러)·theme.js
scripts/gen_sample.py   샘플 Excel 생성기 (시드 고정)
sample_data/            생성된 샘플 Excel
templates/              업로드용 마스터/트랜잭션 양식(.xlsx)
```

## 핵심 규칙 (요약)

- **TM-NO**: 전 구간 문자열(`1545-01`). Excel에서 날짜로 변환된 값은 업로드 거부.
- **배분**: 불량유형 배분기준(`소결,정형` = 균등 / `정형:70,성형:30` = 비율 / 빈칸 = 발생공정)
  → 품목 라우팅 교집합 → 정수 배분(최대잉여법, 합 보존).
- **COPQ**: Scrap Cost에서 **성형(COPQ제외) 공정 배분분 제외** + Claim 6항목, 분모 = SVP(없으면 생산금액 추정).
- **추이 차트**: 최근 7개월 롤링(당월 우측), FY 경계에서 목표선 계단식.

## 2차 (예정)

외주소재불량 폴더 자동수집 · 폐기불량 DB연동 · 생산실적/SVP/Claim ERP 자동입력(RPA).
