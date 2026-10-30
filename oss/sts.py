"""Shared COROS STS (temporary OSS credential) acquisition.

Channel history
===============

Until 2026-10-03 COROS exposed an *anonymous* STS endpoint::

    GET https://faq.coros.com/openapi/oss/sts
        ?bucket=<bucket>&service=<aws|aliyun>&app_id=...&sign=...&v=2

On 2026-10-03 COROS removed that channel (the endpoint now answers with an
nginx 404) and the web training centre moved to its own BFF proxy::

    GET https://training<region>.coros.com/api/proxy/oss/sts
        ?bucket=<bucket>&service=<aws|aliyun>&v=2
    Cookie: CPL-coros-token=<accessToken from /account/login>

The proxy needs neither `sign` nor `app_id` — the login access token *is*
the credential. The open API also grew a `v2` variant that answers
``401 {"code":401,"msg":"unauthorized"}`` when no `accesstoken` header is
supplied.

This module walks the channels in priority order and returns the first set
of credentials it manages to fetch:

  1. ``web-proxy``   — needs the login access token (the current channel)
  2. ``v2``          — needs the login access token
  3. ``v2+sign``     — as above, plus the per-bucket sign/app_id
  4. ``legacy``      — anonymous, but dead since 2026-10-03; kept as a
                       fallback for accounts whose region still serves it.

Upstream reference: XiaoSiHwang/garmin-sync-coros issue #41 and PR #42
(https://github.com/XiaoSiHwang/garmin-sync-coros/issues/41).
"""

import base64
import json
import logging

import requests

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30  # seconds

# The "salt" is not a secret: it is a fixed public string baked into every
# COROS client. The credentials blob is base64(salt + JSON).
SALT = '9y78gpoERW4lBNYL'
APP_ID = '1660188068672619112'

# Anonymous channel — removed by COROS on 2026-10-03 (nginx 404).
LEGACY_STS_BASE = 'https://faq.coros.com/openapi/oss/sts'
# v2 channel — alive, but requires the login access token.
V2_STS_BASE = 'https://faq.coros.com/openapi/v2/oss/sts'
# Path of the training-centre BFF proxy that replaced the anonymous channel.
PROXY_STS_PATH = '/api/proxy/oss/sts'
# Name of the cookie the training centre uses for the login token.
TOKEN_COOKIE = 'CPL-coros-token'


def decode_credentials(blob: str) -> dict:
    """Decode the COROS STS credentials blob (base64 + salt wrapped JSON).

    NOTE: this is obfuscation, not cryptography. The SALT is a fixed public
    string shared across all installs. Treat the resulting AccessKey as
    short-lived (STS default = 1h).
    """
    return json.loads(base64.b64decode(blob.replace(SALT, '')).decode('utf-8'))


def normalize_credentials(creds: dict) -> dict:
    """Normalize a decoded credentials dict to a single set of key names.

    COROS returns the AWS-flavoured names (``SecretAccessKey`` /
    ``SessionToken``) on some regions and the Aliyun-flavoured ones
    (``AccessKeySecret`` / ``SecurityToken``) on others, so accept both.

    Returns: {'AccessKeyId', 'AccessKeySecret', 'SecurityToken'}
    """
    access_key_id = creds.get('AccessKeyId')
    access_key_secret = creds.get('AccessKeySecret') or creds.get('SecretAccessKey')
    security_token = creds.get('SecurityToken') or creds.get('SessionToken')
    if not access_key_id or not access_key_secret or not security_token:
        raise RuntimeError(
            "COROS STS response is missing credential fields (got: %s)"
            % ', '.join(sorted(creds.keys()))
        )
    return {
        'AccessKeyId': access_key_id,
        'AccessKeySecret': access_key_secret,
        'SecurityToken': security_token,
    }


def _channel_attempts(bucket, service, access_token, sts_hosts, sign, app_id):
    """Yield (label, url, headers) for every STS channel, in priority order."""
    base_query = 'bucket=%s&service=%s&v=2' % (bucket, service)
    signed_query = '%s&app_id=%s&sign=%s' % (base_query, app_id, sign)
    if access_token:
        # 1. Training-centre BFF proxy (the channel that replaced the
        #    anonymous one on 2026-10-03). The login token is the credential.
        for host in sts_hosts:
            yield (
                'web-proxy(%s)' % host,
                '%s%s?%s' % (host, PROXY_STS_PATH, base_query),
                {'Cookie': '%s=%s' % (TOKEN_COOKIE, access_token)},
            )
        # 2. The v2 open API — same token, different header name.
        yield (
            'v2',
            '%s?%s' % (V2_STS_BASE, base_query),
            {'accesstoken': access_token},
        )
        if sign:
            yield (
                'v2+sign',
                '%s?%s' % (V2_STS_BASE, signed_query),
                {'accesstoken': access_token},
            )
    # 3. The old anonymous channel. Dead since 2026-10-03, but harmless to
    #    try and it keeps accounts on an un-migrated region working.
    if sign:
        yield (
            'legacy',
            '%s?%s' % (LEGACY_STS_BASE, signed_query),
            {},
        )


def _error_detail(r) -> str:
    """Short, whitelisted diagnostic from a failed STS response.

    Only the protocol-level fields are included — never the raw body,
    which can contain account data.
    """
    if r is None:
        return ''
    try:
        body = r.json()
    except Exception:
        return ''
    if not isinstance(body, dict):
        return ''
    parts = []
    for key in ('code', 'msg', 'message', 'errorCode'):
        val = body.get(key)
        if isinstance(val, (str, int)):
            parts.append('%s=%s' % (key, val))
    if not parts:
        return ''
    return ' (%s)' % ', '.join(parts)


def _try_channel(label, url, headers):
    """Fetch one STS channel. Returns normalized credentials or None."""
    r = None
    try:
        r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:
        # The proxy answers 401 with {"code":401,"msg":"unauthorized"} —
        # surfacing the protocol fields makes "token expired" obvious
        # without dumping the whole body into the log.
        logger.warning("COROS STS channel %s failed: %s%s" % (
            label, repr(exc), _error_detail(r)))
        return None
    if not isinstance(payload, dict):
        logger.warning("COROS STS channel %s returned a non-object body" % label)
        return None
    ok = payload.get('code') in (200, '200') or payload.get('result') == '0000'
    data = payload.get('data')
    blob = data.get('credentials') if isinstance(data, dict) else None
    if not ok or not blob:
        # Field-whitelist: only log the protocol-meaningful fields.
        logger.warning(
            "COROS STS channel %s returned no credentials: code=%s msg=%s"
            % (label, payload.get('code'), payload.get('msg') or payload.get('message'))
        )
        return None
    try:
        return normalize_credentials(decode_credentials(blob))
    except Exception as exc:
        logger.warning("COROS STS channel %s returned unusable credentials: %s"
                       % (label, repr(exc)))
        return None


def fetch_sts(bucket, service, access_token=None, sts_hosts=(), sign=None,
              app_id=APP_ID):
    """Fetch temporary OSS credentials for `bucket` from COROS.

    access_token: the accessToken returned by /account/login. Required for
        the channels that are still alive (the anonymous one was closed by
        COROS on 2026-10-03).
    sts_hosts: training-centre hosts to try as BFF proxies, in order.
    sign: the per-bucket `sign` parameter. Only the anonymous legacy channel
        needs it; pass None if the bucket has no usable sign.

    Returns: {'AccessKeyId', 'AccessKeySecret', 'SecurityToken'}
    Raises: RuntimeError if no channel yields credentials.
    """
    failures = []
    for label, url, headers in _channel_attempts(
            bucket, service, access_token, sts_hosts, sign, app_id):
        creds = _try_channel(label, url, headers)
        if creds:
            logger.info("COROS STS ok: channel=%s bucket=%s" % (label, bucket))
            return creds
        failures.append(label)
    raise RuntimeError(
        "COROS STS request failed: no credentials from any channel (%s)"
        % ', '.join(failures)
    )
