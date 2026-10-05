"""
drawing_info.py - 도면 PDF 한 장에서 '도면 자체의 정보'를 추출한다.

pdf_dimension_parser 가 GA/리프팅 도면에서 용기 치수(VesselSpec)를 뽑는 전용 파서라면,
이 모듈은 도면 종류와 무관하게 공통으로 있는 정보를 뽑는 범용 계층이다. 결과(DrawingInfo)는
drawing_set 에서 도면 사이의 연관성 파악과 누락 값 보완에 쓰인다.

추출 항목
  - 제목란      : 도면번호, 리비전, 제목, 시트(n/m), 프로젝트, 발주처 도면번호, TAG, 날짜
  - 입력 등급   : A(벡터+문자) / B(벡터, 문자 없음) / C(문자 일부 깨짐) / D(이미지 스캔)  <- PROJECT_PLAN 4.7
  - 종류        : lifting / ga / nameplate / body_support / nozzle_detail / external_attachment /
                  transportation / development / unknown  (제목 문구로 판별)
  - 뷰          : '( SCALE = 1 : N )' 바로 위 제목. 뷰 영역은 벡터 연결요소(선분 끝점 연결)의 경계 상자
  - 부품표(BOM) : ITEM / NAME OF PART / MATERIAL / Q'TY / UNIT / REMARK, 노즐 상세도는 마크 열 포함
  - 노즐 마크, 용접선(L.W.L/C.W.L/H.W.L), 단면 표식("A"), 노트, SEE DWG./SEE NOTE 참조
  - 라벨 치수   : 'nnnn TO C.L', 'nnnn TO T.L' (어느 뷰에 속하는지 포함)
  - 사실(fact)  : 도면 간 비교가 가능한 키-값 (T.L~T.L, 새들 간격, O.D, 중량, 설계 압력 ...)
  - 전개도      : 마크별 T.L+ 위치 (입면도 노즐 위치와 교차 검증용)

이미지 스캔(등급 D) 도면은 문자가 없어 파일명에서 도면번호/리비전/시트만 얻고, 나머지는
같은 도면번호의 다른 시트에서 물려받거나(drawing_set) AI(ai_headless)로 읽는다.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

import pdfplumber

from pdf_dimension_parser import DrawingParser, Word, MARK_GROUP, expand_marks, _dist

DWG_NO = r"P-[A-Z0-9]+-\d{3}-[A-Z]{2}-\d{3}-\d{4}"
FILE_RE = re.compile(r"^(?P<no>P-\S+?)\s+Rev[ .]?(?P<rev>[\w-]+?)(?:\s+\((?P<sheet>\d+)of(?P<total>\d+)\))?\.pdf$", re.I)

KIND_PATTERNS = [
    ("lifting", r"LIFTING ARRANGEMENT"),
    ("ga", r"GENERAL ARRANGEMENT"),
    ("nameplate", r"NAME ?PLATE"),
    ("body_support", r"DETAIL OF BODY"),
    ("nozzle_detail", r"DETAIL OF NOZZ"),
    ("external_attachment", r"EXTERNAL ATTACHMENT"),
    ("transportation", r"TRANSPORTATION"),
    ("development", r"DEVELOPMENT"),
]
KIND_KO = {
    "lifting": "리프팅 배치도", "ga": "일반 배치도(GA)", "nameplate": "명판", "body_support": "몸체/서포트 상세",
    "nozzle_detail": "노즐 상세", "external_attachment": "외부 부착물 상세", "transportation": "운송도",
    "development": "전개도", "unknown": "미분류",
}
GRADE_KO = {"A": "벡터+문자", "B": "벡터(문자 없음)", "C": "문자 일부 깨짐", "D": "이미지 스캔"}

NUMBER = r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?"


def _num(s: str) -> float:
    return float(s.replace(",", ""))


@dataclass
class View:
    name: str
    scale: str = ""
    x: float = 0.0
    y: float = 0.0
    kind: str = "view"                 # nozzle | section | view | detail | general
    marks: List[str] = field(default_factory=list)
    bbox: Optional[List[float]] = None   # 뷰 영역 [x0, y0, x1, y1] (pt)
    dims: List[str] = field(default_factory=list)
    words: List[list] = field(default_factory=list)   # 노즐 뷰 안의 단어 [text, upright, x, y] (힐사이드 판정용)
    profile: Optional[dict] = None                     # 노즐 외곽선 (벡터 굵은 선에서 읽음): points [[s_from_face, r] mm], bore_d, flange_od ...
    region: Optional[List[float]] = None               # 노즐 뷰 확장 영역: 노즐 윤곽에 닿는 연결요소(지지판, 쉘 판재)까지 합친 상자
    region_words: List[list] = field(default_factory=list)   # 확장 영역 안의 단어 (+ 가장 가까운 뷰에 배정된 지지판 문구)
    letters: List[str] = field(default_factory=list)  # 뷰 안의 보조 뷰 표식 ("A", "B" ...; 용접 W1 제외)


@dataclass
class Part:
    item: str
    name: str = ""
    material: str = ""
    qty: str = ""
    unit: str = ""
    remark: str = ""
    marks: List[str] = field(default_factory=list)


@dataclass
class Dim:
    text: str
    value: float
    ref: str            # C.L | T.L
    x: float
    y: float
    view: str = ""
    upright: bool = True      # 정립 문자 = 가로 치수, 회전 문자 = 세로 치수


@dataclass
class Fact:
    key: str
    value: object
    unit: str = ""
    text: str = ""


@dataclass
class DrawingInfo:
    file: str
    path: str
    drawing_no: str = ""
    rev: str = ""
    sheet: int = 1
    sheet_total: int = 1
    title: str = ""
    subtitle: str = ""
    title_inferred: bool = False        # 스캔 도면: 제목을 다른 시트에서 물려받음
    project: str = ""
    owner_dwg_no: str = ""
    tag_no: str = ""
    date: str = ""
    grade: str = "A"
    kind: str = "unknown"
    parser_kinds: List[str] = field(default_factory=list)   # pdf_dimension_parser 판별: lifting / nozzle_list / elevation
    n_words: int = 0
    n_lines: int = 0
    n_curves: int = 0
    n_images: int = 0
    views: List[View] = field(default_factory=list)
    parts: List[Part] = field(default_factory=list)
    marks: List[str] = field(default_factory=list)
    mark_views: Dict[str, List[str]] = field(default_factory=dict)
    see_dwg_marks: List[str] = field(default_factory=list)   # GA 노즐 리스트에서 투영이 'SEE DWG.' 인 마크
    tl_labels: Dict[str, float] = field(default_factory=dict) # 전개도: 마크 -> T.L+ 위치
    seams: List[str] = field(default_factory=list)
    seam_angles: Dict[str, float] = field(default_factory=dict)
    markers: List[str] = field(default_factory=list)          # 단면/뷰 표식 "A"
    unresolved_markers: List[str] = field(default_factory=list)   # 같은 시트에 VIEW/SECTION 제목이 없는 표식
    side_labels: List[list] = field(default_factory=list)   # 'FIXED SIDE' / 'SLIDING SIDE' 문구 [text, x, y] (고정 새들 방향 판별)
    notes: List[str] = field(default_factory=list)
    dims: List[Dim] = field(default_factory=list)
    facts: List[Fact] = field(default_factory=list)
    refs: List[str] = field(default_factory=list)
    ai: Optional[dict] = None
    log: List[str] = field(default_factory=list)
    text: str = field(default="", repr=False)      # 전체 문자 (다른 도면 제목/번호 언급 검색용, JSON 제외)
    rows_text: List[str] = field(default_factory=list, repr=False)   # 정립 문자 행별 텍스트 (표/치수 행 패턴 검색용)

    @property
    def short(self) -> str:
        """'0005 (3/8)' 형태의 짧은 이름"""
        tail = self.drawing_no.rsplit("-", 1)[-1] if self.drawing_no else self.file
        return f"{tail} ({self.sheet}/{self.sheet_total})" if self.sheet_total > 1 else tail

    @property
    def kind_ko(self) -> str:
        return KIND_KO.get(self.kind, self.kind)

    def fact(self, key: str):
        for f in self.facts:
            if f.key == key:
                return f.value
        return None

    def fact_values(self, key: str) -> list:
        return [f.value for f in self.facts if f.key == key]

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("text", None)
        d.pop("rows_text", None)
        d["short"] = self.short
        d["kind_ko"] = self.kind_ko
        return d


# ----------------------------------------------------------------------
def parse_filename(name: str) -> dict:
    m = FILE_RE.match(os.path.basename(name))
    if not m:
        return {}
    return {"drawing_no": m.group("no"), "rev": m.group("rev"),
            "sheet": int(m.group("sheet") or 1), "sheet_total": int(m.group("total") or 1)}


def _cell_text(ws: List["Word"]) -> str:
    """표 칸의 단어들 -> 문자열. 같은 줄(top 2.5pt 이내)끼리 x 순으로 잇는다. 비고가 두 줄이고 글자 단위로 쪼개진
    경우('t 4 .5 F O R 26' 처럼) 앞 단어와 1pt 이내로 붙은 조각은 공백 없이 이어 붙인다"""
    lines: List[List["Word"]] = []
    for w in sorted(ws, key=lambda w: (w.top, w.x0)):
        if lines and abs(w.top - lines[-1][0].top) < 2.5:
            lines[-1].append(w)
        else:
            lines.append([w])
    out = []
    for ln in lines:
        s, prev = "", None
        for w in sorted(ln, key=lambda w: w.x0):
            if prev is not None:
                s += "" if w.x0 - prev.x1 < 1.0 else " "
            s += w.text.strip()
            prev = w
        out.append(s)
    return " ".join(out)


def _components(segs: List[Tuple[Tuple[float, float], Tuple[float, float]]], tol: float = 8.0) -> List[dict]:
    """선분 끝점이 tol 이내로 이어진 것끼리 묶은 연결요소의 경계 상자 목록 (큰 것부터)"""
    n = len(segs)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cell: Dict[Tuple[int, int], List[Tuple[int, Tuple[float, float]]]] = {}

    def key(p):
        return (int(p[0] // tol), int(p[1] // tol))

    for i, (a, b) in enumerate(segs):
        for p in (a, b):
            cell.setdefault(key(p), []).append((i, p))
    for i, (a, b) in enumerate(segs):
        for p in (a, b):
            kx, ky = key(p)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j, q in cell.get((kx + dx, ky + dy), []):
                        if j != i and math.hypot(p[0] - q[0], p[1] - q[1]) <= tol:
                            parent[find(i)] = find(j)
    boxes: Dict[int, List[float]] = {}
    members: Dict[int, List[int]] = {}
    for i, (a, b) in enumerate(segs):
        members.setdefault(find(i), []).append(i)
        bb = boxes.setdefault(find(i), [1e9, 1e9, -1e9, -1e9, 0])
        for p in (a, b):
            bb[0], bb[1] = min(bb[0], p[0]), min(bb[1], p[1])
            bb[2], bb[3] = max(bb[2], p[0]), max(bb[3], p[1])
        bb[4] += 1
    out = [{"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3], "n": b[4], "idx": members[r]} for r, b in boxes.items()]
    return sorted(out, key=lambda c: -(c["x1"] - c["x0"]) * (c["y1"] - c["y0"]))


# ----------------------------------------------------------------------
class SheetExtractor:
    """PDF 한 페이지 -> DrawingInfo"""

    def __init__(self, pdf_path: str):
        self.path = pdf_path
        self.pt_per_mm: Optional[float] = None   # DXF 는 시트 축척을 정확히 알고 있다 (PDF 는 None)
        if pdf_path.lower().endswith(".dxf"):
            # DXF: 모델 공간 요소를 PDF 와 같은 기본 도형(같은 좌표 규모, 같은 굵기 관례)으로 바꿔 받는다
            from dxf_reader import load_dxf
            d = load_dxf(pdf_path)
            self.n_lines, self.n_curves, self.n_images = d["n_lines"], d["n_curves"], d["n_images"]
            self.W, self.H = d["page_w"], d["page_h"]
            self.raw_lines, self.raw_curves = d["raw_lines"], d["raw_curves"]
            self.pt_per_mm = d["scale_pt_per_mm"]
        else:
            with pdfplumber.open(pdf_path) as pdf:
                page = pdf.pages[0]
                self.n_lines, self.n_curves, self.n_images = len(page.lines), len(page.curves), len(page.images)
                self.W, self.H = float(page.width), float(page.height)
                # 선 굵기/대시 정보가 있는 원본 선분 (노즐 외곽선 추출용: 물체선은 치수선보다 굵고, 중심선은 일점쇄선)
                self.raw_lines = []
                for l in page.lines:
                    pts = l.get("pts") or []
                    if len(pts) < 2:
                        continue
                    dash = l.get("dash")
                    self.raw_lines.append({"p1": (pts[0][0], pts[0][1]), "p2": (pts[-1][0], pts[-1][1]),
                                           "lw": float(l.get("linewidth") or 0), "dash": list(dash[0]) if dash and dash[0] else []})
                self.raw_curves = []
                for c in page.curves:
                    pts = c.get("pts") or []
                    if len(pts) < 2:
                        continue
                    self.raw_curves.append({"p1": (pts[0][0], pts[0][1]), "p2": (pts[-1][0], pts[-1][1]),
                                            "lw": float(c.get("linewidth") or 0),
                                            "w": c["x1"] - c["x0"], "h": c["bottom"] - c["top"]})
        self.dp = DrawingParser(pdf_path)
        self.words: List[Word] = self.dp.words
        self.full_text = self.dp.full_text
        self.rows = self._cluster_rows([w for w in self.words if w.upright])
        self.log: List[str] = []

    # ---- 헬퍼 ------------------------------------------------------------
    @staticmethod
    def _cluster_rows(words: List[Word], tol: float = 3.5) -> List[List[Word]]:
        ws = sorted(words, key=lambda w: w.top)
        rows: List[List[Word]] = []
        for w in ws:
            if rows and w.top - rows[-1][-1].top <= tol:
                rows[-1].append(w)
            else:
                rows.append([w])
        return [sorted(r, key=lambda w: w.x0) for r in rows]

    @staticmethod
    def _row_text(row: List[Word], sep: str = " ") -> str:
        return sep.join(w.text for w in row)

    def _label(self, pattern: str, region=None) -> Optional[Word]:
        for w in self.words:
            if not w.upright or not re.fullmatch(pattern, w.text.strip(), re.I):
                continue
            if region and not region(w):
                continue
            return w
        return None

    def _below(self, lab: Word, dy0: float = 4, dy1: float = 14, dx: float = 60) -> List[Word]:
        out = [w for w in self.words if w.upright and w is not lab
               and dy0 <= w.top - lab.top <= dy1 and abs(w.xc - lab.xc) < dx]
        return sorted(out, key=lambda w: w.x0)

    # ---- 제목란 ----------------------------------------------------------
    def _title_block(self, info: DrawingInfo) -> None:
        W, H = self.W, self.H
        region = lambda w: w.x0 > W * 0.68 and w.top > H * 0.72
        fn = parse_filename(info.file)
        lab = self._label(r"TITLE", region)
        if lab:
            l1 = [w for w in self.words if w.upright and lab.top + 4 < w.top < lab.top + 15
                  and lab.x0 - 5 <= w.x0 < W * 0.96 and not re.fullmatch(r"\d", w.text)]
            info.title = " ".join(w.text for w in sorted(l1, key=lambda w: w.x0)).strip()
            l2 = [w for w in self.words if w.upright and lab.top + 15 < w.top < lab.top + 26
                  and lab.x0 + 15 <= w.x0 < W * 0.96 and not re.fullmatch(r"\d", w.text)]
            info.subtitle = " ".join(w.text for w in sorted(l2, key=lambda w: w.x0)).strip()
        # 도면번호/발주처 도면번호: 제목란 라벨 아래 단어 우선 (참조 도면 목록에 다른 번호가 있을 수 있음)
        lab = self._label(r"SLB DWG\. NO\.?", region)
        below = [w for w in self._below(lab, 3, 14, 120) if re.fullmatch(DWG_NO, w.text)] if lab else []
        if below:
            info.drawing_no = below[0].text
        else:
            m = re.search(r"\b(" + DWG_NO + r")\b", self.full_text)
            info.drawing_no = m.group(1) if m else fn.get("drawing_no", "")
        lab = self._label(r"OWNER DWG\. NO\.?", region)
        below = [w for w in self._below(lab, 3, 14, 120) if re.match(r"VP-", w.text)] if lab else []
        if below:
            info.owner_dwg_no = below[0].text
        lab = self._label(r"REV\.?", region)
        rev = None
        if lab:
            c = [w for w in self.words if w.upright and w.x0 > lab.x0 - 3 and w.x0 < lab.x0 + 40
                 and lab.top + 10 < w.top < lab.top + 24 and re.fullmatch(r"[\w-]+", w.text)]
            if c:
                rev = c[0].text
        if rev is None:
            m = re.search(DWG_NO + r"\s+Rev\.?\s*([\w-]+)", self.full_text)
            rev = m.group(1) if m else fn.get("rev", "")
        info.rev = rev
        m = re.search(r"\((\d+)\s*/\s*(\d+)\)", info.title)
        if m:
            info.sheet, info.sheet_total = int(m.group(1)), int(m.group(2))
        elif fn:
            info.sheet, info.sheet_total = fn.get("sheet", 1), fn.get("sheet_total", 1)
        date_re = r"\d{1,2}[.\-/ ][A-Z]{3}[.\-/ ]\d{4}|\d{4}[.\-/]\d{2}[.\-/]\d{2}|\d{2}[.\-/]\d{2}[.\-/]\d{4}"
        lab = self._label(r"DATE DRAWN", region)
        if lab:
            b = [w for w in self._below(lab, 4, 14, 40) if re.search(date_re, w.text)]
            if b:
                info.date = b[0].text
        if not info.date:   # 라벨 문자가 글자 단위로 쪼개진 시트: 제목란(리비전 표 제외) 영역의 날짜 형식 단어
            c = [w for w in self.words if w.upright and w.x0 > W * 0.75 and w.x0 < W * 0.86 and w.top > H * 0.83
                 and re.fullmatch(date_re, w.text.strip())]
            if c:
                info.date = min(c, key=lambda w: w.top).text
        if not info.owner_dwg_no:
            m = re.search(r"\b(VP-[A-Z0-9-]+)\b", self.full_text)
            if m:
                info.owner_dwg_no = m.group(1)
        lab = self._label(r"PROJECT", region)
        if lab:
            b = [w for w in self.words if w.upright and 3 < w.top - lab.top < 12 and w.x0 >= lab.x0 - 5 and w.x0 < W * 0.96]
            info.project = " ".join(w.text for w in sorted(b, key=lambda w: w.x0))
        lab = self._label(r"TAG NO\.?", lambda w: w.x0 > W * 0.68 and w.top > H * 0.6)
        if lab:
            b = [w for w in self.words if w.upright and 3 < w.top - lab.top < 16 and abs(w.xc - lab.xc) < 90
                 and re.search(r"[A-Z]\d|\d", w.text) and not re.fullmatch(r"TAG NO\.?", w.text)]
            if b:
                info.tag_no = max(b, key=lambda w: len(w.text)).text

    # ---- 종류 / 등급 ------------------------------------------------------
    def _kind(self, info: DrawingInfo) -> None:
        for kind, pat in KIND_PATTERNS:
            if re.search(pat, info.title, re.I):
                info.kind = kind
                return
        pk = self.dp.kinds
        if "lifting" in pk:
            info.kind = "lifting"
        elif "nozzle_list" in pk or "elevation" in pk:
            info.kind = "ga"

    def _grade(self, info: DrawingInfo) -> None:
        nw = len(self.words)
        vec = self.n_lines + self.n_curves
        if nw < 20 and self.n_images >= 1:
            info.grade = "D"
        elif nw < 20 and vec > 200:
            info.grade = "B"
        elif nw >= 50 and vec >= 200:
            info.grade = "A"
        else:
            info.grade = "C"

    # ---- 뷰 --------------------------------------------------------------
    def _views(self, info: DrawingInfo) -> List[dict]:
        segs = list(self.dp.segs) + list(self.dp.centerlines)
        comps = [c for c in _components(segs, 8.0)
                 if (c["x1"] - c["x0"]) < 0.8 * self.W and (c["y1"] - c["y0"]) < 0.8 * self.H
                 and (c["x1"] - c["x0"]) > 25 and (c["y1"] - c["y0"]) > 25]
        noise = re.compile(r'\d+|"[A-Z]\d?"|\(\s*\)|I\.D [\d.]+|VESSEL C\.L|[()]')
        for w in self.words:
            m = re.search(r"SCALE\s*=\s*([\d.]+\s*:\s*[\d.]+|NONE|N\.?T\.?S\.?)", w.text, re.I)
            if not m:
                continue
            # SCALE 바로 위 띠(24pt) 안의 단어. 노즐 풍선은 마크(위)/사이즈(아래) 두 줄이라 줄이 아니라
            # x 방향으로 이어진 덩어리를 제목으로 본다 (겹치거나 14pt 이내로 붙은 단어 연결)
            band = [u for u in self.words if u.upright == w.upright and u is not w
                    and -1 <= w.top - u.bottom <= 24 and abs(u.xc - w.xc) < 260
                    and not noise.fullmatch(u.text.strip()) and "SCALE" not in u.text.upper()]
            near = [u for u in band if w.top - u.bottom <= 12]
            if not near:
                continue
            seed = min(near, key=lambda u: abs(u.xc - w.xc))

            def keep(u: Word) -> bool:
                """12~24pt 위 단어는 풍선 마크이거나, 제목 바로 위의 긴 문구(LEFT HEAD 등)만 인정"""
                if w.top - u.bottom <= 12:
                    return True
                t = u.text.strip()
                if re.fullmatch(MARK_GROUP, t):
                    return True
                overlap = min(u.x1, seed.x1) - max(u.x0, seed.x0)
                return overlap > 0.5 * (seed.x1 - seed.x0) and len(re.sub(r"[^A-Z]", "", t)) >= 5 and not t.endswith(".")

            same = sorted([u for u in band if keep(u)], key=lambda u: u.x0)
            idx = same.index(seed)
            lo, hi = idx, idx
            right = seed.x1
            while hi < len(same) - 1 and same[hi + 1].x0 - right < 30:
                hi += 1
                right = max(right, same[hi].x1)
            left = seed.x0
            while lo > 0 and left - same[lo - 1].x1 < 30:
                lo -= 1
                left = min(left, same[lo].x0)
            title_words = same[lo:hi + 1]
            marks: List[str] = []
            sizes: List[str] = []
            others: List[str] = []
            for u in title_words:
                t = u.text.strip()
                if re.fullmatch(MARK_GROUP, t):
                    marks += expand_marks(t)
                elif re.fullmatch(r'\d+(?:\s\d/\d)?"', t):
                    if t not in sizes:
                        sizes.append(t)
                elif t not in others:
                    others.append(t)
            name = " ".join(([compact_marks(marks)] if marks else []) + sizes + others)
            kind = "general"
            if marks and re.search(r"NOZZLE|MANHOLE|MANWAY", name, re.I):
                kind = "nozzle"
            elif re.match(r"SECTION", name, re.I):
                kind = "section"
            elif re.match(r"DETAIL", name, re.I):
                kind = "detail"
            elif re.match(r"VIEW", name, re.I):
                kind = "view"
            scale = re.sub(r"\s+", "", m.group(1)).upper()
            v = View(name=name, scale=scale, x=round(w.xc, 1), y=round(w.top, 1), kind=kind, marks=marks)
            # 뷰 영역: 제목 바로 위(60pt 이내)에 있고 제목 x 를 덮는 연결요소 중 가장 가까운 것
            best = None
            for c in comps:
                if c["x0"] - 15 <= seed.xc <= c["x1"] + 15:
                    gap = seed.top - c["y1"]
                    if -8 <= gap <= 60 and (best is None or gap < best[0]):
                        best = (gap, c)
            if best:
                c = best[1]
                v.bbox = [round(c["x0"], 1), round(c["y0"], 1), round(c["x1"], 1), round(c["y1"], 1)]
                v.words = [[u.text.strip(), u.upright, round(u.xc, 1), round(u.yc, 1)] for u in self.words
                           if c["x0"] - 10 <= u.xc <= c["x1"] + 10 and c["y0"] - 10 <= u.yc <= c["y1"] + 10]
            info.views.append(v)
        # 확장 영역(지지판 문구·선 포함)을 먼저 정한 뒤 외곽선/지지판을 읽는다
        self._view_regions(info, comps, segs)
        for v in info.views:
            if v.kind == "nozzle" and v.bbox:
                try:
                    v.profile = self._nozzle_profile(v)
                except Exception as e:      # 외곽선 추출은 부가 기능: 실패해도 뷰 정보는 유지
                    self.log.append(f"{v.name}: 외곽선 추출 실패 ({e})")
        for v in info.views:
            for mk in v.marks:
                info.mark_views.setdefault(mk, []).append(v.name)
        return comps

    def _view_regions(self, info: DrawingInfo, comps: List[dict], segs) -> None:
        """노즐 뷰의 확장 영역: 노즐 윤곽 연결요소에 닿는(3pt 이내로 겹치는) 다른 연결요소를 반복해서 합친다.
        지지판·쉘 판재는 목에 T 자로 닿거나 목을 가로질러 끝점 연결요소가 따로 생기므로(N30) 기본 영역에서 빠진다.
        '닿음'은 상자 겹침이 아니라 선분 끝점이 이미 합친 선분 위(3pt 이내)에 있는지로 판정한다(상자로 보면 표제란이
        딸려 들어옴). 다른 뷰의 기본 영역과 겹치는 요소, 지면의 절반보다 큰 요소는 합치지 않는다. 그 뒤 'n-SUP'T PLATES' 문구와
        '(tXxWY)' 는 가장 가까운 노즐 뷰(80pt 이내)에 배정하고, 영역 안의 보조 뷰 표식("A" 등)을 모은다"""
        prim = {id(v): tuple(v.bbox) for v in info.views if v.bbox}

        def overlap(a, b, m=0.0):
            return a[0] - m <= b[2] and b[0] - m <= a[2] and a[1] - m <= b[3] and b[1] - m <= a[3]

        def near_seg(p, sg, tol=3.0) -> bool:
            (ax_, ay_), (bx_, by_) = sg
            vx, vy = bx_ - ax_, by_ - ay_
            L2 = vx * vx + vy * vy
            t = 0.0 if L2 < 1e-9 else max(0.0, min(1.0, ((p[0] - ax_) * vx + (p[1] - ay_) * vy) / L2))
            return math.hypot(p[0] - (ax_ + t * vx), p[1] - (ay_ + t * vy)) <= tol

        def touches(c, used_idx) -> bool:
            box = (c["x0"] - 3, c["y0"] - 3, c["x1"] + 3, c["y1"] + 3)
            near = [segs[i] for i in used_idx
                    if overlap(box, (min(segs[i][0][0], segs[i][1][0]), min(segs[i][0][1], segs[i][1][1]),
                                     max(segs[i][0][0], segs[i][1][0]), max(segs[i][0][1], segs[i][1][1])))]
            if not near:
                return False
            mine = [segs[i] for i in c["idx"]]
            return (any(near_seg(p, sg) for s2 in mine for p in s2 for sg in near)
                    or any(near_seg(p, sg) for s2 in near for p in s2 for sg in mine))

        for v in info.views:
            if v.kind != "nozzle" or not v.bbox:
                continue
            others = [bb for k, bb in prim.items() if k != id(v)]
            reg = list(v.bbox)
            base = next((c for c in comps if all(abs(round(c[key], 1) - v.bbox[j]) < 0.06
                                                 for j, key in enumerate(("x0", "y0", "x1", "y1")))), None)
            used_idx = list(base["idx"]) if base else []
            used = {id(base)} if base else set()
            changed = bool(base)
            while changed:
                changed = False
                for c in comps:
                    cb = (c["x0"], c["y0"], c["x1"], c["y1"])
                    if id(c) in used or (cb[2] - cb[0]) > 0.5 * self.W or (cb[3] - cb[1]) > 0.5 * self.H:
                        continue
                    if not overlap(reg, cb, 3.0) or any(overlap(cb, ob, 2.0) for ob in others):
                        continue
                    if not touches(c, used_idx):
                        continue
                    used.add(id(c))
                    used_idx += c["idx"]
                    reg = [min(reg[0], cb[0]), min(reg[1], cb[1]), max(reg[2], cb[2]), max(reg[3], cb[3])]
                    changed = True
            v.region = [round(x, 1) for x in reg]
            v.region_words = [[u.text.strip(), u.upright, round(u.xc, 1), round(u.yc, 1)] for u in self.words
                              if reg[0] - 10 <= u.xc <= reg[2] + 10 and reg[1] - 10 <= u.yc <= reg[3] + 10]
        # 지지판 문구는 목 옆에 지시선으로 달려 영역 밖에 놓이기도 한다 -> 가장 가까운 노즐 뷰에 배정
        nozzle_views = [v for v in info.views if v.kind == "nozzle" and v.region]

        def assign(u: Word, cands: List[View], limit: float) -> None:
            best = None
            for v in cands:
                r = v.region
                dist = math.hypot(max(r[0] - u.xc, 0.0, u.xc - r[2]), max(r[1] - u.yc, 0.0, u.yc - r[3]))
                if dist <= limit and (best is None or dist < best[0]):
                    best = (dist, v)
            if best:
                entry = [u.text.strip(), u.upright, round(u.xc, 1), round(u.yc, 1)]
                if entry not in best[1].region_words:
                    best[1].region_words.append(entry)

        for u in self.words:
            t = u.text.strip()
            if (re.search(r"SUP'?T\.?\s*PLATES?", t, re.I)
                    or re.fullmatch(r"W\.?P\.?", t.replace(" ", ""), re.I)
                    or re.fullmatch(r"\(\s*t\d+(?:\.\d+)?\s*[xX×]\s*W\d+(?:\.\d+)?\s*\)", t)):
                assign(u, nozzle_views, 80.0)
        # 힐사이드 표기('VESSEL C.L' 수평선 + 축과의 각도)는 뷰 그림 위쪽에 떨어져 있어 바로 위 뷰가 더 가깝기도 하다
        # (0005 8/8 의 N31 용 'VESSEL C.L' 은 N31 보다 위쪽 N30 뷰에 더 가깝다). 이 문구는 W.P 가 있는 뷰에서만
        # 뜻이 있으므로 후보를 그런 뷰로 한정한다
        hill = [v for v in nozzle_views
                if any(w[0].replace(" ", "") in ("W.P", "W.P.") for w in v.region_words)]
        if hill:
            for u in self.words:
                t = u.text.strip()
                if re.search(r"VESSEL\s*C\.?L", t, re.I) or re.fullmatch(r"\d{1,2}(?:\.\d)?°", t):
                    assign(u, hill, 130.0)
        # 보조 뷰 표식("A"): 1pt 이내로 붙은 같은 줄 조각을 이어 붙인 뒤('"B' + '"'), 가장 가까운 노즐 뷰(60pt 이내)에 배정.
        # 표식 화살표는 목 옆 허공에 있어 선 연결로 만든 영역 밖에 놓이기 쉽다(0005 8/8 N30)
        ws = sorted([u for u in self.words if u.upright], key=lambda u: (round(u.top), u.x0))
        toks: List[list] = []
        cur = None                                      # [text, top, x0, x1, bottom]
        for u in ws:
            if cur is not None and abs(u.top - cur[1]) < 1.0 and -0.5 <= u.x0 - cur[3] < 1.0:
                cur = [cur[0] + u.text.strip(), cur[1], cur[2], u.x1, max(cur[4], u.bottom)]
            else:
                if cur is not None:
                    toks.append(cur)
                cur = [u.text.strip(), u.top, u.x0, u.x1, u.bottom]
        if cur is not None:
            toks.append(cur)
        for t, top, tx0, tx1, bot in toks:
            m = re.fullmatch(r'"\s*((?!W\d)[A-Z]\d?)\s*"', t)
            if not m:
                continue
            xc, yc = (tx0 + tx1) / 2, (top + bot) / 2
            best = None
            for v in nozzle_views:
                r = v.region
                dist = math.hypot(max(r[0] - xc, 0.0, xc - r[2]), max(r[1] - yc, 0.0, yc - r[3]))
                if dist <= 60 and (best is None or dist < best[0]):
                    best = (dist, v)
            if best and m.group(1) not in best[1].letters:
                best[1].letters.append(m.group(1))

    # ---- 노즐 외곽선 (벡터) ---------------------------------------------------
    def _prof_fail(self, v: View, reason: str):
        """외곽선 추출이 중간에 끊긴 이유를 로그에 남긴다 (다른 도면 양식/포맷을 붙일 때 원인 추적용)"""
        self.log.append(f"{v.name}: 외곽선 없음 ({reason})")
        return None

    def _nozzle_profile(self, v: View) -> Optional[dict]:
        """노즐 상세 뷰의 굵은 물체선을 축 기준 (s, r) 로 바꿔 바깥 실루엣을 만든다.
        - 축: 뷰 안의 일점쇄선 중 굵은 물체선이 가장 많이 평행한 것 (가로/세로뿐 아니라 45° 로 그린 뷰도 지원)
        - 물체선: 가장 얇은 선(치수선)의 1.5배보다 굵은 선. 작은 곡선(필릿)은 현으로, 큰 곡선(쉘 원호)은 제외
        - 플랜지: 노즐 호칭(제목의 8" 등)의 CL300 표준 외경과 맞는 수직선. 플랜지 선과 끝점이 이어진
          선분만 노즐로 보고(쉘 판재·용접 표시 제외), 플랜지 반경을 넘는 부분(서포트 플레이트 등)은 잘라낸다
        - s 는 플랜지 면에서 쉘 쪽으로 증가 (mm). 축척은 뷰의 'Ønnn' 문자와 대칭 선 쌍의 간격으로 보정"""
        from pdf_dimension_parser import parse_size_inch
        from vessel_spec import FLANGE_CL300
        x0, y0, x1, y1 = v.bbox
        m = 6.0

        def inside(p):
            return x0 - m <= p[0] <= x1 + m and y0 - m <= p[1] <= y1 + m

        lines = [l for l in self.raw_lines if inside(l["p1"]) and inside(l["p2"])]
        if len(lines) < 6:
            return self._prof_fail(v, f"뷰 안 선분 {len(lines)}개")
        thin = min(l["lw"] for l in lines)
        thick = [l for l in lines if l["lw"] > thin * 1.5 + 1e-6]
        dashed = [l for l in lines if l["dash"] and max(l["dash"]) > 5
                  and _dist(l["p1"], l["p2"]) > 12]
        if not thick or not dashed:
            return self._prof_fail(v, f"굵은 물체선 {len(thick)}개, 일점쇄선 {len(dashed)}개")

        def direction(l):
            dx, dy = l["p2"][0] - l["p1"][0], l["p2"][1] - l["p1"][1]
            n_ = math.hypot(dx, dy)
            return (dx / n_, dy / n_) if n_ > 1e-9 else (1.0, 0.0)

        def parallel_thick(d) -> int:
            """축 후보와 평행한(2도 이내) 굵은 물체선 수: 목/보어 선은 축과 나란하다"""
            cnt = 0
            for l in thick:
                dx, dy = l["p2"][0] - l["p1"][0], l["p2"][1] - l["p1"][1]
                n_ = math.hypot(dx, dy)
                if n_ > 3 and abs(dx * d[1] - dy * d[0]) / n_ < 0.035:
                    cnt += 1
            return cnt

        # 축: 일점쇄선 중 굵은 물체선이 가장 많이 평행한 방향, 같으면 가장 긴 것. 45도로 그린 뷰(N31/N31B)도 잡힌다
        ax = max(dashed, key=lambda l: (parallel_thick(direction(l)), _dist(l["p1"], l["p2"])))
        d_ax = direction(ax)
        n_ax = (-d_ax[1], d_ax[0])
        a0 = ax["p1"]
        ax_deg = math.degrees(math.atan2(d_ax[1], d_ax[0])) % 180
        vertical = abs(ax_deg - 90) < 1

        def uv(p):
            """축 기준 좌표: u = 축 방향 투영, w = 축에서 떨어진 부호 있는 거리"""
            vx, vy = p[0] - a0[0], p[1] - a0[1]
            return (vx * d_ax[0] + vy * d_ax[1], vx * n_ax[0] + vy * n_ax[1])

        raw = []                                      # (p1, p2) 굵은 선분 + 작은 곡선의 현
        for l in thick:
            raw.append((l["p1"], l["p2"]))
        for c in self.raw_curves:
            if c["lw"] > thin * 1.5 + 1e-6 and inside(c["p1"]) and inside(c["p2"]) and max(c["w"], c["h"]) < 8:
                raw.append((c["p1"], c["p2"]))
        # (u, r) 로 변환. 축을 가로지르는 선은 r 범위 [0, max] 로
        segs = []
        for p1, p2 in raw:
            u1, w1 = uv(p1)
            u2, w2 = uv(p2)
            if w1 * w2 < 0:
                r1, r2 = 0.0, max(abs(w1), abs(w2))
            else:
                r1, r2 = abs(w1), abs(w2)
            segs.append([u1, r1, u2, r2, p1, p2])
        # 연결요소 (끝점 1.2pt 이내)
        n = len(segs)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def on_seg(p, s) -> bool:
            """점 p 가 선분 s 위(1.2pt 이내)에 있는지 (T 자 접합: 보어 선이 플랜지 면 선 중간에 닿는 경우)"""
            (ax_, ay_), (bx_, by_) = s[4], s[5]
            vx, vy = bx_ - ax_, by_ - ay_
            L2 = vx * vx + vy * vy
            if L2 < 1e-9:
                return _dist(p, s[4]) <= 1.2
            t = max(0.0, min(1.0, ((p[0] - ax_) * vx + (p[1] - ay_) * vy) / L2))
            return _dist(p, (ax_ + t * vx, ay_ + t * vy)) <= 1.2

        for i in range(n):
            for j in range(i + 1, n):
                if any(on_seg(p, segs[j]) for p in segs[i][4:6]) or any(on_seg(q, segs[i]) for q in segs[j][4:6]):
                    parent[find(i)] = find(j)
        par = [s for s in segs if abs(s[1] - s[3]) < 0.3 and abs(s[0] - s[2]) > 3]
        perp = [s for s in segs if abs(s[0] - s[2]) < 0.3 and abs(s[1] - s[3]) > 1]
        if len(par) < 2 or not perp:
            return self._prof_fail(v, f"축과 나란한 선 {len(par)}개, 축에 수직인 선 {len(perp)}개")
        # 축척 보정 (뷰 안 'Ønnn' 과 대칭 평행선 쌍)
        scale_n = int(v.scale.split(":")[1]) if re.fullmatch("1:[0-9]+", v.scale) else 5
        # 축척 기준값: DXF 는 시트 pt/mm 를 정확히 알므로 그대로, PDF 는 인쇄 축소를 감안한 실측 상수
        k0 = (scale_n / self.pt_per_mm) if self.pt_per_mm else 0.7317 * scale_n
        radii = sorted({round(s[1], 1) for s in par if abs(s[0] - s[2]) > 5})
        ks = []
        for t, up, _, _ in (v.words or []):
            mm = re.fullmatch(r"Ø(\d+(?:\.\d+)?)", t)
            if not mm:
                continue
            dia = float(mm.group(1))
            for r in radii:
                if r > 1 and abs(2 * r * k0 - dia) < 0.03 * dia:      # 인쇄 축척 기본값과 3% 이내인 쌍만 (오매칭 방지)
                    ks.append(dia / (2 * r))
        k = sorted(ks)[len(ks) // 2] if ks else k0
        # 플랜지: 호칭 사이즈의 표준 외경 (없으면 가장 큰 수직선)
        size_m = re.search(r'(\d+(?:\s\d/\d)?)"', v.name)
        r_f_exp = None
        if size_m:
            from vessel_spec import NozzleSpec
            od_std = NozzleSpec(mark="", size_in=parse_size_inch(size_m.group(1) + '"') or 0.0).standard_flange_od()
            if od_std:
                r_f_exp = od_std / 2 / k
        if r_f_exp:
            cands = [s for s in perp if abs(max(s[1], s[3]) - r_f_exp) < 0.12 * r_f_exp]
            # 플랜지 윤곽은 축 위아래 양쪽에 그려진다. 한쪽에만 있는 선(맨홀의 데빗 브래킷 등)은 허용오차 안에
            # 들어와도 플랜지가 아니다 (DXF 의 N8 은 한쪽만 있는 Ø959.9 가 실제 플랜지 Ø867.1 을 가렸음)
            sides: Dict[float, set] = {}
            for s in cands:
                w = uv(s[4])[1] + uv(s[5])[1]
                sides.setdefault(round(max(s[1], s[3]), 1), set()).add(0 if abs(w) < 1e-6 else (1 if w > 0 else -1))
            both = {r for r, sd in sides.items() if len(sd) > 1 or 0 in sd}
            fl = [s for s in cands if round(max(s[1], s[3]), 1) in both] or cands
            if not fl:
                return self._prof_fail(v, f"표준 플랜지 반경 {r_f_exp * k:.0f}mm 과 맞는 수직선 없음 "
                                          f"(있는 반경 {sorted({round(max(s[1], s[3]) * k) for s in perp})[-6:]})")
        else:
            r_top = max(max(s[1], s[3]) for s in perp)
            fl = [s for s in perp if max(s[1], s[3]) >= 0.9 * r_top]
        r_fl = max(max(s[1], s[3]) for s in fl)
        roots = {find(segs.index(s)) for s in fl}
        comp = [s for i, s in enumerate(segs) if find(i) in roots]
        if len(comp) < 4:
            return self._prof_fail(v, f"플랜지에 이어진 선분 {len(comp)}개")
        u_lo = min(min(s[0], s[2]) for s in comp)
        u_hi = max(max(s[0], s[2]) for s in comp)
        span = u_hi - u_lo
        if span < 20:
            return self._prof_fail(v, f"축 방향 길이 {span:.0f}pt")
        u_fl = sum(s[0] for s in fl) / len(fl)
        face_low = abs(u_fl - u_lo) < abs(u_fl - u_hi)
        u_face = u_lo if face_low else u_hi
        sgn = 1.0 if face_low else -1.0
        long_par = [s for s in par if abs(s[0] - s[2]) >= 0.6 * span and find(segs.index(s)) in roots]
        r_bore = min(s[1] for s in long_par) if long_par else None

        def s_of(u):
            return (u - u_face) * sgn

        cover = []
        for u1, r1, u2, r2, _, _ in comp:
            s1, s2 = s_of(u1), s_of(u2)
            if abs(s1 - s2) < 0.05:
                continue
            if s1 > s2:
                s1, s2, r1, r2 = s2, s1, r2, r1
            cover.append((s1, r1, s2, r2))
        if not cover:
            return self._prof_fail(v, "구간 없음")
        brk = sorted({round(s, 2) for c in cover for s in (c[0], c[2])})

        def r_at(s):
            best = 0.0
            for s1, r1, s2, r2 in cover:
                if s1 - 0.05 <= s <= s2 + 0.05:
                    t = 0.0 if s2 == s1 else (s - s1) / (s2 - s1)
                    best = max(best, r1 + (r2 - r1) * max(0.0, min(1.0, t)))
            return best

        pts = []
        for i in range(len(brk) - 1):
            s1, s2 = brk[i], brk[i + 1]
            eps = min(0.05, (s2 - s1) / 4)
            for s, r in ((s1, r_at(s1 + eps)), (s2, r_at(s2 - eps))):
                if not pts or abs(pts[-1][0] - s) > 0.01 or abs(pts[-1][1] - r) > 0.01:
                    pts.append((s, r))
        # 플랜지 반경을 넘는 곳(서포트 플레이트, 데빗 등)부터는 노즐 몸체가 아님 -> 잘라냄
        cut = None
        for i, (s, r) in enumerate(pts):
            if r > r_fl * 1.08 and s > 2:
                cut = i
                break
        if cut is not None:
            pts = pts[:cut]
        # 플랜지 면 앞에 붙여 그린 것(블라인드 플랜지 등)은 노즐 몸체가 아니다: 보어 선이 시작되는 곳이 실제 면.
        # DXF 는 블라인드가 노즐 윤곽에 이어져 있어 면 앞쪽까지 실루엣에 들어온다 (PDF 는 따로 떨어져 있었음)
        if long_par:
            s_bore = min(min(s_of(sg[0]), s_of(sg[2])) for sg in long_par)
            if s_bore > 2:
                pts = [(s - s_bore, r) for s, r in pts if s >= s_bore - 0.05]
                _s_of_face = s_of
                s_of = lambda u, _f=_s_of_face, _d=s_bore: _f(u) - _d      # 지지판 위치도 같은 기준으로
        # 물체선이 풍선/치수 문자에 가려 잠깐 끊긴 곳은 실루엣이 움푹 패인다 -> 짧은 구간(40mm)에서 같은 반경으로
        # 돌아오면 패임을 앞 반경으로 메운다
        i = 1
        while i < len(pts) - 1:
            s_i, r_i = pts[i]
            r_prev = pts[i - 1][1]
            if r_i < r_prev - 0.5:
                j = next((j for j in range(i + 1, len(pts)) if abs(pts[j][1] - r_prev) < 0.5), None)
                # 보어 반경까지 내려가는 패임은 바깥 실루엣일 수 없으니(관통 구멍) 길이와 무관하게 메운다
                to_bore = r_bore is not None and r_i <= r_bore + 1.0
                if j is not None and ((pts[j][0] - s_i) * k < 40 or to_bore):
                    for t in range(i, j):
                        pts[t] = (pts[t][0], r_prev)
                    i = j
                    continue
            i += 1
        # 끝점이 보어 반경으로 떨어지는 것(쉘 내면 모서리의 필릿)은 잘라낸다
        while len(pts) > 3 and r_bore is not None and pts[-1][1] <= r_bore + 1.0:
            pts.pop()
        if len(pts) < 3:
            return self._prof_fail(v, f"실루엣 점 {len(pts)}개")
        prof = [[round(s * k, 1), round(r * k, 1)] for s, r in pts]
        slim = []
        for p in prof:
            if len(slim) >= 2 and abs(slim[-1][1] - p[1]) < 0.05 and abs(slim[-2][1] - p[1]) < 0.05:
                slim[-1] = p
            else:
                slim.append(p)
        if len(slim) < 3 or r_fl * k < 20:
            return self._prof_fail(v, f"실루엣 점 {len(slim)}개, 플랜지 반경 {r_fl * k:.0f}mm")
        # 지지판은 확장 영역(v.region: 목에 닿는 판, 쉘 판재 포함) 의 선으로 찾는다
        rx0, ry0, rx1, ry1 = v.region or v.bbox

        def in_region(p):
            return rx0 - m <= p[0] <= rx1 + m and ry0 - m <= p[1] <= ry1 + m

        def to_ur(p1, p2):
            u1, w1 = uv(p1)
            u2, w2 = uv(p2)
            if w1 * w2 < 0:
                return [u1, 0.0, u2, max(abs(w1), abs(w2))]
            return [u1, abs(w1), u2, abs(w2)]

        thick_r = [to_ur(l["p1"], l["p2"]) for l in self.raw_lines
                   if l["lw"] > thin * 1.5 + 1e-6 and in_region(l["p1"]) and in_region(l["p2"])]
        thin_r = [to_ur(l["p1"], l["p2"]) for l in self.raw_lines
                  if l["lw"] <= thin * 1.5 + 1e-6 and in_region(l["p1"]) and in_region(l["p2"])]
        supports = self._nozzle_supports(v, thick_r, thin_r, s_of, k, r_fl, uv)
        return {
            "supports": supports,
            "points": slim, "bore_d": round(2 * r_bore * k, 1) if r_bore else None,
            "flange_od": round(2 * r_fl * k, 1), "length": round(slim[-1][0], 1),
            "mm_per_pt": round(k, 4), "calibrated": bool(ks),
            "axis": "vertical" if vertical else ("horizontal" if ax_deg < 1 or ax_deg > 179 else f"{ax_deg:g}deg"),
            "axis_deg": round(ax_deg, 1),
            "face_end": ("top" if face_low else "bottom") if vertical else ("left" if face_low else "right"),
            "truncated": cut is not None,
        }

    @staticmethod
    def _nozzle_supports(v: View, thick, thin, s_of, k: float, r_fl: float, uv) -> Optional[dict]:
        """노즐 지지판(거싯). 확장 영역의 선(축 기준 u, r)으로 찾는다.
        - 문구 'n-SUP'T PLATES (tXxWY)': 개수/두께/폭. 없으면 0 (부품표 SUPPORT PLATE 로 보완)
        - 굵은 사선 중 목 표면에서 시작해 쉘 쪽으로 갈수록 반경이 커지는 것 = 판의 긴 변 (허브 테이퍼는 반대 방향,
          쉘 판재 사선은 축을 가로지르거나 목에서 시작하지 않음). 가장 긴 것이 면 쪽 변 -> 각도, 붙는 위치, 길이
        - 붙는 위치는 그려진 위치가 아니라 플랜지 면~붙는 점 사이에 그려진 치수 문자(150, 295 ...) 를 우선한다.
          목 중간에 파단선이 있으면 그려진 거리가 실제와 다르기 때문(N7: 그림 216 vs 치수 300)
        결과: n, t, w, angle_deg(축 기준), s_attach(플랜지 면부터 mm), s_attach_src('dim'|'drawn'), length, r_from, r_to"""
        words = v.region_words or v.words or []
        texts = " ".join(w[0] for w in words)
        m_n = re.search(r"(\d)\s*-\s*SUP'?T\.?\s*PLATES?", texts, re.I)
        m_tw = re.search(r"\(\s*t(\d+(?:\.\d+)?)\s*[xX×]\s*W(\d+(?:\.\d+)?)\s*\)", texts)
        # 목 반경 후보: 축과 평행한 굵은 선의 반경 (플랜지보다 작은 것)
        necks = sorted({round(sg[1], 1) for sg in thick if abs(sg[1] - sg[3]) < 0.3 and abs(sg[0] - sg[2]) > 3
                        and 1 < sg[1] < r_fl * 0.95})
        obl = []
        for u1, r1, u2, r2 in thick:
            s1, s2 = s_of(u1), s_of(u2)
            ds, dr = abs(s2 - s1), abs(r2 - r1)
            if ds <= 8 or dr <= 8:
                continue
            (s_lo, r_lo), (s_hi, r_hi) = sorted(((s1, r1), (s2, r2)))
            if r_hi <= r_lo:                                     # 쉘 쪽으로 갈수록 반경이 커져야 판
                continue
            if not any(abs(r_lo - rn) < 2.5 for rn in necks):    # 목 표면에서 시작
                continue
            obl.append((s_lo, s_hi, r_lo, r_hi, ds, dr))
        # 판은 폭 W 만큼 떨어진 평행한 두 긴 변으로 그려진다. 짝이 없는 사선(단조 노즐의 원뿔 허브, 용접 개선)은 제외.
        # 허브 원뿔은 축 양쪽 선이 (s, r) 에서 겹치므로 축 방향으로 3pt 넘게 떨어진 짝만 인정. 폭을 알면 w/sin(a) 와 비교
        w_known = float(m_tw.group(2)) if m_tw else 0.0

        def paired(o) -> bool:
            a_o = math.atan2(o[5], o[4])
            for p_ in obl:
                if p_ is o or abs(math.atan2(p_[5], p_[4]) - a_o) > math.radians(3) or abs(p_[0] - o[0]) <= 3:
                    continue
                if w_known > 0 and k > 0 and math.sin(a_o) > 1e-3:
                    sep = abs(p_[0] - o[0]) * k
                    if not (0.5 <= sep / (w_known / math.sin(a_o)) <= 2.0):
                        continue
                return True
            return False

        obl = [o for o in obl if paired(o)]
        if not obl and not (m_n or m_tw):
            return None
        out = {"n": int(m_n.group(1)) if m_n else 0,
               "t": float(m_tw.group(1)) if m_tw else 0.0,
               "w": float(m_tw.group(2)) if m_tw else 0.0,
               "angle_deg": 0.0, "s_attach": 0.0, "s_attach_src": "", "length": 0.0, "r_from": 0.0, "r_to": 0.0}
        if not obl:
            return out
        s_lo, s_hi, r_lo, r_hi, ds, dr = max(obl, key=lambda o: math.hypot(o[4], o[5]))
        angle = math.degrees(math.atan2(dr, ds))          # 판과 노즐 축 사이 각도
        s_attach, src = s_lo * k, "drawn"
        # 플랜지 면(s=0)과 붙는 점(s_lo) 사이에 걸친 축 방향 치수선 -> 그 중앙 근처의 숫자
        for u1, r1, u2, r2 in thin:
            if abs(r1 - r2) > 0.3 or abs(u1 - u2) < 3:
                continue
            a, b = sorted((s_of(u1), s_of(u2)))
            if abs(a) > 4 or abs(b - s_lo) > 4:
                continue
            mid_u = (u1 + u2) / 2
            best = None
            for t, up, x, y in words:
                mm = re.fullmatch(r"(\d+(?:\.\d+)?)", t)
                if not mm:
                    continue
                wu, ww = uv((x, y))
                val = float(mm.group(1))
                # 치수선 가운데(축 방향 14pt) + 치수선 옆(반경 방향 12pt) 에 있고, 그려진 위치의 0.8~2.5배인 값만 (파단선 보정 범위)
                if abs(wu - mid_u) > 14 or abs(abs(ww) - r1) > 12 or not (0.8 * s_lo * k <= val <= 2.5 * s_lo * k):
                    continue
                d = abs(wu - mid_u)
                if best is None or d < best[0]:
                    best = (d, val)
            if best:
                s_attach, src = best[1], "dim"
                break
        out.update({"angle_deg": round(angle, 1), "s_attach": round(s_attach, 1), "s_attach_src": src,
                    "length": round(math.hypot(ds, dr) * k, 1), "r_from": round(r_lo * k, 1), "r_to": round(r_hi * k, 1)})
        return out

    # ---- 라벨 치수 ---------------------------------------------------------
    def _dims(self, info: DrawingInfo) -> None:
        titled = [v for v in info.views if v.bbox]
        for w in self.words:
            m = re.search(r"(" + NUMBER + r")\s*TO\s*(C\.?L|T\.?L)\b", w.text, re.I)
            if not m:
                continue
            ref = "C.L" if m.group(2).upper().startswith("C") else "T.L"
            d = Dim(text=w.text.strip(), value=_num(m.group(1)), ref=ref, x=round(w.xc, 1), y=round(w.yc, 1),
                    upright=w.upright)
            # 뷰 영역 안에 있으면 그 뷰, 없으면 치수 아래쪽에 있는 가장 가까운 뷰 제목
            inside = [v for v in titled if v.bbox[0] - 8 <= w.xc <= v.bbox[2] + 8 and v.bbox[1] - 8 <= w.yc <= v.bbox[3] + 8]
            if inside:
                v = min(inside, key=lambda v: (v.bbox[2] - v.bbox[0]) * (v.bbox[3] - v.bbox[1]))
            else:
                below = [v for v in info.views if v.y > w.yc - 5]
                v = min(below, key=lambda v: _dist((v.x, v.y), (w.xc, w.yc))) if below else None
            if v:
                d.view = v.name
                v.dims.append(d.text)
            info.dims.append(d)

    # ---- 부품표 ------------------------------------------------------------
    def _bom(self, info: DrawingInfo) -> None:
        W, H = self.W, self.H
        item = self._label(r"ITEM", lambda w: w.x0 > W * 0.6 and w.top < H * 0.3)
        if not item:
            return
        # 헤더: MATERIAL / REMARK 는 두 줄 헤더의 위 칸에 있어 ITEM 행보다 4pt 위 -> ±7pt 허용
        hdr_names = ["ITEM", "NAME OF PART", "MATERIAL", "Q'TY", "SP1", "SP2", "UNIT", "REMARK"]
        cols: List[Tuple[str, float]] = []
        for name in hdr_names:
            for w in self.words:
                if w.upright and abs(w.top - item.top) < 7 and w.text.strip().upper() == name:
                    cols.append((name, w.x0))
                    break
        if len(cols) < 4:
            return
        cols.sort(key=lambda c: c[1])
        mark_col = [w for w in self.words if w.upright and w.x0 < item.x0 - 3 and w.x0 > item.x0 - 60
                    and w.top > item.top and re.fullmatch(MARK_GROUP, w.text.strip())]
        item_words = sorted([w for w in self.words if w.upright and abs(w.x0 - item.x0) < 9
                             and w.top > item.top + 6 and re.fullmatch(r"\d{1,3}", w.text.strip())],
                            key=lambda w: w.top)
        rows: List[Word] = []
        for w in item_words:
            if rows and w.top - rows[-1].top > 30:
                # 노즐 상세도: 마크만 있는 빈 행(N4B~N4E)이 사이에 있으면 같은 표가 이어지는 것
                if not any(rows[-1].top < m.top < w.top for m in mark_col) or w.top - rows[-1].top > 90:
                    break
            rows.append(w)
        if not rows:
            return
        # 열 경계: 헤더 행을 지나는 표의 세로선 x (헤더 문자는 가운데 정렬이라 문자 위치로는 경계를 못 잡음)
        xs = sorted({round(a[0], 1) for a, b in self.dp.segs
                     if abs(a[0] - b[0]) < 0.6 and min(a[1], b[1]) <= item.top + 2 and max(a[1], b[1]) >= item.top + 8
                     and item.x0 - 80 < a[0] < W})
        bounds: List[float] = []
        for x in xs:
            if not bounds or x - bounds[-1] > 3:
                bounds.append(x)

        def interval_of(x: float) -> int:
            for i in range(len(bounds) - 1):
                if bounds[i] <= x < bounds[i + 1]:
                    return i
            return -1

        col_iv = {name: interval_of((cx + 4)) for name, cx in cols}
        iv_col = {iv: name for name, iv in col_iv.items() if iv >= 0}
        item_iv = col_iv.get("ITEM", -1)
        use_grid = len(bounds) >= 4 and item_iv >= 0

        def col_of(w: Word) -> str:
            if use_grid:
                return iv_col.get(interval_of(w.xc), "")
            best = None
            for name, cx in cols:
                if w.x0 + 6 >= cx and (best is None or cx > best[1]):
                    best = (name, cx)
            return best[0] if best else ""

        # 블록: 행 간격이 13pt 넘게 벌어지면 새 블록 (노즐 상세도의 마크 그룹 구분)
        blocks: List[List[Word]] = []
        for w in rows:
            if blocks and w.top - blocks[-1][-1].top <= 13:
                blocks[-1].append(w)
            else:
                blocks.append([w])
        table_end = max([rows[-1].top + 30] + [w.top + 5 for w in mark_col])

        for bi, block in enumerate(blocks):
            y_lo = block[0].top - 6
            y_hi = (blocks[bi + 1][0].top - 3) if bi + 1 < len(blocks) else table_end
            bmarks: List[str] = []
            for w in mark_col:
                if y_lo <= w.top < y_hi:
                    bmarks += expand_marks(w.text.strip())
            for rw in block:
                cells: Dict[str, List[Word]] = {}
                row_words = [w for w in self.words
                             if w.upright and abs(w.top - rw.top) <= 3.5 and item.x0 - 1 <= w.x0 <= W - 8]
                alnum = [w for w in row_words if re.search(r"[A-Za-z0-9]", w.text) or "Ø" in w.text]
                for w in row_words:
                    if w not in alnum:
                        # 기호만 있는 조각('"', '.')은 글자 단위로 쪼개진 문구의 일부일 때만 (이웃 글자에 1.5pt 이내) 유지
                        if not any(o is not w and abs(o.top - w.top) < 2.5 and (-1.5 <= w.x0 - o.x1 <= 1.5 or -1.5 <= o.x0 - w.x1 <= 1.5)
                                   for o in alnum):
                            continue
                    cells.setdefault(col_of(w), []).append(w)
                p = Part(item=rw.text.strip(),
                         name=_cell_text(cells.get("NAME OF PART", [])),
                         material=_cell_text(cells.get("MATERIAL", [])),
                         qty=" ".join(_cell_text(cells.get(c, [])) for c in ("Q'TY", "SP1", "SP2") if cells.get(c)),
                         unit=_cell_text(cells.get("UNIT", [])),
                         remark=_cell_text(cells.get("REMARK", [])),
                         marks=list(bmarks))
                if p.name:
                    info.parts.append(p)
        for p in info.parts:
            for mk in p.marks:
                info.mark_views.setdefault(mk, [])
        self.log.append(f"부품표 {len(info.parts)} 행, 블록 {len(blocks)} 개")

    # ---- 마크 / 용접선 / 표식 / 노트 / 참조 ----------------------------------
    def _marks_and_labels(self, info: DrawingInfo) -> None:
        marks: set = set()
        for w in self.words:
            t = w.text.strip()
            if re.fullmatch(MARK_GROUP, t):
                marks.update(expand_marks(t))
            m = re.match(r"(N\d+[A-Z]?)/\d", t)          # 전개도 'N4/8"'
            if m:
                marks.add(m.group(1))
        marks.update(info.mark_views.keys())
        info.marks = sorted(marks, key=_mark_key)

        # 용접선: L.W.L-1, C.W.L. 2, H.W.L-1~10, L.W.L-1,3,5 ...
        seams: set = set()
        for w in self.words:
            for m in re.finditer(r"\b([LCH])\.W\.L\.?\s*[-.]?\s*([\d,~\s]+)", w.text):
                kind = m.group(1)
                body = m.group(2).strip()
                for part in re.split(r"\s*,\s*", body):
                    r = re.fullmatch(r"(\d+)\s*~\s*(\d+)", part.strip())
                    if r:
                        for i in range(int(r.group(1)), int(r.group(2)) + 1):
                            seams.add(f"{kind}.W.L-{i}")
                    elif re.fullmatch(r"\d+", part.strip()):
                        seams.add(f"{kind}.W.L-{int(part)}")
            m = re.search(r"\b([LCH])\.W\.L\.?\s*[-.]?\s*(\d+)\s*\((\d+)°\)", w.text)
            if m:
                info.seam_angles[f"{m.group(1)}.W.L-{int(m.group(2))}"] = float(m.group(3))
        info.seams = sorted(seams, key=lambda s: (s[0], int(s.split("-")[1])))

        # 단면/뷰 표식과 제목
        markers: set = set()
        covered: set = set()
        for w in self.words:
            t = w.text.strip()
            m = re.fullmatch(r'"\s*([A-Z]\d?|W\d)\s*"', t)
            if m:
                markers.add(m.group(1))
            for m in re.finditer(r'(?:VIEW|SECTION|DETAIL)\s*"\s*([A-Z]\d?|W\d)\s*"', t):
                covered.add(m.group(1))
        for v in info.views:
            for m in re.finditer(r'"\s*([A-Z]\d?|W\d)\s*"', v.name):
                covered.add(m.group(1))
        info.markers = sorted(markers)
        info.side_labels = [[w.text.strip(), round(w.xc, 1), round(w.yc, 1)] for w in self.words
                            if w.upright and re.search(r"\b(FIXED|SLIDING)\s+SIDE\b", w.text, re.I)]
        info.unresolved_markers = sorted(markers - covered)

        # 노트
        anchor = self._label(r"NOTES?:?", lambda w: w.x0 > self.W * 0.68)
        if anchor:
            for row in self.rows:
                rw = [w for w in row if w.x0 > self.W * 0.7]
                if not rw or rw[0].top <= anchor.top + 2:
                    continue
                if rw[0].top > self.H * 0.72:
                    break
                text = self._row_text(rw)
                if re.match(r"^(ASME|VENDOR DATA|MFR\.|WHC WORK|REV\.?\s|OWNER)", text):
                    break
                if re.match(r"^\d{1,2}\.\d?\s*\S", text):
                    info.notes.append(text)
                elif info.notes and rw[0].x0 > anchor.x0 - 2:
                    info.notes[-1] += " " + text
        # SEE DWG. / SEE NOTE 참조 (제목란의 SCALE 'SEE DWG.' 제외)
        for w in self.words:
            if re.search(r"SEE\s+(DWG|NOTE)", w.text, re.I) and not (w.x0 > self.W * 0.75 and w.top > self.H * 0.8):
                row = [u for u in self.words if u.upright == w.upright and abs(u.top - w.top) < 4 and abs(u.xc - w.xc) < 260]
                info.refs.append(self._row_text(sorted(row, key=lambda u: u.x0)))

    # ---- GA 노즐 리스트: 투영이 SEE DWG. 인 마크 ---------------------------
    def _see_dwg_marks(self, info: DrawingInfo) -> None:
        if "nozzle_list" not in self.dp.kinds:
            return
        hdr = self.dp._find(r"^MARK$", upright=True)
        if not hdr:
            return
        for row in self.rows:
            mk = [w for w in row if abs(w.xc - hdr.xc) < 25 and re.fullmatch(MARK_GROUP, w.text.strip())]
            if not mk or not any(re.search(r"SEE\s+DWG", w.text, re.I) for w in row):
                continue
            info.see_dwg_marks += expand_marks(mk[0].text.strip())

    # ---- 전개도: 마크별 T.L+ ------------------------------------------------
    def _tl_labels(self, info: DrawingInfo) -> None:
        tl = []
        for w in self.words:
            m = re.search(r"T\.L\s*\+\s*(\d+)", w.text)
            if m:
                tl.append((w, float(m.group(1))))
        if len(tl) < 5:
            return
        for w in self.words:
            m = re.match(r"(N\d+[A-Z]?)/", w.text.strip())
            if not m:
                continue
            mark = m.group(1)
            # 전개도의 회전 문자는 'T.L+ nnnn' 줄이 마크 줄 바로 왼쪽(x 작은 쪽)에 붙는다.
            # 한 단어로 합쳐진 'N7/1"T.L+ 23470' 의 T.L+ 는 옆 마크(N6E) 것일 수 있어 왼쪽 라벨을 우선한다.
            near = [(t, v) for t, v in tl if t.upright == w.upright and t is not w
                    and -13 < t.xc - w.xc < 0 and abs(t.yc - w.yc) < 45]
            if near:
                t, v = min(near, key=lambda tv: _dist((tv[0].xc, tv[0].yc), (w.xc, w.yc)))
                info.tl_labels[mark] = v
                continue
            m2 = re.search(r"T\.L\s*\+\s*(\d+)", w.text)
            if m2:
                info.tl_labels[mark] = float(m2.group(1))
        if info.tl_labels:
            self.log.append(f"T.L+ 라벨 {len(tl)} 개, 마크 매칭 {len(info.tl_labels)} 개")

    # ---- 사실(fact) ---------------------------------------------------------
    def _facts(self, info: DrawingInfo) -> None:
        facts: List[Fact] = []
        seen: set = set()

        def add(key, value, unit="", text=""):
            k = (key, str(value))
            if k in seen:
                return
            seen.add(k)
            facts.append(Fact(key, value, unit, text[:80]))

        ft = self.full_text
        # 용기 기본 치수
        ods = [(_num(m.group(1)), w.text) for w in self.words for m in [re.search(r"\bO\.?D\.?\s*(" + NUMBER + ")", w.text)] if m]
        big = [o for o in ods if o[0] >= 1000]
        if big:
            v = max(big, key=lambda o: o[0])
            add("outer_diameter", v[0], "mm", v[1])
        ids = [(w, _num(m.group(1))) for w in self.words for m in [re.search(r"\bI\.?D\.?\s*(" + NUMBER + ")", w.text)] if m]
        big = [o for o in ids if o[1] >= 1000]
        if big:
            w, v = max(big, key=lambda o: o[1])
            add("inner_diameter", v, "mm", w.text)
            cands = [c for c in self.words if c.upright == w.upright and re.fullmatch(r"t\d+(?:\.\d+)?", c.text)]
            t = self.dp._nearest(w, cands, 120)
            if t:
                add("shell_thickness", _num(t.text[1:]), "mm", f"{w.text} / {t.text}")
        m = re.search(r"(" + NUMBER + r")\s*\(T\.L\s*TO\s*T\.L\)", ft, re.I)
        if m:
            add("tl_length", _num(m.group(1)), "mm", m.group(0))
        for w in self.words:
            m = re.search(r"(" + NUMBER + r")\s*\(SADDLE TO SADDLE", w.text, re.I)
            if m:
                add("saddle_spacing", _num(m.group(1)), "mm", w.text)
                row = [u for u in self.words if u.upright == w.upright and abs(u.top - w.top) < 4.5 and re.fullmatch(NUMBER, u.text.strip())]
                left = [u for u in row if u.x1 <= w.x0 + 1]
                right = [u for u in row if u.x0 >= w.x1 - 1]
                if left:
                    add("saddle_offset", _num(left[-1].text), "mm", f"{left[-1].text} | {w.text}")
                elif right:
                    add("saddle_offset", _num(right[0].text), "mm", f"{w.text} | {right[0].text}")
                break
        if re.search(r"HEMI\.?\s*HEAD", ft, re.I):
            add("head_type", "hemispherical", "", "HEMI. HEADS")
        elif re.search(r"2\s*:\s*1\s*(?:SEMI[- ]?)?ELLIP|ELLIPSOIDAL|ELLIP\.?\s*HEAD", ft, re.I):
            add("head_type", "2:1 ellipsoidal", "", "")
        m = re.search(r"USED\s+TH'?K\.?\s*:?\s*(" + NUMBER + ")", ft, re.I)
        if m:
            add("head_thickness", _num(m.group(1)), "mm", m.group(0))
        m = re.search(r"MIN\.?\s+TH'?K\.?\s*:?\s*(" + NUMBER + ")", ft, re.I)
        if m:
            add("head_min_thickness", _num(m.group(1)), "mm", m.group(0))
        base_r = (info.fact("inner_diameter") or 0) / 2
        if info.fact("head_type"):      # 경판 표기가 있는 시트에서만 (노즐 상세도의 R1829 는 쉘 내반경)
            for w in self.words:
                m = re.fullmatch(r"R\s?(\d{4})", w.text.strip())
                if m and (base_r == 0 or abs(float(m.group(1)) - base_r) < 100) and float(m.group(1)) > 500:
                    add("head_radius", float(m.group(1)), "mm", w.text)
                    break
        m = re.search(r"INSULATION\s+TH'?K\.?\s*:?\s*(\d+)\s*mm", ft, re.I) or re.search(r"/(\d+)\s*mm/", ft)
        if m:
            add("insulation_thickness", float(m.group(1)), "mm", m.group(0))
        else:
            lab = self._label(r"INSULATION TH'?K\.?", None)
            if lab:
                b = [w for w in self._below(lab, 4, 16, 50) if re.fullmatch(r"(\d+)\s*mm", w.text)]
                if b:
                    add("insulation_thickness", float(re.match(r"\d+", b[0].text).group(0)), "mm", f"{lab.text} {b[0].text}")
        m = re.search(r"(" + NUMBER + r")\s*\+\s*(" + NUMBER + r")\s*=\s*(" + NUMBER + r")\s*\(APPROX", ft, re.I)
        if m:
            add("overall_length", _num(m.group(3)), "mm", m.group(0))
        m = re.search(r"(" + NUMBER + r")\s*\(FOR\s+(?:FIXED|SLIDING)\s+SIDE\)", ft, re.I)
        if m:
            add("centerline_height", _num(m.group(1)), "mm", m.group(0))
        if info.kind == "body_support":
            for d in info.dims:
                if d.ref == "C.L" and 1500 < d.value < 3500:
                    add("centerline_height", d.value, "mm", d.text)
        bcs = sorted({_num(m.group(1)) for w in self.words for m in [re.match(r"B\.C\.?\s*(" + NUMBER + ")", w.text)] if m})
        if bcs:
            add("saddle_bolt_circle", bcs, "mm", "B.C")
        els = sorted({float(m.group(1)) for w in self.words for m in [re.search(r"EL\.\s*\+?\s*(\d{3,5})", w.text)] if m})
        if els:
            add("platform_elevations", els, "mm", "EL.")

        # 중량
        m = re.search(r"LIFTING WEIGHT\s*:\s*(" + NUMBER + r")\s*kg", ft, re.I)
        if m:
            add("weight_lifting", _num(m.group(1)), "kg", m.group(0))
        for row in self.rows:
            text = self._row_text(row, " | ")
            if not re.search(r"WEIGHT", text, re.I):
                continue
            m = re.search(r"WEIGHT\s*\(EMPTY\s*/\s*F\.?W\.?\)\s*\|?\s*(" + NUMBER + r")\s*/\s*(" + NUMBER + ")", text, re.I)
            if m:
                add("weight_empty", _num(m.group(1)), "kg", m.group(0))
                add("weight_full_of_water", _num(m.group(2)), "kg", m.group(0))
                continue
            for m in re.finditer(r"([A-Z][A-Z .()/&-]*?)\s*\|\s*\[?K?G?\]?\s*\|?\s*(\d{1,3}(?:,\d{3})+)", text):
                label = m.group(1).strip()
                if re.fullmatch(r"WEIGHT", label, re.I):
                    continue
                label = re.sub(r"\s*WEIGHT\s*", "", label, flags=re.I).strip(" .()")
                slug = re.sub(r"[^A-Z]+", "_", label.upper()).strip("_").lower()
                slug = {"full_of_water": "full_of_water", "f_w": "full_of_water"}.get(slug, slug)
                if slug:
                    add(f"weight_{slug}", _num(m.group(2)), "kg", m.group(0))
            # 값이 다음 행에 있는 2줄 라벨 (NET WEIGHT / (DESALTER AND INTERNALS ONLY) / 146,200)
            m = re.match(r"(NET|GROSS|SHIPPING SADDLE)\s+WEIGHT\s*\|\s*\[KG\]\s*$", text, re.I)
            if m:
                lab = row[0]
                nxt = [w for w in self.words if w.upright and 4 < w.top - lab.top < 16 and w.x0 > lab.x1 and re.fullmatch(r"\d{1,3}(?:,\d{3})+", w.text)]
                if nxt:
                    add("weight_" + m.group(1).lower().replace(" ", "_"), _num(nxt[0].text), "kg", f"{text} -> {nxt[0].text}")
        lab = self._label(r"DIMENSION", lambda w: w.x0 > self.W * 0.68)
        if lab:
            nxt = [w for w in self.words if w.upright and 4 < w.top - lab.top < 20 and w.x0 > lab.x1 and re.fullmatch(r"\d{3,5}", w.text)]
            nxt.sort(key=lambda w: w.x0)
            if len(nxt) >= 3:
                add("shipping_dimension_lwh", [float(w.text) for w in nxt[:3]], "mm", "DIMENSION [MM] L W H")

        # 설계 데이터 (GA 데이터 시트 / 명판)
        design = [
            ("design_pressure", r"DESIGN\s+PRESS", r"([\d.]+\s*\([\d.]+\)\s*/\s*(?:F\.?V|[\d.]+(?:\([\d.]+\))?))"),
            ("design_temperature", r"DESIGN\s+TEMP", r"(-?\d+(?:\.\d+)?\s*/\s*-?\d+(?:\.\d+)?)"),
            ("operating_pressure", r"OPER\.?\s+PRESS", r"(\d[\d.~]*\s*\([\d.~]+\))"),
            ("operating_temperature", r"OPER\.?\s+TEMP", r"(\d[\d.~]*)"),
            ("mawp", r"M\.A\.W\.P", r"([\d.]+\s*\([\d.]+\))"),
            ("hydro_test_pressure", r"HYDRO\.?\s+TEST", r"([\d.]+\s*\([\d.]+\))"),
            ("corrosion_allowance", r"PRESSURE PART|CORR\.?\s*ALLOW", r"(\d+(?:\.\d+)?)\s*\|?\s*mm"),
            ("volume", r"VOLUME", r"([\d.]+)\s*\|?\s*m"),
            ("radiography", r"RADIOGRAPHY", r"((?:FULL|SPOT|NONE)\s*/\s*(?:FULL|SPOT|NONE))"),
            ("material_shell_head", r"MATERIAL\s*\(SHELL", r"([A-Z]+\d+-\d+N?\s*/\s*[A-Z]+\d+-\d+N?)"),
            ("thickness_shell_head", r"THICKNESS\s*\(SHELL", r"(t?\d+\s*/\s*t?\d+)"),
            ("fluid", r"FLUID\s+NAME", r"\|\s*([A-Z][A-Z /]+)"),
            ("code", r"(?:CONSTRUCTION\s+CODE|CODE\s*&\s*SPEC)", r"\|\s*(ASME[^|]+)"),
            ("pwht", r"P\.W\.H\.T", r"\|\s*(YES|NO)"),
            ("design_life", r"DESIGN\s+LIFE", r"(\d+\s*YEARS?)"),
        ]
        for key, lab_re, val_re in design:
            for i, row in enumerate(self.rows):
                text = self._row_text(row, " | ")
                m = re.search(lab_re, text, re.I)
                if not m:
                    continue
                rest = text[m.end():]
                v = re.search(val_re, rest, re.I)
                if not v and i + 1 < len(self.rows):
                    v = re.search(val_re, self._row_text(self.rows[i + 1], " | "), re.I)
                if v:
                    val = re.sub(r"\s+", " ", v.group(1)).strip()
                    add(key, val, "", text)
                    break

        # 쉘 코스 폭 (몸체 상세 / 전개도 상단의 4자리 숫자 행)
        if info.kind in ("body_support", "development"):
            for row in self.rows:
                nums = [w for w in row if re.fullmatch(r"\d{4}", w.text.strip())]
                if len(nums) >= 5 and row[0].top < self.H * 0.15:
                    widths = [float(w.text) for w in nums]
                    add("course_widths", widths, "mm", "상단 코스 폭")
                    add("course_total", sum(widths), "mm", "코스 폭 합계")
                    break
        if info.kind == "ga" and "nozzle_list" in self.dp.kinds:
            spec = self.dp.parse()
            add("nozzle_count", len(spec.nozzles), "", "NOZZLE LIST 마크 전개")
        if info.seams:
            add("seam_count", len(info.seams), "", ", ".join(info.seams[:6]) + " ...")
        info.facts = facts

    # ---- 실행 ---------------------------------------------------------------
    def run(self) -> DrawingInfo:
        info = DrawingInfo(file=os.path.basename(self.path), path=self.path)
        info.n_words, info.n_lines, info.n_curves, info.n_images = len(self.words), self.n_lines, self.n_curves, self.n_images
        info.parser_kinds = list(self.dp.kinds)
        info.text = self.full_text
        info.rows_text = [self._row_text(r) for r in self.rows]
        self._grade(info)
        if info.grade == "D":
            fn = parse_filename(info.file)
            info.drawing_no, info.rev = fn.get("drawing_no", ""), fn.get("rev", "")
            info.sheet, info.sheet_total = fn.get("sheet", 1), fn.get("sheet_total", 1)
            self.log.append("이미지 스캔 도면: 문자 객체가 없어 파일명에서 도면번호/리비전/시트만 확인")
            info.log = self.log
            return info
        self._title_block(info)
        self._kind(info)
        self._views(info)
        self._dims(info)
        self._bom(info)
        self._marks_and_labels(info)
        self._see_dwg_marks(info)
        self._tl_labels(info)
        self._facts(info)
        self.log.append(f"뷰 {len(info.views)} 개, 라벨 치수 {len(info.dims)} 개, 마크 {len(info.marks)} 개, "
                        f"용접선 {len(info.seams)} 개, 사실 {len(info.facts)} 개")
        info.log = self.log
        return info


def _mark_key(m: str):
    r = re.match(r"N(\d+)([A-Z]?)", m)
    return (int(r.group(1)), r.group(2)) if r else (9999, m)


def compact_marks(marks: List[str]) -> str:
    """[N4, N4A, ..., N4E] -> 'N4~N4E', [N27, N29] -> 'N27/N29', [N24, N25, N26] -> 'N24/N25/N26'"""
    fams: Dict[str, List[str]] = {}
    for mk in sorted(set(marks), key=_mark_key):
        r = re.match(r"(N\d+)([A-Z]?)", mk)
        if r:
            fams.setdefault(r.group(1), []).append(r.group(2))
        else:
            fams.setdefault(mk, []).append("")
    out = []
    for fam, sufs in fams.items():
        if len(sufs) >= 3 and sufs[0] == "" and sufs[1:] == [chr(ord("A") + i) for i in range(len(sufs) - 1)]:
            out.append(f"{fam}~{fam}{sufs[-1]}")
        else:
            out.append("/".join(fam + s for s in sufs))
    return "/".join(out)


def extract_drawing_info(pdf_path: str) -> DrawingInfo:
    return SheetExtractor(pdf_path).run()


if __name__ == "__main__":
    import json
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    for p in sys.argv[1:]:
        info = extract_drawing_info(p)
        print(json.dumps(info.to_dict(), ensure_ascii=False, indent=1))
