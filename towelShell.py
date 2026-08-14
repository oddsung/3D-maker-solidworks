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
        template_path = r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2023\templates\part.prtdot"
        swModel = swApp.NewDocument(template_path, 0, 0, 0)    

        if swModel is None:
            print("문서 생성 실패: 올바른 템플릿 이름을 확인하세요.")
            return

        time.sleep(1)  # 문서 로드 대기

        # 기본 평면 정보 출력
        print("\n기본 평면 정보 확인 중...")
        
        # 방법 1: Extension을 통한 평면 선택 시도
        print("\n방법 1: SelectByID2로 평면 확인")
        plane_names = ["Front Plane", "Top Plane", "Right Plane", "정면", "윗면", "우측면"]
        for name in plane_names:
            try:
                result = swModel.Extension.SelectByID2(
                    name,       # 이름
                    "PLANE",    # 타입
                    0, 0, 0,    # 좌표
                    False,      # Append 선택
                    0,          # Mark
                    None,       # Callout
                    0           # SelectOption
                )
                print(f"- {name}: {'선택 가능' if result else '선택 불가'}")
                # 선택 해제
                swModel.ClearSelection2(True)
            except Exception as e:
                print(f"- {name}: 오류 발생 ({str(e)})")

        # 방법 2: 원점을 통한 평면 확인
        print("\n방법 2: 원점을 통한 평면 확인")
        try:
            origin = swModel.Extension.GetOrigin()
            if origin:
                print("원점 접근 성공")
                try:
                    front_plane = origin.GetFrontPlane()
                    top_plane = origin.GetTopPlane()
                    right_plane = origin.GetRightPlane()
                    
                    print(f"- Front Plane: {'존재함' if front_plane else '없음'}")
                    print(f"- Top Plane: {'존재함' if top_plane else '없음'}")
                    print(f"- Right Plane: {'존재함' if right_plane else '없음'}")
                except Exception as e:
                    print(f"평면 정보 접근 실패: {str(e)}")
            else:
                print("원점 접근 실패")
        except Exception as e:
            print(f"원점 접근 중 오류: {str(e)}")

        # 방법 3: 활성 문서의 피처 확인
        print("\n방법 3: 활성 문서 정보")
        try:
            active_doc = swApp.ActiveDoc
            if active_doc:
                print(f"- 활성 문서 타입: {active_doc.GetType()}")
                print(f"- 활성 문서 이름: {active_doc.GetTitle()}")
            else:
                print("활성 문서 없음")
        except Exception as e:
            print(f"활성 문서 정보 확인 실패: {str(e)}")

        print("\n계속하려면 아무 키나 누르세요...")
        input()

    except Exception as e:
        print(f"\n오류 발생: {str(e)}")
    finally:
        # COM 객체 해제
        try:
            pythoncom.CoUninitialize()
        except:
            pass

if __name__ == "__main__":
    main()
