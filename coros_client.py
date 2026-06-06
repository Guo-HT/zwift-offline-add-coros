"""COROS activity upload client.

Mirrors the patterns in /Users/mac/Code/pyCode/garmin-sync-coros/scripts/coros/
(REFERENCE ONLY — do not modify that project). Public surface:

    client = CorosClient()
    client.login(email, password_md5)         # MD5-hashed password
    client.upload_activity(fit_bytes, fit_filename, fit_md5)

All HTTP calls timeout=30s. Internal exceptions are converted to logger.warning
+ return None; callers should not need to catch.
"""

import json
import logging
import uuid

import requests

from oss import ali_oss_client, aws_oss_client

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30  # seconds

# Hardcoded login endpoint per the COROS reference project (always teamcnapi for login,
# the real teamapi URL is returned in the login response).
LOGIN_URL = "https://teamcnapi.coros.com/account/login"

# regionId (returned by login) -> {teamapi, bucket, service, oss module}.
# Mirrors scripts/coros/sts_config.py + region_config.py in the reference.
REGION_CONFIG = {
    1: {"teamapi": "https://teamapi.coros.com",   "bucket": "coros-s3", "service": "aws",    "oss": aws_oss_client},
    2: {"teamapi": "https://teamcnapi.coros.com", "bucket": "coros-oss", "service": "aliyun", "oss": ali_oss_client},
    3: {"teamapi": "https://teameuapi.coros.com", "bucket": "eu-coros",  "service": "aws",    "oss": aws_oss_client},
}


class CorosClient:
    def __init__(self):
        self.access_token = None
        self.user_id = None
        self.region_id = None
        self.teamapi = None
        self._cfg = None  # filled in by login()

    def login(self, email: str, password_md5: str) -> None:
        """POST teamcnapi/account/login with account + MD5(pwd) + accountType=2.

        On success: populates self.access_token / user_id / region_id / teamapi / _cfg.
        On failure: logs a warning and returns without raising.
        """
        try:
            r = requests.post(
                LOGIN_URL,
                json={"account": email, "pwd": password_md5, "accountType": 2},
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json;charset=UTF-8",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "referer": "https://teamcnapi.coros.com/",
                    "origin": "https://teamcnapi.coros.com/",
                },
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            logger.warning("COROS login HTTP failed: %s" % repr(exc))
            return
        if data.get("result") != "0000":
            # Field-whitelist: avoid logging arbitrary fields from the response
            # (some historical COROS responses included user email traces).
            logger.warning("COROS login failed: result=%s message=%s" % (
                data.get("result"), data.get("message")))
            return
        d = data["data"]
        self.access_token = d["accessToken"]
        self.user_id = d["userId"]
        self.region_id = d.get("regionId", 1)
        self._cfg = REGION_CONFIG.get(self.region_id)
        if not self._cfg:
            logger.warning("COROS login: unknown regionId=%s" % self.region_id)
            return
        self.teamapi = self._cfg["teamapi"]
        logger.info("COROS login ok: userId=%s regionId=%s teamapi=%s" % (
            self.user_id, self.region_id, self.teamapi))

    def upload_activity(self, fit_bytes: bytes, fit_filename: str, fit_md5: str) -> None:
        """3-step: upload FIT to OSS (STS handled inside oss client) → call COROS import API.

        fit_bytes: raw FIT file content
        fit_filename: display name (e.g. 'activity_2026-06-05_120000.fit').
            Sanitized to [A-Za-z0-9._-] — the OSS key embeds it, and a
            client-controlled filename with '/' or NULs would let a user
            inject path segments or truncate the key.
        fit_md5: MD5 hex digest of fit_bytes (used by COROS import API)
        """
        if not self.access_token or not self._cfg:
            logger.warning("COROS upload_activity called without login; skipping")
            return
        # Sanitize filename: keep only the safe subset. Replace runs of
        # anything else with '_' and cap at 200 chars (the OSS key is
        # 'fit_zip/<uuid>_<name>' and we don't want a long name to push
        # us past any SDK key-length limit).
        import re
        safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', fit_filename)[:200]
        if not safe_name:
            safe_name = "activity.fit"
        try:
            unique = uuid.uuid4().hex
            oss_key = 'fit_zip/%s_%s' % (unique, safe_name)
            oss_module = self._cfg["oss"]
            if oss_module is aws_oss_client:
                oss_module.upload(fit_bytes, oss_key, bucket=self._cfg["bucket"])
            else:
                oss_module.upload(fit_bytes, oss_key)
            self._call_import_api(oss_key, fit_md5, safe_name, len(fit_bytes))
        except Exception as exc:
            # logger.exception includes the traceback, which is what the
            # user needs to tell "no internet" from "OSS sign rotated" from
            # "import API rejected the file". The old message collapsed all
            # three into one line.
            logger.exception("COROS upload failed; if the FIT was already "
                             "pushed to OSS, the object at key %r is now an "
                             "orphan (COROS won't import and won't clean up "
                             "for you): %s" % (oss_key, repr(exc)))

    def _call_import_api(self, oss_key: str, fit_md5: str, fit_filename: str, size: int) -> None:
        """POST {teamapi}/activity/fit/import as multipart/form-data with jsonParameter field.

        Body JSON (per reference): {source, timezone, bucket, md5, size, object, serviceName, oriFileName}
        Auth: 'accesstoken' (lowercase) header.
        Success: result=="0000" AND data.status==2.
        """
        bucket = self._cfg["bucket"]
        service_name = self._cfg["service"]
        data = {
            "source": 1,
            "timezone": 32,
            "bucket": bucket,
            "md5": fit_md5,
            "size": size,
            "object": oss_key,
            "serviceName": service_name,
            "oriFileName": fit_filename,
        }
        r = requests.post(
            self.teamapi + "/activity/fit/import",
            headers={"accesstoken": self.access_token},
            files={"jsonParameter": (None, json.dumps(data))},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("result") != "0000" or payload.get("data", {}).get("status") != 2:
            # Field-whitelist: don't dump the full response (which may include
            # user-specific data fields) — only the protocol-meaningful codes.
            raise RuntimeError(
                "COROS import API failed: result=%s status=%s message=%s" % (
                    payload.get("result"),
                    payload.get("data", {}).get("status"),
                    payload.get("message"),
                )
            )
        logger.info("COROS activity import ok: %s" % fit_filename)
