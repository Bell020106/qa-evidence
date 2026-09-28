"""Only explicitly supplied test text; hash-linked optional v4 sidecar."""
from copy import deepcopy
import hashlib,json,re
from pathlib import Path
from signup031.ingestion_contract import safe_path

LIMITS={'tc_id':200,'expected':8000,'automation_code':16000}
MAX_BYTES=150000


def validate_context(value):
    if not isinstance(value,dict) or set(value)!=set(LIMITS):raise ValueError('test_context에는 tc_id, expected, automation_code 문자열만 지정하세요.')
    for key,limit in LIMITS.items():
        if not isinstance(value[key],str) or len(value[key])>limit:raise ValueError(f'test_context {key} 길이 상한 {limit}자')
    return deepcopy(value)


def mask_text(value):
    value=re.sub(r'(?i)\bBearer\s+[^\s,;\"\']+','Bearer [REDACTED]',value)
    value=re.sub(r'''(?i)((?:password|secret|api[_-]?key|token|authorization|cookie)["']?\s*[:=]\s*)(["'])(.*?)\2''',lambda m:m[1]+m[2]+'[REDACTED]'+m[2],value)
    return re.sub(r'(?i)((?:password|secret|api[_-]?key|token|authorization|cookie)\s*[:=]\s*)[^\s,;&\"\']+',r'\1[REDACTED]',value)


def save_context(root,eid,context,redact):
    value={k:mask_text(redact(v)) for k,v in validate_context(context).items()}
    data={'version':1,'execution_id':eid,'source':'explicit-selenium-test','test_context':value}
    raw=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()
    if len(raw)>MAX_BYTES:raise ValueError('test_context 파일 크기 상한')
    path=safe_path(root,'test-context.json');path.write_bytes(raw)
    return {'version':1,'path':'test-context.json','size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}


def validate_declaration(declaration):
    if not isinstance(declaration,dict) or set(declaration)!={'version','path','size','sha256'} or declaration['version']!=1 or declaration['path']!='test-context.json':raise ValueError('테스트 자료 선언 오류')
    if type(declaration['size']) is not int or not 0<declaration['size']<=MAX_BYTES or not isinstance(declaration['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',declaration['sha256']):raise ValueError('테스트 자료 크기/해시 오류')
    return declaration


def validate_bytes(raw,eid,declaration):
    validate_declaration(declaration)
    if len(raw)!=declaration['size'] or hashlib.sha256(raw).hexdigest()!=declaration['sha256']:raise ValueError('테스트 자료 해시 불일치')
    data=json.loads(raw)
    if not isinstance(data,dict) or set(data)!={'version','execution_id','source','test_context'} or data['version']!=1 or data['execution_id']!=eid or data['source']!='explicit-selenium-test':raise ValueError('테스트 자료 실행 연결 오류')
    validate_context(data['test_context']);return data


def load_test_context(evidence_path):
    path=Path(evidence_path);payload=json.loads(path.read_bytes());declaration=payload.get('test_context')
    if declaration is None:return None
    validate_declaration(declaration);target=safe_path(path.parent,declaration['path'])
    with target.open('rb') as stream:raw=stream.read(MAX_BYTES+1)
    return validate_bytes(raw,payload['execution']['id'],declaration)
