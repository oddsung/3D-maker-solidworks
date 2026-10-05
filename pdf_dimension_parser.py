"""
pdf_dimension_parser.py - 도면 PDF 에서 용기 주요 치수를 추출한다.

pdfplumber 로 단어(word)와 좌표, 선/곡선 벡터를 읽은 뒤, 규칙(정규식 + 위치 휴리스틱)으로
VesselSpec 을 채운다. 도면 양식이 바뀌면 이 파일의 규칙만 수정하면 된다.

지원 도면 (내용으로 자동 판별, 한 PDF 에 여러 종류가 있으면 모두 적용):
  - 공통            : 도면번호, O.D, I.D / 쉘 두께, 경판 형식/두께/반경, T.L~T.L, 새들 간격/오프셋, 보온
  - 리프팅 배치도    : C.O.G / 러그 위치, 리프팅 중량, 새들 단면(VIEW A-A) 폭/높이
  - GA NOZZLE LIST  : 노즐 마크/수량/사이즈/플랜지/서비스/투영/목 두께/목 외경
  - GA 입면도        : 새들 바닥 폭(B.C)/중심선 높이, C.O.G, 노즐 위치(T.L+), 단면도의 노즐 방향/투영/오프셋

주의:
- 도면에서 90도 회전된 치수 문자는 pdfplumber 가 글자 순서를 뒤집어 반환한다.
  (예: "O.D 3748" -> "8473 D.O")  upright=False 인 단어는 문자열을 반전시켜 보정한다.
- 노즐 위치: 풍선(원) 에서 나가는 지시선(선분 + 둥근 꺾임 곡선)을 벡터로 추적해 끝점 x 를
  T.L+ 라벨과 맞춘다. 서로 맞닿은 풍선들은 한 지시선을 공유하므로 같은 위치로 본다.
  지시선을 못 찾은 풍선은 인접 라벨로 추정하고 notes 에 '추정' 을 남긴다.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import pdfplumber

from vessel_spec import VesselSpec, HEAD_HEMI, HEAD_ELLIP

NUM = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"   # 1,234 / 1234 / 1234.5
MARK = r"N\d+[A-Z]?"                            # 노즐 마크 (N4, N4A ...)
MARK_GROUP = r"N\d+[A-Z]?(?:\s*[~/]\s*N?\d+[A-Z]?)?"   # N4~N4E, N1/N1A, N31~31B


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    upright: bool

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.top + self.bottom) / 2


Pt = Tuple[float, float]
Circle = Tuple[float, float, float]     # cx, cy, r


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def _is_number(s: str) -> bool:
    return re.fullmatch(NUM, s.strip()) is not None


def _dist(a: Pt, b: Pt) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def expand_marks(text: str) -> List[str]:
    """'N4~N4E' -> [N4, N4A..N4E], 'N1/N1A' -> [N1, N1A], 'N31~31B' -> [N31, N31A, N31B]"""
    text = text.strip()
    m = re.fullmatch(r"N(\d+)([A-Z]?)\s*~\s*N?(\d+)?([A-Z])", text)
    if m:
        num, first, last = m.group(1), m.group(2) or "", m.group(4)
        start = 0 if first == "" else ord(first) - ord("A") + 1
        return [f"N{num}" + ("" if i == 0 else chr(ord("A") + i - 1))
                for i in range(start, ord(last) - ord("A") + 2)]
    if "/" in text:
        return [p.strip() if p.strip().startswith("N") else "N" + p.strip() for p in text.split("/")]
    return [text]


def parse_size_inch(text: str) -> float:
    """'1 1/2"' -> 1.5, '26"' -> 26"""
    t = text.replace('"', "").strip()
    m = re.fullmatch(r"(\d+)(?:\s+(\d+)/(\d+))?", t)
    if not m:
        m2 = re.fullmatch(r"(\d+)/(\d+)", t)
        return int(m2.group(1)) / int(m2.group(2)) if m2 else 0.0
    v = float(m.group(1))
    if m.group(2):
        v += int(m.group(2)) / int(m.group(3))
    return v


def _cluster(vals: List[float], gap: float = 40.0) -> List[List[float]]:
    vals = sorted(vals)
    groups: List[List[float]] = []
    for v in vals:
        if groups and v - groups[-1][-1] < gap:
            groups[-1].append(v)
        else:
            groups.append([v])
    return groups


# ----------------------------------------------------------------------
def load_page(pdf_path: str, page_no: int = 0):
    """도면 한 장의 단어 목록(회전 보정), 선분(작은 곡선 포함), 원 후보, 페이지 크기를 반환.
    DXF 는 dxf_reader 가 같은 형태(같은 좌표 규모)로 바꿔 주므로 아래 규칙들이 그대로 돌아간다"""
    if pdf_path.lower().endswith(".dxf"):
        from dxf_reader import load_page_dxf
        return load_page_dxf(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_no]
        raw = page.extract_words(keep_blank_chars=True, extra_attrs=["upright"])
        words = []
        for w in raw:
            text = w["text"].strip()
            if not text:
                continue
            upright = bool(w.get("upright", True))
            if not upright:
                text = text[::-1]          # 회전 문자 보정
            words.append(Word(text, w["x0"], w["x1"], w["top"], w["bottom"], upright))
        segs: List[Tuple[Pt, Pt]] = []
        centerlines: List[Tuple[Pt, Pt]] = []     # 일점쇄선 (긴 dash + 점): 뷰 중심선
        for ln in page.lines:
            pts = ln.get("pts") or []
            if len(pts) < 2:
                continue
            a, b = (pts[0][0], pts[0][1]), (pts[-1][0], pts[-1][1])
            dash = ln.get("dash")
            if dash and dash[0] and max(dash[0]) > 20 and (abs(a[0] - b[0]) < 0.5 or abs(a[1] - b[1]) < 0.5):
                centerlines.append((a, b))
            else:
                segs.append((a, b))
        circles: List[Tuple[float, float, float, float]] = []
        big: set = set()                          # 큰 곡선(스플라인 지시선): 풍선에서 나가는 첫 구간에만 허용
        for c in page.curves:
            pts = c.get("pts") or []
            w, h = c["x1"] - c["x0"], c["bottom"] - c["top"]
            if len(pts) < 2:
                continue
            if _dist(pts[0], pts[-1]) > 0.5 and w < 80 and h < 80:
                if w >= 15 or h >= 15:
                    big.add(len(segs))
                segs.append(((pts[0][0], pts[0][1]), (pts[-1][0], pts[-1][1])))   # 지시선의 꺾임/스플라인
            elif 12 < w < 24 and abs(w - h) <= 4:
                circles.append((c["x0"], c["top"], c["x1"], c["bottom"]))          # 풍선 원 후보
        return words, segs, big, circles, centerlines, page.width, page.height


# ----------------------------------------------------------------------
class DrawingParser:
    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path
        (self.words, self.segs, self.seg_big, self.circle_boxes, self.centerlines,
         self.page_w, self.page_h) = load_page(pdf_path)
        self._elev_ids: set = set()               # 입면도에서 위치를 확정한 풍선 단어 id
        self._b = 0.0                             # 입면도 축척 (pt/mm)
        self._elev_cl_y: Optional[float] = None   # 입면도 용기 중심선 y
        self._elev_scale: Optional[int] = None    # 입면도 축척 분모 (1:50 -> 50)
        self.full_text = " ".join(w.text for w in self.words)
        self.log: List[str] = []
        self.kinds: List[str] = []
        if re.search(r"LIFTING WEIGHT", self.full_text, re.I):
            self.kinds.append("lifting")
        if re.search(r"NOZZLE LIST", self.full_text, re.I) and self._find(r"^MARK$", upright=True):
            self.kinds.append("nozzle_list")
        if len(self._tl_labels()) >= 3 or re.search(r"\(T\.L\s*TO\s*T\.L\)", self.full_text, re.I):
            self.kinds.append("elevation")
        self._head_nozzle_proj: Optional[float] = None
        self._angle_from_row: Dict[str, str] = {}
        self._elev_y_max = 0.0

    # ---- 공통 헬퍼 ------------------------------------------------------
    @staticmethod
    def _same_row(a: Word, b: Word, tol: float = 4.0) -> bool:
        return abs(a.top - b.top) <= tol

    def _row_numbers(self, ref: Word, tol: float = 4.0) -> List[Word]:
        return sorted([w for w in self.words if self._same_row(w, ref, tol) and _is_number(w.text)],
                      key=lambda w: w.x0)

    def _find(self, pattern: str, upright: Optional[bool] = None) -> Optional[Word]:
        for w in self.words:
            if (upright is None or w.upright == upright) and re.search(pattern, w.text, re.I):
                return w
        return None

    def _find_all(self, pattern: str, upright: Optional[bool] = None) -> List[Word]:
        return [w for w in self.words
                if (upright is None or w.upright == upright) and re.search(pattern, w.text, re.I)]

    @staticmethod
    def _nearest(ref: Word, cands: Sequence[Word], max_d: float) -> Optional[Word]:
        best, best_d = None, max_d
        for w in cands:
            if w is ref:
                continue
            d = _dist((ref.xc, ref.yc), (w.xc, w.yc))
            if d < best_d:
                best, best_d = w, d
        return best

    def _preceding_number(self, ref: Word, pattern: str, max_d: float = 25.0) -> Optional[Word]:
        """ref 바로 앞 줄의 숫자: 회전 문자는 왼쪽(x 작은 쪽), 정립 문자는 위쪽(y 작은 쪽)"""
        best, best_d = None, max_d
        for w in self.words:
            if w is ref or w.upright != ref.upright or not re.fullmatch(pattern, w.text):
                continue
            if ref.upright:
                ok = w.yc < ref.yc and abs(w.xc - ref.xc) < 40
                d = ref.top - w.bottom + abs(w.xc - ref.xc) * 0.2
            else:
                ok = w.xc < ref.xc and abs(w.yc - ref.yc) < 40
                d = ref.x0 - w.x1 + abs(w.yc - ref.yc) * 0.2
            if ok and -2 < d < best_d:
                best, best_d = w, d
        return best

    def _tl_labels(self) -> List[Tuple[float, Word]]:
        out = []
        for w in self.words:
            m = re.fullmatch(r"T\.L\s*\+\s*(\d+)", w.text.strip())
            if m:
                out.append((float(m.group(1)), w))
        return out

    # ---- 공통 규칙 ------------------------------------------------------
    def _find_drawing_no(self, spec: VesselSpec) -> None:
        m = re.search(r"\b(P-[A-Z0-9]+-\d{3}-[A-Z]{2}-\d{3}-\d{4})\b", self.full_text)
        if m:
            spec.drawing_no = m.group(1)
        m = re.search(r"((?:LIFTING|GENERAL) ARRANGEMENT DRAWINGS?(?:\s*\(\d/\d\))?)", self.full_text)
        if m:
            spec.title = m.group(1)

    def _find_od(self, spec: VesselSpec) -> None:
        for w in self.words:
            m = re.search(r"\bO\.?D\.?\s*" + NUM, w.text)
            if m:
                spec.outer_diameter = _to_float(m.group(1))
                self.log.append(f"O.D = {spec.outer_diameter} ('{w.text}')")
                return

    def _find_shell_thickness(self, spec: VesselSpec) -> None:
        for w in self.words:
            m = re.search(r"\bI\.?D\.?\s*" + NUM, w.text)
            if not m:
                continue
            spec.inner_diameter = _to_float(m.group(1))
            m2 = re.search(r"\bt(\d+(?:\.\d+)?)\b", w.text)
            if m2:
                spec.shell_thickness = _to_float(m2.group(1))
            else:
                cands = [c for c in self.words if c.upright == w.upright and re.fullmatch(r"t\d+(?:\.\d+)?", c.text)]
                t = self._nearest(w, cands, 120)
                if t:
                    spec.shell_thickness = _to_float(t.text[1:])
            self.log.append(f"I.D = {spec.inner_diameter} ('{w.text}'), 쉘 두께 t = {spec.shell_thickness}")
            return

    def _find_heads(self, spec: VesselSpec) -> None:
        if not re.search(r"HEAD", self.full_text):
            return
        found = False
        if re.search(r"HEMI", self.full_text, re.I):
            spec.head_type = HEAD_HEMI
            found = True
        elif re.search(r"2\s*:\s*1|ELLIP", self.full_text, re.I):
            spec.head_type = HEAD_ELLIP
            found = True
        m = re.search(r"USED\s+TH'?K\.?\s*:?\s*" + NUM, self.full_text, re.I)
        if m:
            spec.head_thickness = _to_float(m.group(1))
            found = True
        base_r = spec.inner_diameter / 2 if spec.inner_diameter else 0
        for w in self.words:
            m = re.fullmatch(r"R(\d{3,5})", w.text.strip())
            if m and (base_r == 0 or abs(float(m.group(1)) - base_r) < 100):
                spec.head_inner_radius = float(m.group(1))
                found = True
                break
        if found:
            self.log.append(f"경판: {spec.head_type}, 두께 {spec.head_thickness}, 내측 R {spec.head_inner_radius}")

    def _find_tl_length(self, spec: VesselSpec) -> None:
        m = re.search(NUM + r"\s*\(T\.L\s*TO\s*T\.L\)", self.full_text, re.I)
        if m:
            spec.tl_length = _to_float(m.group(1))
            self.log.append(f"T.L ~ T.L = {spec.tl_length}")

    def _find_saddle(self, spec: VesselSpec) -> None:
        """'20727 (SADDLE TO SADDLE C.L)' 행: 바로 왼쪽 숫자 = T.L 오프셋,
        그 바깥 숫자(오프셋보다 큰 값) = T.L 에서 경판 노즐 플랜지 면까지 거리"""
        for w in self.words:
            m = re.search(NUM + r"\s*\(SADDLE TO SADDLE", w.text, re.I)
            if not m:
                continue
            spec.saddle_spacing = _to_float(m.group(1))
            left = [n for n in self._row_numbers(w) if n.x1 <= w.x0 + 1]
            if left:
                spec.saddle_offset = _to_float(left[-1].text)
                if len(left) >= 2 and _to_float(left[-2].text) > spec.saddle_offset:
                    self._head_nozzle_proj = _to_float(left[-2].text)
            if spec.tl_length <= 0:   # 리프팅 도면: 바로 아래 행의 가장 큰 숫자 = T.L ~ T.L
                below = [n for n in self.words if 4 < n.top - w.top < 20 and _is_number(n.text)]
                if below:
                    spec.tl_length = max(_to_float(n.text) for n in below)
            self.log.append(f"SADDLE spacing={spec.saddle_spacing}, offset={spec.saddle_offset}, "
                            f"T.L={spec.tl_length}, 경판노즐 플랜지면={self._head_nozzle_proj}")
            return

    def _find_misc(self, spec: VesselSpec) -> None:
        m = re.search(NUM + r"\s*\+\s*" + NUM + r"\s*=\s*" + NUM + r"\s*\(APPROX", self.full_text, re.I)
        if m:
            spec.overall_length = _to_float(m.group(3))
        m = re.search(r"LIFTING WEIGHT\s*:\s*" + NUM + r"\s*kg", self.full_text, re.I)
        if m:
            spec.lifting_weight_kg = _to_float(m.group(1))
        m = re.search(r"INSULATION TH'?K\.?\s*:?\s*" + NUM + r"\s*mm", self.full_text, re.I)
        if m:
            spec.insulation_thickness = _to_float(m.group(1))

    # ---- 리프팅 배치도 규칙 ---------------------------------------------
    def _find_lugs(self, spec: VesselSpec) -> None:
        """정면도 영역: 한 행에 소수점 숫자 2개 (C.O.G 기준 좌/우 러그 거리),
        그 바로 위 행에 소수점 숫자 1개 (좌측 T.L ~ C.O.G)"""
        decimals = [w for w in self.words if w.upright and re.fullmatch(r"\d+\.\d+", w.text)]
        for w in decimals:
            row = sorted([n for n in decimals if self._same_row(n, w)], key=lambda n: n.x0)
            if len(row) != 2:
                continue
            above = [n for n in decimals if 4 < w.top - n.top < 20]
            if len(above) != 1:
                continue
            spec.cog_from_tl = _to_float(above[0].text)
            spec.lug_offsets_from_cog = [-_to_float(row[0].text), _to_float(row[1].text)]
            self.log.append(f"LUG cog={spec.cog_from_tl}, offsets={spec.lug_offsets_from_cog}")
            return

    def _find_saddle_section_lifting(self, spec: VesselSpec) -> None:
        """좌측 단면도(VIEW A-A, x < 30% 폭): 정수 2개가 한 행 -> 새들 바닥 폭 (O.D 보다 크면 무시),
        회전 치수 중 반지름보다 큰 최소값 -> 새들 바닥 ~ 중심선 높이"""
        left = [w for w in self.words if w.xc < self.page_w * 0.30]
        ints = [w for w in left if w.upright and re.fullmatch(r"\d{4}", w.text)]
        for w in ints:
            row = sorted([n for n in ints if self._same_row(n, w)], key=lambda n: n.x0)
            if len(row) == 2 and abs(_to_float(row[0].text) - _to_float(row[1].text)) < 200:
                width = _to_float(row[0].text) + _to_float(row[1].text)
                if spec.outer_diameter and width > spec.outer_diameter + 200:
                    self.log.append(f"SADDLE base width 후보 {width} 는 O.D 보다 커서 무시 (노즐 돌출 폭으로 추정)")
                else:
                    spec.saddle_base_width = width
                    self.log.append(f"SADDLE base width = {width} ({row[0].text}+{row[1].text})")
                break
        vert = [w for w in left if not w.upright and _is_number(w.text)]
        if vert:
            vals = [_to_float(w.text) for w in vert]
            cand = [v for v in vals if spec.outer_radius < v < spec.outer_diameter] if spec.outer_diameter else vals
            if cand:
                spec.centerline_height = min(cand)
                self.log.append(f"centerline height = {spec.centerline_height} (후보 {sorted(vals)})")

    # ---- GA 노즐 리스트 규칙 --------------------------------------------
    def _find_nozzle_list(self, spec: VesselSpec) -> None:
        title = self._find(r"^NOZZLE LIST$", upright=True)
        mark_hdr = self._find(r"^MARK$", upright=True)
        if not title or not mark_hdr:
            return
        y0 = title.top
        loads = self._find(r"^NOZZLE LOADS$", upright=True)
        y1 = loads.top if loads and loads.top > y0 else self.page_h
        count = 0
        for w in self.words:
            if not w.upright or not (y0 < w.top < y1) or abs(w.xc - mark_hdr.xc) > 25:
                continue
            if not re.fullmatch(MARK_GROUP, w.text.strip()):
                continue
            row = sorted([r for r in self.words if r.upright and abs(r.top - w.top) <= 4.5 and r.x0 > w.x0 + 1],
                         key=lambda r: r.x0)
            tokens = [r.text.strip() for r in row]
            joined = " ".join(tokens)
            marks = expand_marks(w.text)
            qty = next((int(t) for t in tokens if re.fullmatch(r"\d{1,2}", t)), len(marks))
            size = next((t for t in tokens if re.fullmatch(r'\d+(?:\s\d/\d)?"', t)), "")
            rating = next((t for t in tokens if re.search(r"CL\.?\s*\d{3}|#\d{3}", t)), "")
            ftype = next((t for t in tokens if re.fullmatch(r"L?WN\.RF|FN\.RF|SO\.RF|SW|THD", t)), "")
            m_neck = re.search(r"\bt(\d+(?:\.\d+)?)\s+Ø(\d+(?:\.\d+)?)", joined)
            thk = _to_float(m_neck.group(1)) if m_neck else 0.0
            od = _to_float(m_neck.group(2)) if m_neck else 0.0
            m_proj = re.search(r"\b(\d{3,4})\s+t\d+(?:\.\d+)?\s+Ø", joined)
            proj = _to_float(m_proj.group(1)) if m_proj else None
            # 서비스: 플랜지 형식/SCH 열 오른쪽 ~ 투영/목두께 열 왼쪽 사이의 문자 (두 줄이면 줄 순서로)
            x_after = max([r.x1 for r in row if re.fullmatch(r"L?WN\.RF|FN\.RF|SO\.RF|SW|THD|\d{2,3}|-|t\d+", r.text.strip())
                           and r.x0 < w.x0 + 200] or [w.x1])
            x_before = min([r.x0 for r in row if re.fullmatch(r"\d{3,4}|SEE DWG\.?|t\d+(?:\.\d+)?|Ø\d+", r.text.strip())
                            and r.x0 > x_after] or [self.page_w])
            srv_words = sorted([r for r in row if r.x0 >= x_after - 1 and r.x1 <= x_before + 1
                                and re.search(r"[A-Z]", r.text)], key=lambda r: (round(r.top / 3), r.x0))
            service = " ".join(r.text.strip() for r in srv_words)
            if len(marks) != qty:
                self.log.append(f"노즐 {w.text}: 수량 {qty} 와 마크 전개 {len(marks)} 불일치")
            for mk in marks:
                n = spec.nozzle(mk)
                n.size_text, n.size_in = size, (parse_size_inch(size) if size else 0.0)
                n.rating, n.flange_type, n.service = rating, ftype, service
                n.neck_od, n.neck_thk = od, thk
                if proj is not None:
                    n.projection = proj
                count += 1
        self.log.append(f"NOZZLE LIST: {count} 개 노즐 (마크 전개 후)")

    # ---- GA 입면도 규칙 -------------------------------------------------
    def _find_saddle_section_ga(self, spec: VesselSpec) -> None:
        m = re.search(NUM + r"\s*\(FOR\s+(?:FIXED|SLIDING)\s+SIDE\)", self.full_text, re.I)
        if m:
            spec.centerline_height = _to_float(m.group(1))
            self.log.append(f"새들 중심선 높이 = {spec.centerline_height} ('{m.group(0)}')")
        bcs = self._find_all(r"^B\.C\s*\d+", upright=True)
        if bcs:
            bc_vals = [_to_float(re.search(NUM, b.text).group(1)) for b in bcs]
            ref = bcs[0]
            cands = [w for w in self.words if w.upright and re.fullmatch(r"\d{4}", w.text)
                     and abs(w.xc - ref.xc) < 40 and abs(w.yc - ref.yc) < 40 and _to_float(w.text) > max(bc_vals)]
            if cands:
                spec.saddle_base_width = _to_float(cands[0].text)
                self.log.append(f"새들 바닥 폭 = {spec.saddle_base_width} (B.C {bc_vals} 근처)")

    def _find_cog_ga(self, spec: VesselSpec) -> None:
        w = self._find(r"LIFTING CONDITION", upright=True)
        if not w:
            return
        below = [n for n in self.words if n.upright and re.fullmatch(r"\d+\.\d+", n.text)
                 and 0 < n.top - w.top < 20 and abs(n.xc - w.xc) < 40]
        if below:
            spec.cog_from_tl = _to_float(below[0].text)
            self.log.append(f"C.O.G (LIFTING CONDITION) = {spec.cog_from_tl}")

    # ---- 입면도 노즐 위치 -----------------------------------------------
    def _circle_for(self, w: Word) -> Circle:
        best = None
        for x0, top, x1, bottom in self.circle_boxes:
            cx, cy, r = (x0 + x1) / 2, (top + bottom) / 2, (x1 - x0) / 2
            d = _dist((cx, cy), (w.xc, w.yc))
            if d < r and (best is None or d < best[0]):
                best = (d, cx, cy, r)
        return best[1:] if best else (w.xc, w.yc + 3.0, 8.2)

    def _on_circle(self, pt: Pt, circles: List[Circle]) -> bool:
        return any(abs(_dist(pt, (cx, cy)) - r) < 3 for cx, cy, r in circles)

    def _follow(self, pt: Pt, used: set, circles: List[Circle], prev: Optional[Pt] = None,
                gap: float = 0.0) -> Pt:
        """pt 에서 이어지는 선분을 따라간다 (분기 시 가장 긴 것). 다른 풍선 원에 닿으면 중단.
        큰 곡선은 중간 구간으로 쓰지 않는다.
        gap > 0 이면 그 뒤에 이어서, 진행 방향으로 작은 간격(문자에 가려 끊긴 곳) 너머에서
        같은 방향으로 계속되는 선분만 따라간다 (방향 전환 없음). 단면도 지시선 전용."""
        for _ in range(10):
            cands = []
            for i, (a, b) in enumerate(self.segs):
                if i in used or i in self.seg_big:
                    continue
                if _dist(a, pt) < 1.0:
                    cands.append((i, b, _dist(a, b)))
                elif _dist(b, pt) < 1.0:
                    cands.append((i, a, _dist(a, b)))
            if not cands:
                break
            i, nxt, _ = max(cands, key=lambda c: c[2])
            used.add(i)
            if self._on_circle(nxt, circles):
                return nxt
            prev, pt = pt, nxt
        if gap <= 0 or prev is None:
            return pt
        for _ in range(10):
            d = (pt[0] - prev[0], pt[1] - prev[1])
            n = math.hypot(*d)
            if n < 0.5:
                break
            ux, uy = d[0] / n, d[1] / n
            cands = []
            for i, (a, b) in enumerate(self.segs):
                if i in used or i in self.seg_big:
                    continue
                for s, e in ((a, b), (b, a)):
                    fwd = (s[0] - pt[0]) * ux + (s[1] - pt[1]) * uy          # 진행 방향 거리
                    lat = abs((s[0] - pt[0]) * uy - (s[1] - pt[1]) * ux)     # 옆으로 벗어난 거리
                    seg_dir = ((e[0] - s[0]) * ux + (e[1] - s[1]) * uy) / max(_dist(s, e), 1e-6)
                    if -0.5 < fwd < gap and lat < 1.5 and seg_dir > 0.98:
                        cands.append((i, e, fwd))
            if not cands:
                break
            i, nxt, _ = min(cands, key=lambda c: c[2])     # 가장 가까운 이어짐부터
            used.add(i)
            if self._on_circle(nxt, circles):
                break
            prev, pt = pt, nxt
        return pt

    def _leader_ends(self, circle: Circle, circles: List[Circle], gap: float = 0.0) -> List[Pt]:
        cx, cy, r = circle
        ends = []
        for i, (a, b) in enumerate(self.segs):
            for start, other in ((a, b), (b, a)):
                if abs(_dist(start, (cx, cy)) - r) < 3 and _dist(other, (cx, cy)) > r + 3 \
                        and not self._on_circle(other, circles):
                    ends.append(self._follow(other, {i}, circles, prev=start, gap=gap))
        ends.sort(key=lambda p: -_dist(p, (cx, cy)))   # 멀리 간 끝점 우선
        return ends

    def _find_nozzle_positions(self, spec: VesselSpec) -> None:
        labels = self._tl_labels()
        if len(labels) < 3:
            return

        def fit(items):
            n = len(items)
            sx = sum(w.xc for _, w in items); sv = sum(v for v, _ in items)
            sxv = sum(w.xc * v for v, w in items); svv = sum(v * v for v, _ in items)
            b = (n * sxv - sx * sv) / (n * svv - sv * sv)
            return (sx - b * sv) / n, b

        a, b = fit(labels)
        good = [(v, w) for v, w in labels if abs(w.xc - (a + b * v)) < 8]
        if len(good) >= 3:
            a, b = fit(good)
        resid = max(abs(w.xc - (a + b * v)) for v, w in good)
        L = spec.tl_length if spec.tl_length > 0 else max(v for v, _ in labels)
        x_left, x_right = a, a + b * L
        self.log.append(f"입면도 축척: {b*1000:.3f} pt/m, T.L(0) x={x_left:.1f}, T.L({L:g}) x={x_right:.1f}, "
                        f"라벨 {len(labels)} 개, 피팅 잔차 {resid:.1f} pt")

        lab_rows = _cluster([w.yc for _, w in labels])
        self._b = b
        # 입면도 용기 중심선: T.L 구간과 겹치는 긴 가로 일점쇄선 중 라벨 행 사이에 있는 것
        y_lo_lab, y_hi_lab = min(w.yc for _, w in labels), max(w.yc for _, w in labels)
        for p1, p2 in self.centerlines:
            if abs(p1[1] - p2[1]) < 0.5 and abs(p1[0] - p2[0]) > 0.5 * b * L \
                    and min(p1[0], p2[0]) < x_left + 20 and y_lo_lab < p1[1] < y_hi_lab:
                self._elev_cl_y = p1[1]
                break
        title = self._find(r"^ELEVATION VIEW", upright=True)
        if title:
            sc = self._nearest(title, [w for w in self.words if re.search(r"SCALE\s*=\s*1\s*:\s*\d+", w.text)], 60)
            if sc:
                self._elev_scale = int(re.search(r"1\s*:\s*(\d+)", sc.text).group(1))
        y_end_limit = max(w.yc for _, w in labels) + 40      # 지시선 끝점이 이보다 아래면 다른 뷰
        y_bal_limit = max(w.yc for _, w in labels) + 60
        x_lo, x_hi = x_left - b * 3500, x_right + b * 3500

        def row_of(y: float) -> str:
            if len(lab_rows) == 1:
                return "any"
            idx = min(range(len(lab_rows)), key=lambda i: abs(sum(lab_rows[i]) / len(lab_rows[i]) - y))
            return "top" if idx == 0 else ("bottom" if idx == len(lab_rows) - 1 else "mid")

        def match_label(pt: Pt, max_dx: float = 20.0) -> Optional[Tuple[float, float]]:
            row = row_of(pt[1])
            best = None
            for v, w in labels:
                if row != "any" and row_of(w.yc) != row:
                    continue
                dx = abs(w.xc - pt[0])
                if dx < max_dx and (best is None or dx < best[1]):
                    best = (v, dx)
            return best

        # 풍선: 마크 단어 + 원. 서로 맞닿은 원은 같은 그룹 (한 지시선 공유)
        balloons = [w for w in self.words if w.upright and re.fullmatch(MARK, w.text.strip())
                    and x_lo - 40 < w.xc < x_hi + 40]     # 경판 노즐 풍선은 지시선 끝보다 더 바깥에 있음
        circles = [self._circle_for(w) for w in balloons]
        parent = list(range(len(balloons)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(balloons)):
            for j in range(i + 1, len(balloons)):
                ci, cj = circles[i], circles[j]
                if abs(_dist(ci[:2], cj[:2]) - (ci[2] + cj[2])) < 0.5:   # 정확히 접한 원만 (인접 풍선은 2~3pt 간격)
                    parent[find(i)] = find(j)
        groups: Dict[int, List[int]] = {}
        for i in range(len(balloons)):
            groups.setdefault(find(i), []).append(i)

        resolved: Dict[int, Tuple[float, str, str]] = {}   # 그룹 root -> (T.L+, 근거, 행)
        head: Dict[int, str] = {}
        used_labels: Dict[float, int] = {}
        pending: List[int] = []
        for root, members in groups.items():
            ends = []
            for i in members:
                ends += self._leader_ends(circles[i], circles)
            inside = [e for e in ends if x_lo < e[0] < x_hi and e[1] < y_end_limit]
            if ends and not inside:
                continue                                   # 다른 뷰(단면도 등)의 풍선
            if not ends and min(circles[i][1] for i in members) > y_bal_limit:
                continue
            matched = None
            for e in inside:
                if e[0] < x_left - 3:
                    head[root] = "left"
                    break
                if e[0] > x_right + 3:
                    head[root] = "right"
                    break
                m = match_label(e)
                if m and (matched is None or m[1] < matched[1]):
                    matched = (m[0], m[1], e)
            if root in head:
                continue
            if matched:
                resolved[root] = (matched[0], "지시선", row_of(matched[2][1]))
                used_labels[matched[0]] = root
            else:
                pending.append(root)

        # 지시선을 못 찾은 그룹: 같은 행의 미사용 라벨 중 가까운 것, 없으면 사용된 라벨 포함
        def best_in_row(root: int, unused_only: bool, max_dx: float):
            members = groups[root]
            gx = sum(circles[i][0] for i in members) / len(members)
            gy = min(circles[i][1] for i in members)
            row = row_of(gy)
            best = None
            for v, w in labels:
                if row != "any" and row_of(w.yc) != row:
                    continue
                if unused_only and v in used_labels:
                    continue
                dx = abs(w.xc - gx)
                if dx < max_dx and (best is None or dx < best[1]):
                    best = (v, dx, row)
            return best

        for root in sorted(pending, key=lambda r: (best_in_row(r, True, 12.0) or (0, 999, ""))[1]):
            m = best_in_row(root, True, 12.0)
            how = "인접 라벨(추정)"
            if not m:
                m = best_in_row(root, False, 30.0)
                how = "인접 라벨, 다른 노즐과 공유(추정)"
            if m:
                resolved[root] = (m[0], how, m[2])
                used_labels.setdefault(m[0], root)
            else:
                names = ",".join(balloons[i].text for i in groups[root])
                self.log.append(f"노즐 {names}: 입면도 위치를 찾지 못함")

        # 결과 반영
        n_leader = n_guess = 0
        for root, members in groups.items():
            for i in members:
                mark = balloons[i].text.strip()
                if root in head:
                    self._elev_ids.add(id(balloons[i]))
                    nz = spec.nozzle(mark)
                    nz.on_head = head[root]
                    if self._head_nozzle_proj:
                        nz.projection = self._head_nozzle_proj
                    zone = (lambda x: x < x_left) if head[root] == "left" else (lambda x: x > x_right)
                    offs = [o for o in self.words if not o.upright and re.fullmatch(r"\d{3,4}", o.text)
                            and zone(o.xc) and x_lo < o.xc < x_hi and abs(o.yc - circles[i][1]) < 40]
                    if offs:
                        # 치수 문자는 치수선 중앙에 놓이므로 용기 중심선보다 아래면 아래쪽 오프셋
                        below = self._elev_cl_y is not None and offs[0].yc > self._elev_cl_y
                        nz.offset_y = -_to_float(offs[0].text) if below else _to_float(offs[0].text)
                        nz.notes.append(f"경판 노즐 축 오프셋 {nz.offset_y:g} "
                                        f"({'입면도 중심선 기준' if self._elev_cl_y is not None else '위쪽으로 가정'})")
                    nz.notes.append(f"경판({head[root]}) 노즐")
                    continue
                if root not in resolved:
                    continue
                self._elev_ids.add(id(balloons[i]))
                v, how, row = resolved[root]
                nz = spec.nozzle(mark)
                nz.position_from_tl = v
                if row == "top":
                    nz.angle_deg = 0.0
                elif row == "bottom":
                    nz.angle_deg = 180.0
                elif row == "mid":
                    nz.angle_deg = 90.0
                if row in ("top", "bottom"):
                    self._angle_from_row[mark] = row
                nz.notes.append(f"위치 {how}" + (f", {balloons[members[0]].text} 와 풍선 공유" if len(members) > 1 and i != members[0] else ""))
                if how == "지시선":
                    n_leader += 1
                else:
                    n_guess += 1
        self.log.append(f"입면도 노즐 위치: 지시선 {n_leader} 개, 추정 {n_guess} 개, 경판 노즐 {sum(len(groups[r]) for r in head)} 개")
        self._elev_y_max = y_bal_limit

    # ---- 단면도 기하 ----------------------------------------------------
    def _text_groups(self):
        """'2370 (N9,N13,...)' 그룹 라벨: 회전 문자 = 수직 노즐(v), 정립 문자 = 수평 노즐(h),
        앞 숫자 = C.L 에서 플랜지 면까지 투영. 회전 '(N7)' 단독 라벨 + 앞 숫자 = 높이 오프셋 크기."""
        groups: Dict[str, dict] = {}
        singles: List[Tuple[Word, str, float]] = []
        for w in self.words:
            m = re.fullmatch(r"\((N[^)]*)\)", w.text.strip())
            if not m:
                continue
            inner = m.group(1)
            marks = []
            for part in re.split(r"\s*,\s*", inner):
                if re.fullmatch(MARK_GROUP, part.strip()):
                    marks += expand_marks(part.strip())
            if not marks:
                continue
            num = self._preceding_number(w, r"\d{3,4}")
            if len(marks) == 1 and not w.upright and "~" not in inner and "/" not in inner:
                if num:
                    singles.append((num, marks[0], _to_float(num.text)))
                continue
            proj = _to_float(num.text) if num and _to_float(num.text) > 500 else None
            for mk in marks:
                groups[mk] = {"orient": "v" if not w.upright else "h", "proj": proj, "word": w}
        return groups, singles

    def _find_section_views(self) -> List[dict]:
        """가로+세로 일점쇄선 중심선의 교점 = 단면도 중심. 같은 높이의 90°/270° 라벨로 좌우 방향,
        가까운 'SCALE = 1 : N' 으로 축척을 정한다."""
        vert = [(a, b) for a, b in self.centerlines if abs(a[0] - b[0]) < 0.5 and 30 < abs(a[1] - b[1]) < 250]
        horiz = [(a, b) for a, b in self.centerlines if abs(a[1] - b[1]) < 0.5 and 30 < abs(a[0] - b[0]) < 250]
        sides = [w for w in self.words if w.upright and re.fullmatch(r"(90|270)°", w.text)]
        scales = [(w, int(m.group(1))) for w in self.words
                  if (m := re.search(r"SCALE\s*=\s*1\s*:\s*(\d+)", w.text))]
        views: List[dict] = []
        for va, vb in vert:
            vx, vy0, vy1 = va[0], min(va[1], vb[1]), max(va[1], vb[1])
            for ha, hb in horiz:
                hy, hx0, hx1 = ha[1], min(ha[0], hb[0]), max(ha[0], hb[0])
                if not (vy0 - 2 <= hy <= vy1 + 2 and hx0 - 2 <= vx <= hx1 + 2):
                    continue
                if any(_dist((vx, hy), (u["cx"], u["cy"])) < 5 for u in views):
                    continue
                near = [s for s in sides if abs(s.yc - hy) < 6 and hx0 - 120 < s.xc < hx1 + 120]
                s90 = [s for s in near if s.text.startswith("90")]
                s270 = [s for s in near if s.text.startswith("270")]
                if s90:
                    sign90 = 1 if s90[0].xc > vx else -1
                elif s270:
                    sign90 = -1 if s270[0].xc > vx else 1
                else:
                    continue
                scale = None
                if scales:
                    sw, sv = min(scales, key=lambda t: _dist((t[0].xc, t[0].yc), (vx, hy)))
                    if _dist((sw.xc, sw.yc), (vx, hy)) < 320:
                        scale = sv
                views.append({"cx": vx, "cy": hy, "sign90": sign90, "scale": scale})
        return views

    def _apply_section_geometry(self, spec: VesselSpec) -> None:
        """단면도의 풍선 지시선 끝점을 뷰 중심 기준 (y: 위+, z: 90°쪽+) mm 로 환산해
        노즐 방향(0/90/180/270 또는 임의 각도)과 축 오프셋을 정한다. 치수 숫자가 있으면 값을 스냅한다."""
        groups, singles = self._text_groups()
        views = self._find_section_views()
        b = self._b
        R_o = spec.outer_radius or (spec.inner_diameter / 2 + spec.shell_thickness)
        self.log.append(f"단면도 {len(views)} 개 (중심선 교점), 텍스트 그룹 {len(groups)} 개")

        def view_b(v: dict) -> float:
            if b and self._elev_scale and v["scale"]:
                return b * self._elev_scale / v["scale"]
            return b

        nums = [w for w in self.words if re.fullmatch(r"\d{3,4}", w.text)]

        def snap(val: float, v: dict, rotated: bool = True, tol: float = 0.15) -> Optional[float]:
            """|val| 에 가장 가까운 뷰 안의 치수 숫자(세로 치수 = 회전 문자, 가로 치수 = 정립 문자)로 스냅.
            맞는 숫자가 없으면 None"""
            best = None
            for w in nums:
                if w.upright == rotated or _dist((w.xc, w.yc), (v["cx"], v["cy"])) > 260:
                    continue
                n = _to_float(w.text)
                if abs(n - abs(val)) <= tol * max(abs(val), 1) and (best is None or abs(n - abs(val)) < abs(best - abs(val))):
                    best = n
            if best is None:
                return None
            return -best if val < 0 else best

        results: Dict[str, dict] = {}
        if b and views:
            balloons = [w for w in self.words if w.upright and re.fullmatch(MARK, w.text.strip())
                        and id(w) not in self._elev_ids]
            circles = [self._circle_for(w) for w in balloons]
            parent = list(range(len(balloons)))

            def find(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i

            for i in range(len(balloons)):
                for j in range(i + 1, len(balloons)):
                    if abs(_dist(circles[i][:2], circles[j][:2]) - (circles[i][2] + circles[j][2])) < 0.5:
                        parent[find(i)] = find(j)
            grp: Dict[int, List[int]] = {}
            for i in range(len(balloons)):
                grp.setdefault(find(i), []).append(i)

            def plausible(y: float, z: float, orient: Optional[str], on_head: bool) -> bool:
                """지시선 끝점이 노즐 위치(쉘 ~ 플랜지 반경)로 그럴듯한지"""
                far = R_o + 1800
                if on_head:
                    return abs(y) < R_o and abs(z) < R_o
                if orient == "h":
                    return abs(y) < R_o * 1.2 and 0.5 * R_o < abs(z) < far
                if orient == "v":
                    return abs(z) < R_o * 1.2 and 0.5 * R_o < abs(y) < far
                return 0.6 * R_o < math.hypot(y, z) < far

            for members in grp.values():
                marks = [balloons[i].text.strip() for i in members]
                orient = next((groups[mk]["orient"] for mk in marks if mk in groups), None)
                on_head = any(spec.nozzle(mk).on_head for mk in marks)
                # 지시선 끝점: 정확 추적(gap 0) 결과를 우선, 문자에 가려 끊긴 경우를 위해 gap 추적 결과도 후보
                ends: List[Pt] = []
                for gap in (0.0, 15.0):
                    for i in members:
                        for e in self._leader_ends(circles[i], circles, gap=gap):
                            if all(_dist(e, q) > 0.5 for q in ends):
                                ends.append(e)
                # 끝점이 그럴듯한 노즐 위치가 되는 (끝점, 뷰) 후보. 뷰는 끝점에서 200pt 이내
                cands = []
                for k, e in enumerate(ends):
                    for v in sorted(views, key=lambda u: _dist(e, (u["cx"], u["cy"]))):
                        if _dist(e, (v["cx"], v["cy"])) > 200:
                            break
                        bv = view_b(v)
                        y = (v["cy"] - e[1]) / bv
                        z = v["sign90"] * (e[0] - v["cx"]) / bv
                        if plausible(y, z, orient, on_head):
                            cands.append((k, y, z, v))
                            break
                if not cands:
                    continue
                if orient is None and not on_head:
                    chosen = max(cands, key=lambda c: math.hypot(c[1], c[2]))   # 반경 방향: 플랜지 쪽(먼 끝점)
                else:
                    chosen = cands[0]                                           # 첫 번째(정확 추적) 우선
                for mk in marks:
                    results[mk] = {"y": chosen[1], "z": chosen[2], "view": chosen[3], "orient": orient}

        # 결과 반영
        n_geo = 0
        for mk, r in results.items():
            nz = spec.nozzle(mk)
            y, z, v = r["y"], r["z"], r["view"]
            nz.notes = [t for t in nz.notes if not t.startswith(("방향", "높이 오프셋", "경판 노즐 축"))]
            if nz.on_head:
                # 지시선은 맨웨이 원 가장자리(반경 ~500) 에서 끝나므로 z 는 700 이상 + 치수 숫자로 확인될 때만 인정
                sy, sz = snap(y, v), snap(z, v, rotated=False)
                nz.offset_y = sy if sy is not None else (round(y) if abs(y) > 300 else 0.0)
                nz.offset_z = sz if (sz is not None and abs(z) > 700) else 0.0
                nz.notes.append(f"경판 노즐 축 오프셋 y={nz.offset_y:g}, z={nz.offset_z:g} (단면도 기하)")
                n_geo += 1
                continue
            orient = r["orient"]
            if orient is None:
                orient = "v" if abs(z) < 500 else ("h" if abs(y) < 300 else "r")
            if orient == "v":
                nz.angle_deg = 0.0 if y > 0 else 180.0
                # 수직 노즐이 좌우로 치우친 경우(예: N4~N4E 엔트런스 부싱 400): 지시선 끝의 z 가 뷰 안의
                # 가로 치수 숫자와 맞으면 축 오프셋으로 인정 (숫자가 없으면 지시선이 플랜지 가장자리에 닿은 것으로 봄)
                sz = snap(z, v, rotated=False)
                if sz is not None and abs(sz) >= 50:
                    nz.offset_z = sz
                    nz.notes.append(f"방향 {nz.angle_deg:g}°, 좌우 오프셋 {sz:g} ({'90' if sz > 0 else '270'}° 쪽, 단면도 기하)")
                else:
                    nz.notes.append(f"방향 {nz.angle_deg:g}° (단면도 기하)")
            elif orient == "h":
                nz.angle_deg = 90.0 if z > 0 else 270.0
                sy = snap(y, v)
                nz.offset_y = (sy if sy is not None else round(y)) if abs(y) > 100 else 0.0
                nz.notes.append(f"방향 {nz.angle_deg:g}°, 높이 오프셋 {nz.offset_y:g} (단면도 기하"
                                f"{'' if sy is not None else ', 치수 미확인'})")
            else:
                phi = math.degrees(math.atan2(z, y)) % 360
                row = self._angle_from_row.get(mk)
                row_angle = 0.0 if row == "top" else (180.0 if row == "bottom" else None)
                if row_angle is not None and abs((phi - row_angle + 180) % 360 - 180) < 20:
                    nz.angle_deg = row_angle
                    nz.notes.append(f"방향 {row_angle:g}° (입면도 행, 단면도 기하 {phi:.0f}° 일치)")
                else:
                    nz.angle_deg = round(phi, 1)
                    rr = math.hypot(y, z)
                    if nz.projection is None and R_o + 100 < rr < R_o + 1500:
                        nz.projection = round(rr / 5) * 5
                        nz.notes.append(f"투영 {nz.projection:g} (지시선 끝, 추정)")
                    nz.notes.append(f"방향 {phi:.1f}° (단면도 기하, 반경 방향 노즐)")
            n_geo += 1

        # 기하로 못 정한 노즐: 텍스트 그룹/단독 라벨로 보완
        n_txt = 0
        for mk, g in groups.items():
            nz = spec.nozzle(mk)
            if g["proj"] is not None and (nz.projection is None or nz.projection <= 0):
                nz.projection = g["proj"]
                nz.notes.append(f"투영 {g['proj']:g} (단면도 그룹)")
            if mk in results:
                continue
            if g["orient"] == "h" and views:
                w = g["word"]
                v = min(views, key=lambda u: _dist((w.xc, w.yc), (u["cx"], u["cy"])))
                z_sign = v["sign90"] * (1 if w.xc > v["cx"] else -1)
                nz.angle_deg = 90.0 if z_sign > 0 else 270.0
                nz.notes.append(f"방향 {nz.angle_deg:g}° (단면도 라벨 위치, 추정)")
                n_txt += 1
            elif g["orient"] == "v" and mk not in self._angle_from_row and views:
                w = g["word"]
                v = min(views, key=lambda u: _dist((w.xc, w.yc), (u["cx"], u["cy"])))
                nz.angle_deg = 0.0 if w.yc < v["cy"] else 180.0
                nz.notes.append(f"방향 {nz.angle_deg:g}° (단면도 라벨 위치, 추정)")
                n_txt += 1
        for num, mk, val in singles:
            nz = spec.nozzle(mk)
            if mk in results or not views:
                continue
            v = min(views, key=lambda u: _dist((num.xc, num.yc), (u["cx"], u["cy"])))
            nz.offset_y = -val if num.yc > v["cy"] else val
            nz.notes.append(f"높이 오프셋 {nz.offset_y:g} (단면도 라벨, 추정)")
            n_txt += 1
        self.log.append(f"단면도 반영: 기하 {n_geo} 개, 텍스트 보완 {n_txt} 개")

    # ---- 전체 실행 ------------------------------------------------------
    def parse(self) -> VesselSpec:
        spec = VesselSpec()
        self._find_drawing_no(spec)
        self._find_od(spec)
        self._find_shell_thickness(spec)
        self._find_heads(spec)
        self._find_tl_length(spec)
        self._find_saddle(spec)
        self._find_misc(spec)
        if "lifting" in self.kinds:
            self._find_lugs(spec)
            self._find_saddle_section_lifting(spec)
        if "nozzle_list" in self.kinds:
            self._find_nozzle_list(spec)
        if "elevation" in self.kinds:
            self._find_saddle_section_ga(spec)
            self._find_cog_ga(spec)
            self._find_nozzle_positions(spec)
            self._apply_section_geometry(spec)
        return spec


def parse_drawing(pdf_path: str, verbose: bool = True) -> VesselSpec:
    return parse_drawings([pdf_path], verbose)


def parse_drawings(pdf_paths: Sequence[str], verbose: bool = True) -> VesselSpec:
    """여러 도면을 파싱해 하나의 VesselSpec 으로 병합.
    리프팅 도면 -> GA 노즐 리스트 -> GA 입면도 순으로 적용해 상세 도면 값이 우선한다."""
    parsers = [DrawingParser(p) for p in pdf_paths]

    def rank(p: DrawingParser) -> int:
        return 2 if "elevation" in p.kinds else (1 if "nozzle_list" in p.kinds else 0)

    merged = VesselSpec()
    for p in sorted(parsers, key=rank):
        spec = p.parse()
        if verbose:
            print(f"[파서 로그] {p.pdf_path} -> {', '.join(p.kinds) or '알 수 없는 양식'}")
            for line in p.log:
                print("  -", line)
        merged.merge(spec)
    for msg in merged.resolve():
        if verbose:
            print("  -", msg)
    return merged


if __name__ == "__main__":
    import sys
    paths = sys.argv[1:] or ["P-SK002-002-ME-490-0001 Rev 2.pdf"]
    s = parse_drawings(paths)
    print()
    print(s.summary())
    print(s.nozzle_table())
    for p in s.validate():
        print("경고:", p)
