"""Aliyun OSS multipart upload for COROS activity sync (regionId=2, China).

Mirrors /Users/mac/Code/pyCode/garmin-sync-coros/scripts/oss/ali_oss_client.py
(REFERENCE ONLY). STS endpoint, app_id, and sign are COROS hardcoded values.
"""

import base64
import json
import logging
import os
import tempfile

import oss2
from oss2 import SizedFileAdapter, determine_part_size
from oss2.models import PartInfo

logger = logging.getLogger(__name__)

# --- COROS hardcoded constants (do not change; verified against prod API) ---
STS_BASE = "https://faq.coros.com/openapi/oss/sts"
APP_ID = "1660188068672619112"
SIGN = "9AD4AA35AAFEE6BB1E847A76848D58DF"  # sign for aliyun
BUCKET = "coros-oss"
SERVICE = "aliyun"
OSS_ENDPOINT = "https://oss-cn-beijing.aliyuncs.com"
SALT = "9y78gpoERW4lBNYL"  # used to decode the STS credentials blob


def _decode_credentials(blob: str) -> dict:
    """Decode the COROS STS response blob.

    NOTE: this is obfuscation, not cryptography. The SALT is a fixed public
    string baked into the reference client and shared across all installs.
    The COROS server relies on the `sign` query param in `fetch_sts` to
    authorize the request; the `credentials` blob is base64(salt+JSON), so
    anyone with the STS response can decode it. Treat the resulting
    AccessKey as short-lived (STS default = 1h).
    """
    return json.loads(base64.b64decode(blob.replace(SALT, '')).decode('utf-8'))


def fetch_sts() -> dict:
    """GET STS endpoint with hardcoded query params.

    Returns dict with: AccessKeyId, AccessKeySecret, SecurityToken.
    """
    import requests
    url = '%s?bucket=%s&service=%s&app_id=%s&sign=%s&v=2' % (
        STS_BASE, BUCKET, SERVICE, APP_ID, SIGN)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    payload = r.json()
    if payload.get('code') != 200:
        raise RuntimeError("COROS STS request failed: %s" % payload)
    creds = _decode_credentials(payload['data']['credentials'])
    return creds  # AccessKeyId / AccessKeySecret / SecurityToken


def upload(fit_bytes: bytes, key: str) -> str:
    """Multipart upload fit_bytes to Aliyun OSS. Returns the OSS object key.

    key: full OSS key (e.g. 'fit_zip/activity_xxx.fit'). Caller chooses.
    """
    creds = fetch_sts()
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
