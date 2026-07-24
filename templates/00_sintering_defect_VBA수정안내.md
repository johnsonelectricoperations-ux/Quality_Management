# 외주소재불량 xlsm — TM-NO 날짜 오인식 수정 (VBA)

## 증상
`00_sintering_defect.xlsm` 를 열면 매크로가 구글드라이브 CSV를 가져오는데,
이때 TM-NO(`598-10`, `6017-01AJ` 등)가 **날짜로 자동 변환**되는 경우가 있다.

## 원인
`Module1 > Final_Complete_Process_With_Placement` 의 CSV 가져오기(`QueryTables.Add`)가
**열 데이터 형식을 지정하지 않아** Excel이 각 열을 자동 판별 → 숫자-숫자 형태의 TM-NO가 날짜가 됨.

## 해결 — 가져올 때 모든 열을 '텍스트'로 강제
이 매크로는 불량 수량을 `GetV() = Val(...)` 로 합산하므로, 텍스트로 가져와도 계산에 문제 없다.
날짜는 2번 열(반입예정일자)만 쓰므로 그 열만 일반서식으로 두면 된다.

### 적용 방법
1. xlsm 파일 열기 → **Alt + F11** (VBA 편집기)
2. 왼쪽 프로젝트 창에서 **Module1** 더블클릭
3. 아래 `With mainWs.QueryTables.Add(...)` 블록을 찾아, `.Refresh` **앞에** 표시된 부분을 추가
4. 저장(Ctrl+S) 후 매크로 다시 실행(또는 파일 다시 열기)

### 수정 전
```vba
With mainWs.QueryTables.Add(Connection:="TEXT;" & csvURL, Destination:=mainWs.Range("A1"))
    .TextFilePlatform = 65001 ' UTF-8 설정
    .TextFileParseType = xlDelimited
    .TextFileCommaDelimiter = True
    .Refresh BackgroundQuery:=False
End With
```

### 수정 후
```vba
With mainWs.QueryTables.Add(Connection:="TEXT;" & csvURL, Destination:=mainWs.Range("A1"))
    .TextFilePlatform = 65001 ' UTF-8 설정
    .TextFileParseType = xlDelimited
    .TextFileCommaDelimiter = True

    ' ── TM-NO 날짜 자동변환 방지: 모든 열을 '텍스트'로 가져오기 ──
    Dim colTypes() As Variant, k As Long
    ReDim colTypes(1 To 60)
    For k = 1 To 60
        colTypes(k) = 2          ' 2 = xlTextFormat (텍스트 강제)
    Next k
    colTypes(2) = 1              ' 1 = xlGeneralFormat (반입예정일자는 날짜로 인식)
    .TextFileColumnDataTypes = colTypes

    .Refresh BackgroundQuery:=False
End With
```

## 참고
- 만약 반입예정일자(2번 열)가 날짜로 안 읽히면 `colTypes(2) = 1` 을 `colTypes(2) = 5`(xlYMDFormat)로 바꾼다.
- 근본 원인이 **구글 시트 원본**에서 TM-NO가 이미 날짜로 저장된 경우라면, 원본 시트의 해당 열 서식을
  '일반 텍스트'로 바꿔야 한다(위 VBA는 Excel 가져오기 단계의 변환만 막는다).
- 시스템(QMS) 쪽은 업로드 시 TM-NO가 날짜(datetime)면 거부하고, 정상 값은 base(접미 알파벳 제거)로 정규화한다.

> 이 저장소 환경에서는 컴파일된 VBA(vbaProject.bin)를 직접 편집할 수 없어, 코드 수정안만 제공한다.
> 실제 반영은 위 절차대로 VBA 편집기에서 수동 적용해야 한다.

---

# 추가 개선 (동작·년도 처리)

현재 매크로는 **열 때마다 구글 CSV 전체를 다시 받아** 월별 시트를 전부 삭제 후 재생성한다.
데이터가 적어(~600행) 성능 문제는 없지만, 년도 처리에 2가지 허점이 있어 아래를 권장한다.
(QMS는 Sheet1 전체를 읽어 날짜/FY로 직접 집계하므로, xlsm 내부 월별 분할은 시스템에 영향 없음.)

## 개선 1) 년도 무관 정리 패턴 (2030년 이후 대비) — 권장
`Like "202*-*월"` 은 2020~2029만 매칭한다 → **2030년이 되면 옛 시트가 안 지워져 중복**된다.
매크로에 나오는 **모든** `"202*-*월"` 을 `"####-*월"` 로 바꾼다 (`#`=숫자 1자리, 4자리 년도 자동 매칭).

- 위치 3곳:
  - 월별 시트 삭제 루프:      `If sh.Name Like "202*-*월" ...`
  - 열 삭제 루프:            `If (sh.Name Like "202*-*월" Or ...)`
  - 서식 적용 루프:          `If (sh.Name Like "202*-*월" Or ...)`
- 예) `If sh.Name Like "####-*월" And sh.Name <> "itemlist" Then sh.Delete`

## 개선 2) 월 0채움 (시트 정렬 정상화) — 권장
`monthName = Year(rowDate) & "-" & Month(rowDate) & "월"` 은 `2026-7월`, `2026-10월` 처럼
자릿수가 달라 시트가 1월,10월,11월,2월… 순으로 섞인다.

### 수정 전
```vba
monthName = Year(rowDate) & "-" & Month(rowDate) & "월"
```
### 수정 후
```vba
monthName = Year(rowDate) & "-" & Format(Month(rowDate), "00") & "월"
```
→ `2026-07월` 형태로 정렬이 정상화된다. (개선 1의 `"####-*월"` 패턴과 함께 쓰면 됨)

## 개선 3) (선택) 이전월은 재정렬하지 않고 '당월'만 갱신
"이전월 데이터는 다시 정렬할 필요 없다"를 반영하려면 아래처럼 바꾼다.
⚠ 트레이드오프: **구글 원본에서 이전월 데이터가 수정돼도 그 달 시트는 갱신되지 않는다.**
(당월만 최신화. 이전월을 강제 갱신하려면 해당 시트를 지우고 다시 열면 됨.)

### (a) 상단의 "월별 시트 전체 삭제" 블록을 '당월 시트만 삭제'로 교체
```vba
' [교체] 기존: 모든 202*-*월 시트 삭제 → 당월 시트만 삭제
Dim curMonth As String
curMonth = Year(Date) & "-" & Format(Month(Date), "00") & "월"
Application.DisplayAlerts = False
For Each sh In ThisWorkbook.Worksheets
    If sh.Name = curMonth Then sh.Delete
Next sh
Application.DisplayAlerts = True
```

### (b) 분할 루프에서 '당월 행'만 시트로 복사
```vba
If IsDate(rowDate) Then
    monthName = Year(rowDate) & "-" & Format(Month(rowDate), "00") & "월"
    If monthName = curMonth Then          ' ← 당월만 처리
        Set targetWs = Nothing
        On Error Resume Next
        Set targetWs = ThisWorkbook.Sheets(monthName)
        On Error GoTo 0
        If targetWs Is Nothing Then
            Set targetWs = ThisWorkbook.Sheets.Add(After:=ThisWorkbook.Sheets(ThisWorkbook.Sheets.Count))
            targetWs.Name = monthName
            mainWs.Rows(1).Copy Destination:=targetWs.Rows(1)
        End If
        mainWs.Rows(i).Copy Destination:=targetWs.Cells(targetWs.Cells(Rows.Count, 1).End(xlUp).Row + 1, 1)
    End If
End If
```

### (c) ⚠ 반드시 함께: 뒤쪽 '열 삭제/서식' 루프도 당월로 제한
매크로 후반부의 두 루프는 `Like "####-*월"` 인 **모든 월 시트**를 다시 처리한다.
그중 **열 삭제 루프**(`sh.Columns(10).Delete` / `sh.Columns(1).Delete`)가 문제인데,
개선 3으로 이전월 시트를 남겨두면 **이미 열이 지워진 이전월 시트에서 열을 또 삭제** → 데이터가 밀려 깨진다.
따라서 두 루프의 조건을 **당월 시트 + 메인 시트로만** 제한해야 한다:
```vba
' 기존:  If (sh.Name Like "####-*월" Or sh.Name = mainWs.Name) And sh.Name <> "itemlist" Then
' 변경:  If (sh.Name = curMonth Or sh.Name = mainWs.Name) And sh.Name <> "itemlist" Then
```
(열 삭제 루프와 서식 적용 루프 **둘 다** 이렇게 바꾼다.)

> 참고: Sheet1(메인 시트)은 어차피 전체가 재생성되므로, **QMS 연동만 쓸 거면 개선 3은 불필요**하다.
> (월별 시트는 사람이 보기용. 시스템은 Sheet1을 읽음.) 개선 3은 위 (a)(b)(c)를 **모두** 정확히
> 반영해야 하고, 실수 시 이전월 데이터가 깨질 수 있어 **권장하지 않는다.**

## 요약 권장
- **꼭 반영**: 개선 1(년도 무관 패턴) — 2030년 중복 방지.
- **권장**: 개선 2(월 0채움) — 시트 정렬.
- **비권장(선택)**: 개선 3(당월만 갱신) — (a)(b)(c) 모두 정확히 해야 하고 이전월 손상 위험.
  실익도 작음(Sheet1은 어차피 전체 재생성, 재정렬 비용 미미). 되도록 개선 1·2만 반영 권장.
