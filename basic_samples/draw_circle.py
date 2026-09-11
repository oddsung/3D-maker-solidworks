import win32com.client
import pythoncom
import time

def main():
    # COM 객체 초기화
    pythoncom.CoInitialize()

    try:
        # 솔리드웍스 애플리케이션 연결
        print("SOLIDWORKS 연결 시도...")
        swApp = win32com.client.Dispatch("SldWorks.Application")
        swApp.Visible = True
        time.sleep(1)  # UI 로딩 대기

        # 새 파트 문서 생성
        print("새 파트 문서 생성 시도...")
        template_path = r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2023\templates\파트.prtdot"
        swModel = swApp.NewDocument(template_path, 0, 0, 0)    

        if swModel is None:
            print("문서 생성 실패: 올바른 템플릿 이름을 확인하세요.")
            return

        time.sleep(1)  # 문서 로드 대기

        # 정면 평면 선택
        print("정면 선택 시도...")
        boolstatus = swModel.Extension.SelectByID2(
            "정면",  # 한글 버전에서는 "정면"
            "PLANE",
            0, 0, 0,
            False, 0, None, 0
        )

        if not boolstatus:
            print("정면 선택 실패")
            return

        # 스케치 생성
        print("스케치 시작...")
        swSketchMgr = swModel.SketchManager
        swSketchMgr.InsertSketch(True)

        # 원 그리기 (원점에서 반지름 50mm의 원)
        print("원 그리기 시도...")
        radius = 0.05  # 50mm를 미터 단위로 변환
        circle = swSketchMgr.CreateCircle(0, 0, 0, radius)

        if circle:
            print("원이 성공적으로 그려졌습니다.")
        else:
            print("원 그리기 실패")

        # 스케치 종료
        swSketchMgr.InsertSketch(False)

        # 파일 저장
        print("파일 저장 중...")
        save_path = r"C:\Temp\Circle.SLDPRT"
        result = swModel.SaveAs3(save_path, 0, 2)
        
        if result == 0:
            print(f"파일 저장 완료: {save_path}")
        else:
            print("파일 저장 실패")

    except Exception as e:
        print(f"오류 발생: {str(e)}")
    finally:
        # COM 객체 해제
        try:
            pythoncom.CoUninitialize()
        except:
            pass

if __name__ == "__main__":
    main()
