# COROS 活动同步 - 设计文档

**日期**：2026-06-05
**状态**：已批准（待用户 review）

## 1. 目标

在 zwift-offline 中增加 COROS 平台作为活动上传目标。Zwift 客户端结束骑行后，运动数据除了已上传到 Strava / Garmin / Runalyze / Intervals.icu / Zwift，还会同时推送到 COROS。

**约束**：
- 不破坏现有项目代码
- 不修改参考项目 `/Users/mac/Code/pyCode/garmin-sync-coros/`
- 遵循现有 Strava / Garmin 集成模式
- 一次 commit 一个关键步骤

## 2. 架构总览

照搬现有 Strava / Garmin 的"独立模块 + 薄包装 + 启动器设置页"模式。

**新增文件**：
- `coros_client.py`（项目根）— `CorosClient` 类，封装 login / 拿 STS / 上传 OSS / 调 COROS 导入 API
- `oss/__init__.py`（空）
- `oss/ali_oss_client.py` — 阿里云 OSS 上传（region 2）
- `oss/aws_oss_client.py` — AWS S3 上传（region 1 / 3）
- `cdn/static/web/launcher/coros.html` — 启动器设置页模板

**修改文件**：
- `zwift_offline.py` — 新增 `coros_upload(player_id, activity)` 函数 + `/coros/<username>/` 路由 + 在 `activity_uploads()` 末尾追加一行
- `cdn/static/web/launcher/settings.html` — 增加 COROS 链接
- `requirements.txt` — 追加 `oss2`、`boto3`、`alibabacloud_tea_openapi`（沿用参考项目锁定的版本）

**不动**：
- 数据库 schema（`Activity` 表保持原状——和 Strava / Garmin 一致的"fire and forget"语义）
- 现有 Strava / Garmin 相关代码

## 3. 模块 API 设计

### 3.1 `coros_client.py`（项目根）

按 `discord_bot.py` / `online_sync.py` 的"一个集成 = 一个根级模块"惯例放置。**延迟导入**：只在 `coros_upload()` 实际调用时 import，和 `garth` 一致。

```python
import hashlib
import logging

import requests

logger = logging.getLogger(__name__)

REGION_CONFIG = {
    1: {"team_api": "https://teamapi.coros.com",   "service": "aws"},
    2: {"team_api": "https://teamcnapi.coros.com", "service": "aliyun"},
    3: {"team_api": "https://teamapi.coros.com",   "service": "aws"},
}

STS_ENDPOINT = "https://faq.coros.com/openapi/oss/sts"


class CorosClient:
    def __init__(self):
        self.access_token = None
        self.user_id = None
        self.region_id = None
        self.team_api = None

    def login(self, email: str, password_md5: str) -> None:
        """email + MD5(password) 登录，成功后填充 self.access_token / user_id / region_id

        COROS POST {team_api}/account/login, body={email, pwd: password_md5}
        响应字段：accessToken, userId, regionId"""

    def upload_activity(self, fit_bytes: bytes, fit_filename: str, fit_md5: str) -> None:
        """3 步：拿 STS → 传 OSS → 调 COROS 导入 API"""

    def _fetch_sts(self) -> dict:
        """调 STS_ENDPOINT 拿临时 OSS 凭证

        返回 dict 包含：AccessKeyId / AccessKeySecret / SecurityToken / Expiration / Bucket / Endpoint / Region
        所有 HTTP 调用 timeout=30s"""

    def _upload_to_oss(self, fit_bytes: bytes, sts: dict, fit_filename: str) -> str:
        """按 sts['Region'] 选 ali/aws 客户端（注意：是 STS 返回的 region，不是 regionId）

        客户端接收 sts 全字段，自行拼 endpoint + bucket。
        返回 OSS 对象 key（导入 API 要用）"""

    def _call_import_api(self, oss_key: str, fit_md5: str, fit_filename: str, size: int) -> None:
        """POST {team_api}/activity/fit/import

        body: {ossObject: oss_key, md5: fit_md5, fileName: fit_filename, size: size}"""
```

**关键决策**：
- `login()` 入参是 `password_md5`（**已散列密码**），由调用方在 `zwift_offline.py` 里用 `hashlib.md5(password.encode()).hexdigest()` 算好，**不**把明文密码透传到 `coros_client.py`
- `upload_activity()` 入参是 `fit_bytes: bytes`（不是 `BytesIO`）
- **不抛异常上抛**：参考 `garmin_upload` 的风格，`coros_client.py` 内部把异常转成 `logger.warning(...)`，不向 `zwift_offline.py` 抛
- 失败返回 `None`；成功返回 `{"activity_id": "..."}`

### 3.2 `oss/` 子包

`oss/__init__.py`：空文件。

`oss/ali_oss_client.py`：~80 行，用 `oss2` 库上传到阿里云 OSS（region 2）。`print` 换成 `logger.debug`。

`oss/aws_oss_client.py`：~80 行，用 `boto3` 客户端上传到 AWS S3（region 1 / 3）。

**两个客户端暴露统一接口**：
```python
def upload(sts: dict, fit_bytes: bytes, key: str) -> str:
    """sts 包含 AccessKeyId/Secret/Token/Bucket/Endpoint；返回 OSS 对象 key"""
```

**为什么单独建子包**：
- 两个 SDK（`oss2`、`boto3`）导入符号命名空间冲突（OSS / S3 各自异常类不同），分文件隔离更清晰
- 后续如要支持 Google Cloud Storage 有现成目录

### 3.3 `zwift_offline.py` 中的 `coros_upload()`

**位置**：`zwift_offline.py` 中 `garmin_upload()` 紧后面（约 2262 行），保持 Strava / Garmin / Runalyze / Intervals / Zwift 五个上传函数的"同区域"原则。

**签名**和 `garmin_upload` 一字不差：

```python
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

    if creds_file.endswith('.bin'):
        email, password = decrypt_credentials(creds_file)
    else:
        with open(creds_file, 'r') as f:
            email, password = f.read().strip().split('\n')

    try:
        password_md5 = hashlib.md5(password.encode()).hexdigest()
        client = coros_client.CorosClient()
        client.login(email, password_md5)
        client.upload_activity(activity.fit, activity.fit_filename)
    except BaseException as exc:
        logger.warning("COROS upload failed. No internet? %s" % repr(exc))
        return
```

**和 `garmin_upload` 的关键差异**：
- 凭据是 email + password 两段
- COROS 客户端需要 MD5 散列密码
- 凭据文件命名 `coros_credentials`（和 `garmin_credentials` 对称）

### 3.4 `activity_uploads()` 末尾追加一行

```python
def activity_uploads(player_id, activity):
    strava_upload(player_id, activity)
    garmin_upload(player_id, activity)
    runalyze_upload(player_id, activity)
    intervals_upload(player_id, activity)
    zwift_upload(player_id, activity)
    coros_upload(player_id, activity)   # 新增
```

**顺序说明**：放在最后——和 Strava / Garmin 完全独立，任意一个失败不影响其他。

## 4. 启动器 UI

### 4.1 路由 `GET/POST /coros/<username>/`

照搬 `garmin` 路由（`zwift_offline.py` 第 891-903 行）的结构：

```python
@app.route('/coros/<username>/', methods=['GET', 'POST'])
def coros(username):
    profile = get_profile(username)
    if not profile:
        return redirect(url_for('login'))
    if request.method == 'POST':
        coros_username = request.form.get('username', '')
        coros_password = request.form.get('password', '')
        if not coros_username or not coros_password:
            flash('Both COROS email and password are required')
        else:
            creds_file = '%s/%s/coros_credentials' % (STORAGE_DIR, profile.id)
            encrypt_credentials(creds_file, '%s\n%s' % (coros_username, coros_password))
            flash('COROS credentials saved')
        return redirect(url_for('coros', username=username))
    creds_file = '%s/%s/coros_credentials.bin' % (STORAGE_DIR, profile.id)
    uname = passw = ''
    if os.path.exists(creds_file):
        uname, passw = decrypt_credentials(creds_file)
    return render_template('coros.html', username=username, uname=uname, passw=passw)
```

### 4.2 模板 `cdn/static/web/launcher/coros.html`

照搬 `garmin.html` 的结构，字段标签改为 COROS：

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

### 4.3 `settings.html` 添加链接

参考现有 Garmin / Strava 链接的写法（btn 形式），在第 16 行后面加一个 COROS 入口：

```html
<a href="{{ url_for('coros', username=username) }}" class="btn btn-sm btn-secondary">COROS</a>
```

### 4.4 凭据删除

`/delete/coros_credentials.bin` 走现有 `/delete/<filename>` 路由——只要 `coros_credentials.bin` 在白名单里即可。和 `garmin_credentials.bin` 完全对称。

## 5. 数据流

```
Zwift 客户端结束骑行
  ↓
  HTTP POST /api/profiles/<id>/activities 收到活动数据（已有流程）
  ↓
  Activity 写入 SQLite（已有）
  ↓
  activity_uploads(player_id, activity)  ← 追加 coros_upload 调用
  ↓
  coros_upload(player_id, activity):
    1. 读 storage/<id>/coros_credentials.bin（无则 return + log）
    2. decrypt_credentials() → email, password
    3. password_md5 = hashlib.md5(password.encode()).hexdigest()
    4. fit_md5 = hashlib.md5(activity.fit).hexdigest()    # 给 import API 用
    5. client = CorosClient()
    6. client.login(email, password_md5)
       → POST {team_api}/account/login
       → 拿到 accessToken / userId / regionId（存入 self.*）
    7. client.upload_activity(activity.fit, activity.fit_filename, fit_md5)
       → 7a. client._fetch_sts()
              → GET https://faq.coros.com/openapi/oss/sts
              → 拿到 STS 临时凭证（含 Bucket / Endpoint / Region）
       → 7b. client._upload_to_oss(fit_bytes, sts, fit_filename)
              → 按 sts['Region'] 选 AliOSSClient.upload / AWSS3Client.upload
              → 上传 activity.fit 到 OSS
              → 返回 oss_key
       → 7c. client._call_import_api(oss_key, fit_md5, fit_filename, len(fit_bytes))
              → POST {team_api}/activity/fit/import
    8. 任意步骤失败 → logger.warning(...) → return（不修改 Activity 表）
```

## 6. 错误处理

- **`ImportError`**（依赖未装）→ `logger.warning(...)` + return（活动已保存，不影响 Zwift 客户端）
- **网络错误**（requests.exceptions.RequestException）→ `logger.warning("COROS upload failed. No internet? %s" % repr(exc))` + return
- **登录失败**（COROS 返回非 0）→ `logger.warning("COROS login failed: %s" % err_msg)` + return
- **STS 获取失败** → 同上
- **OSS 上传失败** → 同上
- **导入 API 失败** → 同上

**核心原则**：COROS 同步失败**不影响** SQLite 写入、**不影响** Zwift 客户端下一次活动保存。和 Strava / Garmin 一致。

## 7. 配置 UX

- 启动器 → Settings → COROS → 输入 email + password → 提交
- 凭据用项目自带的 `encrypt_credentials` 加密后存到 `storage/<player_id>/coros_credentials.bin`
- 「Remove credentials」按钮：调用 `/delete/coros_credentials.bin`

## 8. 实施步骤（每次 commit）

| 步骤 | 内容 | Commit 消息 |
|------|------|-------------|
| 0 | Baseline（已完成） | `Baseline: initial state before COROS activity sync work` |
| 1 | `oss/__init__.py` + `oss/ali_oss_client.py` + `oss/aws_oss_client.py` | `Add OSS clients (Ali + AWS) for COROS upload` |
| 2 | `coros_client.py` | `Add CorosClient (login, STS, upload, import API)` |
| 3 | `requirements.txt` 增加依赖 | `Pin COROS SDK deps (oss2, boto3, alibabacloud_tea_openapi)` |
| 4 | `zwift_offline.py`：coros_upload 函数 + 路由 | `Add coros_upload() + /coros/<username>/ route` |
| 5 | `cdn/static/web/launcher/coros.html` + settings.html 加链接 | `Add COROS launcher settings page and link from settings` |
| 6 | `activity_uploads()` 末尾追加 coros_upload 调用 | `Wire coros_upload into activity_uploads()` |

每步独立可运行（除依赖外不互相阻塞），方便中途回滚。

## 9. 风险与后续

- **依赖体积**：`oss2 + boto3 + alibabacloud-tea` 是大依赖（合计 ~30MB）。和 `garth`（Garmin）量级相当，可接受。
- **COROS API 稳定性**：COROS 没有公开 API 文档，`garmin-sync-coros` 参考项目通过逆向工程实现。**接口可能变化**，建议把 `coros_client.py` 集中、所有 COROS 端点 + 参数都集中在这一个文件，方便以后调整。
- **STS 凭据有效期**：COROS 临时凭证有效期通常 1 小时。每次上传重新拿一次即可（已经在 `upload_activity` 里这么做）。
- **多区域**：海外（region 1/3）用 AWS S3，国内（region 2）用阿里云 OSS。参考项目已经验证。
- **后续**（不在本次范围）：
  - 在 Activity 模型加 `coros_upload_id` 字段记录上传结果（避免重复上传）
  - 启动器首页 dashboard 显示 COROS 上传状态
  - 多 token 缓存（避免每次都重新登录）
  - 重试 / 退避策略

## 10. 参考

- `/Users/mac/Code/pyCode/garmin-sync-coros/scripts/coros/coros_client.py` — 登录和导入 API
- `/Users/mac/Code/pyCode/garmin-sync-coros/scripts/oss/ali_oss_client.py` — 阿里云 OSS 上传
- `/Users/mac/Code/pyCode/garmin-sync-coros/scripts/oss/aws_oss_client.py` — AWS S3 上传
- `/Users/mac/Code/pyCode/garmin-sync-coros/scripts/coros/sts_config.py` — 区域和 STS 配置
- `zwift_offline.py:2215` — `garmin_upload()` 函数（参考模板）
- `zwift_offline.py:891` — `/garmin/<username>/` 路由（参考模板）
- `cdn/static/web/launcher/garmin.html` — 设置页模板（参考模板）
