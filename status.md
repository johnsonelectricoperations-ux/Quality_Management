# 진행 상태 (status.md)

> 매 작업 세션 시작 시 이 파일을 먼저 읽는다. 종료(또는 큰 단계마다) 갱신한다.
> 상세 작업 목록은 `task.md`.

## 현재 마일스톤
- **M0~M7 완료** + **실데이터 반영 착수**. 사용자 실제 파일(templates/)을 하나씩 반영 중.

### 2026-07-24 실데이터 반영 (1) 제품 목록 CSV
- 입력 파일: `templates/TM-NO_List_VMS Part.CSV`(2,675품목), `TM-NO_List_TM Part.CSV`(84품목)
  - 인코딩 CP949, wide 형식. 컬럼: TM-NO·품명·중량·[성형·소결·정형·가공·압입·밴딩·후처리]
  - 파트=파일명, 공정 칸에 값(코드) 있으면 라우팅 포함(좌→우 순서)
- **실제 공정 7종**: 성형·소결·정형·가공·압입·밴딩·후처리 (성형=COPQ 제외 기본)
- 반영:
  - db: product.weight, product_route.op_code, process.ord 컬럼 추가(+마이그레이션),
    db.ensure_processes()/CANON_PROCESSES(7공정)
  - ingest.ingest_product_csv(cp949 wide 파서)
  - 화면 신설: **제품 마스터**(/admin/products: CSV 가져오기·검색·페이징·등록/수정/삭제),
    **공정 관리**(/admin/processes), 메뉴 추가(관리 그룹)
  - calc.process_breakdown → 공정 순서 DB(ord) 기반 동적화
  - 검증: 실제 CSV 2,759품목 import, 화면 렌더(라우팅/코드/중량/COPQ) 정상
- **미확정(다음 파일에서)**: 공정별 **기준단가**(현재 0, Scrap Cost 계산에 필요) —
  공정 칸 코드가 단가인지/작업번호인지 확인 필요. xlsx/xlsm/scrap_data.db 설명 대기.

### 2026-07-24 실데이터 반영 (2) 폐기불량 DB / 외주소재불량 xlsm — 분석·규칙 확정 (구현 전)
- **scrap_data.db** (폐기불량): 테이블 scrap_data 2,593건(2/25~7/24). part=1Part/2Part,
  컬럼 date·process·tmno·scrap_reason·quantity·defect_category('해당'만 363)·defect_process.
  → **defect_category='해당' 만 공정불량율에 반영**. 지정 폴더 자동수집(2차) 예정.
- **00_sintering_defect.xlsm** (외주소재불량): Sheet1(609)+월별시트, 28컬럼.
  컬럼 반입예정일자·업체명·TM-NO·품명·Part·성형불량합계·소결불량합계·기타합계·합계·세부19종.
  → **성형불량합계→성형, 소결불량합계→소결** 공정불량율. **사람이 특정 폴더에 수동 업로드**.
- 확정 규칙(plan §0/실데이터 소스 매핑에 기록):
  · 1Part=VMS PART, 2Part=TM PART
  · 불량율 집계 공정 = **성형·소결·정형·가공 4개만** (양 파트 공통)
  · 폐기·외주소재불량 모두 공정불량(공정불량율)에 합산
- **다음 확인 필요**: xlsm '기타합계' 처리 / 정형·가공 공정불량 데이터 출처 / 공정별 기준단가 출처
- 아직 **구현 안 함**(사용자 "먼저 참고" 요청). 다음 단계에서 ingest(scrap_data.db, xlsm) 설계.

### 2026-07-24 TM-NO 정규화 규칙
- 접미 알파벳 제거해 base로 합쳐 집계 (598-10A→598-10). calc.base_tmno() 추가.
  전 파일 조인/집계 키=base. VMS 96그룹 합쳐짐. 제품마스터=base로 합침 확정(변형행 유지 안함). ingest·저장 시 base 정규화 적용.

### 2026-07-24 실데이터 반영 (3) 생산수량 xlsx + 추가 규칙 확정 (구현 전)
- **생산수량 파일** `구분_시작일_종료일.xlsx` (예: 1part_20260723_20260723.xlsx):
  ERP '제품창고재고금액조회' 내보내기. 헤더 2행, 3행 TOTAL(스킵), 4행~ 데이터.
  · **C열=규격=TM-NO, I열=입고수량=생산수량, J열=입고금액(원)→÷1000=생산금액(천원)**
  · 파일명: 구분(1part/2part)=파트, 시작~종료=생산일자
  · 규격 빈 행/소모품·상품·수량0 행 스킵. production에 파트도 저장(마스터 미등록 규격 대비)
- 추가 확정:
  1. xlsm **기타합계 → 신설 '기타' 공정** 에 배분 (불량율 집계 공정: 성형·소결·정형·가공+기타)
  2. **정형·가공(사내) 공정불량** = 직접입력 또는 Excel 업로드로 별도 입력
  3. **공정별 기준단가** = 별도 초기세팅 파일(일괄) + 제품 등록 폼 공정별 단가 입력(폼 구현됨)
- 남은 소소한 확인: 생산 xlsx 기간(시작≠종료) 처리 / 기준단가 초기세팅 파일 양식
- 다음 단계(구현 예정): ingest_production_xlsx(C/I/J·파일명 파트/일자), ingest_scrap_db('해당' 필터),
  ingest_outsource_xlsm(성형/소결/기타 배분), 단가 초기세팅 import, '기타' 공정 추가,
  불량율 집계공정 플래그(성형·소결·정형·가공·기타)
- 주의: 데모 seed(gen_sample.py)는 아직 옛 4공정 기준 — 실운영은 실제 CSV import 사용.

### M7 배포 (2026-07-22 완료)
- deploy/: setup.bat, run_server.bat, _service.bat, register_tasks.bat(작업 스케줄러),
  backup_db.py/.bat(온라인 백업·30개 보관), .env.example, DEPLOY.md(전체 가이드)
- app/setpw.py: 비밀번호 변경·계정 추가 CLI
- 검증: setpw(변경/추가/목록), backup_db(온라인 백업·복원), python -m app.db/uvicorn 동작
- .gitignore: qms.db, backups/, venv/, __pycache__

## 최근 로그

### 2026-07-22
- 목표: 시스템 실제 구현 (Excel 샘플 → DB → 계산 → 웹).
- 완료:
  - M0 골격 + 샘플 Excel 생성기 (scripts/gen_sample.py, sample_data/)
  - M1 DB 스키마(app/db.py) + Excel 적재/검증(app/ingest.py) + 로그인/3권한
  - M3 배분 계산엔진(app/calc.py) — 정수 배분(찍힘40→소결20/정형20 검증)
  - M4 KPI 집계 — FY 매핑, Scrap Cost/Qty%·COPQ%·불량율 ppm, 분모 SVP/추정
  - M5 대시보드 — KPI 카드·롤링7개월(FY계단목표)·주별·TOP5 (실데이터)
  - M6 리포트(KPI현황·공정별) + 목표관리 + 사용자관리 + 데이터입력 업로드
  - Playwright 검증: 로그인→전화면 200, 차트 실데이터 렌더, JS 오류 0
- 다음 할 일:
  - M7: 포트 5003 상시구동(Windows) 가이드 + DB 백업 스크립트
  - 입력 화면에 '직접입력 폼'(현재는 Excel 업로드 위주) 추가
  - 대시보드 지표 선택 토글(현재 COPQ 고정) 등 UX
- 주의:
  - 실행: python scripts/gen_sample.py → python -m app.seed → uvicorn app.main:app --port 5003
  - 샘플 COPQ가 다소 높음(Claim 샘플값 큼) — 실데이터로 조정됨. 디자인/로직은 정상.
  - 목표 FY는 2자리(26/27) 저장, calc.fy_of는 4자리(2027) → target_val에서 %100 정규화.

## 열려있는 결정/확인 대기
- 마스터 기준단가 단위: '원' 가정 (집계 천원 환산)
- TOP5 불량유형: 상위 3개 태그
- 대시보드 '지표 선택'(COPQ 외) 필요 여부
