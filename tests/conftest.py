"""Shared pytest fixtures for COROS integration self-tests.

These tests MUST NOT hit the real COROS servers. Every external HTTP call and
every heavy SDK (`oss2`, `boto3`) is mocked. Only the project's own Python
modules are exercised for real.

ISOLATION STRATEGY
==================

`zwift_offline` initializes a SQLite database at `STORAGE_DIR/zwift-offline.db`
at module import time. To avoid sharing the DB across tests, the
`fresh_zwift_offline` fixture, for each test:

  1. Wipes any pre-existing DB file at the test's tmp_path.
  2. Points STORAGE_DIR and DATABASE_PATH at the test's tmp_path.
  3. Updates the Flask app's SQLALCHEMY_DATABASE_URI to the new DB.
  4. Disposes the cached SQLAlchemy engine so the next access rebuilds
     against the new URI.
  5. Calls db.create_all() to provision the schema.

We deliberately do NOT reload zwift_offline. Reloading re-binds `db` to a
fresh SQLAlchemy instance, which would orphan the model classes (User,
Activity, ...) that were declared with the original `db` reference. The
single import + per-test re-bind of URI is enough for isolation.
"""

import os
import sys
import types
from unittest.mock import MagicMock

import pytest


# --- 1. Seed STORAGE_DIR via env var (defensive — real binding is via fixture).
TEST_STORAGE = '/tmp/zo_coros_test_storage'
os.makedirs(TEST_STORAGE, exist_ok=True)
os.environ.setdefault('STORAGE_DIR', TEST_STORAGE)


# --- 2. Pre-import zwift_offline so the module is loaded once. Tests reuse it.
#        This avoids the SQLAlchemy model-orphan problem that reload causes.
import zwift_offline as _zo_loaded  # noqa: F401


# --- 3. Provide a fake `oss2` / `boto3` if the real ones fail to import.
try:
    import oss2  # noqa: F401
except Exception:  # pragma: no cover
    sys.modules['oss2'] = types.ModuleType('oss2')
    sys.modules['oss2'].StsAuth = MagicMock()
    sys.modules['oss2'].Bucket = MagicMock()
    sys.modules['oss2'].SizedFileAdapter = MagicMock()
    sys.modules['oss2'].determine_part_size = MagicMock(return_value=5 * 1024 * 1024)
    models_mod = types.ModuleType('oss2.models')
    models_mod.PartInfo = lambda *a, **k: None
    sys.modules['oss2.models'] = models_mod

try:
    import boto3  # noqa: F401
    from boto3.s3.transfer import TransferConfig  # noqa: F401
except Exception:  # pragma: no cover
    sys.modules['boto3'] = types.ModuleType('boto3')
    sys.modules['boto3'].client = MagicMock()
    boto3_s3 = types.ModuleType('boto3.s3')
    boto3_transfer = types.ModuleType('boto3.s3.transfer')
    boto3_transfer.TransferConfig = MagicMock()
    sys.modules['boto3.s3'] = boto3_s3
    sys.modules['boto3.s3.transfer'] = boto3_transfer


@pytest.fixture
def test_storage_dir():
    """Per-test isolated storage dir; cleaned up at teardown."""
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix='zo_coros_')
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fresh_zwift_offline(test_storage_dir, monkeypatch):
    """Yield a zwift_offline module bound to a fresh tmp_path STORAGE_DIR.

    See module docstring for the isolation strategy.
    """
    import zwift_offline as zo
    db_path = os.path.join(test_storage_dir, 'zwift-offline.db')
    if os.path.exists(db_path):
        os.unlink(db_path)

    # Re-bind module-level constants and the app's DB URI
    monkeypatch.setattr(zo, 'STORAGE_DIR', test_storage_dir, raising=False)
    monkeypatch.setattr(zo, 'DATABASE_PATH', db_path, raising=False)
    zo.app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'

    # flask_sqlalchemy caches engines in `_app_engines[app][bind_key]`.
    # The cache is built eagerly in init_app() and NOT rebuilt lazily — so
    # if we just clear it, `engines[None]` raises KeyError. We must dispose
    # the old engine and create a new one against the new URI ourselves.
    import sqlalchemy as sa
    engines_dict = zo.db._app_engines.setdefault(zo.app, {})
    for engine in list(engines_dict.values()):
        try:
            engine.dispose()
        except Exception:
            pass
    engines_dict.clear()
    engines_dict[None] = sa.create_engine(f'sqlite:///{db_path}')

    with zo.app.app_context():
        zo.db.create_all()

    # Initialize the LoginManager on the app. In production this happens
    # inside run_standalone(); we replicate it here so /login/ works in
    # tests without running the full standalone bootstrap.
    if not hasattr(zo.app, 'login_manager') or zo.app.login_manager is None:
        from flask_login import LoginManager
        lm = LoginManager()
        lm.login_view = 'login'
        lm.session_protection = None
        lm.anonymous_user = zo.AnonUser
        lm.init_app(zo.app)
        zo.app.login_manager = lm
        zo.login_manager = lm

        @lm.user_loader
        def load_user(uid):
            return zo.db.session.get(zo.User, int(uid))

    return zo


@pytest.fixture
def mock_requests(monkeypatch):
    """Patch `requests.post` / `requests.get` to return controllable responses.

    Tests register a list of (handler) callables; the next call pops the
    handler. If the list is empty, an AssertionError is raised.
    """
    import requests

    state = {'queue': []}

    def _make_response(handler):
        r = MagicMock()
        handler(r)
        return r

    def fake_post(url, *args, **kwargs):
        assert state['queue'], f"Unexpected requests.post to {url}"
        handler = state['queue'].pop(0)
        return _make_response(handler)

    def fake_get(url, *args, **kwargs):
        assert state['queue'], f"Unexpected requests.get to {url}"
        handler = state['queue'].pop(0)
        return _make_response(handler)

    monkeypatch.setattr(requests, 'post', fake_post)
    monkeypatch.setattr(requests, 'get', fake_get)
    return state
