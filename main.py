"""
main.py - 도면 PDF -> SolidWorks 3D 모델 자동 생성

사용 예:
  python main.py "P-SK002-002-ME-490-0001 Rev 2.pdf" --dry-run           # 파싱 결과만 확인
  python main.py "…-0001 Rev 2.pdf" "…-0002 Rev 3 (1of2).pdf" "…-0002 Rev 3 (2of2).pdf"
                                                                          # 여러 도면을 합쳐 모델링
  python main.py drawing.pdf --out C:/Temp/Vessel.SLDPRT --spec override.json
  python main.py drawing.pdf --export-spec spec.json                      # 파싱 결과를 JSON 으로 저장
  python main.py … --no-nozzles                                           # 노즐 제외 (빠른 확인)
  python main.py … --only N2,N8,N24                                       # 지정한 노즐만

흐름:
  1. pdf_dimension_parser 로 PDF 들에서 치수 추출 -> VesselSpec (리프팅 도면 < GA 순으로 병합)
  2. (선택) JSON 으로 값 보정, 도면에 없는 값은 기본값
  3. sw_modeler 로 SolidWorks 파트 생성 (중공 쉘+경판 회전체, 새들, 리프팅 러그, 노즐)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from pdf_dimension_parser import parse_drawings
from vessel_spec import VesselSpec


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="도면 PDF 를 SolidWorks 3D 모델로 변환")
    p.add_argument("pdf", nargs="*", help="도면 PDF 파일 경로 (여러 개 가능: 리프팅 배치도, GA 1/2, GA 2/2)")
    p.add_argument("--set", dest="set_dir",
                   help="도면 세트 폴더: 리프팅/GA 시트를 자동 선택해 파싱하고, 노즐 상세도/전개도로 누락 값을 보완")
    p.add_argument("--out", help="저장할 SLDPRT 경로 (기본: C:/Temp/<도면번호>.SLDPRT)")
    p.add_argument("--spec", help="파싱 결과를 덮어쓸 JSON 파일 (VesselSpec 필드명, nozzles, nozzle_overrides)")
    p.add_argument("--export-spec", help="파싱된 사양을 JSON 으로 저장할 경로")
    p.add_argument("--template", help="SolidWorks 파트 템플릿(.prtdot) 경로")
    p.add_argument("--dry-run", action="store_true", help="SolidWorks 를 실행하지 않고 파싱 결과만 출력")
    p.add_argument("--no-save", action="store_true", help="모델 생성 후 저장하지 않음")
    p.add_argument("--no-nozzles", action="store_true", help="노즐을 모델링하지 않음")
    p.add_argument("--only", help="모델링할 노즐 마크를 쉼표로 지정 (예: N2,N8,N24)")
    return p


def main(argv=None) -> int:
    # Windows 콘솔(cp949)에서도 한글 출력이 깨지지 않도록 UTF-8 로 고정
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = build_arg_parser().parse_args(argv)

    for path in args.pdf:
        if not os.path.exists(path):
            print(f"PDF 파일을 찾을 수 없습니다: {path}")
            return 1
    if not args.pdf and not args.set_dir:
        print("도면 PDF 또는 --set <폴더> 를 지정하세요.")
        return 1

    drawing_set = None
    if args.set_dir:
        from drawing_set import DrawingSet, STATUS_KO   # 도면 세트 분석은 선택 기능이라 지연 import
        print(f"=== 0. 도면 세트 분석: {args.set_dir}")
        drawing_set = DrawingSet.from_folder(args.set_dir, verbose=True)
        args.pdf = list(dict.fromkeys(args.pdf + drawing_set.spec_sources()))
        print(f"  용기 사양 파싱에 쓸 시트: {[os.path.basename(p) for p in args.pdf]}")

    print(f"=== 1. 도면 파싱: {len(args.pdf)} 개")
    spec: VesselSpec = parse_drawings(args.pdf)

    if drawing_set is not None:
        res = drawing_set.resolve(spec, apply=True)
        cnt = {}
        for r in res:
            cnt[r.status] = cnt.get(r.status, 0) + 1
        print("도면 세트 보완/검증: " + ", ".join(f"{STATUS_KO[k]} {v}" for k, v in cnt.items()))
        for r in res:
            if r.status in ("resolved", "conflict", "unresolved"):
                print(f"  - {r.mark} {r.field}: {r.old} -> {r.new} ({r.source}; {r.evidence}) [{r.status_ko}]")

    if args.spec:
        with open(args.spec, "r", encoding="utf-8") as f:
            overrides = json.load(f)
        spec.apply_overrides(overrides)
        spec.resolve()
        print(f"JSON 보정 적용: {list(overrides.keys())}")

    for msg in spec.fill_defaults():
        print("기본값:", msg)

    print()
    print("=== 2. 추출된 용기 사양")
    print(spec.summary())
    print()
    print(spec.nozzle_table())

    problems = spec.validate()
    for msg in problems:
        print("경고:", msg)

    if args.export_spec:
        spec.to_json(args.export_spec)
        print(f"사양 JSON 저장: {args.export_spec}")

    if any("값이 없습니다" in m for m in problems):
        print("필수 치수가 부족하여 모델링을 진행할 수 없습니다. --spec JSON 으로 보정하세요.")
        return 2

    only = [m.strip() for m in args.only.split(",")] if args.only else None
    n_ready = sum(1 for n in spec.nozzles if n.is_ready() and (not only or n.mark in only))
    print()
    print("=== 3. 모델링 계획")
    print(f"  - 쉘+경판 회전체: O.D {spec.outer_diameter:g}, I.D {spec.inner_diameter:g}, "
          f"T.L {spec.tl_length:g}, 경판 {spec.head_type} t{spec.head_t:g}")
    print(f"  - 새들 x 위치(T.L 기준): {spec.saddle_positions_from_tl()}")
    print(f"  - 러그 x 위치(T.L 기준): {[round(v, 1) for v in spec.lug_positions_from_tl()]}, 각도 {spec.lug_angles_deg}")
    print(f"  - 노즐: {'제외' if args.no_nozzles else f'{n_ready} 개'}")

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
        modeler.build(spec, save_path=out, with_nozzles=not args.no_nozzles, only_nozzles=only)
    except Exception as e:
        print(f"모델링 중 오류: {e}")
        import traceback
        traceback.print_exc()
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
