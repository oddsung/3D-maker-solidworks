"""
vessel_spec.py - 도면에서 추출한 수평형 압력용기(Horizontal Vessel) 형상 사양 정의

모든 길이 단위는 mm 이다. (SolidWorks API 호출 시 m 로 변환한다)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import List, Optional


@dataclass
class VesselSpec:
    # --- 도면 식별 정보 ---
    drawing_no: str = ""
    title: str = ""

    # --- 쉘 / 경판 ---
    outer_diameter: float = 0.0          # O.D (mm)
    tl_length: float = 0.0               # T.L ~ T.L (접선 간 거리, mm)
    head_type: str = "2:1 ellipsoidal"   # 경판 형식 (현재 2:1 타원형만 지원)
    straight_flange: float = 50.0        # 경판 직선부(SF) 길이 (mm)

    # --- 새들 (Saddle) ---
    saddle_spacing: float = 0.0          # 새들 중심 간 거리 (mm)
    saddle_offset: float = 0.0           # T.L 에서 새들 중심까지 거리 (mm)
    saddle_base_width: float = 0.0       # 새들 바닥 폭 (용기 축 직각 방향, mm)
    saddle_web_thickness: float = 300.0  # 새들 두께 (용기 축 방향, mm) - 도면에 없으면 기본값
    centerline_height: float = 0.0       # 새들 바닥면 ~ 용기 중심선 높이 (mm)

    # --- 리프팅 러그 (Lifting Lug) ---
    cog_from_tl: float = 0.0             # 좌측 T.L 에서 무게중심(C.O.G)까지 거리 (mm)
    lug_offsets_from_cog: List[float] = field(default_factory=list)  # C.O.G 기준 러그 위치 (-: 좌측, +: 우측)
    lug_width: float = 400.0             # 러그 폭 (축 방향, mm)
    lug_height: float = 600.0            # 쉘 상단에서 러그 상단까지 높이 (mm)
    lug_thickness: float = 40.0          # 러그 판 두께 (mm)
    lug_hole_diameter: float = 120.0     # 러그 구멍 지름 (mm)

    # --- 기타 참고 값 ---
    overall_length: Optional[float] = None   # 전체 길이 (APPROX.) (mm)
    lifting_weight_kg: Optional[float] = None

    # ------------------------------------------------------------------
    @property
    def outer_radius(self) -> float:
        return self.outer_diameter / 2.0

    @property
    def head_depth(self) -> float:
        """2:1 타원형 경판의 깊이 (SF 제외) = D/4"""
        return self.outer_diameter / 4.0

    @property
    def total_shell_length(self) -> float:
        """T.L~T.L + 양쪽 경판 깊이 (노즐 제외한 순수 형상 전체 길이)"""
        return self.tl_length + 2 * self.head_depth

    def lug_positions_from_tl(self) -> List[float]:
        """좌측 T.L 기준 러그 위치 목록"""
        return [self.cog_from_tl + off for off in self.lug_offsets_from_cog]

    def saddle_positions_from_tl(self) -> List[float]:
        """좌측 T.L 기준 새들 중심 위치 목록"""
        if self.saddle_offset > 0 and self.tl_length > 0:
            return [self.saddle_offset, self.tl_length - self.saddle_offset]
        return []

    # ------------------------------------------------------------------
    def validate(self) -> List[str]:
        """모델링에 필요한 필수 값 검사. 문제가 있으면 메시지 목록 반환."""
        problems = []
        if self.outer_diameter <= 0:
            problems.append("outer_diameter (O.D) 값이 없습니다.")
        if self.tl_length <= 0:
            problems.append("tl_length (T.L~T.L) 값이 없습니다.")
        if self.saddle_spacing > 0 and self.saddle_offset > 0 and self.tl_length > 0:
            calc = self.saddle_spacing + 2 * self.saddle_offset
            if abs(calc - self.tl_length) > 1.0:
                problems.append(
                    f"새들 간격({self.saddle_spacing}) + 2 x 오프셋({self.saddle_offset}) = {calc} 가 "
                    f"T.L 길이({self.tl_length}) 와 일치하지 않습니다."
                )
        if self.centerline_height and self.centerline_height <= self.outer_radius:
            problems.append("centerline_height 가 반지름보다 작아 새들 높이가 음수가 됩니다.")
        return problems

    def summary(self) -> str:
        lines = [
            f"도면번호        : {self.drawing_no or '-'}",
            f"제목            : {self.title or '-'}",
            f"O.D             : {self.outer_diameter:g} mm",
            f"T.L ~ T.L       : {self.tl_length:g} mm",
            f"경판            : {self.head_type} (깊이 {self.head_depth:g} mm)",
            f"형상 전체 길이  : {self.total_shell_length:g} mm",
            f"전체 길이(도면) : {self.overall_length if self.overall_length else '-'} mm",
            f"새들 간격       : {self.saddle_spacing:g} mm (T.L 오프셋 {self.saddle_offset:g} mm)",
            f"새들 바닥 폭    : {self.saddle_base_width:g} mm, 중심선 높이 {self.centerline_height:g} mm",
            f"C.O.G (좌측 T.L): {self.cog_from_tl:g} mm",
            f"러그 위치(T.L)  : {[round(p, 1) for p in self.lug_positions_from_tl()]} mm",
            f"리프팅 중량     : {self.lifting_weight_kg if self.lifting_weight_kg else '-'} kg",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def to_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, path: str) -> "VesselSpec":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(**data)

    def apply_overrides(self, overrides: dict) -> None:
        """JSON 등으로 받은 값으로 기존 필드를 덮어쓴다 (알 수 없는 키는 무시)."""
        for k, v in overrides.items():
            if hasattr(self, k):
                setattr(self, k, v)
