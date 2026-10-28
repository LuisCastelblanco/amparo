"""Run the full pipeline on a demo case and print every step.

Usage (from the repo root):
    uv run --project backend python scripts/run_case.py 01           # file name prefix in docs/demo_cases
    uv run --project backend python scripts/run_case.py path/to/case.md
    uv run --project backend python scripts/run_case.py --all --quiet
    uv run --project backend python scripts/run_case.py --all --quiet --record frontend/src/data/samples
    uv run --project backend python scripts/run_case.py --sample-documents frontend/src/data/samples
    uv run --project backend python scripts/run_case.py 01 --no-cache        # force live Tavily searches

Tavily results come from the search cache when possible (0 credits); --no-cache searches live and refreshes it.
Recording also writes each sample's .docx and PDF to frontend/public/samples/<case>/ and links them from the
recorded case_completed event, so sample runs download static files; --sample-documents only redoes that.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8")

from app import storage  # noqa: E402
from app.pipeline import run_case  # noqa: E402
from app.search import Searcher  # noqa: E402

DEMO_DIR = REPO_ROOT / "docs" / "demo_cases"
SAMPLE_FILES_DIR = REPO_ROOT / "frontend" / "public" / "samples"


def save_recording(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def write_sample_documents(recording: Path) -> None:
    """Render a recorded run's .docx and PDF into the frontend's public folder and link them from the recording."""
    payload = json.loads(recording.read_text(encoding="utf-8"))
    final = next((e for e in payload["events"] if e["event"] == "case_completed"), None)
    if final is None or not final["data"]["package"].get("draft"):
        return
    folder = SAMPLE_FILES_DIR / recording.stem
    if folder.exists():
        for old in folder.iterdir():
            old.unlink()
    folder.mkdir(parents=True, exist_ok=True)
    links = {}
    for name, (content, _) in storage.render(final["data"]["package"]["draft"]).items():
        (folder / name).write_bytes(content)
        links[name.rsplit(".", 1)[1]] = f"/samples/{recording.stem}/{name}"
    final["data"]["documents"] = links
    save_recording(recording, payload)
    print(f"sample documents -> {folder}")


def resolve_case(arg: str) -> Path:
    """A case file: the path itself, or the demo case whose file name starts with `arg`."""
    path = Path(arg)
    if not path.exists():
        matches = sorted(DEMO_DIR.glob(f"{arg}*.md"))
        if not matches:
            raise SystemExit(f"No demo case matches {arg!r} in {DEMO_DIR}")
        path = matches[0]
    return path


def load_case(arg: str) -> tuple[str, str]:
    path = resolve_case(arg)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    title = lines[0].lstrip("# ").strip()
    return title, "\n".join(lines[1:]).strip()


def fmt_metrics(m: dict) -> str:
    cost = f"${m['cost_usd']:.6f}" + ("" if m["cost_complete"] else " (partial)")
    parts = [
        f"models={','.join(m['models']) or '-'}",
        f"tokens in/out={m['input_tokens']}/{m['output_tokens']}",
        f"latency={m['latency_s']}s",
        f"cost={cost}",
    ]
    if m["tavily_searches"]:
        parts.append(fmt_tavily(m))
    return " ".join(parts)


def fmt_tavily(m: dict) -> str:
    return (
        f"tavily live={m.get('tavily_live', 0)} cached={m.get('tavily_cached', 0)} "
        f"credits={m.get('tavily_credits', 0)} (${m.get('search_cost_usd', 0):.4f})"
    )


def fmt_routing(t: dict) -> str:
    """Model cost against the counterfactual of running every call on the reasoning tier (models only)."""
    real, ultra = t.get("llm_cost_usd", 0), t.get("cost_all_reasoning_usd", 0)
    ratio = f"{ultra / real:.1f}x cheaper" if real else "n/a"
    return f"models=${real:.6f} all-reasoning=${ultra:.6f} ({ratio})"


async def run(
    title: str, narrative: str, quiet: bool, english_summary: bool, use_cache: bool = True
) -> tuple[dict, list[dict]]:
    print(f"\n{'#' * 78}\n# {title}\n{'#' * 78}")
    summary: dict = {"title": title, "steps": {}, "completed": False, "escalations": []}
    recorded: list[dict] = []
    start = time.perf_counter()
    async for event, data in run_case(narrative, english_summary=english_summary, searcher=Searcher(use_cache)):
        recorded.append({"t": round(time.perf_counter() - start, 2), "event": event, "data": data})
        if event == "escalated":
            summary["escalations"].append(data)
            print(f"    ESCALATED {data['from_model']} -> {data['to_model']}: {'; '.join(data['reasons'])}")
        elif event == "verify_iteration":
            details = {k: v for k, v in data.items() if k not in ("step", "iteration", "phase")}
            print(f"    VERIFY iteration {data['iteration']} {data['phase']}: {json.dumps(details)}")
        elif event == "step_started":
            print(f"\n--- [{data['index']}/{data['total']}] {data['step']} ...")
        elif event == "step_completed":
            summary["steps"][data["step"]] = data["metrics"]
            if data["step"] == "research":
                summary["precedents"] = [p["sentencia_id"] for p in data["result"]["precedentes"]]
                summary["accents_restored"] = sum(p["acentos_restaurados"] for p in data["result"]["precedentes"])
            print(f"    OK  {fmt_metrics(data['metrics'])}")
            if not quiet and data["step"] not in ("draft", "verify", "package"):
                print(json.dumps(data["result"], ensure_ascii=False, indent=2))
        elif event == "step_failed":
            summary["failed"] = data
            print(f"    FAILED {data['error']}")
        elif event == "step_unavailable":
            summary["steps"][data["step"]] = data["metrics"]
            summary["unavailable"] = data["reason"]
            print(f"    UNAVAILABLE {data['message']}")
        elif event in ("case_failed", "case_unavailable"):
            summary["totals"] = data["totals"]
        elif event == "case_completed":
            pkg = data["package"]
            summary.update(
                completed=True, totals=data["totals"], verification=pkg["verification"],
                channel=pkg.get("canal_radicacion"), reviewer_summary=bool(pkg.get("resumen_revisores")),
            )
            if not quiet:
                guidance = {
                    k: pkg.get(k) for k in (
                        "checklist_anexos", "donde_radicar", "que_hacer_si_no_cumplen", "canal_radicacion",
                        "resumen_revisores", "aviso",
                    )
                }
                print(f"\n{'=' * 78}\nDRAFT\n{'=' * 78}\n{pkg['draft']['texto_completo']}")
                print(f"\n{'=' * 78}\nVERIFICATION\n{'=' * 78}")
                print(json.dumps(pkg["verification"], ensure_ascii=False, indent=2))
                print(f"\n{'=' * 78}\nFILING GUIDANCE\n{'=' * 78}")
                print(json.dumps(guidance, ensure_ascii=False, indent=2))
    t = summary.get("totals", {})
    print(
        f"\nTOTAL tokens in/out={t.get('input_tokens')}/{t.get('output_tokens')} "
        f"latency={t.get('latency_s')}s cost=${t.get('cost_usd', 0):.6f} "
        f"(models ${t.get('llm_cost_usd', 0):.6f} + search ${t.get('search_cost_usd', 0):.6f})"
    )
    print(f"TAVILY  {fmt_tavily(t)}")
    print(f"ROUTING {fmt_routing(t)}")
    return summary, recorded


def print_summary(summaries: list[dict]) -> None:
    print(f"\n{'#' * 78}\n# SUMMARY\n{'#' * 78}")
    for s in summaries:
        v = s.get("verification") or {}
        if s["completed"]:
            status = "6/6 steps completed"
        elif s.get("unavailable"):
            status = f"stopped before drafting: {s['unavailable']}"
        else:
            status = f"FAILED at {s['failed']['step']}"
        print(f"\n{s['title']}\n  {status}")
        if "precedents" in s:
            print(f"  precedents: {len(s['precedents'])} {s['precedents']} accents_restored={s['accents_restored']}")
        if v:
            print(
                f"  citations: final={len(v['citas_encontradas'])} verified={len(v['citas_verificadas'])} "
                f"unverified_before_loop={len(v['citas_no_verificadas_iniciales'])} "
                f"unverified_after_loop={len(v['citas_no_verificadas'])} iterations={v['iteraciones']}"
            )
            for o in v["ciclo"]:
                print(f"    {o['sentencia_id']}: {o['resultado']} (iteration {o['iteracion']})")
        if s.get("channel"):
            c = s["channel"]
            print(f"  filing channel ({c['ciudad']}): found={c['encontrado']} source={c.get('fuente_url')}")
        if "reviewer_summary" in s:
            print(f"  english summary: {'yes' if s['reviewer_summary'] else 'no'}")
        for e in s["escalations"]:
            print(f"  escalated {e['step']}: {e['from_model']} -> {e['to_model']} ({'; '.join(e['reasons'])})")
        for step, m in s["steps"].items():
            print(f"  {step:<9} {fmt_metrics(m)}")
        t = s.get("totals", {})
        print(
            f"  {'TOTAL':<9} latency={t.get('latency_s')}s cost=${t.get('cost_usd', 0):.6f} "
            f"(models ${t.get('llm_cost_usd', 0):.6f} + search ${t.get('search_cost_usd', 0):.6f})"
        )
        print(f"  {'TAVILY':<9} {fmt_tavily(t)}")
        print(f"  {'ROUTING':<9} {fmt_routing(t)}")
        print("  models per step: " + ", ".join(
            f"{step}={(m.get('final_model') or '-').split('/')[-1]}" for step, m in s["steps"].items()
        ))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", nargs="?", default="01")
    parser.add_argument("--all", action="store_true", help="run every demo case")
    parser.add_argument("--quiet", action="store_true", help="only print metrics")
    parser.add_argument("--verbose", action="store_true", help="show LLM call logs")
    parser.add_argument("--record", metavar="DIR", help="save every SSE event of each case as DIR/<case>.json")
    parser.add_argument("--english-summary", action="store_true", help="add the English reviewer summary")
    parser.add_argument("--sample-documents", metavar="DIR", help="only (re)write the documents of DIR/*.json")
    parser.add_argument("--no-cache", action="store_true", help="search Tavily live instead of using the cache")
    args = parser.parse_args()
    if args.sample_documents:
        for recording in sorted(Path(args.sample_documents).glob("*.json")):
            write_sample_documents(recording)
        return
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    paths = [str(p) for p in sorted(DEMO_DIR.glob("[0-9]*.md"))] if args.all else [args.case]
    summaries = []
    for path in paths:
        title, narrative = load_case(path)
        # Recorded sample runs always include the English reviewer summary.
        summary, recorded = await run(
            title, narrative, args.quiet, args.english_summary or bool(args.record), use_cache=not args.no_cache
        )
        summaries.append(summary)
        if args.record:
            out = Path(args.record) / f"{resolve_case(path).stem}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            save_recording(out, {"title": title, "narrative": narrative, "events": recorded})
            print(f"recorded {len(recorded)} events -> {out}")
            write_sample_documents(out)
    if len(summaries) > 1:
        print_summary(summaries)


if __name__ == "__main__":
    asyncio.run(main())
