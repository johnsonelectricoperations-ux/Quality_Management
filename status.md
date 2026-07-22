# 진행 상태 (status.md)

> 매 작업 세션 시작 시 이 파일을 먼저 읽는다. 종료(또는 큰 단계마다) 갱신한다.
> 상세 작업 목록은 `task.md`.

## 현재 마일스톤
- **M0: 프로젝트 골격 + 샘플 데이터** (진행 중)

## 최근 로그

### 2026-07-22
- 목표: 시스템 실제 구현 착수. task.md/status.md로 진행 관리 시작.
- 완료:
  - task.md / status.md 생성
- 진행 중:
  - 프로젝트 골격, 샘플 Excel 데이터 생성기
- 다음 할 일:
  - M0 마무리 → M1(DB 스키마 + 마스터 업로드 + 로그인)
- 주의:
  - TM-NO는 전 구간 문자열 처리(날짜 변환 금지)
  - COPQ는 성형공정 배분분 제외 (공정 마스터 COPQ제외여부 플래그)
  - 목표·집계는 FY(4월~익년3월) 기준, 추이 차트는 최근 7개월 롤링

## 실행 방법 (예정)
```
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python scripts/gen_sample.py         # 샘플 Excel 생성
python -m app.seed                   # 샘플 → DB 적재 (개발용)
uvicorn app.main:app --port 5003     # http://localhost:5003
```

## 열려있는 결정/확인 대기
- 마스터 기준단가 단위: '원' 가정 (집계 시 천원 환산) — 실제 단위 확인 필요
- TOP5 불량유형 표기: 상위 3개 태그 (전체/접기 여부 확인 필요)
