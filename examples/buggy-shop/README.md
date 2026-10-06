# Buggy Shop

The current service is the deliberately small subset used by the M0 spike and M1 deterministic baseline.

```bash
go run .
```

It listens on `127.0.0.1:18080` and stores users and orders in memory. The public contract contains:

```text
POST   /users
GET    /users/{id}
DELETE /users/{id}
POST   /orders
GET    /orders/{id}
PATCH  /orders/{id}
```

The default mode is the clean reference behavior. Defects are opt-in and may be combined:

```bash
BUGGY_SHOP_DEFECTS=invalid-email-accepted,unknown-user-200 go run .
```

Available defect switches are:

- `invalid-email-accepted`
- `unknown-user-200`
- `duplicate-email-accepted`
- `confirm-conflict`

The expanded contract also requires unique user emails and models order state
transitions (`created → confirmed/cancelled`, `confirmed → cancelled`). These cases
exercise repeated-request policy, stateful workflows, and failure observations that
cannot be explained by request Schema alone.

`/test/reset` is registered only when `BUGGY_SHOP_TEST_MODE=1`; it is deliberately absent from `openapi.yaml` and API Doctor never calls it:

```bash
BUGGY_SHOP_TEST_MODE=1 go run .
curl -X POST http://127.0.0.1:18080/test/reset
```

From the repository root, run the deterministic baseline with:

```bash
.venv/bin/api-doctor run \
  --spec examples/buggy-shop/openapi.yaml \
  --base-url http://127.0.0.1:18080 \
  --max-tests 30 \
  --output runs/m1/baseline
```

Order time, amount, payment-token, unique-email, and order-state relations remain natural-language business constraints. They are useful for comparing the deterministic Schema baseline with semantic and Agent modes.

The OpenAPI Links on create responses feed generated IDs into later GET, PATCH, and DELETE operations. Exact candidate totals are asserted by automated tests and saved run artifacts rather than duplicated here, because the demo contract evolves with each milestone.
