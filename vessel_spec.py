"""
vessel_spec.py - 도면에서 추출한 수평형 압력용기(Horizontal Vessel) 형상 사양 정의

모든 길이 단위는 mm 이다. (SolidWorks API 호출 시 m 로 변환한다)

좌표/각도 규약 (sw_modeler 와 공유):
  X : 용기 축 방향 (좌측 T.L = 0, 우측 T.L = tl_length)
  Y : 상하 (위가 +)
  Z : 축 직각 수평 방향
  노즐 각도 angle_deg : 0 = TOP(+Y), 90 = +Z, 180 = BTM(-Y), 270 = -Z  (단면도 표기와 동일)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict, fields
from typing import List, Optional

# ASME B16.5 Class 300 플랜지 외경 / 두께 (mm). 노즐 플랜지 표현용 근사값.
# 26" 는 ASME B16.47 Series B 근사값.
FLANGE_CL300 = {
    0.5: (95.0, 14.3), 0.75: (117.0, 15.9), 1.0: (124.0, 17.5), 1.5: (155.0, 20.7),
    2.0: (165.0, 22.3), 3.0: (210.0, 28.6), 4.0: (254.0, 31.8), 6.0: (318.0, 36.6),
    8.0: (381.0, 41.3), 10.0: (445.0, 47.7), 12.0: (520.0, 50.8), 14.0: (585.0, 53.9),
    16.0: (650.0, 57.2), 18.0: (710.0, 60.4), 20.0: (775.0, 63.5), 24.0: (915.0, 69.9),
    26.0: (970.0, 74.7),
}

HEAD_HEMI = "hemispherical"
HEAD_ELLIP = "2:1 ellipsoidal"


def _empty(v) -> bool:
    """병합 시 '값 없음' 으로 취급할지"""
    return v is None or v == "" or v == [] or v == 0 or v == 0.0


@dataclass
class NozzleSpec:
    mark: str
    size_in: float = 0.0                       # 호칭 지름 (inch)
    size_text: str = ""                        # 도면 표기 (예: 1 1/2")
    rating: str = ""
    flange_type: str = ""                      # WN.RF / FN.RF / LWN.RF
    service: str = ""
    position_from_tl: Optional[float] = None   # 좌측 T.L 기준 x (mm) - 쉘 노즐
    angle_deg: Optional[float] = None          # 0=TOP, 90, 180=BTM, 270
    projection: Optional[float] = None         # 용기 C.L ~ 플랜지 면 (mm). 경판 노즐은 T.L ~ 플랜지 면
    neck_od: float = 0.0                       # 노즐 목 외경 (mm)
    neck_thk: float = 0.0                      # 노즐 목 두께 (mm)
    offset_y: float = 0.0                      # 노즐 축의 수직 오프셋 (+위, mm) - 수평/경판 노즐용
    offset_z: float = 0.0                      # 노즐 축의 수평 오프셋 (+Z, mm)
    on_head: str = ""                          # "left" / "right" : 경판에 축방향으로 붙는 노즐
    profile: List[List[float]] = field(default_factory=list)   # 상세도 외곽선 [[플랜지 면부터 s, 반경 r] mm, ...]
                                               # 있으면 모델러가 균일 목+표준 플랜지 대신 이 실루엣을 회전한다
    flange_std: str = ""                       # "B16.5" / "B16.47A" / "B16.47B" (asme_flange.choose_standard)
    bolt_circle: float = 0.0                   # 볼트 원 지름 (mm)
    n_bolts: int = 0                           # 볼트 구멍 수
    bolt_hole_d: float = 0.0                   # 볼트 구멍 지름 (mm)
    blind: bool = False                        # 블라인드 플랜지 부착 (부품표 BLIND FLANGE)
    blind_t: float = 0.0                       # 블라인드 두께 (ASME 표)
    gasket_t: float = 0.0                      # 개스킷 두께 (부품표 GASKET tX)
    support_n: int = 0                         # 노즐 지지판(거싯) 개수 (상세도 'n-SUP'T PLATES (tXxWY)')
    support_t: float = 0.0                     # 지지판 두께
    support_w: float = 0.0                     # 지지판 폭 (판 면 안에서 축과 직각 방향)
    support_len: float = 0.0                   # 지지판 길이 (목에서 쉘까지, 판 방향)
    support_angle_deg: float = 0.0             # 지지판과 노즐 축 사이 각도
    support_s: float = 0.0                     # 지지판이 목에 붙는 위치 (플랜지 면부터 mm)
    support_azimuths: List[float] = field(default_factory=list)   # 노즐 축 둘레 방위각 (support_az_ref 기준, deg)
    support_az_ref: str = ""                   # 보조 뷰의 기준 문구: "fixed" (FIXED/SLIDING SIDE, 고정 새들 쪽 용기 축) | "top" (TOP/BTM, 연직 위)
    support_az_dir: float = 0.0                # 각도 라벨 쌍의 이등분 방향: 기준 문구(FIXED/TOP) 방향에서 화면 시계 방향으로 잰 각 (deg)
    notes: List[str] = field(default_factory=list)   # 파서 추정 근거

    def standard_flange_od(self) -> Optional[float]:
        """표준 플랜지 외경 (26" 부터 ASME B16.47, 그 밖은 CL300 표). 표에 없으면 None (근사값은 표준이 아님)"""
        if self.size_in >= 26:
            from asme_flange import flange_cl300
            fd = flange_cl300(self.size_in, "A" if self.flange_std.upper().endswith("A") else "")
            if fd is not None and fd.od > 0:
                return fd.od
        if self.size_in in FLANGE_CL300:
            return FLANGE_CL300[self.size_in][0]
        return None

    @property
    def neck_id(self) -> float:
        return max(self.neck_od - 2 * self.neck_thk, 0.0)

    @property
    def flange(self):
        """(플랜지 외경, 두께). 26" 부터는 ASME B16.47 표(Series 는 flange_std 에 따름), 그 밖은 CL300 표,
        표에 없는 사이즈는 목 외경 기준 근사"""
        if self.size_in >= 26:
            from asme_flange import flange_cl300
            fd = flange_cl300(self.size_in, "A" if self.flange_std.upper().endswith("A") else "")
            if fd is not None and fd.od > 0 and fd.thickness > 0:
                return (fd.od, fd.thickness)
        if self.size_in in FLANGE_CL300:
            return FLANGE_CL300[self.size_in]
        return (self.neck_od * 1.8, max(self.neck_od * 0.12, 20.0))

    def apply_flange_standard(self, series_hint: str = "") -> str:
        """ASME 표에서 볼트 원/구멍 수/구멍 지름을 채운다. 채운 내용 설명을 돌려준다 (표에 없으면 빈 문자열)"""
        from asme_flange import flange_cl300, describe
        if self.size_in <= 0:
            return ""
        fd = flange_cl300(self.size_in, series_hint)
        if fd is None:
            return ""
        self.flange_std, self.bolt_circle, self.n_bolts, self.bolt_hole_d = fd.std, fd.bolt_circle, fd.n_bolts, fd.hole_d
        if self.blind and self.blind_t <= 0:
            self.blind_t = fd.blind_t
        return describe(fd)

    def is_ready(self) -> bool:
        if self.neck_od <= 0 or self.projection is None:
            return False
        if self.on_head:
            return True
        return self.position_from_tl is not None and self.angle_deg is not None

    _ZERO_IS_EMPTY = ("size_in", "neck_od", "neck_thk", "offset_y", "offset_z", "bolt_circle", "n_bolts", "bolt_hole_d",
                      "blind_t", "gasket_t", "support_n", "support_t", "support_w", "support_len", "support_angle_deg", "support_s",
                      "support_az_dir")

    def merge(self, other: "NozzleSpec") -> None:
        for f in fields(NozzleSpec):
            v = getattr(other, f.name)
            if f.name == "notes":
                self.notes += [n for n in v if n not in self.notes]
                continue
            if v is None or v == "" or v == []:
                continue
            if f.name in self._ZERO_IS_EMPTY and v == 0:
                continue
            setattr(self, f.name, v)   # angle_deg=0 (TOP), position 0 은 유효한 값


@dataclass
class VesselSpec:
    # --- 도면 식별 정보 ---
    drawing_no: str = ""
    title: str = ""

    # --- 쉘 / 경판 ---
    outer_diameter: float = 0.0          # O.D (mm)
    inner_diameter: float = 0.0          # I.D (mm)
    shell_thickness: float = 0.0         # 쉘 판두께 (mm)
    tl_length: float = 0.0               # T.L ~ T.L (접선 간 거리, mm)
    head_type: str = HEAD_ELLIP          # "2:1 ellipsoidal" | "hemispherical"
    head_thickness: float = 0.0          # 경판 두께 (mm), 0 이면 쉘 두께 사용
    head_inner_radius: float = 0.0       # 반구형 경판 내측 반경 (mm), 0 이면 I.D/2
    straight_flange: float = 50.0        # 경판 직선부(SF) 길이 (mm) - 참고용
    insulation_thickness: float = 0.0    # 보온 두께 (mm) - 참고용, 모델링 안 함

    # --- 새들 (Saddle) ---
    saddle_spacing: float = 0.0          # 새들 중심 간 거리 (mm)
    saddle_offset: float = 0.0           # T.L 에서 새들 중심까지 거리 (mm)
    saddle_base_width: float = 0.0       # 새들 바닥 폭 (용기 축 직각 방향, mm)
    saddle_web_thickness: float = 300.0  # 새들 두께 (용기 축 방향, mm) - 판 구성이 없을 때 쓰는 단순 블록 기본값
    # --- 새들 판 구성 (몸체/서포트 상세도에서 읽음, base_t 가 0 이면 단순 블록으로 모델링) ---
    saddle_length: float = 0.0           # 베이스 플레이트 축 방향 길이 (mm)
    saddle_base_t: float = 0.0           # 베이스 플레이트 두께
    saddle_web_t: float = 0.0            # 웹 플레이트(축 직각 수직판) 두께
    saddle_web_length: float = 0.0       # 웹 플레이트 폭 (Z 방향), 0 이면 베이스 폭
    saddle_web_spacing: float = 0.0      # 웹(서포트) 판이 두 장일 때 축 방향 간격 (0 이면 가운데 한 장)
    saddle_rib_t: float = 0.0            # 리브 플레이트 두께
    saddle_rib_z: List[float] = field(default_factory=list)   # 리브 위치 (용기 중심 기준 Z, mm)
    saddle_rib_margin: float = 50.0      # 리브가 베이스 끝에서 들어오는 여유 (축 방향)
    saddle_wear_t: float = 0.0           # 서포트(웨어) 플레이트 두께
    saddle_wrap_deg: float = 0.0         # 서포트(웨어) 플레이트 감싸는 각 (전체, deg) - 새들 뷰의 큰 각 (152°)
    saddle_web_wrap_deg: float = 0.0     # 웹 판 뿔(horn)의 각 (전체, deg) - 새들 뷰의 작은 각 (140°), 0 이면 wrap 과 같음
    fixed_side: str = ""                 # 고정 새들 쪽: "right"(+X) / "left"(-X) / ""(모름 -> +X 로 가정)
    centerline_height: float = 0.0       # 새들 바닥면 ~ 용기 중심선 높이 (mm)

    # --- 리프팅 러그 (Lifting Lug) ---
    cog_from_tl: float = 0.0             # 좌측 T.L 에서 무게중심(C.O.G)까지 거리 (mm)
    lug_offsets_from_cog: List[float] = field(default_factory=list)  # C.O.G 기준 러그 위치 (-: 좌측, +: 우측)
    lug_angles_deg: List[float] = field(default_factory=lambda: [-45.0, 45.0])  # 각 위치의 러그 각도 (TOP 기준)
    lug_width: float = 400.0             # 러그 폭 (접선 방향, mm)
    lug_height: float = 600.0            # 쉘 외면에서 러그 끝까지 높이 (mm)
    lug_thickness: float = 40.0          # 러그 판 두께 (축 방향, mm)
    lug_hole_diameter: float = 120.0     # 러그 구멍 지름 (mm)

    # --- 노즐 ---
    nozzles: List[NozzleSpec] = field(default_factory=list)

    # --- 기타 참고 값 ---
    overall_length: Optional[float] = None   # 전체 길이 (APPROX.) (mm)
    lifting_weight_kg: Optional[float] = None

    # ------------------------------------------------------------------
    # 파생 치수
    @property
    def outer_radius(self) -> float:
        return self.outer_diameter / 2.0

    @property
    def inner_radius(self) -> float:
        return self.inner_diameter / 2.0

    @property
    def head_t(self) -> float:
        return self.head_thickness if self.head_thickness > 0 else self.shell_thickness

    @property
    def is_hemi(self) -> bool:
        return self.head_type.lower().startswith("hemi")

    @property
    def head_inner_r(self) -> float:
        """경판 내측 반경(반구) / 내측 장반경(타원)"""
        if self.is_hemi:
            return self.head_inner_radius if self.head_inner_radius > 0 else self.inner_radius
        return self.outer_radius - self.head_t

    @property
    def head_outer_r(self) -> float:
        if self.is_hemi:
            return self.head_inner_r + self.head_t
        return self.outer_radius

    @property
    def head_depth_outer(self) -> float:
        """T.L 에서 경판 외면 정점까지 깊이 (SF 제외)"""
        return self.head_outer_r if self.is_hemi else self.outer_diameter / 4.0

    @property
    def head_depth_inner(self) -> float:
        return self.head_inner_r if self.is_hemi else self.head_depth_outer - self.head_t

    @property
    def head_depth(self) -> float:   # 이전 버전 호환
        return self.head_depth_outer

    @property
    def total_shell_length(self) -> float:
        """T.L~T.L + 양쪽 경판 깊이 (노즐 제외한 순수 형상 전체 길이)"""
        return self.tl_length + 2 * self.head_depth_outer

    def lug_positions_from_tl(self) -> List[float]:
        return [self.cog_from_tl + off for off in self.lug_offsets_from_cog]

    def saddle_positions_from_tl(self) -> List[float]:
        if self.saddle_offset > 0 and self.tl_length > 0:
            return [self.saddle_offset, self.tl_length - self.saddle_offset]
        return []

    def nozzle(self, mark: str) -> NozzleSpec:
        for n in self.nozzles:
            if n.mark == mark:
                return n
        n = NozzleSpec(mark=mark)
        self.nozzles.append(n)
        return n

    # ------------------------------------------------------------------
    def resolve(self) -> List[str]:
        """O.D / I.D / 두께 중 빠진 값을 서로 계산해 채운다. 메시지 목록 반환"""
        msgs = []
        if self.outer_diameter <= 0 and self.inner_diameter > 0 and self.shell_thickness > 0:
            self.outer_diameter = self.inner_diameter + 2 * self.shell_thickness
            msgs.append(f"O.D = I.D + 2t = {self.outer_diameter:g}")
        if self.inner_diameter <= 0 and self.outer_diameter > 0 and self.shell_thickness > 0:
            self.inner_diameter = self.outer_diameter - 2 * self.shell_thickness
            msgs.append(f"I.D = O.D - 2t = {self.inner_diameter:g}")
        if self.shell_thickness <= 0 and self.outer_diameter > 0 and self.inner_diameter > 0:
            self.shell_thickness = (self.outer_diameter - self.inner_diameter) / 2
            msgs.append(f"t = (O.D - I.D)/2 = {self.shell_thickness:g}")
        if self.tl_length <= 0 and self.saddle_spacing > 0 and self.saddle_offset > 0:
            self.tl_length = self.saddle_spacing + 2 * self.saddle_offset
            msgs.append(f"T.L 계산값 사용 = {self.tl_length:g}")
        return msgs

    def fill_defaults(self) -> List[str]:
        """도면에 없는 값에 기본값을 넣는다 (JSON 보정 후 호출). 메시지 목록 반환"""
        import re
        msgs = []
        for n in self.nozzles:
            if n.n_bolts <= 0 and n.size_in > 0:
                # 볼트 원/구멍: 담당자 규칙 (24" 까지 B16.5, 26" 부터 B16.47 Series B 기본). 상세도 부품표에
                # SERIES A 가 있으면 drawing_set.resolve 가 먼저 A 로 채워 둔다
                desc = n.apply_flange_standard()
                if desc:
                    n.notes.append(f"볼트 구멍 {n.n_bolts}-Ø{n.bolt_hole_d:g} B.C {n.bolt_circle:g} ({n.flange_std})")
            if n.projection is None and n.neck_od > 0 and not n.on_head:
                # 같은 계열(N31, N31A, N31B ...) 에 투영이 있으면 그 평균, 없으면 반경 + 350
                family = re.match(r"N\d+", n.mark).group(0)
                sib = [m.projection for m in self.nozzles
                       if m is not n and m.projection and re.match(r"N\d+", m.mark).group(0) == family]
                if sib:
                    n.projection = round(sum(sib) / len(sib), 1)
                    n.notes.append(f"투영 {n.projection:g} (같은 계열 노즐 기준)")
                else:
                    n.projection = round(self.outer_radius + 350.0, 1)
                    n.notes.append(f"투영 기본값 {n.projection:g}")
                msgs.append(f"노즐 {n.mark}: 투영 길이가 도면에 없어 {n.projection:g} 사용")
        return msgs

    def validate(self) -> List[str]:
        """모델링에 필요한 필수 값 검사. 문제가 있으면 메시지 목록 반환."""
        problems = []
        if self.outer_diameter <= 0:
            problems.append("outer_diameter (O.D) 값이 없습니다.")
        if self.tl_length <= 0:
            problems.append("tl_length (T.L~T.L) 값이 없습니다.")
        if self.shell_thickness <= 0:
            problems.append("shell_thickness (쉘 두께) 를 찾지 못해 속이 찬 형상으로 모델링됩니다.")
        if self.inner_diameter > 0 and self.shell_thickness > 0 and self.outer_diameter > 0:
            if abs(self.inner_diameter + 2 * self.shell_thickness - self.outer_diameter) > 1.0:
                problems.append(
                    f"I.D({self.inner_diameter:g}) + 2t({self.shell_thickness:g}) 가 "
                    f"O.D({self.outer_diameter:g}) 와 일치하지 않습니다."
                )
        if self.saddle_spacing > 0 and self.saddle_offset > 0 and self.tl_length > 0:
            calc = self.saddle_spacing + 2 * self.saddle_offset
            if abs(calc - self.tl_length) > 1.0:
                problems.append(
                    f"새들 간격({self.saddle_spacing}) + 2 x 오프셋({self.saddle_offset}) = {calc} 가 "
                    f"T.L 길이({self.tl_length}) 와 일치하지 않습니다."
                )
        if self.centerline_height and self.centerline_height <= self.outer_radius:
            problems.append("centerline_height 가 반지름보다 작아 새들 높이가 음수가 됩니다.")
        for n in self.nozzles:
            if not n.is_ready():
                missing = []
                if n.neck_od <= 0:
                    missing.append("neck_od")
                if n.projection is None:
                    missing.append("projection")
                if not n.on_head and n.position_from_tl is None:
                    missing.append("position_from_tl")
                if not n.on_head and n.angle_deg is None:
                    missing.append("angle_deg")
                problems.append(f"노즐 {n.mark}: {', '.join(missing)} 없음 -> 모델링에서 제외")
        return problems

    def summary(self) -> str:
        t_h = self.head_t
        lines = [
            f"도면번호        : {self.drawing_no or '-'}",
            f"제목            : {self.title or '-'}",
            f"O.D / I.D / t   : {self.outer_diameter:g} / {self.inner_diameter:g} / {self.shell_thickness:g} mm",
            f"T.L ~ T.L       : {self.tl_length:g} mm",
            f"경판            : {self.head_type}, 두께 {t_h:g} mm, 내측 R {self.head_inner_r:g}, "
            f"깊이(외면) {self.head_depth_outer:g} mm",
            f"형상 전체 길이  : {self.total_shell_length:g} mm",
            f"전체 길이(도면) : {self.overall_length if self.overall_length else '-'} mm",
            f"보온 두께       : {self.insulation_thickness:g} mm (모델링 안 함)",
            f"새들 간격       : {self.saddle_spacing:g} mm (T.L 오프셋 {self.saddle_offset:g} mm)",
            f"새들 바닥 폭    : {self.saddle_base_width:g} mm, 중심선 높이 {self.centerline_height:g} mm",
            f"C.O.G (좌측 T.L): {self.cog_from_tl:g} mm",
            f"러그 위치(T.L)  : {[round(p, 1) for p in self.lug_positions_from_tl()]} mm, 각도 {self.lug_angles_deg}",
            f"리프팅 중량     : {self.lifting_weight_kg if self.lifting_weight_kg else '-'} kg",
            f"노즐            : {len(self.nozzles)} 개 (모델링 가능 {sum(1 for n in self.nozzles if n.is_ready())} 개)",
        ]
        return "\n".join(lines)

    def nozzle_table(self) -> str:
        if not self.nozzles:
            return "(노즐 없음)"
        hdr = (f"{'MARK':7s} {'SIZE':7s} {'TYPE':7s} {'x(T.L+)':>9s} {'각도':>5s} {'투영':>6s} {'목OD':>6s} {'목t':>6s} {'offY':>6s} "
               f"{'볼트구멍':>12s}  비고")
        rows = [hdr]
        for n in sorted(self.nozzles, key=_mark_sort_key):
            pos = "경판" + ("L" if n.on_head == "left" else "R") if n.on_head else (
                f"{n.position_from_tl:g}" if n.position_from_tl is not None else "?")
            ang = "-" if n.on_head else (f"{n.angle_deg:g}" if n.angle_deg is not None else "?")
            prj = f"{n.projection:g}" if n.projection is not None else "?"
            bolts = f"{n.n_bolts}-Ø{n.bolt_hole_d:g}" if n.n_bolts else "-"
            rows.append(
                f"{n.mark:7s} {n.size_text:7s} {n.flange_type:7s} {pos:>9s} {ang:>5s} {prj:>6s} "
                f"{n.neck_od:6g} {n.neck_thk:6g} {n.offset_y:6g} {bolts:>12s}  {'; '.join(n.notes)}"
            )
        return "\n".join(rows)

    # ------------------------------------------------------------------
    def merge(self, other: "VesselSpec") -> None:
        """other 의 값이 있는 필드로 self 를 덮어쓴다 (노즐은 마크별 병합)."""
        for f in fields(VesselSpec):
            if f.name == "nozzles":
                for n in other.nozzles:
                    self.nozzle(n.mark).merge(n)
                continue
            if f.name == "lug_angles_deg":
                if other.lug_angles_deg != [-45.0, 45.0]:
                    self.lug_angles_deg = list(other.lug_angles_deg)
                continue
            v = getattr(other, f.name)
            default = f.default if f.default is not None else None
            if not _empty(v) and v != default:
                setattr(self, f.name, v)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "VesselSpec":
        data = dict(data)
        nozzles = [NozzleSpec(**d) for d in data.pop("nozzles", [])]
        spec = cls(**{k: v for k, v in data.items() if k in {f.name for f in fields(cls)}})
        spec.nozzles = nozzles
        return spec

    @classmethod
    def from_json(cls, path: str) -> "VesselSpec":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def apply_overrides(self, overrides: dict) -> None:
        """JSON 으로 받은 값으로 필드를 덮어쓴다.
        - "nozzles": [ {...}, ... ]            노즐 목록 전체 교체
        - "nozzle_overrides": {"N9": {...}}    마크별 부분 수정 (없는 마크는 새로 추가)
        - 그 외 키: VesselSpec 필드명 (알 수 없는 키는 무시)
        """
        for k, v in overrides.items():
            if k == "nozzles":
                self.nozzles = [NozzleSpec(**d) for d in v]
            elif k == "nozzle_overrides":
                for mark, patch in v.items():
                    n = self.nozzle(mark)
                    for pk, pv in patch.items():
                        if hasattr(n, pk):
                            setattr(n, pk, pv)
                    n.notes.append("JSON 보정")
            elif hasattr(self, k):
                setattr(self, k, v)


def _mark_sort_key(n: NozzleSpec):
    import re
    m = re.match(r"N(\d+)([A-Z]?)", n.mark)
    return (int(m.group(1)), m.group(2)) if m else (9999, n.mark)
