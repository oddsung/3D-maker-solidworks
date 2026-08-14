import win32com.client
import pythoncom
import os
import openpyxl
import time

def get_document_type(file_path):
    """파일 확장자를 기반으로 문서 타입을 반환"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.sldprt':
        return 1  # swDocPART
    elif ext == '.sldasm':
        return 2  # swDocASSEMBLY
    elif ext == '.slddrw':
        return 3  # swDocDRAWING
    else:
        return 1  # 기본값으로 PART 반환

def get_custom_properties(model):
    """모델의 커스텀 속성을 가져오는 함수"""
    props = {}
    try:
        if not model:
            print("유효하지 않은 모델입니다.")
            return props

        try:
            # Summary Information 가져오기
            summary_props = {}
            
            # 파일 속성 가져오기
            prps = model.Extension.CustomPropertyManager("")
            if prps:
                # 기본 파일 속성 시도
                for prop_name in ["Title", "Author", "Comments", "Keywords", "Subject"]:
                    try:
                        ret = prps.Get2(prop_name)
                        if ret and ret[0]:  # 값이 존재하면
                            summary_props[prop_name] = ret[1]
                    except:
                        continue
                
                if summary_props:
                    props.update(summary_props)
                    print("Summary Information 추출 완료")
        except Exception as e:
            print(f"Summary Information 읽기 실패: {str(e)}")

        try:
            # 사용자 정의 속성 읽기
            swCustPrpMgr = model.Extension.CustomPropertyManager("")
            if swCustPrpMgr:
                try:
                    # 속성 개수 확인
                    count = swCustPrpMgr.Count
                    print(f"발견된 속성 수: {count}")
                    
                    if count > 0:
                        # 각 속성에 대해 Get2 메서드 사용
                        for i in range(count):
                            try:
                                # 속성 이름 가져오기
                                name = swCustPrpMgr.GetPropertyName(i)
                                if name:
                                    # Get2 메서드로 값 가져오기
                                    ret = swCustPrpMgr.Get2(name)
                                    if ret and ret[0]:  # 값이 존재하면
                                        props[name] = ret[1]
                                        print(f"사용자 정의 속성 추출: {name} = {ret[1]}")
                            except Exception as e:
                                print(f"속성 {i} 읽기 실패: {str(e)}")
                                continue
                except Exception as e:
                    print(f"속성 읽기 실패: {str(e)}")
        except Exception as e:
            print(f"사용자 정의 속성 읽기 실패: {str(e)}")

        try:
            # 물성 정보 추출
            modelExt = model.Extension
            if modelExt:
                try:
                    # 질량 속성
                    massProp = modelExt.CreateMassProperty()
                    if massProp:
                        # 계산 실행
                        status = massProp.Calculate()
                        if status:
                            mass = massProp.Mass
                            volume = massProp.Volume
                            area = massProp.SurfaceArea
                            
                            if mass > 0:
                                props["Mass"] = f"{mass:.6f} kg"
                                props["Volume"] = f"{volume:.6f} m³"
                                props["Surface Area"] = f"{area:.6f} m²"
                                print("물성 정보 추출 완료")
                except Exception as e:
                    print(f"질량 속성 읽기 실패: {str(e)}")

                try:
                    # 재질 정보
                    config = model.GetActiveConfiguration()
                    if config:
                        material = config.GetReferenceProperty("Material")
                        if material:
                            props["Material"] = material
                            print("재질 정보 추출 완료")
                except Exception as e:
                    print(f"재질 정보 읽기 실패: {str(e)}")

        except Exception as e:
            print(f"구성 정보 읽기 실패: {str(e)}")

    except Exception as e:
        print(f"속성 추출 중 오류 발생: {str(e)}")
        import traceback
        traceback.print_exc()

    return props

def extract_solidworks_data_to_excel(sld_file_path, excel_file_path):
    try:
        # COM 객체 초기화
        pythoncom.CoInitialize()

        # SOLIDWORKS Application 연결
        print("SOLIDWORKS 연결 시도...")
        sw_app = win32com.client.Dispatch("SldWorks.Application")
        sw_app.Visible = True
        time.sleep(2)  # UI 로딩 대기 시간 증가

        print("파일 열기 시도:", sld_file_path)
        # 파일 타입 확인
        doc_type = get_document_type(sld_file_path)
        print(f"문서 타입: {doc_type}")

        # 기존 문서가 있다면 모두 닫기
        sw_app.CloseAllDocuments(True)

        # SOLIDWORKS 파일 열기 (수정된 매개변수)
        errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        
        model = sw_app.OpenDoc6(
            sld_file_path,     # 파일 경로
            doc_type,          # 문서 타입
            1,                 # 읽기 전용 옵션
            "",               # 설정 이름
            errors,           # 오류
            warnings         # 경고
        )

        if model is None:
            print(f"파일을 열 수 없습니다. 오류: {errors.value}, 경고: {warnings.value}")
            return

        # 활성 문서 가져오기
        model = sw_app.ActiveDoc
        if not model:
            print("활성 문서를 가져올 수 없습니다.")
            return

        # 모델 정보 가져오기
        print("모델 정보 추출 중...")
        try:
            # 파일 이름에서 제목 추출
            model_title = os.path.splitext(os.path.basename(sld_file_path))[0]
            print(f"모델 제목: {model_title}")
            
            # 문서 타입 확인
            model_type = "Part" if doc_type == 1 else "Assembly" if doc_type == 2 else "Drawing"
            print(f"모델 타입: {model_type}")

            # 기본 정보 수집
            props = {
                "Title": model_title,
                "Type": model_type,
                "Path": sld_file_path
            }

            # 커스텀 속성 읽기
            print("\n커스텀 속성 읽기 시도...")
            custom_props = get_custom_properties(model)
            props.update(custom_props)

            # Excel 파일 생성
            print("\nExcel 파일 생성 중...")
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Model Data"

            # 데이터 쓰기
            ws.append(["Property", "Value"])
            for key, value in props.items():
                ws.append([key, value])

            # Excel 파일 저장
            wb.save(excel_file_path)
            print(f"데이터가 저장됨: {excel_file_path}")

        except Exception as model_error:
            print(f"모델 정보 추출 중 오류: {str(model_error)}")
            raise

        # 문서 닫기
        try:
            sw_app.CloseDoc(model_title)
        except:
            print("문서 닫기 실패")

    except Exception as e:
        print(f"오류 발생: {str(e)}")
        import traceback
        print("상세 오류:")
        traceback.print_exc()
    finally:
        # COM 객체 해제
        try:
            pythoncom.CoUninitialize()
        except:
            pass
        print("처리 완료.")

if __name__ == "__main__":
    # 파일 경로 설정
    # solidworks_file_path = r"C:\Users\doosung.oh\SolidProxy\SolidProxy\TestFiles\cube.SLDPRT"
    solidworks_file_path = r"C:\Users\doosung.oh\SolidProxy\SolidProxy\TestFiles\BODY_NBD40840.SLDPRT"
    output_excel_path = r"C:\Users\doosung.oh\Desktop\solidworks_model_data.xlsx"

    # 파일 존재 여부 확인
    if not os.path.exists(solidworks_file_path):
        print(f"SOLIDWORKS 파일을 찾을 수 없습니다: {solidworks_file_path}")
    else:
        extract_solidworks_data_to_excel(solidworks_file_path, output_excel_path)
