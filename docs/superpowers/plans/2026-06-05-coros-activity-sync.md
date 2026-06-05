# COROS 活动同步 - 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 zwift-offline 中增加 COROS 平台作为活动上传目标，活动结束后 FIT 数据自动推送至 COROS。

**Architecture:** 严格照搬现有 Strava / Garmin 的"独立模块 + 薄包装 + 启动器设置页"模式。6 个原子化 commit：
1. `oss/` 子包（Ali OSS + AWS S3 客户端）
2. `coros_client.py`（CorosClient 类）
3. `requirements.txt` 加 SDK 依赖
4. `zwift_offline.py` 新增 `coros_upload()` 函数 + `/coros/<username>/` 路由
5. `coros.html` 启动器模板 + `settings.html` 加链接
6. `activity_uploads()` 末尾追加 `coros_upload()` 调用

**Tech Stack:** Python 3.8+, Flask, requests, oss2, boto3, alibabacloud_tea_openapi

**Spec:** `docs/superpowers/specs/2026-06-05-coros-activity-sync-design.md`

**Ref:** `/Users/mac/Code/pyCode/garmin-sync-coros/`（**只读参考**，不修改）

**项目上下文：**
- 项目没有测试套件（CLAUDE.md 明示）。每步的"验证"用 `python -c "..."` / Flask app 启动检查代替 pytest。
- 4055 行单体 `zwift_offline.py`，所有改动必须遵循现有 import / 缩进风格。
- 凭据存储使用项目自带的 `encrypt_credentials`（AES-CFB，文件 = `.bin`）。
- 用户指示："有问题后面测试调试解决"——按"快速 smoke 验证"走，不要写形式化测试。

---

## Task 1: 创建 oss 子包（3 个文件）

**Files:**
- Create: `oss/__init__.py`（空）
- Create: `oss/ali_oss_client.py`（~80 行）
- Create: `oss/aws_oss_client.py`（~80 行）

- [ ] **Step 1: 创建 oss/__init__.py**

```bash
mkdir -p oss
```

写一个空文件 `oss/__init__.py`（用 Write 工具创建，content 为空字符串）。

- [ ] **Step 2: 写 oss/ali_oss_client.py**

```python
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
    """Decode the base64+salt-wrapped credentials string from COROS STS response."""
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
```

- [ ] **Step 3: 写 oss/aws_oss_client.py**

```python
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
```

- [ ] **Step 4: 验证 import 通畅（容忍 ImportError 直到 Task 3 装包）**

```bash
python -c "from oss import ali_oss_client, aws_oss_client; print('ok')"
```

预期：
- 如果已装 `oss2` / `boto3`：输出 `ok`
- 如果没装：报 `ModuleNotFoundError`——预期，继续 Task 3

- [ ] **Step 5: Commit**

```bash
git add oss/__init__.py oss/ali_oss_client.py oss/aws_oss_client.py
git commit -m "Add OSS clients (Ali + AWS) for COROS upload"
```

---

## Task 2: 写 coros_client.py

**Files:**
- Create: `coros_client.py`（项目根，~150 行）

- [ ] **Step 1: 写 coros_client.py**

```python
"""COROS activity upload client.

Mirrors the patterns in /Users/mac/Code/pyCode/garmin-sync-coros/scripts/coros/
(REFERENCE ONLY — do not modify that project). Public surface:

    client = CorosClient()
    client.login(email, password_md5)         # MD5-hashed password
    client.upload_activity(fit_bytes, name, fit_md5)

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
    3: {"teamapi": "https://teamapi.coros.com",   "bucket": "eu-coros",  "service": "aws",    "oss": aws_oss_client},
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
            data = r.json()
        except Exception as exc:
            logger.warning("COROS login HTTP failed: %s" % repr(exc))
            return
        if data.get("result") != "0000":
            logger.warning("COROS login failed: %s" % data.get("message", data))
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
        fit_filename: display name (e.g. 'activity_2026-06-05_120000.fit')
        fit_md5: MD5 hex digest of fit_bytes (used by COROS import API)
        """
        if not self.access_token or not self._cfg:
            logger.warning("COROS upload_activity called without login; skipping")
            return
        try:
            unique = uuid.uuid4().hex
            oss_key = 'fit_zip/%s_%s' % (unique, fit_filename)
            self._cfg["oss"].upload(fit_bytes, oss_key)
            self._call_import_api(oss_key, fit_md5, fit_filename, len(fit_bytes))
        except Exception as exc:
            logger.warning("COROS upload failed. No internet? %s" % repr(exc))

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
        payload = r.json()
        if payload.get("result") != "0000" or payload.get("data", {}).get("status") != 2:
            raise RuntimeError("COROS import API failed: %s" % payload)
        logger.info("COROS activity import ok: %s" % fit_filename)
```

- [ ] **Step 2: 验证 import 通畅（容忍 ImportError 直到 Task 3 装包）**

```bash
python -c "import coros_client; print('ok')"
```

预期：
- 如果已经装了 `oss2` 和 `boto3`：输出 `ok`
- 如果没装：报 `ModuleNotFoundError: No module named 'oss2'` —— 这是预期的，继续 Task 3

- [ ] **Step 3: Commit**

```bash
git add coros_client.py
git commit -m "Add CorosClient (login, OSS upload, import API)"
```

---

## Task 3: 更新 requirements.txt 并安装依赖

**Files:**
- Modify: `requirements.txt`（在末尾追加 3 行）

- [ ] **Step 1: 追加依赖**

读 `requirements.txt` 现有内容（首 11 行已知），用 Edit 在末尾追加：

```text
oss2==2.19.1
boto3==1.36.26
alibabacloud_tea_openapi==0.3.12
```

实际命令（用 Edit 工具）：

```
old_string: fitdecode==0.10.0
werkzeug==3.0.3
new_string: fitdecode==0.10.0
werkzeug==3.0.3
oss2==2.19.1
boto3==1.36.26
alibabacloud_tea_openapi==0.3.12
replace_all: false
```

- [ ] **Step 2: 安装新依赖**

```bash
pip install -r requirements.txt
```

预期：3 个包成功安装。可能需要先 `pip install --upgrade pip`。

- [ ] **Step 3: 验证 4 个模块都能 import**

```bash
python -c "import oss2, boto3, alibabacloud_tea_openapi, coros_client; print('ok')"
```

预期：`ok`

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "Pin COROS SDK deps (oss2, boto3, alibabacloud_tea_openapi)"
```

---

## Task 4: 在 zwift_offline.py 加 coros_upload() 函数 + /coros/<username>/ 路由

**Files:**
- Modify: `zwift_offline.py` — 在 2 处插入新代码
  - 在 903 行后插入 `/coros/<username>/` 路由
  - 在 2262 行后插入 `coros_upload()` 函数

⚠️ **行号会因前面的插入而漂移**——用 Edit 工具按**上下文锚点**定位，不要按行号。

- [ ] **Step 1: 添加 `/coros/<username>/` 路由（紧跟 `/garmin` 路由之后）**

用 Edit 工具：

```
old_string:
    cred = decrypt_credentials(file)
    return render_template("garmin.html", username=current_user.username, uname=cred[0], passw=cred[1])

new_string:
    cred = decrypt_credentials(file)
    return render_template("garmin.html", username=current_user.username, uname=cred[0], passw=cred[1])


@app.route("/coros/<username>/", methods=["GET", "POST"])
@login_required
def coros(username):
    file = '%s/%s/coros_credentials.bin' % (STORAGE_DIR, current_user.player_id)
    if request.method == "POST":
        if request.form['username'] == "" or request.form['password'] == "":
            flash("COROS credentials can't be empty.")
            return render_template("coros.html", username=current_user.username)
        encrypt_credentials(file, (request.form['username'], request.form['password']))
        return redirect(url_for('settings', username=current_user.username))
    cred = decrypt_credentials(file)
    return render_template("coros.html", username=current_user.username, uname=cred[0], passw=cred[1])

replace_all: false
```

- [ ] **Step 2: 添加 `coros_upload()` 函数（紧跟 `garmin_upload()` 之后）**

用 Edit 工具：

```
old_string:
    try:
        garth.client.post("connectapi", "/upload-service/upload", api=True, files={"file": (activity.fit_filename, BytesIO(activity.fit))})
    except Exception as exc:
        logger.warning("Garmin upload failed. No internet? %s" % repr(exc))

new_string:
    try:
        garth.client.post("connectapi", "/upload-service/upload", api=True, files={"file": (activity.fit_filename, BytesIO(activity.fit))})
    except Exception as exc:
        logger.warning("Garmin upload failed. No internet? %s" % repr(exc))


def coros_upload(player_id, activity):
    try:
        import coros_client
    except ImportError as exc:
        logger.warning("COROS client libs not installed. Skipping COROS upload: %s" % repr(exc))
        return
    profile_dir = '%s/%s' % (STORAGE_DIR, player_id)
    creds_file = '%s/coros_credentials' % profile_dir
    if os.path.exists(creds_file + '.bin'):
        creds_file += '.bin'
    elif os.path.exists(creds_file + '.txt'):
        creds_file += '.txt'
    else:
        logger.info("coros_credentials missing, skip COROS activity update")
        return
    email, password = decrypt_credentials(creds_file)
    try:
        import hashlib
        password_md5 = hashlib.md5(password.encode()).hexdigest()
        fit_md5 = hashlib.md5(activity.fit).hexdigest()
        client = coros_client.CorosClient()
        client.login(email, password_md5)
        client.upload_activity(activity.fit, activity.fit_filename, fit_md5)
    except Exception as exc:
        logger.warning("COROS upload failed. No internet? %s" % repr(exc))

replace_all: false
```

- [ ] **Step 3: 验证 Python 语法**

```bash
python -c "import zwift_offline; print('zwift_offline imported ok')"
```

预期：`zwift_offline imported ok`

如果 `ImportError` 或 `SyntaxError`：回查 Step 1/2 的 Edit，常见错是 `coros` 函数名和 `import coros_client` 模块名冲突——Python 允许同名（模块和函数不冲突），但 `import coros_client` 一定要写完整模块名。

- [ ] **Step 4: 验证路由注册**

```bash
python -c "
import zwift_offline
app = zwift_offline.app
rules = [str(r) for r in app.url_map.iter_rules() if 'coros' in str(r)]
print('COROS routes:', rules)
assert any('/coros/<username>/' in r for r in rules), 'route missing'
print('ok')
"
```

预期：
```
COROS routes: ['/coros/<username>/']
ok
```

- [ ] **Step 5: Commit**

```bash
git add zwift_offline.py
git commit -m "Add coros_upload() and /coros/<username>/ route"
```

---

## Task 5: 添加 coros.html 启动器模板 + settings.html 链接

**Files:**
- Create: `cdn/static/web/launcher/coros.html`（~50 行）
- Modify: `cdn/static/web/launcher/settings.html`（在第 16 行后插入一行）

- [ ] **Step 1: 写 coros.html**

参考 `cdn/static/web/launcher/garmin.html`（已存在于仓库），用 Write 创建 `coros.html`：

```html
{% extends "./layout.html" %}
{% block content %}
  <h1><div class="text-shadow">COROS credentials</div></h1>
  {% if username != "zoffline" %}
    <h4 class="text-shadow">Logged in as {{ username }}</h4>
  {% endif %}
  <div class="row">
    <div class="col-md-12">
      <a href="{{ url_for('settings', username=username) }}" class="btn btn-sm btn-secondary">Back</a>
      {% if uname or passw %}
        <a href="/delete/coros_credentials.bin" class="btn btn-sm btn-danger">Remove credentials</a>
      {% endif %}
    </div>
  </div>
  <div class="row">
    <div class="col-sm-6 col-md-5 top-buffer">
      <form id="coros" action="{{ url_for('coros', username=username) }}" method="post">
        <div class="row">
          <div class="col-md-12">
            <label class="col-form-label col-form-label-sm text-shadow">COROS email</label>
            <input type="text" id="username" name="username" value="{{ uname }}" class="form-control form-control-sm">
          </div>
          <div class="col-md-12">
            <label class="col-form-label col-form-label-sm text-shadow">COROS password</label>
            <input type="password" id="password" name="password" value="{{ passw }}" class="form-control form-control-sm">
          </div>
        </div>
        <div class="row">
          <div class="col-md-12 top-buffer">
            <input type="submit" value="Submit" class="btn btn-sm btn-light">
          </div>
        </div>
      </form>
      {% with messages = get_flashed_messages() %}
        {% if messages %}
          <ul class="list-group top-buffer">
          {% for message in messages %}
            <li class="list-group-item py-2">
              <div class="text-shadow">{{ message }}</div>
            </li>
          {% endfor %}
          </ul>
        {% endif %}
      {% endwith %}
    </div>
  </div>
{% endblock %}
```

- [ ] **Step 2: 在 settings.html 加 COROS 链接**

读 `cdn/static/web/launcher/settings.html`，找到这两行（行 15-16）：

```html
      <a href="{{ url_for('strava', username=username) }}" class="btn btn-sm btn-secondary">Strava</a>
      <a href="{{ url_for('garmin', username=username) }}" class="btn btn-sm btn-secondary">Garmin</a>
```

用 Edit 在 Garmin 链接后追加 COROS：

```
old_string:
      <a href="{{ url_for('strava', username=username) }}" class="btn btn-sm btn-secondary">Strava</a>
      <a href="{{ url_for('garmin', username=username) }}" class="btn btn-sm btn-secondary">Garmin</a>

new_string:
      <a href="{{ url_for('strava', username=username) }}" class="btn btn-sm btn-secondary">Strava</a>
      <a href="{{ url_for('garmin', username=username) }}" class="btn btn-sm btn-secondary">Garmin</a>
      <a href="{{ url_for('coros', username=username) }}" class="btn btn-sm btn-secondary">COROS</a>

replace_all: false
```

- [ ] **Step 3: 启动 Flask 路由层验证模板能找到**

```bash
python -c "
import zwift_offline
with zwift_offline.app.test_request_context():
    from flask import render_template
    html = render_template('coros.html', username='testuser', uname='', passw='')
    assert 'COROS credentials' in html
    assert 'COROS email' in html
    print('template renders ok')
"
```

预期：`template renders ok`

- [ ] **Step 4: Commit**

```bash
git add cdn/static/web/launcher/coros.html cdn/static/web/launcher/settings.html
git commit -m "Add COROS launcher settings page and link from settings"
```

---

## Task 6: 在 activity_uploads() 末尾追加 coros_upload 调用

**Files:**
- Modify: `zwift_offline.py`（在 2374 行 `zwift_upload` 调用后追加 1 行）

- [ ] **Step 1: 添加调用**

用 Edit 工具（按上下文锚点定位，避免行号漂移）：

```
old_string:
def activity_uploads(player_id, activity):
    strava_upload(player_id, activity)
    garmin_upload(player_id, activity)
    runalyze_upload(player_id, activity)
    intervals_upload(player_id, activity)
    zwift_upload(player_id, activity)

new_string:
def activity_uploads(player_id, activity):
    strava_upload(player_id, activity)
    garmin_upload(player_id, activity)
    runalyze_upload(player_id, activity)
    intervals_upload(player_id, activity)
    zwift_upload(player_id, activity)
    coros_upload(player_id, activity)

replace_all: false
```

- [ ] **Step 2: 验证 Python 语法和函数引用**

```bash
python -c "
import zwift_offline
import inspect
src = inspect.getsource(zwift_offline.activity_uploads)
assert 'coros_upload' in src
print('activity_uploads now calls coros_upload')
"
```

预期：
```
activity_uploads now calls coros_upload
```

- [ ] **Step 3: Commit**

```bash
git add zwift_offline.py
git commit -m "Wire coros_upload into activity_uploads()"
```

---

## Task 7: 端到端 smoke test

**Files:** 无（验证步骤）

- [ ] **Step 1: 启动 standalone.py**

```bash
sudo ./standalone.py 2>&1 | head -40
```

预期：无 `ImportError`、无 `NameError`、无 `SyntaxError`。看到 `running on http://0.0.0.0:80`（或 443，看配置）即代表服务起来了。

如果失败：回查最近 6 个 commit 的 Edit 改动。

- [ ] **Step 2: 通过浏览器配置 COROS 凭据**

1. 浏览器打开 `http://localhost/settings/<你的用户名>/`
2. 应看到新增的 **COROS** 按钮
3. 点击进入 `/coros/<username>/` 页
4. 看到 email + password 表单
5. 填入真实 COROS 凭据 → 提交
6. 页面应 flash "Credentials saved."

- [ ] **Step 3: 验证凭据加密存储**

```bash
ls -la storage/*/coros_credentials.bin
```

预期：每个有 COROS 凭据的玩家目录下都有这个文件（权限 `-rw-------`）。

- [ ] **Step 4: 触发一次活动保存**

通过 Zwift 客户端完成一次短途骑行并保存，或通过 REST API 模拟：

```bash
# 用 curl 模拟活动保存（player_id 和 token 从 storage/<id>/zwift_credentials.bin 读取）
# 具体 payload 参考 zwift_offline.py 里的 /api/profiles/<id>/activities 路由定义
```

或者最简单：在 Zwift 客户端里骑一圈。

- [ ] **Step 5: 看日志确认 COROS 上传尝试**

```bash
# standalone.py 输出 / 日志文件
grep -i "COROS" /var/log/zwift-offline.log 2>/dev/null || \
  grep -i "COROS" <(sudo ./standalone.py 2>&1) | head -10
```

预期：看到这些日志之一（成功或失败都算"调用过"）：
- `COROS login ok (regionId=2, userId=...)`（成功）
- `COROS upload failed. No internet? ...`（失败但调用过）

**注意**：COROS 端点可能因为地区、账号、API 变化而失败——这是参考项目的已知风险（设计文档第 9 节）。失败不阻塞功能（fire-and-forget），调通后即可投入使用。

- [ ] **Step 6: (可选) 端到端 commit**

如果测试中发现并修复了 bug：

```bash
git add -A
git commit -m "Fix COROS upload smoke-test issues"
```

---

## 完成检查清单

- [ ] 6 个 commit 都已 push（baseline 之后）
- [ ] `python -c "import zwift_offline"` 干净通过
- [ ] 启动器 → Settings → COROS 页可见、表单可保存凭据
- [ ] 活动保存时日志里有 "COROS" 字样
- [ ] CHANGELOG.md 可选加一行（如果项目维护者期望）

## 风险与回退

- **COROS API 端点 / 字段名可能和参考项目不一致**——Task 2 的 `coros_client.py` 是**唯一**需要改的文件，常见调点：
  - `login()` 的 `LOGIN_URL`（可能不是 `teamcnapi`）
  - 登录 body 字段名（`account` vs `email`、`pwd` 是否需要小写）
  - 登录响应字段（`result=="0000"` vs `code==0`）
  - import API 的 multipart 字段名（`jsonParameter` vs 直接 JSON body）
  - import API 鉴权 header（`accesstoken` vs `Authorization`）
- **STS sign 是 COROS 服务端验证的硬编码 magic 字符串**——如果 COROS 改 sign（罕见），Task 1 的 `oss/ali_oss_client.py:APP_ID/SIGN` 和 `oss/aws_oss_client.py:APP_ID/SIGNS` 需要更新。
- **AWS S3 `coros-s3` bucket 的 sign 标记为 TODO**（参考项目只展示了 `eu-coros` 的 sign）——Task 7 烟雾测试时需要抓包确认。
- **阿里云 OSS / AWS S3 SDK 装包失败时**，`oss/ali_oss_client.py` / `oss/aws_oss_client.py` 报 `ModuleNotFoundError`——可以临时注释掉对应 import 让单边（只国际 / 只国内）工作。
- **整个 COROS 集成可以一次性 `git revert <last 6 commits>` 撤回**。
