# Buggy Shop

The current service is the deliberately small subset needed by the TestSpec technical spike. M1 will expand it into the complete configurable buggy-shop demonstration service.

```bash
go run .
```

It listens on `127.0.0.1:18080` and implements `POST /orders` and `POST /users`. It has no reset endpoint because M0 stores no state.
