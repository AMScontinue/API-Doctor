# Buggy Shop

The current service is the deliberately small subset used by the M0 spike and M1 deterministic baseline.

```bash
go run .
```

It listens on `127.0.0.1:18080` and implements `POST /orders` and `POST /users`. It has no reset endpoint because M0 stores no state.

From the repository root, run the deterministic baseline with:

```bash
.venv/bin/api-doctor run \
  --spec examples/buggy-shop/openapi.yaml \
  --base-url http://127.0.0.1:18080 \
  --max-tests 21 \
  --output runs/m1/baseline
```

The service is intentionally incomplete: it does not validate email format, while the order endpoint enforces cross-field time and amount rules that JSON Schema does not fully describe. These behaviors are useful for comparing the deterministic baseline with later semantic/Agent modes.
