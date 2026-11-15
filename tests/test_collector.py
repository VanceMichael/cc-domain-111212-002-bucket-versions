import hashlib
import json
import unittest

from collector import (ListingError, build_list_url, build_object_url,
                       collect_pages, parse_page, verify_retrieval)
from sampledata import samples


def make_http(pages, *, statuses=None):
    """用内存页构造假 http，记录每次请求 URL，避免真实端口。"""
    calls = []
    statuses = statuses or {}

    def http(url):
        calls.append(url)
        index = len(calls) - 1
        if url in statuses:
            return statuses[url], {}, b'blocked'
        raw = pages[min(index, len(pages) - 1)]
        return 200, {}, raw
    http.calls = calls
    return http


class ParsePageTests(unittest.TestCase):
    def test_decodes_key_once_and_keeps_delete_marker(self):
        page = parse_page(samples()['sample.json'])
        self.assertTrue(page['is_truncated'])
        self.assertEqual(page['next_key_marker'], 'cargo/ship A.csv')
        self.assertEqual(page['next_version_id_marker'], 'v2')
        delete, version = page['entries']
        self.assertEqual(delete['kind'], 'delete')
        self.assertEqual(delete['key'], 'cargo/ship A.csv')
        self.assertTrue(delete['is_latest'])
        self.assertEqual(version['version_id'], 'v2')

    def test_unknown_kind_and_missing_markers_rejected(self):
        bad = {'entries': [{'key': 'a', 'versionId': 'x', 'kind': 'folder',
                            'isLatest': True}]}
        with self.assertRaises(ValueError):
            parse_page(json.dumps(bad).encode())
        truncated = {'entries': [], 'isTruncated': True}
        with self.assertRaises(ListingError):
            parse_page(json.dumps(truncated).encode())

    def test_last_modified_must_not_define_identity(self):
        # 同一键两个版本即便 lastModified 相同，也必须各自保留。
        page = {'entries': [
            {'key': 'k', 'versionId': 'a', 'kind': 'version', 'isLatest': True,
             'lastModified': '2026-11-14T00:00:00Z'},
            {'key': 'k', 'versionId': 'b', 'kind': 'version', 'isLatest': False,
             'lastModified': '2026-11-14T00:00:00Z'}],
            'isTruncated': False}
        result = parse_page(json.dumps(page).encode())
        self.assertEqual([e['version_id'] for e in result['entries']], ['a', 'b'])


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = samples()

    def test_dual_markers_continue_and_same_key_spans_pages(self):
        http = make_http([self.fixtures['sample.json'], self.fixtures['page2.json']])
        result = collect_pages('http://src', http=http)
        self.assertFalse(result['truncated'])
        self.assertEqual(len(http.calls), 2)
        expected = build_list_url(
            'http://src', 'cargo/ship A.csv', 'v2')
        self.assertEqual(http.calls[1], expected)
        # 键名编码出现在实际请求里，双标记成对。
        self.assertIn('key-marker=cargo%2Fship%20A.csv', http.calls[1])
        self.assertIn('version-id-marker=v2', http.calls[1])
        # 同一键三个版本（含删除标记）全部保留，跨页不丢失。
        ship = [e for e in result['entries'] if e['key'] == 'cargo/ship A.csv']
        self.assertEqual([e['version_id'] for e in ship], ['v3', 'v2', 'v1'])
        self.assertEqual({e['page_seq'] for e in ship}, {1, 2})

    def test_max_pages_pauses_and_exposes_resume_markers(self):
        http = make_http([self.fixtures['sample.json'], self.fixtures['page2.json']])
        first = collect_pages('http://src', max_pages=1, http=http)
        self.assertTrue(first['truncated'])
        self.assertEqual(first['next_key_marker'], 'cargo/ship A.csv')
        self.assertEqual(first['next_version_id_marker'], 'v2')
        second = collect_pages('http://src',
                               key_marker=first['next_key_marker'],
                               version_id_marker=first['next_version_id_marker'],
                               http=http)
        self.assertFalse(second['truncated'])
        self.assertTrue(http.calls[1].endswith(
            '?key-marker=cargo%2Fship%20A.csv&version-id-marker=v2'))

    def test_is_latest_observation_not_rewritten_by_older_versions(self):
        http = make_http([self.fixtures['sample.json'], self.fixtures['page2.json']])
        result = collect_pages('http://src', http=http)
        latest = {(e['key'], e['version_id']): e['is_latest']
                  for e in result['entries']}
        # 第二页抓到更老的 v1/new1，第一页 v3 的 is_latest 观察保持不动。
        self.assertTrue(latest[('cargo/ship A.csv', 'v3')])
        self.assertFalse(latest[('cargo/ship A.csv', 'v2')])
        self.assertFalse(latest[('cargo/ship A.csv', 'v1')])
        self.assertTrue(latest[('cargo/new.csv', 'new1')])

    def test_non_200_is_not_an_empty_listing(self):
        http = make_http([self.fixtures['sample.json']],
                         statuses={build_list_url('http://src'): 500})
        with self.assertRaises(ListingError):
            collect_pages('http://src', http=http)


class RetrievalVerificationTests(unittest.TestCase):
    def test_restore_pending_is_not_missing(self):
        result = {'status': 423, 'headers': {}, 'raw': b''}
        verdict = verify_retrieval(result, 'v2')
        self.assertEqual(verdict['state'], 'restore-pending')

    def test_404_is_missing(self):
        verdict = verify_retrieval({'status': 404, 'headers': {}, 'raw': b''}, 'v9')
        self.assertEqual(verdict['state'], 'missing')

    def test_version_header_mismatch_detected(self):
        result = {'status': 200,
                  'headers': {'x-object-version-id': 'other'}, 'raw': b'x'}
        verdict = verify_retrieval(result, 'v2')
        self.assertEqual(verdict['state'], 'version-mismatch')

    def test_checksum_verified_despite_non_md5_etag(self):
        payload = b'synthetic,cargo\nA,12\n'
        digest = hashlib.sha256(payload).hexdigest()
        result = {'status': 200,
                  'headers': {'x-object-version-id': 'v2',
                              'x-checksum-sha256': digest,
                              'etag': 'multipart-not-md5-2'},
                  'raw': payload}
        verdict = verify_retrieval(result, 'v2',
                                   advertised_checksum='sha256:' + digest)
        self.assertEqual(verdict['state'], 'verified')
        self.assertEqual(verdict['digest'], digest)

    def test_etag_alone_cannot_verify(self):
        # 只有非 MD5 的 ETag、没有 sha256：收到但不可核实。
        result = {'status': 200,
                  'headers': {'etag': 'multipart-not-md5-2'},
                  'raw': b'previous,cargo'}
        verdict = verify_retrieval(result, 'v1')
        self.assertEqual(verdict['state'], 'received-unverified')

    def test_checksum_mismatch_detected(self):
        result = {'status': 200,
                  'headers': {'x-checksum-sha256': '0' * 64},
                  'raw': b'corrupt'}
        verdict = verify_retrieval(result, 'v2')
        self.assertEqual(verdict['state'], 'checksum-mismatch')

    def test_object_url_always_carries_version_id(self):
        url = build_object_url('http://src', 'cargo/ship A.csv', 'v2')
        self.assertEqual(
            url, 'http://src/object?key=cargo%2Fship%20A.csv&versionId=v2')


if __name__ == '__main__':
    unittest.main()
