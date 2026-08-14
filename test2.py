import win32com.client
import pythoncom
import time

def print_object_info(obj, indent=""):
    """객체의 정보를 출력하는 함수"""
    try:
        # 기본 정보 출력
        print(f"{indent}타입: {type(obj)}")
        
        # 이름 속성이 있는 경우
        try:
            name = obj.Name
            print(f"{indent}이름: {name}")
        except:
            pass
        
        # GetTypeName2 메서드가 있는 경우
        try:
            type_name = obj.GetTypeName2()
            print(f"{indent}타입 이름: {type_name}")
        except:
            pass

    except Exception as e:
        print(f"{indent}[정보 읽기 실패: {str(e)}]")

def main():
    try:
        # COM 객체 초기화
        pythoncom.CoInitialize()

        # SolidWorks 연결
        print("SOLIDWORKS 연결 시도...")
        sw = win32com.client.Dispatch("SldWorks.Application")
        sw.Visible = True
        time.sleep(10)  # UI 로딩 대기
        # 현재 열린 문서 확인
        model = sw.GetFirstDocument()
        print("debug-050", model)
        # 열린 문서가 없으면 새 문서 생성
        if not model:
            print("열린 문서가 없습니다. 새 문서를 생성합니다...")
            template_path = r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2023\templates\파트.prtdot"
            model = sw.NewDocument(template_path, 0, 0, 0)
            
            if not model:
                raise Exception("새 문서 생성 실패")
            
            time.sleep(1)  # 문서 로드 대기

        print("\n=== 문서 정보 ===")
        print_object_info(model)

        # 피처 정보 출력
        print("\n=== 피처 정보 ===")
        feat = model.FirstFeature()
        while feat:
            print("\n- 피처:")
            print_object_info(feat, "  ")
            
            # 하위 피처 확인
            sub_feat = feat.GetFirstSubFeature()
            while sub_feat:
                print("\n  └ 하위 피처:")
                print_object_info(sub_feat, "    ")
                sub_feat = sub_feat.GetNextSubFeature()
            
            feat = feat.GetNextFeature()

        # 평면 정보 출력
        print("\n=== 평면 정보 ===")
        try:
            origin = model.Extension.GetOrigin()
            if origin:
                planes = {
                    "정면": origin.GetFrontPlane(),
                    "윗면": origin.GetTopPlane(),
                    "우측면": origin.GetRightPlane()
                }
                for plane_name, plane in planes.items():
                    if plane:
                        print(f"\n{plane_name}:")
                        print_object_info(plane, "  ")
                    else:
                        print(f"\n{plane_name}: 없음")
            else:
                print("원점 접근 실패")

        except Exception as e:
            print(f"평면 정보 접근 실패: {str(e)}")

        # 스케치 매니저 정보 출력
        print("\n=== 스케치 매니저 정보 ===")
        try:
            sketch_mgr = model.SketchManager
            print_object_info(sketch_mgr)
        except Exception as e:
            print(f"스케치 매니저 접근 실패: {str(e)}")

        print("\n계속하려면 아무 키나 누르세요...")
        input()

    except Exception as e:
        print(f"\n오류 발생: {str(e)}")
    finally:
        try:
            pythoncom.CoUninitialize()
        except:
            pass

if __name__ == "__main__":
    main()
