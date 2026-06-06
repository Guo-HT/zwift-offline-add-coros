"""Flask route tests for /coros/<u>/ and /delete/coros_credentials.bin.

Uses the `fresh_zwift_offline` fixture for proper DB isolation.
"""

import os
import pytest


@pytest.fixture
def logged_in_client(fresh_zwift_offline):
    """Yield (client, zo, tmp_path) with a logged-in test user."""
    zo = fresh_zwift_offline
    import tempfile
    tmp_path = tempfile.mkdtemp(prefix='zo_route_')
    zo.STORAGE_DIR = tmp_path  # already bound by fixture, but be explicit

    with zo.app.app_context():
        from werkzeug.security import generate_password_hash
        from zwift_offline import db, User
        u = User(
            username='testuser',
            first_name='Test',
            last_name='User',
            pass_hash=generate_password_hash('testpass'),
            player_id=99999,
            is_admin=0,
            remember=0,
        )
        db.session.add(u)
        db.session.commit()

    client = zo.app.test_client()
    resp = client.post('/login/', data={'username': 'testuser', 'password': 'testpass'},
                       follow_redirects=False)
    assert resp.status_code in (302, 303), f"login failed: {resp.status_code}"
    return client, zo, tmp_path


def _seed_creds(zo, tmp_path, email='show@coros.com', password='shown'):
    profile_dir = os.path.join(tmp_path, '99999')
    os.makedirs(profile_dir, exist_ok=True)
    with zo.app.test_request_context():
        zo.encrypt_credentials(
            os.path.join(profile_dir, 'coros_credentials.bin'),
            (email, password),
        )


# --- /coros/<u>/ route -------------------------------------------------------

def test_get_coros_renders_template_with_existing_creds(logged_in_client):
    client, zo, tmp_path = logged_in_client
    _seed_creds(zo, tmp_path, 'show@coros.com', 'shown')
    resp = client.get('/coros/testuser/')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'COROS credentials' in body
    assert 'show@coros.com' in body
    assert 'Remove credentials' in body


def test_get_coros_renders_empty_when_no_creds(logged_in_client):
    client, _, _ = logged_in_client
    resp = client.get('/coros/testuser/')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'COROS credentials' in body
    assert 'Remove credentials' not in body


def test_post_coros_with_empty_fields_flashes_error(logged_in_client):
    client, _, _ = logged_in_client
    resp = client.post('/coros/testuser/', data={'username': '', 'password': ''},
                       follow_redirects=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "COROS credentials can" in body


def test_post_coros_saves_credentials(logged_in_client):
    client, zo, tmp_path = logged_in_client
    resp = client.post('/coros/testuser/',
                       data={'username': 'new@coros.com', 'password': 'newpass'},
                       follow_redirects=False)
    assert resp.status_code == 302
    creds_file = os.path.join(tmp_path, '99999', 'coros_credentials.bin')
    assert os.path.isfile(creds_file)
    with zo.app.test_request_context():
        email, password = zo.decrypt_credentials(creds_file)
    assert email == 'new@coros.com'
    assert password == 'newpass'


def test_post_coros_redirects_to_settings(logged_in_client):
    client, _, _ = logged_in_client
    resp = client.post('/coros/testuser/',
                       data={'username': 'a@b.com', 'password': 'pwd'})
    assert resp.status_code == 302
    assert '/settings/testuser' in resp.headers['Location']


# --- /delete/coros_credentials.bin route -------------------------------------

def test_delete_coros_credentials_removes_file(logged_in_client):
    client, _, tmp_path = logged_in_client
    creds_file = os.path.join(tmp_path, '99999', 'coros_credentials.bin')
    os.makedirs(os.path.dirname(creds_file), exist_ok=True)
    with open(creds_file, 'wb') as f:
        f.write(b'fake-encrypted-bytes')
    assert os.path.isfile(creds_file)
    resp = client.get('/delete/coros_credentials.bin', follow_redirects=True)
    assert resp.status_code == 200
    assert not os.path.isfile(creds_file)


def test_delete_coros_credentials_redirects_to_settings(logged_in_client):
    client, _, tmp_path = logged_in_client
    creds_file = os.path.join(tmp_path, '99999', 'coros_credentials.bin')
    os.makedirs(os.path.dirname(creds_file), exist_ok=True)
    open(creds_file, 'wb').close()
    resp = client.get('/delete/coros_credentials.bin', follow_redirects=False)
    assert resp.status_code == 302
    assert '/settings/testuser' in resp.headers['Location']


def test_delete_coros_credentials_missing_file_is_ok(logged_in_client):
    client, _, _ = logged_in_client
    resp = client.get('/delete/coros_credentials.bin', follow_redirects=True)
    assert resp.status_code == 200


# --- delete whitelist guard --------------------------------------------------

def test_delete_unknown_file_returns_403(logged_in_client):
    client, _, _ = logged_in_client
    resp = client.get('/delete/not_in_whitelist.bin')
    assert resp.status_code == 403


def test_coros_credentials_in_delete_whitelist(fresh_zwift_offline):
    """Static check: the delete whitelist must include coros_credentials.bin,
    otherwise the Remove button would 403. The whitelist now lives as a
    module-level constant (DELETABLE_FILENAMES) rather than inside the
    delete() function, so we check the module attribute instead of the
    function source."""
    import zwift_offline as zo
    assert hasattr(zo, 'DELETABLE_FILENAMES'), "module must expose DELETABLE_FILENAMES"
    assert 'coros_credentials.bin' in zo.DELETABLE_FILENAMES
