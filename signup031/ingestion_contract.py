"""Versioned transport around immutable original evidence, with bounded file declarations."""
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tempfile

MAX_EVIDENCE_BYTES = 2_000_000
MAX_ENVELOPE_BYTES = 3_000_000
MAX_ATTACHMENT_BYTES = 80_000_000
MAX_TOTAL_BYTES = 100_000_000
MAX_FILES = 16


def digest(data):
    return hashlib.sha256(data).hexdigest()


def relative_path(value):
    if not isinstance(value, str) or len(value) > 240 or not value:
        raise ValueError('invalid attachment path')
    parts = value.split('/')
    if ('\\' in value or ':' in value or PureWindowsPath(value).is_absolute() or
        PurePosixPath(value).is_absolute() or any(part in ('', '.', '..') for part in parts) or
        any(not re.fullmatch(r'[A-Za-z0-9_.-]+', part) for part in parts)):
        raise ValueError('unsafe attachment path')
    suffix = PurePosixPath(value).suffix.lower()
    if suffix not in ('.png', '.har', '.html') and value not in ('archive/manifest.json', 'scenario.json', 'timeline.json','test-context.json'):
        raise ValueError('unsupported attachment type')
    if len(parts) > 3:
        raise ValueError('attachment path too deep')
    return value


def is_link(path):
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError:
        return False


def safe_path(root, relative):
    """Reject links/junctions and escaping paths before both reads and writes."""
    root = Path(root).absolute()
    parts = PurePosixPath(relative).parts
    if not parts or any(part in ('', '.', '..') for part in parts) or ':' in relative or '\\' in relative or PurePosixPath(relative).is_absolute():
        raise ValueError('unsafe owned path')
    candidate = root
    if is_link(candidate): raise ValueError('linked storage root')
    for part in parts:
        candidate = candidate / part
        if is_link(candidate): raise ValueError('linked attachment/storage path')
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError('path escapes storage root')
    return candidate


def string(value, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError('invalid identifier')
    return value


def validate_evidence(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError('evidence exceeds byte limit')
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get('contract_version') not in ('1', '2', '3', '4'):
        raise ValueError('unsupported original evidence contract')
    execution = payload['execution']
    string(execution['id'])
    for key in ('started_at', 'finished_at'):
        if key in execution:
            timestamp = datetime.fromisoformat(string(execution[key]).replace('Z', '+00:00'))
            if timestamp.tzinfo is None: raise ValueError('timestamp requires timezone')
    from signup031.viewer_model import _load_one
    with tempfile.TemporaryDirectory(prefix='qa-evidence-validate-') as temporary:
        path = Path(temporary) / 'evidence.json'; path.write_bytes(raw)
        record = _load_one(path, path.parent)
    if record.business_status in ('incomplete', 'read_error', 'legacy'):
        raise ValueError('invalid original evidence schema')
    if payload['contract_version'] == '1':
        expected = {'passed': 'passed', 'failed': 'failed', 'preparation_failed': 'error'}[record.business_status]
        if payload['result']['pytest']['status'] != expected:
            raise ValueError('original pytest verdict mismatch')
    replay = payload.get('replay', {})
    if 'timeline' in payload:
        from signup031.timeline import validate_declaration
        validate_declaration(payload['timeline'])
    if 'test_context' in payload:
        from signup031.test_context import validate_declaration
        validate_declaration(payload['test_context'])
    if replay.get('manifest') is not None and replay['manifest'] != 'archive/manifest.json':
        raise ValueError('unsupported replay manifest path')
    return payload


def validate_envelope(envelope, *, max_attachment_bytes=MAX_ATTACHMENT_BYTES, max_total_bytes=MAX_TOTAL_BYTES):
    keys = {'envelope_version', 'project_id', 'provider', 'source_run_id', 'attempt', 'test_id',
            'execution_id', 'provenance', 'evidence_b64', 'files'}
    if not isinstance(envelope, dict) or set(envelope) != keys or type(envelope['envelope_version']) is not int or envelope['envelope_version'] != 1:
        raise ValueError('invalid transport envelope')
    for key in ('project_id', 'provider', 'source_run_id', 'execution_id'): string(envelope[key])
    string(envelope['test_id'], 4096)
    if type(envelope['attempt']) is not int or not 1 <= envelope['attempt'] <= 1_000_000:
        raise ValueError('invalid attempt')
    raw = base64.b64decode(envelope['evidence_b64'], validate=True)
    payload = validate_evidence(raw)
    if payload['execution']['id'] != envelope['execution_id'] or (payload.get('tc_id') and payload['tc_id'] != envelope['test_id']):
        raise ValueError('original/source identity mismatch')
    provenance = envelope['provenance']
    if not isinstance(provenance, dict) or len(provenance) > 16:
        raise ValueError('invalid provenance')
    for key, value in provenance.items(): string(key, 64); string(value, 2048)
    files = envelope['files']
    if not isinstance(files, list) or len(files) > MAX_FILES: raise ValueError('too many attachments')
    seen, total = set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256'}: raise ValueError('invalid file declaration')
        name = relative_path(item['path'])
        if name in seen: raise ValueError('duplicate attachment')
        seen.add(name)
        size, sha = item['size'], item['sha256']
        if size is not None and (type(size) is not int or not 0 <= size <= max_attachment_bytes):
            raise ValueError('attachment exceeds byte limit')
        if sha is not None and (not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha)):
            raise ValueError('invalid attachment hash')
        if size is not None and sha is None: raise ValueError('sized attachment requires hash')
        total += size or 0
    if total > max_total_bytes: raise ValueError('total attachment byte limit')
    timeline=payload.get('timeline')
    if timeline:
        item=next((f for f in files if f['path']=='timeline.json'),None)
        if item is None or item['sha256']!=timeline['sha256'] or item['size'] not in (None,timeline['size']):raise ValueError('timeline attachment declaration mismatch')
    elif 'timeline.json' in seen:raise ValueError('undeclared original timeline')
    context=payload.get('test_context')
    if context:
        item=next((f for f in files if f['path']=='test-context.json'),None)
        if item is None or item['sha256']!=context['sha256'] or item['size'] not in (None,context['size']):raise ValueError('test context attachment declaration mismatch')
    elif 'test-context.json' in seen:raise ValueError('undeclared original test context')
    if payload.get('replay', {}).get('status') == 'recorded' and 'archive/manifest.json' not in seen:
        raise ValueError('recorded archive must declare its manifest, including when the file is missing')
    return raw, payload


def validate_manifest(raw, payload, files):
    manifest = json.loads(raw)
    version = manifest['archive_version']
    if type(version) is not int or version not in (1, 2, 3, 4) or manifest['execution_id'] != payload['execution']['id']:
        raise ValueError('manifest execution mismatch')
    if str(version) != payload['contract_version']: raise ValueError('manifest contract mismatch')
    for key in ({2: ('scenario_snapshot',), 3: ('manual_record',), 4: ('selenium_record', 'test_identity')}.get(version, ())):
        if manifest.get(key) != payload.get(key): raise ValueError('manifest original identity mismatch')
    if version == 2 and manifest.get('observed_checks') != payload.get('checks'):
        raise ValueError('manifest observation mismatch')
    inventory = {item['path']: item for item in files}
    if manifest.get('status') == 'recorded':
        resources = manifest.get('resources')
        if not isinstance(resources, list) or len(resources) != 1: raise ValueError('missing HAR declaration')
        for item in resources + (manifest.get('attachments', []) if version == 4 else []):
            name = relative_path('archive/' + item['path'])
            if name not in inventory or inventory[name]['sha256'] != item['sha256']:
                raise ValueError('manifest/file hash declaration mismatch')
        if version == 4 and not any(item['path'] == 'page.html' for item in manifest.get('attachments', [])):
            raise ValueError('missing Selenium DOM declaration')
    return manifest


def build_bundle(folder, project, provider, run, attempt=1, *, test_id=None, provenance=None):
    root = Path(folder).absolute()
    raw = safe_path(root, 'evidence.json').read_bytes()
    payload = validate_evidence(raw)
    names, expected = set(), {}
    if payload.get('timeline'):
        names.add('timeline.json');expected['timeline.json']=payload['timeline']['sha256']
    if payload.get('test_context'):
        names.add('test-context.json');expected['test-context.json']=payload['test_context']['sha256']
    screenshot = payload['evidence']['screenshot']
    if screenshot.get('status') == 'collected':
        path = screenshot.get('path', '')
        # v1 original evidence can contain its original absolute PNG path; copy only its local basename.
        if payload['contract_version'] == '1' and (PureWindowsPath(path).is_absolute() or Path(path).is_absolute()):
            path = PureWindowsPath(path).name
        names.add(relative_path(path))
    if payload.get('replay', {}).get('manifest'):
        names.add('archive/manifest.json')
        manifest_path = safe_path(root, 'archive/manifest.json')
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_bytes())
            for item in manifest.get('resources', []) + manifest.get('attachments', []):
                name = relative_path('archive/' + item['path']); names.add(name); expected[name] = item['sha256']
    if safe_path(root, 'scenario.json').is_file(): names.add('scenario.json')
    contents, files = {}, []
    for name in sorted(names):
        path = safe_path(root, name)
        if path.is_file():
            if path.stat().st_size > MAX_ATTACHMENT_BYTES: raise ValueError('attachment exceeds byte limit')
            content = path.read_bytes(); sha = digest(content)
            if name in expected and sha != expected[name]: raise ValueError('original attachment hash mismatch')
            contents[name] = content
            files.append({'path': name, 'size': len(content), 'sha256': sha})
        else: files.append({'path': name, 'size': None, 'sha256': expected.get(name)})
    envelope = {'envelope_version': 1, 'project_id': project, 'provider': provider, 'source_run_id': run,
                'attempt': attempt, 'test_id': test_id or payload.get('tc_id') or 'manual:' + payload['execution']['id'],
                'execution_id': payload['execution']['id'], 'provenance': provenance or {},
                'evidence_b64': base64.b64encode(raw).decode('ascii'), 'files': files}
    validate_envelope(envelope)
    if 'archive/manifest.json' in contents: validate_manifest(contents['archive/manifest.json'], payload, files)
    if 'timeline.json' in contents:
        from signup031.timeline import validate_bytes
        validate_bytes(contents['timeline.json'],payload['execution']['id'],payload['timeline'])
    if 'test-context.json' in contents:
        from signup031.test_context import validate_bytes
        validate_bytes(contents['test-context.json'],payload['execution']['id'],payload['test_context'])
    return envelope, contents
