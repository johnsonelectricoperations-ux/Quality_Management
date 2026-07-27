# 시스템 구조 (ARCHITECTURE)

> **이 문서의 목적**: 새 세션/새 담당자가 이 문서 하나만 읽고도 시스템 전체를 파악할 수 있게 한다.
> 코드를 바꿨는데 이 문서와 어긋나면, **이 문서를 같이 고친다.**
>
> 관련 문서 — 요구사항·마일스톤: `plan.md` · 작업 로그: `status.md` · 코딩 규칙: `CLAUDE.md`

---

## 1. 한 장 요약

Johnson Electric Operations의 품질 KPI(Scrap·COPQ·불량율·Claim·Incident)를
**Excel/DB 원천 → SQLite 적재 → 계산 → 웹 대시보드**로 잇는 사내 시스템.

| 항목 | 값 |
|---|---|
| 스택 | Python 3.11 · FastAPI · SQLite · Jinja2 · 순수 JS(SVG 차트, 라이브러리 없음) |
| 포트 | **5003** |
| DB 파일 | `qms.db` (리포지토리 루트, `QMS_DB` 환경변수로 변경 가능) |
| 회계연도(FY) | **4월~익년 3월**. FY 이름 = 종료연도 뒤 2자리 → 2026-07은 **FY27** |
| 파트 | DB 저장값 `VMS PART` / `TM PART` ↔ 화면 표기 **1PART / 2PART** |
| 권한 | 조회자(viewer) / 입력자(editor) / 관리자(admin) |

---

## 2. 전체 데이터 흐름

```mermaid
flowchart LR
  subgraph SRC["원천 데이터"]
    A1["사내 불량 일일시트<br/>(공정별 xlsx)"]
    A2["생산량 xlsx"]
    A3["외주소재<br/>00_sintering_defect.xlsm"]
    A4["폐기불량<br/>scrap_data.db"]
    A5["제품별 단가 Master xlsx"]
    A6["과거 KPI 실적<br/>과거데이터.xlsx"]
  end

  subgraph ING["적재 (app/ingest.py)"]
    I1["TM-NO 정규화<br/>base_tmno()"]
    I2["멱등 적재<br/>batch_key / source 교체"]
    I3["100EA↑ 격리<br/>status=pending"]
  end

  DB[("SQLite<br/>qms.db")]

  subgraph CALC["계산 (app/calc.py)"]
    C1["Masters 로드<br/>제품·라우팅·단가·불량유형"]
    C2["resolve()<br/>불량 → 집계공정 배분"]
    C3["compute_daily()<br/>일 단위 누적"]
    C4["month_kpi() / analyze()"]
  end

  subgraph WEB["웹 (app/main.py)"]
    W1["대시보드"]
    W2["세부지표현황"]
    W3["입력·관리 화면"]
  end

  SRC -->|"① 초기 구축: templates/<br/>app/init_data.py"| ING
  SRC -->|"② 운영: 공유폴더 스캔<br/>app/scan.py"| ING
  ING --> DB --> CALC --> WEB
```

### 2단계 운영 흐름 (중요)

시스템은 **두 가지 경로**로 데이터를 받는다. 목적이 다르므로 섞지 않는다.

| | ① 초기 구축 | ② 운영(신규 수집) |
|---|---|---|
| 모듈 | `app/init_data.py` | `app/scan.py` |
| 입력 위치 | 리포지토리 `templates/` 폴더 | 서버 공유 폴더 `\\carp130001\TheEyesHaveIt\QC_Data\Quality_Data` |
| 실행 방법 | `python -m app.init_data` 또는 `/admin/scan` 화면의 **초기 구축** 버튼 | `/admin/scan` 화면의 **폴더 반영** 버튼 (향후 자동 주기) |
| 횟수 | 최초 1회 | 반복 (멱등) |

> `templates/` 는 초기 구축에만 쓰고, 이후에는 보지 않는다.

### 공유 폴더 구조

```
\\carp130001\TheEyesHaveIt\QC_Data\Quality_Data\
  01_사내불량\{YYYY-MM}\{1PART|2PART}\{공정}\   ← 재귀 스캔, 파일명으로 파트/공정/구분/일자 판단
  02_생산량\{YYYY-MM}\
  03_외주소재\00_sintering_defect.xlsm          ← 고정 파일, 전체 교체
  04_폐기불량\scrap_data.db                      ← 고정 파일, 전체 교체
  05_단가마스터\제품별 단가 Master_*.xlsx        ← 최신 파일 1개 upsert
```

일일시트 파일명 규칙: `1PART_성형_공정불량_20260724.xlsx`

**원칙**: 폴더의 파일은 **읽기만** 한다(수정·이동·삭제 없음).
재적재는 멱등 — 사내불량은 `batch_key`(파트\|공정\|구분\|일자) 단위 교체,
외주/폐기는 `source` 전체 교체, 생산/단가는 upsert.
단, **`reviewed=1` 행(사람이 검토한 100EA 건)은 재스캔해도 보존**한다.

---

## 3. 모듈 지도

```
app/
  db.py         SQLite 스키마·연결·마이그레이션·공정 정의(bucket_of)
  ingest.py     원천 파일 → DB 적재 (파일 형식별 ingest_* 함수 20여 개)
  scan.py       공유 폴더 순회 → ingest 호출 (scan_all이 단일 진입점)
  init_data.py  templates/ 실데이터로 최초 1회 DB 구축
  calc.py       ★ 계산 엔진 — 배분·KPI·세부지표 분석
  main.py       ★ FastAPI 라우트·인증·화면 조립
  seed.py       (데모용) 샘플 Excel 일괄 적재
  setpw.py      비밀번호 변경 CLI
  templates/    Jinja2 화면
  static/       qms.css · charts.js(SVG 차트 렌더러) · theme.js(다크모드)
scripts/
  gen_daily_defect_templates.py   일일 불량 입력시트 양식 생성기
  gen_sample.py                   (데모용) 샘플 Excel 생성기
templates/      초기 구축용 실데이터 + 업로드 양식(.xlsx)
deploy/         Windows 상시구동·백업 배치 (DEPLOY.md 참고)
```

**핵심 파일 2개**: `calc.py`(무엇을 어떻게 계산하는가) + `main.py`(무엇을 어떻게 보여주는가).
수정할 일이 생기면 대부분 이 둘 중 하나다.

---

## 4. 공정 체계 (가장 헷갈리는 부분)

공정에는 **두 층**이 있다. 이걸 섞으면 반드시 버그가 난다.

```
물리공정 7종 (db.PHYS_PROCESSES)        →  집계공정 5종 (db.AGG_PROCESSES)
제품 라우팅·일일시트에 쓰이는 실제 공정      화면·KPI에 표시되는 단위
─────────────────────────────────────────────────────────────
성형 ──────────────────────────────────→  성형
소결 ──────────────────────────────────→  소결
정형 ──────────────────────────────────→  정형
가공 ──────────────────────────────────→  가공
압입 ┐
밴딩 ├─────────────────────────────────→  기타   (화면 표기: "기타후공정")
선별 ┘
```

- 매핑 함수: `db.bucket_of(물리공정) → 집계공정`
- **공정별 불량현황은 집계공정 5종만 표시**한다.
- 화면 표기: `main.PROC_LABEL = {"기타": "기타후공정", "?": "(공정미상)"}`
- `?` = 제품 마스터에 없는 TM-NO라 공정을 특정할 수 없는 경우.
  현재 7건(88EA) 존재 — 제품 마스터에 등록하면 사라진다.

---

## 5. 불량 → 공정 배분 규칙

사내 불량시트는 여러 공정에서 작성하지만, **불량은 "발생한 공정"에 집계**한다.
시트를 어느 공정에서 썼는지가 아니라 **불량유형 마스터의 배분기준**이 우선이다.

```mermaid
flowchart TD
  S["불량 1건<br/>(TM-NO, 불량명, 수량, 시트공정)"] --> Q1{"불량유형 마스터에<br/>발생공정 or 배분기준이<br/>지정돼 있나?"}
  Q1 -->|"예"| R1["마스터 규칙대로 배분<br/>(시트공정 무시)"]
  Q1 -->|"아니오 (공란)"| R2["시트에 기록된 공정에<br/>100% 귀속"]
  R1 --> X["제품 라우팅과 교집합"]
  X --> Y{"교집합이 비었나?"}
  Y -->|"아니오"| Z["largest_remainder()<br/>정수 배분 (소수점 없음)"]
  Y -->|"예"| F["라우팅 첫 공정 폴백<br/>라우팅도 없으면 '?'"]
  R2 --> OUT["[(집계공정, 수량)]"]
  Z --> OUT
  F --> OUT
```

배분기준 표기법 (`defect_type.alloc_rule`):

| 표기 | 의미 |
|---|---|
| `소결,정형` | 두 공정에 **균등** 배분 |
| `정형:70,성형:30` | 비율 배분 |
| 빈칸 또는 `입력공정 100%` | 시트를 작성한 공정에 100% 귀속 |

**주의**: `Masters.defect` 는 `(part, name)` 으로 키잉한다.
파트마다 같은 불량명의 배분기준이 다른 경우가 22건 있어서, 불량명만으로 키잉하면 충돌한다.

### 확정된 계산 규칙 (사용자 확정, 변경 금지)

1. **분모는 공통** — 모든 불량율의 분모는 생산(입고)수량.
2. **외주소재불량·폐기불량은 별도 불량율을 계산하지 않는다.** 불량유형별로 해당 공정의 공정불량 수량에 **합산만** 한다.
3. **비집계 공정(압입·밴딩·선별)의 불량도** 유형별 해당 집계공정에 포함시킨다.
4. **발생일 기준**으로 정리한다.
5. **성형 공정에서 작성한 시트의 데이터는 DB에 반영하지 않는다.**
   (다른 공정 시트에 기록된 "성형 원인" 불량은 정상 반영 — 이 둘은 다른 얘기다.)
6. Scrap Quantity·Scrap Cost = **공정불량 + 셋팅불량** 합산.
7. **COPQ/Scrap에서 성형을 제외하는 규칙은 없다.** (5번의 시트 제외와 혼동 금지)

---

## 6. TM-NO 정규화

여러 파일의 표기 흔들림을 하나의 base 키로 통일한다 (`calc.base_tmno`).

| 규칙 | 예 |
|---|---|
| ① 접미 알파벳 제거 | `598-10A`, `598-10B` → `598-10` |
| ② 접미부 2자리 0채움 | `588-5` → `588-05` |
| ③ 접미부 없으면 `-00` | `1632`, `1632-0` → `1632-00` |

안전장치: 접미부가 숫자가 아니면(Excel이 날짜로 깨뜨린 값 등) 손대지 않고 원본 유지.
TM-NO는 **전 구간 TEXT**로 저장한다(날짜 자동변환 방지).

정규화 규칙이 바뀌면 `db._normalize_tmno()` 마이그레이션이 기존 행을 재정규화한다.

---

## 7. DB 스키마

```mermaid
erDiagram
  product ||--o{ product_route : "라우팅(순서·물리공정)"
  product ||--o{ product_price : "집계공정별 단가"
  product ||--o{ production : "생산수량(분모)"
  product ||--o{ defect_entry : "불량(분자)"
  defect_type ||--o{ defect_entry : "배분기준 제공"
```

| 테이블 | 역할 | 핵심 컬럼 |
|---|---|---|
| `product` | 제품 마스터 | `tm_no`(PK), `name`, `part` |
| `product_route` | 제품별 공정 순서 | `tm_no`, `seq`, `process`(물리공정), `unit_price` |
| `product_price` | 집계공정별 누적단가(원) | `tm_no`, `process`(집계 5종), `unit_price` |
| `defect_type` | 불량유형 마스터 | `part`, `kind`(공정\|셋팅), `process`(발생공정), `name`, `alloc_rule` |
| `defect_entry` | **불량 실적(분자)** | `d`, `tm_no`, `defect_name`, `qty`, `source`, `status`, `batch_key`, `reviewed` |
| `production` | **생산수량(분모)** | `d`, `tm_no`, `qty`, `amount`(천원) |
| `svp` | 월별 SVP(매출) | `ym`, `part`, `amount`(천원) — COPQ/Scrap Cost 분모 |
| `claim` | Claim 6항목 비용 | `ym`, `part`, `item`, `amount` |
| `incident` | Customer Incident | `d`, `part`, `customer` |
| `kpi_actual` | **과거 월 KPI 실적** | `ym`, `part`, `kpi`, `value` — 계산값보다 **우선** |
| `target` | KPI 목표 | `fy`, `part`, `kpi`, `value`, `mon`(0=연간, 1~12=월별) |
| `setting` | 설정(`data_root` 등) | `k`, `v` |
| `upload_log` | 적재 이력 | `ts`, `kind`, `filename`, `ok`, `note` |

**`defect_entry` 특수 컬럼**

- `source`: `direct`(사내) / `outsource`(외주) / `discard`(폐기)
- `status`: `confirmed` / `pending`(100EA↑ 검토대기, KPI에서 일시 제외) / `rejected`(soft-delete)
- `batch_key`: 폴더 재적재 멱등 키 (`파트|공정|구분|일자`)
- `reviewed`: 1이면 사람이 검토한 행 → 재스캔해도 덮어쓰지 않음

---

## 8. KPI 계산식

모든 KPI는 `calc.month_kpi(conn, m, daily, y, mth, part)` 하나에서 나온다.

| KPI | 분자 | 분모 | 단위 |
|---|---|---|---|
| Scrap Cost | 불량수량 × 집계공정 단가 (공정+셋팅) | SVP | % |
| Scrap Quantity | 공정불량 + 셋팅불량 수량 | 생산수량 | % |
| COPQ | Scrap Cost + Claim 6항목 | SVP | % |
| Warranty | Claim 중 Warranty 항목 | — | 천원 |
| 공정불량율 | 공정불량 수량 | 생산수량 | ppm |
| 셋팅불량율 | 셋팅불량 수량 | 생산수량 | ppm |
| Customer Incident | incident 건수 | — | 건 |

- **분모 SVP**: `svp` 테이블 값 우선. 통합 파트는 별도 입력한 합계가 있으면 그것을 쓴다.
  SVP가 없으면 생산금액 합으로 **추정**하고 화면에 추정 표시.
- **`kpi_actual` 우선**: 해당 월의 실적이 입력돼 있으면 계산값을 덮어쓴다.
  (과거 월은 원천 데이터가 없고, 보고된 공식 수치가 기준이므로)
- Claim COPQ 6항목: `calc.CLAIM_COPQ_ITEMS`

---

## 9. 화면 구조

```
개요
  /                          대시보드 — KPI 카드 클릭 시 아래 그래프 전환(막대+목표선)
데이터 입력
  /input/svp                 SVP 입력 (FY 가로 표, 단위 원, 합계 직접 입력)
  /input/claim               Claim 입력
  /input/incident            Customer Incident 관리
  /input/outsource-review    불량 검토(100EA↑) — 승인/수정/삭제  ※대기 건 있을 때만 노출
집계 / 리포트
  /report/detail             ★ 세부지표현황 (아래 상세)
관리
  /admin/products            제품 마스터 (단가 포함) — 추가/수정/삭제
  /admin/processes           공정 관리
  /admin/scan                폴더 반영 — 초기 구축 / 스캔 실행 / 루트 설정     [관리자]
  /masters                   마스터 조회
  /admin/target              목표 관리 (공정·셋팅 불량율은 월별)              [관리자]
  /admin/users               사용자 관리                                      [관리자]
```

구 라우트 `/report/kpi`, `/report/defect` 는 `/report/detail` 로 **리다이렉트**만 한다(옛 링크 호환).

### 세부지표현황 (`/report/detail`)

검색조건 8개 × 분석유형 7종의 조합으로 본다.

- **검색조건**: 파트 · 기간(년월~년월) · 집계단위 · 구분(공정/셋팅) · 공정 · 소스(사내/외주/폐기) · 지표(ppm/EA) · 표시개수(상위 5~20)
- **집계단위**: 월 / 분기 / 년(FY). **주별은 제공하지 않는다** —
  사내불량·생산수량이 월 단위로 수집돼 월말 1일에 몰려 있어 주별 불량율이 왜곡된다.
  일일 입력이 쌓이면 `calc.PERIOD_UNITS` 에 "주"를 추가하면 된다.
- **분석유형**: KPI 종합 / 파트별·공정별·불량유형별·TM-NO별 추이 / 불량유형별·품명별 비중
- **모든 그래프 아래에 분자·분모 소스 데이터 표**를 함께 보여준다(계산 검증용).

계산 진입점은 `calc.analyze(conn, m, kind_of_chart, ...)` 하나.
반환값에 `qty`(분자) · `denom`(분모)가 함께 들어 있어 화면에서 표로 뿌린다.

---

## 10. 차트 렌더러 (`static/charts.js`)

라이브러리 없이 SVG를 문자열로 만들어 `innerHTML` 에 넣는다.
`div.jchart` 의 `data-*` 속성을 읽어 그린다.

| `data-type` | 렌더러 | 쓰임 |
|---|---|---|
| `bar` | `renderBar` | 대시보드·KPI 카드 (막대 + 목표선 + 값 라벨) |
| `multi` | `renderMulti` | 다중 시리즈 꺾은선 (추이 분석) |
| `hbar` | `renderHBar` | 가로 막대 (비중·파레토) |
| `area` | `renderArea` | 영역 그래프 |

**캔버스가 두 종류**다. 좁은 카드용(`W=340, H=150`)과 패널용(`PW=900, PH=300`).
패널 차트에 카드용 좌표계를 쓰면 글자가 3배 이상 확대되므로 반드시 구분한다.
값·이름 라벨 폭은 한글/숫자 글자폭을 실측해 여백을 잡는다(잘림 방지).

`window.renderChart = draw` 로 노출 — 대시보드에서 지표를 바꿀 때 재호출한다.

---

## 11. 알려진 함정 (반복해서 당한 것들)

| 함정 | 증상 | 대응 |
|---|---|---|
| **Jinja `values` 키 충돌** | `<built-in method values>` 출력 | dict 키로 `values` 금지 → **`vseries`** 사용 |
| **Jinja `{% set %}` 스코프** | `'c' is undefined` 500 오류 | `{% set %}`은 블록 스코프. `scripts` 블록에선 원본 참조 |
| **지수표기** | `1.77769e+07` → JS가 못 읽음 | `main._join()` 이 고정소수로 변환 |
| **서버가 옛 코드 사용** | 고쳤는데 안 바뀜 | `--reload` 없으면 **재시작 필수** |
| **curl 한글 깨짐** | `BODY(ìì )` | urllib + `charset=UTF-8` 로 테스트 |
| **Playwright 브라우저 불일치** | 실행 실패 | `executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome"` |
| **상위 N 합산 이름 충돌** | 값 이중 계상 | 합산 항목은 `그 외 N개` (실제 불량유형 '기타'와 구분) |

---

## 12. 실행 · 검증

```bash
# 개발 서버
uvicorn app.main:app --host 0.0.0.0 --port 5003
# 브라우저 http://localhost:5003  (admin/admin)

# 초기 DB 구축 (최초 1회)
python -m app.init_data

# 화면 검증 (JS 오류·라벨 잘림 확인)
#  → Playwright로 로그인 후 각 화면 순회, pageerror/console.error 수집
#  → executable_path 지정 필수
```

**검증 없이 "완료"라고 하지 않는다** (CLAUDE.md §0-4).
화면을 고쳤으면 실제로 띄워서 렌더링과 JS 오류 0을 확인한다.

---

## 13. 현재 미해결 항목

| # | 내용 | 조치 |
|---|---|---|
| 1 | 100EA 검토대기 **46건**이 KPI에서 일시 제외 중 | `/input/outsource-review` 에서 반영/수정/삭제 |
| 2 | `(공정미상)` 88EA — 제품 마스터에 없는 TM-NO 7건<br/>(`1634-01`, `1641-00`, `1752-00`, `588-42`, `798-01`, `1987-01`, `670-02`) | 제품 마스터에 등록하면 정상 분류 |
| 3 | 신규 불량유형 9종이 DB에만 있고 양식 파일 미갱신 | `templates/불량유형마스터_양식.xlsx` 갱신 |
| 4 | 폴더 스캔이 수동 버튼 | 자동 주기 전환 시 `scan.scan_all()` 을 그대로 호출 |
