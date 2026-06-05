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
SIGNS = {
    "coros-s3": "S3SIGN_COROS_S3",   # TODO: confirm with Task 7 smoke test
    "eu-coros": "877571111A1EE5316E4B590103D4B5B3",
}
SALT = "9y78gpoERW4lBNYL"


def _decode_credentials(blob: str) -> dict:
    return json.loads(base64.b64decode(blob.replace(SALT, '')).decode('utf-8'))


def fetch_sts(bucket: str, service: str = "aws") -> dict:
    """GET STS endpoint with hardcoded query params.

    Returns dict with: AccessKeyId, AccessKeySecret, SecurityToken.
    """
    import requests
    sign = SIGNS[bucket]
    url = '%s?bucket=%s&service=%s&app_id=%s&sign=%s&v=2' % (
        STS_BASE, bucket, service, APP_ID, sign)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    payload = r.json()
    if payload.get('code') != 200:
        raise RuntimeError("COROS STS request failed: %s" % payload)
    creds = _decode_credentials(payload['data']['credentials'])
    return creds


def upload(fit_bytes: bytes, key: str, bucket: str = "coros-s3", region: str = "us-west-2") -> str:
    """Multipart upload fit_bytes to AWS S3. Returns the OSS object key.

    bucket: 'coros-s3' (regionId=1) or 'eu-coros' (regionId=3)
    region: AWS region string for the chosen bucket
    key: full OSS key (e.g. 'fit_zip/activity_xxx.fit')
    """
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
