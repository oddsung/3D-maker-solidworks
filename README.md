# 3D-maker-solidworks

도면 PDF 에서 치수를 추출하여 SolidWorks API(COM)로 3D 모델을 자동 생성하는 Python 프로젝트입니다.

## 📁 프로젝트 구조

```
SolidWorks/
├── main.py                  # 메인 프로그램: PDF -> 치수 추출 -> SolidWorks 모델링
├── pdf_dimension_parser.py  # PDF 도면에서 치수 추출 (pdfplumber + 규칙/휴리스틱)
├── vessel_spec.py           # 용기 형상 사양 데이터 클래스 (VesselSpec)
├── sw_modeler.py            # SolidWorks COM 으로 쉘/경판/새들/러그 생성
├── requirements.txt
├── P-SK002-002-ME-490-0001 Rev 2.pdf   # 테스트용 도면 (리프팅 배치도)
└── basic_samples/           # SolidWorks API 기초 테스트 스크립트 모음
    ├── draw_line.py, draw_circle.py, towelShell.py, test2.py
    └── extraction.py, dataExtraction.py
```

## 🛠 필수 요구사항
- Windows + SOLIDWORKS 설치 (COM 등록: `SLDWORKS.exe /regserver`)
- Python 3.x
- `pip install -r requirements.txt`

## 🚀 사용법

```bash
# 1) SolidWorks 없이 PDF 파싱 결과만 확인
python main.py "P-SK002-002-ME-490-0001 Rev 2.pdf" --dry-run

# 2) 파싱 후 SolidWorks 에서 모델 생성 및 저장 (기본 C:\Temp\<도면번호>.SLDPRT)
python main.py "P-SK002-002-ME-490-0001 Rev 2.pdf"

# 3) 저장 경로 / 템플릿 지정
python main.py drawing.pdf --out C:/Temp/Vessel.SLDPRT --template "C:/.../Part.prtdot"

# 4) 파싱 값이 틀리거나 누락된 경우 JSON 으로 보정
python main.py drawing.pdf --export-spec spec.json      # 파싱 결과 저장
python main.py drawing.pdf --spec spec.json             # 수정한 값으로 모델링
```

## 🔍 현재 추출하는 치수 (리프팅 배치도 기준)
| 항목 | 추출 방법 |
|---|---|
| O.D | `O.D 3748` 패턴 (회전 문자 자동 보정) |
| 새들 간격 / T.L 오프셋 / T.L~T.L | `(SADDLE TO SADDLE C.L)` 행과 그 아래 행 |
| 리프팅 러그 위치 | C.O.G 기준 좌/우 거리(소수 2개 행) + 좌측 T.L~C.O.G |
| 새들 바닥 폭 / 중심선 높이 | 좌측 단면도(VIEW A-A) 영역의 수평/세로 치수 |
| 전체 길이, 리프팅 중량 | `... = 29340 (APPROX.)`, `LIFTING WEIGHT : ... kg` |

## 🧱 생성되는 형상
1. **Shell+Heads** : 쉘 + 2:1 타원 경판을 하나의 회전(Revolve) 피처로 생성
2. **Saddle_1/2** : 새들 위치에 사각 단면 돌출 (두께는 도면에 없어 기본값 300 mm)
3. **LiftingLug_1/2** : 쉘 상부에 구멍 있는 러그 판 돌출 (크기는 기본값)

도면에 없는 값(새들 두께, 러그 크기, 경판 SF 등)은 `vessel_spec.py` 의 기본값을 사용하며 `--spec` JSON 으로 변경할 수 있습니다.

## ⚠️ 제한 사항
- 현재 파서는 이 리프팅 배치도 양식에 맞춘 규칙 기반이며, 다른 양식은 규칙 추가가 필요합니다.
- 노즐, 플랫폼, 사다리 등은 아직 모델링하지 않습니다.
