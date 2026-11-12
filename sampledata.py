import json

def samples():
    first = {'encoding': 'url', 'entries': [
        {'key': 'cargo%2Fship%20A.csv', 'versionId': 'v3', 'kind': 'delete', 'isLatest': True},
        {'key': 'cargo%2Fship%20A.csv', 'versionId': 'v2', 'kind': 'version', 'isLatest': False,
         'etag': 'not-an-md5-2', 'checksum': 'sha256:known', 'restore': 'ongoing'}],
        'isTruncated': True, 'nextKeyMarker': 'cargo/ship A.csv', 'nextVersionIdMarker': 'v2'}
    second = {'entries': [{'key': 'cargo/ship A.csv', 'versionId': 'v1', 'kind': 'version', 'isLatest': False},
                         {'key': 'cargo/new.csv', 'versionId': 'new1', 'kind': 'version', 'isLatest': True}],
              'isTruncated': False}
    return {'sample.json': json.dumps(first).encode(), 'page2.json': json.dumps(second).encode(),
            'invalid.json': b'{"entries": null}'}
