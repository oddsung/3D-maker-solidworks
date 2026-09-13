"""
sw_modeler.py - VesselSpec 을 바탕으로 SolidWorks 에서 3D 파트를 생성한다.

좌표계 (SolidWorks 파트 기준, 단위 m):
  X : 용기 축 방향 (좌측 T.L 이 x=0, 우측 T.L 이 x=tl_length)
  Y : 상하 (위가 +)
  Z : 용기 축 직각 수평 방향
스케치 평면:
  정면(Front Plane, XY) - 쉘 회전 프로파일, 새들, 러그
"""
from __future__ import annotations

import math
import os
import time
from typing import Optional

from vessel_spec import VesselSpec

try:
    import pythoncom
    import win32com.client
except ImportError:  # SolidWorks 가 없는 PC 에서도 import 는 되도록
    pythoncom = None
    win32com = None

MM = 1.0 / 1000.0   # mm -> m

# SolidWorks 상수
swDocPART = 1
swDefaultTemplatePart = 8          # swUserPreferenceStringValue_e
swEndCondBlind = 0                 # swEndConditions_e
swEndCondMidPlane = 6
swSaveAsCurrentVersion = 0
swSaveAsOptions_Silent = 1

PLANE_NAME_CANDIDATES = {
    "front": ["Front Plane", "정면", "Front"],
    "top": ["Top Plane", "윗면", "Top"],
    "right": ["Right Plane", "우측면", "Right"],
}


class SolidWorksModeler:
    def __init__(self, template_path: Optional[str] = None, visible: bool = True):
        if win32com is None:
            raise RuntimeError("pywin32 가 설치되어 있지 않습니다. pip install pywin32")
        self.template_path = template_path
        self.visible = visible
        self.app = None
        self.model = None
        self.sketch_mgr = None
        self.feat_mgr = None

    # ------------------------------------------------------------------
    def connect(self) -> None:
        pythoncom.CoInitialize()
        print("SOLIDWORKS 연결 중...")
        self.app = win32com.client.Dispatch("SldWorks.Application")
        self.app.Visible = self.visible
        time.sleep(1)

    def _resolve_template(self) -> str:
        if self.template_path and os.path.exists(self.template_path):
            return self.template_path
        # 사용자 기본 파트 템플릿
        try:
            t = self.app.GetUserPreferenceStringValue(swDefaultTemplatePart)
            if t and os.path.exists(t):
                return t
        except Exception:
            pass
        candidates = [
            r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2023\templates\파트.prtdot",
            r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2023\templates\Part.prtdot",
            r"C:\ProgramData\SolidWorks\SOLIDWORKS 2023\templates\Part.PRTDOT",
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        raise FileNotFoundError("파트 템플릿을 찾을 수 없습니다. --template 옵션으로 지정하세요.")

    def new_part(self) -> None:
        template = self._resolve_template()
        print(f"새 파트 생성: {template}")
        self.model = self.app.NewDocument(template, 0, 0, 0)
        if self.model is None:
            raise RuntimeError("새 파트 문서 생성 실패")
        self.sketch_mgr = self.model.SketchManager
        self.feat_mgr = self.model.FeatureManager
        time.sleep(0.5)

    # ------------------------------------------------------------------
    def _select_plane(self, key: str) -> None:
        for name in PLANE_NAME_CANDIDATES[key]:
            ok = self.model.Extension.SelectByID2(name, "PLANE", 0, 0, 0, False, 0, None, 0)
            if ok:
                return
        raise RuntimeError(f"{key} 평면 선택 실패 (한글/영문 이름 모두 시도)")

    def _begin_sketch(self, plane_key: str) -> None:
        self.model.ClearSelection2(True)
        self._select_plane(plane_key)
        self.sketch_mgr.InsertSketch(True)
        self.sketch_mgr.AddToDB = True       # 스냅/추론 없이 정확한 좌표로 입력

    def _end_sketch(self) -> None:
        self.sketch_mgr.AddToDB = False
        self.model.ClearSelection2(True)

    # ------------------------------------------------------------------
    def build_shell(self, spec: VesselSpec) -> None:
        """쉘 + 양쪽 2:1 타원 경판을 하나의 회전 피처로 생성"""
        R = spec.outer_radius * MM
        L = spec.tl_length * MM
        h = spec.head_depth * MM
        print(f"쉘 회전체 생성: R={R:.4f} m, L={L:.4f} m, 경판깊이={h:.4f} m")

        self._begin_sketch("front")
        sm = self.sketch_mgr
        # 축선 (프로파일의 한 변이자 회전축)
        axis = sm.CreateLine(-h, 0, 0, L + h, 0, 0)
        # 쉘 외면 직선
        sm.CreateLine(0, R, 0, L, R, 0)
        # 좌측 경판 (타원호): 중심(0,0), 장축 끝(0,R), 단축 끝(-h,0), 시작(0,R) -> 끝(-h,0)
        sm.CreateEllipticalArc(0, 0, 0,   0, R, 0,   -h, 0, 0,   0, R, 0,   -h, 0, 0,   1)
        # 우측 경판: 중심(L,0), 장축 끝(L,R), 단축 끝(L+h,0), 시작(L+h,0) -> 끝(L,R)
        sm.CreateEllipticalArc(L, 0, 0,   L, R, 0,   L + h, 0, 0,   L + h, 0, 0,   L, R, 0,   1)
        self._end_sketch()

        # 회전축으로 사용할 선 선택 (mark = 16)
        self.model.ClearSelection2(True)
        if axis is not None:
            sel_data = self.model.SelectionManager.CreateSelectData()
            sel_data.Mark = 16
            axis.Select4(True, sel_data)

        feat = self.feat_mgr.FeatureRevolve2(
            True,            # SingleDir
            True,            # IsSolid
            False,           # IsThin
            False,           # IsCut
            False,           # ReverseDir
            False,           # BothDirectionUpToSameEntity
            0, 0,            # Dir1Type, Dir2Type (blind)
            2 * math.pi, 0,  # Dir1Angle, Dir2Angle
            False, False,    # OffsetReverse1/2
            0, 0,            # OffsetDistance1/2
            0, 0, 0,         # ThinType, ThinThickness1/2
            True,            # Merge
            True,            # UseFeatScope
            True,            # UseAutoSelect
        )
        if feat is None:
            raise RuntimeError("쉘 회전 피처 생성 실패 (스케치 프로파일/회전축 확인)")
        feat.Name = "Shell+Heads"
        self.model.ClearSelection2(True)

    # ------------------------------------------------------------------
    def _extrude_midplane(self, depth_m: float, name: str) -> None:
        feat = self.feat_mgr.FeatureExtrusion3(
            True,               # Sd (single direction)
            False,              # Flip
            False,              # Dir
            swEndCondMidPlane,  # T1
            swEndCondBlind,     # T2
            depth_m, 0.0,       # D1, D2
            False, False,       # Dchk1/2 (draft)
            False, False,       # Ddir1/2
            0.0, 0.0,           # Dang1/2
            False, False,       # OffsetReverse1/2
            False, False,       # TranslateSurface1/2
            True,               # Merge
            True,               # UseFeatScope
            True,               # UseAutoSelect
            0,                  # T0 (start condition: sketch plane)
            0.0,                # StartOffset
            False,              # FlipStartOffset
        )
        if feat is None:
            raise RuntimeError(f"{name} 돌출 실패")
        feat.Name = name
        self.model.ClearSelection2(True)

    def build_saddles(self, spec: VesselSpec) -> None:
        positions = spec.saddle_positions_from_tl()
        if not positions:
            print("새들 위치 정보 없음 - 건너뜀")
            return
        R = spec.outer_radius
        H = spec.centerline_height if spec.centerline_height > R else R + 500.0
        web = spec.saddle_web_thickness
        base_w = spec.saddle_base_width if spec.saddle_base_width > 0 else spec.outer_diameter * 1.1
        y_top = -R * 0.80          # 쉘 하부에 물리도록 위로 겹침
        y_bot = -H
        for i, xc in enumerate(positions, start=1):
            print(f"새들 {i}: x={xc:g} mm, 바닥폭 {base_w:g} mm, 높이 {H - R:g} mm")
            self._begin_sketch("front")
            self.sketch_mgr.CreateCornerRectangle(
                (xc - web / 2) * MM, y_bot * MM, 0,
                (xc + web / 2) * MM, y_top * MM, 0,
            )
            self._end_sketch()
            self._extrude_midplane(base_w * MM, f"Saddle_{i}")

    def build_lugs(self, spec: VesselSpec) -> None:
        positions = spec.lug_positions_from_tl()
        if not positions:
            print("러그 위치 정보 없음 - 건너뜀")
            return
        R = spec.outer_radius
        w, hgt, t, hole_d = spec.lug_width, spec.lug_height, spec.lug_thickness, spec.lug_hole_diameter
        for i, xc in enumerate(positions, start=1):
            print(f"러그 {i}: x={xc:g} mm")
            self._begin_sketch("front")
            self.sketch_mgr.CreateCornerRectangle(
                (xc - w / 2) * MM, (R * 0.90) * MM, 0,
                (xc + w / 2) * MM, (R + hgt) * MM, 0,
            )
            # 구멍 (내부 루프) - 상단에서 지름 1 배 내려온 위치
            self.sketch_mgr.CreateCircleByRadius(
                xc * MM, (R + hgt - hole_d) * MM, 0, (hole_d / 2) * MM
            )
            self._end_sketch()
            self._extrude_midplane(t * MM, f"LiftingLug_{i}")

    # ------------------------------------------------------------------
    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        print(f"저장: {path}")
        errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        ok = self.model.Extension.SaveAs(
            path, swSaveAsCurrentVersion, swSaveAsOptions_Silent, None, errors, warnings
        )
        if not ok:
            raise RuntimeError(f"저장 실패 (errors={errors.value}, warnings={warnings.value})")

    def fit_view(self) -> None:
        try:
            self.model.ShowNamedView2("*Isometric", 7)
            self.model.ViewZoomtofit2()
        except Exception:
            pass

    def close(self) -> None:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def build(self, spec: VesselSpec, save_path: Optional[str] = None) -> None:
        self.connect()
        try:
            self.new_part()
            self.build_shell(spec)
            self.build_saddles(spec)
            self.build_lugs(spec)
            self.fit_view()
            if save_path:
                self.save(save_path)
            print("모델링 완료.")
        finally:
            self.close()
