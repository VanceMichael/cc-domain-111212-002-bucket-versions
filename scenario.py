import base64
import hashlib
import json
from sampledata import samples, MANIFEST_PAYLOAD


def response(raw, status=200, headers=None, **options):
    return dict(base64=base64.b64encode(raw).decode(), status=status,
                headers=headers or {}, chunk_bytes=3, **options)


def scenario():
    fixtures = samples()
    routes = {}
    routes['GET /list'] = [response(fixtures['sample.json'], status=200)]
    # 键名含斜杠与空格：续页查询串必须做 URL 编码，且双标记成对出现。
    routes['GET /list?key-marker=cargo%2Fship%20A.csv&version-id-marker=v2'] = [
        response(fixtures['page2.json'], status=200)]
    # 合作方先删后补：先遇到删除标记，再取回被删前的历史版本 v2。
    # 第一次 423 表示归档取回中（等待，不是缺失），第二次返回正文。
    routes['GET /object?key=cargo%2Fship%20A.csv&versionId=v2'] = [
        response(b'archive restore pending', status=423),
        response(MANIFEST_PAYLOAD, headers={
            'x-object-version-id': 'v2',
            'x-checksum-sha256': hashlib.sha256(MANIFEST_PAYLOAD).hexdigest(),
            # 刻意保留分段后缀，证明 ETag 不能当内容摘要使用。
            'ETag': 'multipart-not-md5-2'})]
    routes['GET /object?key=cargo%2Fship%20A.csv&versionId=v1'] = [
        response(b'previous,cargo')]
    routes['GET /object?key=cargo%2Fnew.csv&versionId=new1'] = [
        response(b'id,note\n1,current\n', headers={'x-object-version-id': 'new1'})]
    return routes


def main():
    import argparse
    from transport import ReplaySource
    parser = argparse.ArgumentParser(description='来源协议本地演练入口')
    parser.add_argument('--export', action='store_true')
    args = parser.parse_args()
    routes = scenario()
    if args.export:
        print(json.dumps(routes, ensure_ascii=False, sort_keys=True))
        return
    with ReplaySource(routes) as server:
        print(json.dumps({'url': server.url, 'routes': list(routes)}, ensure_ascii=False), flush=True)
        try:
            server.thread.join()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
