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
```

The default mode is the clean reference behavior. Defects are opt-in and may be combined:

```bash
BUGGY_SHOP_DEFECTS=invalid-email-accepted,unknown-user-200 go run .
```

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

Order time, amount, and payment-token relations remain natural-language business constraints. They are useful for comparing the deterministic Schema baseline with later semantic and Agent modes.

The OpenAPI Links on create responses feed generated IDs into later GET/DELETE happy paths. A current clean run executes 26 cases successfully and blocks four safely: three parameter-boundary candidates lack a declared validation response, and DELETE lacks explicit approval. With both defects enabled, 24 cases pass and two fail: the contract-backed invalid-email check and a heuristic unknown-resource probe. These figures apply only to this example version and saved run.
