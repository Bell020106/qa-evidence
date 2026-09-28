"""Versioned declarative web steps. Configuration never executes arbitrary code."""
from copy import deepcopy
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from signup031.storage import write_evidence

ACTION_LABELS = {'wait': '요소 대기', 'fill': '입력', 'press': '키 입력', 'click': '클릭'}
CHECK_LABELS = {'input_length': '입력 길이', 'text': '표시 텍스트', 'visible': '요소 표시 여부'}
LOCATOR_LABELS = {'css': 'CSS 선택자', 'label': '접근성 라벨 (정확히)'}


def _object(value, keys, location):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f'{location}: missing or unknown fields')


def _text(value, location, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f'{location}: nonempty text required')
    if len(value) > 10000:
        raise ValueError(f'{location}: text exceeds 10000 characters')


def validate_scenario(config, *, allow_draft=False):
    if not isinstance(config, dict):
        raise ValueError('scenario: object required')
    optional = set(config) & {'source_manual', 'source_ai', 'draft'}
    _object(config, ('version', 'id', 'title', 'url', 'steps', 'checks', *optional), 'scenario')
    draft = config.get('draft', False)
    if 'draft' in config and (draft is not True or not allow_draft):
        raise ValueError('TC 초안: 실행 전에 단계와 기대 결과를 작성하세요')
    if 'source_ai' in config:
        source=config['source_ai']
        _object(source,('provider','requested_model','returned_model','proposal_id','request_sha256','proposal_sha256','review_notes','reviewed'),'source_ai')
        if source['provider'] not in ('openai-responses','local-http-test'):raise ValueError('AI 공급자 형식 오류')
        for key in ('requested_model','returned_model'):
            if not isinstance(source[key],str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,100}',source[key]):raise ValueError('AI 모델 형식 오류')
        for key,size in (('proposal_id',32),('request_sha256',64),('proposal_sha256',64)):
            if not isinstance(source[key],str) or not re.fullmatch('[0-9a-f]{'+str(size)+'}',source[key]):raise ValueError('AI 출처 ID/해시 오류')
        if type(source['reviewed']) is not bool or (not source['reviewed'] and not draft):raise ValueError('AI 초안은 QA 검토 후 저장해야 실행할 수 있습니다')
        if not isinstance(source['review_notes'],list) or len(source['review_notes'])>30 or any(not isinstance(s,str) or not s.strip() or len(s)>1000 for s in source['review_notes']):raise ValueError('AI 검토 항목 형식 오류')
    if 'source_manual' in config:
        source = config['source_manual']
        _object(source, ('execution_id', 'evidence_sha256', 'limitations'), 'source_manual')
        _text(source['execution_id'], 'source execution id')
        if not isinstance(source['evidence_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', source['evidence_sha256']):
            raise ValueError('invalid source evidence hash')
        if not isinstance(source['limitations'], list) or len(source['limitations']) > 10000:
            raise ValueError('invalid source limitations')
        for reason in source['limitations']:
            _text(reason, 'source limitation')
    if type(config['version']) is not int or config['version'] != 1:
        raise ValueError('unsupported scenario version')
    for key in ('id', 'title', 'url'):
        _text(config[key], key)
    try:
        parsed = urlsplit(config['url'])
        valid = parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('URL: use http(s) with a host and no credentials')
    for group in ('steps', 'checks'):
        rows = config[group]
        if not isinstance(rows, list) or not (0 if draft else 1) <= len(rows) <= 100:
            raise ValueError(f'{group}: 1 to 100 rows required')
        for index, row in enumerate(rows, 1):
            location = f'{group} {index}'
            kind_key, value_key = ('action', 'value') if group == 'steps' else ('kind', 'expected')
            _object(row, (kind_key, 'locator', 'target', value_key), location)
            choices = ACTION_LABELS if group == 'steps' else CHECK_LABELS
            if not isinstance(row[kind_key], str) or row[kind_key] not in choices:
                raise ValueError(f'{location}: unsupported {kind_key}')
            if not isinstance(row['locator'], str) or row['locator'] not in LOCATOR_LABELS:
                raise ValueError(f'{location}: unsupported locator')
            _text(row['target'], location + ' target')
            value = row[value_key]
            if group == 'steps':
                _text(value, location + ' value', allow_empty=row['action'] != 'press')
                if row['action'] in ('wait', 'click') and value:
                    raise ValueError(f'{location}: wait/click value must be empty')
            elif row['kind'] == 'input_length':
                if type(value) is not int or not 0 <= value <= 1000000:
                    raise ValueError(f'{location}: expected must be a nonnegative integer')
            elif row['kind'] == 'visible':
                if type(value) is not bool:
                    raise ValueError(f'{location}: expected must be true or false')
            else:
                _text(value, location + ' expected', allow_empty=True)
    return deepcopy(config)


def save_scenario(path, config):
    write_evidence(Path(path), validate_scenario(config, allow_draft=True))


def validate_observed_checks(config, checks):
    if not isinstance(checks, list) or len(checks) != len(config['checks']):
        raise ValueError('missing observed checks')
    for expected, observed in zip(config['checks'], checks):
        _object(observed, (*expected.keys(), 'actual', 'passed'), 'observed check')
        if any(observed[key] != value for key, value in expected.items()):
            raise ValueError('observed check does not match scenario')
        if type(observed['actual']) is not type(expected['expected']) or type(observed['passed']) is not bool:
            raise ValueError('invalid observed value')
        if observed['passed'] != (observed['actual'] == expected['expected']):
            raise ValueError('inconsistent observed verdict')
    return checks


def load_scenario(path):
    return validate_scenario(json.loads(Path(path).read_text(encoding='utf-8')), allow_draft=True)


def locate(page, row):
    if row['locator'] == 'css':
        return page.locator(row['target'])
    return page.get_by_label(row['target'], exact=True)


def execute_scenario(page, config, *, progress=None):
    """Run declarations and return actual checks; mismatch is not a setup exception."""
    config = validate_scenario(config)
    progress=progress or (lambda **event:None)
    page.goto(config['url'], wait_until='domcontentloaded', timeout=30000)
    for index, row in enumerate(config['steps'], 1):
        progress(phase='action',stopped_action=index)
        try:
            field = locate(page, row)
            if row['action'] == 'wait':
                field.wait_for(state='visible', timeout=5000)
            elif row['action'] == 'fill':
                field.fill(row['value'], timeout=5000)
            elif row['action'] == 'press':
                field.press(row['value'], timeout=5000)
            else:
                field.click(timeout=5000)
        except Exception as exc:
            raise ValueError(f'step {index} ({row["action"]}, {row["target"]}): {exc}') from exc
        progress(completed_actions=index)
    progress(phase='observations',stopped_action=None)
    checks = []
    for index, row in enumerate(config['checks'], 1):
        try:
            field = locate(page, row)
            if row['kind'] == 'input_length':
                actual = len(field.input_value(timeout=5000))
            elif row['kind'] == 'text':
                actual = field.inner_text(timeout=5000)
            else:
                actual = field.is_visible()
            checks.append({**row, 'actual': actual, 'passed': actual == row['expected']})
        except Exception as exc:
            raise ValueError(f'check {index} ({row["kind"]}, {row["target"]}): {exc}') from exc
    return checks
