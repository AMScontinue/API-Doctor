# M2 Analyzer evaluation

These scenarios evaluate the observation-driven Analyzer against the real
`examples/buggy-shop` HTTP service and DeepSeek. They are not unit-test
fixtures and are intentionally excluded from the normal test suite.

## Core scenario: broken order confirmation

Start a fresh service process:

```bash
cd examples/buggy-shop
BUGGY_SHOP_DEFECTS=confirm-conflict BUGGY_SHOP_TEST_MODE=1 go run .
```

In another shell, run the complete Agent pipeline:

```bash
.venv/bin/api-doctor agent-run \
  --spec examples/buggy-shop/openapi.yaml \
  --rules examples/buggy-shop/rules.yaml \
  --base-url http://127.0.0.1:18080 \
  --max-tests 20 \
  --max-steps 30 \
  --output runs/m2/analyzer-confirm-conflict
```

The Agent must not call `/test/reset`. Restart the in-memory service, or have
the user call the reset endpoint, before another comparable run.

This scenario is successful when the saved artifacts demonstrate all of the
following without validator bypasses:

1. a validated workflow sends `status=confirmed` to an order created in the
   same workflow and observes the unexpected 409;
2. Analyzer creates a stable, evidence-linked hypothesis;
3. Analyzer adds at least one diagnostic case using a host-provided
   `memory_key`;
4. the diagnostic case passes deterministic OpenAPI, duplicate, safety and
   budget checks and is sent to the real service;
5. a later Analyzer call updates the same hypothesis to `confirmed` or
   `rejected` using exact trajectory evidence references;
6. `finish_investigation` resumes the remaining initial plan;
7. checkpoint, trajectory, run, report and pipeline artifacts are present.

`revise_pending_actions`, guarded `finish_run`, invalid-decision rollback and
one-time diagnostic replay are deterministic controller policies. They remain
covered by unit tests unless a real service scenario naturally justifies those
choices; the benchmark must not force an LLM to choose them merely to improve
a demo score.

## Control scenarios

- Clean service: run without `BUGGY_SHOP_DEFECTS`; use it to detect false
  positives and unnecessary diagnosis.
- Direct contract defects: `invalid-email-accepted`, `unknown-user-200`, or
  `duplicate-email-accepted`; these test whether Analyzer avoids wasteful
  experiments when the failure is already directly evidenced.
- Combined defects: enable multiple switches only after the single-defect
  scenarios are stable; use this to assess budget allocation and hypothesis
  separation, not as the first debugging signal.

## Infrastructure scenario: backend unavailable

Do not start buggy-shop and run the same command against
`http://127.0.0.1:18080` with a smaller budget. A sound Agent should not report
connection refusal as an API contract defect. It should gather bounded
reachability evidence, distinguish transport failure from HTTP behavior, and
use guarded `finish_run` once continued API testing has no information value.

The selected real run in `backend-unavailable-summary.json` observed two
independent workflow connection failures, executed one minimal GET diagnostic,
confirmed a high-confidence `server_unreachable` hypothesis, retained one
unexecuted pending action, and terminated through `finish_run` after only three
HTTP attempts. Its first diagnostic proposal duplicated an executed request
without `repeat=once`; deterministic validation rejected that proposal and
rolled back its hypothesis before the Agent gathered independent evidence.

Model output is nondeterministic. Keep the raw ignored `runs/` directory for
local audit and commit only a compact factual summary of a selected run.

Selected control results are saved in `clean-control-summary.json` and
`duplicate-email-summary.json`. The clean run produced no failed/error
Observation, no hypothesis and no Analyzer call. One LLM workflow was safely
blocked because it omitted the required `repeat=once`; this is a proposal
quality limitation, not a service false positive. The duplicate-email run used
a GET diagnostic to rule out non-persistence before confirming the uniqueness
defect and resuming the remaining plan.

The selected budget results are saved in `budget-management-summary.json`.
They compare a run with enough room for a diagnostic plus follow-up analysis
against a tight run where the Analyzer correctly leaves the hypothesis open,
skips diagnosis, and spends the final budgets on independent coverage.
