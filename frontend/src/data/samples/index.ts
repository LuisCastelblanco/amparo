// Recorded runs of the synthetic demo cases, regenerated with:
//   uv run --project backend python scripts/run_case.py --all --quiet --record frontend/src/data/samples
import type { SampleRun } from "@/lib/types";

import case01 from "./01-medicamento-no-pbs.json";
import case02 from "./02-cirugia-sin-fecha.json";
import case03 from "./03-transporte-adulto-mayor.json";

export const SAMPLE_RUNS = [case01, case02, case03] as unknown as SampleRun[];
