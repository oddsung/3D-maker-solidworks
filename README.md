# 3D-maker-solidworks

SolidWorks API(COM)를 활용한 3D CAD 모델링 및 도면/설계 데이터 추출 자동화 Python 프로젝트입니다.

## 📌 주요 기능
- **SolidWorks COM API 연동**: SolidWorks 애플리케이션 자동 제어
- **CAD 모델 데이터 추출 (`extraction.py`, `dataExtraction.py`)**: 부품/어셈블리 속성, 재질 및 형상 데이터 추출
- **3D 형상 자동 생성 (`draw_line.py`, `draw_circle.py`, `towelShell.py`)**: 선, 원, 쉘 등 기본 및 복합 피처 생성 자동화

## 🛠 기술 스택
- Python 3.x
- `pywin32` (`win32com.client`, `pythoncom`)
- SOLIDWORKS API (COM)

## 🚀 시작하기

### 1. 필수 요구사항
- Windows OS
- SOLIDWORKS 설치 및 COM 등록 (`SLDWORKS.exe /regserver`)
- Python 3.x

### 2. 패키지 설치
```bash
pip install pywin32 openpyxl pandas
```

### 3. 실행 예시
```bash
python extraction.py
```
