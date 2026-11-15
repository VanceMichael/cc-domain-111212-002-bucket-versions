import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import app
from sampledata import samples, MANIFEST_PAYLOAD
from scenario import scenario
from storage import Archive
from transport import ReplaySource


class ArchiveObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'store.sqlite3'
        self.fixtures = samples()

    def tearDown(self):
        self.temp.cleanup()

    def _save_full(self, archive, source='partner'):
        pages = [
            {'seq': 1, 'url': 'http://src/list', 'raw': self.fixtures['sample.json'],
             'bytes': len(self.fixtures['sample.json']),
             'digest': 'd1', 'key_marker': None, 'version_id_marker': None,
             'entries': [
                 {'key': 'cargo/ship A.csv', 'version_id': 'v3', 'kind': 'delete',
                  'is_latest': True, 'page_seq': 1, 'etag': None, 'checksum': None,
                  'restore': None, 'last_modified': None},
                 {'key': 'cargo/ship A.csv', 'version_id': 'v2', 'kind': 'version',
                  'is_latest': False, 'page_seq': 1, 'etag': 'not-an-md5-2',
                  'checksum': 'sha256:known', 'restore': 'ongoing',
                  'last_modified': None}]},
            {'seq': 2, 'url': 'http://src/list?m', 'raw': self.fixtures['page2.json'],
             'bytes': len(self.fixtures['page2.json']),
             'digest': 'd2', 'key_marker': 'cargo/ship A.csv',
             'version_id_marker': 'v2',
             'entries': [
                 {'key': 'cargo/ship A.csv', 'version_id': 'v1', 'kind': 'version',
                  'is_latest': False, 'page_seq': 2, 'etag': None, 'checksum': None,
                  'restore': None, 'last_modified': None},
                 {'key': 'cargo/new.csv', 'version_id': 'new1', 'kind': 'version',
                  'is_latest': True, 'page_seq': 2, 'etag': None, 'checksum': None,
                  'restore': None, 'last_modified': None}]}]
        observation_id = archive.start_observation(source, 'http://src')
        for page in pages:
            archive.save_page(observation_id, page, page.pop('raw'))
        archive.finish_observation(observation_id, truncated=False)
        return observation_id

    def test_pages_versions_and_span_pages_persisted(self):
        with Archive(self.path) as archive:
            observation_id = self._save_full(archive)
            versions = archive.list_versions(observation_id)
            self.assertEqual(len(versions), 4)
            ship = [v for v in versions if v['key'] == 'cargo/ship A.csv']
            self.assertEqual([v['version_id'] for v in ship], ['v3', 'v2', 'v1'])
            self.assertEqual(archive.page_raw(observation_id, 1),
                             self.fixtures['sample.json'])
            summary = archive.get_observation(observation_id)['summary']
            self.assertEqual(summary['versions'], 3)
            self.assertEqual(summary['delete_markers'], 1)
            self.assertEqual(summary['distinct_keys'], 2)

    def test_is_latest_frozen_at_observation_time(self):
        with Archive(self.path) as archive:
            observation_id = self._save_full(archive)
            versions = {v['version_id']: v for v in
                        archive.list_versions(observation_id)}
            self.assertTrue(versions['v3']['is_latest'])
            self.assertFalse(versions['v1']['is_latest'])
            self.assertTrue(versions['new1']['is_latest'])

    def test_retrieval_state_machine_and_no_false_missing(self):
        with Archive(self.path) as archive:
            observation_id = self._save_full(archive)
            # 未取回：仅在清单发现，不能标缺失。
            history = archive.export_history(observation_id)
            v2 = next(v for v in history['versions'] if v['version_id'] == 'v2')
            self.assertEqual(v2['retrieval_state'], 'manifest-only')
            # 423 等待。
            pending = {'state': 'restore-pending', 'http_status': 423,
                       'reason': '归档取回尚未完成'}
            archive.record_retrieval(
                observation_id, 'cargo/ship A.csv', 'v2',
                'http://src/object?versionId=v2', pending)
            history = archive.export_history(observation_id)
            v2 = next(v for v in history['versions'] if v['version_id'] == 'v2')
            self.assertEqual(v2['retrieval_state'], 'restore-pending')
            # 404 才是缺失。
            archive.record_retrieval(
                observation_id, 'cargo/new.csv', 'new1',
                'http://src/object?versionId=new1',
                {'state': 'missing', 'http_status': 404,
                 'reason': '来源声明该版本不可用'})
            history = archive.export_history(observation_id)
            new1 = next(v for v in history['versions']
                        if v['version_id'] == 'new1')
            self.assertEqual(new1['retrieval_state'], 'missing')
            # 核实成功：仅 verified 才留原始字节。
            import hashlib
            digest = hashlib.sha256(MANIFEST_PAYLOAD).hexdigest()
            archive.record_retrieval(
                observation_id, 'cargo/ship A.csv', 'v2',
                'http://src/object?versionId=v2',
                {'state': 'verified', 'http_status': 200, 'digest': digest,
                 'bytes': len(MANIFEST_PAYLOAD), 'etag': 'multipart-not-md5-2'},
                MANIFEST_PAYLOAD)
            retrieval = archive.get_retrieval(
                observation_id, 'cargo/ship A.csv', 'v2')
            self.assertEqual(retrieval['state'], 'verified')
            self.assertTrue(retrieval['has_raw'])

    def test_delete_marker_cannot_be_fetched_and_is_not_empty_file(self):
        with Archive(self.path) as archive:
            observation_id = self._save_full(archive)
            with self.assertRaises(ValueError):
                archive.record_retrieval(
                    observation_id, 'cargo/ship A.csv', 'v3',
                    'http://src/object', {'state': 'verified', 'http_status': 200})
            current = archive.export_current(observation_id)
            # 每个键取其 is_latest 条目：ship A 的当前是删除标记，不进文件集合；
            # new.csv 的当前是普通版本 new1，保留且仅在清单发现。
            self.assertEqual(
                [f['version_id'] for f in current['current_files']], ['new1'])
            self.assertEqual(
                [d['version_id'] for d in current['current_delete_markers']], ['v3'])

    def test_resume_is_a_separate_observation_not_atomic_snapshot(self):
        with Archive(self.path) as archive:
            first = archive.start_observation('partner', 'http://src')
            archive.save_page(first, {
                'seq': 1, 'url': 'http://src/list',
                'raw': self.fixtures['sample.json'],
                'bytes': 1, 'digest': 'd', 'key_marker': None,
                'version_id_marker': None,
                'entries': [{'key': 'cargo/ship A.csv', 'version_id': 'v3',
                             'kind': 'delete', 'is_latest': True, 'page_seq': 1,
                             'etag': None, 'checksum': None, 'restore': None,
                             'last_modified': None}]},
                self.fixtures['sample.json'])
            archive.finish_observation(first, truncated=True,
                                       next_key_marker='cargo/ship A.csv',
                                       next_version_id_marker='v2')
            second = archive.start_observation(
                'partner', 'http://src', resume_of=first,
                key_marker='cargo/ship A.csv', version_id_marker='v2')
            archive.finish_observation(second, truncated=False)
            observations = archive.list_observations()
            self.assertEqual([o['id'] for o in observations], [first, second])
            self.assertEqual(observations[1]['resume_of'], first)
            # 两个批次各自独立，不把跨批次版本拼成同一快照。
            self.assertEqual(len(archive.list_versions(first)), 1)
            self.assertEqual(archive.list_versions(second), [])

    def test_durable_across_reopen(self):
        with Archive(self.path) as archive:
            observation_id = self._save_full(archive)
        with Archive(self.path) as archive:
            self.assertEqual(
                len(archive.list_versions(observation_id)), 4)
            self.assertEqual(
                archive.get_observation(observation_id)['summary']['pages'], 2)


class CliObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / 'cli.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = app.main(list(argv) + ['--database', self.database])
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue())

    def test_full_cli_flow_against_loopback_source(self):
        with ReplaySource(scenario()) as server:
            observed = self._run('observe', server.url, '--source', 'partner')
            observation_id = observed['observation_id']
            self.assertFalse(observed['truncated'])
            self.assertEqual(observed['summary']['pages'], 2)
            # 423 等待，再核实。
            pending = self._run('fetch-object', str(observation_id),
                                '--key', 'cargo/ship A.csv', '--version-id', 'v2')
            self.assertEqual(pending['verification']['state'], 'restore-pending')
            verified = self._run('fetch-object', str(observation_id),
                                 '--key', 'cargo/ship A.csv', '--version-id', 'v2')
            self.assertEqual(verified['verification']['state'], 'verified')
            current = self._run('export-current', str(observation_id))
            self.assertEqual(
                [f['version_id'] for f in current['current_files']], ['new1'])
            self.assertEqual(
                [d['version_id'] for d in current['current_delete_markers']], ['v3'])
            history = self._run('export-history', str(observation_id))
            states = {v['version_id']: v['retrieval_state']
                      for v in history['versions']}
            self.assertEqual(states, {
                'v3': 'delete-marker', 'v2': 'verified',
                'v1': 'manifest-only', 'new1': 'manifest-only'})

    def test_cli_pause_and_resume_keeps_batches_distinct(self):
        with ReplaySource(scenario()) as server:
            first = self._run('observe', server.url, '--source', 'partner',
                              '--max-pages', '1')
            self.assertTrue(first['truncated'])
            resumed = self._run('resume', str(first['observation_id']))
            self.assertEqual(resumed['resume_of'], first['observation_id'])
            self.assertFalse(resumed['truncated'])
            listing = self._run('observations')
            self.assertEqual(len(listing), 2)


if __name__ == '__main__':
    unittest.main()
