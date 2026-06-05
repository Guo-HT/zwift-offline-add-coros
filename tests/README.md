# COROS integration self-tests

This directory contains a self-contained pytest suite for the COROS activity
sync feature added to zwift-offline. The tests cover the new code only —
they do **not** exercise the rest of the project, and they do **not** make
any real network calls to COROS or any other service.

## What is covered

| File                              | What it tests                                                                    |
|-----------------------------------|----------------------------------------------------------------------------------|
| `test_coros_helpers.py`           | Pure helpers: `oss/ali_oss_client._decode_credentials`, `oss/aws_oss_client._decode_credentials`, the `SIGNS` and `BUCKET_TO_REGION` constants |
| `test_oss_sts.py`                 | `oss.ali_oss_client.fetch_sts` / `oss.aws_oss_client.fetch_sts` — success, non-200 code, HTTP error, unknown bucket, hardcoded URL params (catches sign drift) |
| `test_oss_upload.py`              | `oss.ali_oss_client.upload` / `oss.aws_oss_client.upload` — multipart upload, single-part path, default bucket, region inference, `TransferConfig` settings, temp file cleanup |
| `test_coros_client.py`            | `CorosClient.login()` and `upload_activity()` — region dispatch (1/2/3), login failure modes, import API success/failure, header casing (`accesstoken` lowercase), unknown regionId, not-logged-in early return |
| `test_coros_upload.py`            | `coros_upload()` inside `zwift_offline.py` — missing creds, .bin creds, .txt creds, trailing-newline handling, login raise swallowing, `coros_client` import error swallowing, .bin precedence over .txt |
| `test_coros_routes.py`            | Flask routes `/coros/<u>/` and `/delete/coros_credentials.bin` — GET, POST, validation, redirect targets, whitelist guard |
| `test_coros_e2e.py`               | End-to-end smoke: `activity_uploads()` calls all 6 platforms (COROS last); full COROS stack from `coros_upload` through to `_call_import_api`; failure isolation |
| `conftest.py`                     | Shared fixtures (`test_storage_dir`, `fresh_zwift_offline`, `mock_requests`) — provides per-test DB isolation and a controllable HTTP mock |

## How to run

```sh
cd /Users/mac/Code/opensource/zwift-offline
python3 -m pytest tests/ -v
```

Expected: **62 passed**.

## What is intentionally NOT tested

- The real COROS API. All HTTP is mocked via the `mock_requests` fixture.
- The real `oss2` / `boto3` SDK calls. They are mocked with `MagicMock` so we
  don't talk to AliOSS or AWS S3 during the test run.
- The live STS signs for `coros-s3` (regionId=1) — the code currently uses
  the placeholder `S3SIGN_COROS_S3` and will fail at runtime until the real
  sign is captured. This is the only `Important` finding from the design
  review that's been left as a live-API verification step.

## Test isolation

Each test that touches the database gets a fresh SQLite file at a unique
`tmp_path`. The `fresh_zwift_offline` fixture:

1. Wipes any prior DB file at the test's path.
2. Re-points `STORAGE_DIR` and `DATABASE_PATH` at the new path.
3. Re-builds the SQLAlchemy engine against the new URI (flask_sqlalchemy
   caches engines per-app, so we have to dispose + recreate them).
4. Re-initializes Flask-Login on the app (in production this happens inside
   `run_standalone()`).
5. Calls `db.create_all()` to provision the schema.

This means tests can run in any order without contaminating each other.
