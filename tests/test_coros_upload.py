"""Tests for the coros_upload() function inside zwift_offline.

We mock the CorosClient so we don't touch HTTP. The Activity object is
a SimpleNamespace with `fit` (bytes) and `fit_filename` (str).
"""

import hashlib
import os
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_activity(fit_content=b'fake-fit-bytes'):
    return SimpleNamespace(
        fit=fit_content,
        fit_filename='ride_2026-06-06_120000.fit',
    )


def _write_bin_creds(zo, profile_dir, email='user@coros.com', password='hunter2'):
    """Write encrypted credentials using the project's own encrypt_credentials."""
    f = os.path.join(profile_dir, 'coros_credentials.bin')
    with zo.app.test_request_context():
        zo.encrypt_credentials(f, (email, password))
    return f


def _write_txt_creds(profile_dir, email='user@coros.com', password='hunter2'):
    f = os.path.join(profile_dir, 'coros_credentials.txt')
    with open(f, 'w') as fh:
        fh.write(email + '\n' + password + '\n')
    return f


# --- Missing credentials -----------------------------------------------------

def test_missing_creds_returns_silently(fresh_zwift_offline, test_storage_dir):
    zo = fresh_zwift_offline
    player_id = 12345
    profile_dir = os.path.join(test_storage_dir, str(player_id))
    os.makedirs(profile_dir, exist_ok=True)
    activity = _make_activity()
    # No .bin and no .txt
    assert not os.path.exists(os.path.join(profile_dir, 'coros_credentials.bin'))
    assert not os.path.exists(os.path.join(profile_dir, 'coros_credentials.txt'))
    zo.coros_upload(player_id, activity)  # should not raise


# --- .bin credentials --------------------------------------------------------

def test_bin_creds_invokes_login_and_upload(fresh_zwift_offline, test_storage_dir):
    zo = fresh_zwift_offline
    player_id = 12345
    profile_dir = os.path.join(test_storage_dir, str(player_id))
    os.makedirs(profile_dir, exist_ok=True)
    _write_bin_creds(zo, profile_dir, 'a@b.com', 'pwd')
    activity = _make_activity(b'fit-content-X')

    fake_client = MagicMock()
    with patch('coros_client.CorosClient', return_value=fake_client) as cls:
        zo.coros_upload(player_id, activity)

    cls.assert_called_once()
    fake_client.login.assert_called_once()
    login_args = fake_client.login.call_args
    assert login_args.args[0] == 'a@b.com'
    expected_md5 = hashlib.md5(b'pwd').hexdigest()
    assert login_args.args[1] == expected_md5
    upload_args = fake_client.upload_activity.call_args
    assert upload_args.args[0] == b'fit-content-X'
    assert upload_args.args[1] == 'ride_2026-06-06_120000.fit'
    expected_fit_md5 = hashlib.md5(b'fit-content-X').hexdigest()
    assert upload_args.args[2] == expected_fit_md5


# --- .txt credentials --------------------------------------------------------

def test_txt_creds_invokes_login_and_upload(fresh_zwift_offline, test_storage_dir):
    zo = fresh_zwift_offline
    player_id = 12345
    profile_dir = os.path.join(test_storage_dir, str(player_id))
    os.makedirs(profile_dir, exist_ok=True)
    _write_txt_creds(profile_dir, 'txt@coros.com', 'txtpwd')
    activity = _make_activity(b'f')

    fake_client = MagicMock()
    with patch('coros_client.CorosClient', return_value=fake_client):
        zo.coros_upload(player_id, activity)

    fake_client.login.assert_called_once()
    expected_md5 = hashlib.md5(b'txtpwd').hexdigest()
    assert fake_client.login.call_args.args[1] == expected_md5
    assert fake_client.login.call_args.args[0] == 'txt@coros.com'
    fake_client.upload_activity.assert_called_once()


def test_txt_creds_strips_trailing_newlines(fresh_zwift_offline, test_storage_dir):
    """A file with no trailing newline on the last line should still parse."""
    zo = fresh_zwift_offline
    player_id = 12345
    profile_dir = os.path.join(test_storage_dir, str(player_id))
    os.makedirs(profile_dir, exist_ok=True)
    f = os.path.join(profile_dir, 'coros_credentials.txt')
    with open(f, 'w') as fh:
        fh.write('a@b.com\npwd')  # NO trailing newline
    activity = _make_activity(b'f')

    fake_client = MagicMock()
    with patch('coros_client.CorosClient', return_value=fake_client):
        zo.coros_upload(player_id, activity)
    assert fake_client.login.call_args.args[1] == hashlib.md5(b'pwd').hexdigest()


# --- Error swallowing --------------------------------------------------------

def test_coros_client_login_raises_swallowed(fresh_zwift_offline, test_storage_dir):
    zo = fresh_zwift_offline
    player_id = 12345
    profile_dir = os.path.join(test_storage_dir, str(player_id))
    os.makedirs(profile_dir, exist_ok=True)
    _write_bin_creds(zo, profile_dir)
    activity = _make_activity()

    fake_client = MagicMock()
    fake_client.login.side_effect = RuntimeError("network down")
    with patch('coros_client.CorosClient', return_value=fake_client):
        zo.coros_upload(player_id, activity)  # MUST NOT raise


def test_coros_client_module_import_error_swallowed(fresh_zwift_offline, test_storage_dir, monkeypatch):
    """If the coros_client module itself can't be imported, we log + return."""
    zo = fresh_zwift_offline
    player_id = 12345
    profile_dir = os.path.join(test_storage_dir, str(player_id))
    os.makedirs(profile_dir, exist_ok=True)
    _write_bin_creds(zo, profile_dir)
    activity = _make_activity()

    # Save and remove coros_client so `import coros_client` raises ImportError
    import sys
    if 'coros_client' in sys.modules:
        saved = sys.modules['coros_client']
        del sys.modules['coros_client']
    else:
        saved = None
    sys.modules['coros_client'] = None  # causes ImportError on `import coros_client`
    try:
        zo.coros_upload(player_id, activity)  # MUST NOT raise
    finally:
        del sys.modules['coros_client']
        if saved is not None:
            sys.modules['coros_client'] = saved


def test_coros_client_module_import_error_warning_includes_expected_path(
    fresh_zwift_offline, test_storage_dir, monkeypatch, caplog
):
    """When `import coros_client` fails, the WARNING must include the
    expected on-disk path of coros_client.py so the user can see
    "the file is missing" instead of a vague "ModuleNotFoundError".

    This guards against a regression to the old vague message that
    pointed users at "pip install" when the real fix was "the file
    isn't shipped" (cf. commit 7c8640b adding coros_client.py)."""
    zo = fresh_zwift_offline
    player_id = 12345
    profile_dir = os.path.join(test_storage_dir, str(player_id))
    os.makedirs(profile_dir, exist_ok=True)
    _write_bin_creds(zo, profile_dir)
    activity = _make_activity()

    import sys
    saved = sys.modules.get('coros_client')
    sys.modules['coros_client'] = None  # force ImportError
    try:
        import logging
        with caplog.at_level(logging.WARNING, logger='zoffline'):
            zo.coros_upload(player_id, activity)
    finally:
        del sys.modules['coros_client']
        if saved is not None:
            sys.modules['coros_client'] = saved

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected at least one WARNING"
    msg = warnings[0].getMessage()
    assert 'coros_client' in msg
    # The expected on-disk path must be mentioned so the user knows where to look.
    assert 'coros_client.py' in msg
    # And it must reference the actual project location, not a generic pip hint.
    assert '/zwift-offline' in msg or os.sep + 'coros_client.py' in msg


# --- .bin preferred over .txt (defensive) -------------------------------------

def test_bin_takes_precedence_over_txt(fresh_zwift_offline, test_storage_dir):
    """If both files exist (shouldn't, but...), .bin wins."""
    zo = fresh_zwift_offline
    player_id = 12345
    profile_dir = os.path.join(test_storage_dir, str(player_id))
    os.makedirs(profile_dir, exist_ok=True)
    _write_bin_creds(zo, profile_dir, 'bin@x', 'binpwd')
    _write_txt_creds(profile_dir, 'txt@x', 'txtpwd')
    activity = _make_activity(b'f')

    fake_client = MagicMock()
    with patch('coros_client.CorosClient', return_value=fake_client):
        zo.coros_upload(player_id, activity)
    assert fake_client.login.call_args.args[0] == 'bin@x'
