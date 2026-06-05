"""Unit tests for the helper functions in oss/ali_oss_client and oss/aws_oss_client.

These tests are pure: no HTTP, no SDK calls. They verify the constants and the
base64+salt decoding logic.
"""

import base64
import json

import pytest

from oss import ali_oss_client, aws_oss_client


# --- Constants integrity ------------------------------------------------------

def test_ali_constants_present():
    assert ali_oss_client.STS_BASE.startswith('https://')
    assert ali_oss_client.APP_ID
    assert ali_oss_client.SIGN
    assert ali_oss_client.BUCKET == 'coros-oss'
    assert ali_oss_client.SERVICE == 'aliyun'
    assert ali_oss_client.OSS_ENDPOINT.startswith('https://')
    assert ali_oss_client.SALT == '9y78gpoERW4lBNYL'


def test_aws_constants_present():
    assert aws_oss_client.STS_BASE.startswith('https://')
    assert aws_oss_client.APP_ID
    assert 'coros-s3' in aws_oss_client.SIGNS
    assert 'eu-coros' in aws_oss_client.SIGNS
    assert aws_oss_client.BUCKET_TO_REGION['coros-s3'] == 'us-west-2'
    assert aws_oss_client.BUCKET_TO_REGION['eu-coros'] == 'eu-central-1'


# --- _decode_credentials ------------------------------------------------------

def _make_blob(payload: dict, salt: str = '9y78gpoERW4lBNYL') -> str:
    """Build a fake COROS STS credentials blob: <salt> + base64(json)."""
    body = json.dumps(payload).encode('utf-8')
    return salt + base64.b64encode(body).decode('ascii')


def test_ali_decode_credentials_roundtrip():
    creds = {'AccessKeyId': 'aki-1', 'AccessKeySecret': 'secret-1', 'SecurityToken': 'tok-1'}
    blob = _make_blob(creds, salt=ali_oss_client.SALT)
    out = ali_oss_client._decode_credentials(blob)
    assert out == creds


def test_aws_decode_credentials_roundtrip():
    creds = {'AccessKeyId': 'aki-2', 'AccessKeySecret': 'secret-2', 'SecurityToken': 'tok-2'}
    blob = _make_blob(creds, salt=aws_oss_client.SALT)
    out = aws_oss_client._decode_credentials(blob)
    assert out == creds


def test_ali_decode_credentials_handles_unicode_payload():
    creds = {'AccessKeyId': 'aki-中文', 'AccessKeySecret': 's', 'SecurityToken': 't'}
    blob = _make_blob(creds, salt=ali_oss_client.SALT)
    out = ali_oss_client._decode_credentials(blob)
    assert out == creds


def test_ali_decode_credentials_works_without_salt():
    """The salt is a COROS obfuscation, not a security check. Even without
    it, valid base64+JSON round-trips through _decode_credentials."""
    raw = json.dumps({'AccessKeyId': 'aki', 'AccessKeySecret': 'sec', 'SecurityToken': 'tok'}).encode('utf-8')
    blob = base64.b64encode(raw).decode('ascii')  # no salt
    out = ali_oss_client._decode_credentials(blob)
    assert out['AccessKeyId'] == 'aki'


def test_ali_decode_credentials_invalid_base64_raises():
    """A blob that isn't valid base64 at all should raise."""
    with pytest.raises(Exception):
        ali_oss_client._decode_credentials('!!!not-base64!!!')
