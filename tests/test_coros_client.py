"""Unit tests for coros_client.CorosClient.

HTTP is mocked. We verify:
  - login() populates state correctly for each region
  - login() gracefully handles failure modes
  - upload_activity() zips the FIT, uploads the ZIP, and builds the
    correct import-API body (md5/size/oriFileName are the ZIP's, not the
    FIT's)
  - upload_activity() dispatches to the right OSS module per region
  - upload_activity() requires login first
"""

import hashlib
import io
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from coros_client import CorosClient, REGION_CONFIG
from oss import ali_oss_client, aws_oss_client


# --- REGION_CONFIG integrity --------------------------------------------------

def test_region_config_has_all_three_regions():
    assert set(REGION_CONFIG.keys()) == {1, 2, 3}
    for rid, cfg in REGION_CONFIG.items():
        assert 'teamapi' in cfg and cfg['teamapi'].startswith('https://')
        assert 'bucket' in cfg
        assert 'service' in cfg
        assert 'oss' in cfg


def test_region_endpoints_correct():
    assert REGION_CONFIG[1]['teamapi'] == 'https://teamapi.coros.com'
    assert REGION_CONFIG[2]['teamapi'] == 'https://teamcnapi.coros.com'
    assert REGION_CONFIG[3]['teamapi'] == 'https://teameuapi.coros.com'  # not teamapi


def test_region_oss_modules_correct():
    assert REGION_CONFIG[1]['oss'] is aws_oss_client
    assert REGION_CONFIG[2]['oss'] is ali_oss_client
    assert REGION_CONFIG[3]['oss'] is aws_oss_client


def test_region_buckets_correct():
    assert REGION_CONFIG[1]['bucket'] == 'coros-s3'
    assert REGION_CONFIG[2]['bucket'] == 'coros-oss'
    assert REGION_CONFIG[3]['bucket'] == 'eu-coros'


# --- login() ------------------------------------------------------------------

def _register_login(state, result='0000', region_id=1, access_token='tok',
                    user_id='u1', message='OK', http_error=False):
    def handler(r):
        if http_error:
            r.raise_for_status.side_effect = RuntimeError("HTTP 500")
            r.json.side_effect = RuntimeError("no json")
        else:
            r.raise_for_status.return_value = None
            r.json.return_value = {
                'result': result,
                'message': message,
                'data': {
                    'accessToken': access_token,
                    'userId': user_id,
                    'regionId': region_id,
                } if result == '0000' else {},
            }
    state['queue'].append(handler)


def test_login_success_region_1(mock_requests):
    _register_login(mock_requests, result='0000', region_id=1, access_token='t1', user_id='u1')
    c = CorosClient()
    c.login('a@b.com', 'md5-hash')
    assert c.access_token == 't1'
    assert c.user_id == 'u1'
    assert c.region_id == 1
    assert c.teamapi == 'https://teamapi.coros.com'
    assert c._cfg is REGION_CONFIG[1]


def test_login_success_region_2(mock_requests):
    _register_login(mock_requests, result='0000', region_id=2, user_id='u2')
    c = CorosClient()
    c.login('a@b.com', 'h')
    assert c.teamapi == 'https://teamcnapi.coros.com'
    assert c._cfg is REGION_CONFIG[2]


def test_login_success_region_3(mock_requests):
    _register_login(mock_requests, result='0000', region_id=3, user_id='u3')
    c = CorosClient()
    c.login('a@b.com', 'h')
    # Regression: regionId=3 must use teameuapi, not teamapi
    assert c.teamapi == 'https://teameuapi.coros.com'
    assert c._cfg is REGION_CONFIG[3]


def test_login_non_0000_result(mock_requests):
    _register_login(mock_requests, result='9999', message='bad password')
    c = CorosClient()
    c.login('a@b.com', 'h')
    assert c.access_token is None
    assert c._cfg is None


def test_login_http_error(mock_requests):
    _register_login(mock_requests, http_error=True)
    c = CorosClient()
    c.login('a@b.com', 'h')
    assert c.access_token is None


def test_login_unknown_region_id(mock_requests):
    _register_login(mock_requests, region_id=99)
    c = CorosClient()
    c.login('a@b.com', 'h')
    # access_token IS set (line 74 happens before the regionId check), but
    # _cfg is None because REGION_CONFIG.get(99) returns None. The contract
    # is: downstream code should check `client._cfg` before uploading.
    assert c.access_token == 'tok'  # was set unconditionally
    assert c._cfg is None  # this is what gates upload_activity()


def test_login_defaults_to_region_1_if_missing(mock_requests):
    """If the server omits regionId, we should not crash — but we should
    also not silently pick a region. The current implementation defaults
    to 1 via dict.get(..., 1) inside REGION_CONFIG.get. Verify behavior."""
    def handler(r):
        r.raise_for_status.return_value = None
        r.json.return_value = {
            'result': '0000',
            'data': {'accessToken': 't', 'userId': 'u'},  # no regionId
        }
    mock_requests['queue'].append(handler)
    c = CorosClient()
    c.login('a@b.com', 'h')
    # The login() uses data.get('regionId', 1)
    assert c.region_id == 1
    assert c._cfg is REGION_CONFIG[1]


# --- upload_activity() --------------------------------------------------------

def test_upload_activity_requires_login():
    c = CorosClient()
    c.upload_activity(b'data', 'a.fit', 'md5sum')  # no login
    # No HTTP / OSS calls should happen. Just check that the early-return
    # path didn't crash and the client's state is still clean.
    assert c.access_token is None


@pytest.mark.parametrize('region_id,expected_bucket,expected_module', [
    (1, 'coros-s3', aws_oss_client),
    (2, 'coros-oss', ali_oss_client),
    (3, 'eu-coros', aws_oss_client),
])
def test_upload_activity_dispatches_to_correct_oss(
    region_id, expected_bucket, expected_module, mock_requests,
):
    # 1. login
    _register_login(mock_requests, region_id=region_id, access_token='tok', user_id='u')
    # 2. import API success
    def import_handler(r):
        r.raise_for_status.return_value = None
        r.json.return_value = {'result': '0000', 'data': {'status': 2}}
    mock_requests['queue'].append(import_handler)

    captured = {}
    def fake_upload(first_arg, *args, **kwargs):
        # first_arg = zip_bytes (top-level function call, no implicit self)
        captured['zip_bytes'] = first_arg
        captured['oss_key'] = args[0] if args else None
        if 'bucket' in kwargs:
            captured['aws_bucket'] = kwargs['bucket']
        return 'oss-key'
    with patch.object(aws_oss_client, 'upload', side_effect=fake_upload), \
         patch.object(ali_oss_client, 'upload', side_effect=fake_upload):
        c = CorosClient()
        c.login('a@b.com', 'h')
        assert c._cfg is not None, f"login failed for region {region_id}"
        c.upload_activity(b'fit-bytes', 'ride.fit', 'md5hex')

    if expected_module is aws_oss_client:
        assert captured.get('aws_bucket') == expected_bucket
    # The bytes pushed to OSS must be a ZIP (not the raw FIT) — the old
    # code pushed b'fit-bytes' directly and COROS returned status=-1.
    assert captured.get('zip_bytes') != b'fit-bytes'
    zf = zipfile.ZipFile(io.BytesIO(captured['zip_bytes']))
    assert zf.read('ride.fit') == b'fit-bytes'
    # OSS key: fit_zip/<userId>/<zip_md5>.zip
    assert captured.get('oss_key') is not None
    assert captured['oss_key'].startswith('fit_zip/u/')
    assert captured['oss_key'].endswith('.zip')
    # The middle segment must be the zip md5 (32 hex chars).
    middle = captured['oss_key'][len('fit_zip/u/'):-len('.zip')]
    assert len(middle) == 32 and all(ch in '0123456789abcdef' for ch in middle)
    assert middle == hashlib.md5(captured['zip_bytes'], usedforsecurity=False).hexdigest()


def test_upload_activity_import_api_success(mock_requests):
    _register_login(mock_requests, region_id=2, access_token='tok', user_id='u')

    captured = {}
    def import_handler(r):
        # Capture request body and headers
        captured['url'] = r._extract_url() if hasattr(r, '_extract_url') else None
        r.raise_for_status.return_value = None
        r.json.return_value = {'result': '0000', 'data': {'status': 2}}
    mock_requests['queue'].append(import_handler)

    with patch.object(ali_oss_client, 'upload', return_value='k'):
        c = CorosClient()
        c.login('a@b.com', 'h')
        c.upload_activity(b'fit', 'r.fit', 'md5sum')
    # The import API was called exactly once
    assert len(mock_requests['queue']) == 0  # both used


def test_upload_activity_import_api_result_not_0000(mock_requests):
    _register_login(mock_requests, region_id=2)
    def handler(r):
        r.raise_for_status.return_value = None
        r.json.return_value = {'result': '9999', 'message': 'rate limited'}
    mock_requests['queue'].append(handler)
    with patch.object(ali_oss_client, 'upload', return_value='k'):
        c = CorosClient()
        c.login('a@b.com', 'h')
        # Should NOT raise — the inner RuntimeError is caught by the
        # upload_activity try/except.
        c.upload_activity(b'fit', 'r.fit', 'md5sum')


def test_upload_activity_import_api_status_not_2(mock_requests):
    _register_login(mock_requests, region_id=2)
    def handler(r):
        r.raise_for_status.return_value = None
        r.json.return_value = {'result': '0000', 'data': {'status': 1, 'message': 'pending'}}
    mock_requests['queue'].append(handler)
    with patch.object(ali_oss_client, 'upload', return_value='k'):
        c = CorosClient()
        c.login('a@b.com', 'h')
        c.upload_activity(b'fit', 'r.fit', 'md5sum')


def test_upload_activity_oss_failure_caught(mock_requests):
    _register_login(mock_requests, region_id=2)
    # No more mock_responses queued; the import API call won't happen
    with patch.object(ali_oss_client, 'upload', side_effect=RuntimeError("oss down")):
        c = CorosClient()
        c.login('a@b.com', 'h')
        # Should NOT raise
        c.upload_activity(b'fit', 'r.fit', 'md5sum')


def test_call_import_api_header_lowercase():
    """Direct test of _call_import_api's header name."""
    import requests
    captured = {}
    def fake_post(url, *args, **kwargs):
        captured['url'] = url
        captured['headers'] = kwargs.get('headers', {})
        captured['files'] = kwargs.get('files', {})
        r = MagicMock()
        r.raise_for_status.return_value = None
        r.json.return_value = {'result': '0000', 'data': {'status': 2}}
        return r
    from unittest.mock import patch
    with patch.object(requests, 'post', side_effect=fake_post):
        c = CorosClient()
        c.access_token = 't1'
        c._cfg = REGION_CONFIG[2]
        c.teamapi = REGION_CONFIG[2]['teamapi']
        c._call_import_api('fit_zip/abc_r.fit', 'md5hex', 'r.fit', 1234)
    assert captured['headers'] == {'accesstoken': 't1'}
    # The body is a multipart-form 'jsonParameter' field
    assert 'jsonParameter' in captured['files']
    import json as _json
    # files[jsonParameter] = (None, json_str)  →  [1] is the JSON string
    body = _json.loads(captured['files']['jsonParameter'][1])
    assert captured['url'] == 'https://teamcnapi.coros.com/activity/fit/import'
    assert body['bucket'] == 'coros-oss'
    assert body['serviceName'] == 'aliyun'
    assert body['md5'] == 'md5hex'
    assert body['size'] == 1234
    assert body['oriFileName'] == 'r.fit'
    assert body['object'] == 'fit_zip/abc_r.fit'
    assert body['source'] == 1
    assert body['timezone'] == 32


# --- zip behavior -------------------------------------------------------------
#
# Regression tests for the "status=-1 message=OK" bug: COROS rejects a raw
# FIT and only accepts a single-entry ZIP wrapped via zipfile.ZIP_DEFLATED.

def test_upload_activity_zips_fit_before_upload(mock_requests):
    """The bytes pushed to OSS must be a ZIP whose single entry contains
    the FIT, not the raw FIT. Otherwise COROS returns status=-1."""
    _register_login(mock_requests, region_id=2, user_id='u42')
    def import_handler(r):
        r.raise_for_status.return_value = None
        r.json.return_value = {'result': '0000', 'data': {'status': 2}}
    mock_requests['queue'].append(import_handler)

    captured = {}
    def fake_upload(zip_bytes, oss_key):
        captured['zip_bytes'] = zip_bytes
        captured['oss_key'] = oss_key
        return oss_key
    with patch.object(ali_oss_client, 'upload', side_effect=fake_upload):
        c = CorosClient()
        c.login('a@b.com', 'h')
        c.upload_activity(b'raw-fit-content', 'activity.fit', 'fit-md5-irrelevant')

    # 1. The OSS key must be fit_zip/<userId>/<zip_md5>.zip — the
    #    reference client's shape, content-addressed.
    expected_zip_md5 = hashlib.md5(captured['zip_bytes'], usedforsecurity=False).hexdigest()
    assert captured['oss_key'] == 'fit_zip/u42/%s.zip' % expected_zip_md5

    # 2. The bytes pushed to OSS must be a valid ZIP (PK\\x03\\x04 magic).
    assert captured['zip_bytes'][:4] == b'PK\x03\x04', "not a ZIP"

    # 3. The ZIP must contain exactly one entry named 'activity.fit' with
    #    the original FIT bytes.
    zf = zipfile.ZipFile(io.BytesIO(captured['zip_bytes']))
    assert zf.namelist() == ['activity.fit']
    assert zf.read('activity.fit') == b'raw-fit-content'

    # 4. The caller's fit_md5 must NOT appear anywhere — the import API
    #    uses the ZIP's md5, so the caller-supplied value is discarded.
    assert b'fit-md5-irrelevant' not in captured['zip_bytes']


def test_upload_activity_import_api_body_fields_via_post_capture(mock_requests, monkeypatch):
    """End-to-end: the bytes sent to /activity/fit/import must contain
    the ZIP md5 (not the FIT md5), the ZIP size (not the FIT size), and
    a .zip oriFileName (not .fit)."""
    import json as _json
    import requests
    _register_login(mock_requests, region_id=2, user_id='u42')
    # Queue the import-API response (status=2 so upload returns cleanly).
    def import_handler(r):
        r.raise_for_status.return_value = None
        r.json.return_value = {'result': '0000', 'data': {'status': 2}}
    mock_requests['queue'].append(import_handler)

    captured_post = {}
    real_post = requests.post  # the patched fake_post from the fixture
    def spy_post(url, *args, **kwargs):
        r = real_post(url, *args, **kwargs)
        if '/activity/fit/import' in str(url):
            captured_post['url'] = url
            captured_post['files'] = kwargs.get('files', {})
        return r
    monkeypatch.setattr(requests, 'post', spy_post)

    captured_oss = {}
    def fake_upload(zip_bytes, oss_key):
        captured_oss['zip_bytes'] = zip_bytes
        captured_oss['oss_key'] = oss_key
        return oss_key
    with patch.object(ali_oss_client, 'upload', side_effect=fake_upload):
        c = CorosClient()
        c.login('a@b.com', 'h')
        c.upload_activity(b'fit-bytes', 'ride.fit', 'fit-md5-callers-value')

    zip_bytes = captured_oss['zip_bytes']
    zip_md5 = hashlib.md5(zip_bytes, usedforsecurity=False).hexdigest()
    body = _json.loads(captured_post['files']['jsonParameter'][1])
    assert body['object'] == 'fit_zip/u42/%s.zip' % zip_md5
    assert body['md5'] == zip_md5
    assert body['md5'] != 'fit-md5-callers-value'  # NOT the FIT md5
    assert body['size'] == len(zip_bytes)
    assert body['size'] != len(b'fit-bytes')       # NOT the FIT size
    assert body['oriFileName'] == '%s.zip' % zip_md5
    assert body['oriFileName'] != 'ride.fit'
