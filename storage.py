import hashlib
import json
import sqlite3
import time


class Archive:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript(
            'CREATE TABLE IF NOT EXISTS captures('
            'id INTEGER PRIMARY KEY, source TEXT NOT NULL, digest TEXT NOT NULL,'
            'raw BLOB NOT NULL, rows_json TEXT NOT NULL, UNIQUE(source,digest));'
            # 观察批次：暂停重开是新的观察，resume_of 表达批次关系而非原子快照。
            'CREATE TABLE IF NOT EXISTS observations('
            'id INTEGER PRIMARY KEY, source TEXT NOT NULL,'
            'started_at TEXT NOT NULL, finished_at TEXT,'
            'resume_of INTEGER REFERENCES observations(id),'
            'key_marker TEXT, version_id_marker TEXT,'
            'truncated INTEGER NOT NULL DEFAULT 1,'
            'next_key_marker TEXT, next_version_id_marker TEXT,'
            'endpoint TEXT NOT NULL, summary_json TEXT NOT NULL);'
            # 每一页原始清单留存，按观察内序号固定。
            'CREATE TABLE IF NOT EXISTS manifest_pages('
            'id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL'
            ' REFERENCES observations(id) ON DELETE CASCADE,'
            'page_seq INTEGER NOT NULL, request_url TEXT NOT NULL,'
            'digest TEXT NOT NULL, bytes INTEGER NOT NULL,'
            'key_marker TEXT, version_id_marker TEXT,'
            'raw BLOB NOT NULL, UNIQUE(observation_id,page_seq));'
            # 同一键允许多版本、允许跨页；IsLatest 是落库时的服务端观察，
            # 之后抓到更老版本绝不回写该标志。
            'CREATE TABLE IF NOT EXISTS manifest_versions('
            'id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL'
            ' REFERENCES observations(id) ON DELETE CASCADE,'
            'key TEXT NOT NULL, version_id TEXT NOT NULL,'
            'kind TEXT NOT NULL CHECK(kind IN ("version","delete")),'
            'is_latest INTEGER NOT NULL, page_seq INTEGER NOT NULL,'
            'etag TEXT, checksum TEXT, restore TEXT, last_modified TEXT,'
            'UNIQUE(observation_id,key,version_id));'
            # 取回状态机：manifest-only -> restore-pending -> verified 等；
            # 423 记等待，404 才记缺失，删除标记不发起取回。
            'CREATE TABLE IF NOT EXISTS object_retrievals('
            'id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL,'
            'version_id INTEGER NOT NULL,'
            'request_url TEXT NOT NULL, http_status INTEGER,'
            'state TEXT NOT NULL, digest TEXT, bytes INTEGER,'
            'etag TEXT, reason TEXT, attempted_at TEXT NOT NULL,'
            'raw BLOB,'
            'FOREIGN KEY(observation_id) REFERENCES observations(id) ON DELETE CASCADE,'
            'FOREIGN KEY(version_id) REFERENCES manifest_versions(id) ON DELETE CASCADE);')

    # -- 既有单页检查收件能力，保持不变 ----------------------------------

    def save(self, source, raw, rows):
        if not source.strip():
            raise ValueError('来源标识不能为空')
        digest = hashlib.sha256(raw).hexdigest()
        payload = json.dumps(rows, ensure_ascii=False, sort_keys=True)
        with self.connection:
            self.connection.execute(
                'INSERT OR IGNORE INTO captures(source,digest,raw,rows_json) VALUES(?,?,?,?)',
                (source, digest, raw, payload))
            record = self.connection.execute(
                'SELECT id FROM captures WHERE source=? AND digest=?', (source, digest)).fetchone()
        return record[0]

    def list(self):
        return [dict(id=row[0], source=row[1], digest=row[2], rows=json.loads(row[3]))
                for row in self.connection.execute(
                    'SELECT id,source,digest,rows_json FROM captures ORDER BY id')]

    def raw(self, capture_id):
        row = self.connection.execute(
            'SELECT raw FROM captures WHERE id=?', (capture_id,)).fetchone()
        if row is None:
            raise KeyError(capture_id)
        return bytes(row[0])

    # -- 版本清单观察批次 -------------------------------------------------

    def start_observation(self, source, endpoint, *, resume_of=None,
                          key_marker=None, version_id_marker=None):
        if not source.strip():
            raise ValueError('来源标识不能为空')
        if resume_of is not None and self.connection.execute(
                'SELECT 1 FROM observations WHERE id=?', (resume_of,)).fetchone() is None:
            raise ValueError('续跑目标观察批次不存在')
        now = _now()
        with self.connection:
            cur = self.connection.execute(
                'INSERT INTO observations(source,started_at,resume_of,key_marker,'
                'version_id_marker,endpoint,summary_json) VALUES(?,?,?,?,?,?,?)',
                (source, now, resume_of, key_marker, version_id_marker, endpoint,
                 json.dumps({}, ensure_ascii=False)))
        return cur.lastrowid

    def save_page(self, observation_id, page, raw):
        with self.connection:
            self.connection.execute(
                'INSERT INTO manifest_pages(observation_id,page_seq,request_url,digest,'
                'bytes,key_marker,version_id_marker,raw) VALUES(?,?,?,?,?,?,?,?)',
                (observation_id, page['seq'], page['url'],
                 hashlib.sha256(raw).hexdigest(), len(raw),
                 page.get('key_marker'), page.get('version_id_marker'), raw))
            for entry in page['entries']:
                self.connection.execute(
                    'INSERT OR IGNORE INTO manifest_versions(observation_id,key,'
                    'version_id,kind,is_latest,page_seq,etag,checksum,restore,last_modified)'
                    ' VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (observation_id, entry['key'], entry['version_id'], entry['kind'],
                     1 if entry['is_latest'] else 0, page['seq'], entry.get('etag'),
                     entry.get('checksum'), entry.get('restore'),
                     entry.get('last_modified')))

    def finish_observation(self, observation_id, *, truncated, next_key_marker=None,
                           next_version_id_marker=None):
        with self.connection:
            self.connection.execute(
                'UPDATE observations SET finished_at=?, truncated=?, next_key_marker=?,'
                ' next_version_id_marker=?, summary_json=? WHERE id=?',
                (_now(), 1 if truncated else 0, next_key_marker, next_version_id_marker,
                 json.dumps(self.observation_summary(observation_id),
                            ensure_ascii=False, sort_keys=True), observation_id))

    def observation_summary(self, observation_id):
        counts = dict(self.connection.execute(
            'SELECT kind, COUNT(*) FROM manifest_versions WHERE observation_id=?'
            ' GROUP BY kind', (observation_id,)).fetchall())
        page_count, total_bytes = self.connection.execute(
            'SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM manifest_pages'
            ' WHERE observation_id=?', (observation_id,)).fetchone()
        keys = self.connection.execute(
            'SELECT COUNT(DISTINCT key) FROM manifest_versions WHERE observation_id=?',
            (observation_id,)).fetchone()[0]
        return {'pages': page_count, 'bytes': total_bytes,
                'distinct_keys': keys,
                'versions': counts.get('version', 0),
                'delete_markers': counts.get('delete', 0)}

    def get_observation(self, observation_id):
        row = self.connection.execute(
            'SELECT id,source,started_at,finished_at,resume_of,key_marker,version_id_marker,'
            'truncated,next_key_marker,next_version_id_marker,endpoint,summary_json'
            ' FROM observations WHERE id=?', (observation_id,)).fetchone()
        if row is None:
            raise KeyError(observation_id)
        keys = ('id', 'source', 'started_at', 'finished_at', 'resume_of', 'key_marker',
                'version_id_marker', 'truncated', 'next_key_marker',
                'next_version_id_marker', 'endpoint', 'summary_json')
        data = dict(zip(keys, row))
        data['truncated'] = bool(data['truncated'])
        data['summary'] = json.loads(data.pop('summary_json'))
        return data

    def list_observations(self):
        rows = self.connection.execute(
            'SELECT id FROM observations ORDER BY id').fetchall()
        return [self.get_observation(row[0]) for row in rows]

    def list_versions(self, observation_id):
        rows = self.connection.execute(
            'SELECT id,key,version_id,kind,is_latest,page_seq,etag,checksum,restore,'
            'last_modified FROM manifest_versions WHERE observation_id=?'
            ' ORDER BY page_seq,id', (observation_id,)).fetchall()
        keys = ('id', 'key', 'version_id', 'kind', 'is_latest', 'page_seq', 'etag',
                'checksum', 'restore', 'last_modified')
        result = []
        for row in rows:
            item = dict(zip(keys, row))
            item['is_latest'] = bool(item['is_latest'])
            result.append(item)
        return result

    def find_version(self, observation_id, key, version_id):
        row = self.connection.execute(
            'SELECT id,key,version_id,kind,is_latest,page_seq,etag,checksum,restore,'
            'last_modified FROM manifest_versions WHERE observation_id=? AND key=?'
            ' AND version_id=?', (observation_id, key, version_id)).fetchone()
        if row is None:
            raise KeyError('清单中不存在该版本')
        keys = ('id', 'key', 'version_id', 'kind', 'is_latest', 'page_seq', 'etag',
                'checksum', 'restore', 'last_modified')
        item = dict(zip(keys, row))
        item['is_latest'] = bool(item['is_latest'])
        return item

    def record_retrieval(self, observation_id, key, version_id, request_url, verification,
                         raw=None):
        version = self.find_version(observation_id, key, version_id)
        if version['kind'] == 'delete':
            raise ValueError('删除标记没有对象正文，不能发起取回')
        with self.connection:
            self.connection.execute(
                'INSERT INTO object_retrievals(observation_id,version_id,request_url,'
                'http_status,state,digest,bytes,etag,reason,attempted_at,raw)'
                ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (observation_id, version['id'], request_url,
                 verification.get('http_status'), verification['state'],
                 verification.get('digest'), verification.get('bytes'),
                 verification.get('etag'), verification.get('reason'), _now(), raw))
        return self.get_retrieval(observation_id, key, version_id)

    def get_retrieval(self, observation_id, key, version_id):
        row = self.connection.execute(
            'SELECT r.http_status,r.state,r.digest,r.bytes,r.etag,r.reason,r.attempted_at,'
            'r.raw IS NOT NULL AS has_raw FROM object_retrievals r'
            ' JOIN manifest_versions v ON v.id=r.version_id'
            ' WHERE r.observation_id=? AND v.key=? AND v.version_id=?'
            ' ORDER BY r.id DESC LIMIT 1',
            (observation_id, key, version_id)).fetchone()
        if row is None:
            return None
        return dict(zip(('http_status', 'state', 'digest', 'bytes', 'etag', 'reason',
                         'attempted_at', 'has_raw'), row))

    def _version_views(self, observation_id):
        views = []
        for version in self.list_versions(observation_id):
            retrieval = self.get_retrieval(
                observation_id, version['key'], version['version_id'])
            if version['kind'] == 'delete':
                # 删除标记既不是空文件也不是缺失，单独标注。
                status = 'delete-marker'
            elif retrieval is None:
                # 仅在清单发现：尚未取回，不能叫缺失。
                status = 'manifest-only'
            else:
                status = retrieval['state']
            view = {k: version[k] for k in
                    ('key', 'version_id', 'kind', 'is_latest', 'page_seq',
                     'checksum', 'etag', 'restore')}
            view['retrieval_state'] = status
            if retrieval is not None:
                view['retrieval'] = {k: retrieval[k] for k in
                                     ('http_status', 'state', 'digest', 'bytes',
                                      'etag', 'reason', 'attempted_at', 'has_raw')}
            views.append(view)
        return views

    def export_current(self, observation_id):
        """某次观察下的当前文件集合：仅 is_latest 且非删除标记。

        IsLatest 直接采用落库的服务端观察值，不随后续版本抓取回退。
        """
        files = []
        delete_markers = []
        for view in self._version_views(observation_id):
            if not view['is_latest']:
                continue
            if view['kind'] == 'delete':
                delete_markers.append(view)
            else:
                files.append(view)
        return {'observation_id': observation_id,
                'observed_at': self.get_observation(observation_id)['finished_at'],
                'current_files': files, 'current_delete_markers': delete_markers}

    def export_history(self, observation_id):
        """实际收到/观察到的全部历史版本（含删除标记与跨页版本）。"""
        views = self._version_views(observation_id)
        verified = [v for v in views if v['retrieval_state'] == 'verified']
        pending = [v for v in views if v['retrieval_state'] == 'restore-pending']
        manifest_only = [v for v in views if v['retrieval_state'] == 'manifest-only']
        return {'observation_id': observation_id,
                'observed_at': self.get_observation(observation_id)['finished_at'],
                'versions': views,
                'verified_count': len(verified),
                'restore_pending_count': len(pending),
                'manifest_only_count': len(manifest_only)}

    def page_raw(self, observation_id, page_seq):
        row = self.connection.execute(
            'SELECT raw FROM manifest_pages WHERE observation_id=? AND page_seq=?',
            (observation_id, page_seq)).fetchone()
        if row is None:
            raise KeyError((observation_id, page_seq))
        return bytes(row[0])

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _now():
    # 本地时间戳仅用于展示批次顺序，不能用作版本唯一性推断依据。
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
