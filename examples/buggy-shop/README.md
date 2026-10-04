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
  --max-tests 22 \
  --output runs/m1/baseline
```

Order time, amount, and payment-token relations remain natural-language business constraints. They are useful for comparing the deterministic Schema baseline with later semantic and Agent modes.

For the current contract, the clean service produces 22/22 passing deterministic cases. With both defects enabled, the request-body baseline detects `invalid-email-accepted`; detecting `unknown-user-200` requires the upcoming create/query dependency flow or an explicit not-found probe.
