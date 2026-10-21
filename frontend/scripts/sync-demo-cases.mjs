// Copies the synthetic demo cases from docs/demo_cases into src/data/demoCases.json.
// Runs before dev/build. If docs/ is not available (e.g. a deploy that only ships
// frontend/), the committed JSON is kept as is.
import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = join(here, "..", "..", "docs", "demo_cases");
const target = join(here, "..", "src", "data", "demoCases.json");

if (!existsSync(source)) {
  console.log(`[sync-demo-cases] ${source} not found, keeping existing demoCases.json`);
  process.exit(0);
}

const cases = readdirSync(source)
  .filter((name) => /^\d.*\.md$/.test(name))
  .sort()
  .map((name) => {
    const [first, ...rest] = readFileSync(join(source, name), "utf8").trim().split(/\r?\n/);
    return {
      id: name.replace(/\.md$/, ""),
      title: first.replace(/^#\s*/, "").trim(),
      narrative: rest.join("\n").trim(),
    };
  });

writeFileSync(target, JSON.stringify(cases, null, 2) + "\n", "utf8");
console.log(`[sync-demo-cases] wrote ${cases.length} cases to src/data/demoCases.json`);
