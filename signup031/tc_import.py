"""Literal TC imports and versioned catalog, independent of execution evidence."""
from collections import Counter
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from signup031.ingestion_contract import safe_path

FIELDS = ('original_id', 'title', 'preconditions', 'steps', 'expected', 'priority')
MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS, MAX_COLUMNS, MAX_CELL = 10000, 128, 100000


def canonical(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
def fingerprint(value): return hashlib.sha256(canonical(value).encode()).hexdigest()
def now(): return datetime.now(timezone.utc).isoformat()


def parse_csv(path, *, encoding='utf-8-sig', header_row=1):
    if encoding not in ('utf-8-sig', 'cp949'): raise ValueError('지원 인코딩: UTF-8/BOM 또는 명시적 CP949')
    if type(header_row) is not int or not 1 <= header_row <= 100: raise ValueError('헤더 논리행은 1~100입니다')
    path = Path(path).absolute()
    with path.open('rb') as stream: raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES: raise ValueError('파일 상한 10 MiB 초과')
    try: text = raw.decode(encoding, errors='strict')
    except UnicodeError: raise ValueError('인코딩 오류: UTF-8/BOM 또는 CP949를 직접 선택하세요') from None
    if '\x00' in text: raise ValueError('CSV NUL 문자는 지원하지 않습니다')
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    rows, headers, end = [], None, 0
    try:
        for logical, values in enumerate(reader, 1):
            start = end + 1; end = reader.line_num
            if len(values) > MAX_COLUMNS or any(len(v) > MAX_CELL for v in values): raise ValueError('열/셀 상한 초과')
            if logical == header_row: headers = values
            elif logical > header_row:
                if len(rows) >= MAX_ROWS: raise ValueError('데이터 행 상한 10000 초과')
                rows.append({'logical_row': logical, 'physical_start': start, 'physical_end': end, 'cells': values})
    except csv.Error as exc:
        raise ValueError(f'CSV 구조/셀 상한 오류 · 물리줄 {reader.line_num}: {exc}') from None
    if not headers: raise ValueError('선택한 헤더 논리행이 없거나 비어 있습니다')
    duplicates = sorted(name for name, count in Counter(headers).items() if count > 1)
    return {'format_version': 1, 'kind': 'csv', 'source_location': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
            'raw_bytes': raw, 'encoding': encoding, 'header_row': header_row, 'headers': headers,
            'duplicate_headers': duplicates, 'rows': rows}


class TCStore:
    def __init__(self, root):
        self.root = Path(root).absolute()
        if any((p / 'evidence.json').exists() for p in (self.root, *self.root.parents)):
            raise ValueError('실행 원본 폴더 밖의 결과 루트를 선택하세요')
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = safe_path(self.root, '.tc/catalog.sqlite3')
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS tc_cases (
                    local_id TEXT PRIMARY KEY, project TEXT NOT NULL, source_id TEXT NOT NULL,
                    original_id TEXT NOT NULL, identity TEXT NOT NULL, current_version INTEGER NOT NULL,
                    UNIQUE(project,source_id,identity));
                CREATE TABLE IF NOT EXISTS tc_versions (
                    local_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
                    batch_id TEXT NOT NULL, logical_row INTEGER NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(local_id,version));
                CREATE TABLE IF NOT EXISTS tc_batches (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, source_id TEXT NOT NULL,
                    metadata TEXT NOT NULL, original BLOB NOT NULL, imported_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tc_candidates (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, source_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL, logical_row INTEGER NOT NULL, target_id TEXT,
                    base_version INTEGER, payload TEXT NOT NULL, reason TEXT NOT NULL,
                    status TEXT NOT NULL, UNIQUE(batch_id,logical_row));
                CREATE TABLE IF NOT EXISTS tc_mappings (
                    project TEXT NOT NULL, signature TEXT NOT NULL, mapping TEXT NOT NULL,
                    PRIMARY KEY(project,signature));
                CREATE TABLE IF NOT EXISTS tc_candidate_reviews (
                    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, previous_base INTEGER,
                    reviewed_base INTEGER NOT NULL, reviewed_at TEXT NOT NULL);
            ''')

    @contextmanager
    def _connect(self, *, timeout=10):
        safe_path(self.root, '.tc/catalog.sqlite3')
        conn = sqlite3.connect(self.db, timeout=timeout); conn.row_factory = sqlite3.Row
        try:
            with conn: yield conn
        finally: conn.close()

    def get_mapping(self, project, headers):
        with self._connect() as conn:
            row = conn.execute('SELECT mapping FROM tc_mappings WHERE project=? AND signature=?', (project, fingerprint(headers))).fetchone()
        return json.loads(row[0]) if row else None

    def list_cases(self, project, *, limit=1000, offset=0):
        with self._connect() as conn:
            rows = conn.execute('''SELECT c.*,v.payload FROM tc_cases c JOIN tc_versions v
                ON c.local_id=v.local_id AND c.current_version=v.version WHERE c.project=? ORDER BY c.rowid LIMIT ? OFFSET ?''', (project, min(limit,1000),max(0,offset))).fetchall()
        return [self._case(row) for row in rows]

    def count_cases(self, project):
        with self._connect() as conn: return conn.execute('SELECT COUNT(*) FROM tc_cases WHERE project=?', (project,)).fetchone()[0]

    @staticmethod
    def _case(row):
        return {'local_id': row['local_id'], 'project': row['project'], 'source_id': row['source_id'],
            'original_id': row['original_id'], 'version': row['current_version'], 'automation': None, **json.loads(row['payload'])}

    def case(self, local_id):
        with self._connect() as conn:
            row = conn.execute('''SELECT c.*,v.payload FROM tc_cases c JOIN tc_versions v
                ON c.local_id=v.local_id AND c.current_version=v.version WHERE c.local_id=?''', (local_id,)).fetchone()
        if not row: raise ValueError('TC가 없습니다')
        return self._case(row)

    def version(self, local_id, version):
        with self._connect() as conn:
            row = conn.execute('SELECT payload FROM tc_versions WHERE local_id=? AND version=?', (local_id,version)).fetchone()
        if not row: raise ValueError('TC 버전이 없습니다')
        return json.loads(row[0])

    def list_candidates(self, project, *, limit=1000, offset=0):
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tc_candidates WHERE project=? AND status='pending' ORDER BY rowid LIMIT ? OFFSET ?", (project,min(limit,1000),max(0,offset))).fetchall()
        return [{**dict(r), 'payload': json.loads(r['payload'])} for r in rows]

    def count_candidates(self,project):
        with self._connect() as conn: return conn.execute("SELECT COUNT(*) FROM tc_candidates WHERE project=? AND status='pending'",(project,)).fetchone()[0]

    def register(self, project, source_id, batch, mapping, *, cancel=None):
        if not all(isinstance(v,str) and v.strip() and len(v) <= 2000 for v in (project,source_id)):
            raise ValueError('프로젝트와 출처 ID를 입력하세요')
        headers = batch['headers']
        if not isinstance(mapping,dict) or set(mapping)-set(FIELDS) or mapping.get('title') is None:
            raise ValueError('제목 열을 매핑하세요')
        chosen = [i for i in mapping.values() if i is not None]
        if any(type(i) is not int or not 0 <= i < len(headers) for i in chosen) or len(set(chosen)) != len(chosen):
            raise ValueError('매핑 열이 없거나 중복되었습니다')
        mapping = {key: mapping.get(key) for key in FIELDS}
        batch_id = fingerprint([project, source_id, batch['kind'], batch['sha256'], batch['header_row'], headers, mapping, batch['encoding']])
        mapped = []
        for row in batch['rows']:
            cells = row['cells']
            fields = {key: cells[i] if i is not None and i < len(cells) else '' for key,i in mapping.items()}
            mapped.append((row,fields))
        ids = Counter(fields['original_id'] for _,fields in mapped if fields['original_id'].strip())
        counts = {'registered':0,'duplicate':0,'candidate':0,'error':0,'review_needed':0}
        outcomes = []
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            prior_no_id = conn.execute("SELECT 1 FROM tc_cases WHERE project=? AND source_id=? AND original_id='' LIMIT 1", (project,source_id)).fetchone() is not None
            metadata = {key:value for key,value in batch.items() if key not in ('raw_bytes','rows')}
            metadata['mapping'] = mapping
            conn.execute('INSERT OR IGNORE INTO tc_batches VALUES(?,?,?,?,?,?)', (batch_id,project,source_id,canonical(metadata),batch['raw_bytes'],now()))
            imported_at = conn.execute('SELECT imported_at FROM tc_batches WHERE id=?',(batch_id,)).fetchone()[0]
            for row,fields in mapped:
                if cancel and cancel(): raise ValueError('등록 취소: 이번 배치를 저장하지 않았습니다')
                error = ('제목 누락' if not fields['title'].strip() else '열 수가 헤더와 다릅니다' if len(row['cells']) != len(headers) else
                         '중복 원본 ID' if fields['original_id'].strip() and ids[fields['original_id']] > 1 else '')
                state, reason, local_id = 'error', error, None
                if not error:
                    oid = fields['original_id'] if fields['original_id'].strip() else ''
                    review = not fields['steps'].strip() or not fields['expected'].strip()
                    counts['review_needed'] += int(review)
                    source = {'batch_id':batch_id, 'kind':batch['kind'], 'location':batch['source_location'],
                        'sha256':batch['sha256'], 'encoding':batch['encoding'], 'header_row':batch['header_row'],
                        'headers':headers,'mapping':mapping,'imported_at':imported_at,
                        'provider_metadata':batch.get('provider_metadata',{}), **row}
                    payload = {'fields':fields,'review_needed':review,'source':source}
                    identity = 'id:' + oid if oid else 'anonymous:' + batch_id + ':' + str(row['logical_row'])
                    existing = conn.execute('SELECT * FROM tc_cases WHERE project=? AND source_id=? AND identity=?', (project,source_id,identity)).fetchone()
                    if not oid and existing is None:
                        existing = conn.execute('''SELECT c.* FROM tc_cases c JOIN tc_versions v ON c.local_id=v.local_id
                            WHERE c.project=? AND c.source_id=? AND v.batch_id=? AND v.logical_row=? LIMIT 1''',
                            (project,source_id,batch_id,row['logical_row'])).fetchone()
                    previous = conn.execute('SELECT payload FROM tc_versions WHERE local_id=? AND version=?', (existing['local_id'],existing['current_version'])).fetchone() if existing else None
                    if previous and json.loads(previous[0])['fields'] == fields:
                        state,local_id = 'duplicate',existing['local_id']
                    elif existing or (not oid and prior_no_id):
                        state = 'candidate'; reason = 'content_changed' if existing else 'identity_uncertain'
                        local_id = existing['local_id'] if existing else None
                        conn.execute('''INSERT INTO tc_candidates VALUES(?,?,?,?,?,?,?,?,?,'pending')
                            ON CONFLICT(batch_id,logical_row) DO UPDATE SET status='pending',base_version=excluded.base_version,
                            target_id=excluded.target_id,payload=excluded.payload WHERE tc_candidates.status='applied' ''',
                            (uuid4().hex,project,source_id,batch_id,row['logical_row'],local_id,
                             existing['current_version'] if existing else None,canonical(payload),reason))
                    else:
                        state,local_id = 'registered',uuid4().hex
                        conn.execute('INSERT INTO tc_cases VALUES(?,?,?,?,?,1)', (local_id,project,source_id,oid,identity))
                        conn.execute('INSERT INTO tc_versions VALUES(?,1,?,?,?,?)', (local_id,canonical(payload),batch_id,row['logical_row'],now()))
                counts[state] += 1
                outcomes.append({'logical_row':row['logical_row'],'physical_start':row['physical_start'],
                    'physical_end':row['physical_end'],'status':state,'reason':reason,'local_id':local_id})
            if cancel and cancel(): raise ValueError('등록 취소: 이번 배치를 저장하지 않았습니다')
            conn.execute('INSERT INTO tc_mappings VALUES(?,?,?) ON CONFLICT(project,signature) DO UPDATE SET mapping=excluded.mapping',
                         (project,fingerprint(headers),canonical(mapping)))
        return {'batch_id':batch_id,'counts':counts,'rows':outcomes}

    def apply_candidate(self, project, candidate_id, *, target_id=None, expected_version=None, as_new=False):
        with self._connect(timeout=0) as conn:
            conn.execute('BEGIN IMMEDIATE')
            candidate = conn.execute("SELECT * FROM tc_candidates WHERE id=? AND project=? AND status='pending'", (candidate_id,project)).fetchone()
            if candidate is None: raise ValueError('대기 중인 변경 후보가 없습니다')
            payload = json.loads(candidate['payload'])
            if as_new:
                if candidate['target_id'] is not None or payload['fields']['original_id'].strip(): raise ValueError('원본 ID 변경은 새 TC로 중복 등록할 수 없습니다')
                local_id = uuid4().hex
                identity = 'anonymous:' + candidate['batch_id'] + ':' + str(candidate['logical_row'])
                conn.execute('INSERT INTO tc_cases VALUES(?,?,?,?,?,1)', (local_id,project,candidate['source_id'],'',identity))
                version = 1
            else:
                target = conn.execute('SELECT * FROM tc_cases WHERE local_id=? AND project=? AND source_id=?', (target_id,project,candidate['source_id'])).fetchone()
                if not target or expected_version != target['current_version']: raise ValueError('대상 TC/버전이 바뀌었습니다. 다시 확인하세요')
                if candidate['target_id'] and (target_id != candidate['target_id'] or expected_version != candidate['base_version']):
                    raise ValueError('후보의 기준 버전과 다릅니다')
                raw_id=payload['fields']['original_id']
                identity_id=raw_id if raw_id.strip() else ''
                if target['original_id'] != identity_id: raise ValueError('원본 ID가 다른 TC에는 적용할 수 없습니다')
                local_id,version = target_id,target['current_version']+1
                conn.execute('UPDATE tc_cases SET current_version=? WHERE local_id=?', (version,local_id))
            conn.execute('INSERT INTO tc_versions VALUES(?,?,?,?,?,?)', (local_id,version,canonical(payload),candidate['batch_id'],candidate['logical_row'],now()))
            conn.execute("UPDATE tc_candidates SET status='applied' WHERE id=?", (candidate_id,))
        return self.case(local_id)

    def review_candidate(self,project,candidate_id,*,expected_version):
        """User explicitly accepts a new comparison baseline; imported content stays intact."""
        with self._connect(timeout=0) as conn:
            conn.execute('BEGIN IMMEDIATE')
            candidate=conn.execute("SELECT * FROM tc_candidates WHERE id=? AND project=? AND status='pending'",(candidate_id,project)).fetchone()
            if not candidate or not candidate['target_id']: raise ValueError('원본 ID가 확인된 변경 후보를 선택하세요')
            target=conn.execute('SELECT * FROM tc_cases WHERE local_id=? AND project=?',(candidate['target_id'],project)).fetchone()
            if not target or target['current_version']!=expected_version: raise ValueError('현재 버전이 바뀌었습니다. 목록을 다시 확인하세요')
            conn.execute('INSERT INTO tc_candidate_reviews VALUES(?,?,?,?,?)',(uuid4().hex,candidate_id,candidate['base_version'],expected_version,now()))
            conn.execute('UPDATE tc_candidates SET base_version=? WHERE id=?',(expected_version,candidate_id))
