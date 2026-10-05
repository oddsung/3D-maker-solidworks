"""
sw_modeler.py - VesselSpec 을 바탕으로 SolidWorks 에서 3D 파트를 생성한다.

좌표계 (SolidWorks 파트 기준, 단위 m):
  X : 용기 축 방향 (좌측 T.L 이 x=0, 우측 T.L 이 x=tl_length)
  Y : 상하 (위가 +)
  Z : 용기 축 직각 수평 방향
스케치 평면:
  정면(Front Plane, XY)      - 쉘+경판 회전 프로파일, 경판(축방향) 노즐
  우측면 오프셋 기준면(YZ)    - 노즐(각도별 회전 단면), 새들, 러그  (x 위치마다 하나씩 생성, 캐시)
스케치 좌표는 ISketch.ModelToSketchTransform 으로 모델 좌표에서 변환해 넣는다.

pywin32 late binding 주의:
  - COM 객체 인자에 None 을 넘기면 형식 오류 -> _nothing() 사용
  - 인자 없는 메서드는 속성 접근 시점에 실행됨 -> _call0() 사용
"""
from __future__ import annotations

import glob
import math
import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

from vessel_spec import VesselSpec, NozzleSpec
from asme_flange import flange_cl300

try:
    import pythoncom
    import win32com.client
except ImportError:  # SolidWorks 가 없는 PC 에서도 import 는 되도록
    pythoncom = None
    win32com = None

MM = 1.0 / 1000.0   # mm -> m

# SolidWorks 상수 (swconst.tlb 에서 확인)
swDocPART = 1
swDefaultTemplatePart = 8          # swUserPreferenceStringValue_e
swEndCondBlind = 0                 # swEndConditions_e
swEndCondMidPlane = 6
swSaveAsCurrentVersion = 0
swSaveAsOptions_Silent = 1
swRefPlaneReferenceConstraint_Distance = 8      # swRefPlaneReferenceConstraints_e
swRefPlaneReferenceConstraint_OptionFlip = 256
swRefPlaneReferenceConstraint_Coincident = 4
swRefPlaneReferenceConstraint_Angle = 16
swInputDimValOnCreate = 10         # swUserPreferenceToggle_e: 치수 입력 대화상자 (자동화에서는 꺼야 멈추지 않음)
swSketchOverdefiningDimsSetDrivenByDefault = 101   # 과구속이 될 치수는 자동으로 참조 치수로
swDimensionDriven = 1              # swDimensionDrivenState_e (구동=2, 참조=1)
swShowDimensionNames = 76          # 치수 이름을 값과 같이 표시 (도면처럼 읽히도록)
swAutoNormalToSketchMode = 477     # 스케치를 열 때 화면을 그 평면 정면으로 자동 회전 (켜져 있으면 시점이 계속 튄다)
swViewZoomFitAndCenter = 369       # 표준 뷰로 바꿀 때 화면에 맞추고 가운데 정렬
swDimensionTextPrefix = 1          # swDimensionTextParts_e
swDimensionTextSuffix = 2
swFullyConstrained = 3             # swConstrainedStatus_e (불완전=2, 완전=3, 과정의=4)
# swSketchFullyDefineRelationType_e 전체: 같음/수평/수직/접선/직각/동일선상/동심/평행/중점/일치
swSketchFullyDefineRelations = 1 | 2 | 4 | 8 | 16 | 32 | 64 | 128 | 256 | 512

PLANE_NAME_CANDIDATES = {
    "front": ["Front Plane", "정면", "Front"],
    "top": ["Top Plane", "윗면", "Top"],
    "right": ["Right Plane", "우측면", "Right"],
}

Vec3 = Tuple[float, float, float]


def _nothing():
    """VBA 의 Nothing. COM 객체 인자에 None 을 그대로 넘기면 VT_EMPTY 가 되어
    '형식이 일치하지 않습니다'(DISP_E_TYPEMISMATCH) 오류가 나므로 VT_DISPATCH 로 감싼다."""
    return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


SAVE_ERRORS = {   # swFileSaveError_e
    1: "일반 저장 오류(같은 이름의 문서가 이미 열려 있거나 경로 문제)",
    2: "읽기 전용", 4: "파일명 없음", 8: "파일명에 @ 포함", 16: "파일 잠김(다른 프로그램에서 사용 중)",
    32: "저장 형식 사용 불가", 64: "재생성 오류와 함께 저장", 128: "덮어쓰기 안 함",
    256: "잘못된 확장자", 2048: "경로가 너무 김",
}


def _save_error_text(code: int) -> str:
    names = [msg for bit, msg in SAVE_ERRORS.items() if code & bit]
    return f"errors={code}: " + (", ".join(names) if names else "알 수 없음")


def _call0(obj, name: str):
    """인자 없는 COM 메서드 호출. late binding 에서는 속성 접근 시점에 이미 실행되고
    결과가 반환되므로, callable 이면 호출하고 아니면 그 값을 그대로 쓴다."""
    v = getattr(obj, name)
    if callable(v) and not isinstance(v, win32com.client.CDispatch):   # COM 객체 자체도 callable 이므로 제외
        return v()
    return v


def _template_folders() -> list:
    """레지스트리(Document Template Folders) + ProgramData 기본 위치의 템플릿 폴더 목록"""
    folders = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\SolidWorks") as root:
            i = 0
            while True:
                try:
                    ver = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, ver + r"\ExtReferences") as k:
                        val, _ = winreg.QueryValueEx(k, "Document Template Folders")
                        folders += [p for p in val.split(";") if p]
                except OSError:
                    pass
    except ImportError:
        pass
    folders += glob.glob(r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS *\templates")
    unique = []
    for f in folders:
        if os.path.isdir(f) and os.path.normcase(os.path.abspath(f)) not in [
            os.path.normcase(os.path.abspath(u)) for u in unique
        ]:
            unique.append(f)
    return unique


def find_part_templates() -> list:
    """사용 가능한 파트 템플릿을 우선순위 순으로 반환.
    일반 Part/파트 템플릿 > MBD 대형(1001mm 이상) 템플릿 > 기타 .prtdot"""
    paths = []
    for folder in _template_folders():
        paths += glob.glob(os.path.join(folder, "**", "*.prtdot"), recursive=True)

    def rank(p: str):
        name = os.path.basename(p).lower()
        in_mbd = os.sep + "mbd" + os.sep in p.lower()
        if name in ("part.prtdot", "파트.prtdot"):
            return 0
        if not in_mbd:
            return 1
        if "1001mm" in name:
            return 2
        return 3

    return sorted(set(paths), key=rank)


# ----------------------------------------------------------------------
def _dedupe(points: Sequence[Vec3]) -> List[Vec3]:
    out: List[Vec3] = []
    for p in points:
        if not out or math.dist(out[-1], p) > 1e-7:
            out.append(p)
    if len(out) > 1 and math.dist(out[0], out[-1]) < 1e-7:
        out.pop()
    return out


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
        self._xf: Optional[Tuple[float, ...]] = None      # 현재 스케치의 모델->스케치 변환
        self._xf_row = True
        self._planes: Dict[tuple, str] = {}                # (기본 평면, 거리 mm) -> 기준면 이름
        self._plane_flips: Dict[str, Optional[bool]] = {}
        self.feature_count = 0
        self.dim_count = 0
        self.dim_log: List[Tuple[str, float, str]] = []      # (치수 이름, 값 mm, 설명)
        self._view_t = 0.0                                 # 마지막 화면 맞춤 시각 (너무 자주 부르지 않도록)
        self._prefs: Dict[int, bool] = {}                   # 바꾼 SolidWorks 설정의 원래 값 (끝나면 되돌린다)

    # ------------------------------------------------------------------
    def connect(self) -> None:
        pythoncom.CoInitialize()
        print("SOLIDWORKS 연결 중...")
        self.app = win32com.client.Dispatch("SldWorks.Application")
        self.app.Visible = self.visible
        time.sleep(1)

    def _resolve_template(self) -> str:
        if self.template_path:
            if os.path.exists(self.template_path):
                return self.template_path
            raise FileNotFoundError(f"지정한 템플릿이 없습니다: {self.template_path}")
        try:
            t = self.app.GetUserPreferenceStringValue(swDefaultTemplatePart)
            if t and os.path.exists(t):
                return t
        except Exception:
            pass
        found = find_part_templates()
        if found:
            return found[0]
        raise FileNotFoundError(
            "파트 템플릿(.prtdot)을 찾을 수 없습니다. --template 옵션으로 지정하거나 "
            "SOLIDWORKS 설치 복구로 기본 템플릿을 복원하세요."
        )

    def new_part(self) -> None:
        template = self._resolve_template()
        print(f"새 파트 생성: {template}")
        self.model = self.app.NewDocument(template, 0, 0, 0)
        if self.model is None:
            raise RuntimeError("새 파트 문서 생성 실패")
        self.sketch_mgr = self.model.SketchManager
        self.feat_mgr = self.model.FeatureManager
        for pref, val in ((swInputDimValOnCreate, False),                  # 값 입력 대화상자로 멈추지 않게
                          (swSketchOverdefiningDimsSetDrivenByDefault, True),
                          (swShowDimensionNames, True),                    # 치수 이름을 값과 같이 표시
                          (swAutoNormalToSketchMode, False),               # 스케치마다 시점이 돌아가지 않게
                          (swViewZoomFitAndCenter, True)):
            try:
                self._prefs.setdefault(pref, bool(self.app.GetUserPreferenceToggle(pref)))
                self.app.SetUserPreferenceToggle(pref, val)
            except Exception:
                pass
        try:
            self.model.ShowNamedView2("*Isometric", 7)
        except Exception:
            pass
        time.sleep(0.5)

    # ------------------------------------------------------------------
    # 평면 / 스케치
    def _select_plane(self, name_or_key: str, append: bool = False, mark: int = 0) -> None:
        names = PLANE_NAME_CANDIDATES.get(name_or_key, [name_or_key])
        for name in names:
            ok = self.model.Extension.SelectByID2(name, "PLANE", 0, 0, 0, append, mark, _nothing(), 0)
            if ok:
                return
        raise RuntimeError(f"평면 선택 실패: {name_or_key}")

    def _last_feature(self):
        return self.model.FeatureByPositionReverse(0)

    def _offset_plane(self, x_mm: float, base: str = "right") -> str:
        """기본 평면(right=YZ 를 x 로, front=XY 를 z 로, top=XZ 를 y 로)에서 떨어진 기준면. 같은 값은 재사용"""
        key = (base, round(x_mm, 1))
        if key in self._planes:
            return self._planes[key]
        axis_idx = {"right": 9, "front": 11, "top": 10}[base]     # Transform.ArrayData 의 원점 성분
        letter = {"right": "x", "front": "z", "top": "y"}[base]
        name = f"Plane_{letter}{key[1]:g}"
        x_m = x_mm * MM
        neg = x_m < 0                      # 음수 쪽은 양수 쪽과 반대로 뒤집는다
        self._plane_flips.setdefault(base, None)
        for attempt in range(2):
            if self._plane_flips[base] is not None:
                flip = bool(self._plane_flips[base]) != neg
            else:
                flip = (attempt == 1) != neg
            constraint = swRefPlaneReferenceConstraint_Distance | (swRefPlaneReferenceConstraint_OptionFlip if flip else 0)
            self.model.ClearSelection2(True)
            self._select_plane(base, False, 0)
            plane = self.feat_mgr.InsertRefPlane(constraint, abs(x_m), 0, 0.0, 0, 0.0)
            if plane is None:
                raise RuntimeError(f"기준면 생성 실패 (x={x_mm:g} mm)")
            feat = self._last_feature()
            feat.Name = name
            # 생성된 평면의 원점 x 로 방향 확인
            try:
                tx = plane.Transform.ArrayData[axis_idx]
            except Exception:
                tx = x_m
            if abs(tx - x_m) < 1e-6:
                self._plane_flips[base] = flip != neg      # 양수 쪽 기준으로 기억
                break
            if self._plane_flips[base] is not None or attempt == 1:
                raise RuntimeError(f"기준면 {name} 위치 확인 실패 (tx={tx})")
            # 반대쪽에 생겼으면 삭제 후 flip 으로 재생성
            self.model.ClearSelection2(True)
            self.model.Extension.SelectByID2(name, "PLANE", 0, 0, 0, False, 0, _nothing(), 0)
            self.model.Extension.DeleteSelection2(0)
        self.model.ClearSelection2(True)
        self._planes[key] = name
        return name

    def _begin_sketch(self, plane: str) -> None:
        self.model.ClearSelection2(True)
        self._select_plane(plane)
        self.sketch_mgr.InsertSketch(True)
        self.sketch_mgr.AddToDB = True       # 스냅/추론 없이 정확한 좌표로 입력
        self._sketch_dims = 0
        self._sketch_ref = None              # 이 스케치의 고정 기준점 (자동 완전정의의 기준)
        self._load_transform(plane)

    def _end_sketch(self, fix: bool = True) -> None:
        """스케치를 완전 정의로 만든다. 먼저 SolidWorks 의 자동 완전 정의(기하 구속 + 모자란 치수)를 쓰고,
        그것이 안 되는 스케치만 고정 구속으로 처리한다. 자동 완전 정의는 도면 치수를 그대로 두고 모두 구동
        치수로 남기므로, 완전 정의와 편집 가능을 동시에 만족한다 (담당자 파일과 같은 방식)"""
        self.sketch_mgr.AddToDB = False
        if fix:
            if getattr(self, "_sketch_dims", 0):
                # 도면 치수를 넣은 스케치: 자동 완전정의로 나머지를 채운다. 여기에 고정 구속을 더하면
                # 과정의가 되므로(실측) 절대 같이 쓰지 않는다
                self._fully_define()
            else:
                # 치수가 없는 보조 스케치(리브·볼트 구멍 등): 고정 구속이 확실하게 완전정의로 만든다
                self._fix_sketch()
        self.model.ClearSelection2(True)

    def _fix_sketch(self) -> None:
        """스케치의 모든 요소에 고정(sgFIXED) 구속을 걸어 완전 정의로 만든다. 좌표로 정확히 그려 넣은
        형상이므로 위치는 그대로이고, 상태만 '불완전 정의'에서 '완전 정의'로 바뀐다"""
        try:
            sk = self.sketch_mgr.ActiveSketch
            if sk is None:
                return
            segs = _call0(sk, "GetSketchSegments") or []
            if not segs:
                return
            self.model.ClearSelection2(True)
            sd = _call0(self.model.SelectionManager, "CreateSelectData")
            picked = False
            for seg in segs:
                picked = bool(seg.Select4(True, sd)) or picked
            if picked:
                self.model.SketchAddConstraints("sgFIXED")
        except Exception as e:
            print(f"    고정 구속 생략 ({e})")
        finally:
            self.model.ClearSelection2(True)

    def _fully_define(self) -> bool:
        """SolidWorks 의 '스케치 완전 정의'로 기하 구속과 모자란 치수를 자동으로 채운다.
        담당자가 손으로 하던 방식(고정 구속 없이 수평·수직·동일선상·일치 + 치수)과 같은 결과가 되고,
        우리가 넣어 둔 도면 치수는 그대로 남아 편집도 된다.
        실측: 형상은 움직이지 않고, 노즐 단면에는 자동 치수 7 개가 더 붙어 완전정의가 된다.
        움직이지 않는 기준점이 없으면 사각형조차 완전정의가 안 되므로, 없으면 스케치 원점에 하나 만든다"""
        try:
            sk = self.sketch_mgr.ActiveSketch
            if sk is None:
                return False
            if getattr(self, "_sketch_ref", None) is None:
                self._anchor_point()
            res = self.sketch_mgr.FullyDefineSketch(
                True, True, swSketchFullyDefineRelations, True,
                0, _nothing(), 0, _nothing(), 0, 0)
            return res == 0 and _call0(sk, "GetConstrainedStatus") == swFullyConstrained
        except Exception as e:
            print(f"    자동 완전정의 생략 ({e})")
            return False

    def _anchor_point(self):
        """스케치 원점 자리에 고정 점을 만든다 (평면 종류와 무관하게 스케치 좌표 (0,0))"""
        try:
            pt = self.sketch_mgr.CreatePoint(0.0, 0.0, 0.0)
            if pt is None:
                return None
            self.model.ClearSelection2(True)
            sd = _call0(self.model.SelectionManager, "CreateSelectData")
            if pt.Select4(False, sd):
                self.model.SketchAddConstraints("sgFIXED")
            self._sketch_ref = pt
            return pt
        except Exception as e:
            print(f"    기준점 생성 생략 ({e})")
            return None
        finally:
            self.model.ClearSelection2(True)

    def _pick(self, target, append: bool, sd) -> bool:
        """치수를 걸 대상 선택: 스케치 요소 객체이거나 ('EXTSKETCHPOINT', x, y, z) 같은 좌표 선택"""
        if target is None:
            return False
        if isinstance(target, tuple):
            kind, x, y, z = target
            return bool(self.model.Extension.SelectByID2("", kind, x, y, z, append, 0, _nothing(), 0))
        return bool(target.Select4(append, sd))

    def _dim(self, seg_a, seg_b, at: Vec3, name: str = "", driven: bool = False,
             tag: str = "", suffix: str = "", dia: bool = False, expect: Optional[float] = None,
             prefix: str = "") -> None:
        """두 대상 사이에 치수를 넣는다. 기본은 구동 치수라 나중에 값을 고치면 형상이 따라 바뀐다
        (도면에서 읽은 값이 그대로 들어간다). 실패해도 모델링은 계속한다.
        at(치수 글자 위치)을 회전축 중심선 건너편에 두면 SolidWorks 가 지름 치수로 만든다 -> 도면의 Ø 표기와 같아진다.
        tag 는 치수 이름(화면에 값과 같이 표시), suffix 는 값 뒤에 붙는 글자('TO C.L' 처럼)"""
        if seg_a is None or seg_b is None:
            return
        try:
            self.model.ClearSelection2(True)
            sd = _call0(self.model.SelectionManager, "CreateSelectData")
            if not self._pick(seg_a, False, sd) or not self._pick(seg_b, True, sd):
                print(f"    치수 대상 선택 실패 ({name})")
                return
            disp = self.model.AddDimension2(at[0], at[1], at[2])
            if disp is None:
                print(f"    치수 생성 실패 ({name})")
                return
            if dia:                       # 축까지의 거리를 도면처럼 지름으로 표시 (값도 2배가 된다)
                try:
                    disp.Diametric = True
                except Exception:
                    pass
            dim = _call0(disp, "GetDimension")
            if dim is not None:
                if driven:
                    try:
                        dim.DrivenState = swDimensionDriven
                    except Exception:
                        pass
                if tag:
                    try:
                        dim.Name = tag
                    except Exception as e:
                        print(f"    치수 이름 지정 실패 ({name}: {e})")
                try:    # 만든 직후에 이름과 값을 되읽어 기록한다 (도면 값과 비교할 수 있게)
                    val = dim.GetSystemValue2("") * 1000.0
                    self.dim_log.append((str(dim.FullName).split("@")[0], val, name))
                    # 평행하지 않은 두 선에 치수를 걸면 SolidWorks 가 각도(라디안) 치수를 만든다 -> 값으로 걸러낸다
                    if expect is not None and abs(val - expect) > max(0.5, abs(expect) * 0.01):
                        print(f"    치수 값 이상 ({name}: 도면 값 {expect:.1f} 인데 {val:.1f} 로 들어감)")
                except Exception:
                    pass
            for part, text in ((swDimensionTextPrefix, prefix), (swDimensionTextSuffix, suffix)):
                if text:
                    try:
                        disp.SetText(part, text)     # 도면처럼 읽히도록 값 앞뒤에 글자를 붙인다
                    except Exception:
                        pass
            self.dim_count += 1
            self._sketch_dims = getattr(self, "_sketch_dims", 0) + 1
        except Exception as e:
            print(f"    치수 생략 ({name}: {e})")
        finally:
            self.model.ClearSelection2(True)

    def _load_transform(self, plane: str, probe: Optional[Vec3] = None) -> None:
        """활성 스케치의 모델->스케치 변환(16 doubles). 실패 시 정면은 항등, 그 외는 오류.
        probe(평면 위에 있어야 하는 모델 점)가 주어지면 행/열 규약 중 그 점의 z' 가 0 에 가까운 쪽을 고른다
        (기울어진 기준면에서는 법선 성분으로 판별할 수 없음)"""
        self._xf = None
        try:
            sk = self.sketch_mgr.ActiveSketch
            xf = sk.ModelToSketchTransform
            arr = tuple(float(v) for v in xf.ArrayData)
            if len(arr) >= 13:
                self._xf = arr
                if probe is not None:
                    self._xf_row = True
                    z_row = abs(self._to_sk_full(probe)[2])
                    self._xf_row = False
                    z_col = abs(self._to_sk_full(probe)[2])
                    self._xf_row = z_row <= z_col
                elif plane != "front":
                    # 행벡터 규약(p' = p·R) 인지 열벡터 규약인지 판별: 기준면 법선(우측면 = X) 이 스케치 z 로 가야 함
                    self._xf_row = abs(arr[2]) > 0.9 or not (abs(arr[6]) > 0.9)
                else:
                    self._xf_row = True
        except Exception as e:
            if plane != "front":
                raise RuntimeError(f"스케치 변환을 읽지 못했습니다 ({plane}): {e}")

    def _to_sk_full(self, p: Vec3) -> Tuple[float, float, float]:
        a = self._xf
        x, y, z = p
        s = a[12] if a[12] else 1.0
        if self._xf_row:
            return (s * (x * a[0] + y * a[3] + z * a[6]) + a[9], s * (x * a[1] + y * a[4] + z * a[7]) + a[10],
                    s * (x * a[2] + y * a[5] + z * a[8]) + a[11])
        return (s * (x * a[0] + y * a[1] + z * a[2]) + a[9], s * (x * a[3] + y * a[4] + z * a[5]) + a[10],
                s * (x * a[6] + y * a[7] + z * a[8]) + a[11])

    def _to_sk(self, p: Vec3) -> Tuple[float, float]:
        """모델 좌표(m) -> 스케치 2D 좌표(m). 평면 밖의 점이면 오류"""
        if self._xf is None:
            return p[0], p[1]
        a = self._xf
        x, y, z = p
        s = a[12] if a[12] else 1.0
        if self._xf_row:
            sx = s * (x * a[0] + y * a[3] + z * a[6]) + a[9]
            sy = s * (x * a[1] + y * a[4] + z * a[7]) + a[10]
            sz = s * (x * a[2] + y * a[5] + z * a[8]) + a[11]
        else:
            sx = s * (x * a[0] + y * a[1] + z * a[2]) + a[9]
            sy = s * (x * a[3] + y * a[4] + z * a[5]) + a[10]
            sz = s * (x * a[6] + y * a[7] + z * a[8]) + a[11]
        if abs(sz) > 1e-4:
            raise RuntimeError(f"점 {p} 이 스케치 평면 위에 있지 않습니다 (z'={sz:.4f})")
        return sx, sy

    def _line(self, p1: Vec3, p2: Vec3):
        a, b = self._to_sk(p1), self._to_sk(p2)
        return self.sketch_mgr.CreateLine(a[0], a[1], 0, b[0], b[1], 0)

    def _centerline(self, p1: Vec3, p2: Vec3):
        """구성선(중심선). 회전축이자 대칭 치수의 기준이 된다"""
        a, b = self._to_sk(p1), self._to_sk(p2)
        return self.sketch_mgr.CreateCenterLine(a[0], a[1], 0, b[0], b[1], 0)

    def _fixed_point(self, p: Vec3):
        """스케치 안에 고정된 점을 만들어 치수의 기준으로 쓴다 (용기 중심선, T.L 같은 기준).
        기준면 스케치에서는 원점을 좌표로 선택할 수 없어(실측 확인) 점을 직접 만들어 고정한다.
        점이 고정되어 있으므로 여기서 잰 치수를 고치면 점이 아니라 형상이 움직인다"""
        a = self._to_sk(p)
        pt = self.sketch_mgr.CreatePoint(a[0], a[1], 0)
        if pt is None:
            return None
        try:
            self.model.ClearSelection2(True)
            sd = _call0(self.model.SelectionManager, "CreateSelectData")
            if pt.Select4(False, sd):
                self.model.SketchAddConstraints("sgFIXED")
        except Exception as e:
            print(f"    기준점 고정 생략 ({e})")
        finally:
            self.model.ClearSelection2(True)
        self._sketch_ref = pt
        return pt

    def _polyline(self, points: Sequence[Vec3], close: bool = True) -> list:
        pts = _dedupe(points)
        n = len(pts)
        return [self._line(pts[i], pts[(i + 1) % n]) for i in range(n if close else n - 1)]

    @staticmethod
    def _turn(c, s, m, e) -> int:
        """스케치 2D 에서 s->m->e 가 중심 c 를 반시계로 도는지 (+1) 시계인지 (-1)"""
        cross1 = (s[0] - c[0]) * (m[1] - c[1]) - (s[1] - c[1]) * (m[0] - c[0])
        cross2 = (m[0] - c[0]) * (e[1] - c[1]) - (m[1] - c[1]) * (e[0] - c[0])
        return 1 if cross1 + cross2 > 0 else -1

    def _arc(self, center: Vec3, start: Vec3, end: Vec3, mid: Vec3):
        """center 를 중심으로 start -> (mid 를 지나) -> end 원호"""
        c, s, e, m = self._to_sk(center), self._to_sk(start), self._to_sk(end), self._to_sk(mid)
        return self.sketch_mgr.CreateArc(c[0], c[1], 0, s[0], s[1], 0, e[0], e[1], 0, self._turn(c, s, m, e))

    def _ellipse_arc(self, center: Vec3, major: Vec3, minor: Vec3, start: Vec3, end: Vec3, mid: Vec3):
        c, mj, mn = self._to_sk(center), self._to_sk(major), self._to_sk(minor)
        s, e, m = self._to_sk(start), self._to_sk(end), self._to_sk(mid)
        return self.sketch_mgr.CreateEllipticalArc(
            c[0], c[1], 0, mj[0], mj[1], 0, mn[0], mn[1], 0, s[0], s[1], 0, e[0], e[1], 0, self._turn(c, s, m, e)
        )

    def _circle(self, center: Vec3, r_m: float):
        c = self._to_sk(center)
        return self.sketch_mgr.CreateCircleByRadius(c[0], c[1], 0, r_m)

    # ------------------------------------------------------------------
    # 피처
    def _sel_info(self) -> str:
        """선택 상태와 활성 스케치 요약 (피처가 왜 안 만들어졌는지 보려고)"""
        try:
            sm = self.model.SelectionManager
            n = sm.GetSelectedObjectCount2(-1)
            parts = [f"종류{sm.GetSelectedObjectType3(i, -1)}/표시{sm.GetSelectedObjectMark(i)}"
                     for i in range(1, n + 1)]
            sk = self.sketch_mgr.ActiveSketch
            segn = len(_call0(sk, "GetSketchSegments") or []) if sk is not None else -1
            return f"선택 {n} 개 [{', '.join(parts)}], 활성 스케치 요소 {segn}"
        except Exception as e:
            return f"선택 정보 확인 실패 ({e})"

    def _revolve(self, axis, name: str, cut: bool = False) -> None:
        """활성 스케치의 닫힌 프로파일을 360도 회전. axis 는 축이 될 스케치 선분 객체이거나 그 선 위의 모델 좌표.
        좌표로 고르면 같은 자리를 지나는 다른 스케치의 선이 잡힐 수 있으므로 가능하면 객체를 넘긴다"""
        self.model.ClearSelection2(True)
        if isinstance(axis, tuple):
            ok = self.model.Extension.SelectByID2("", "SKETCHSEGMENT", axis[0], axis[1], axis[2],
                                                  False, 16, _nothing(), 0)
        else:
            sd = _call0(self.model.SelectionManager, "CreateSelectData")
            try:
                sd.Mark = 16
            except Exception:
                pass
            ok = bool(axis is not None and axis.Select4(False, sd))
        if not ok:
            raise RuntimeError(f"{name}: 회전축 선 선택 실패 {axis if isinstance(axis, tuple) else ''}")
        feat = self.feat_mgr.FeatureRevolve2(
            True, True, False, cut, False, False,
            0, 0, 2 * math.pi, 0,
            False, False, 0, 0,
            0, 0, 0,
            True, True, True,
        )
        if feat is None:
            raise RuntimeError(f"{name}: 회전 피처 생성 실패 (스케치 프로파일/회전축 확인. {self._sel_info()})")
        feat.Name = name
        self.feature_count += 1
        self.model.ClearSelection2(True)
        self._frame_view()

    def _extrude_midplane(self, depth_m: float, name: str) -> None:
        feat = self.feat_mgr.FeatureExtrusion3(
            True, False, False,
            swEndCondMidPlane, swEndCondBlind,
            depth_m, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False,
            True, True, True,
            0, 0.0, False,
        )
        if feat is None:
            raise RuntimeError(f"{name} 돌출 실패")
        feat.Name = name
        self.feature_count += 1
        self.model.ClearSelection2(True)
        self._frame_view()

    # ------------------------------------------------------------------
    def build_shell(self, spec: VesselSpec) -> None:
        """쉘 + 양쪽 경판을 하나의 회전 피처로 생성. 두께가 있으면 속이 빈 단면(외면+내면) 으로 회전"""
        R_o = spec.outer_radius * MM
        L = spec.tl_length * MM
        hollow = spec.shell_thickness > 0
        R_i = spec.inner_radius * MM if hollow else 0.0
        Ro_h, Ri_h = spec.head_outer_r * MM, spec.head_inner_r * MM
        D_o, D_i = spec.head_depth_outer * MM, spec.head_depth_inner * MM
        print(f"쉘 회전체: R_o={R_o:.4f} R_i={R_i:.4f} L={L:.4f} m, 경판 {spec.head_type} "
              f"R={Ro_h:.4f}/{Ri_h:.4f} 깊이={D_o:.4f}/{D_i:.4f} m, {'중공' if hollow else '솔리드'}")

        self._begin_sketch("front")
        hemi = spec.is_hemi

        def head_arc(cx: float, r: float, top: Vec3, apex: Vec3, from_apex: bool):
            """중심 (cx,0), 상단점 top=(cx,r), 정점 apex 사이의 경판 호 (반구=원호, 2:1=타원호)"""
            mid = (cx + (apex[0] - cx) * math.cos(math.pi / 4), r * math.sin(math.pi / 4), 0.0)
            s, e = (apex, top) if from_apex else (top, apex)
            if hemi:
                self._arc((cx, 0, 0), s, e, mid)
            else:
                self._ellipse_arc((cx, 0, 0), (cx, r, 0), apex, s, e, mid)

        taper_o = 3 * abs(R_o - Ro_h)
        A = (-D_o, 0.0, 0.0)
        B = (0.0, Ro_h, 0.0)
        C = (taper_o, R_o, 0.0)
        D = (L - taper_o, R_o, 0.0)
        E = (L, Ro_h, 0.0)
        F = (L + D_o, 0.0, 0.0)
        head_arc(0.0, Ro_h, B, A, from_apex=True)      # 좌측 경판 외면: A -> B
        outer_segs = self._polyline([B, C, D, E], close=False)   # 외면 상부
        head_arc(L, Ro_h, E, F, from_apex=False)        # 우측 경판 외면: E -> F
        if hollow:
            taper_i = 3 * abs(R_i - Ri_h)
            G = (L + D_i, 0.0, 0.0)
            H = (L, Ri_h, 0.0)
            I = (L - taper_i, R_i, 0.0)
            J = (taper_i, R_i, 0.0)
            K = (0.0, Ri_h, 0.0)
            M = (-D_i, 0.0, 0.0)
            self._line(F, G)
            head_arc(L, Ri_h, H, G, from_apex=True)     # 우측 경판 내면: G -> H
            inner_segs = self._polyline([H, I, J, K], close=False)
            head_arc(0.0, Ri_h, K, M, from_apex=False)  # 좌측 경판 내면: K -> M
            axis_seg = self._line(M, A)
            axis_pt = (-(D_i + D_o) / 2, 0.0, 0.0)
        else:
            inner_segs = []
            axis_seg = self._line(F, A)
            axis_pt = (L / 2, 0.0, 0.0)
        # 치수: 축 중심선 건너편에 글자를 놓아 도면과 같은 지름(O.D / I.D)으로 읽히게 하고, 접선점 사이로 T.L 을 넣는다
        cl = (self._centerline((-D_i + 0.01, 0.0, 0.0), (L + D_i - 0.01, 0.0, 0.0)) if hollow
              else self._centerline((L + D_o + 0.05, 0.0, 0.0), (L + D_o + 0.4, 0.0, 0.0)))
        x_mid = (C[0] + D[0]) / 2
        if outer_segs and len(outer_segs) > 1:
            self._dim(cl, outer_segs[1], (x_mid, -(R_o + 0.3), 0.0), "쉘 외경", tag="SHELL_OD", dia=True,
                      expect=2 * R_o / MM, prefix="O.D")
        if inner_segs and len(inner_segs) > 1:
            self._dim(cl, inner_segs[1], (x_mid + 1.0, -(R_i + 0.9), 0.0), "쉘 내경", tag="SHELL_ID", dia=True,
                      expect=2 * R_i / MM, prefix="I.D")
        if outer_segs and len(outer_segs) > 2:      # 접선점 B, E = 바깥 폴리선의 시작점과 끝점
            self._dim(_call0(outer_segs[0], "GetStartPoint2"), _call0(outer_segs[2], "GetEndPoint2"),
                      (x_mid, R_o + 0.6, 0.0), "T.L 길이", tag="TL_LENGTH", expect=L / MM)
        self._end_sketch()
        self._revolve(axis_seg if axis_seg is not None else axis_pt, "Shell+Heads")

    # ------------------------------------------------------------------
    def build_saddles(self, spec: VesselSpec) -> None:
        positions = spec.saddle_positions_from_tl()
        if not positions:
            print("새들 위치 정보 없음 - 건너뜀")
            return
        if spec.saddle_base_t > 0 and spec.saddle_web_t > 0:
            self._build_plate_saddles(spec, positions)
            return
        R = spec.outer_radius
        H = spec.centerline_height if spec.centerline_height > R else R + 500.0
        web = spec.saddle_web_thickness
        W = spec.saddle_base_width if spec.saddle_base_width > 0 else spec.outer_diameter * 0.9
        phi = min(math.radians(60), math.asin(min(W / 2, R * 0.999) / R))
        y_h = -R * math.cos(phi)
        for i, xc in enumerate(positions, start=1):
            print(f"새들 {i}: x={xc:g} mm, 바닥폭 {W:g} mm, 높이 {H - R:g} mm, 감싸는 각 ±{math.degrees(phi):.0f}°")
            plane = self._offset_plane(xc)
            self._begin_sketch(plane)
            x = xc * MM
            A = (x, -H * MM, -W / 2 * MM)
            B = (x, -H * MM, W / 2 * MM)
            C = (x, y_h * MM, W / 2 * MM)
            E1 = (x, y_h * MM, R * math.sin(phi) * MM)
            E2 = (x, y_h * MM, -R * math.sin(phi) * MM)
            Dp = (x, y_h * MM, -W / 2 * MM)
            self._polyline([A, B, C, E1], close=False)
            self._arc((x, 0.0, 0.0), E1, E2, (x, -R * MM, 0.0))
            self._polyline([E2, Dp, A], close=False)
            self._end_sketch()
            self._extrude_midplane(web * MM, f"Saddle_{i}")

    def _build_plate_saddles(self, spec: VesselSpec, positions) -> None:
        """판 구성 새들: 베이스(폭 W x 축 L x t_b) + 웹(축 직각 판, 쉘까지) + 리브(축 방향 판, z 위치별) + 서포트(웨어) 플레이트(호)"""
        R = spec.outer_radius
        H = spec.centerline_height if spec.centerline_height > R else R + 500.0
        W = spec.saddle_base_width if spec.saddle_base_width > 0 else spec.outer_diameter * 0.9
        Lb = spec.saddle_length if spec.saddle_length > 0 else spec.saddle_web_thickness
        tb, tw, tr, tp = spec.saddle_base_t, spec.saddle_web_t, spec.saddle_rib_t, spec.saddle_wear_t
        Rw = R + tp                                   # 웨어 플레이트 바깥 반경
        wl = spec.saddle_web_length if spec.saddle_web_length > 0 else W
        wrap = math.radians(spec.saddle_wrap_deg / 2) if spec.saddle_wrap_deg > 0 else math.asin(min(wl / 2, R * 0.999) / R)
        # 웹 판 뿔(horn): 새들 뷰의 작은 각(140°). 옆 변은 베이스 위 모서리(±wl/2)에서 웨어 플레이트 바깥면의 뿔 점까지
        # 직선 -> 위가 넓은 부채꼴 (0004 2/2 FIXED SIDE SADDLE 의 옆 판 벡터: 아래 ±1609, 위 ±1799 @ y −648 = R_w·(sin70°, −cos70°))
        wrap_web = math.radians(spec.saddle_web_wrap_deg / 2) if spec.saddle_web_wrap_deg > 0 else wrap
        y_base_top = -H + tb
        for i, xc in enumerate(positions, start=1):
            print(f"새들 {i}: x={xc:g} mm, 베이스 {W:g}x{Lb:g}xt{tb:g}, 웹 t{tw:g} 폭 {wl:g}, 리브 t{tr:g} x{len(spec.saddle_rib_z)}, "
                  f"서포트 t{tp:g} {math.degrees(2 * wrap):.0f}°")
            x = xc * MM
            plane = self._offset_plane(xc)
            # 베이스 플레이트
            self._begin_sketch(plane)
            bsegs = self._polyline([(x, -H * MM, -W / 2 * MM), (x, -H * MM, W / 2 * MM),
                                    (x, y_base_top * MM, W / 2 * MM), (x, y_base_top * MM, -W / 2 * MM)])
            if len(bsegs) >= 4:
                self._dim(bsegs[1], bsegs[3], (x, (-H + tb / 2) * MM, 0.0), f"새들 {i} 베이스 폭",
                          tag=f"SADDLE{i}_BASE_W", expect=W)
                self._dim(bsegs[0], bsegs[2], (x, (-H + tb / 2) * MM, (W / 2 + 200) * MM), f"새들 {i} 베이스 두께",
                          tag=f"SADDLE{i}_BASE_T", expect=tb)
            self._end_sketch()
            self._extrude_midplane(Lb * MM, f"SaddleBase_{i}")
            # 웹(서포트) 판: 베이스 위 모서리 -> 뿔 점(웨어 플레이트 바깥면, ±wrap_web) 직선, 뿔 사이는 호.
            # 부품표에 새들 한 대당 두 장이면 축 방향으로 간격만큼 벌려 양쪽에 세운다 (0004 2/2 SECTION "B": 480)
            phi_w = min(wrap_web, wrap)
            gap = spec.saddle_web_spacing
            offs = [-gap / 2, gap / 2] if gap > tw else [0.0]
            for j, off in enumerate(offs, start=1):
                xw = (xc + off) * MM
                plane_w = plane if off == 0.0 else self._offset_plane(xc + off)
                E1 = (xw, -Rw * math.cos(phi_w) * MM, Rw * math.sin(phi_w) * MM)
                E2 = (xw, -Rw * math.cos(phi_w) * MM, -Rw * math.sin(phi_w) * MM)
                self._begin_sketch(plane_w)
                self._polyline([(xw, y_base_top * MM, -wl / 2 * MM), (xw, y_base_top * MM, wl / 2 * MM), E1], close=False)
                self._arc((xw, 0.0, 0.0), E1, E2, (xw, -Rw * MM, 0.0))
                self._polyline([E2, (xw, y_base_top * MM, -wl / 2 * MM)], close=False)
                self._end_sketch()
                self._extrude_midplane(tw * MM, f"SaddleWeb_{i}" + (f"_{j}" if len(offs) > 1 else ""))
            # 서포트(웨어) 플레이트: 쉘 외면 R ~ Rw, ±wrap
            if tp > 0:
                P1 = (x, -R * math.cos(wrap) * MM, R * math.sin(wrap) * MM)
                P2 = (x, -Rw * math.cos(wrap) * MM, Rw * math.sin(wrap) * MM)
                P3 = (x, -Rw * math.cos(wrap) * MM, -Rw * math.sin(wrap) * MM)
                P4 = (x, -R * math.cos(wrap) * MM, -R * math.sin(wrap) * MM)
                self._begin_sketch(plane)
                self._line(P1, P2)
                self._arc((x, 0.0, 0.0), P2, P3, (x, -Rw * MM, 0.0))
                self._line(P3, P4)
                self._arc((x, 0.0, 0.0), P4, P1, (x, -R * MM, 0.0))
                self._end_sketch()
                self._extrude_midplane(Lb * MM, f"SaddleWear_{i}")
            # 리브 플레이트: z 위치마다 XY 평면(정면 오프셋)에 사각형, 베이스 위 ~ 웨어 플레이트 아래
            if tr > 0 and spec.saddle_rib_z:
                half = (Lb / 2 - spec.saddle_rib_margin) * MM
                for zr in spec.saddle_rib_z:
                    if abs(zr) >= Rw * math.sin(wrap):
                        continue
                    y_top = -math.sqrt(Rw * Rw - zr * zr) + 5.0            # 5mm 겹치게
                    pz = self._offset_plane(zr, base="front")
                    self._begin_sketch(pz)
                    z = zr * MM
                    self._polyline([(x - half, y_base_top * MM, z), (x + half, y_base_top * MM, z),
                                    (x + half, y_top * MM, z), (x - half, y_top * MM, z)])
                    self._end_sketch()
                    self._extrude_midplane(tr * MM, f"SaddleRib_{i}_{zr:g}")

    def build_lugs(self, spec: VesselSpec) -> None:
        positions = spec.lug_positions_from_tl()
        if not positions:
            print("러그 위치 정보 없음 - 건너뜀")
            return
        R = spec.outer_radius
        t_s = spec.shell_thickness if spec.shell_thickness > 0 else 40.0
        w, hgt, t, hole_d = spec.lug_width, spec.lug_height, spec.lug_thickness, spec.lug_hole_diameter
        angles = spec.lug_angles_deg or [0.0]
        k = 0
        for xc in positions:
            plane = self._offset_plane(xc)
            for ang in angles:
                k += 1
                print(f"러그 {k}: x={xc:g} mm, 각도 {ang:g}°")
                th = math.radians(ang)
                d = (math.cos(th), math.sin(th))      # 반경 방향 (y, z)
                n = (-math.sin(th), math.cos(th))     # 접선 방향

                def pt(s: float, u: float, x=xc * MM) -> Vec3:
                    return (x, (s * d[0] + u * n[0]) * MM, (s * d[1] + u * n[1]) * MM)

                s0, s1 = R - t_s / 2, R + hgt
                self._begin_sketch(plane)
                self._polyline([pt(s0, -w / 2), pt(s1, -w / 2), pt(s1, w / 2), pt(s0, w / 2)])
                self._circle(pt(s1 - hole_d, 0.0), hole_d / 2 * MM)
                self._end_sketch()
                self._extrude_midplane(t * MM, f"LiftingLug_{k}")

    # ------------------------------------------------------------------
    def build_nozzles(self, spec: VesselSpec, only: Optional[Sequence[str]] = None) -> None:
        ready = [n for n in spec.nozzles if n.is_ready() and (not only or n.mark in only)]
        if not ready:
            print("모델링할 노즐 없음")
            return
        print(f"노즐 {len(ready)} 개 모델링 시작")
        R_i = spec.inner_radius if spec.shell_thickness > 0 else spec.outer_radius
        L = spec.tl_length
        for idx, nz in enumerate(ready, start=1):
            try:
                self._build_nozzle(spec, nz, R_i, L)
                print(f"  [{idx}/{len(ready)}] {nz.mark} 완료")
            except Exception as e:
                print(f"  [{idx}/{len(ready)}] {nz.mark} 실패: {e}")
                try:
                    # 실패한 스케치가 열려 있으면 닫는다
                    self.sketch_mgr.AddToDB = False
                    if self.sketch_mgr.ActiveSketch is not None:
                        self.sketch_mgr.InsertSketch(True)
                except Exception:
                    pass
        if spec.shell_thickness > 0:
            try:
                self._trim_inside(spec)
            except Exception as e:
                print(f"  내면 트림 생략 ({e})")
                self._abandon_sketch()

    def _trim_inside(self, spec: VesselSpec) -> None:
        """용기 내면(쉘 내면 + 경판 내면)으로 회전 컷: 목이 내면 안쪽으로 튀어나온 부분을 내면과 같은 높이로 자른다
        (set-through 노즐, 상세도의 목 끝은 내면 모서리와 일치: N8 792 = 아래 모서리의 경판 내면). 용기 안은 원래 비어 있어
        노즐 목 외에는 영향이 없다. 안쪽 돌출(50 등)은 현재 모델링하지 않는다"""
        L = spec.tl_length * MM
        R_i, Ri_h, D_i = spec.inner_radius * MM, spec.head_inner_r * MM, spec.head_depth_inner * MM
        taper_i = 3 * abs(R_i - Ri_h)
        hemi = spec.is_hemi
        G, Hh, I, J, K, M = (L + D_i, 0.0, 0.0), (L, Ri_h, 0.0), (L - taper_i, R_i, 0.0), (taper_i, R_i, 0.0), (0.0, Ri_h, 0.0), (-D_i, 0.0, 0.0)

        def head_arc(cx, r, top, apex, from_apex):
            mid = (cx + (apex[0] - cx) * math.cos(math.pi / 4), r * math.sin(math.pi / 4), 0.0)
            s, e = (apex, top) if from_apex else (top, apex)
            if hemi:
                self._arc((cx, 0, 0), s, e, mid)
            else:
                self._ellipse_arc((cx, 0, 0), (cx, r, 0), apex, s, e, mid)

        self._begin_sketch("front")
        head_arc(0.0, Ri_h, K, M, from_apex=True)      # M -> K (좌측 경판 내면)
        self._polyline([K, J, I, Hh], close=False)
        head_arc(L, Ri_h, Hh, G, from_apex=False)      # H -> G (우측 경판 내면)
        axis_ln = self._line(G, M)                      # 축
        self._end_sketch()
        self._revolve(axis_ln, "InnerTrim", cut=True)
        print("  내면 트림: 노즐 목의 안쪽 돌출을 내면 높이로 잘랐음")

    @staticmethod
    def _outline_from_profile(nz: NozzleSpec, s0: float, P: float):
        """상세도 외곽선(플랜지 면부터 s, 반경 r) -> 회전용 닫힌 단면 [(s, r)].
        플랜지 면은 s = P, 쉘 내면(s0) 안쪽은 마지막 반경으로 이어 붙인다. 점이 부족하면 None."""
        pts = nz.profile
        if not pts or len(pts) < 3:
            return None
        outer = [(P - float(s_off), float(r)) for s_off, r in pts]          # 플랜지 면 -> 쉘 쪽 (s 감소)
        # 플랜지 면(RF)은 평평해야 볼트 구멍을 뚫을 면이 생긴다. 면 바로 뒤 짧은 구간에서 반경이 크게 뛰면
        # (도면에서 읽는 방식에 따라 중간점이 빠질 수 있다) 계단으로 펴 준다: 평면 + 원통 벽 + 링 면
        if len(outer) >= 2:
            (s_a, r_a), (s_b, r_b) = outer[0], outer[1]
            if 0 < s_a - s_b <= 3.0 and r_b - r_a > 5.0:
                outer.insert(1, (s_b, r_a))
        body = [(s, r) for s, r in outer if s > s0 + 0.5]
        if len(body) < 2 or max(r for _, r in body) <= 0:
            return None
        r_last = body[-1][1]
        # 기본 단면과 같은 순서(축 -> 플랜지 면 -> 바깥 -> 쉘)로 만든다. 반대 순서는 SolidWorks 회전이 실패했다(N7/N30)
        poly = [(s0, 0.0), (P, 0.0)] + list(body) + [(s0, r_last)]
        # 0.3mm 이내로 붙은 점은 합침 (스케치 미세 선분 방지)
        out = []
        for p in poly:
            if out and abs(out[-1][0] - p[0]) < 0.3 and abs(out[-1][1] - p[1]) < 0.3:
                continue
            out.append(p)
        if out[0] != (s0, 0.0):
            out.insert(0, (s0, 0.0))
        # 일직선 위의 중간점 제거 (같은 직선의 선분이 이어지면 SolidWorks 회전이 윤곽을 못 잡음 - N7/N30 사례)
        clean = [out[0]]
        for p in out[1:]:
            if len(clean) >= 2:
                a, b = clean[-2], clean[-1]
                cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
                if abs(cross) < 1e-6 * max(1.0, abs(p[0] - a[0]) + abs(p[1] - a[1])):
                    clean[-1] = p
                    continue
            clean.append(p)
        return clean if len(clean) >= 4 else None

    def _build_nozzle(self, spec: VesselSpec, nz: NozzleSpec, R_i: float, L: float) -> None:
        r_o = nz.neck_od / 2
        r_i = nz.neck_id / 2 if nz.neck_id > 0 else r_o * 0.8
        P = float(nz.projection)
        mk = nz.mark.replace("-", "_").replace(" ", "")      # 치수 이름에 쓸 마크 (기호 없이)
        fl_od, fl_t = nz.flange
        R_f = fl_od / 2

        if nz.on_head:
            # 경판 노즐: 축이 X 방향, y = offset_y. 정면 스케치. s = T.L 에서 축방향 거리
            off = nz.offset_y
            Ri_h, D_i = spec.head_inner_r, spec.head_depth_inner
            if abs(off) >= Ri_h:
                raise ValueError(f"오프셋 {off} 이 경판 반경보다 큼")
            s0 = D_i * math.sqrt(1 - (off / Ri_h) ** 2)          # 축선상 경판 내면까지 거리
            sign = -1.0 if nz.on_head == "left" else 1.0
            x_base = 0.0 if nz.on_head == "left" else L

            def to_model(s: float, r: float) -> Vec3:
                return ((x_base + sign * s) * MM, (off + r) * MM, 0.0)

            def s_inner(r: float, off=off, Ri_h=Ri_h, D_i=D_i) -> Optional[float]:
                """반경 r 의 목 둘레에서 경판 내면이 가장 깊은(축 방향 s 가 가장 작은) 곳: 오프셋 반대쪽 모서리 |off|+r"""
                q = 1 - ((abs(off) + r) / Ri_h) ** 2
                return D_i * math.sqrt(q) if q > 0 else None

            plane = "front"
        else:
            x0 = float(nz.position_from_tl)
            th = math.radians(float(nz.angle_deg))
            d = (math.cos(th), math.sin(th))          # (y, z)
            n = (-math.sin(th), math.cos(th))
            o = (nz.offset_y, nz.offset_z)
            od = o[0] * d[0] + o[1] * d[1]
            disc = od * od - (o[0] ** 2 + o[1] ** 2) + R_i ** 2
            if disc <= 0:
                raise ValueError(f"오프셋 {o} 이 쉘 반경 밖")
            s0 = -od + math.sqrt(disc)                 # 축선상 쉘 내면까지 거리
            on = o[0] * n[0] + o[1] * n[1]             # 축에 수직한 오프셋 성분

            def to_model(s: float, r: float, x0=x0, d=d, n=n, o=o) -> Vec3:
                return (x0 * MM, (o[0] + s * d[0] + r * n[0]) * MM, (o[1] + s * d[1] + r * n[1]) * MM)

            def s_inner(r: float, od=od, on=on, R_i=R_i) -> Optional[float]:
                """반경 r 의 목 둘레에서 쉘 내면이 가장 깊은 곳: 수직 오프셋 방향 모서리 |on|+r (원통이라 x 성분은 무관)"""
                q = R_i * R_i - (abs(on) + r) ** 2
                return -od + math.sqrt(q) if q > 0 else None

            plane = self._offset_plane(x0)

        if P <= s0 + 5:
            raise ValueError(f"투영 {P} 이 쉘 내면 거리 {s0:.0f} 보다 작음")
        # 목의 안쪽 끝: 축선상 내면(s0)이 아니라 목 둘레에서 내면이 가장 깊은 곳(s_inner(r))까지 넣는다. 경판의 오프셋
        # 노즐은 둘레에 따라 내면 위치가 크게 달라(N8: 축 1730 vs 아래 모서리 1493) 축 기준으로 자르면 아래쪽이 경판에
        # 못 미쳐 떠 있었다. 안쪽으로 튀어나온 부분은 노즐을 다 만든 뒤 내면 회전 컷(_trim_inside)으로 내면과 같은 높이로 자른다
        r_last = float(nz.profile[-1][1]) if nz.profile else r_o
        s_end = s_inner(max(r_last, r_o))
        s_end = (s_end - 5.0) if s_end is not None else s0
        s_bore = s_inner(r_i)
        s_bore = (s_bore - 20.0) if s_bore is not None else (s0 - min(100.0, s0 * 0.5))

        # 1) 보스: 상세도 외곽선이 있으면 그 실루엣(플랜지 면 기준 s, r)을, 없으면 목(반경 r_o) + 표준 플랜지 원판
        if R_f > r_o + 1:
            simple = [(s_end, 0), (P, 0), (P, R_f), (P - fl_t, R_f), (P - fl_t, r_o), (s_end, r_o)]
        else:
            simple = [(s_end, 0), (P, 0), (P, r_o), (s_end, r_o)]
        outline = self._outline_from_profile(nz, s_end, P)
        # 시도 순서: 상세도 실루엣(치수 포함) -> 같은 단면을 치수 없이 -> 기본 단면(치수 포함) -> 기본 단면을 치수 없이.
        # 치수용 중심선·기준점이 회전 피처를 막는 경우가 있어, 막히면 치수를 빼고라도 형상은 반드시 만든다
        attempts = [(prof, label, wd)
                    for prof, label in ((outline, "상세도 실루엣"), (simple, "균일 목+표준 플랜지"))
                    if prof is not None
                    for wd in (True, False)]
        for i_try, (prof, label, with_dims) in enumerate(attempts):
            self._begin_sketch(plane)
            segs = self._polyline([to_model(s, r) for s, r in prof])
            if with_dims:
                # 지름 치수의 기준이 되는 중심선(이것이 있어야 Diametric 으로 도면과 같은 Ø 값이 된다).
                # 단면의 축 변과 겹치지 않게 플랜지 면 바깥에 둔다
                axis_cl = self._centerline(to_model(P + 15.0, 0.0), to_model(P + 150.0, 0.0))
                # 치수 기준점: 용기 중심선 위의 점(경판 노즐은 그 경판의 T.L 면). 여기서 플랜지 면까지가 도면의 투영,
                # 여기서 노즐 축선까지가 축 오프셋. 부품 치수는 플랜지 외경과 목 외경
                ref_x = (0.0 if nz.on_head == "left" else L * MM) if nz.on_head else (x0 * MM)
                org = self._fixed_point((ref_x, 0.0, 0.0))
                if len(segs) > 1:
                    self._dim(org, segs[1], to_model(P + 60.0, 60.0), f"{nz.mark} 투영(C.L 기준)", tag=f"{mk}_PROJ",
                              suffix=("TO T.L" if nz.on_head else "TO C.L"), expect=P)
                off_mag = abs(nz.offset_y if nz.on_head else (nz.offset_y * math.sin(math.radians(float(nz.angle_deg)))
                                                             - nz.offset_z * math.cos(math.radians(float(nz.angle_deg)))))
                if segs and off_mag > 1.0:
                    self._dim(org, segs[0], to_model((s_end + P) / 2, -60.0), f"{nz.mark} 축 오프셋",
                              tag=f"{mk}_OFFSET", expect=off_mag)
                n_pt = len(prof)
                par = [i for i in range(n_pt) if abs(prof[i][1] - prof[(i + 1) % n_pt][1]) < 0.3 and prof[i][1] > 0.5]
                if segs and par:
                    # 플랜지 외경은 '가장 큰 반경'이 아니라 표준 플랜지 반경에 가장 가까운 단으로 고른다.
                    # N8 맨홀은 목 Ø920 이 플랜지 Ø867 보다 굵어, 최대 반경으로 고르면 목을 플랜지로 표시하게 된다
                    i_f = (min(par, key=lambda i: abs(prof[i][1] - R_f)) if R_f > 1
                           else max(par, key=lambda i: prof[i][1]))
                    i_n = max(par, key=lambda i: abs(prof[i][0] - prof[(i + 1) % n_pt][0]))
                    for idx, dlabel, nm, near in ((i_f, "플랜지 외경", "FLG_OD", R_f), (i_n, "목 외경", "NECK_OD", r_o)):
                        if idx >= len(segs) or idx == 0:
                            continue
                        # 도면에서 읽은 선은 축과 0.1mm 정도 기울어 있을 수 있고, 그러면 선 기준 치수가 각도 치수로
                        # 만들어진다(실측). 그래서 반지름이 도면 값에 가까운 쪽 '끝점'을 기준으로 지름을 잡는다
                        r1, r2 = prof[idx][1], prof[(idx + 1) % n_pt][1]
                        first = abs(r1 - near) <= abs(r2 - near)
                        pt = _call0(segs[idx], "GetStartPoint2" if first else "GetEndPoint2")
                        s_mid = (prof[idx][0] + prof[(idx + 1) % n_pt][0]) / 2
                        self._dim(axis_cl, pt, to_model(s_mid, -(prof[idx][1] + 40.0)),
                                  f"{nz.mark} {dlabel}", tag=f"{mk}_{nm}", dia=True,
                                  expect=2 * (r1 if first else r2))
            self._end_sketch()
            try:
                self._revolve(segs[0] if segs else to_model((s0 + P) / 2, 0), f"Nozzle_{nz.mark}")
                if not with_dims:
                    print(f"    {nz.mark}: 치수를 넣으면 회전이 실패해 치수 없이 만들었음 ({label})")
                break
            except RuntimeError as e:
                if i_try == len(attempts) - 1:
                    raise
                print(f"    {nz.mark}: {label}{'' if with_dims else ' (치수 없이)'} 회전 실패 -> 다음 시도 ({e}); "
                      f"단면 {[(round(s), round(r, 1)) for s, r in prof]}")
                # 실패하면 스케치가 편집 상태로 남아 다음 InsertSketch 가 '나가기' 로 동작함 -> 먼저 빠져나온 뒤 지운다
                self._abandon_sketch()
                self._drop_last_sketch()

        # 2) 보어: 반경 r_i, 내면이 가장 깊은 곳보다 20mm 안쪽(s_bore) 에서 플랜지 면 너머까지 회전 컷 (벽을 둘레 전체에서 관통)
        self._begin_sketch(plane)
        bsegs = self._polyline([to_model(s_bore, 0), to_model(P + 1, 0), to_model(P + 1, r_i), to_model(s_bore, r_i)])
        bore_cl = self._centerline(to_model(P + 15.0, 0), to_model(P + 150.0, 0))
        if len(bsegs) > 2:          # 도면의 보어 지름 (Ø)
            self._dim(bore_cl, _call0(bsegs[2], "GetStartPoint2"), to_model((s_bore + P) / 2, -(r_i + 40.0)),
                      f"{nz.mark} 보어", tag=f"{mk}_BORE", dia=True, expect=2 * r_i)
        self._end_sketch()
        self._revolve(bsegs[0] if bsegs else to_model((s_bore + P + 1) / 2, 0), f"Bore_{nz.mark}", cut=True)

        # 3) 블라인드 플랜지: RF 면 위에 개스킷 링(외경 = RF 지름, 내경 = 보어, 부품표 두께) 을 놓고 그 위에
        #    플랜지와 같은 표준(B16.5 / B16.47 Series)의 외경·두께로 원판을 만든다. 서로 닿아 있어 한 몸체로 합쳐진다
        #    (상세도에서 블라인드를 띄워 그린 거리는 조립 위치가 아니므로 쓰지 않는다)
        extra = 0.0
        if nz.blind and nz.blind_t > 0:
            g = nz.gasket_t if nz.gasket_t > 0 else 3.0
            fd = flange_cl300(nz.size_in, "A" if nz.flange_std.upper().endswith("A") else "") if nz.size_in > 0 else None
            R_b = fd.od / 2 if fd and fd.od > 0 else R_f
            r_rf = fd.raised_face_d / 2 if fd and fd.raised_face_d > 0 else (nz.profile[0][1] if nz.profile else 0.85 * R_b)
            r_rf = max(min(r_rf, R_b - 2.0), r_i + 2.0)
            gk = [(P, r_i), (P + g, r_i), (P + g, r_rf), (P, r_rf)]
            try:
                self._begin_sketch(plane)
                self._polyline([to_model(s_, r_) for s_, r_ in gk])
                # 링 단면은 축 위에 선이 없으므로 회전축 중심선을 따로 그린다 (좌표가 아니라 그 객체로 축을 지정)
                gcl = self._centerline(to_model(P - 5.0, 0), to_model(P + g + 5.0, 0))
                self._end_sketch()
                self._revolve(gcl, f"Gasket_{nz.mark}")
            except Exception as e:
                print(f"    {nz.mark}: 개스킷 생략 ({e})")
                self._abandon_sketch()
            bl = [(P + g, 0.0), (P + g + nz.blind_t, 0.0), (P + g + nz.blind_t, R_b), (P + g, R_b)]
            self._begin_sketch(plane)
            blsegs = self._polyline([to_model(s_, r_) for s_, r_ in bl])
            self._end_sketch()
            self._revolve(blsegs[0] if blsegs else to_model(P + g + nz.blind_t / 2, 0), f"Blind_{nz.mark}")
            extra = g + nz.blind_t
        # 4) 볼트 구멍: ASME 표(볼트 원, 수, 구멍 지름)대로 플랜지 링 면에 원을 그려 양방향 블라인드 컷 (블라인드까지 관통)
        if nz.n_bolts > 0 and nz.bolt_circle > 0 and nz.bolt_hole_d > 0:
            try:
                self._bolt_holes(nz, spec, P, L, fl_t, extra)
            except Exception as e:
                print(f"    {nz.mark}: 볼트 구멍 생략 ({e})")
                self._abandon_sketch()
        # 5) 지지판(거싯): 목에서 쉘로 내려가는 판 n 장, 노즐 축 둘레 방위각대로
        if nz.support_n > 0 and nz.support_t > 0 and nz.support_len > 0:
            try:
                self._support_plates(nz, spec, P, L)
            except Exception as e:
                print(f"    {nz.mark}: 지지판 생략 ({e})")
                self._abandon_sketch()

    def _abandon_sketch(self) -> None:
        """실패한 작업이 스케치를 편집 상태로 남기면 다음 InsertSketch 가 '나가기' 로 동작하므로 먼저 빠져나온다"""
        try:
            if self.sketch_mgr.ActiveSketch is not None:
                self.sketch_mgr.InsertSketch(True)
        except Exception:
            pass
        self.model.ClearSelection2(True)

    def _drop_last_sketch(self) -> None:
        """실패한 시도가 남긴 스케치를 지운다 (피처 트리와 화면 범위가 지저분해지지 않도록)"""
        try:
            last = self._last_feature()
            if last is None or last.GetTypeName2() != "ProfileFeature":
                return
            self.model.ClearSelection2(True)
            if self.model.Extension.SelectByID2(last.Name, "SKETCH", 0, 0, 0, False, 0, _nothing(), 0):
                self.model.Extension.DeleteSelection2(0)
        except Exception:
            pass
        finally:
            self.model.ClearSelection2(True)

    def _nozzle_frame(self, nz: NozzleSpec, L: float):
        """노즐 3D 프레임 (m): 원점, 축 방향 A, 면 안의 U(단면 스케치 평면 안), V(=용기 축 X 또는 Z)"""
        if nz.on_head:
            sign = -1.0 if nz.on_head == "left" else 1.0
            origin = ((0.0 if nz.on_head == "left" else L) * MM, nz.offset_y * MM, 0.0)
            return origin, (sign, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
        th = math.radians(float(nz.angle_deg))
        origin = (float(nz.position_from_tl) * MM, nz.offset_y * MM, nz.offset_z * MM)
        return origin, (0.0, math.cos(th), math.sin(th)), (0.0, -math.sin(th), math.cos(th)), (1.0, 0.0, 0.0)

    def _support_plates(self, nz: NozzleSpec, spec: VesselSpec, P: float, L: float) -> None:
        """지지판(거싯) n 장: 폭 w 두께 t 의 띠판이 목 표면(플랜지 면에서 support_s) 에서 쉘 쪽으로 축과 angle 을
        이루며 내려가 쉘 외면에 닿는다. 판의 평면은 노즐 축을 포함하고, 축 둘레 방위각 az 는 support_az_ref 기준
        ("axis" = 용기 축 +X (보조 뷰 FIXED/SLIDING SIDE, 고정 새들 쪽을 +X 로 가정), "up" = 연직 위 (TOP/BTM)).
        - 상세도의 각도는 판 평면 안의 실제 각도로 본다 (2D 도면 관행: 판을 실형으로 그리고 방위는 보조 뷰로 표시)
        - 길이는 그려진 길이가 아니라 판의 긴 변이 쉘 외면(반경 R_o 원통)에 닿는 거리로 계산한다. 그림의 길이는 목의
          파단선 때문에 실제와 다르기 때문(N7). 교점이 없거나 그림 길이와 2.5배 넘게 다르면 그림 길이를 쓴다
        - 판 모양은 평행사변형: 목 쪽 끝은 목을 따라(축 방향) 잘리고(두 긴 변이 목에 닿는 점은 w/sin a 만큼 떨어짐),
          쉘 쪽 끝은 각 변이 쉘에 닿는 점까지. 목에는 3mm, 쉘에는 min(10, 0.8t) 만큼 묻어 한 몸체로 합쳐지게 한다
        - 스케치는 노즐 x 오프셋 평면에 그린 축 중심선을 회전축으로 만든 기울어진 기준면에 그린다
          (평면 3D 스케치 돌출은 SolidWorks 가 거부하는 경우가 있어 쓰지 않음)"""
        if nz.on_head:
            return
        if not (5.0 < nz.support_angle_deg < 85.0):
            raise RuntimeError(f"지지판 각도 {nz.support_angle_deg:g}° 는 모델링 범위(5~85°) 밖")
        origin, A, U, V = self._nozzle_frame(nz, L)
        a = math.radians(nz.support_angle_deg)
        r_neck = (nz.profile[-1][1] if nz.profile else nz.neck_od / 2)
        wall = max(2.0, (nz.neck_od - (nz.neck_id if nz.neck_id > 0 else nz.neck_od * 0.8)) / 2)
        emb_neck = min(3.0, wall / 2)
        t_sh = spec.shell_thickness if spec.shell_thickness > 0 else 10.0
        emb_shell = min(10.0, 0.8 * t_sh)
        R_o = spec.outer_radius
        s_a = P - nz.support_s                      # 목에 붙는 위치 (축 좌표, 용기 중심선 기준)
        w = nz.support_w if nz.support_w > 0 else 50.0

        def dot(p, q):
            return sum(p[i] * q[i] for i in range(3))

        def cross(p, q):
            return (p[1] * q[2] - p[2] * q[1], p[2] * q[0] - p[0] * q[2], p[0] * q[1] - p[1] * q[0])

        def unit(p):
            n_ = math.sqrt(dot(p, p))
            return tuple(c / n_ for c in p)

        # 방위각 0° 방향 R1: 보조 뷰의 + 기준 문구 방향 u 에서 화면 시계 방향으로 support_az_dir 만큼 돌린 방향.
        #  u: "top" = 연직 위(축에 수직 성분), 그 밖 = 고정 새들 쪽 용기 축(fixed_side, 모르면 +X).
        #  보조 뷰는 노즐 바깥에서 쉘 쪽(-A)으로 본다고 가정 -> 화면 오른쪽 = u x A
        if nz.support_az_ref in ("top", "up"):
            up = (0.0, 1.0, 0.0)
            d_ = dot(up, A)
            u_ = tuple(up[i] - d_ * A[i] for i in range(3))
            u_ = unit(u_) if math.sqrt(dot(u_, u_)) > 0.3 else V     # 축이 연직이면 용기 축 기준
        else:
            u_ = tuple(-c for c in V) if spec.fixed_side == "left" else V
        right = cross(u_, A)
        dd = math.radians(nz.support_az_dir)
        R1 = unit(tuple(math.cos(dd) * u_[i] + math.sin(dd) * right[i] for i in range(3)))
        R2 = cross(A, R1)

        def shell_hit(E, D):
            """E 에서 D 방향으로 쉘 외면(|yz| = R_o) 에 처음 닿는 거리 (mm). 없으면 None"""
            qa = D[1] ** 2 + D[2] ** 2
            qb = 2 * (E[1] * D[1] + E[2] * D[2])
            qc = E[1] ** 2 + E[2] ** 2 - R_o ** 2
            if qa < 1e-12:
                return None
            disc = qb * qb - 4 * qa * qc
            if disc < 0:
                return None
            t = (-qb - math.sqrt(disc)) / (2 * qa)
            return t if t > 1.0 else None

        base_plane = self._offset_plane(float(nz.position_from_tl))
        # 축 중심선 스케치 (기준면의 회전축)
        p1 = tuple(origin[i] + (s_a - 100.0) * A[i] * MM for i in range(3))
        p2 = tuple(origin[i] + (P + 50.0) * A[i] * MM for i in range(3))
        self._begin_sketch(base_plane)
        q1, q2 = self._to_sk(p1), self._to_sk(p2)
        axis_seg = self.sketch_mgr.CreateCenterLine(q1[0], q1[1], 0, q2[0], q2[1], 0)
        self._end_sketch()
        self.sketch_mgr.InsertSketch(True)
        self.model.ClearSelection2(True)
        if axis_seg is None:
            raise RuntimeError("축 중심선 생성 실패")
        origin_mm = tuple(c / MM for c in origin)
        for az_deg in nz.support_azimuths[: nz.support_n]:
            az = math.radians(az_deg)
            W = unit(tuple(math.cos(az) * R1[i] + math.sin(az) * R2[i] for i in range(3)))   # 판 안, 축에 수직
            D = tuple(-math.cos(a) * A[i] + math.sin(a) * W[i] for i in range(3))            # 판 길이 방향 (쉘 쪽)
            want_n = cross(A, W)                                                               # 판 법선
            # 기준면: 축 중심선을 회전축으로, x 오프셋 평면(A,U) 에서 W 와 U 사이 각도만큼 돌린 평면
            ang = math.acos(max(-1.0, min(1.0, abs(dot(W, U)))))
            plane_name = base_plane if ang < math.radians(0.5) else None
            for flip in ((False, True) if plane_name is None else ()):
                self.model.ClearSelection2(True)
                self.model.Extension.SelectByID2(base_plane, "PLANE", 0, 0, 0, False, 0, _nothing(), 0)
                # 좌표 선택은 같은 x 평면에 있는 이웃 노즐의 선을 잡을 수 있으므로(N24/N24A/N24B) 객체로 직접 선택
                sd = _call0(self.model.SelectionManager, "CreateSelectData")
                sd.Mark = 1
                if not axis_seg.Select4(True, sd):
                    raise RuntimeError("축 중심선 선택 실패")
                c1 = swRefPlaneReferenceConstraint_Angle | (swRefPlaneReferenceConstraint_OptionFlip if flip else 0)
                plane = self.feat_mgr.InsertRefPlane(c1, ang, swRefPlaneReferenceConstraint_Coincident, 0, 0, 0)
                if plane is None:
                    raise RuntimeError(f"지지판 기준면 생성 실패 (az={az_deg:g})")
                feat = self._last_feature()
                feat.Name = f"SupportPlane_{nz.mark}_{az_deg:g}"
                try:
                    tf = list(plane.Transform.ArrayData)
                    nrm = (tf[6], tf[7], tf[8])
                except Exception:
                    nrm = want_n
                if abs(dot(nrm, want_n)) > 0.95:
                    plane_name = feat.Name
                    break
                # 반대쪽으로 만들어졌으면 지우고 flip 으로 재생성
                self.model.ClearSelection2(True)
                self.model.Extension.SelectByID2(feat.Name, "PLANE", 0, 0, 0, False, 0, _nothing(), 0)
                self.model.Extension.DeleteSelection2(0)
            if plane_name is None:
                raise RuntimeError(f"지지판 기준면 방향 확인 실패 (az={az_deg:g})")
            # 두 긴 변의 목 쪽 끝 (mm): 면 쪽 변 E1 (치수 위치), 쉘 쪽 변 E2 는 목을 따라 w/sin(a) 만큼 쉘 쪽
            E1 = tuple(origin_mm[i] + s_a * A[i] + (r_neck - emb_neck) * W[i] for i in range(3))
            E2 = tuple(E1[i] - (w / math.sin(a)) * A[i] for i in range(3))
            drawn = nz.support_len if nz.support_len > 0 else 0.0
            t1, t2 = shell_hit(E1, D), shell_hit(E2, D)
            if t1 is None or (drawn > 0 and not (0.4 * drawn <= t1 <= 2.5 * drawn)):
                why = "교점 없음" if t1 is None else f"교점 {t1:.0f} 이 그림 길이 {drawn:.0f} 과 크게 다름"
                print(f"    {nz.mark}: 지지판(az {az_deg:g}) 쉘 교점 대신 그림 길이 사용 ({why})")
                t1 = drawn if drawn > 0 else (t1 or 100.0)
                t2 = max(t1 - w / math.tan(a), 10.0)
            elif t2 is None or t2 <= 0:
                t2 = max(t1 - w / math.tan(a), 10.0)
            corners = [E1,
                       tuple(E1[i] + (t1 + emb_shell) * D[i] for i in range(3)),
                       tuple(E2[i] + (t2 + emb_shell) * D[i] for i in range(3)),
                       E2]
            if E2[1] ** 2 + E2[2] ** 2 < R_o ** 2:
                # 판 폭이 목 부착 높이보다 커서 쉘 쪽 변의 목 끝이 이미 쉘 안: 목을 따라 쉘 외면까지만 잘라 삼각형으로
                h = shell_hit(E1, tuple(-c for c in A))
                if h is not None:
                    corners = [E1, corners[1], tuple(E1[i] - (h + emb_shell) * A[i] for i in range(3))]
                    print(f"    {nz.mark}: 지지판(az {az_deg:g}) 폭 {w:g} 이 목 부착 높이 {h:.0f} 보다 커 삼각형으로 자름")
            corners_m = [tuple(c * MM for c in p) for p in corners]
            self._begin_sketch(plane_name)
            self._load_transform(plane_name, probe=corners_m[0])
            psegs = self._polyline(corners_m)
            if len(psegs) >= 4:      # 0 = 면 쪽 긴 변, 2 = 쉘 쪽 긴 변, 1/3 = 짧은 변(판 폭)
                mid = tuple((corners_m[0][i] + corners_m[2][i]) / 2 for i in range(3))
                nm = f"{nz.mark}_SUPT_{az_deg:g}".replace("-", "M").replace(" ", "").replace(".", "_")
                # 길이는 긴 변의 두 끝점 사이로 잰다. 두 짧은 변은 쉘 쪽이 더 짧아 서로 평행하지 않고,
                # 평행하지 않은 두 선에 치수를 걸면 각도 치수가 되어 버린다 (실측)
                self._dim(_call0(psegs[0], "GetStartPoint2"), _call0(psegs[0], "GetEndPoint2"), mid,
                          f"{nz.mark} 지지판 길이", tag=f"{nm}_L", expect=math.dist(corners_m[0], corners_m[1]) / MM)
                self._dim(psegs[0], psegs[2], mid, f"{nz.mark} 지지판 폭", tag=f"{nm}_W", expect=w)
            self._end_sketch()
            self._extrude_midplane(nz.support_t * MM, f"Support_{nz.mark}_{az_deg:g}")
            end = corners[1]
            print(f"    {nz.mark}: 지지판 az {az_deg:g}° ({nz.support_az_ref}) 길이 {t1:.0f}/{t2:.0f} mm, 쉘 쪽 끝 |yz| = "
                  f"{math.hypot(end[1], end[2]):.1f} (쉘 외면 {R_o:g})")

    def _bolt_holes(self, nz: NozzleSpec, spec: VesselSpec, P: float, L: float, fl_t: float, extra: float = 0.0) -> None:
        r_bc = nz.bolt_circle / 2
        r_h = nz.bolt_hole_d / 2
        # 플랜지 링 면의 위치: 실루엣이 있으면 구멍이 들어갈 만큼 넓은(r >= B.C + 구멍) 첫 단, 없으면 플랜지 면
        s_off = 0.0
        if nz.profile:
            wide = [float(s) for s, r in nz.profile if float(r) >= r_bc + r_h]
            if wide:
                s_off = min(wide)
        s_face = P - s_off
        # 3D 프레임: 축 방향 A, 면 안의 두 방향 U, V (모델 좌표, m)
        if nz.on_head:
            sign = -1.0 if nz.on_head == "left" else 1.0
            origin = ((0.0 if nz.on_head == "left" else L) * MM, nz.offset_y * MM, 0.0)
            A, U, V = (sign, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
        else:
            th = math.radians(float(nz.angle_deg))
            origin = (float(nz.position_from_tl) * MM, nz.offset_y * MM, nz.offset_z * MM)
            A, U, V = (0.0, math.cos(th), math.sin(th)), (0.0, -math.sin(th), math.cos(th)), (1.0, 0.0, 0.0)

        def pt(s: float, r: float, phi: float) -> Vec3:
            return tuple(origin[i] + (s * A[i] + r * (math.cos(phi) * U[i] + math.sin(phi) * V[i])) * MM for i in range(3))

        # 직전 회전 컷 직후에는 토폴로지가 갱신되지 않아 좌표 선택이 실패하므로 먼저 재빌드
        try:
            self.model.ForceRebuild3(True)
        except Exception:
            pass
        # 스케치 평면 후보: 링 면(볼트 원 위치) → 플랜지 면(RF, 축 가까이) → 링 면에 광선. 축에 수직인 평면이면
        # 어느 것이든 구멍 원을 그릴 수 있다 (양방향 컷이라 1.6mm RF 단차는 무관)
        # 경판 노즐은 축이 X 라 플랜지 면이 우측면(YZ)과 평행 -> 면 선택 대신 x 오프셋 기준면을 쓴다 (면 선택은 실패함)
        r_rf = max(min(r_bc - r_h - 2.0, 0.6 * r_bc), 1.0)
        # 후보 평면(축에 수직이면 어느 것이든 됨): 링 면, 플랜지(RF) 면, 블라인드 바깥 면, 링 면에 광선. 각 후보의 s 에 원을 그린다
        candidates = [("ring", s_face, pt(s_face, r_bc, 0.0), False), ("face", P, pt(P, r_rf, 0.0), False)]
        if extra > 0:
            candidates.append(("blind", P + extra, pt(P + extra, r_rf, 0.0), False))
        candidates.append(("ring-ray", s_face, pt(s_face + 1.0, r_bc, 0.0), True))
        s_sk = s_face
        if nz.on_head:
            x_face_mm = pt(s_face, 0.0, 0.0)[0] / MM
            self._begin_sketch(self._offset_plane(x_face_mm))
            candidates = []
        opened = nz.on_head
        for label, s_c, p, ray in candidates:
            self.model.ClearSelection2(True)
            try:
                if ray:
                    ok = self.model.Extension.SelectByRay(p[0], p[1], p[2], -A[0], -A[1], -A[2], 0.002, 2, False, 0, 0)
                else:
                    ok = self.model.Extension.SelectByID2("", "FACE", p[0], p[1], p[2], False, 0, _nothing(), 0)
            except Exception:
                ok = False
            if not ok:
                continue
            self.sketch_mgr.InsertSketch(True)
            if self.sketch_mgr.ActiveSketch is not None:
                # 잡힌 면이 정말 구멍 원이 놓일 평면인지 확인 (이웃 노즐의 면이 잡히는 경우가 있음: N31/N31B)
                self._load_transform("flange face", probe=pt(s_c, r_bc, 0.0))
                try:
                    self._to_sk(pt(s_c, r_bc, 0.0))
                    s_sk = s_c
                    opened = True
                    break
                except RuntimeError:
                    self.sketch_mgr.InsertSketch(True)      # 다른 평면 -> 스케치 취소하고 다음 후보
                    continue
        if not opened:
            raise RuntimeError(f"플랜지 면 스케치 열기 실패 (s={s_face:.0f}, r={r_bc:.0f})")
        if not nz.on_head:
            self.sketch_mgr.AddToDB = True
            self._load_transform("flange face", probe=pt(s_sk, r_bc, 0.0))
        try:
            for k in range(nz.n_bolts):
                phi = 2 * math.pi * (k + 0.5) / nz.n_bolts      # 표준 관례: 구멍이 중심선을 걸치도록(straddle)
                self._circle(pt(s_sk, r_bc, phi), r_h * MM)
        finally:
            self.sketch_mgr.AddToDB = False
        self._fix_sketch()     # 구멍 위치는 ASME 표에서 계산한 값이라 편집할 것이 없다 -> 고정으로 완전정의
        depth = (fl_t + 5.0 + extra + abs(P - s_face)) * MM   # 어느 후보 면에서 시작해도 링과 블라인드를 모두 관통 (양방향)
        feat = self.feat_mgr.FeatureCut4(
            False, False, False, swEndCondBlind, swEndCondBlind, depth, depth,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False,
            False, True, True, False, False, False,
            0, 0.0, False, False,
        )
        if feat is None:
            try:
                if self.sketch_mgr.ActiveSketch is not None:
                    self.sketch_mgr.InsertSketch(True)
            except Exception:
                pass
            raise RuntimeError("볼트 구멍 컷 피처 생성 실패")
        feat.Name = f"BoltHoles_{nz.mark}"
        self.feature_count += 1
        self.model.ClearSelection2(True)

    # ------------------------------------------------------------------
    def _open_doc_paths(self) -> List[str]:
        """SolidWorks 에 열려 있는 문서들의 경로 (소문자)"""
        paths = []
        try:
            doc = _call0(self.app, "GetFirstDocument")
            while doc is not None:
                p = _call0(doc, "GetPathName")
                if p:
                    paths.append(os.path.normcase(os.path.abspath(p)))
                doc = _call0(doc, "GetNext")
        except Exception:
            pass
        return paths

    def resolve_save_path(self, path: str) -> str:
        """같은 경로의 문서가 이미 열려 있으면 SaveAs 가 실패하므로(같은 이름 문서 중복 불가)
        <이름>_<HHMMSS>.SLDPRT 로 바꾼다. 빌드 전에 호출해 미리 알린다."""
        target = os.path.normcase(os.path.abspath(path))
        if target in self._open_doc_paths():
            root, ext = os.path.splitext(path)
            alt = f"{root}_{time.strftime('%H%M%S')}{ext}"
            print(f"주의: {os.path.basename(path)} 가 SolidWorks 에 이미 열려 있어 {os.path.basename(alt)} 로 저장합니다.")
            return alt
        return path

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        path = self.resolve_save_path(path)
        print(f"저장: {path}")
        errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        ok = self.model.Extension.SaveAs(
            path, swSaveAsCurrentVersion, swSaveAsOptions_Silent, _nothing(), errors, warnings
        )
        if not ok:
            raise RuntimeError(f"저장 실패 ({_save_error_text(errors.value)}, warnings={warnings.value}). "
                               f"모델은 SolidWorks 에 열려 있으니 수동으로 저장하세요.")

    def _frame_view(self, force: bool = False) -> None:
        """피처가 생길 때마다 모델 전체가 화면 가운데 보이도록 맞춘다.
        ViewZoomtofit2 는 치수선·주석까지 화면에 넣으려 하기 때문에 치수를 붙일수록 모델이 점점 작아진다.
        그래서 솔리드의 경계 상자만 보고, 그 중심을 화면 중심에 두는 정육면체로 직접 맞춘다"""
        now = time.time()
        if not force and now - self._view_t < 0.5:
            return
        self._view_t = now
        try:
            box = None
            try:      # 솔리드 바디만 본다. GetPartBox 는 스케치·중심선까지 넣어 화면이 조금씩 넓어진다
                for b in (self.model.GetBodies2(0, True) or []):
                    bb = _call0(b, "GetBodyBox")
                    if bb and len(bb) >= 6:
                        box = (list(bb) if box is None else
                               [min(box[i], bb[i]) for i in range(3)] + [max(box[i + 3], bb[i + 3]) for i in range(3)])
            except Exception:
                box = None
            if not box:
                box = self.model.GetPartBox(True)
            if not box or len(box) < 6:
                return
            c = [(box[i] + box[i + 3]) / 2 for i in range(3)]
            half = 1.1 * max(max(box[i + 3] - box[i] for i in range(3)) / 2, 0.05)
            self.model.ViewZoomTo2(c[0] - half, c[1] - half, c[2] - half,
                                   c[0] + half, c[1] + half, c[2] + half)
        except Exception:
            pass

    def fit_view(self) -> None:
        try:
            self.model.ShowNamedView2("*Isometric", 7)
            self._frame_view(force=True)
        except Exception:
            pass

    def report_box(self) -> None:
        try:
            box = self.model.GetPartBox(True)
            if box:
                mm = [v / MM for v in box]
                print(f"모델 범위(mm): X {mm[0]:.0f}~{mm[3]:.0f}, Y {mm[1]:.0f}~{mm[4]:.0f}, Z {mm[2]:.0f}~{mm[5]:.0f}")
        except Exception:
            pass

    def close(self) -> None:
        if self.app is not None:                 # 바꿔 둔 SolidWorks 설정은 원래 값으로 되돌린다
            for pref, val in self._prefs.items():
                try:
                    self.app.SetUserPreferenceToggle(pref, val)
                except Exception:
                    pass
        self._prefs.clear()
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def build(self, spec: VesselSpec, save_path: Optional[str] = None,
              with_nozzles: bool = True, only_nozzles: Optional[Sequence[str]] = None) -> None:
        self.connect()
        t0 = time.time()
        try:
            if save_path:
                save_path = self.resolve_save_path(save_path)   # 4분짜리 빌드 뒤에 저장이 막히지 않도록 미리 확인
            self.new_part()
            self.build_shell(spec)
            self.build_saddles(spec)
            self.build_lugs(spec)
            if with_nozzles:
                self.build_nozzles(spec, only_nozzles)
            self.fit_view()
            self.report_box()
            if save_path:
                self.save(save_path)
            print(f"모델링 완료. 피처 {self.feature_count} 개, 치수 {self.dim_count} 개, {time.time() - t0:.0f} 초")
            if self.dim_log:
                print("모델에 들어간 치수 (이름 = 값, 도면과 비교용):")
                for tag, val, desc in self.dim_log:
                    print(f"  {tag:<18} = {val:9.1f} mm   {desc}")
        finally:
            self.close()
