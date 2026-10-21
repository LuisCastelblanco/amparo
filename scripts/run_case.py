"""Run the full pipeline on a demo case and print every step.

Usage (from the repo root):
    uv run --project backend python scripts/run_case.py 01           # file name prefix in docs/demo_cases
    uv run --project backend python scripts/run_case.py path/to/case.md
    uv run --project backend python scripts/run_case.py --all --quiet
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8")

from app.pipeline import run_case  # noqa: E402

DEMO_DIR = REPO_ROOT / "docs" / "demo_cases"


def load_case(arg: str) -> tuple[str, str]:
    path = Path(arg)
    if not path.exists():
        matches = sorted(DEMO_DIR.glob(f"{arg}*.md"))
        if not matches:
            raise SystemExit(f"No demo case matches {arg!r} in {DEMO_DIR}")
        path = matches[0]
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
        parts.append(f"tavily_searches={m['tavily_searches']}")
    return " ".join(parts)


async def run(title: str, narrative: str, quiet: bool) -> dict:
    print(f"\n{'#' * 78}\n# {title}\n{'#' * 78}")
    summary: dict = {"title": title, "steps": {}, "completed": False}
    async for event, data in run_case(narrative):
        if event == "step_started":
            print(f"\n--- [{data['index']}/{data['total']}] {data['step']} ...")
        elif event == "step_completed":
            summary["steps"][data["step"]] = data["metrics"]
            print(f"    OK  {fmt_metrics(data['metrics'])}")
            if not quiet and data["step"] not in ("draft", "verify", "package"):
                print(json.dumps(data["result"], ensure_ascii=False, indent=2))
        elif event == "step_failed":
            summary["failed"] = data
            print(f"    FAILED {data['error']}")
        elif event == "case_failed":
            summary["totals"] = data["totals"]
        elif event == "case_completed":
            pkg = data["package"]
            summary.update(completed=True, totals=data["totals"], verification=pkg["verification"])
            if not quiet:
                guidance = {k: pkg[k] for k in ("checklist_anexos", "donde_radicar", "que_hacer_si_no_cumplen", "aviso")}
                print(f"\n{'=' * 78}\nDRAFT\n{'=' * 78}\n{pkg['draft']['texto_completo']}")
                print(f"\n{'=' * 78}\nVERIFICATION\n{'=' * 78}")
                print(json.dumps(pkg["verification"], ensure_ascii=False, indent=2))
                print(f"\n{'=' * 78}\nFILING GUIDANCE\n{'=' * 78}")
                print(json.dumps(guidance, ensure_ascii=False, indent=2))
    t = summary.get("totals", {})
    print(
        f"\nTOTAL tokens in/out={t.get('input_tokens')}/{t.get('output_tokens')} "
        f"latency={t.get('latency_s')}s cost=${t.get('cost_usd', 0):.6f}"
    )
    return summary


def print_summary(summaries: list[dict]) -> None:
    print(f"\n{'#' * 78}\n# SUMMARY\n{'#' * 78}")
    for s in summaries:
        v = s.get("verification") or {}
        status = "6/6 steps completed" if s["completed"] else f"FAILED at {s['failed']['step']}"
        print(f"\n{s['title']}\n  {status}")
        if v:
            print(
                f"  citations: found={len(v['citas_encontradas'])} verified={len(v['citas_verificadas'])} "
                f"unverified={len(v['citas_no_verificadas'])}"
            )
        for step, m in s["steps"].items():
            print(f"  {step:<9} {fmt_metrics(m)}")
        t = s.get("totals", {})
        print(f"  {'TOTAL':<9} latency={t.get('latency_s')}s cost=${t.get('cost_usd', 0):.6f}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", nargs="?", default="01")
    parser.add_argument("--all", action="store_true", help="run every demo case")
    parser.add_argument("--quiet", action="store_true", help="only print metrics")
    parser.add_argument("--verbose", action="store_true", help="show LLM call logs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    paths = [str(p) for p in sorted(DEMO_DIR.glob("[0-9]*.md"))] if args.all else [args.case]
    summaries = [await run(*load_case(p), args.quiet) for p in paths]
    if len(summaries) > 1:
        print_summary(summaries)


if __name__ == "__main__":
    asyncio.run(main())
