"""
main.py - 도면 PDF -> SolidWorks 3D 모델 자동 생성 (테스트 버전)

사용 예:
  python main.py "P-SK002-002-ME-490-0001 Rev 2.pdf"                 # PDF 파싱 후 SolidWorks 모델링
  python main.py "P-SK002-002-ME-490-0001 Rev 2.pdf" --dry-run       # SolidWorks 없이 파싱 결과만 확인
  python main.py drawing.pdf --out C:/Temp/Vessel.SLDPRT --spec override.json
  python main.py drawing.pdf --export-spec spec.json                  # 파싱 결과를 JSON 으로 저장

흐름:
  1. pdf_dimension_parser 로 PDF 에서 치수 추출 -> VesselSpec
  2. (선택) JSON 으로 값 보정
  3. sw_modeler 로 SolidWorks 파트 생성 (쉘+경판 회전체, 새들, 리프팅 러그)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from pdf_dimension_parser import parse_drawing
from vessel_spec import VesselSpec


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="도면 PDF 를 SolidWorks 3D 모델로 변환")
    p.add_argument("pdf", help="도면 PDF 파일 경로")
    p.add_argument("--out", help="저장할 SLDPRT 경로 (기본: C:/Temp/<도면번호>.SLDPRT)")
    p.add_argument("--spec", help="파싱 결과를 덮어쓸 JSON 파일 (VesselSpec 필드명 사용)")
    p.add_argument("--export-spec", help="파싱된 사양을 JSON 으로 저장할 경로")
    p.add_argument("--template", help="SolidWorks 파트 템플릿(.prtdot) 경로")
    p.add_argument("--dry-run", action="store_true", help="SolidWorks 를 실행하지 않고 파싱 결과만 출력")
    p.add_argument("--no-save", action="store_true", help="모델 생성 후 저장하지 않음")
    return p


def main(argv=None) -> int:
    # Windows 콘솔(cp949)에서도 한글 출력이 깨지지 않도록 UTF-8 로 고정
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = build_arg_parser().parse_args(argv)

    if not os.path.exists(args.pdf):
        print(f"PDF 파일을 찾을 수 없습니다: {args.pdf}")
        return 1

    print(f"=== 1. 도면 파싱: {args.pdf}")
    spec: VesselSpec = parse_drawing(args.pdf)

    if args.spec:
        with open(args.spec, "r", encoding="utf-8") as f:
            overrides = json.load(f)
        spec.apply_overrides(overrides)
        print(f"JSON 보정 적용: {list(overrides.keys())}")

    print()
    print("=== 2. 추출된 용기 사양")
    print(spec.summary())

    problems = spec.validate()
    for msg in problems:
        print("경고:", msg)

    if args.export_spec:
        spec.to_json(args.export_spec)
        print(f"사양 JSON 저장: {args.export_spec}")

    if any("값이 없습니다" in m for m in problems):
        print("필수 치수가 부족하여 모델링을 진행할 수 없습니다. --spec JSON 으로 보정하세요.")
        return 2

    print()
    print("=== 3. 모델링 계획")
    print(f"  - 쉘+경판 회전체: O.D {spec.outer_diameter:g}, T.L {spec.tl_length:g}, 경판 {spec.head_type}")
    print(f"  - 새들 x 위치(T.L 기준): {spec.saddle_positions_from_tl()}")
    print(f"  - 러그 x 위치(T.L 기준): {[round(v, 1) for v in spec.lug_positions_from_tl()]}")

    if args.dry_run:
        print("\n--dry-run: SolidWorks 모델링은 생략합니다.")
        return 0

    out = None
    if not args.no_save:
        out = args.out or os.path.join(r"C:\Temp", f"{spec.drawing_no or 'Vessel'}.SLDPRT")

    print()
    print("=== 4. SolidWorks 모델링")
    from sw_modeler import SolidWorksModeler   # SolidWorks 없는 환경에서 dry-run 이 가능하도록 지연 import
    modeler = SolidWorksModeler(template_path=args.template)
    try:
        modeler.build(spec, save_path=out)
    except Exception as e:
        print(f"모델링 중 오류: {e}")
        import traceback
        traceback.print_exc()
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
