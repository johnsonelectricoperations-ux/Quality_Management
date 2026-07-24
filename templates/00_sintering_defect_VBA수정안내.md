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
