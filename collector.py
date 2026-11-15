"""版本清单采集与版本取回。

来源契约见 docs/source-contract.md：清单按 key-marker/version-id-marker
双标记续页；删除标记（kind=delete）没有对象正文，必须与普通版本同样保留；
versionId 不透明，IsLatest 只是服务端在该页当时的观察结果。
"""

import hashlib
import json
from urllib.parse import quote, urlencode

from transport import fetch

LIST_PATH = '/list'
OBJECT_PATH = '/object'
VALID_KINDS = ('version', 'delete')


class ListingError(Exception):
    """清单协议或续页约定被破坏。"""

    def __init__(self, reason, status=None, url=None):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.url = url


def _parse_checksum(value):
    """把 'sha256:<hex>' 归一化为 ('sha256', '<hex>')，无法识别返回 None。"""
    if not isinstance(value, str) or ':' not in value:
        return None
    algorithm, digest = value.split(':', 1)
    if algorithm != 'sha256' or not digest:
        return None
    return algorithm, digest.lower()


def parse_page(raw):
    """解析单个清单页，归一化条目，不做跨页推断。

    返回 dict(entries, is_truncated, next_key_marker,
    next_version_id_marker, encoding)。key 按 encoding 只解码一次，
    versionId 原样保留。
    """
    page = json.loads(raw)
    if not isinstance(page, dict) or not isinstance(page.get('entries'), list):
        raise ValueError('缺少版本清单 entries')
    encoding = page.get('encoding')
    entries = []
    for entry in page['entries']:
        if not isinstance(entry, dict):
            raise ValueError('版本条目必须是对象')
        kind = entry.get('kind')
        if kind not in VALID_KINDS:
            raise ValueError('未知版本条目类型')
        raw_key = entry.get('key')
        version_id = entry.get('versionId')
        if not isinstance(raw_key, str) or not raw_key:
            raise ValueError('缺少版本条目 key')
        if not isinstance(version_id, str) or not version_id:
            raise ValueError('缺少不透明 versionId')
        if not isinstance(entry.get('isLatest'), bool):
            raise ValueError('缺少布尔 isLatest 观察值')
        key = _decode_key(raw_key, encoding)
        normalized = {
            'key': key,
            'version_id': version_id,
            'kind': kind,
            'is_latest': entry['isLatest'],
            'etag': entry.get('etag'),
            'checksum': entry.get('checksum'),
            'restore': entry.get('restore'),
            'last_modified': entry.get('lastModified'),
        }
        entries.append(normalized)
    is_truncated = bool(page.get('isTruncated'))
    result = {
        'entries': entries,
        'is_truncated': is_truncated,
        'next_key_marker': page.get('nextKeyMarker'),
        'next_version_id_marker': page.get('nextVersionIdMarker'),
        'encoding': encoding,
    }
    if is_truncated and (result['next_key_marker'] is None
                         or result['next_version_id_marker'] is None):
        raise ListingError('截断页缺少双标记，无法按契约续页')
    return result


def _decode_key(raw_key, encoding):
    # 只按来源声明解码一次；解码后的标记在发请求时重新编码，不重复解码。
    if encoding == 'url':
        from urllib.parse import unquote
        return unquote(raw_key)
    return raw_key


def build_list_url(endpoint, key_marker=None, version_id_marker=None):
    params = []
    if key_marker is not None:
        params.append(('key-marker', key_marker))
    if version_id_marker is not None:
        # 两个标记必须成对透传，缺一只传 key-marker 会重复或漏页。
        params.append(('version-id-marker', version_id_marker))
    query = urlencode(params, quote_via=quote) if params else ''
    return endpoint.rstrip('/') + LIST_PATH + (('?' + query) if query else '')


def build_object_url(endpoint, key, version_id):
    query = urlencode([('key', key), ('versionId', version_id)], quote_via=quote)
    return endpoint.rstrip('/') + OBJECT_PATH + '?' + query


def fetch_page(endpoint, key_marker=None, version_id_marker=None, http=fetch):
    """取一页清单并记录请求现场，HTTP 非 200 不允许当作空清单。"""
    url = build_list_url(endpoint, key_marker, version_id_marker)
    status, _headers, raw = http(url)
    if status != 200:
        raise ListingError('清单页 HTTP 状态非 200', status=status, url=url)
    page = parse_page(raw)
    page['url'] = url
    page['raw'] = raw
    page['bytes'] = len(raw)
    page['digest'] = hashlib.sha256(raw).hexdigest()
    page['key_marker'] = key_marker
    page['version_id_marker'] = version_id_marker
    return page


def collect_pages(endpoint, *, key_marker=None, version_id_marker=None,
                  max_pages=None, http=fetch):
    """按双标记从给定位置续页。

    max_pages 用于暂停：到达页数上限且来源仍截断时，返回下一对标记，
    调用方据此另开观察批次，而不是把多批次拼成一次原子快照。
    """
    pages = []
    entries = []
    page_count = 0
    while True:
        page = fetch_page(endpoint, key_marker, version_id_marker, http=http)
        page_count += 1
        page_seq = page_count
        # 原始字节随页记录返回，由存储层留存，避免二次请求产生观察偏差。
        pages.append({
            'seq': page_seq,
            'url': page['url'],
            'raw': page['raw'],
            'bytes': page['bytes'],
            'digest': page['digest'],
            'key_marker': page['key_marker'],
            'version_id_marker': page['version_id_marker'],
        })
        for entry in page['entries']:
            # 同一键的版本允许跨页；标注页码但不合并、不改写 IsLatest。
            entries.append(dict(entry, page_seq=page_seq))
        if not page['is_truncated']:
            return {
                'entries': entries,
                'pages': pages,
                'truncated': False,
                'next_key_marker': None,
                'next_version_id_marker': None,
            }
        key_marker = page['next_key_marker']
        version_id_marker = page['next_version_id_marker']
        if max_pages is not None and page_count >= max_pages:
            return {
                'entries': entries,
                'pages': pages,
                'truncated': True,
                'next_key_marker': key_marker,
                'next_version_id_marker': version_id_marker,
            }


def fetch_version(endpoint, key, version_id, http=fetch):
    """取回指定版本，versionId 必须进查询串，不能按键取当前版本。"""
    url = build_object_url(endpoint, key, version_id)
    status, headers, raw = http(url)
    return {'url': url, 'status': status,
            'headers': {name.lower(): value for name, value in headers.items()},
            'raw': raw}


def verify_retrieval(result, requested_version, advertised_checksum=None):
    """根据 HTTP 状态、版本响应头和校验值判断取回结果。

    423 归档取回中 -> restore-pending（等待，不是缺失）；
    404 -> missing；200 时强制核对 x-object-version-id，再按 sha256
    校验值核对正文。ETag 可能带分段后缀，绝不参与内容摘要判断。
    """
    status = result['status']
    headers = result['headers']
    raw = result['raw']
    if status == 423:
        return {'state': 'restore-pending', 'http_status': status,
                'reason': '归档取回尚未完成'}
    if status == 404:
        return {'state': 'missing', 'http_status': status,
                'reason': '来源声明该版本不可用'}
    if status != 200:
        return {'state': 'http-error', 'http_status': status,
                'reason': '未处理的 HTTP 状态'}
    served_version = headers.get('x-object-version-id')
    if served_version is not None and served_version != requested_version:
        return {'state': 'version-mismatch', 'http_status': status,
                'reason': '响应头版本与请求 versionId 不一致',
                'served_version': served_version}
    digest = hashlib.sha256(raw).hexdigest()
    expected = set()
    header_checksum = _parse_checksum(headers.get('x-checksum-sha256')
                                      and 'sha256:' + headers['x-checksum-sha256'])
    manifest_checksum = _parse_checksum(advertised_checksum)
    if header_checksum is not None:
        expected.add(header_checksum[1])
    if manifest_checksum is not None:
        expected.add(manifest_checksum[1])
    if expected:
        if any(value != digest for value in expected):
            return {'state': 'checksum-mismatch', 'http_status': status,
                    'reason': '正文 sha256 与声明校验值不符',
                    'digest': digest, 'expected': sorted(expected)}
        state = 'verified'
    else:
        # 没有任何 sha256 证据：收到正文但无法核实，ETag 不能替代。
        state = 'received-unverified'
    return {'state': state, 'http_status': status, 'digest': digest,
            'bytes': len(raw), 'etag': headers.get('etag')}
