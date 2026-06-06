"""AWS S3 multipart upload for COROS activity sync (regionId=1 or 3, international).

Mirrors /Users/mac/Code/pyCode/garmin-sync-coros/scripts/oss/aws_oss_client.py
(REFERENCE ONLY). STS endpoint, app_id, and sign are COROS hardcoded values.
"""

import base64
import json
import logging
import os
import tempfile

import boto3
from boto3.s3.transfer import TransferConfig

logger = logging.getLogger(__name__)

# --- COROS hardcoded constants (do not change; verified against prod API) ---
STS_BASE = "https://faq.coros.com/openapi/oss/sts"
APP_ID = "1660188068672619112"
# Two signs observed in the wild; for regionId=1 the bucket is 'coros-s3',
# for regionId=3 (Europe) the bucket is 'eu-coros'. We pick per region below.
# `coros-s3` is still a placeholder (S3SIGN_COROS_S3) — see fetch_sts() for
# the runtime check that turns it into a clean error instead of a silent
# COROS upload failure.
SIGNS = {
    "coros-s3": "S3SIGN_COROS_S3",   # placeholder: see fetch_sts() guard
    "eu-coros": "877571111A1EE5316E4B590103D4B5B3",
}
# AWS region per bucket. boto3 must be told the right region or the upload
# will fail / get cross-region routed.
BUCKET_TO_REGION = {
    "coros-s3": "us-west-2",
    "eu-coros": "eu-central-1",
}
SALT = "9y78gpoERW4lBNYL"


def _decode_credentials(blob: str) -> dict:
    """Decode the COROS STS response blob.

    NOTE: this is obfuscation, not cryptography. The SALT is a fixed public
    string baked into the reference client (`/Users/mac/Code/pyCode/garmin-sync-coros`)
    and shared across all installs. The COROS server relies on the `sign`
    query param in `fetch_sts` to authorize the request; the `credentials`
    blob is base64(salt+JSON), so anyone with the STS response can decode it.
    Treat the resulting AccessKey as short-lived (STS default = 1h).
    """
    return json.loads(base64.b64decode(blob.replace(SALT, '')).decode('utf-8'))


def fetch_sts(bucket: str, service: str = "aws") -> dict:
    """GET STS endpoint with hardcoded query params.

    Returns dict with: AccessKeyId, AccessKeySecret, SecurityToken.

    Raises:
        ValueError: if `bucket` is unknown OR if its STS sign is still a
            placeholder (S3SIGN_COROS_S3). The latter is intentionally
            distinguished so the caller can show "COROS regionId=1 is not
            yet supported" instead of an opaque "COROS upload failed"
            warning that the user can't act on.
    """
    import requests
    if bucket not in SIGNS:
        raise ValueError("No COROS STS sign configured for bucket %r" % bucket)
    sign = SIGNS[bucket]
    if sign.startswith("S3SIGN_COROS_S3"):
        # Placeholder. Without this guard, the upload would proceed,
        # COROS would 4xx the STS call, and the user would see only a
        # vague logger.warning("COROS upload failed"). Fail loud here.
        raise NotImplementedError(
            "COROS regionId=1 (bucket %r) is not yet supported: STS sign is "
            "still the S3SIGN_COROS_S3 placeholder. Until it is filled in, "
            "regionId=1 uploads will fail. Use regionId=2 or 3." % bucket
        )
    url = '%s?bucket=%s&service=%s&app_id=%s&sign=%s&v=2' % (
        STS_BASE, bucket, service, APP_ID, sign)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    payload = r.json()
    if payload.get('code') != 200:
        raise RuntimeError("COROS STS request failed: %s" % payload)
    creds = _decode_credentials(payload['data']['credentials'])
    return creds


def upload(fit_bytes: bytes, key: str, bucket: str = "eu-coros", region: str = None) -> str:
    """Multipart upload fit_bytes to AWS S3. Returns the OSS object key.

    bucket: 'coros-s3' (regionId=1) or 'eu-coros' (regionId=3). Defaults to
        'eu-coros' because 'coros-s3' still has a placeholder STS sign
        (S3SIGN_COROS_S3) and would fail until Task 7 confirms it.
    region: AWS region for the bucket. If None, inferred via BUCKET_TO_REGION
        ('us-west-2' for coros-s3, 'eu-central-1' for eu-coros).
    key: full OSS key (e.g. 'fit_zip/activity_xxx.fit')
    """
    if region is None:
        region = BUCKET_TO_REGION.get(bucket, "us-west-2")
    creds = fetch_sts(bucket)
    client = boto3.client(
        's3',
        aws_access_key_id=creds['AccessKeyId'],
        aws_secret_access_key=creds['AccessKeySecret'],
        aws_session_token=creds['SecurityToken'],
        region_name=region,
    )
    # Multipart upload: write to temp file (boto3 needs a path for part splitting).
    fd, tmp = tempfile.mkstemp(suffix='.fit')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(fit_bytes)
        config = TransferConfig(
            multipart_threshold=5 * 1024 * 1024,
            max_concurrency=4,
            multipart_chunksize=5 * 1024 * 1024,
            use_threads=True,
        )
        client.upload_file(tmp, Bucket=bucket, Key=key, Config=config)
        logger.debug("AWS S3 multipart upload ok: %s" % key)
        return key
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
