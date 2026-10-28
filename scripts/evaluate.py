"""Run the 3 demo cases and the 10 evaluation cases and write docs/EVALUATION.md.

Usage (from the repo root):
    uv run --project backend python scripts/evaluate.py                  # all 13 cases, 3 at a time
    uv run --project backend python scripts/evaluate.py --only 10 11     # file name prefixes
    uv run --project backend python scripts/evaluate.py --from-json docs/eval_results.json   # re-render only
    uv run --project backend python scripts/evaluate.py --no-cache       # live Tavily searches only

Tavily results come from the search cache when possible (0 credits). Before running, the script prints an
upper bound of the live Tavily credits and asks for confirmation above CONFIRM_CREDITS (--yes skips it).

Each case runs the clarifying-question check (as the UI does before a live run; no answers are given) and
then the full pipeline. Raw results go to docs/eval_results.json; the report to docs/EVALUATION.md.
"""

import argparse
import asyncio
import json
import logging
import math
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8")

from app.config import get_settings  # noqa: E402
from app.pipeline import StepUsage, clarifying_questions, run_case  # noqa: E402
from app.search import CREDITS_PER_SEARCH, Searcher  # noqa: E402

DEMO_DIR = REPO_ROOT / "docs" / "demo_cases"
EVAL_DIR = REPO_ROOT / "docs" / "eval_cases"
RESULTS = REPO_ROOT / "docs" / "eval_results.json"
BASELINE = REPO_ROOT / "docs" / "eval_results_baseline.json"
REPORT = REPO_ROOT / "docs" / "EVALUATION.md"
LIMITATIONS = EVAL_DIR / "known_limitations.md"
STEPS = ("intake", "analysis", "research", "draft", "verify", "package")
CONFIRM_CREDITS = 150
# Live searches of an admissible case at most: research queries, filing channel, and a typical check loop.
MAX_RESEARCH_QUERIES, CHANNEL_QUERIES, TYPICAL_TARGETED = 4, 2, 2


def estimate_credits(cases: list[dict], use_cache: bool) -> int:
    """Upper bound of the live Tavily credits; cases expected not to be admissible do not search."""
    research = CREDITS_PER_SEARCH[get_settings().tavily_research_depth]
    per_case = MAX_RESEARCH_QUERIES * research + (CHANNEL_QUERIES + TYPICAL_TARGETED) * CREDITS_PER_SEARCH["basic"]
    searching = [c for c in cases if c["expected"].get("procede", True)]
    print(
        f"Tavily estimate: {len(searching)} of {len(cases)} cases search, up to {per_case} credits each "
        f"(research {get_settings().tavily_research_depth}) = up to {per_case * len(searching)} credits"
        + (" before cache hits." if use_cache else ", all live (--no-cache).")
    )
    return per_case * len(searching)


def load_cases(only: list[str]) -> list[dict]:
    expected = json.loads((EVAL_DIR / "expected.json").read_text(encoding="utf-8"))
    paths = sorted(DEMO_DIR.glob("[0-9]*.md")) + sorted(EVAL_DIR.glob("[0-9]*.md"))
    cases = []
    for path in paths:
        if only and not any(path.stem.startswith(o) for o in only):
            continue
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        cases.append({
            "id": path.stem,
            "title": lines[0].lstrip("# ").strip(),
            "narrative": "\n".join(lines[1:]).strip(),
            "expected": expected.get(path.stem, {}),
        })
    return cases


async def evaluate_case(case: dict, use_cache: bool = True) -> dict:
    out: dict = {
        "id": case["id"], "title": case["title"], "expected": case["expected"], "steps": {}, "skipped": [],
        "escalations": [], "completed": False, "error": None,
    }
    q_usage = StepUsage()
    try:
        out["questions"] = await clarifying_questions(case["narrative"], q_usage, get_settings().max_clarifying_questions)
    except Exception as exc:  # noqa: BLE001 - recorded in the report
        out["questions"], out["questions_error"] = [], f"{type(exc).__name__}: {exc}"
    out["questions_cost_usd"] = q_usage.metrics()["cost_usd"]

    start = time.perf_counter()
    async for event, data in run_case(case["narrative"], searcher=Searcher(use_cache)):
        if event in ("step_completed", "step_unavailable"):
            out["steps"][data["step"]] = {"metrics": data["metrics"], "result": data.get("result")}
            if event == "step_unavailable":
                out["error"] = f"{data['step']}: {data['reason']}"
        elif event == "step_skipped":
            out["skipped"].append(data["step"])
        elif event == "escalated":
            out["escalations"].append({"step": data["step"], "reasons": data["reasons"],
                                       "from": data["from_model"], "to": data["to_model"]})
        elif event == "step_failed":
            out["error"] = f"{data['step']}: {data['error']}"
        elif event in ("case_failed", "case_unavailable"):
            out["totals"] = data.get("totals")
            out["error"] = out["error"] or data.get("error")
        elif event == "case_completed":
            out["completed"] = True
            out["totals"] = data["totals"]
            out["package"] = data["package"]
    out["wall_s"] = round(time.perf_counter() - start, 2)
    return out


def summarize(r: dict) -> dict:
    """The per-case figures shown in the report."""
    steps = r["steps"]
    analysis = (steps.get("analysis") or {}).get("result") or {}
    proc = analysis.get("procedencia_tutela") or {}
    research = (steps.get("research") or {}).get("result") or {}
    verify = (steps.get("verify") or {}).get("result") or {}
    pkg = r.get("package") or {}
    totals = r.get("totals") or {}
    expected = r["expected"].get("procede")
    detected = proc.get("procede")
    final_ids = verify.get("citas_encontradas", [])
    initial_unverified = verify.get("citas_no_verificadas_iniciales", [])
    initial_cited = len(set(final_ids) | set(initial_unverified))
    # Results from before the Tavily accounting: cost_usd was models only and every search was advanced.
    llm = totals.get("llm_cost_usd", totals.get("cost_usd", 0.0))
    credits = totals.get("tavily_credits", CREDITS_PER_SEARCH["advanced"] * totals.get("tavily_searches", 0))
    search = credits * get_settings().tavily_usd_per_credit
    ultra = totals.get("cost_all_reasoning_usd", 0.0)
    return {
        "steps_done": len(steps),
        "skipped": r["skipped"],
        "procede": detected,
        "procede_expected": expected,
        "procede_match": None if expected is None or detected is None else detected == expected,
        "razon": proc.get("razon", ""),
        "paso_previo": proc.get("paso_previo"),
        "drafted": bool(pkg.get("draft")),
        "rulings_found": [p["sentencia_id"] for p in research.get("precedentes", [])],
        "cited_before": initial_cited,
        "verified_before": initial_cited - len(initial_unverified),
        "unverified_before": len(initial_unverified),
        "verified_after": len(verify.get("citas_verificadas", [])),
        "unverified_after": len(verify.get("citas_no_verificadas", [])),
        "loop": [f"{o['sentencia_id']} {o['resultado']}" for o in verify.get("ciclo", [])],
        "questions": r.get("questions", []),
        "latency_s": r.get("wall_s", 0.0),
        "cost_usd": llm + search,
        "llm_cost_usd": llm,
        "search_cost_usd": search,
        "tavily_credits": credits,
        "tavily_live": totals.get("tavily_live", totals.get("tavily_searches", 0)),
        "tavily_cached": totals.get("tavily_cached", 0),
        "cost_all_ultra_usd": ultra,
        # Routing compares models only: Tavily costs the same whichever model runs each step.
        "ratio": ultra / llm if llm else None,
        "questions_cost_usd": r.get("questions_cost_usd", 0.0),
    }


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def _cost_rows(label: str, cases: list[dict]) -> list[str]:
    """Average cost, routing saving and latency percentiles over `cases`."""
    if not cases:
        return [f"| Per {label} | n/a |"]
    ratios = [s["ratio"] for s in cases if s["ratio"]]
    latencies = [s["latency_s"] for s in cases]
    mean = lambda key: statistics.mean(s[key] for s in cases)  # noqa: E731
    return [
        f"| Average cost per {label} | ${mean('cost_usd'):.4f} = models ${mean('llm_cost_usd'):.4f} + Tavily "
        f"${mean('search_cost_usd'):.4f} ({mean('tavily_credits'):.1f} credits; "
        f"{mean('tavily_live'):.1f} live / {mean('tavily_cached'):.1f} cached searches) |",
        f"| Models only, per {label} | ${mean('llm_cost_usd'):.4f} (all on Ultra: ${mean('cost_all_ultra_usd'):.4f}); "
        f"routing is {statistics.mean(ratios):.1f}× cheaper on average |",
        f"| Latency per {label}, p50 / p95 | {percentile(latencies, 50):.1f} s / {percentile(latencies, 95):.1f} s |",
    ]


def yes_no(value: bool | None) -> str:
    return "n/a" if value is None else ("yes" if value else "no")


def render(results: list[dict], meta: dict) -> str:
    rows = [(r, summarize(r)) for r in results]
    n = len(rows)
    completed = [s for r, s in rows if r["completed"]]
    with_expectation = [s for _, s in rows if s["procede_match"] is not None]
    matches = sum(s["procede_match"] for s in with_expectation)
    escalated_cases = sum(bool(r["escalations"]) for r, _ in rows)
    escalations = sum(len(r["escalations"]) for r, _ in rows)
    unverified_final = sum(s["unverified_after"] for _, s in rows)
    drafted = [s for _, s in rows if s["drafted"]]


    settings = get_settings()

    out = [
        "# Evaluation",
        "",
        f"Generated by `scripts/evaluate.py` on {meta['date']} with `{settings.model_fast}` (fast), "
        f"`{settings.model_draft}` (draft) and `{settings.model_reasoning}` (reasoning). "
        f"{n} synthetic cases: the 3 demo cases (`docs/demo_cases/`) and 10 evaluation cases (`docs/eval_cases/`). "
        "Each case first runs the clarifying-question check, as the UI does, without answering it, and then the "
        "full pipeline. Raw results: [`eval_results.json`](eval_results.json).",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Runs completed without errors | {len(completed)}/{n} ({len(completed) / n:.0%}) |",
        f"| Admissibility as expected | {matches}/{len(with_expectation)} (cases with an expected answer) |",
        f"| Tutelas drafted | {len(drafted)}/{n} |",
        f"| Cases with at least one escalation | {escalated_cases}/{n} ({escalated_cases / n:.0%}); {escalations} escalations in total |",
        f"| Unverified citations in final documents | **{unverified_final}** |",
        *_cost_rows("drafted tutela", drafted),
        *_cost_rows("case, all 13" if n == 13 else f"case, all {n}", completed),
        f"| Not-admissible cases (no draft) | {len(completed) - len(drafted)} cases, "
        f"${statistics.mean(s['cost_usd'] for s in completed if not s['drafted']):.4f} and "
        f"{statistics.mean(s['latency_s'] for s in completed if not s['drafted']):.1f} s on average |"
        if len(completed) > len(drafted) else "| Not-admissible cases | none |",
        f"| Clarifying-question check | ${sum(s['questions_cost_usd'] for _, s in rows) / n:.4f} per case on average, not included above |",
        "",
        "Costs are estimates: models from per-token prices, Tavily at "
        f"${settings.tavily_usd_per_credit} per credit (basic search 1 credit, advanced 2; cache hits 0). "
        "“All on Ultra” prices the same model calls and tokens at the reasoning model's rate (a lower bound: Ultra "
        "would also spend thinking tokens); the routing saving compares models only, since Tavily costs the same "
        f"either way. Main case-law search depth: {meta.get('research_depth', 'advanced')}. "
        "Results recorded before the Tavily accounting (no `research_depth` in their metadata) used advanced for "
        "every search and no cache; their credits are counted as 2 per search. Latency is wall-clock time for the pipeline, without the question check.",
        "",
        "## Per case",
        "",
        "| Case | Group | Steps | Admissible (expected) | Rulings found | Citations before loop (verified / unverified) | After loop | Escalations | Questions | Latency | Cost (models + Tavily) | Tavily credits (live / cached searches) | Models all-Ultra | Models saving |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r, s in rows:
        steps = f"{s['steps_done']}/6" + (f" ({len(s['skipped'])} skipped)" if s["skipped"] else "")
        if not r["completed"]:
            steps += " **failed**"
        mark = "" if s["procede_match"] is None else (" ✓" if s["procede_match"] else " ✗")
        admissible = f"{yes_no(s['procede'])} ({yes_no(s['procede_expected'])}){mark}"
        cited = f"{s['verified_before']} / {s['unverified_before']}" if s["cited_before"] or s["drafted"] else "–"
        after = f"{s['verified_after']} / {s['unverified_after']}" if s["drafted"] else "–"
        ratio = f"{s['ratio']:.1f}×" if s["ratio"] else "–"
        out.append(
            f"| {r['id']} | {r['expected'].get('group', '')} | {steps} | {admissible} | {len(s['rulings_found'])} | "
            f"{cited} | {after} | {len(r['escalations'])} | {len(s['questions'])} | {s['latency_s']:.1f} s | "
            f"${s['cost_usd']:.4f} (${s['llm_cost_usd']:.4f} + ${s['search_cost_usd']:.4f}) | "
            f"{s['tavily_credits']} ({s['tavily_live']} / {s['tavily_cached']}) | ${s['cost_all_ultra_usd']:.4f} | {ratio} |"
        )
    out += ["", "## Details", ""]
    for r, s in rows:
        out += [f"### {r['id']}: {r['title']}", ""]
        exp = r["expected"]
        out.append(f"- **Expected:** {exp.get('kind', '')}; admissible: {yes_no(exp.get('procede'))}"
                   + (f"; next step: {exp['next_step']}" if exp.get("next_step") else "") + ".")
        if r["error"]:
            out.append(f"- **Error:** {r['error']}")
        out.append(f"- **Admissibility detected:** {yes_no(s['procede'])}. {s['razon']}")
        if s["paso_previo"]:
            out.append(f"- **Step to take first:** {s['paso_previo']}")
        out.append(f"- **Tutela drafted:** {yes_no(s['drafted'])}" + (f"; skipped steps: {', '.join(s['skipped'])}" if s["skipped"] else ""))
        if s["rulings_found"]:
            out.append(f"- **Rulings found:** {', '.join(s['rulings_found'])}")
        if s["drafted"]:
            out.append(
                f"- **Citations:** {s['cited_before']} cited in the first draft, {s['unverified_before']} unverified; "
                f"after the loop {s['verified_after']} verified, {s['unverified_after']} unverified"
                + (f" ({'; '.join(s['loop'])})" if s["loop"] else "") + "."
            )
        for e in r["escalations"]:
            out.append(f"- **Escalation ({e['step']}):** {e['from'].split('/')[-1]} → {e['to'].split('/')[-1]}: {'; '.join(e['reasons'])}")
        if s["questions"]:
            out.append("- **Clarifying questions:** " + " / ".join(f"“{q}”" for q in s["questions"]))
        else:
            out.append("- **Clarifying questions:** none")
        models = ", ".join(
            f"{step} {((r['steps'][step]['metrics'].get('final_model') or 'no LLM').split('/')[-1])}"
            for step in STEPS if step in r["steps"]
        )
        out += [f"- **Models:** {models}", ""]

    if BASELINE.exists():
        base = {r["id"]: summarize(r) for r in json.loads(BASELINE.read_text(encoding="utf-8"))["results"]}
        out += [
            "## Before the admissibility fix",
            "",
            f"The first evaluation ({json.loads(BASELINE.read_text(encoding='utf-8'))['meta']['date']}, "
            "[`eval_results_baseline.json`](eval_results_baseline.json)) drafted a tutela in every case, even "
            "when its own analysis said it was not admissible. Since then, a case the analysis finds not admissible "
            "skips research, drafting and the citation check, and delivery explains why and what to do first.",
            "",
            "| Case | Before: admissible / drafted | Now: admissible / drafted |",
            "| --- | --- | --- |",
        ]
        for r, s in rows:
            b = base.get(r["id"])
            if b and (b["procede"] != s["procede"] or b["drafted"] != s["drafted"] or r["expected"].get("group") != "admissible"):
                out.append(f"| {r['id']} | {yes_no(b['procede'])} / {yes_no(b['drafted'])} | {yes_no(s['procede'])} / {yes_no(s['drafted'])} |")
        out.append("")

    out += ["## Known limitations", ""]
    if LIMITATIONS.exists():
        out.append(LIMITATIONS.read_text(encoding="utf-8").strip())
    observed = []
    for r, s in rows:
        if s["procede_match"] is False:
            observed.append(f"- {r['id']}: admissibility was {yes_no(s['procede'])}, expected {yes_no(s['procede_expected'])}.")
        if r["expected"].get("expect_questions") and not s["questions"]:
            observed.append(f"- {r['id']}: no clarifying questions for a story that needs them.")
        if not r["expected"].get("expect_questions") and s["questions"] and r["expected"].get("group") in ("demo", "admissible"):
            observed.append(f"- {r['id']}: {len(s['questions'])} clarifying question(s) for a complete story.")
        if not r["completed"]:
            observed.append(f"- {r['id']}: the run failed ({r['error']}).")
        for step in r["steps"].values():
            for e in step["metrics"]["escalations"]:
                if e.get("remaining_issues"):
                    observed.append(f"- {r['id']}: after escalating {e['step']}, still: {'; '.join(e['remaining_issues'])}.")
    if observed:
        out += ["", "Observed in this run:", "", *observed]
    return "\n".join(out) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=[], help="case file name prefixes")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--from-json", help="re-render the report from saved results")
    parser.add_argument("--no-cache", action="store_true", help="search Tavily live instead of using the cache")
    parser.add_argument("--yes", action="store_true", help=f"do not ask for confirmation above {CONFIRM_CREDITS} credits")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    if args.from_json:
        saved = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        REPORT.write_text(render(saved["results"], saved["meta"]), encoding="utf-8")
        print(f"report -> {REPORT}")
        return

    cases = load_cases(args.only)
    estimate = estimate_credits(cases, use_cache=not args.no_cache)
    if estimate > CONFIRM_CREDITS and not args.yes:
        if input(f"That may exceed {CONFIRM_CREDITS} credits. Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            raise SystemExit("Cancelled.")
    semaphore = asyncio.Semaphore(args.concurrency)

    async def one(case: dict) -> dict:
        async with semaphore:
            print(f"start {case['id']}", flush=True)
            result = await evaluate_case(case, use_cache=not args.no_cache)
            print(f"done  {case['id']} completed={result['completed']} {result['wall_s']}s", flush=True)
            return result

    results = await asyncio.gather(*(one(c) for c in cases))
    meta = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "research_depth": get_settings().tavily_research_depth,
        "cache": not args.no_cache,
    }
    RESULTS.write_text(json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
    REPORT.write_text(render(results, meta), encoding="utf-8")
    for r in results:
        s = summarize(r)
        print(f"{r['id']:<30} procede={s['procede']} (exp {s['procede_expected']}) drafted={s['drafted']} "
              f"rulings={len(s['rulings_found'])} unverified_final={s['unverified_after']} esc={len(r['escalations'])} "
              f"q={len(s['questions'])} {s['latency_s']}s ${s['cost_usd']:.4f} credits={s['tavily_credits']} "
              f"live/cached={s['tavily_live']}/{s['tavily_cached']}")
    print(f"results -> {RESULTS}\nreport  -> {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
