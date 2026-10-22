"""Verify HTTP pairing/lifecycle in disposable storage; optional public-room read."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--room', help='Optional public Douyin room URL/number for a bounded real check')
    parser.add_argument('--seconds', type=int, default=25)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 120:
        parser.error('real verification duration must be 5–120 seconds')
    report = {'checks': [], 'uses_login_cookie': False, 'llm_calls': 0, 'platform_writes': 0,
              'production_database_modified': False}
    destination = ROOT/'runtime/douyin-source-verification'
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='shengbo-source-') as directory:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        env = {**os.environ, 'SHENGBO_RUNTIME': directory}
        log = open(destination/'http-server.log', 'ab')
        process = None

        def request(path, method='GET', data=None, expected=200):
            req = urllib.request.Request(origin+path, method=method,
                data=None if data is None else json.dumps(data).encode(),
                headers={'Content-Type': 'application/json'})
            try:
                with OPENER.open(req, timeout=20) as response:
                    status, body = response.status, json.load(response)
            except urllib.error.HTTPError as e:
                status, body = e.code, json.load(e)
            assert status == expected, (path, status, body)
            return body

        def start():
            child = subprocess.Popen([sys.executable,'-m','uvicorn','app.server:app','--host','127.0.0.1','--port',str(port),'--no-access-log'],
                cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            for _ in range(60):
                if child.poll() is not None:
                    raise RuntimeError('临时HTTP验证服务启动失败')
                try:
                    request('/api/library')
                    return child
                except (OSError, urllib.error.URLError):
                    time.sleep(.2)
            stop(child)
            raise RuntimeError('临时服务启动超时')

        def stop(child):
            if child and child.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                else:
                    child.terminate()
                child.wait(timeout=10)

        try:
            process = start()
            a = request('/api/rooms','POST',{'name':'来源HTTP A'})['id']
            b = request('/api/rooms','POST',{'name':'来源HTTP B'})['id']
            path = f'/api/rooms/{a}/live-source'
            view = request(path)
            assert view['status']['connected'] is False
            assert view['events'] == []
            request('/api/rooms/missing/live-source', expected=404)
            pair = {'room':args.room or 'https://live.douyin.com/505233570304','purpose':'external_test'}
            view = request(path,'PUT',pair)
            extid = view['config']['external_room_id']
            assert extid and extid != a
            assert not request(f'/api/rooms/{b}/live-source')['config']['external_room_id']
            report['checks'].append('HTTP保存外部房间引用，且声播直播间之间独立')
            for bad in ['https://evil.example/505233570304','https://127.0.0.1/505233570304','https://live.douyin.com:8765/505233570304']:
                request(path,'PUT',{'room':bad},expected=400)
            report['checks'].append('拒绝外部域名、本机URL、任意端口等非抖音房间输入')
            request(path+'/disconnect','POST',{})
            assert request(path)['status']['connected'] is False
            report['checks'].append('未开声播或未连接时可独立配置、读取历史和安全断开')
            history_count = 0
            if args.room:
                seen, kinds, states = set(), Counter(), []
                request(path+'/connect','POST',pair)
                start_time, cursor = time.monotonic(), 0
                while time.monotonic()-start_time < args.seconds:
                    view = request(path+f'/events?after={cursor}')
                    state = view['status']['state']
                    if not states or states[-1] != state:
                        states.append(state)
                    for event in view['events']:
                        if event['id'] not in seen:
                            seen.add(event['id']);kinds[event['type']] += 1
                    cursor = max(cursor, view['next_cursor'])
                    if state in {'offline','error'}:
                        break
                    time.sleep(1)
                report['real_probe'] = {'external_room_id':extid,'states':states,'counts':dict(kinds),
                    'received_events':len(seen),'elapsed_seconds':round(time.monotonic()-start_time,2),
                    'status_at_end':view['status']}
                # Only status summaries, no viewer names or comment text in this report.
                request(path+'/disconnect','POST',{})
                stopped = request(path)
                assert not stopped['status']['connected']
                history_count = len(stopped['events'])
                report['checks'].append('真实公开直播间读取已尝试，停止后连接释放且历史可见')
            stop(process); process = start()
            view = request(path)
            assert view['config']['external_room_id'] == extid
            assert not view['status']['connected']
            if history_count:
                assert view['events']
            report['checks'].append('服务重启后配对与历史保留，采集需手动开启')
            report['passed'] = True
        finally:
            try:
                if process is not None and process.poll() is None and 'a' in locals():
                    request(f'/api/rooms/{a}/live-source/disconnect','POST',{})
            finally:
                stop(process);log.close()
    report_name = 'http-real-result.json' if args.room else 'http-result.json'
    (destination/report_name).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
