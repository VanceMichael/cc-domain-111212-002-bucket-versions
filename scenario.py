import base64
import json
from sampledata import samples

def response(raw, status=200, headers=None, **options):
    return dict(base64=base64.b64encode(raw).decode(),status=status,
                headers=headers or {},chunk_bytes=3,**options)

def scenario():
    fixtures = samples()
    routes = {}
    routes['GET /list'] = [response(fixtures['sample.json'],status=200)]
    routes['GET /list?key-marker=cargo%2Fship%20A.csv&version-id-marker=v2'] = [response(fixtures['page2.json'],status=200)]
    routes['GET /object?key=cargo%2Fship%20A.csv&versionId=v2'] = [response(b'archive restore pending',status=423)]

    import hashlib
    payload = b'synthetic,cargo\nA,12\n'
    routes['GET /object?key=cargo%2Fship%20A.csv&versionId=v2'].append(
        response(payload, headers={'x-object-version-id':'v2','x-checksum-sha256':hashlib.sha256(payload).hexdigest(),'ETag':'multipart-not-md5-2'}))
    routes['GET /object?key=cargo%2Fship%20A.csv&versionId=v1'] = [response(b'previous,cargo')]
    return routes

def main():
    import argparse
    from transport import ReplaySource
    parser = argparse.ArgumentParser(description='来源协议本地演练入口')
    parser.add_argument('--export', action='store_true')
    args = parser.parse_args()
    routes = scenario()
    if args.export:
        print(json.dumps(routes,ensure_ascii=False,sort_keys=True))
        return
    with ReplaySource(routes) as server:
        print(json.dumps({'url':server.url,'routes':list(routes)},ensure_ascii=False),flush=True)
        try:
            server.thread.join()
        except KeyboardInterrupt:
            pass

if __name__ == '__main__':
    main()
