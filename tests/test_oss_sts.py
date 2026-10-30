"""Unit tests for the COROS STS credential channels.

Since 2026-10-03 COROS closed the anonymous STS channel
(`faq.coros.com/openapi/oss/sts` now answers nginx 404). The credentials
now come from the training-centre BFF proxy, authenticated with the
`accessToken` returned by `/account/login`, with the v2 open API and the
(old) anonymous channel kept as fallbacks.

All HTTP is mocked — no test in this file touches the real COROS servers.
"""

import base64
import json
from unittest.mock import MagicMock

import pytest
import requests

from oss import ali_oss_client, aws_oss_client, sts


# --- helpers ------------------------------------------------------------------

def _sts_payload(creds: dict, code: int = 200) -> dict:
    body = json.dumps(creds).encode('utf-8')
    blob = sts.SALT + base64.b64encode(body).decode('ascii')
    return {'code': code, 'data': {'credentials': blob}}


class _FakeGet:
    """Stand-in for `requests.get`.

    `responses` is a list of (url_substring, handler) pairs; the first
    match wins. Every call is recorded in `self.calls`.
    """

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, *args, **kwargs):
        self.calls.append({
            'url': url,
            'headers': kwargs.get('headers') or {},
            'timeout': kwargs.get('timeout'),
        })
        for needle, handler in self.responses:
            if needle in url:
                r = MagicMock()
                r.raise_for_status.return_value = None
                handler(r)
                return r
        # Unmatched URL: behave like a 404.
        r = MagicMock()
        r.raise_for_status.side_effect = RuntimeError("HTTP 404")
        r.json.return_value = {}
        return r


def _json_handler(payload):
    def handler(r):
        r.json.return_value = payload
    return handler


def _status_handler(status, payload):
    def handler(r):
        r.json.return_value = payload
        if status >= 400:
            r.raise_for_status.side_effect = RuntimeError("HTTP %d" % status)
    return handler


# --- the web-proxy channel (the current one) ---------------------------------

def test_ali_fetch_sts_uses_training_proxy_with_token(monkeypatch):
    creds = {'AccessKeyId': 'aki', 'AccessKeySecret': 'sec', 'SecurityToken': 'tok'}
    fake = _FakeGet([('api/proxy/oss/sts', _json_handler(_sts_payload(creds)))])
    monkeypatch.setattr(requests, 'get', fake)

    out = ali_oss_client.fetch_sts('tok-123')
    assert out == creds
    # The proxy answers immediately — no fallback channel is contacted.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call['url'].startswith('https://trainingcn.coros.com/api/proxy/oss/sts?')
    assert 'bucket=coros-oss' in call['url']
    assert 'service=aliyun' in call['url']
    assert 'v=2' in call['url']
    # The login token authenticates via the CPL-coros-token cookie.
    assert call['headers']['Cookie'] == 'CPL-coros-token=tok-123'
    assert call['timeout'] == sts.HTTP_TIMEOUT


def test_aws_eu_fetch_sts_uses_region_training_proxy(monkeypatch):
    creds = {'AccessKeyId': 'aki', 'AccessKeySecret': 'sec', 'SecurityToken': 'tok'}
    fake = _FakeGet([('api/proxy/oss/sts', _json_handler(_sts_payload(creds)))])
    monkeypatch.setattr(requests, 'get', fake)

    out = aws_oss_client.fetch_sts('eu-coros', access_token='tok-eu')
    assert out == creds
    assert len(fake.calls) == 1
    call = fake.calls[0]
    # EU region host first, global training.coros.com as fallback.
    assert call['url'].startswith('https://trainingeu.coros.com/api/proxy/oss/sts?')
    assert 'bucket=eu-coros' in call['url']
    assert 'service=aws' in call['url']
    assert call['headers']['Cookie'] == 'CPL-coros-token=tok-eu'


def test_aws_region1_uses_proxy_even_though_sign_is_a_placeholder(monkeypatch):
    """regionId=1 (bucket 'coros-s3') has no usable `sign` — the sign is only
    needed by the anonymous channel, which COROS closed. With a login token
    the proxy channel works, so the upload must not be blocked."""
    creds = {'AccessKeyId': 'aki', 'AccessKeySecret': 'sec', 'SecurityToken': 'tok'}
    fake = _FakeGet([('api/proxy/oss/sts', _json_handler(_sts_payload(creds)))])
    monkeypatch.setattr(requests, 'get', fake)

    out = aws_oss_client.fetch_sts('coros-s3', access_token='tok-us')
    assert out == creds
    assert len(fake.calls) == 1
    assert fake.calls[0]['url'].startswith('https://training.coros.com/api/proxy/oss/sts?')
    assert 'bucket=coros-s3' in fake.calls[0]['url']


# --- fallback order -----------------------------------------------------------

def test_sts_falls_back_from_proxy_to_v2_to_legacy(monkeypatch):
    creds = {'AccessKeyId': 'aki', 'AccessKeySecret': 'sec', 'SecurityToken': 'tok'}
    fake = _FakeGet([
        ('trainingcn.coros.com', _status_handler(401, {'code': 401, 'msg': 'unauthorized'})),
        ('/openapi/v2/oss/sts', _status_handler(401, {'code': 401, 'msg': 'unauthorized'})),
        ('faq.coros.com', _json_handler(_sts_payload(creds))),
    ])
    monkeypatch.setattr(requests, 'get', fake)

    out = ali_oss_client.fetch_sts('tok-123')
    assert out == creds
    urls = [c['url'] for c in fake.calls]
    assert urls[0].startswith('https://trainingcn.coros.com/api/proxy/oss/sts?')
    assert urls[1].startswith('https://faq.coros.com/openapi/v2/oss/sts?')
    # The v2 channel authenticates with a header, not a cookie.
    assert fake.calls[1]['headers']['accesstoken'] == 'tok-123'
    # ... and the old anonymous channel is the last resort.
    assert urls[-1].startswith('https://faq.coros.com/openapi/oss/sts?')
    assert 'app_id=%s' % ali_oss_client.APP_ID in urls[-1]
    assert 'sign=%s' % ali_oss_client.SIGN in urls[-1]


def test_ali_fetch_sts_without_token_skips_authenticated_channels(monkeypatch):
    """Without a login token only the anonymous legacy channel is usable."""
    creds = {'AccessKeyId': 'aki', 'AccessKeySecret': 'sec', 'SecurityToken': 'tok'}
    fake = _FakeGet([('faq.coros.com', _json_handler(_sts_payload(creds)))])
    monkeypatch.setattr(requests, 'get', fake)

    out = ali_oss_client.fetch_sts()
    assert out == creds
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call['url'].startswith(ali_oss_client.STS_BASE + '?')
    assert 'bucket=%s' % ali_oss_client.BUCKET in call['url']
    assert 'service=%s' % ali_oss_client.SERVICE in call['url']
    assert 'app_id=%s' % ali_oss_client.APP_ID in call['url']
    assert 'sign=%s' % ali_oss_client.SIGN in call['url']
    assert 'v=2' in call['url']


def test_aws_fetch_sts_eu_coros_success_without_token(monkeypatch):
    """eu-coros still has a real sign, so the anonymous channel can be used
    when no token is available."""
    creds = {'AccessKeyId': 'aki', 'AccessKeySecret': 'sec', 'SecurityToken': 'tok'}
    fake = _FakeGet([('faq.coros.com', _json_handler(_sts_payload(creds)))])
    monkeypatch.setattr(requests, 'get', fake)

    out = aws_oss_client.fetch_sts('eu-coros')
    assert out == creds
    assert fake.calls[0]['url'].startswith(aws_oss_client.STS_BASE + '?')


def test_all_channels_failing_raises_runtime_error(monkeypatch):
    fake = _FakeGet([
        ('trainingcn.coros.com', _status_handler(401, {'code': 401, 'msg': 'unauthorized'})),
        ('faq.coros.com', _status_handler(500, {'code': 500, 'msg': 'boom'})),
    ])
    monkeypatch.setattr(requests, 'get', fake)

    with pytest.raises(RuntimeError, match="COROS STS request failed"):
        ali_oss_client.fetch_sts('tok-123')


def test_non_json_response_is_treated_as_a_failed_channel(monkeypatch):
    """The dead legacy endpoint answers with an nginx HTML page, which is
    not JSON — that must not crash the whole upload."""
    def handler(r):
        r.json.side_effect = ValueError("Expecting value: line 1 column 1")
    fake = _FakeGet([
        ('trainingcn.coros.com', handler),
        ('/openapi/v2/oss/sts', handler),
        ('faq.coros.com', handler),
    ])
    monkeypatch.setattr(requests, 'get', fake)

    with pytest.raises(RuntimeError, match="COROS STS request failed"):
        ali_oss_client.fetch_sts('tok-123')


# --- credential payload shapes ------------------------------------------------

def test_aws_style_credential_keys_are_normalized(monkeypatch):
    """Some COROS regions answer with the AWS key names
    (SecretAccessKey / SessionToken) instead of the Aliyun ones."""
    creds = {'AccessKeyId': 'aki', 'SecretAccessKey': 'sec', 'SessionToken': 'tok'}
    fake = _FakeGet([('api/proxy/oss/sts', _json_handler(_sts_payload(creds)))])
    monkeypatch.setattr(requests, 'get', fake)

    out = ali_oss_client.fetch_sts('tok-123')
    assert out == {
        'AccessKeyId': 'aki',
        'AccessKeySecret': 'sec',
        'SecurityToken': 'tok',
    }


def test_missing_credential_fields_is_a_failed_channel(monkeypatch):
    creds = {'AccessKeyId': 'aki'}  # no secret, no token
    fake = _FakeGet([
        ('trainingcn.coros.com', _json_handler(_sts_payload(creds))),
        ('/openapi/v2/oss/sts', _json_handler(_sts_payload(creds))),
        ('faq.coros.com', _json_handler(_sts_payload(creds))),
    ])
    monkeypatch.setattr(requests, 'get', fake)

    with pytest.raises(RuntimeError, match="COROS STS request failed"):
        ali_oss_client.fetch_sts('tok-123')


# --- guards -------------------------------------------------------------------

def test_aws_fetch_sts_unknown_bucket_raises():
    with pytest.raises(ValueError, match="No COROS STS sign configured for bucket 'bogus'"):
        aws_oss_client.fetch_sts('bogus')


def test_aws_fetch_sts_coros_s3_without_token_rejected(monkeypatch):
    """regionId=1 has no usable sign, so without a login token there is no
    channel left to authenticate with. Fail loud — and without touching the
    network."""
    fake = _FakeGet()
    monkeypatch.setattr(requests, 'get', fake)

    with pytest.raises(NotImplementedError, match="regionId=1"):
        aws_oss_client.fetch_sts('coros-s3')
    assert fake.calls == [], "placeholder bucket must not hit the network"
