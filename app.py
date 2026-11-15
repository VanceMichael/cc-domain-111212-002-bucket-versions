import argparse
import json
from pathlib import Path
import sys
import tempfile

from source import inspect_bytes
from sampledata import samples
from storage import Archive
from transport import ReplaySource
from collector import collect_pages, fetch_version, verify_retrieval

MAX_INPUT = 16*1024*1024


def read_input(path):
    with Path(path).open('rb') as handle:
        data = handle.read(MAX_INPUT+1)
    if len(data) > MAX_INPUT:
        raise ValueError('输入超过接收限制')
    return data


def run_observation(archive, source, endpoint, *, resume_of=None,
                    key_marker=None, version_id_marker=None, max_pages=None):
    """执行一次观察并把每一页原始清单与版本条目落库。

    暂停（max_pages 受限或来源仍截断）时只返回续跑标记；重开由调用方
    另建观察批次，批次间用 resume_of 关联，不伪装成一次原子快照。
    """
    observation_id = archive.start_observation(
        source, endpoint, resume_of=resume_of,
        key_marker=key_marker, version_id_marker=version_id_marker)
    result = collect_pages(endpoint, key_marker=key_marker,
                           version_id_marker=version_id_marker, max_pages=max_pages)
    # 解析阶段拿到的原始页直接留存；条目按 page_seq 归回所属页，
    # 同一键的版本允许跨页，IsLatest 保持该页服务端观察值不回写。
    for page in result['pages']:
        page_raw = page.pop('raw')
        parsed = {**page, 'entries': [e for e in result['entries']
                                      if e['page_seq'] == page['seq']]}
        archive.save_page(observation_id, parsed, page_raw)
    archive.finish_observation(
        observation_id, truncated=result['truncated'],
        next_key_marker=result['next_key_marker'],
        next_version_id_marker=result['next_version_id_marker'])
    return observation_id, result


def resume_from(archive, observation_id):
    """从已暂停观察的双标记开新批次，返回新观察 id。"""
    previous = archive.get_observation(observation_id)
    if not previous['truncated']:
        raise ValueError('该观察已完整，无需续跑')
    new_id, _ = run_observation(
        archive, previous['source'], previous['endpoint'],
        resume_of=observation_id, key_marker=previous['next_key_marker'],
        version_id_marker=previous['next_version_id_marker'])
    return new_id


def retrieve(archive, observation_id, key, version_id):
    observation = archive.get_observation(observation_id)
    version = archive.find_version(observation_id, key, version_id)
    if version['kind'] == 'delete':
        raise ValueError('删除标记没有对象正文，不能取回')
    result = fetch_version(observation['endpoint'], key, version_id)
    verification = verify_retrieval(result, version_id, version.get('checksum'))
    saved_raw = result['raw'] if verification['state'] == 'verified' else None
    record = archive.record_retrieval(
        observation_id, key, version_id, result['url'], verification, saved_raw)
    return dict(key=key, version_id=version_id, url=result['url'],
                verification=verification, retrieval=record)


def demo():
    from scenario import scenario
    routes = scenario()
    with tempfile.TemporaryDirectory() as directory, ReplaySource(routes) as server:
        database = Path(directory)/'archive.sqlite3'
        with Archive(database) as archive:
            observation_id, listing = run_observation(
                archive, 'local-demo', server.url)
            observation = archive.get_observation(observation_id)
            # v2 先遇到归档取回中（423），重试后核实成功。
            first = retrieve(archive, observation_id, 'cargo/ship A.csv', 'v2')
            pending_seen = first['verification']['state']
            second = retrieve(archive, observation_id, 'cargo/ship A.csv', 'v2')
            # v1 无 sha256 证据、ETag 也不是摘要：只能标已收到未核实。
            v1 = retrieve(archive, observation_id, 'cargo/ship A.csv', 'v1')
            current = archive.export_current(observation_id)
            history = archive.export_history(observation_id)
            return dict(
                endpoint=server.url,
                observation=observation,
                pages=len(listing['pages']),
                entries=len(listing['entries']),
                truncated=listing['truncated'],
                v2_first_state=pending_seen,
                v2_final_state=second['verification']['state'],
                v1_state=v1['verification']['state'],
                current_files=[f['key'] + '@' + f['version_id']
                               for f in current['current_files']],
                current_delete_markers=[d['version_id']
                                        for d in current['current_delete_markers']],
                history_states={v['key'] + '@' + v['version_id']: v['retrieval_state']
                                for v in history['versions']},
                requests=len(server.requests),
                same_capture=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='版本清单采集、取回与导出')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'capture'):
        command = commands.add_parser(name)
        command.add_argument('path')
        if name == 'capture':
            command.add_argument('--database', required=True)
            command.add_argument('--source', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--database', required=True)
    fixtures = commands.add_parser('fixtures')
    fixtures.add_argument('directory')
    commands.add_parser('demo')

    observe = commands.add_parser('observe', help='采集一次版本清单观察')
    observe.add_argument('endpoint')
    observe.add_argument('--database', required=True)
    observe.add_argument('--source', required=True)
    observe.add_argument('--max-pages', type=int, default=None)
    observe.add_argument('--key-marker')
    observe.add_argument('--version-id-marker')

    resume = commands.add_parser('resume', help='从暂停观察的双标记另开批次')
    resume.add_argument('observation_id', type=int)
    resume.add_argument('--database', required=True)

    observations = commands.add_parser('observations', help='列出观察批次')
    observations.add_argument('--database', required=True)

    show = commands.add_parser('observation', help='查看单个观察与版本条目')
    show.add_argument('observation_id', type=int)
    show.add_argument('--database', required=True)

    fetch_cmd = commands.add_parser('fetch-object', help='按 versionId 取回并校验版本')
    fetch_cmd.add_argument('observation_id', type=int)
    fetch_cmd.add_argument('--key', required=True)
    fetch_cmd.add_argument('--version-id', required=True)
    fetch_cmd.add_argument('--database', required=True)

    export_current = commands.add_parser('export-current', help='导出观察下当前文件集合')
    export_current.add_argument('observation_id', type=int)
    export_current.add_argument('--database', required=True)

    export_history = commands.add_parser('export-history', help='导出观察下历史版本')
    export_history.add_argument('observation_id', type=int)
    export_history.add_argument('--database', required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == 'demo':
            result = demo()
        elif args.command == 'fixtures':
            destination = Path(args.directory)
            destination.mkdir(parents=True, exist_ok=True)
            for name, raw in samples().items():
                path = destination/name
                if path.exists():
                    raise ValueError('不覆盖已有夹具文件：'+name)
                path.write_bytes(raw)
            result = dict(files=sorted(samples()))
        elif args.command == 'list':
            with Archive(args.database) as archive:
                result = archive.list()
        elif args.command == 'observe':
            with Archive(args.database) as archive:
                observation_id, listing = run_observation(
                    archive, args.source, args.endpoint,
                    max_pages=args.max_pages, key_marker=args.key_marker,
                    version_id_marker=args.version_id_marker)
                result = dict(observation_id=observation_id,
                              **archive.get_observation(observation_id))
        elif args.command == 'resume':
            with Archive(args.database) as archive:
                new_id = resume_from(archive, args.observation_id)
                result = dict(observation_id=new_id,
                              **archive.get_observation(new_id))
        elif args.command == 'observations':
            with Archive(args.database) as archive:
                result = archive.list_observations()
        elif args.command == 'observation':
            with Archive(args.database) as archive:
                result = dict(archive.get_observation(args.observation_id),
                              versions=archive.list_versions(args.observation_id))
        elif args.command == 'fetch-object':
            with Archive(args.database) as archive:
                result = retrieve(archive, args.observation_id,
                                  args.key, args.version_id)
        elif args.command == 'export-current':
            with Archive(args.database) as archive:
                result = archive.export_current(args.observation_id)
        elif args.command == 'export-history':
            with Archive(args.database) as archive:
                result = archive.export_history(args.observation_id)
        else:
            raw = read_input(args.path)
            rows = inspect_bytes(raw)
            if args.command == 'inspect':
                result = rows
            else:
                with Archive(args.database) as archive:
                    result = dict(capture_id=archive.save(args.source, raw, rows), rows=len(rows))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps(dict(error=type(error).__name__, reason=str(error)),
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
