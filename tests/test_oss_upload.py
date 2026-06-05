"""Unit tests for oss.ali_oss_client.upload and oss.aws_oss_client.upload.

The OSS SDKs (`oss2`, `boto3`) are mocked. We verify:
  - the right STS-fetching path is taken
  - the right bucket / region is passed
  - the multipart upload is invoked
  - temp files are cleaned up
"""

import os
from unittest.mock import MagicMock, patch

import pytest


# --- ali_oss_client.upload ----------------------------------------------------

def test_ali_upload_invokes_multipart(monkeypatch, tmp_path):
    from oss import ali_oss_client
    import oss2

    # Mock fetch_sts to avoid HTTP
    monkeypatch.setattr(
        ali_oss_client, 'fetch_sts',
        lambda: {'AccessKeyId': 'a', 'AccessKeySecret': 'b', 'SecurityToken': 'c'},
    )

    # Mock the Bucket class to capture multipart call sequence
    init_resp = MagicMock()
    init_resp.upload_id = 'fake-upload-id'
    part_result = MagicMock()
    part_result.etag = 'etag-1'

    bucket_mock = MagicMock()
    bucket_mock.init_multipart_upload.return_value = init_resp
    bucket_mock.upload_part.return_value = part_result

    with patch.object(oss2, 'Bucket', return_value=bucket_mock):
        # Provide 7 MB of data so we definitely get >1 part
        data = b'x' * (7 * 1024 * 1024)
        key = ali_oss_client.upload(data, 'fit_zip/abc.fit')

    assert key == 'fit_zip/abc.fit'
    bucket_mock.init_multipart_upload.assert_called_once_with('fit_zip/abc.fit')
    assert bucket_mock.upload_part.call_count >= 2
    bucket_mock.complete_multipart_upload.assert_called_once()
    # Verify each upload_part received a SizedFileAdapter
    for call in bucket_mock.upload_part.call_args_list:
        args = call.args
        assert args[0] == 'fit_zip/abc.fit'
        assert args[1] == 'fake-upload-id'
        assert isinstance(args[2], int)  # part number
        # 4th positional = SizedFileAdapter instance
        assert 'SizedFileAdapter' in type(args[3]).__name__


def test_ali_upload_cleans_up_temp_file(monkeypatch):
    from oss import ali_oss_client
    import oss2

    monkeypatch.setattr(ali_oss_client, 'fetch_sts', lambda: {'AccessKeyId': 'a', 'AccessKeySecret': 'b', 'SecurityToken': 'c'})
    init_resp = MagicMock(); init_resp.upload_id = 'uid'
    part_result = MagicMock(); part_result.etag = 'e'
    bucket_mock = MagicMock()
    bucket_mock.init_multipart_upload.return_value = init_resp
    bucket_mock.upload_part.return_value = part_result
    with patch.object(oss2, 'Bucket', return_value=bucket_mock):
        ali_oss_client.upload(b'fit-bytes', 'k1')

    # No temp file left behind in the system temp dir from this test
    leftovers = [p for p in os.listdir('/tmp')
                 if p.endswith('.fit') and os.stat('/tmp/' + p).st_size == len(b'fit-bytes')]
    # The file may have been overwritten by other tests; we just check the
    # *most recent* upload was cleaned. Easiest check: scan for files created
    # within the last 1 second with our specific size.
    import time
    now = time.time()
    recent = [p for p in os.listdir('/tmp')
              if p.endswith('.fit')
              and (now - os.stat('/tmp/' + p).st_mtime) < 2
              and os.stat('/tmp/' + p).st_size == len(b'fit-bytes')]
    assert not recent, f"temp file leaked: {recent}"


def test_ali_upload_single_part_data(monkeypatch):
    """A short payload stays single-part. Verify we still complete the upload."""
    from oss import ali_oss_client
    import oss2

    monkeypatch.setattr(ali_oss_client, 'fetch_sts', lambda: {'AccessKeyId': 'a', 'AccessKeySecret': 'b', 'SecurityToken': 'c'})
    init_resp = MagicMock(); init_resp.upload_id = 'uid'
    part_result = MagicMock(); part_result.etag = 'e'
    bucket_mock = MagicMock()
    bucket_mock.init_multipart_upload.return_value = init_resp
    bucket_mock.upload_part.return_value = part_result
    with patch.object(oss2, 'Bucket', return_value=bucket_mock):
        ali_oss_client.upload(b'short', 'k2')

    assert bucket_mock.upload_part.call_count == 1
    bucket_mock.complete_multipart_upload.assert_called_once()


# --- aws_oss_client.upload ----------------------------------------------------

def test_aws_upload_default_bucket_is_eu_coros(monkeypatch):
    from oss import aws_oss_client
    monkeypatch.setattr(aws_oss_client, 'fetch_sts', lambda b: {'AccessKeyId': 'a', 'AccessKeySecret': 'b', 'SecurityToken': 'c'})

    captured = {}
    fake_client = MagicMock()
    def fake_boto_client(*args, **kwargs):
        captured['kwargs'] = kwargs
        return fake_client
    monkeypatch.setattr(aws_oss_client.boto3, 'client', fake_boto_client)
    aws_oss_client.upload(b'data', 'k')
    assert captured['kwargs']['region_name'] == 'eu-central-1'
    fake_client.upload_file.assert_called_once()
    # Bucket should be the default eu-coros
    call = fake_client.upload_file.call_args
    assert call.kwargs['Bucket'] == 'eu-coros'


def test_aws_upload_coros_s3_uses_us_west_2(monkeypatch):
    from oss import aws_oss_client
    monkeypatch.setattr(aws_oss_client, 'fetch_sts', lambda b: {'AccessKeyId': 'a', 'AccessKeySecret': 'b', 'SecurityToken': 'c'})

    captured = {}
    fake_client = MagicMock()
    def fake_boto_client(*args, **kwargs):
        captured['kwargs'] = kwargs
        return fake_client
    monkeypatch.setattr(aws_oss_client.boto3, 'client', fake_boto_client)
    aws_oss_client.upload(b'data', 'k', bucket='coros-s3')
    assert captured['kwargs']['region_name'] == 'us-west-2'
    assert fake_client.upload_file.call_args.kwargs['Bucket'] == 'coros-s3'


def test_aws_upload_explicit_region_overrides_default(monkeypatch):
    from oss import aws_oss_client
    monkeypatch.setattr(aws_oss_client, 'fetch_sts', lambda b: {'AccessKeyId': 'a', 'AccessKeySecret': 'b', 'SecurityToken': 'c'})
    captured = {}
    fake_client = MagicMock()
    def fake_boto_client(*args, **kwargs):
        captured['kwargs'] = kwargs
        return fake_client
    monkeypatch.setattr(aws_oss_client.boto3, 'client', fake_boto_client)
    aws_oss_client.upload(b'data', 'k', bucket='coros-s3', region='ap-southeast-1')
    assert captured['kwargs']['region_name'] == 'ap-southeast-1'


def test_aws_upload_uses_transfer_config(monkeypatch):
    """TransferConfig is what triggers multipart; verify it's set with the
    5 MB threshold that matches the ali client."""
    from oss import aws_oss_client
    monkeypatch.setattr(aws_oss_client, 'fetch_sts', lambda b: {'AccessKeyId': 'a', 'AccessKeySecret': 'b', 'SecurityToken': 'c'})

    captured_config = {}
    fake_client = MagicMock()
    def fake_boto_client(*args, **kwargs):
        return fake_client
    monkeypatch.setattr(aws_oss_client.boto3, 'client', fake_boto_client)

    real_TC = aws_oss_client.TransferConfig
    def fake_TC(*args, **kwargs):
        captured_config.update(kwargs)
        return MagicMock()
    monkeypatch.setattr(aws_oss_client, 'TransferConfig', fake_TC)
    aws_oss_client.upload(b'data', 'k')
    assert captured_config.get('multipart_threshold') == 5 * 1024 * 1024
    assert captured_config.get('multipart_chunksize') == 5 * 1024 * 1024
