import json
from urllib.parse import unquote

def inspect_bytes(data):
    page = json.loads(data)
    if not isinstance(page, dict) or not isinstance(page.get('entries'), list):
        raise ValueError('缺少版本清单 entries')
    result = []
    for entry in page['entries']:
        kind = entry['kind']
        if kind not in ('version', 'delete'):
            raise ValueError('未知版本条目类型')
        key = unquote(entry['key']) if page.get('encoding') == 'url' else entry['key']
        result.append({'key': key, 'version': entry['versionId'], 'kind': kind,
                       'is_latest': bool(entry['isLatest'])})
    return result
