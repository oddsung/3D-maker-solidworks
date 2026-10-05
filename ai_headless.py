"""
ai_headless.py - Claude Code headless(`claude -p`) 호출 래퍼와 도면 분석용 AI 작업

규칙 파서가 못 하는 일(이미지 스캔 도면 읽기, 규칙이 못 찾은 값 찾기, 도면 관계 해석)을
Claude Code 를 비대화형으로 불러 처리한다. Claude API 키 없이 로그인된 Claude Code 로 동작하므로
"구현 가능성 확인" 단계에 맞다. 실측(2026-09-14, sonnet):
  - 고정 오버헤드: 호출당 시스템 컨텍스트 약 4.7만 토큰(캐시) -> 최소 약 $0.07, 1~2초
  - 텍스트만 프롬프트에 넣고 `--effort low`: 15초, 약 $0.3, 도구 사용 없음  <- 권장
  - PDF/PNG 를 Read 도구로 읽게 하면: 90초~5분, $0.6 이상 (여러 턴). 큰 벡터 도면은 예산 초과로 실패
  => 규칙으로 위치를 좁힌 뒤 작은 텍스트/크롭만 넘기는 하이브리드가 현실적.

사용:
  r = run_claude(prompt, schema=..., model="sonnet", effort="low", budget=1.0)
  r.ok / r.data(구조화 출력) / r.cost_usd / r.seconds
모든 호출은 AI_LOG(JSON Lines) 에 기록한다 (작업, 모델, 비용, 시간, 성공 여부).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from typing import List, Optional, Sequence

AI_LOG = os.environ.get("AI_HEADLESS_LOG", "")
DEFAULT_MODEL = "sonnet"


@dataclass
class AIResult:
    task: str
    ok: bool
    data: Optional[dict]
    text: str
    cost_usd: float
    seconds: float
    turns: int
    model: str
    error: str = ""

    def summary(self) -> str:
        st = "성공" if self.ok else f"실패({self.error[:60]})"
        return f"[AI {self.task}] {st}, {self.seconds:.0f}s, ${self.cost_usd:.2f}, {self.turns}턴, {self.model}"


def claude_command() -> Optional[List[str]]:
    """claude 실행 명령. Windows 에서는 cmd.exe 인용 문제를 피하려고 node + cli.js 를 직접 부른다."""
    exe = shutil.which("claude") or shutil.which("claude.cmd")
    if not exe:
        return None
    if os.name == "nt":
        base = os.path.dirname(exe)
        cli = os.path.join(base, "node_modules", "@anthropic-ai", "claude-code", "cli.js")
        node = shutil.which("node")
        if node and os.path.exists(cli):
            return [node, cli]
        if exe.lower().endswith(".cmd"):
            return [exe]
        cmd = exe + ".cmd"
        return [cmd] if os.path.exists(cmd) else [exe]
    return [exe]


def run_claude(prompt: str, schema: Optional[dict] = None, model: str = DEFAULT_MODEL, effort: str = "low",
               budget: float = 1.0, allowed_tools: Sequence[str] = (), timeout: int = 900,
               task: str = "", log_path: str = "") -> AIResult:
    cmd = claude_command()
    if not cmd:
        return AIResult(task, False, None, "", 0.0, 0.0, 0, model, "claude CLI 를 찾을 수 없음")
    args = cmd + ["-p", "--model", model, "--output-format", "json", "--no-session-persistence",
                  "--max-budget-usd", str(budget)]
    if effort:
        args += ["--effort", effort]
    if schema:
        args += ["--json-schema", json.dumps(schema)]
    if allowed_tools:
        args += ["--allowedTools", ",".join(allowed_tools)]
    t0 = time.time()
    try:
        # 프롬프트는 stdin 으로 넘긴다 (Windows 명령줄 길이 제한 회피)
        proc = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
        raw = proc.stdout
        err = proc.stderr
    except subprocess.TimeoutExpired:
        res = AIResult(task, False, None, "", 0.0, time.time() - t0, 0, model, f"시간 초과 {timeout}s")
        _log(res, log_path)
        return res
    except OSError as e:
        return AIResult(task, False, None, "", 0.0, time.time() - t0, 0, model, f"실행 오류: {e}")
    seconds = time.time() - t0
    try:
        d = json.loads(raw)
    except Exception:
        res = AIResult(task, False, None, raw[:2000], 0.0, seconds, 0, model,
                       f"JSON 응답 아님 (exit {proc.returncode}): {(err or raw)[:200]}")
        _log(res, log_path)
        return res
    ok = not d.get("is_error") and d.get("subtype") == "success"
    data = d.get("structured_output")
    if schema and ok and data is None:
        ok = False
    res = AIResult(task, ok, data, str(d.get("result", ""))[:4000], float(d.get("total_cost_usd") or 0.0),
                   seconds, int(d.get("num_turns") or 0), model,
                   "" if ok else str(d.get("result") or d.get("subtype") or "")[:300])
    _log(res, log_path)
    return res


def _log(res: AIResult, log_path: str) -> None:
    path = log_path or AI_LOG
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            d = asdict(res)
            d["text"] = d["text"][:500]
            d["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ----------------------------------------------------------------------
def render_png(pdf_path: str, out_png: str, dpi: int = 110, bbox=None) -> str:
    """PDF 첫 페이지(또는 bbox 영역, pt)를 PNG 로 렌더링"""
    import pdfplumber
    os.makedirs(os.path.dirname(out_png) or ".", exist_ok=True)
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        if bbox:
            x0, y0, x1, y1 = bbox
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(float(page.width), x1), min(float(page.height), y1)
            page = page.crop((x0, y0, x1, y1))
        page.to_image(resolution=dpi).save(out_png)
    return out_png


def words_as_text(words, bbox=None, limit: int = 600) -> str:
    """Word 목록 -> 'text @(x,top) [rot]' 줄 목록 (bbox 안의 것만)"""
    lines = []
    for w in words:
        if bbox and not (bbox[0] <= w.xc <= bbox[2] and bbox[1] <= w.yc <= bbox[3]):
            continue
        lines.append(f"{w.text.strip()} @({w.x0:.0f},{w.top:.0f}){'' if w.upright else ' rot'}")
        if len(lines) >= limit:
            break
    return "\n".join(lines)


# ---- 작업 1: 이미지 스캔 도면 읽기 --------------------------------------------
SHEET_SCHEMA = {
    "type": "object",
    "properties": {
        "drawing_no": {"type": "string"},
        "title": {"type": "string"},
        "sheet": {"type": "string"},
        "rev": {"type": "string"},
        "views": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "scale": {"type": "string"}}, "required": ["name"]}},
        "nozzle_marks": {"type": "array", "items": {"type": "string"}},
        "key_labels": {"type": "array", "items": {"type": "string"}},
        "dimensions_to_cl_or_tl": {"type": "array", "items": {"type": "string"}},
        "summary_ko": {"type": "string"},
    },
    "required": ["drawing_no", "title", "views", "summary_ko"],
}


def describe_scanned_sheet(png_path: str, hint: str = "", model: str = DEFAULT_MODEL, budget: float = 1.5,
                           log_path: str = "") -> AIResult:
    prompt = (
        f"Read the image file '{png_path}' once with the Read tool. It is one sheet of a pressure-vessel "
        f"fabrication drawing set (scanned raster page, title block at bottom right). {hint}\n"
        "Extract only what you can actually read: drawing number (SLB DWG. NO.), title (both lines), sheet n/m, "
        "revision, every view/section/detail title with its scale, nozzle marks (N1, N4A ...), important labels "
        "(platform names with EL. elevations, clip/bracket ids like CL-1 / BK-2, ladder, insulation thickness), "
        "any dimension written as 'xxxx TO C.L' or 'xxxx TO T.L', and a 2~3 sentence Korean summary of what the sheet shows. "
        "Do not use any tool other than Read."
    )
    return run_claude(prompt, SHEET_SCHEMA, model=model, effort="low", budget=budget,
                      allowed_tools=("Read",), task="scanned_sheet", log_path=log_path)


# ---- 작업 2: 규칙이 못 찾은 노즐 값 찾기 (텍스트 전용) -------------------------------
NOZZLE_SCHEMA = {
    "type": "object",
    "properties": {
        "nozzles": {"type": "array", "items": {"type": "object", "properties": {
            "mark": {"type": "string"},
            "projection_mm": {"type": "number"},
            "projection_reference": {"type": "string"},
            "offset_from_cl_mm": {"type": "number"},
            "boss_or_neck_od_mm": {"type": "number"},
            "evidence": {"type": "string"},
            "confidence": {"type": "string"}},
            "required": ["mark", "projection_mm", "projection_reference", "evidence", "confidence"]}},
        "notes_ko": {"type": "string"},
    },
    "required": ["nozzles"],
}


def find_nozzle_values(marks: Sequence[str], words_text: str, sheet_name: str, model: str = DEFAULT_MODEL,
                       budget: float = 1.0, log_path: str = "") -> AIResult:
    prompt = (
        f"Below are text objects from pressure-vessel nozzle detail drawing sheet {sheet_name}, one per line as "
        "'text @(x,top)' in PDF points (origin top-left, 'rot' = rotated 90 degrees). Nozzle views are titled "
        "'Nxx size NOZZLE' just above a '( SCALE = ... )' line; a view's geometry and dimensions are ABOVE its title. "
        "A title listing several marks (e.g. N4 8\" N4A 8\" ... NOZZLE) is one view shared by identical nozzles.\n"
        f"For the nozzle marks {', '.join(marks)} report: projection = the dimension 'xxxx TO C.L' (vessel centerline "
        "to flange face; if a view has several TO C.L values the flange-face one is the largest) or 'xxxx TO T.L' "
        "for head nozzles; the reference (C.L or T.L); any smaller 'xxx TO C.L' value in the same view as "
        "offset_from_cl_mm; the BOSS diameter 'Øxxx' from the part table rows of that nozzle if present; the exact "
        "evidence text; and confidence (high/medium/low). Answer from the text only, no tools. "
        "Write notes_ko in Korean.\n\nTEXT OBJECTS:\n" + words_text
    )
    return run_claude(prompt, NOZZLE_SCHEMA, model=model, effort="low", budget=budget,
                      task="nozzle_values", log_path=log_path)


# ---- 작업 3: 도면 세트 관계 해석 (텍스트 전용) ----------------------------------------
RELATION_SCHEMA = {
    "type": "object",
    "properties": {
        "relationships": {"type": "array", "items": {"type": "object", "properties": {
            "from": {"type": "string"}, "to": {"type": "string"}, "kind": {"type": "string"},
            "evidence": {"type": "string"}}, "required": ["from", "to", "kind", "evidence"]}},
        "lookup_hints": {"type": "array", "items": {"type": "object", "properties": {
            "missing": {"type": "string"}, "look_in": {"type": "string"}, "how": {"type": "string"}},
            "required": ["missing", "look_in", "how"]}},
        "summary_ko": {"type": "string"},
    },
    "required": ["relationships", "lookup_hints", "summary_ko"],
}


def infer_relationships(index_text: str, model: str = DEFAULT_MODEL, budget: float = 1.0,
                        log_path: str = "") -> AIResult:
    prompt = (
        "You are given an index of a pressure-vessel fabrication drawing set (one block per sheet: number, title, "
        "kind, views, nozzle marks, weld seam ids, section markers without a matching view on the same sheet, "
        "'SEE DWG.' items, and key facts). Identify how the sheets relate to each other: which sheet details what "
        "another sheet references (nozzle marks -> nozzle detail sheets, weld seams -> development drawing, section "
        "markers -> the sheet holding VIEW/SECTION with that letter, 'SEE DWG.' projections -> detail sheets, "
        "shared dimensions such as T.L to T.L / saddle spacing / O.D that must agree). Then list lookup hints: for "
        "values marked as missing or 'SEE DWG.', which sheet and which label to read. Use sheet short names exactly "
        "as given. Write summary_ko in Korean. No tools.\n\nINDEX:\n" + index_text
    )
    return run_claude(prompt, RELATION_SCHEMA, model=model, effort="low", budget=budget,
                      task="relationships", log_path=log_path)


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    r = run_claude("Reply with exactly: OK", model=DEFAULT_MODEL, effort="low", budget=0.5, task="ping")
    print(r.summary(), "|", r.text[:40])
