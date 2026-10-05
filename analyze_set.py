"""
analyze_set.py - 도면 세트(폴더) 분석 CLI

  python analyze_set.py sample_pdf/sameple_001                 # 1) 도면별 추출 2) 연관성 3) 누락 값 보완
  python analyze_set.py sample_pdf/sameple_001 --ai scanned    # + 스캔 도면을 Claude headless 로 읽기
  python analyze_set.py sample_pdf/sameple_001 --ai assist     # + 규칙이 못 채운 노즐 값을 AI 로 시도
  python analyze_set.py sample_pdf/sameple_001 --ai full       # + AI 관계 해석, 규칙 결과와 비교 (실현 가능성 확인)
  python analyze_set.py <folder> --export-spec spec.json       # 보완된 VesselSpec 저장 -> main.py --spec 으로 모델링

출력 (기본 output/<폴더명>/):
  drawing_set.json         도면별 추출 결과 + 연관성 + 일관성 + 보완 내역 (+ AI 결과)
  drawing_set_report.md    사람이 읽는 리포트
  ai_calls.jsonl           AI 호출 기록 (작업, 비용, 시간)
  render/*.png             AI 에 넘긴 렌더링 이미지
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from drawing_set import DrawingSet, STATUS_KO, EDGE_KO, list_pdfs
from pdf_dimension_parser import parse_drawings


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="도면 세트 분석: 도면별 정보 추출, 연관성, 누락 값 보완")
    p.add_argument("inputs", nargs="+", help="도면 폴더 또는 PDF 파일들")
    p.add_argument("--out", help="출력 폴더 (기본 output/<폴더명>)")
    p.add_argument("--ai", choices=["off", "scanned", "assist", "full"], default="off",
                   help="Claude headless 사용 범위: off(기본) / scanned(스캔 도면 읽기) / assist(+미해결 노즐 값) / full(+관계 해석)")
    p.add_argument("--model", default="sonnet", help="AI 모델 별칭 (sonnet/opus/haiku)")
    p.add_argument("--budget", type=float, default=1.5, help="AI 호출당 최대 비용(USD)")
    p.add_argument("--export-spec", help="보완된 VesselSpec 을 JSON 으로 저장")
    p.add_argument("--no-spec", action="store_true", help="VesselSpec 파싱/보완 단계 생략 (도면 정보와 연관성만)")
    p.add_argument("--ai-refresh", action="store_true",
                   help="이전 실행의 AI 결과(drawing_set.json)를 재사용하지 않고 다시 호출")
    p.add_argument("--quiet", action="store_true")
    return p


class AICache:
    """같은 작업/대상의 이전 AI 결과를 drawing_set.json 에서 읽어 재사용 (비용 절약)"""

    def __init__(self, path: str, enabled: bool):
        self.items = {}
        if enabled and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for r in json.load(f).get("ai_results", []):
                        if r.get("data"):
                            self.items[(r.get("task"), r.get("target"))] = r
            except (OSError, ValueError):
                pass

    def get(self, task: str, target: str):
        hit = self.items.get((task, target))
        if hit is None and task == "nozzle_values":      # 같은 시트면 마크 목록이 달라도 재사용
            sheet = target.split(" [")[0]
            hit = next((v for (t, k), v in self.items.items() if t == task and k.split(" [")[0] == sheet), None)
        return hit


class _Cached:
    """AIResult 와 같은 모양의 캐시 결과"""

    def __init__(self, r: dict):
        self.task, self.ok, self.data, self.error = r.get("task"), True, r.get("data"), ""
        self._summary = r.get("summary", "")

    def summary(self) -> str:
        base = self._summary.split(" (이전 결과 재사용)")[0]
        return f"{base} (이전 결과 재사용)"


def collect_paths(inputs):
    paths = []
    for inp in inputs:
        if os.path.isdir(inp):
            paths += list_pdfs(inp)
        elif os.path.isfile(inp):
            paths.append(inp)
        else:
            print(f"입력을 찾을 수 없습니다: {inp}")
    return list(dict.fromkeys(paths))


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = build_arg_parser().parse_args(argv)
    paths = collect_paths(args.inputs)
    if not paths:
        return 1
    folder_name = os.path.basename(os.path.normpath(args.inputs[0])) if os.path.isdir(args.inputs[0]) else "set"
    out_dir = args.out or os.path.join("output", folder_name)
    os.makedirs(out_dir, exist_ok=True)
    ai_log = os.path.join(out_dir, "ai_calls.jsonl")
    cache = AICache(os.path.join(out_dir, "drawing_set.json"), enabled=not args.ai_refresh)
    verbose = not args.quiet

    print(f"=== 1. 도면별 정보 추출 ({len(paths)} 장)")
    t0 = time.time()
    ds = DrawingSet.from_paths(paths, verbose=verbose)
    print(f"  ({time.time() - t0:.1f}s)\n")
    print(ds.drawing_table())

    # ---- AI: 스캔 도면 읽기 --------------------------------------------------
    if args.ai != "off":
        from ai_headless import describe_scanned_sheet, render_png
        # 스캔 도면 읽기는 페이지를 그림으로 만들 수 있는 PDF 에만 해당 (DXF 는 문자가 요소로 남아 있어 필요 없음)
        scanned = [d for d in ds.drawings if d.grade == "D" and d.path.lower().endswith(".pdf")]
        if scanned:
            print(f"\n=== 1a. 스캔 도면 {len(scanned)} 장을 Claude headless 로 읽기 (모델 {args.model})")
        for d in scanned:
            png = render_png(d.path, os.path.join(out_dir, "render", os.path.splitext(d.file)[0] + ".png"), dpi=130)
            hint = f"Sibling sheet title is '{d.title}'." if d.title else ""
            c = cache.get("scanned_sheet", d.short)
            r = _Cached(c) if c else describe_scanned_sheet(os.path.abspath(png), hint=hint, model=args.model,
                                                            budget=args.budget, log_path=ai_log)
            print("  -", r.summary())
            ds.ai_results.append({"task": r.task, "target": d.short, "summary": r.summary(), "data": r.data, "error": r.error})
            if r.ok and r.data:
                d.ai = r.data
                if r.data.get("views"):
                    from drawing_info import View
                    for v in r.data["views"]:
                        d.views.append(View(name=str(v.get("name", "")), scale=str(v.get("scale", "")), kind="ai"))
                for mk in r.data.get("nozzle_marks", []) or []:
                    if mk not in d.marks:
                        d.marks.append(mk)
                d.log.append("AI 읽기 결과로 뷰/마크 보강")

    # ---- 연관성 -----------------------------------------------------------
    print(f"\n=== 2. 도면 사이의 연관성 ({len(ds.edges)} 개)")
    for kind in ("sheet_series", "see_dwg", "nozzle_mark", "weld_seam", "section_view", "title_ref", "fact_match", "fact_conflict"):
        es = [e for e in ds.edges if e.kind == kind]
        if not es:
            continue
        print(f"  [{EDGE_KO[kind]}] {len(es)} 개")
        for e in es[: (6 if kind == "fact_match" else 40)]:
            print(f"    {e.src} -> {e.dst}: {e.detail}")
        if kind == "fact_match" and len(es) > 6:
            print(f"    ... 외 {len(es) - 6} 개 (리포트 참고)")
    print("\n  값 일관성:")
    from drawing_set import NON_COMPARABLE
    for k, vals in ds.fact_matrix.items():
        if len(vals) < 2 or k in NON_COMPARABLE:
            continue
        bad = any(c["key"] == k for c in ds.conflicts)
        print(f"    {'!! ' if bad else '   '}{k:24s} " + "; ".join(f"{s}={vals[s]}" for s in vals))
    for m in ds.log:
        print("  -", m)

    # ---- VesselSpec + 누락 값 보완 ------------------------------------------
    spec = None
    if not args.no_spec:
        srcs = ds.spec_sources()
        print(f"\n=== 3. 용기 사양 파싱 (리프팅/GA {len(srcs)} 장) 후 상세도로 누락 값 보완")
        spec = parse_drawings(srcs, verbose=False)
        res = ds.resolve(spec, apply=True)
        for msg in spec.fill_defaults():
            print("  기본값:", msg)
        cnt = {}
        for r in res:
            cnt[r.status] = cnt.get(r.status, 0) + 1
        print("  판정 집계: " + ", ".join(f"{STATUS_KO[k]} {v}" for k, v in cnt.items()))
        print(f"\n  {'마크':7s} {'항목':16s} {'GA 값':>10s} {'상세도 값':>14s}  {'출처':30s} 근거 / 판정")
        for r in res:
            if r.status in ("resolved", "conflict", "unresolved"):
                print(f"  {r.mark:7s} {r.field:16s} {r.old:>10s} {r.new:>14s}  {r.source[:30]:30s} {r.evidence} / {r.status_ko}")
        n_conf = sum(1 for r in res if r.status == "confirmed")
        print(f"  (확인 {n_conf} 건은 리포트 5절 참고)")

        # AI: 미해결 노즐 값
        unresolved = [r for r in res if r.status == "unresolved"]
        if args.ai in ("assist", "full"):
            from ai_headless import find_nozzle_values, words_as_text
            from pdf_dimension_parser import DrawingParser
            targets = {}
            if unresolved:
                for r in unresolved:
                    for short, role, view in ds.mark_index.get(r.mark, []):
                        if role in ("detail", "bom"):
                            targets.setdefault(short, []).append(r.mark)
                            break
            if not targets and args.ai == "full":
                # 실현 가능성 확인용: 규칙 판정이 약한(참고/불일치) 마크가 있는 시트를 AI 로도 읽어 결과를 비교
                weak = [r.mark for r in res if r.status in ("info", "conflict") and r.mark.startswith("N")]
                cand = None
                for d in ds.drawings:
                    if d.kind == "nozzle_detail" and d.dims and (cand is None or any(mk in d.marks for mk in weak)):
                        cand = d
                        if any(mk in d.marks for mk in weak):
                            break
                if cand:
                    targets[cand.short] = [mk for v in cand.views for mk in v.marks][:10]
            for short, marks in targets.items():
                d = ds.by_short[short]
                target = f"{short} {marks}"
                c = cache.get("nozzle_values", target)
                if c:
                    r = _Cached(c)
                else:
                    words = DrawingParser(d.path).words
                    r = find_nozzle_values(marks, words_as_text(words), short, model=args.model, budget=args.budget, log_path=ai_log)
                print("  -", r.summary())
                ds.ai_results.append({"task": r.task, "target": target, "summary": r.summary(), "data": r.data, "error": r.error})
                if r.ok and r.data:
                    rule = {(x.mark, x.field): x for x in res if x.source.startswith(short)}
                    for item in r.data.get("nozzles", []):
                        mk = item.get("mark")
                        rr = rule.get((mk, "projection"))
                        cmp = f"규칙 {rr.new}" if rr else "규칙 값 없음"
                        print(f"      AI {mk}: 투영 {item.get('projection_mm')} ({item.get('projection_reference')}) "
                              f"[{item.get('confidence')}] vs {cmp} | {str(item.get('evidence'))[:60]}")
                        nz = spec.nozzle(mk) if mk else None
                        if nz and (nz.projection is None) and item.get("projection_mm"):
                            nz.projection = float(item["projection_mm"])
                            nz.notes.append(f"투영 {nz.projection:g} (AI 읽기, {short})")

        print("\n=== 보완된 용기 사양")
        print(spec.summary())
        print()
        print(spec.nozzle_table())
        for msg in spec.validate():
            print("경고:", msg)
        if args.export_spec:
            spec.to_json(args.export_spec)
            print(f"사양 JSON 저장: {args.export_spec}")

    # ---- AI: 관계 해석 ------------------------------------------------------
    if args.ai == "full":
        from ai_headless import infer_relationships
        print("\n=== 4. AI 관계 해석 (규칙 결과와 비교용)")
        c = cache.get("relationships", "set")
        r = _Cached(c) if c else infer_relationships(ds.index_text(), model=args.model, budget=args.budget, log_path=ai_log)
        print("  -", r.summary())
        ds.ai_results.append({"task": r.task, "target": "set", "summary": r.summary(), "data": r.data, "error": r.error})
        if r.ok and r.data:
            for rel in r.data.get("relationships", [])[:30]:
                print(f"    {rel.get('from')} -> {rel.get('to')} [{rel.get('kind')}]: {str(rel.get('evidence'))[:80]}")
            for h in r.data.get("lookup_hints", [])[:15]:
                print(f"    힌트: {h.get('missing')} -> {h.get('look_in')}: {str(h.get('how'))[:80]}")
            print("    요약:", r.data.get("summary_ko", "")[:400])

    # ---- 저장 ---------------------------------------------------------------
    js = os.path.join(out_dir, "drawing_set.json")
    md = os.path.join(out_dir, "drawing_set_report.md")
    ds.save_json(js)
    ds.save_report(md, spec)
    print(f"\n저장: {js}\n      {md}")
    if ds.ai_results:
        total = 0.0
        try:
            with open(ai_log, encoding="utf-8") as f:
                for line in f:
                    total += json.loads(line).get("cost_usd", 0.0)
        except OSError:
            pass
        print(f"      {ai_log} (이번 실행까지 누적 AI 비용 약 ${total:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
