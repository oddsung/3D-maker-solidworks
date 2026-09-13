"""
pdf_dimension_parser.py - 도면 PDF 에서 용기 주요 치수를 추출한다.

pdfplumber 로 단어(word)와 좌표를 읽은 뒤, 규칙(정규식 + 위치 휴리스틱)으로
VesselSpec 을 채운다. 도면 양식이 바뀌면 이 파일의 규칙만 수정하면 된다.

주의:
- 도면에서 90도 회전된 치수 문자는 pdfplumber 가 글자 순서를 뒤집어 반환한다.
  (예: "O.D 3748" -> "8473 D.O")  upright=False 인 단어는 문자열을 반전시켜 보정한다.
- 여기서 사용하는 위치 휴리스틱은 "리프팅 배치도(LIFTING ARRANGEMENT)" 양식을 기준으로 한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

import pdfplumber

from vessel_spec import VesselSpec

NUM = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"   # 1,234 / 1234 / 1234.5


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    upright: bool

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def _is_number(s: str) -> bool:
    return re.fullmatch(NUM, s.strip()) is not None


# ----------------------------------------------------------------------
def load_words(pdf_path: str, page_no: int = 0) -> Tuple[List[Word], float, float]:
    """PDF 페이지의 단어 목록과 페이지 크기(width, height)를 반환"""
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
            words.append(Word(text, w["x0"], w["x1"], w["top"], upright))
        return words, page.width, page.height


def _same_row(a: Word, b: Word, tol: float = 4.0) -> bool:
    return abs(a.top - b.top) <= tol


def _row_numbers(words: List[Word], ref: Word, tol: float = 4.0) -> List[Word]:
    """ref 와 같은 행에 있는 숫자 단어들을 x 순으로 반환"""
    return sorted(
        [w for w in words if _same_row(w, ref, tol) and _is_number(w.text)],
        key=lambda w: w.x0,
    )


# ----------------------------------------------------------------------
class DrawingParser:
    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path
        self.words, self.page_w, self.page_h = load_words(pdf_path)
        self.full_text = " ".join(w.text for w in self.words)
        self.log: List[str] = []

    # ---- 개별 규칙 ----------------------------------------------------
    def _find_drawing_no(self, spec: VesselSpec) -> None:
        m = re.search(r"\b(P-[A-Z0-9]+-\d{3}-[A-Z]{2}-\d{3}-\d{4})\b", self.full_text)
        if m:
            spec.drawing_no = m.group(1)
        m = re.search(r"(LIFTING ARRANGEMENT DRAWINGS?)", self.full_text)
        if m:
            spec.title = m.group(1)

    def _find_od(self, spec: VesselSpec) -> None:
        for w in self.words:
            m = re.search(r"O\.?D\.?\s*" + NUM, w.text)
            if m:
                spec.outer_diameter = _to_float(m.group(1))
                self.log.append(f"O.D = {spec.outer_diameter} ('{w.text}')")
                return

    def _find_saddle(self, spec: VesselSpec) -> None:
        for w in self.words:
            m = re.search(NUM + r"\s*\(SADDLE TO SADDLE", w.text, re.I)
            if not m:
                continue
            spec.saddle_spacing = _to_float(m.group(1))
            # 같은 행의 좌/우 숫자 = T.L 에서 새들까지 오프셋
            nums = [n for n in _row_numbers(self.words, w) if n is not w]
            if len(nums) >= 2:
                spec.saddle_offset = _to_float(nums[0].text)
            # 바로 아래 행의 가장 큰 숫자 = T.L ~ T.L
            below = [
                n for n in self.words
                if 4 < n.top - w.top < 20 and _is_number(n.text)
            ]
            if below:
                spec.tl_length = max(_to_float(n.text) for n in below)
            self.log.append(
                f"SADDLE spacing={spec.saddle_spacing}, offset={spec.saddle_offset}, T.L={spec.tl_length}"
            )
            return

    def _find_overall(self, spec: VesselSpec) -> None:
        m = re.search(NUM + r"\s*\+\s*" + NUM + r"\s*=\s*" + NUM + r"\s*\(APPROX", self.full_text, re.I)
        if m:
            spec.overall_length = _to_float(m.group(3))
        m = re.search(r"LIFTING WEIGHT\s*:\s*" + NUM + r"\s*kg", self.full_text, re.I)
        if m:
            spec.lifting_weight_kg = _to_float(m.group(1))

    def _find_lugs(self, spec: VesselSpec) -> None:
        """
        휴리스틱: 도면 상단(정면도) 영역에서
          - 한 행에 소수점 숫자 2개 (C.O.G 에서 좌/우 러그까지 거리)
          - 그 바로 위 행에 소수점 숫자 1개 (좌측 T.L 에서 C.O.G 까지 거리)
        """
        decimals = [w for w in self.words if w.upright and re.fullmatch(r"\d+\.\d+", w.text)]
        for w in decimals:
            row = [n for n in decimals if _same_row(n, w)]
            if len(row) != 2:
                continue
            row.sort(key=lambda n: n.x0)
            above = [n for n in decimals if 4 < w.top - n.top < 20]
            if len(above) != 1:
                continue
            spec.cog_from_tl = _to_float(above[0].text)
            left, right = _to_float(row[0].text), _to_float(row[1].text)
            spec.lug_offsets_from_cog = [-left, right]
            self.log.append(f"LUG cog={spec.cog_from_tl}, offsets={spec.lug_offsets_from_cog}")
            return

    def _find_saddle_section(self, spec: VesselSpec) -> None:
        """
        휴리스틱: 좌측 단면도(VIEW A-A) 영역(x < 30% 폭)에서
          - 정수 2개가 한 행에 나란히 -> 새들 바닥 폭 (좌/우 반폭)
          - 회전(세로) 치수 중 반지름 < 값 < 지름 인 최대값 -> 새들 바닥 ~ 중심선 높이
        """
        left = [w for w in self.words if w.xc < self.page_w * 0.30]
        ints = [w for w in left if w.upright and re.fullmatch(r"\d{4}", w.text)]
        for w in ints:
            row = sorted([n for n in ints if _same_row(n, w)], key=lambda n: n.x0)
            if len(row) == 2 and abs(_to_float(row[0].text) - _to_float(row[1].text)) < 200:
                spec.saddle_base_width = _to_float(row[0].text) + _to_float(row[1].text)
                self.log.append(f"SADDLE base width = {spec.saddle_base_width} ({row[0].text}+{row[1].text})")
                break
        vert = [w for w in left if not w.upright and _is_number(w.text)]
        if vert:
            vals = [_to_float(w.text) for w in vert]
            if spec.outer_diameter:
                cand = [v for v in vals if spec.outer_radius < v < spec.outer_diameter]
            else:
                cand = vals
            if cand:
                spec.centerline_height = max(cand)
                self.log.append(f"centerline height = {spec.centerline_height} (후보 {sorted(vals)})")

    # ---- 전체 실행 ------------------------------------------------------
    def parse(self) -> VesselSpec:
        spec = VesselSpec()
        self._find_drawing_no(spec)
        self._find_od(spec)
        self._find_saddle(spec)
        self._find_overall(spec)
        self._find_lugs(spec)
        self._find_saddle_section(spec)
        # T.L 을 못 찾았고 새들 정보가 있으면 계산으로 보완
        if spec.tl_length <= 0 and spec.saddle_spacing > 0 and spec.saddle_offset > 0:
            spec.tl_length = spec.saddle_spacing + 2 * spec.saddle_offset
            self.log.append(f"T.L 계산값 사용 = {spec.tl_length}")
        return spec


def parse_drawing(pdf_path: str, verbose: bool = True) -> VesselSpec:
    parser = DrawingParser(pdf_path)
    spec = parser.parse()
    if verbose:
        print("[파서 로그]")
        for line in parser.log:
            print("  -", line)
    return spec


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "P-SK002-002-ME-490-0001 Rev 2.pdf"
    s = parse_drawing(path)
    print()
    print(s.summary())
    for p in s.validate():
        print("경고:", p)
