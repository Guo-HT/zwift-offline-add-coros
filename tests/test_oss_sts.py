"""Unit tests for oss.ali_oss_client.fetch_sts and oss.aws_oss_client.fetch_sts.

All HTTP is mocked via the `mock_requests` fixture.
"""

import base64
import json
from unittest.mock import MagicMock

import pytest

from oss import ali_oss_client, aws_oss_client


def _sts_payload(creds: dict, code: int = 200) -> dict:
    body = json.dumps(creds).encode('utf-8')
    blob = ali_oss_client.SALT + base64.b64encode(body).decode('ascii')
    return {'code': code, 'data': {'credentials': blob}}


def _register(state, payload, code=200):
    def handler(r):
        r.json.return_value = payload if code == 200 else {**payload, 'code': code}
        r.raise_for_status.return_value = None
    state['queue'].append(handler)


# --- ali_oss_client.fetch_sts -------------------------------------------------

def test_ali_fetch_sts_success(mock_requests):
    creds = {'AccessKeyId': 'aki', 'AccessKeySecret': 'sec', 'SecurityToken': 'tok'}
    _register(mock_requests, _sts_payload(creds, code=200))
    out = ali_oss_client.fetch_sts()
    assert out == creds


def test_ali_fetch_sts_non_200_code_raises(mock_requests):
    def handler(r):
        r.json.return_value = {'code': 500, 'message': 'boom'}
        r.raise_for_status.return_value = None
    mock_requests['queue'].append(handler)
    with pytest.raises(RuntimeError, match="COROS STS request failed"):
        ali_oss_client.fetch_sts()


def test_ali_fetch_sts_http_error_propagates(mock_requests):
    def handler(r):
        r.raise_for_status.side_effect = RuntimeError("HTTP 500")
        r.json.return_value = {}
    mock_requests['queue'].append(handler)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        ali_oss_client.fetch_sts()


def test_ali_fetch_sts_url_contains_required_params(mock_requests, monkeypatch):
    """Verify the URL has the right hardcoded query params (catch sign drift)."""
    captured = {}
    creds = {'AccessKeyId': 'a', 'AccessKeySecret': 'b', 'SecurityToken': 'c'}

    def fake_get(url, *args, **kwargs):
        captured['url'] = url
        m = MagicMock()
        m.json.return_value = _sts_payload(creds)
        m.raise_for_status.return_value = None
        return m

    import requests
    monkeypatch.setattr(requests, 'get', fake_get)
    ali_oss_client.fetch_sts()
    u = captured['url']
    assert u.startswith(ali_oss_client.STS_BASE + '?')
    assert f"bucket={ali_oss_client.BUCKET}" in u
    assert f"service={ali_oss_client.SERVICE}" in u
    assert f"app_id={ali_oss_client.APP_ID}" in u
    assert f"sign={ali_oss_client.SIGN}" in u
    assert "v=2" in u


# --- aws_oss_client.fetch_sts -------------------------------------------------

def test_aws_fetch_sts_unknown_bucket_raises():
    with pytest.raises(ValueError, match="No COROS STS sign configured for bucket 'bogus'"):
        aws_oss_client.fetch_sts('bogus')


def test_aws_fetch_sts_eu_coros_success(mock_requests):
    creds = {'AccessKeyId': 'a', 'AccessKeySecret': 'b', 'SecurityToken': 'c'}
    _register(mock_requests, _sts_payload(creds))
    out = aws_oss_client.fetch_sts('eu-coros')
    assert out == creds


def test_aws_fetch_sts_non_200_code_raises(mock_requests):
    def handler(r):
        r.json.return_value = {'code': 403, 'message': 'forbidden'}
        r.raise_for_status.return_value = None
    mock_requests['queue'].append(handler)
    with pytest.raises(RuntimeError, match="COROS STS request failed"):
        aws_oss_client.fetch_sts('eu-coros')


def test_aws_fetch_sts_coros_s3_placeholder_rejected(mock_requests, monkeypatch):
    """The S3SIGN_COROS_S3 placeholder for regionId=1 (bucket 'coros-s3') must
    be rejected BEFORE any HTTP request is sent. Without this guard, the
    upload would proceed, COROS would 4xx the STS call, and the user would
    see only a vague logger.warning("COROS upload failed")."""
    import requests
    sent = {'count': 0}

    def fake_get(url, *args, **kwargs):
        sent['count'] += 1
        m = MagicMock()
        m.json.return_value = _sts_payload({'AccessKeyId': 'a'})
        m.raise_for_status.return_value = None
        return m
    monkeypatch.setattr(requests, 'get', fake_get)
    with pytest.raises(NotImplementedError, match="regionId=1"):
        aws_oss_client.fetch_sts('coros-s3')
    assert sent['count'] == 0, "placeholder bucket must not hit the network"
