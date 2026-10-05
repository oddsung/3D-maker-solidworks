"""
dxf_reader.py - DXF 도면(모델 공간)을 PDF 파서와 똑같은 기본 도형으로 바꿔 준다.

이 프로그램의 도면 해석 규칙(drawing_info, pdf_dimension_parser)은 '단어 + 선분 + 곡선' 이라는
기본 도형 위에서 돌아간다. PDF 는 그 도형에 의미가 없어서 굵기(0.57/0.28)와 파선 배열로 물체선/치수선/
중심선을 구분하지만, DXF 는 레이어·선종류·요소 종류에 그 의미가 들어 있다. 여기서 DXF 를 읽어
같은 형태(같은 좌표계, 같은 굵기 관례)로 바꿔 주면 위층 규칙과 모델러는 그대로 쓸 수 있다.

변환 규칙
- 좌표: 모델 공간 mm -> 기준 페이지 포인트. 시트 크기를 ISO 규격(A0~A4)으로 맞춘 뒤 A3 72dpi 페이지
  (1190.4 x 841.68 pt) 에 대응시킨다. 규칙의 간격·허용오차가 전부 이 크기에 맞춰져 있기 때문.
  y 축은 PDF 처럼 아래로 증가하게 뒤집는다.
- 블록(INSERT)·치수(DIMENSION)·폴리선은 구성 요소로 펼친다 (플로터가 그리는 모습과 같아진다).
- 굵기: 치수에서 펼쳐진 선과 '치수 레이어'(치수 요소·화살촉이 몰려 있는 레이어)의 선은 0.28,
  나머지 물체선은 0.57 로 준다. 선 굵기 속성은 대개 BYLAYER 기본값이라 쓸 수 없다.
- 파선: 선종류 이름에 CENTER/PHANTOM/DASHDOT 이 있으면 일점쇄선, DASHED/HIDDEN 이면 파선으로 표시한다.
- 문자: TEXT/MTEXT/ATTRIB 을 한 단어로 (PDF 의 keep_blank_chars=True 와 같은 단위). 정렬·회전을 반영해
  외곽 상자를 만들고, %%c 같은 제어 코드는 기호(Ø, °, ±)로 바꾼다.
"""
from __future__ import annotations

import math
import os
import re
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import ezdxf
except ImportError:                      # ezdxf 가 없는 PC 에서도 import 는 되도록
    ezdxf = None

from pdf_dimension_parser import Word

Pt = Tuple[float, float]

PAGE_W_PT, PAGE_H_PT = 1190.4, 841.68        # 규칙이 맞춰진 기준 페이지 (A3, 72dpi)
ISO_MM = (1189.0, 841.0, 594.0, 420.0, 297.0, 210.0, 148.0)
LW_OBJECT, LW_THIN = 0.57, 0.28              # PDF 물체선/치수선 굵기와 같은 값
CENTER_DASH = [34.2, 2.5, 1.6, 2.5]          # 일점쇄선 (PDF 의 dash 배열과 같은 역할)
HIDDEN_DASH = [3.0, 2.0]
CENTER_RE = re.compile(r"CENTER|PHANTOM|DASHDOT|ACANSTGL", re.I)
HIDDEN_RE = re.compile(r"HIDDEN|DASHED|DOT", re.I)
CHAR_W = 0.75                                # 평균 글자 폭 / 글자 높이 (romans 계열 글꼴, PDF 단어 폭 274개와 맞춰 보정)

_CACHE: Dict[tuple, dict] = {}


# ----------------------------------------------------------------------
def _clean_text(s: str) -> str:
    """제어 코드(%%c 등)와 MTEXT 서식을 지운다"""
    if not s:
        return ""
    s = s.replace("%%C", "Ø").replace("%%c", "Ø")
    s = s.replace("%%D", "°").replace("%%d", "°")
    s = s.replace("%%P", "±").replace("%%p", "±")
    s = s.replace("%%%", "%")
    s = re.sub(r"%%[uUoO]", "", s)                     # 밑줄/윗줄 켜고 끄기
    s = re.sub(r"%%(\d{3})", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\[A-Za-z][^;\\]*;", "", s)          # \H2.5x; \f나눔;  같은 서식
    s = re.sub(r"[{}]", "", s).replace("\\~", " ")
    return s.strip()


def _snap_iso(v: float) -> float:
    """시트 한 변의 길이를 가까운 ISO 규격(10% 이내)으로 맞춘다. 맞는 게 없으면 그대로"""
    for m in ISO_MM:
        if abs(v - m) <= 0.1 * m:
            return m
    return v


def _linetype(e, layer_lt: Dict[str, str]) -> str:
    lt = (getattr(e.dxf, "linetype", "BYLAYER") or "BYLAYER").upper()
    if lt in ("BYLAYER", "BYBLOCK", ""):
        lt = (layer_lt.get(getattr(e.dxf, "layer", "0"), "CONTINUOUS") or "CONTINUOUS").upper()
    return lt


def _flatten(entities, is_dim: bool = False, depth: int = 0):
    """블록·치수·폴리선을 구성 요소로 펼친다. (요소, 치수에서 나왔는지) 를 내놓는다"""
    for e in entities:
        t = e.dxftype()
        if depth > 6:
            continue
        if t == "INSERT":
            try:
                yield from _flatten(e.virtual_entities(), is_dim, depth + 1)
            except Exception:
                pass
            for a in getattr(e, "attribs", []):
                yield a, is_dim
        elif t in ("DIMENSION", "LEADER", "MULTILEADER", "ARC_DIMENSION"):
            try:
                yield from _flatten(e.virtual_entities(), True, depth + 1)
            except Exception:
                pass
        elif t in ("LWPOLYLINE", "POLYLINE"):
            try:
                yield from _flatten(e.virtual_entities(), is_dim, depth + 1)
            except Exception:
                pass
        else:
            yield e, is_dim


def _dim_layers(items) -> set:
    """치수선 레이어: 시트의 치수 요소(치수에서 펼쳐진 것 + 화살촉) 중 과반을 담고 있는 레이어,
    또는 자기 내용이 거의 다 치수인 레이어. 선 굵기 속성이 BYLAYER 기본값이라 굵기로는 못 가른다.

    '자기 내용의 10% 이상이 치수' 같은 기준은 못 쓴다. 물체 레이어에도 치수가 조금씩 섞여 있어
    (0005 6/8 의 물체 레이어 '1' 은 24%) 물체선을 통째로 잃는다."""
    total: Dict[str, int] = {}
    dim: Dict[str, int] = {}
    for e, from_dim in items:
        lay = getattr(e.dxf, "layer", "0")
        total[lay] = total.get(lay, 0) + 1
        if from_dim or e.dxftype() == "SOLID":
            dim[lay] = dim.get(lay, 0) + 1
    grand = sum(dim.values()) or 1
    return {lay for lay, n in dim.items()
            if n and (n / grand >= 0.5 or n / max(total.get(lay, 1), 1) >= 0.8)}


def _entity_points(e) -> List[Pt]:
    t = e.dxftype()
    try:
        if t == "LINE":
            return [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
        if t == "ARC":
            c, r = e.dxf.center, e.dxf.radius
            return [(c.x - r, c.y - r), (c.x + r, c.y + r)]
        if t == "CIRCLE":
            c, r = e.dxf.center, e.dxf.radius
            return [(c.x - r, c.y - r), (c.x + r, c.y + r)]
    except Exception:
        pass
    return []


def _iso_like(frame) -> bool:
    """도면 틀다운 직사각형인지 (ISO 용지 비율 √2 근처)"""
    w, h = frame[2] - frame[0], frame[3] - frame[1]
    if w < 100 or h < 70:
        return False
    return 1.25 < max(w, h) / min(w, h) < 1.6


class _Transform:
    """모델 공간 mm -> 기준 페이지 pt (y 는 아래로 증가).

    시트는 도면 틀(가장 긴 가로 물체선 x 가장 긴 세로 물체선)로 잡는다. 요소 분포로 잡으면 틀 밖에 놓인
    작업용 사본 때문에 엉뚱한 크기가 나온다 (0005 1/8 은 도면 전체가 x 로 954mm 떨어진 곳에 그려져 있음).
    틀을 못 찾으면 요소 분포(1~99%)로 물러난다."""

    def __init__(self, xs: List[float], ys: List[float], frame=None):
        def q(v, f):
            v = sorted(v)
            return v[min(len(v) - 1, max(0, int(len(v) * f)))] if v else 0.0

        if frame and _iso_like(frame):
            fx0, fy0, fx1, fy1 = frame
        else:
            fx0, fx1 = min(q(xs, 0.01), 0.0), q(xs, 0.99)
            fy0, fy1 = min(q(ys, 0.01), 0.0), q(ys, 0.99)
        fw, fh = fx1 - fx0, fy1 - fy0
        sheet_w = _snap_iso(fw) or 841.0
        sheet_h = _snap_iso(fh) or 594.0
        self.s = min(PAGE_W_PT / sheet_w, PAGE_H_PT / sheet_h)
        self.x0 = fx0 - (sheet_w - fw) / 2               # 틀을 시트 한가운데 둔다
        self.y_top = fy1 + (sheet_h - fh) / 2
        self.page_w, self.page_h = sheet_w * self.s, sheet_h * self.s
        self.sheet = (sheet_w, sheet_h)
        self.frame = (fx0, fy0, fx1, fy1)

    def __call__(self, x: float, y: float) -> Pt:
        return ((x - self.x0) * self.s, (self.y_top - y) * self.s)


def _text_word(e, tr: _Transform) -> Optional[List[Word]]:
    """TEXT/MTEXT/ATTRIB -> Word (회전·정렬 반영한 외곽 상자). MTEXT 여러 줄은 줄마다 하나"""
    t = e.dxftype()
    try:
        if t in ("TEXT", "ATTRIB"):
            raw = [_clean_text(e.dxf.text)]
            h = float(e.dxf.height or 2.5)
            rot = float(getattr(e.dxf, "rotation", 0.0) or 0.0)
            wf = float(getattr(e.dxf, "width", 1.0) or 1.0)
            halign = int(getattr(e.dxf, "halign", 0) or 0)
            valign = int(getattr(e.dxf, "valign", 0) or 0)
            anchor = e.dxf.insert
            if (halign or valign) and e.dxf.hasattr("align_point"):
                anchor = e.dxf.align_point
            hx = {0: 0.0, 3: 0.0, 5: 0.0, 1: -0.5, 4: -0.5, 2: -1.0}.get(halign, 0.0)
            vy = {0: 0.0, 1: 0.0, 2: -0.5, 3: -1.0}.get(valign, 0.0)
        else:                                  # MTEXT
            txt = _clean_text(e.plain_text() if hasattr(e, "plain_text") else e.text)
            raw = [ln for ln in re.split(r"\n|\\P", txt)]
            h = float(e.dxf.char_height or 2.5)
            rot = float(getattr(e.dxf, "rotation", 0.0) or 0.0)
            # MTEXT 는 회전을 '문자 방향 벡터'로 저장하기도 한다 (이 도면의 세로 치수 문자가 그렇다).
            # 놓치면 세로 치수가 정립 문자로 읽혀 수직 노즐이 수평으로 판정된다
            if e.dxf.hasattr("text_direction"):
                td = e.dxf.text_direction
                if abs(td.x) > 1e-9 or abs(td.y) > 1e-9:
                    rot = math.degrees(math.atan2(td.y, td.x))
            wf = 1.0
            ap = int(getattr(e.dxf, "attachment_point", 1) or 1)
            hx = {1: 0.0, 4: 0.0, 7: 0.0, 2: -0.5, 5: -0.5, 8: -0.5, 3: -1.0, 6: -1.0, 9: -1.0}.get(ap, 0.0)
            vy = {1: -1.0, 2: -1.0, 3: -1.0, 4: -0.5, 5: -0.5, 6: -0.5, 7: 0.0, 8: 0.0, 9: 0.0}.get(ap, -1.0)
            anchor = e.dxf.insert
    except Exception:
        return None
    if h <= 0:
        h = 2.5
    ca, sa = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    # upright 는 pdfplumber 와 같은 뜻으로 맞춘다: 세로로 세운 문자(90/270도 부근)만 False.
    # 45도로 기울인 문자도 PDF 에서는 upright 로 나오므로 여기서도 True 로 본다
    rot_n = rot % 180.0
    upright = not (70.0 < rot_n < 110.0)
    out: List[Word] = []
    for i, text in enumerate(raw):
        if not text:
            continue
        w_mm = CHAR_W * h * wf * max(len(text), 1)
        x_off, y_off = hx * w_mm, vy * h - i * h * 1.35
        corners = [(x_off, y_off), (x_off + w_mm, y_off), (x_off + w_mm, y_off + h), (x_off, y_off + h)]
        pts = [tr(anchor.x + cx * ca - cy * sa, anchor.y + cx * sa + cy * ca) for cx, cy in corners]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        out.append(Word(text, min(xs), max(xs), min(ys), max(ys), upright))
    return out


# ----------------------------------------------------------------------
def load_dxf(path: str) -> dict:
    """DXF 한 장 -> PDF 파서와 같은 기본 도형 묶음 (같은 파일은 캐시)"""
    key = (os.path.abspath(path), os.path.getmtime(path))
    if key in _CACHE:
        return _CACHE[key]
    if ezdxf is None:
        raise RuntimeError("ezdxf 가 설치되어 있지 않습니다 (pip install ezdxf)")
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    layer_lt = {l.dxf.name: l.dxf.linetype for l in doc.layers}
    items = list(_flatten(msp))
    dim_layers = _dim_layers(items)

    xs: List[float] = []
    ys: List[float] = []
    long_h: List[Tuple[float, float, float]] = []      # (길이, x 최소, x 최대) 도면 틀 후보
    long_v: List[Tuple[float, float, float]] = []
    for e, _ in items:
        pts = _entity_points(e)
        for p in pts:
            xs.append(p[0])
            ys.append(p[1])
        if e.dxftype() == "LINE" and len(pts) == 2:
            (ax, ay), (bx, by) = pts
            if abs(ay - by) < 0.05 and abs(ax - bx) > 150:
                long_h.append((abs(ax - bx), min(ax, bx), max(ax, bx)))
            elif abs(ax - bx) < 0.05 and abs(ay - by) > 100:
                long_v.append((abs(ay - by), min(ay, by), max(ay, by)))
    frame = None
    if long_h and long_v:
        h, v = max(long_h), max(long_v)
        frame = (h[1], v[1], h[2], v[2])
    tr = _Transform(xs, ys, frame)

    words: List[Word] = []
    raw_lines: List[dict] = []
    raw_curves: List[dict] = []
    segs: List[Tuple[Pt, Pt]] = []
    centerlines: List[Tuple[Pt, Pt]] = []
    seg_big: set = set()
    circles: List[Tuple[float, float, float, float]] = []
    n_images = 0

    def on_page(*pts) -> bool:
        """시트 테두리 밖(작업용 사본 등)은 버린다. PDF 는 출력할 때 잘려 나가 애초에 없는 내용"""
        m = 10.0
        return any(-m <= x <= tr.page_w + m and -m <= y <= tr.page_h + m for x, y in pts)

    for e, from_dim in items:
        t = e.dxftype()
        if t in ("TEXT", "MTEXT", "ATTRIB"):
            for w in (_text_word(e, tr) or []):
                if on_page((w.x0, w.top), (w.x1, w.bottom)):
                    words.append(w)
            continue
        if t in ("IMAGE", "OLE2FRAME", "WIPEOUT"):
            n_images += 1
            continue
        if t not in ("LINE", "ARC", "CIRCLE", "SPLINE", "ELLIPSE"):
            continue
        thin = from_dim or getattr(e.dxf, "layer", "0") in dim_layers
        lw = LW_THIN if thin else LW_OBJECT
        lt = _linetype(e, layer_lt)
        dash = CENTER_DASH if CENTER_RE.search(lt) else (HIDDEN_DASH if HIDDEN_RE.search(lt) else [])
        if t == "LINE":
            p1, p2 = tr(e.dxf.start.x, e.dxf.start.y), tr(e.dxf.end.x, e.dxf.end.y)
            if not on_page(p1, p2):
                continue
            raw_lines.append({"p1": p1, "p2": p2, "lw": lw, "dash": list(dash)})
            axis_aligned = abs(p1[0] - p2[0]) < 0.5 or abs(p1[1] - p2[1]) < 0.5
            if dash is CENTER_DASH and axis_aligned and math.dist(p1, p2) >= 25:
                centerlines.append((p1, p2))        # 뷰 축 (단면도 중심선 교점 찾기용)
            else:
                segs.append((p1, p2))
            continue
        if t in ("SPLINE", "ELLIPSE"):
            # 자유 곡선(지시선·구름 표시·경판 윤곽)을 잘게 펴서 PDF 의 curve 와 같은 형태로 넣는다
            try:
                pts = [tr(q.x, q.y) for q in e.flattening(0.5)]
            except Exception:
                continue
            if len(pts) < 2 or not on_page(*pts):
                continue
            xs_c = [q[0] for q in pts]
            ys_c = [q[1] for q in pts]
            w_pt, h_pt = max(xs_c) - min(xs_c), max(ys_c) - min(ys_c)
            raw_curves.append({"p1": pts[0], "p2": pts[-1], "lw": lw, "w": w_pt, "h": h_pt})
            if w_pt < 80 and h_pt < 80:
                if w_pt >= 15 or h_pt >= 15:
                    seg_big.add(len(segs))          # 풍선에서 나가는 스플라인 지시선
                segs.append((pts[0], pts[-1]))
            continue
        # ARC / CIRCLE: 끝점(현)과 외곽 상자 크기 -> PDF 의 curve 와 같은 형태
        c, r = e.dxf.center, float(e.dxf.radius)
        if t == "ARC":
            p1, p2 = tr(e.start_point.x, e.start_point.y), tr(e.end_point.x, e.end_point.y)
        else:
            p1 = p2 = tr(c.x + r, c.y)
        box = (tr(c.x - r, c.y + r), tr(c.x + r, c.y - r))
        if not on_page(*box):
            continue
        w_pt, h_pt = abs(box[1][0] - box[0][0]), abs(box[1][1] - box[0][1])
        raw_curves.append({"p1": p1, "p2": p2, "lw": lw, "w": w_pt, "h": h_pt})
        if t == "CIRCLE":
            if 12 < w_pt < 24 and abs(w_pt - h_pt) <= 4:      # 풍선 원 후보 (PDF 와 같은 조건)
                circles.append((box[0][0], box[0][1], box[1][0], box[1][1]))
            continue
        # 호는 지시선의 꺾임으로도 쓰이므로 현을 선분 목록에도 넣는다 (PDF 와 같은 취급)
        if w_pt < 80 and h_pt < 80:
            if w_pt >= 15 or h_pt >= 15:
                seg_big.add(len(segs))
            segs.append((p1, p2))

    out = {
        "words": words, "segs": segs, "seg_big": seg_big, "circle_boxes": circles,
        "centerlines": centerlines, "page_w": tr.page_w, "page_h": tr.page_h,
        "raw_lines": raw_lines, "raw_curves": raw_curves,
        "n_lines": len(raw_lines), "n_curves": len(raw_curves), "n_images": n_images,
        "sheet_mm": tr.sheet, "scale_pt_per_mm": tr.s,
    }
    _CACHE.clear()                 # 시트 하나만 들고 있으면 충분 (20장 x 수만 요소)
    _CACHE[key] = out
    return out


def load_page_dxf(path: str):
    """pdf_dimension_parser.load_page 와 같은 형태로 돌려준다"""
    d = load_dxf(path)
    return (d["words"], d["segs"], d["seg_big"], d["circle_boxes"], d["centerlines"],
            d["page_w"], d["page_h"])


def is_dxf(path: str) -> bool:
    return os.path.splitext(path)[1].lower() == ".dxf"
