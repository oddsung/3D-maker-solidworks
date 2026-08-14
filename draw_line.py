import win32com.client
import pythoncom

def create_box(width, height, depth):
    # COM 객체 초기화
    pythoncom.CoInitialize()

    # 솔리드웍스 애플리케이션 연결
    swApp = win32com.client.Dispatch("SldWorks.Application")
    swApp.Visible = True

    # 새 파트 문서 생성
    print("새 파트 문서 생성 시도...")
    # 템플릿 경로를 명시적으로 지정하여 새 파트 파일 생성
    template_path = r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2023\templates\파트.prtdot"
    swModel = swApp.NewDocument(template_path, 0, 0, 0)    

    if swModel is None:
        print("문서 생성 실패: 올바른 템플릿 이름을 확인하세요.")
        return

    # 스케치 생성
    swSketchMgr = swModel.SketchManager
    swSketchMgr.InsertSketch(True)

    # 선 그리기
    # swSketchMgr.CreateLine(0, 0, 0, 0.3, 0.1, 0)

    # 중심 사각형 생성
    swSketchMgr.CreateCenterRectangle(0, 0, 0, width / 2, height / 2, 0)
    swModel.ClearSelection2(True)

    # 스케치 종료
    swModel.ClearSelection2(True)

    print("성공적으로 그려졌습니다.")

    # COM 객체 해제
    pythoncom.CoUninitialize()

if __name__ == "__main__":
    width = 100  # 박스 너비 (mm)
    height = 50  # 박스 높이 (mm)
    depth = 20  # 박스 깊이 (mm)

    create_box(width, height, depth)