import hashlib
import json
from pathlib import Path

import pytest

from signup031.archive import load_archive


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / 'archive'
    root.mkdir()
    har = root / 'resources.har'
    har.write_text(json.dumps({'log': {'entries': []}}), encoding='utf-8')
    manifest = {
        'archive_version': 1, 'status': 'recorded', 'execution_id': 'unit',
        'navigation_url': 'http://example.test/',
        'procedure': {'name': 'SIGNUP-031', 'initial_length': 128, 'append': 'Z',
                      'selector': '#password', 'label_pattern': None},
        'observed': {'initial_length': 128, 'after_extra_length': 129},
        'resources': [{'path': 'resources.har', 'sha256': hashlib.sha256(har.read_bytes()).hexdigest()}],
    }
    (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return root, manifest


@pytest.mark.parametrize('path', ['../outside.har', 'C:/outside.har'])
def test_resource_path_escape_is_rejected(archive, path):
    root, manifest = archive
    manifest['resources'][0]['path'] = path
    (root / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='path'):
        load_archive(root)


def test_modified_har_is_rejected_before_browser(archive):
    root, _ = archive
    (root / 'resources.har').write_text('{}')
    with pytest.raises(ValueError, match='hash mismatch'):
        load_archive(root)


def test_missing_har_is_reported(archive):
    root, _ = archive
    (root / 'resources.har').unlink()
    with pytest.raises(FileNotFoundError):
        load_archive(root)


def test_embedded_external_body_is_rejected_even_with_matching_hash(archive):
    root, manifest = archive
    har = root / 'resources.har'
    har.write_text(json.dumps({'log': {'entries': [{'response': {
        'content': {'_file': '../../private.txt'}}}]}}))
    manifest['resources'][0]['sha256'] = hashlib.sha256(har.read_bytes()).hexdigest()
    (root / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='external HAR'):
        load_archive(root)


def test_incomplete_manifest_does_not_launch(archive):
    root, manifest = archive
    manifest['status'] = 'failed'
    manifest['reason'] = 'capture failed'
    (root / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='incomplete'):
        load_archive(root)
