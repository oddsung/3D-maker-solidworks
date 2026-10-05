"""
drawing_set.py - 도면 세트(폴더) 단위 분석: 도면 사이의 연관성, 값 일관성, 누락 값 보완

1. 도면마다 drawing_info 로 정보를 뽑는다 (스캔 도면은 같은 도면번호의 다른 시트에서 제목/종류를 물려받음).
2. 연관성(Edge): 시트 시리즈, 노즐 마크 공유(GA 리스트/입면도 <-> 노즐 상세 <-> 전개도), 용접선 공유,
   단면 표식 -> 다른 시트의 VIEW/SECTION, GA 의 'SEE DWG.' -> 상세도, 다른 도면 제목/번호 언급,
   같은 사실(T.L~T.L, 새들 간격, O.D ...)의 일치/불일치.
3. 값 일관성: 같은 키의 사실을 도면별로 모아 표로 만들고 다른 값은 충돌로 표시.
4. 누락 값 보완(resolve): GA 에서 'SEE DWG.'/추정/기본값이었던 노즐 값(투영, 오프셋, 목 외경, 위치)을
   노즐 상세도의 'nnnn TO C.L / T.L', 부품표 BOSS Ø, 전개도의 T.L+ 로 확정하거나 교차 검증한다.
   근거는 NozzleSpec.notes 에 남긴다.
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

from drawing_info import DrawingInfo, extract_drawing_info, GRADE_KO, _mark_key, compact_marks
from vessel_spec import VesselSpec, NozzleSpec

FACT_ALIASES = {"weight_erection": "weight_lifting"}      # 같은 뜻의 키
FACT_KO = {
    "outer_diameter": "O.D", "inner_diameter": "I.D", "shell_thickness": "쉘 두께", "tl_length": "T.L~T.L",
    "saddle_spacing": "새들 간격", "saddle_offset": "새들 T.L 오프셋", "head_type": "경판 형식",
    "head_thickness": "경판 두께", "head_min_thickness": "경판 최소 두께", "head_radius": "경판 반경",
    "insulation_thickness": "보온 두께", "overall_length": "전체 길이", "centerline_height": "새들 중심선 높이",
    "saddle_bolt_circle": "새들 B.C", "platform_elevations": "플랫폼 EL", "weight_lifting": "리프팅(설치) 중량",
    "weight_full_of_water": "만수 중량", "weight_empty": "빈 중량", "weight_net": "운송 순중량",
    "weight_shipping_saddle": "운송 새들 중량", "weight_gross": "운송 총중량", "shipping_dimension_lwh": "운송 치수 LxWxH",
    "design_pressure": "설계 압력", "design_temperature": "설계 온도", "operating_pressure": "운전 압력",
    "operating_temperature": "운전 온도", "mawp": "MAWP", "hydro_test_pressure": "수압시험 압력",
    "corrosion_allowance": "부식 여유", "volume": "용적", "radiography": "방사선 검사", "material_shell_head": "재질(쉘/경판)",
    "thickness_shell_head": "두께(쉘/경판)", "fluid": "유체", "code": "설계 코드", "pwht": "PWHT", "design_life": "설계 수명",
    "course_widths": "쉘 코스 폭", "course_total": "코스 폭 합계", "nozzle_count": "노즐 수", "seam_count": "용접선 수",
}
EDGE_KO = {
    "sheet_series": "같은 도면번호의 시트", "nozzle_mark": "노즐 마크 공유", "weld_seam": "용접선 공유",
    "section_view": "단면 표식 -> 다른 시트의 뷰", "see_dwg": "GA 'SEE DWG.' -> 상세도", "title_ref": "다른 도면 언급",
    "fact_match": "같은 값 공유", "fact_conflict": "값 불일치",
}
STATUS_KO = {"resolved": "보완", "confirmed": "확인", "conflict": "불일치", "unresolved": "미해결", "info": "참고"}


@dataclass
class Edge:
    kind: str
    src: str
    dst: str
    detail: str = ""
    items: List[str] = field(default_factory=list)


@dataclass
class Resolution:
    mark: str
    field: str
    old: str
    new: str
    source: str
    evidence: str
    status: str          # resolved | confirmed | conflict | unresolved | info

    @property
    def status_ko(self) -> str:
        return STATUS_KO.get(self.status, self.status)


def list_pdfs(folder: str) -> List[str]:
    """폴더의 도면 파일 목록 (PDF + DXF). Windows 는 glob 이 대소문자를 구분하지 않아 중복될 수 있으니
    소문자 기준으로 정리. 같은 도면이 두 형식으로 다 있으면 DXF 를 쓴다(문자·치수가 요소로 남아 있어 정확)"""
    seen: Dict[str, str] = {}
    for ext in ("pdf", "PDF", "dxf", "DXF"):
        for p in glob.glob(os.path.join(folder, "*." + ext)):
            seen.setdefault(os.path.normcase(os.path.abspath(p)), p)
    by_stem: Dict[str, str] = {}
    for p in sorted(seen.values()):
        stem = os.path.splitext(os.path.normcase(os.path.basename(p)))[0]
        if stem not in by_stem or p.lower().endswith(".dxf"):
            by_stem[stem] = p
    return sorted(by_stem.values())


list_drawings = list_pdfs        # 이름만 일반화 (PDF 전용이 아님)


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return str(v)


NON_COMPARABLE = {"seam_count", "nozzle_count"}     # 시트마다 다른 게 정상인 개수 항목


def _norm_text(s) -> str:
    s = str(s).upper()
    s = re.sub(r"\(|\)|,|\.", " ", s)
    s = re.sub(r"\bEDITION\b|\bED\b", "ED", s)
    return re.sub(r"\s+", " ", s).strip()


def _same(a, b, tol: float = 1.0) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= tol
    if isinstance(a, list) and isinstance(b, list):
        # 한쪽이 다른 쪽을 포함하면 같은 값으로 본다 (B.C 목록처럼 시트마다 표기 범위가 다름)
        sa, sb = [float(x) for x in a], [float(x) for x in b]
        sub = lambda s, t: all(any(abs(x - y) <= tol for y in t) for x in s)
        return sub(sa, sb) or sub(sb, sa)
    na, nb = _norm_text(a), _norm_text(b)
    return na == nb or (len(na) > 8 and (na in nb or nb in na))


def _estimated(n: NozzleSpec, field_name: str) -> bool:
    """해당 값이 파서의 추정/기본값/계열 평균에서 왔는지 (notes 근거 문구로 판단)"""
    if field_name == "projection":
        return n.projection is None or any(k in t for t in n.notes for k in ("투영 기본값", "같은 계열", "지시선 끝, 추정"))
    if field_name == "offset_y":
        return any("오프셋" in t and "추정" in t for t in n.notes)
    return False


# ----------------------------------------------------------------------
SUPPORT_PLATE_RE = r"(?:SUP'?T\.?|SUPPORT)\s*PLATES?"
_REF_KO = {"fixed": "고정 새들 쪽 용기 축", "top": "연직 위"}


def _aux_azimuth(av) -> Optional[tuple]:
    """노즐 축 방향으로 본 보조 뷰(VIEW "A") -> (각도, 기준 문구 종류, 이등분 방향, 설명). 같은 각도 라벨 두 개가 없으면 None.
    - 기준 문구: FIXED SIDE / SLIDING SIDE 가 있으면 "fixed" (FIXED 방향이 +), TOP / BTM 이면 "top" (TOP 방향이 +)
    - 이등분 방향: 두 문구의 가운데를 뷰 중심으로 보고, + 문구 방향에서 화면 시계 방향으로 잰 각도 라벨 쌍 중점의 방향.
      90° 배수 근처(25° 이내)면 맞춘다. 예) 라벨이 FIXED 쪽 위에 있으면 0°, 뷰 왼쪽에 있으면 -90° (0005 8/8 VIEW "B")
    - 문구가 하나뿐이면 뷰 상자 중심 기준, 없으면 ("fixed", 0°) 로 가정"""
    angs = [(float(m.group(1)), wd[2], wd[3]) for wd in av.words
            for m in [re.fullmatch(r"(\d{1,2}(?:\.\d+)?)°", wd[0])] if m]
    if len(angs) < 2 or abs(angs[0][0] - angs[1][0]) >= 0.5:
        return None
    ang = angs[0][0]
    labs = [(wd[0].upper(), wd[2], wd[3]) for wd in av.words]
    pos = [l for l in labs if re.search(r"\bFIXED\b|\bTOP\b", l[0])]
    neg = [l for l in labs if re.search(r"\bSLIDING\b|\bBTM\b|\bBOTTOM\b", l[0])]
    if not pos and not neg:
        return (ang, "fixed", 0.0, "기준 문구 없음 (FIXED 쪽 가정)")
    kind = "top" if any(re.search(r"\bTOP\b", l[0]) for l in pos) or any(re.search(r"BTM|BOTTOM", l[0]) for l in neg) else "fixed"
    if pos and neg:
        cx, cy = (pos[0][1] + neg[0][1]) / 2, (pos[0][2] + neg[0][2]) / 2
        vx, vy = pos[0][1] - cx, -(pos[0][2] - cy)
    else:
        if not av.bbox:
            return (ang, kind, 0.0 if pos else 180.0, "뷰 영역 없음")
        cx, cy = (av.bbox[0] + av.bbox[2]) / 2, (av.bbox[1] + av.bbox[3]) / 2
        lx, ly = (pos or neg)[0][1], (pos or neg)[0][2]
        vx, vy = (lx - cx, -(ly - cy)) if pos else (cx - lx, -(cy - ly))
    nrm = math.hypot(vx, vy)
    if nrm < 1e-6:
        return (ang, kind, 0.0, "기준 문구 위치 판별 불가")
    vx, vy = vx / nrm, vy / nrm
    px, py = vy, -vx                                   # 화면에서 + 방향을 시계 방향으로 90° 돌린 방향
    gx = (angs[0][1] + angs[1][1]) / 2 - cx
    gy = -((angs[0][2] + angs[1][2]) / 2 - cy)
    d = math.degrees(math.atan2(gx * px + gy * py, gx * vx + gy * vy))
    snap = round(d / 90.0) * 90.0
    if abs(d - snap) <= 25:
        d = snap
    if d == -180.0:
        d = 180.0
    return (ang, kind, d + 0.0, "")


class DrawingSet:
    def __init__(self, drawings: List[DrawingInfo]):
        self.drawings = sorted(drawings, key=lambda d: (d.drawing_no, d.sheet))
        self.by_short: Dict[str, DrawingInfo] = {}
        for d in self.drawings:
            self.by_short[d.short] = d
        self.edges: List[Edge] = []
        self.fact_matrix: Dict[str, Dict[str, object]] = {}
        self.conflicts: List[dict] = []
        self.mark_index: Dict[str, List[Tuple[str, str, str]]] = {}   # mark -> [(short, role, view)]
        self.resolutions: List[Resolution] = []
        self.ai_results: List[dict] = []
        self.log: List[str] = []
        self._inherit_titles()
        self._index_marks()
        self._facts()
        self._relations()

    # ---- 생성 ------------------------------------------------------------
    @classmethod
    def from_paths(cls, paths: List[str], verbose: bool = True) -> "DrawingSet":
        infos = []
        for p in paths:
            t0 = time.time()
            info = extract_drawing_info(p)
            infos.append(info)
            if verbose:
                print(f"  - {info.short:12s} {GRADE_KO[info.grade]:9s} {info.kind_ko:12s} "
                      f"뷰 {len(info.views):2d} 마크 {len(info.marks):2d} 치수 {len(info.dims):2d} "
                      f"사실 {len(info.facts):2d}  ({time.time() - t0:.1f}s)")
        return cls(infos)

    @classmethod
    def from_folder(cls, folder: str, verbose: bool = True) -> "DrawingSet":
        paths = list_pdfs(folder)
        if verbose:
            print(f"도면 세트: {folder} ({len(paths)} 장)")
        return cls.from_paths(paths, verbose)

    def _inherit_titles(self) -> None:
        for d in self.drawings:
            if d.grade != "D" or d.title:
                continue
            sib = [s for s in self.drawings if s.drawing_no == d.drawing_no and s is not d and s.title]
            if sib:
                s = sib[0]
                d.title = re.sub(r"\(\d+/\d+\)", f"({d.sheet}/{d.sheet_total})", s.title)
                d.subtitle, d.kind, d.project, d.tag_no = s.subtitle, s.kind, s.project, s.tag_no
                d.owner_dwg_no = s.owner_dwg_no
                d.title_inferred = True
                d.log.append(f"제목/종류를 {s.short} 에서 물려받음 (스캔 도면)")

    def _index_marks(self) -> None:
        for d in self.drawings:
            roles: Dict[str, set] = {}
            if d.kind == "ga":
                role = "list" if "nozzle_list" in d.parser_kinds else ("elevation" if "elevation" in d.parser_kinds else "ga")
                for mk in d.marks:
                    roles.setdefault(mk, set()).add((role, ""))
            for v in d.views:
                for mk in v.marks:
                    roles.setdefault(mk, set()).add(("detail" if d.kind == "nozzle_detail" else "view", v.name))
            for p in d.parts:
                for mk in p.marks:
                    roles.setdefault(mk, set()).add(("bom", ""))
            for mk in d.tl_labels:
                roles.setdefault(mk, set()).add(("development", ""))
            for mk in d.marks:
                roles.setdefault(mk, set())
                if not roles[mk]:
                    roles[mk].add(("mention", ""))
            for mk, rs in roles.items():
                for role, view in sorted(rs):
                    self.mark_index.setdefault(mk, []).append((d.short, role, view))

    def _facts(self) -> None:
        for d in self.drawings:
            for f in d.facts:
                key = FACT_ALIASES.get(f.key, f.key)
                self.fact_matrix.setdefault(key, {})
                if d.short not in self.fact_matrix[key]:
                    self.fact_matrix[key][d.short] = f.value
        for key, vals in self.fact_matrix.items():
            if len(vals) < 2 or key in NON_COMPARABLE:
                continue
            shorts = list(vals)
            base = vals[shorts[0]]
            diff = [s for s in shorts[1:] if not _same(base, vals[s])]
            if diff:
                self.conflicts.append({"key": key, "values": {s: _fmt(v) for s, v in vals.items()}})

    # ---- 연관성 ----------------------------------------------------------
    def _add_edge(self, kind: str, a: DrawingInfo, b: DrawingInfo, detail: str = "", items=None) -> None:
        self.edges.append(Edge(kind, a.short, b.short, detail, sorted(items or [], key=_mark_key)))

    def _relations(self) -> None:
        ds = self.drawings
        # 1. 시트 시리즈
        series: Dict[str, List[DrawingInfo]] = {}
        for d in ds:
            series.setdefault(d.drawing_no, []).append(d)
        for no, sheets in series.items():
            for s in sheets[1:]:
                self._add_edge("sheet_series", sheets[0], s, f"{no} 시트 {sheets[0].sheet}/{s.sheet_total} -> {s.sheet}/{s.sheet_total}")
        # 2. 노즐 마크 공유 (역할이 다른 도면끼리)
        def role_of(d: DrawingInfo) -> str:
            if d.kind == "ga":
                return "list" if "nozzle_list" in d.parser_kinds else "elevation"
            return d.kind
        for i, a in enumerate(ds):
            for b in ds[i + 1:]:
                if not a.marks or not b.marks or a.drawing_no == b.drawing_no:
                    continue
                shared = sorted(set(a.marks) & set(b.marks), key=_mark_key)
                if not shared:
                    continue
                ra, rb = role_of(a), role_of(b)
                if ra == rb and a.kind != "nozzle_detail":
                    continue
                self._add_edge("nozzle_mark", a, b, f"{len(shared)} 개 마크 공유 ({compact_marks(shared)})", shared)
        # 3. 용접선 공유
        for i, a in enumerate(ds):
            for b in ds[i + 1:]:
                shared = sorted(set(a.seams) & set(b.seams), key=lambda s: (s[0], int(s.split("-")[1])))
                if len(shared) >= 3 and a.drawing_no != b.drawing_no:
                    self._add_edge("weld_seam", a, b, f"용접선 {len(shared)} 개 공유 ({shared[0]} ~ {shared[-1]})", shared)
        # 4. 단면 표식 -> 다른 시트의 뷰 (같은 도면번호 안에서)
        for d in ds:
            for letter in d.unresolved_markers:
                for s in series.get(d.drawing_no, []):
                    if s is d:
                        continue
                    hit = [v.name for v in s.views if re.search(r'"\s*' + re.escape(letter) + r'\s*"', v.name)]
                    if hit:
                        self._add_edge("section_view", d, s, f'표식 "{letter}" -> {s.short} 의 {hit[0]}', [letter])
        # 5. GA 'SEE DWG.' -> 상세도
        for d in ds:
            if not d.see_dwg_marks:
                continue
            targets: Dict[str, List[str]] = {}
            for mk in d.see_dwg_marks:
                for short, role, view in self.mark_index.get(mk, []):
                    if role == "detail":
                        targets.setdefault(short, []).append(mk)
                        break
            for short, marks in targets.items():
                self._add_edge("see_dwg", d, self.by_short[short],
                               f"투영 'SEE DWG.' 마크 {len(marks)} 개의 상세 ({compact_marks(marks)})", marks)
            missing = [mk for mk in d.see_dwg_marks if not any(r == "detail" for _, r, _ in self.mark_index.get(mk, []))]
            if missing:
                self.log.append(f"{d.short}: SEE DWG. 마크 중 상세도를 못 찾음: {compact_marks(missing)}")
        # 6. 다른 도면의 제목/번호 언급
        for d in ds:
            if not d.text:
                continue
            for o in ds:
                if o is d or o.drawing_no == d.drawing_no or not o.title:
                    continue
                base_title = re.sub(r"\s*\(\d+/\d+\)", "", o.title).strip()
                if len(base_title) > 12 and base_title in d.text and o.sheet == 1:
                    self._add_edge("title_ref", d, o, f"'{base_title}' 언급")
                elif o.drawing_no and o.drawing_no in d.text and o.sheet == 1:
                    self._add_edge("title_ref", d, o, f"{o.drawing_no} 언급")
        # 7. 같은 사실 공유 / 불일치 (도면 쌍별로 묶음)
        pair_match: Dict[Tuple[str, str], List[str]] = {}
        pair_conf: Dict[Tuple[str, str], List[str]] = {}
        for key, vals in self.fact_matrix.items():
            if key in NON_COMPARABLE:
                continue
            shorts = list(vals)
            for i, a in enumerate(shorts):
                for b in shorts[i + 1:]:
                    if self.by_short[a].drawing_no == self.by_short[b].drawing_no:
                        continue
                    (pair_match if _same(vals[a], vals[b]) else pair_conf).setdefault((a, b), []).append(key)
        for (a, b), keys in pair_match.items():
            self._add_edge("fact_match", self.by_short[a], self.by_short[b],
                           ", ".join(FACT_KO.get(k, k) for k in keys), keys)
        for (a, b), keys in pair_conf.items():
            self._add_edge("fact_conflict", self.by_short[a], self.by_short[b],
                           ", ".join(f"{FACT_KO.get(k, k)}: {_fmt(self.fact_matrix[k][a])} vs {_fmt(self.fact_matrix[k][b])}" for k in keys), keys)

    # ---- 누락 값 보완 ----------------------------------------------------
    def spec_sources(self) -> List[str]:
        """VesselSpec 파서(pdf_dimension_parser)가 읽을 수 있는 시트 (리프팅/GA)"""
        return [d.path for d in self.drawings if d.parser_kinds and d.kind in ("lifting", "ga")]

    def detail_views(self, mark: str) -> List[Tuple[DrawingInfo, object]]:
        out = []
        for d in self.drawings:
            if d.kind != "nozzle_detail":
                continue
            for v in d.views:
                if mark in v.marks:
                    out.append((d, v))
        return out

    @staticmethod
    def _hillside(n: NozzleSpec, d: DrawingInfo, v, dims, spec: VesselSpec) -> Optional[dict]:
        """힐사이드 노즐 상세 뷰 해석.
        표기: 'VESSEL C.L' 수평선과 축 사이 각도(예 45°), W.P(축과 쉘 내면 R_i 교점)의 높이 'nnnn TO C.L'(세로 치수),
        W.P 에서 플랜지 면까지 축 방향 길이(320; 괄호 값 274.9 = 쉘 외면부터 = 320 - t45).
        모델러 규약으로 변환: 축 방향 theta(TOP 기준), 축이 지나는 점 o (offset_y, offset_z, 축에 수직),
        투영 = o 에서 플랜지 면까지 축 방향 거리."""
        # 'VESSEL C.L' 은 뷰 그림 위쪽에 떨어져 있어 확장 영역(가까운 뷰에 배정된 문구 포함)으로 읽는다
        words = getattr(v, "region_words", None) or getattr(v, "words", None) or []
        texts_up = [(t, x, y) for t, up, x, y in words if up]
        if not any(t.replace(" ", "") in ("W.P", "W.P.") for t, _, _ in texts_up):
            return None
        cl = [(x, y) for t, x, y in texts_up if "VESSEL C.L" in t.upper()]
        angles = [(float(m.group(1)), x, y) for t, x, y in texts_up
                  for m in [re.fullmatch(r"(\d{1,2}(?:\.\d)?)°", t)] if m and float(m.group(1)) not in (30.0, 37.5)]
        if not cl or not angles:
            return None
        alpha = min(angles, key=lambda a: math.hypot(a[1] - cl[0][0], a[2] - cl[0][1]))[0]
        vt = [dm for dm in dims if not dm.upright]
        if len(vt) != 1 or spec.inner_radius <= 0:
            return None
        v_off = vt[0].value
        if v_off >= spec.inner_radius:
            return None
        nums = [float(t) for t, _, _ in texts_up if re.fullmatch(r"\d{2,3}(?:\.\d)?", t) and 100 <= float(t) <= 800]
        parens = [float(m.group(1)) for t, _, _ in texts_up for m in [re.fullmatch(r"\((\d{2,3}(?:\.\d)?)\)", t)] if m]
        L = None
        t_shell = spec.shell_thickness
        for p in parens:                       # 괄호(외면 기준) + 쉘 두께 = W.P 기준 길이
            for x in nums:
                if abs(x - (p + t_shell)) <= 1.5:
                    L = x
        if L is None and len(nums) >= 2:       # 대안: 두 번째로 큰 값 (가장 큰 값은 관통부 먼 쪽 모서리 기준)
            L = sorted(set(nums), reverse=True)[1]
        if L is None:
            return None
        below, zpos = 90 < n.angle_deg < 270, n.angle_deg < 180
        theta = (90.0 + alpha if below else 90.0 - alpha) if zpos else (270.0 - alpha if below else 270.0 + alpha)
        y_wp = -v_off if below else v_off
        z_wp = math.sqrt(spec.inner_radius ** 2 - v_off ** 2) * (1 if zpos else -1)
        th = math.radians(theta)
        dy, dz = math.cos(th), math.sin(th)
        s_wp = y_wp * dy + z_wp * dz
        oy, oz = y_wp - s_wp * dy, z_wp - s_wp * dz
        proj = s_wp + L
        r_f = math.hypot(oy + proj * dy, oz + proj * dz)
        return {
            "theta": round(theta, 1), "oy": round(oy, 1), "oz": round(oz, 1), "proj": round(proj, 1),
            "new": f"힐사이드 {theta:g}°, 오프셋 ({oy:.0f}, {oz:.0f}), 투영 {proj:.0f} (플랜지 중심 반경 {r_f:.0f})",
            "evidence": f"W.P {v_off:g} TO C.L(쉘 내면 R{spec.inner_radius:g}), 축 {alpha:g}°, W.P→플랜지 면 {L:g}",
            "note": (f"힐사이드 노즐: 축 {alpha:g}° (TOP 기준 {theta:g}°), W.P 높이 {y_wp:g} (쉘 내면), "
                     f"W.P→플랜지 {L:g} → 투영 {proj:.1f}, 축 오프셋 ({oy:.1f}, {oz:.1f}) (상세도 {d.short})"),
        }

    @staticmethod
    def _saddle_from_sheet(d: DrawingInfo, spec: VesselSpec, apply: bool) -> str:
        """새들 상세 시트 -> 판 두께(부품표), 축 방향 길이('a b a' 행과 그 합), 감싸는 각(NNN°), 리브 위치(@pitch x n)"""
        def thick(name_re):
            for p in d.parts:
                if re.search(name_re, p.name, re.I):
                    m = re.search(r"t(\d+(?:\.\d+)?)", p.remark)
                    if m:
                        return float(m.group(1))
            return 0.0
        base_t, web_t, rib_t, wear_t = thick(r"BASE\s*PLATE"), thick(r"WEB\s*PLATE"), thick(r"RIB\s*PLATE"), thick(r"SUPPORT\s*PLATE")
        if base_t <= 0 or web_t <= 0:
            return ""
        views = [v for v in d.views if re.search(r"SADDLE", v.name, re.I) and v.words] or [v for v in d.views if v.words]
        wrap, web_wrap, pitch, count, web_len, length = 0.0, 0.0, 0.0, 0, 0.0, 0.0
        for v in views:
            texts = [w[0] for w in v.words]
            angs = sorted({float(m.group(1)) for t in texts for m in [re.fullmatch(r"(\d{2,3})°", t)] if m and 90 <= float(m.group(1)) <= 180})
            if angs and not wrap:
                # 새들 뷰의 각도 라벨: 큰 각 = 서포트(웨어) 플레이트가 감싸는 각, 작은 각 = 웹 판 뿔(horn) 사이 각.
                # 하나뿐이면 둘 다 그 값 (0004 2/2: 152°/140°, 웨어 플레이트가 뿔보다 6° 씩 더 감쌈)
                wrap, web_wrap = max(angs), min(angs)
            for t in texts:
                m = re.fullmatch(r"@(\d+(?:\.\d+)?)\s*[xX]\s*(\d+)\s*=\s*(\d+(?:\.\d+)?)", t)
                if m and not pitch:
                    pitch, count = float(m.group(1)), int(m.group(2))
            nums = sorted({float(t) for t in texts if re.fullmatch(r"\d{4}", t)})
            if pitch and nums and not web_len:
                cands = [x for x in nums if pitch * count < x < (spec.saddle_base_width or 1e9)]
                if cands:
                    web_len = cands[0]
        # 축 방향 길이: 'a b a' 로 나뉜 행(50 500 50)과 그 합(600)이 같은 시트에 있으면 그 값
        all_text = " ".join(getattr(d, "rows_text", []) or [])
        for row in getattr(d, "rows_text", []) or []:
            nums = re.findall(r"(?<![\d.])(\d{2,4})(?![\d.])", row)
            for i in range(len(nums) - 2):
                a, b, c = nums[i], nums[i + 1], nums[i + 2]
                if a == c and float(a) <= 100 < float(b):
                    total = float(a) * 2 + float(b)
                    if re.search(rf"(?<![\d.]){total:g}(?![\d.])", all_text):
                        length = total
                        break
            if length:
                break
        # 서포트(웹) 판이 몇 장인지: 부품표 수량 'a x b' 의 a 가 새들 한 대당 개수. 두 장이면 축 방향으로 벌려 세운다
        def per_saddle(name_re: str) -> int:
            for p in d.parts:
                if re.search(name_re, p.name, re.I):
                    m = re.match(r"(\d+)", (p.qty or "").strip())
                    if m:
                        return int(m.group(1))
            return 0

        web_gap = 0.0
        if max(per_saddle(r"SUPPORT\s*PLATE"), per_saddle(r"WEB\s*PLATE")) >= 2:
            # 간격은 단면 뷰(SECTION "B")의 숫자 중 베이스 축 길이보다 작은 가장 큰 값 (0004 2/2: 600 안의 480)
            for v in d.views:
                if not re.match(r"SECTION", v.name, re.I) or not v.words:
                    continue
                nums = [float(t) for t, up, x, y in v.words if re.fullmatch(r"\d{2,4}(?:\.\d)?", t)]
                cands = [n for n in nums if 50 < n < (length or 1e9)]
                if cands:
                    web_gap = max(cands)
                    break
        parts = [f"베이스 t{base_t:g}", f"웹 t{web_t:g}" + (f" x2 (간격 {web_gap:g})" if web_gap else "")]
        if rib_t:
            parts.append(f"리브 t{rib_t:g}")
        if wear_t:
            parts.append(f"서포트 t{wear_t:g}")
        if length:
            parts.append(f"축 길이 {length:g}")
        if wrap:
            parts.append(f"웨어 플레이트 {wrap:g}°" + (f", 웹 뿔 {web_wrap:g}°" if web_wrap and web_wrap != wrap else ""))
        if pitch:
            parts.append(f"리브 @{pitch:g}x{count}")
        if apply:
            spec.saddle_base_t, spec.saddle_web_t, spec.saddle_rib_t, spec.saddle_wear_t = base_t, web_t, rib_t, wear_t
            if length:
                spec.saddle_length = length
            if wrap:
                spec.saddle_wrap_deg, spec.saddle_web_wrap_deg = wrap, web_wrap
            if web_len:
                spec.saddle_web_length = web_len
            if web_gap:
                spec.saddle_web_spacing = web_gap
            if pitch and count:
                spec.saddle_rib_z = [round((i - count / 2) * pitch, 1) for i in range(count + 1)]
        return ", ".join(parts)

    def resolve(self, spec: VesselSpec, apply: bool = True) -> List[Resolution]:
        """GA 에서 확정하지 못한 노즐 값을 상세도/전개도로 보완·검증한다."""
        res: List[Resolution] = []
        dev = next((d for d in self.drawings if d.kind == "development" and d.tl_labels), None)
        for n in sorted(spec.nozzles, key=lambda n: _mark_key(n.mark)):
            views = self.detail_views(n.mark)
            if not views:
                if n.projection is None or _estimated(n, "projection"):
                    res.append(Resolution(n.mark, "projection", _fmt(n.projection) if n.projection else "-", "",
                                          "", "노즐 상세 뷰를 찾지 못함", "unresolved"))
                continue
            d, v = views[0]
            src = f"{d.short} '{v.name}'"
            ref = "T.L" if n.on_head else "C.L"
            dims = [dm for dm in d.dims if dm.view == v.name and dm.ref == ref]
            inclined = n.angle_deg is not None and not n.on_head and abs(n.angle_deg % 90) > 0.5
            handled = False
            # 1) GA 단면도가 '반경 방향 임의 각도' 로 추정한 노즐: 상세 뷰의 표기 방식으로 실제 형상을 정한다
            #    a. 'W.P' + 'VESSEL C.L' + 각도 라벨 + TO C.L 하나  -> 힐사이드 노즐 (축이 중심을 지나지 않음)
            #    b. 정립 TO C.L(가로) + 회전 TO C.L(세로)            -> 수평 노즐 + 높이 오프셋 (N24 류와 같은 표기)
            if dims and inclined:
                hs = self._hillside(n, d, v, dims, spec)
                if hs:
                    old = (f"{n.angle_deg:g}° 반경 방향, 투영 {_fmt(n.projection) if n.projection else '-'}")
                    if apply:
                        n.angle_deg, n.offset_y, n.offset_z, n.projection = hs["theta"], hs["oy"], hs["oz"], hs["proj"]
                        n.notes = [t for t in n.notes if not t.startswith(("투영", "방향"))]
                        n.notes.append(hs["note"])
                    res.append(Resolution(n.mark, "projection", old, hs["new"], src, hs["evidence"], "resolved"))
                    handled = True
                else:
                    hz = sorted([dm for dm in dims if dm.upright], key=lambda dm: -dm.value)
                    vt = sorted([dm for dm in dims if not dm.upright], key=lambda dm: -dm.value)
                    if hz and vt:
                        below, zpos = 90 < n.angle_deg < 270, n.angle_deg < 180
                        theta = 90.0 if zpos else 270.0
                        off = -vt[0].value if below else vt[0].value
                        old = f"{n.angle_deg:g}° 반경 방향, 투영 {_fmt(n.projection) if n.projection else '-'}"
                        if apply:
                            n.angle_deg, n.offset_y, n.projection = theta, off, hz[0].value
                            n.notes = [t for t in n.notes if not t.startswith(("투영", "방향"))]
                            n.notes.append(f"수평 노즐 {theta:g}°, 높이 오프셋 {off:g}, 투영 {hz[0].value:g} (상세도 {d.short})")
                        res.append(Resolution(n.mark, "projection", old,
                                              f"{theta:g}° 수평, 오프셋 {off:g}, 투영 {hz[0].value:g}", src,
                                              f"{hz[0].text}(가로) + {vt[0].text}(세로)", "resolved"))
                        handled = True
                    else:
                        vals = sorted({dm.value for dm in dims}, reverse=True)
                        res.append(Resolution(n.mark, "projection", _fmt(n.projection) if n.projection else "-", "",
                                              src, f"경사 노즐: TO C.L {vals} 만으로는 투영 확정 불가 (GA 값 유지)", "info"))
                        handled = True
            if handled:
                pass
            elif dims:
                proj = max(dm.value for dm in dims)
                ev = next(dm.text for dm in dims if dm.value == proj)
                if n.projection is None or _estimated(n, "projection"):
                    old = _fmt(n.projection) if n.projection else "-"
                    if apply:
                        n.projection = proj
                        n.notes = [t for t in n.notes if not t.startswith("투영")]
                        n.notes.append(f"투영 {proj:g} (상세도 {d.short} '{ev}')")
                    res.append(Resolution(n.mark, "projection", old, _fmt(proj), src, ev, "resolved"))
                elif _same(n.projection, proj):
                    res.append(Resolution(n.mark, "projection", _fmt(n.projection), _fmt(proj), src, ev, "confirmed"))
                else:
                    # 플랜지 면 치수가 같은 시트의 보조 뷰(VIEW A-A 등)에 있는 경우: 시트 전체에서 같은 값을 찾는다
                    same_sheet = [dm for dm in d.dims if dm.ref == ref and _same(dm.value, n.projection)]
                    if same_sheet:
                        dm = same_sheet[0]
                        res.append(Resolution(n.mark, "projection", _fmt(n.projection), _fmt(dm.value),
                                              f"{d.short} '{dm.view}'", f"{dm.text} (보조 뷰)", "confirmed"))
                    else:
                        res.append(Resolution(n.mark, "projection", _fmt(n.projection), _fmt(proj), src, ev, "conflict"))
                # 2) 수평 노즐의 높이 오프셋: 투영이 아닌 나머지 TO C.L 값
                others = sorted({dm.value for dm in dims if dm.value != proj})
                if others and not n.on_head and n.angle_deg is not None and n.angle_deg % 180 == 90:
                    off = max(others)
                    if _estimated(n, "offset_y") and off > 50:
                        old = _fmt(n.offset_y)
                        if apply:
                            n.offset_y = -off if n.offset_y < 0 else off
                            n.notes.append(f"높이 오프셋 |{off:g}| (상세도 {d.short})")
                        res.append(Resolution(n.mark, "offset_y", old, _fmt(n.offset_y), src, f"{off:g} TO C.L", "resolved"))
                    elif abs(abs(n.offset_y) - off) <= 1.0:
                        res.append(Resolution(n.mark, "offset_y", _fmt(n.offset_y), _fmt(off), src, f"{off:g} TO C.L", "confirmed"))
                    else:
                        res.append(Resolution(n.mark, "offset_y", _fmt(n.offset_y), _fmt(off), src, f"{off:g} TO C.L", "conflict"))
                elif others and n.on_head:
                    off = max(others)
                    st = "confirmed" if abs(abs(n.offset_y) - off) <= 1.0 else "conflict"
                    res.append(Resolution(n.mark, "offset_y", _fmt(n.offset_y), _fmt(off), src, f"{off:g} TO C.L", st))
                elif others and n.angle_deg is not None and n.angle_deg % 180 == 0:
                    # 수직 노즐(0°/180°)의 가로 'nnn TO C.L' = 축의 좌우 오프셋 (예: N4~N4E 엔트런스 부싱 400)
                    hz = [dm.value for dm in dims if dm.upright and dm.value != proj]
                    if hz:
                        off = max(hz)
                        if abs(n.offset_z) < 1.0:
                            if apply:
                                n.offset_z = off      # 좌우 방향(90°/270° 쪽)은 GA 단면도가 없으면 알 수 없음 -> + 로 두고 표시
                                n.notes.append(f"좌우 오프셋 |{off:g}| (상세도 {d.short}, 방향 미확정)")
                            res.append(Resolution(n.mark, "offset_z", "0", f"{off:g} (방향 미확정)", src, f"{off:g} TO C.L (가로)", "resolved"))
                        elif abs(abs(n.offset_z) - off) <= 1.0:
                            res.append(Resolution(n.mark, "offset_z", _fmt(n.offset_z), _fmt(off), src, f"{off:g} TO C.L (가로)", "confirmed"))
                        else:
                            res.append(Resolution(n.mark, "offset_z", _fmt(n.offset_z), _fmt(off), src, f"{off:g} TO C.L (가로)", "conflict"))
            elif n.projection is None or _estimated(n, "projection"):
                res.append(Resolution(n.mark, "projection", _fmt(n.projection) if n.projection else "-", "",
                                      src, f"뷰에 TO {ref} 치수 없음", "unresolved"))
            # 2b) 상세 뷰의 벡터 외곽선 -> 노즐 단면 (플랜지/허브/보스 계단형). 표준 플랜지 외경과 대조
            pr = getattr(v, "profile", None)
            if pr and pr.get("points") and not n.profile:
                od_std = n.standard_flange_od()
                std = (od_std,) if od_std else None
                fl_ok = std is not None and abs(pr["flange_od"] - std[0]) <= 0.06 * std[0]
                bore = pr.get("bore_d")
                bore_ok = bore is None or n.neck_id <= 0 or abs(bore - n.neck_id) <= 3.0
                if apply:
                    n.profile = [list(p) for p in pr["points"]]
                    n.notes.append(f"단면 외곽선 {len(pr['points'])}점 (상세도 {d.short}, 플랜지 Ø{pr['flange_od']:g}"
                                   f"{', 보어 Ø' + format(bore, 'g') if bore else ''}{', 부속 잘라냄' if pr.get('truncated') else ''})")
                desc = (f"외곽선 {len(pr['points'])}점, 플랜지 Ø{pr['flange_od']:g}, 길이 {pr['length']:g}"
                        f"{', 보어 Ø' + format(bore, 'g') if bore else ''}")
                st = "resolved" if (fl_ok and bore_ok) else "info"
                ev = "벡터 물체선" + ("" if fl_ok else f" (표준 플랜지 Ø{std[0]:g} 와 차이)" if std else "") + \
                     ("" if bore_ok else f" (보어 {bore:g} vs 목 내경 {n.neck_id:g})")
                res.append(Resolution(n.mark, "profile", "균일 목 + 표준 플랜지", desc, src, ev, st))
            # 2c) 블라인드 플랜지 / 개스킷 / 지지판 (부품표 + 상세 뷰)
            blk = [p for p in d.parts if n.mark in p.marks]
            if any(re.search(r"BLIND\s*FLANGE|BL\.?RF", p.name + " " + p.remark, re.I) for p in blk) and not n.blind:
                gk = next((p for p in blk if re.search(r"GASKET", p.name, re.I)), None)
                # 비고가 두 줄로 흩어져 있어도 읽히게 공백 제거 후 't4.5'
                mg = re.search(r"t(\d+(?:\.\d+)?)", re.sub(r"\s+", "", gk.remark)) if gk else None
                if apply:
                    n.blind = True
                    n.gasket_t = float(mg.group(1)) if mg else 0.0
                    n.notes.append(f"블라인드 플랜지 (부품표{', 개스킷 t' + mg.group(1) if mg else ''})")
                res.append(Resolution(n.mark, "blind", "-", f"블라인드 플랜지{' + 개스킷 t' + mg.group(1) if mg else ''}",
                                      d.short, "부품표 BLIND FLANGE", "resolved"))
            sp = (pr or {}).get("supports") if pr else None
            sup_part = next((p for p in blk if re.search(SUPPORT_PLATE_RE, p.name, re.I)), None)
            # 지지판이 있다는 근거는 뷰 문구('n-SUP'T PLATES') 또는 부품표(SUPPORT PLATE) 뿐이다. 사선 기하만으로는
            # 판이라 보지 않는다 (N8 단조 노즐의 원뿔 허브, 용접 개선 선도 목에서 바깥으로 뻗는 사선이므로)
            if ((sp and sp.get("n")) or sup_part) and n.support_n <= 0:
                sp = sp or {}
                # 개수/두께: 뷰 문구 > 부품표 (수량 'a x b' 의 a 를 블록의 마크 수로 나눔, 비고 t10)
                cnt = int(sp.get("n") or 0)
                if cnt <= 0 and sup_part:
                    mq = re.match(r"(\d+)", sup_part.qty or "")
                    if mq:
                        cnt = max(1, int(mq.group(1)) // max(1, len(sup_part.marks)))
                mt = re.search(r"t(\d+(?:\.\d+)?)", re.sub(r"\s+", "", sup_part.remark)) if sup_part else None
                t = sp.get("t") or (float(mt.group(1)) if mt else 0.0)
                w = float(sp.get("w") or 0.0)
                # 방위각: 노즐 뷰의 표식("A")과 같은 글자의 보조 뷰(VIEW "A")에서 같은 각도 라벨 두 개 -> ±각.
                # 기준 방향은 보조 뷰 문구(FIXED/SLIDING SIDE 또는 TOP/BTM)와 각도 라벨 쌍의 위치 관계로 (_aux_azimuth)
                nv = next((vw for vw in d.views if vw.kind == "nozzle" and n.mark in vw.marks), None)
                letters = list(nv.letters) if nv else []
                aux_views = [av for av in d.views if av.kind in ("view", "section") and av.words]
                cands = [(av, _aux_azimuth(av)) for av in aux_views]
                cands = [(av, r_) for av, r_ in cands if r_]
                hit = [(av, r_) for av, r_ in cands if any(re.search(r'"\s*%s\s*"' % re.escape(L), av.name) for L in letters)]
                az, ref, az_dir, az_src, az_sure = [], "", 0.0, "", False
                if hit or cands:
                    av, (ang_, ref, az_dir, how) = (hit or cands)[0]
                    az = [ang_, -ang_]
                    distinct = {(r_[0], r_[1], r_[2]) for _, r_ in cands}
                    az_sure = bool(hit) or len(distinct) == 1
                    az_src = av.name + ("" if hit else (" (표식 없음, 시트에 하나뿐)" if az_sure else " (표식 없음, 첫 보조 뷰로 추정)")) \
                        + (f", {how}" if how else "")
                if not az:
                    az = [90.0 * i for i in range(cnt)] if cnt not in (0, 2) else [30.0, -30.0]
                cnt = cnt or 2
                geom = float(sp.get("length") or 0.0) > 0
                if apply:
                    n.support_n, n.support_t, n.support_w = cnt, t, w
                    if geom:
                        n.support_len, n.support_angle_deg, n.support_s = sp["length"], sp["angle_deg"], sp["s_attach"]
                    n.support_azimuths, n.support_az_ref, n.support_az_dir = az[:cnt], ref, az_dir
                ref_ko = f"{_REF_KO.get(ref, '기준 미상')} 에서 {az_dir:+g}°"
                if apply:
                    n.notes.append(f"지지판 {cnt}장 t{t:g}xW{w:g}"
                                   + (f", 축과 {sp['angle_deg']:g}°, 목 위치 {sp['s_attach']:g} ({'치수' if sp.get('s_attach_src') == 'dim' else '그림'})" if geom else "")
                                   + f", 방위 {az[:cnt]} ({ref_ko}{', ' + az_src if az_src else ''}) (상세도 {d.short})")
                desc = (f"{cnt}장 t{t:g}xW{w:g}"
                        + (f" 각도 {sp['angle_deg']:g}° 목 위치 {sp['s_attach']:g}({'치수' if sp.get('s_attach_src') == 'dim' else '그림'}) 길이 {sp['length']:g}" if geom else " (기하 없음)")
                        + f" 방위 {az[:cnt]} {ref_ko}")
                ev = ("상세 뷰 문구 + 굵은 사선" if sp.get("n") else "부품표 SUPPORT PLATE" + (" + 굵은 사선" if geom else "")) \
                    + (f" + {az_src}" if az_src else " (보조 뷰 없음, 기본 방위)")
                res.append(Resolution(n.mark, "supports", "-", desc, src, ev, "resolved" if (geom and az_sure) else "info"))
            # 3) 부품표 BOSS Ø vs 목 외경, 플랜지 표기
            for p in d.parts:
                if n.mark not in p.marks:
                    continue
                m = re.search(r"Ø(\d+(?:\.\d+)?)", p.remark)
                if re.search(r"BOSS", p.name, re.I) and m:
                    boss = float(m.group(1))
                    if n.neck_od <= 0:
                        if apply:
                            n.neck_od = boss
                            n.notes.append(f"목 외경 {boss:g} (상세도 부품표 BOSS)")
                        res.append(Resolution(n.mark, "neck_od", "-", _fmt(boss), d.short, f"BOSS {p.remark}", "resolved"))
                    else:
                        st = "confirmed" if _same(n.neck_od, boss) else "conflict"
                        res.append(Resolution(n.mark, "neck_od", _fmt(n.neck_od), _fmt(boss), d.short, f"BOSS {p.remark}", st))
                if re.search(r"^FLANGE", p.name, re.I) and p.remark and n.n_bolts <= 0:
                    # 볼트 구멍 표준: 부품표 비고에 SERIES A 가 있으면 B16.47A, 아니면 담당자 규칙(24" 까지 B16.5, 26" 부터 B16.47B)
                    txt = p.remark.upper()
                    # 공백을 지우지 않은 문자열로: 'SERIES A CL.300', 'SERIES "A"', 'B16.47A'
                    hint = "A" if (re.search(r"SERIES?\s*[\"']?\s*A(?![A-Z0-9])", txt)
                                   or "B16.47A" in re.sub(r"\s+", "", txt)) else ""
                    desc = n.apply_flange_standard(hint)
                    if desc:
                        if apply:
                            n.notes.append(f"볼트 구멍 {n.n_bolts}-Ø{n.bolt_hole_d:g} B.C {n.bolt_circle:g} ({n.flange_std})")
                        res.append(Resolution(n.mark, "bolt_holes", "-", desc, d.short,
                                              "ASME 표" + (" (부품표 SERIES A)" if hint else ""), "resolved"))
                if re.search(r"^FLANGE", p.name, re.I) and p.remark:
                    ok = (n.size_text and n.size_text in p.remark) and (not n.flange_type or n.flange_type in p.remark)
                    res.append(Resolution(n.mark, "flange", f"{n.size_text} {n.rating} {n.flange_type}".strip(), p.remark,
                                          d.short, "부품표 FLANGE", "confirmed" if ok else "conflict"))
            # 4) 전개도 T.L+ 위치
            if dev and n.mark in dev.tl_labels and not n.on_head:
                pos = dev.tl_labels[n.mark]
                if n.position_from_tl is None:
                    if apply:
                        n.position_from_tl = pos
                        n.notes.append(f"위치 T.L+{pos:g} (전개도 {dev.short})")
                    res.append(Resolution(n.mark, "position_from_tl", "-", _fmt(pos), dev.short, f"T.L+ {pos:g}", "resolved"))
                elif _same(n.position_from_tl, pos):
                    res.append(Resolution(n.mark, "position_from_tl", _fmt(n.position_from_tl), _fmt(pos), dev.short, f"T.L+ {pos:g}", "confirmed"))
                else:
                    # 전개도는 마크마다 위치를 글자로 적어 둔다. 입면도 지시선은 라벨이 10pt 간격으로 붙어 있어
                    # 이웃 라벨로 어긋날 수 있으므로, 값이 다르면 전개도 라벨을 따른다
                    old = _fmt(n.position_from_tl)
                    if apply:
                        n.position_from_tl = pos
                        n.notes = [t for t in n.notes if not t.startswith("위치")]
                        n.notes.append(f"위치 T.L+{pos:g} (전개도 {dev.short}, 입면도 추정 {old} 대신)")
                    res.append(Resolution(n.mark, "position_from_tl", old, _fmt(pos), dev.short,
                                          f"T.L+ {pos:g} (전개도 라벨 우선)", "resolved"))
        # 지지판 정보가 뷰 문구에 일부만 있는 노즐(N7A 처럼 '2-SUP'T' 만 남은 경우): 같은 시트·같은 사이즈 노즐 값을 물려받음
        def sheet_of(nz_) -> str:
            return next((d.short for d in self.drawings if d.kind == "nozzle_detail"
                         and (any(nz_.mark in vw.marks for vw in d.views) or any(nz_.mark in p.marks for p in d.parts))), "")

        by_sheet = {}
        for n in spec.nozzles:
            if n.support_n > 0 and n.support_t > 0 and n.support_w > 0 and n.support_len > 0:
                by_sheet.setdefault((sheet_of(n), n.size_in), n)
        for n in spec.nozzles:
            has_bom = any(n.mark in p.marks and re.search(SUPPORT_PLATE_RE, p.name, re.I)
                          for d in self.drawings if d.kind == "nozzle_detail" for p in d.parts)
            complete = n.support_n > 0 and n.support_t > 0 and n.support_w > 0 and n.support_len > 0
            if not has_bom or complete:
                continue
            sheet = sheet_of(n)
            sib = by_sheet.get((sheet, n.size_in))      # 다른 시트의 노즐은 투영·부착 위치가 달라 기하를 빌려오지 않음
            if sib is None or sib is n:
                res.append(Resolution(n.mark, "supports", "-", "지지판 기하 없음 (같은 시트·같은 사이즈의 참조 노즐 없음)",
                                      sheet, "부품표 SUPPORT PLATE", "unresolved"))
                continue
            filled = []
            if apply:
                if n.support_n <= 0:
                    n.support_n = sib.support_n
                    filled.append("개수")
                if n.support_t <= 0:
                    n.support_t = sib.support_t
                    filled.append("두께")
                if n.support_w <= 0:
                    n.support_w = sib.support_w
                    filled.append("폭")
                if n.support_len <= 0:
                    n.support_len, n.support_angle_deg, n.support_s = sib.support_len, sib.support_angle_deg, sib.support_s
                    filled.append("각도/위치/길이")
                if not n.support_azimuths:
                    n.support_azimuths, n.support_az_ref, n.support_az_dir = list(sib.support_azimuths), sib.support_az_ref, sib.support_az_dir
                    filled.append("방위")
                n.notes.append(f"지지판 {'/'.join(filled) or '값'} 은 {sib.mark} 와 동일 (부품표 SUPPORT PLATE, 뷰 문구 불완전)")
            res.append(Resolution(n.mark, "supports", "-", f"{sib.mark} 와 동일 ({sib.support_n}장 t{sib.support_t:g}xW{sib.support_w:g})"
                                  + (f": {'/'.join(filled)} 보완" if filled else ""),
                                  sheet, "부품표 SUPPORT PLATE", "resolved"))
        # 고정 새들 쪽: 같은 높이(6pt 이내)에 괄호 없는 'FIXED SIDE' 와 'SLIDING SIDE' 가 한 쌍으로 있는 곳에서 좌우를 읽어
        # 다수결. 괄호 문구('(FIXED SIDE) (EXISTING RE-USED)')는 표/범례라 제외. 모델 +X 는 도면 오른쪽(오른쪽 T.L)
        if not spec.fixed_side:
            votes = {"right": 0, "left": 0}
            srcs: List[str] = []
            for d in self.drawings:
                labs = [lb for lb in getattr(d, "side_labels", []) if "(" not in lb[0]]
                fx = [lb for lb in labs if re.search(r"FIXED", lb[0], re.I)]
                sl = [lb for lb in labs if re.search(r"SLIDING", lb[0], re.I)]
                for f_ in fx:
                    pair = [s_ for s_ in sl if abs(s_[2] - f_[2]) < 6]
                    if len(pair) == 1:
                        votes["right" if f_[1] > pair[0][1] else "left"] += 1
                        if d.short not in srcs:
                            srcs.append(d.short)
            if votes["right"] != votes["left"]:
                side = "right" if votes["right"] > votes["left"] else "left"
                if apply:
                    spec.fixed_side = side
                res.append(Resolution("(용기)", "fixed_side", "-", "오른쪽(+X)" if side == "right" else "왼쪽(-X)", ", ".join(srcs),
                                      f"FIXED/SLIDING SIDE 문구 좌우 (오른쪽 {votes['right']}, 왼쪽 {votes['left']})",
                                      "resolved" if min(votes.values()) == 0 else "info"))
        # 새들 판 구성 (몸체/서포트 상세도)
        for d in self.drawings:
            if d.kind != "body_support" or spec.saddle_base_t > 0:
                continue
            got = self._saddle_from_sheet(d, spec, apply)
            if got:
                res.append(Resolution("(새들)", "saddle_plates", f"단순 블록 두께 {spec.saddle_web_thickness:g}", got, d.short,
                                      "부품표 두께 + 새들 뷰 치수", "resolved"))
                break
        # 용기 수준 값 교차 검증
        checks = [("tl_length", spec.tl_length), ("saddle_spacing", spec.saddle_spacing), ("saddle_offset", spec.saddle_offset),
                  ("outer_diameter", spec.outer_diameter), ("inner_diameter", spec.inner_diameter),
                  ("shell_thickness", spec.shell_thickness), ("head_thickness", spec.head_thickness),
                  ("insulation_thickness", spec.insulation_thickness), ("centerline_height", spec.centerline_height)]
        for key, val in checks:
            if not val:
                continue
            vals = self.fact_matrix.get(key, {})
            agree = [s for s, v in vals.items() if _same(val, v)]
            disagree = [f"{s}={_fmt(v)}" for s, v in vals.items() if not _same(val, v)]
            if disagree:
                res.append(Resolution("(용기)", key, _fmt(val), ", ".join(disagree), ", ".join(agree), FACT_KO.get(key, key), "conflict"))
            elif agree:
                res.append(Resolution("(용기)", key, _fmt(val), _fmt(val), ", ".join(agree), FACT_KO.get(key, key), "confirmed"))
        self.resolutions = res
        return res

    # ---- 출력 ------------------------------------------------------------
    def index_text(self) -> str:
        """AI 관계 해석용 간단한 색인 (도면당 한 블록)"""
        lines = []
        for d in self.drawings:
            lines.append(f"## {d.short} | {d.drawing_no} rev {d.rev} | {d.title} | kind={d.kind} | grade={d.grade}")
            if d.views:
                lines.append("views: " + "; ".join(f"{v.name} [{v.scale}]" for v in d.views[:14]))
            if d.marks:
                lines.append("nozzle marks: " + compact_marks(d.marks))
            if d.seams:
                lines.append(f"weld seams: {len(d.seams)} ({d.seams[0]} .. {d.seams[-1]})")
            if d.unresolved_markers:
                lines.append("section markers without view here: " + ", ".join(d.unresolved_markers))
            if d.see_dwg_marks:
                lines.append("projection 'SEE DWG.' for: " + compact_marks(d.see_dwg_marks))
            if d.dims:
                lines.append("labeled dims: " + "; ".join(f"{dm.text} in {dm.view}" for dm in d.dims[:10]))
            if d.facts:
                lines.append("facts: " + "; ".join(f"{f.key}={_fmt(f.value)}" for f in d.facts[:12]))
            lines.append("")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "drawings": [d.to_dict() for d in self.drawings],
            "edges": [asdict(e) for e in self.edges],
            "fact_matrix": {k: {s: v for s, v in vals.items()} for k, vals in self.fact_matrix.items()},
            "conflicts": self.conflicts,
            "mark_index": {mk: [list(t) for t in v] for mk, v in self.mark_index.items()},
            "resolutions": [asdict(r) for r in self.resolutions],
            "ai_results": self.ai_results,
            "log": self.log,
        }

    def save_json(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)

    def drawing_table(self) -> str:
        rows = [f"{'도면':12s} {'종류':14s} {'등급':10s} {'Rev':4s} {'날짜':12s} {'뷰':>3s} {'마크':>4s} {'치수':>4s} {'부품':>4s}  제목"]
        for d in self.drawings:
            rows.append(f"{d.short:12s} {d.kind_ko:14s} {GRADE_KO[d.grade]:10s} {d.rev:4s} {d.date:12s} {len(d.views):3d} "
                        f"{len(d.marks):4d} {len(d.dims):4d} {len(d.parts):4d}  {d.title}{' (물려받음)' if d.title_inferred else ''}")
        return "\n".join(rows)

    def report_markdown(self, spec: Optional[VesselSpec] = None) -> str:
        L: List[str] = []
        L.append(f"# 도면 세트 분석 리포트\n")
        L.append(f"생성: {time.strftime('%Y-%m-%d %H:%M')} · 도면 {len(self.drawings)} 장\n")
        L.append("## 1. 도면 목록\n")
        L.append("| 도면 | 종류 | 등급 | Rev | 날짜 | 뷰 | 마크 | 라벨 치수 | 부품 | 제목 |")
        L.append("|---|---|---|---|---|---:|---:|---:|---:|---|")
        for d in self.drawings:
            L.append(f"| {d.short} | {d.kind_ko} | {GRADE_KO[d.grade]} | {d.rev} | {d.date} | {len(d.views)} | {len(d.marks)} | "
                     f"{len(d.dims)} | {len(d.parts)} | {d.title}{' *(물려받음)*' if d.title_inferred else ''} |")
        L.append("\n## 2. 도면별 추출 내용\n")
        for d in self.drawings:
            L.append(f"### {d.short} — {d.title}\n")
            L.append(f"- 파일: `{d.file}` · {d.kind_ko} · {GRADE_KO[d.grade]} · Rev {d.rev} · {d.date} · 발주처 도면 {d.owner_dwg_no}")
            if d.views:
                L.append("- 뷰: " + "; ".join(f"{v.name} [{v.scale}]" for v in d.views))
            if d.marks:
                L.append(f"- 노즐 마크({len(d.marks)}): {compact_marks(d.marks)}")
            if d.see_dwg_marks:
                L.append(f"- 투영 'SEE DWG.' 마크: {compact_marks(d.see_dwg_marks)}")
            if d.dims:
                L.append("- 라벨 치수: " + "; ".join(f"{dm.text} → {dm.view}" for dm in d.dims))
            if d.parts:
                L.append(f"- 부품표 {len(d.parts)} 행: " + "; ".join(
                    f"{('[' + compact_marks(p.marks) + '] ') if p.marks else ''}{p.item} {p.name} {p.material} {p.qty}{p.unit} {p.remark}".strip()
                    for p in d.parts[:8]) + (" …" if len(d.parts) > 8 else ""))
            if d.seams:
                L.append(f"- 용접선 {len(d.seams)}: {d.seams[0]} … {d.seams[-1]}" + (f" (각도 {d.seam_angles})" if d.seam_angles else ""))
            if d.tl_labels:
                L.append(f"- 전개도 T.L+ 위치 {len(d.tl_labels)} 개: " + ", ".join(f"{k} {v:g}" for k, v in list(d.tl_labels.items())[:10]) + " …")
            if d.markers:
                L.append(f"- 단면/뷰 표식: {', '.join(d.markers)}" + (f" (이 시트에 뷰 없음: {', '.join(d.unresolved_markers)})" if d.unresolved_markers else ""))
            if d.facts:
                L.append("- 사실: " + "; ".join(f"{FACT_KO.get(f.key, f.key)}={_fmt(f.value)}{f.unit}" for f in d.facts))
            if d.notes:
                L.append(f"- 노트 {len(d.notes)} 개: " + " / ".join(n[:60] for n in d.notes[:3]) + (" …" if len(d.notes) > 3 else ""))
            if d.ai:
                L.append(f"- AI 읽기: {json.dumps(d.ai, ensure_ascii=False)[:600]}")
            L.append("")
        L.append("## 3. 도면 사이의 연관성\n")
        for kind in ("sheet_series", "see_dwg", "nozzle_mark", "weld_seam", "section_view", "title_ref", "fact_match", "fact_conflict"):
            es = [e for e in self.edges if e.kind == kind]
            if not es:
                continue
            L.append(f"### {EDGE_KO[kind]} ({len(es)})\n")
            for e in es:
                L.append(f"- {e.src} → {e.dst}: {e.detail}")
            L.append("")
        L.append("## 4. 값 일관성 (같은 항목이 여러 도면에 있는 경우)\n")
        keys = [k for k, v in self.fact_matrix.items() if len(v) >= 2 and k not in NON_COMPARABLE]
        L.append("| 항목 | 도면별 값 | 판정 |")
        L.append("|---|---|---|")
        conf_keys = {c["key"] for c in self.conflicts}
        for k in keys:
            vals = self.fact_matrix[k]
            L.append(f"| {FACT_KO.get(k, k)} | " + "; ".join(f"{s}: {_fmt(v)}" for s, v in vals.items()) +
                     f" | {'**불일치**' if k in conf_keys else '일치'} |")
        L.append("")
        if self.resolutions:
            L.append("## 5. 누락 값 보완 / 교차 검증 (GA 값 ↔ 상세도·전개도)\n")
            L.append("| 마크 | 항목 | GA 값 | 상세도 값 | 출처 | 근거 | 판정 |")
            L.append("|---|---|---|---|---|---|---|")
            for r in self.resolutions:
                L.append(f"| {r.mark} | {r.field} | {r.old} | {r.new} | {r.source} | {r.evidence} | {r.status_ko} |")
            L.append("")
            cnt = {}
            for r in self.resolutions:
                cnt[r.status_ko] = cnt.get(r.status_ko, 0) + 1
            L.append("판정 집계: " + ", ".join(f"{k} {v}" for k, v in cnt.items()) + "\n")
        if self.ai_results:
            L.append("## 6. AI(Claude headless) 실행 결과\n")
            for r in self.ai_results:
                L.append(f"- {r.get('summary', '')}")
                if r.get("data"):
                    L.append("  ```json\n  " + json.dumps(r["data"], ensure_ascii=False)[:1500] + "\n  ```")
            L.append("")
        if self.log:
            L.append("## 로그\n")
            L += [f"- {m}" for m in self.log]
        return "\n".join(L)

    def save_report(self, path: str, spec: Optional[VesselSpec] = None) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.report_markdown(spec))
