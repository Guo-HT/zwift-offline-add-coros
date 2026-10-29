"""AWS S3 multipart upload for COROS activity sync (regionId=1 or 3, international).

Mirrors /Users/mac/Code/pyCode/garmin-sync-coros/scripts/oss/aws_oss_client.py
(REFERENCE ONLY). STS endpoint, app_id, and sign are COROS hardcoded values.

Since 2026-10-03 COROS closed the anonymous STS channel
(faq.coros.com/openapi/oss/sts → nginx 404); the credentials now come from
the training-centre BFF proxy, authenticated with the login accessToken.
See oss/sts.py for the channel list.
"""

import logging
import os
import tempfile

import boto3
from boto3.s3.transfer import TransferConfig

from oss import sts

logger = logging.getLogger(__name__)

# --- COROS hardcoded constants (do not change; verified against prod API) ---
STS_BASE = sts.LEGACY_STS_BASE  # anonymous channel — closed by COROS 2026-10-03
APP_ID = sts.APP_ID
# Two signs observed in the wild; for regionId=1 the bucket is 'coros-s3',
# for regionId=3 (Europe) the bucket is 'eu-coros'. We pick per region below.
# `coros-s3` is still a placeholder (S3SIGN_COROS_S3). The sign is only
# needed by the anonymous channel, which COROS closed on 2026-10-03, so
# regionId=1 now relies on the token-authenticated channels (see fetch_sts()).
SIGNS = {
    "coros-s3": "S3SIGN_COROS_S3",   # placeholder: see fetch_sts() guard
    "eu-coros": "877571111A1EE5316E4B590103D4B5B3",
}
# Training-centre hosts that proxy the STS request, per bucket. The region
# host is listed first; training.coros.com is the global fallback.
STS_HOSTS = {
    "coros-s3": ("https://training.coros.com",),
    "eu-coros": ("https://trainingeu.coros.com", "https://training.coros.com"),
}
# AWS region per bucket. boto3 must be told the right region or the upload
# will fail / get cross-region routed.
BUCKET_TO_REGION = {
    "coros-s3": "us-west-2",
    "eu-coros": "eu-central-1",
}
SALT = sts.SALT


def _decode_credentials(blob: str) -> dict:
    """Decode the COROS STS response blob.

    NOTE: this is obfuscation, not cryptography. The SALT is a fixed public
    string baked into the reference client (`/Users/mac/Code/pyCode/garmin-sync-coros`)
    and shared across all installs. The COROS server relies on the `sign`
    query param in `fetch_sts` to authorize the request; the `credentials`
    blob is base64(salt+JSON), so anyone with the STS response can decode it.
    Treat the resulting AccessKey as short-lived (STS default = 1h).
    """
    return sts.decode_credentials(blob)


def fetch_sts(bucket: str, service: str = "aws", access_token: str = None) -> dict:
    """Fetch temporary S3 credentials for `bucket` from COROS.

    access_token: the accessToken returned by COROS /account/login. Required
        by the channels that are still alive — the anonymous one was closed
        by COROS on 2026-10-03.

    Returns dict with: AccessKeyId, AccessKeySecret, SecurityToken.

    Raises:
        ValueError: if `bucket` is unknown.
        NotImplementedError: if the bucket has no usable STS sign *and* no
            login token was supplied. The sign is only needed by the
            anonymous channel, which is gone; without a token there is
            nothing left to authenticate with.
    """
    if bucket not in SIGNS:
        raise ValueError("No COROS STS sign configured for bucket %r" % bucket)
    sign = SIGNS[bucket]
    if sign.startswith("S3SIGN_COROS_S3"):
        # Placeholder sign — only the (now dead) anonymous channel needs it.
        sign = None
    if sign is None and not access_token:
        # Without a token the remaining channels cannot authenticate, so
        # fail loud here instead of letting the upload die further down with
        # a vague "COROS upload failed" the user can't act on.
        raise NotImplementedError(
            "COROS regionId=1 (bucket %r) needs a logged-in COROS access "
            "token: the anonymous STS channel was closed by COROS on "
            "2026-10-03 and this bucket's STS sign is still the "
            "S3SIGN_COROS_S3 placeholder. Use regionId=2 or 3 if you "
            "cannot log in." % bucket
        )
    return sts.fetch_sts(
        bucket=bucket,
        service=service,
        access_token=access_token,
        sts_hosts=STS_HOSTS.get(bucket, ()),
        sign=sign,
        app_id=APP_ID,
    )


def upload(fit_bytes: bytes, key: str, bucket: str = "eu-coros", region: str = None,
           access_token: str = None) -> str:
    """Multipart upload fit_bytes to AWS S3. Returns the OSS object key.

    bucket: 'coros-s3' (regionId=1) or 'eu-coros' (regionId=3). Defaults to
        'eu-coros' because 'coros-s3' still has a placeholder STS sign
        (S3SIGN_COROS_S3) and would fail until Task 7 confirms it.
    region: AWS region for the bucket. If None, inferred via BUCKET_TO_REGION
        ('us-west-2' for coros-s3, 'eu-central-1' for eu-coros).
    key: full OSS key (e.g. 'fit_zip/activity_xxx.fit')
    access_token: COROS login token, used to fetch the STS credentials.
    """
    if region is None:
        region = BUCKET_TO_REGION.get(bucket, "us-west-2")
    creds = fetch_sts(bucket, access_token=access_token)
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
