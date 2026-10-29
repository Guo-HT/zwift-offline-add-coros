"""Aliyun OSS multipart upload for COROS activity sync (regionId=2, China).

Mirrors /Users/mac/Code/pyCode/garmin-sync-coros/scripts/oss/ali_oss_client.py
(REFERENCE ONLY). STS endpoint, app_id, and sign are COROS hardcoded values.

Since 2026-10-03 COROS closed the anonymous STS channel
(faq.coros.com/openapi/oss/sts → nginx 404); the credentials now come from
the training-centre BFF proxy, authenticated with the login accessToken.
See oss/sts.py for the channel list.
"""

import logging
import os
import tempfile

import oss2
from oss2 import SizedFileAdapter, determine_part_size
from oss2.models import PartInfo

from oss import sts

logger = logging.getLogger(__name__)

# --- COROS hardcoded constants (do not change; verified against prod API) ---
STS_BASE = sts.LEGACY_STS_BASE  # anonymous channel — closed by COROS 2026-10-03
APP_ID = sts.APP_ID
SIGN = "9AD4AA35AAFEE6BB1E847A76848D58DF"  # sign for aliyun
BUCKET = "coros-oss"
SERVICE = "aliyun"
OSS_ENDPOINT = "https://oss-cn-beijing.aliyuncs.com"
SALT = sts.SALT  # used to decode the STS credentials blob

# Training-centre host that proxies the STS request for the CN region.
STS_HOSTS = ("https://trainingcn.coros.com",)


def _decode_credentials(blob: str) -> dict:
    """Decode the COROS STS response blob.

    NOTE: this is obfuscation, not cryptography. The SALT is a fixed public
    string baked into the reference client and shared across all installs.
    The COROS server relies on the `sign` query param in `fetch_sts` to
    authorize the request; the `credentials` blob is base64(salt+JSON), so
    anyone with the STS response can decode it. Treat the resulting
    AccessKey as short-lived (STS default = 1h).
    """
    return sts.decode_credentials(blob)


def fetch_sts(access_token: str = None) -> dict:
    """Fetch temporary OSS credentials for the CN region.

    access_token: the accessToken returned by COROS /account/login. Required
        by the channels that are still alive — the anonymous one was closed
        by COROS on 2026-10-03.

    Returns dict with: AccessKeyId, AccessKeySecret, SecurityToken.
    """
    return sts.fetch_sts(
        bucket=BUCKET,
        service=SERVICE,
        access_token=access_token,
        sts_hosts=STS_HOSTS,
        sign=SIGN,
        app_id=APP_ID,
    )


def upload(fit_bytes: bytes, key: str, access_token: str = None) -> str:
    """Multipart upload fit_bytes to Aliyun OSS. Returns the OSS object key.

    key: full OSS key (e.g. 'fit_zip/activity_xxx.fit'). Caller chooses.
    access_token: COROS login token, used to fetch the STS credentials.
    """
    creds = fetch_sts(access_token)
    auth = oss2.StsAuth(creds['AccessKeyId'], creds['AccessKeySecret'], creds['SecurityToken'])
    bucket = oss2.Bucket(auth, OSS_ENDPOINT, BUCKET)

    # Multipart upload (per reference project): write to temp file first because
    # oss2 needs a real file path for part splitting.
    fd, tmp = tempfile.mkstemp(suffix='.fit')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(fit_bytes)
        total_size = os.path.getsize(tmp)
        part_size = determine_part_size(total_size, preferred_size=5 * 1024 * 1024)
        upload_id = bucket.init_multipart_upload(key).upload_id
        parts = []
        with open(tmp, 'rb') as f:
            part_number = 1
            offset = 0
            while offset < total_size:
                size = min(part_size, total_size - offset)
                f.seek(offset)
                result = bucket.upload_part(key, upload_id, part_number, SizedFileAdapter(f, size))
                parts.append(PartInfo(part_number, result.etag))
                offset += size
                part_number += 1
        bucket.complete_multipart_upload(key, upload_id, parts)
        logger.debug("Ali OSS multipart upload ok: %s" % key)
        return key
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
