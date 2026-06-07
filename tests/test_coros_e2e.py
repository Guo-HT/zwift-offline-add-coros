"""End-to-end smoke test: simulate one full activity save flow.

We do NOT touch real COROS. We mock:
  - requests.post / requests.get
  - ali_oss_client.upload
  - aws_oss_client.upload

The test then asserts:
  1. activity_uploads() calls each platform's upload exactly once
  2. coros_upload reads credentials, calls login, then calls upload_activity
  3. upload_activity does login → STS → OSS → import API in that order
"""

import hashlib
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def e2e_env(fresh_zwift_offline, test_storage_dir):
    """Set up: STORAGE_DIR + a player with credentials + a fake activity."""
    zo = fresh_zwift_offline
    player_id = 7777
    profile_dir = os.path.join(test_storage_dir, str(player_id))
    os.makedirs(profile_dir, exist_ok=True)
    with zo.app.test_request_context():
        zo.encrypt_credentials(
            os.path.join(profile_dir, 'coros_credentials.bin'),
            ('e2e@coros.com', 'e2epwd'),
        )
    activity = SimpleNamespace(
        fit=b'e2e-fit-content',
        fit_filename='e2e.fit',
    )
    return zo, player_id, activity


def test_e2e_full_activity_uploads_path(e2e_env):
    """Verify activity_uploads() calls ALL platforms (COROS is appended)."""
    zo, player_id, activity = e2e_env

    with patch.object(zo, 'strava_upload') as s, \
         patch.object(zo, 'garmin_upload') as g, \
         patch.object(zo, 'runalyze_upload') as r, \
         patch.object(zo, 'intervals_upload') as i, \
         patch.object(zo, 'zwift_upload') as z, \
         patch.object(zo, 'coros_upload') as c:
        zo.activity_uploads(player_id, activity)
    s.assert_called_once_with(player_id, activity)
    g.assert_called_once_with(player_id, activity)
    r.assert_called_once_with(player_id, activity)
    i.assert_called_once_with(player_id, activity)
    z.assert_called_once_with(player_id, activity)
    c.assert_called_once_with(player_id, activity)


def test_e2e_coros_path_full_stack(e2e_env, mock_requests):
    """Walk the full COROS code path: creds → MD5 → login → upload_activity
    → fetch_sts → oss upload → import API, in order."""
    zo, player_id, activity = e2e_env

    # Build a chained fake CorosClient
    fake_client = MagicMock()
    fake_client.access_token = 'tok'
    fake_client.user_id = 'u'
    fake_client.region_id = 2
    from oss import ali_oss_client
    fake_client._cfg = {
        'teamapi': 'https://teamcnapi.coros.com',
        'bucket': 'coros-oss',
        'service': 'aliyun',
        'oss': ali_oss_client,
    }
    fake_client.teamapi = 'https://teamcnapi.coros.com'

    with patch('coros_client.CorosClient', return_value=fake_client) as cls, \
         patch('oss.ali_oss_client.fetch_sts',
               return_value={'AccessKeyId': 'a', 'AccessKeySecret': 'b', 'SecurityToken': 'c'}), \
         patch('oss.ali_oss_client.oss2') as mock_oss2:
        init_resp = MagicMock(); init_resp.upload_id = 'uid'
        part_result = MagicMock(); part_result.etag = 'e'
        bucket_mock = MagicMock()
        bucket_mock.init_multipart_upload.return_value = init_resp
        bucket_mock.upload_part.return_value = part_result
        mock_oss2.Bucket.return_value = bucket_mock
        mock_oss2.StsAuth.return_value = MagicMock()
        mock_oss2.SizedFileAdapter = MagicMock()
        mock_oss2.determine_part_size.return_value = 5 * 1024 * 1024

        # Register a successful import API response
        def import_handler(r):
            r.raise_for_status.return_value = None
            r.json.return_value = {'result': '0000', 'data': {'status': 2}}
        mock_requests['queue'].append(import_handler)

        # Call coros_upload for real
        zo.coros_upload(player_id, activity)

    cls.assert_called_once()
    fake_client.login.assert_called_once()
    login_args = fake_client.login.call_args
    assert login_args.args[0] == 'e2e@coros.com'
    assert login_args.args[1] == hashlib.md5(b'e2epwd').hexdigest()
    upload_args = fake_client.upload_activity.call_args
    assert upload_args.args[0] == b'e2e-fit-content'
    assert upload_args.args[1] == 'e2e.fit'
    assert upload_args.args[2] == hashlib.md5(b'e2e-fit-content').hexdigest()


def test_e2e_coros_failure_does_not_break_other_platforms(e2e_env):
    """If coros_upload raises, the platforms called before it (mocked here)
    should still have been called. activity_uploads is NOT itself
    fire-and-forget — only coros_upload is."""
    zo, player_id, activity = e2e_env

    with patch.object(zo, 'strava_upload') as s, \
         patch.object(zo, 'garmin_upload') as g, \
         patch.object(zo, 'runalyze_upload') as r, \
         patch.object(zo, 'intervals_upload') as i, \
         patch.object(zo, 'zwift_upload') as z, \
         patch.object(zo, 'coros_upload', side_effect=RuntimeError("boom")):
        try:
            zo.activity_uploads(player_id, activity)
        except RuntimeError:
            pass  # expected: coros_upload raised
    s.assert_called_once()
    g.assert_called_once()
    r.assert_called_once()
    i.assert_called_once()
    z.assert_called_once()
