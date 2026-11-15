import hashlib
import json

# 演练场景中 v2 版本取回成功后的实际正文，清单校验值据此生成。
MANIFEST_PAYLOAD = b'synthetic,cargo\nA,12\n'


def samples():
    first = {'encoding': 'url', 'entries': [
        {'key': 'cargo%2Fship%20A.csv', 'versionId': 'v3', 'kind': 'delete', 'isLatest': True},
        {'key': 'cargo%2Fship%20A.csv', 'versionId': 'v2', 'kind': 'version', 'isLatest': False,
         'etag': 'not-an-md5-2',
         'checksum': 'sha256:' + hashlib.sha256(MANIFEST_PAYLOAD).hexdigest(),
         'restore': 'ongoing'}],
        'isTruncated': True, 'nextKeyMarker': 'cargo/ship A.csv', 'nextVersionIdMarker': 'v2'}
    second = {'entries': [{'key': 'cargo/ship A.csv', 'versionId': 'v1', 'kind': 'version', 'isLatest': False},
                          {'key': 'cargo/new.csv', 'versionId': 'new1', 'kind': 'version', 'isLatest': True}],
              'isTruncated': False}
    return {'sample.json': json.dumps(first).encode(), 'page2.json': json.dumps(second).encode(),
            'invalid.json': b'{"entries": null}'}
