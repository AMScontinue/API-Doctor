# Repository instructions

## Source of truth

Before planning or implementing changes, read `EXECUTION_DESIGN.md`. It is the single source of truth for product scope, architecture, data models, safety constraints, milestones, and acceptance criteria. `README.md` is only a user-facing overview.

If implementation and design conflict, report the conflict before changing scope. Update `EXECUTION_DESIGN.md` when a product or architecture decision changes; do not add new decisions to the legacy implementation-spec document.

## Implementation boundaries

- Keep deterministic validation, safety policy, HTTP execution, budgets, redaction, and persistence outside the LLM.
- Treat all LLM output as untrusted input and validate it with typed schemas before use.
- Do not execute arbitrary model-generated code on the host.
- Only send requests to the configured base URL and revalidate redirect targets.
- POST, PUT, and PATCH are allowed within policy; DELETE requires explicit human approval.
- The Agent must never call the demo service reset endpoint. Reset is a user-operated demo-service action.
- Do not hard-code buggy-shop paths, operation IDs, field names, credentials, or defect knowledge in generic Agent code or prompts.
- Do not add MCP, RAG, multi-agent orchestration, a Web UI, or Docker as a required dependency unless the design is explicitly revised.

## Development workflow

- Implement milestones in the order defined by `EXECUTION_DESIGN.md`.
- Start with the deterministic baseline and the TestSpec technical spike before expanding Agent behavior.
- Run relevant tests after every milestone.
- Never fabricate API responses, coverage, benchmark results, defect-detection rates, or model metrics.
- Keep secrets in environment variables and redact Authorization, Cookie, API keys, and tokens from logs and artifacts.
- Generated pytest must be executed before it is described as runnable.
- Preserve unrelated user changes and keep commits and patches scoped to the requested task.

## Delivery expectations

Every completed feature should include proportional unit or integration tests, structured failure handling, and any necessary documentation updates. End-to-end claims require a real saved run with its trajectory and report artifacts.
