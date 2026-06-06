"""Regression tests for standalone.spec packaging.

The COROS upload path is wired in via a runtime `import coros_client` inside
`zwift_offline.coros_upload()`. PyInstaller's static analysis does NOT scan
function bodies, so without an explicit `hiddenimports` entry, the bundled
`standalone.exe` will fail with `ModuleNotFoundError: No module named
'coros_client'` the first time the user finishes a ride and we try to upload.

These tests catch the regression where someone resets `hiddenimports=[]` in
standalone.spec — the spec file lives next to the source and is edited by
hand, so we lock it down with a test.
"""

import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_PATH = os.path.join(REPO_ROOT, 'standalone.spec')


def _read_spec():
    with open(SPEC_PATH) as f:
        return f.read()


def test_standalone_spec_exists():
    assert os.path.isfile(SPEC_PATH), "standalone.spec not found at repo root"


def test_hiddenimports_includes_coros_client():
    """The whole point of the runtime import in coros_upload() — if pyinstaller
    doesn't ship coros_client in the bundle, the .exe silently skips every
    upload (the ImportError gets caught and logged as a WARNING)."""
    text = _read_spec()
    # Find the hiddenimports=[ ... ] block — it's a Python list literal, so
    # we look for the opening bracket after 'hiddenimports=' and the matching
    # closing ']' on its own (the list is multiline and indented).
    m = re.search(r'hiddenimports\s*=\s*\[(.*?)\]', text, flags=re.DOTALL)
    assert m, "standalone.spec has no hiddenimports=[...] block"
    block = m.group(1)
    # The block is comma-separated; normalize whitespace.
    entries = {e.strip().strip("'\"") for e in block.split(',') if e.strip()}
    assert 'coros_client' in entries, (
        "standalone.spec is missing 'coros_client' in hiddenimports. "
        "Without it, the pyinstaller-bundled standalone.exe will fail with "
        "ModuleNotFoundError on every COROS upload."
    )


def test_hiddenimports_includes_oss_submodules():
    """coros_client.py does `from oss import ali_oss_client, aws_oss_client`
    at module top — PyInstaller must bundle both submodules and the parent
    `oss` package marker."""
    text = _read_spec()
    m = re.search(r'hiddenimports\s*=\s*\[(.*?)\]', text, flags=re.DOTALL)
    assert m
    entries = {e.strip().strip("'\"") for e in m.group(1).split(',') if e.strip()}
    for needed in ('oss', 'oss.ali_oss_client', 'oss.aws_oss_client'):
        assert needed in entries, f"standalone.spec hiddenimports missing {needed!r}"


def test_hiddenimports_includes_oss2_and_boto3():
    """oss/ali_oss_client.py imports oss2; oss/aws_oss_client.py imports boto3.
    Both are optional in dev (mocked in tests) but mandatory at runtime."""
    text = _read_spec()
    m = re.search(r'hiddenimports\s*=\s*\[(.*?)\]', text, flags=re.DOTALL)
    assert m
    entries = {e.strip().strip("'\"") for e in m.group(1).split(',') if e.strip()}
    for needed in ('oss2', 'boto3', 'boto3.s3', 'boto3.s3.transfer'):
        assert needed in entries, f"standalone.spec hiddenimports missing {needed!r}"


def test_hiddenimports_block_is_not_empty():
    """Defensive: an empty `hiddenimports=[]` MUST not silently regress.
    An empty list is exactly the bug that caused the runtime ModuleNotFoundError
    in the bundled .exe."""
    text = _read_spec()
    m = re.search(r'hiddenimports\s*=\s*\[(.*?)\]', text, flags=re.DOTALL)
    assert m
    block = m.group(1)
    entries = [e.strip().strip("'\"") for e in block.split(',') if e.strip()]
    assert len(entries) >= 5, (
        f"hiddenimports looks suspiciously short ({len(entries)} entries). "
        "Did someone reset it to []?"
    )
