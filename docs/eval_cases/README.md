# Evaluation cases

Ten more synthetic cases for `scripts/evaluate.py`, which runs them together with the three demo cases in
`docs/demo_cases/` and writes `docs/EVALUATION.md`. Names, EPS names, dates and places are invented.

- `04`-`09`: the tutela is admissible (diagnostic test, minor, disability, treatment interrupted by an EPS
  change, drug, surgery without a date).
- `10`-`11`: the tutela is not the right tool yet (no prior request to the EPS; mistreatment, no service denied).
- `12`-`13`: adversarial (very short and ambiguous; contradictory data).

`expected.json` holds the expected admissibility (`procede`; `null` when there is no single right answer) and
whether clarifying questions are expected.
