# 진행 상태 (status.md)

> 매 작업 세션 시작 시 이 파일을 먼저 읽는다. 종료(또는 큰 단계마다) 갱신한다.
> 상세 작업 목록은 `task.md`.

## 현재 마일스톤
- **M1~M6 완료** (백엔드 + 웹 앱 동작·검증). 다음: **M7 배포 가이드** + 입력 화면 직접입력 폼 보강.

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
