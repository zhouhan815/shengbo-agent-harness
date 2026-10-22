"""Bounded read-only probe of the locally compiled Douyin collector.

No login cookies, platform writes, LLM calls or playback actions. Data from an
external room is never assigned to a Shengbo selling room by this probe.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import time
import urllib.request
from urllib.parse import urlparse

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / 'github-research-output/douyin-io-20261015/verification'


def room_id(value):
    value = value.strip()
    if re.fullmatch(r'\d{5,20}', value):
        return value
    u = urlparse(value)
    if u.scheme == 'https' and u.hostname == 'live.douyin.com' and not u.username and not u.port:
        rid = u.path.strip('/')
        if re.fullmatch(r'\d{5,20}', rid):
            return rid
    raise ValueError('请提供公开抖音网页直播间链接或数字编号')


def integer(value):
    # Unknown is not zero; display strings such as "1.2万" retain their raw form.
    return int(value) if isinstance(value, (int, str)) and not isinstance(value, bool) and re.fullmatch(r'\d+', str(value)) else None


def normalize(message, rid):
    method = message.get('method')
    common = message.get('common') or {}
    user = message.get('user') or {}
    user_key = str(user.get('idStr') or user.get('id') or user.get('nickname') or '')
    result = {'platform':'douyin', 'external_room_id':rid, 'source':'real_external_probe', 'raw_method':method,
              'received_at':datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='milliseconds'),
              'event_id':str(common.get('msgId') or message.get('msgId') or ''),
              'viewer_hash':hashlib.sha256(user_key.encode()).hexdigest()[:12] if user_key else None}
    if method == 'WebcastChatMessage':
        result.update(type='comment', text=str(message.get('content',''))[:500])
    elif method == 'WebcastLikeMessage':
        result.update(type='like', count=integer(message.get('count')), total=integer(message.get('total')))
    elif method == 'WebcastMemberMessage':
        result.update(type='enter')
    elif method == 'WebcastRoomUserSeqMessage':
        result.update(type='audience_snapshot', metric_basis='RoomUserSeq fields; verify against own-room UI',
                      online_total_candidate=integer(message.get('total')),
                      total_user_raw=message.get('totalUser'), total_pv_for_anchor_raw=message.get('totalPvForAnchor'),
                      online_user_for_anchor_raw=message.get('onlineUserForAnchor'))
    elif method == 'WebcastRoomStatsMessage':
        result.update(type='stats_display', display_middle=message.get('displayMiddle'), display_short=message.get('displayShort'))
    else:
        return None
    return result


def health(opener, url):
    with opener.open(url, timeout=.3) as response:
        return response.status == 200


async def probe(args, executable, out):
    rid = room_id(args.room)
    with socket.socket() as check:
        check.bind(('127.0.0.1', args.port))
    config = out / 'probe-config.yaml'
    config.write_text(f'port: "{args.port}"\nprotocol:\n  mode: web\nlog:\n  level: info\nsign:\n  provider: local\ncookie:\n  use_stored: false\nmonitor:\n  poll_interval: "30s"\n  notify_interval: "30s"\n', encoding='utf-8')
    key = secrets.token_urlsafe(32)
    env = {k:v for k,v in os.environ.items() if not k.startswith('APP_')}
    env['APP_API_KEY'] = key
    # Match the existing system route used by urllib; never log its credentials.
    proxies = urllib.request.getproxies()
    for name in ('http','https'):
        if proxies.get(name):
            env[name.upper()+'_PROXY'] = proxies[name]
    env['NO_PROXY'] = 'localhost,127.0.0.1,::1'
    report = {'room_url':'https://live.douyin.com/'+rid, 'duration_requested_seconds':args.seconds,
              'source':'real_external_probe','status':'starting','local_socket_connected':False,'upstream_online':False,
              'methods':{},'counts':{},'unique_comments':0,'duplicates':0,'samples':[], 'statuses':[],
              'llm_calls':0,'platform_writes':0,'uses_login_cookie':False}
    process = None
    with (out/'collector.log').open('wb') as log:
        try:
            process = subprocess.Popen([str(executable),'--config',str(config)],cwd=executable.parent,env=env,
                       stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(60):
                if process.poll() is not None:
                    raise RuntimeError('采集服务启动失败，请查看本次测试日志')
                try:
                    await asyncio.to_thread(health, opener, f'http://127.0.0.1:{args.port}/health')
                    break
                except Exception:
                    await asyncio.sleep(.1)
            headers = {'Authorization':'Bearer '+key}
            counts, methods, seen = Counter(), Counter(), set()
            start = time.monotonic()
            async with connect(f'ws://127.0.0.1:{args.port}/ws/{rid}',additional_headers=headers,open_timeout=10,max_size=4000000) as ws:
                report['local_socket_connected'] = True
                report['status'] = 'observing'
                while time.monotonic()-start < args.seconds:
                    try:
                        raw = await asyncio.wait_for(ws.recv(),timeout=max(.1,args.seconds-(time.monotonic()-start)))
                    except asyncio.TimeoutError:
                        break
                    if raw in ('ping','pong'):
                        continue
                    try: message = json.loads(raw)
                    except (ValueError,TypeError):
                        counts['unparsed'] += 1
                        continue
                    if message.get('type') == 'system':
                        report['statuses'].append({k:message.get(k) for k in ('code','status','message','live','title')})
                        report['upstream_online'] |= bool(message.get('live'))
                        if message.get('status') in ('not_found','error'):
                            break
                        continue
                    methods[message.get('method','unknown')] += 1
                    event = normalize(message,rid)
                    if not event:
                        continue
                    token = (event['raw_method'],event['event_id']) if event['event_id'] not in ('','0') else None
                    if token and token in seen:
                        report['duplicates'] += 1
                        continue
                    if token: seen.add(token)
                    counts[event['type']] += 1
                    if event['type'] == 'comment' and report['unique_comments'] < args.sample_comments:
                        report['samples'].append(event)
                    if event['type'] == 'audience_snapshot':
                        report['latest_audience_snapshot'] = event
                    if event['type'] == 'comment': report['unique_comments'] += 1
                report.update(elapsed_seconds=round(time.monotonic()-start,2),methods=dict(methods),counts=dict(counts),
                              status='comments_received' if report['unique_comments'] else 'no_comments_received')
        except Exception as error:
            report.update(status='error', error=f'{type(error).__name__}: {str(error)[:300]}')
        finally:
            if process and process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill();process.wait(timeout=3)
    (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('samples',)},ensure_ascii=False))
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--room', required=True)
    p.add_argument('--seconds',type=int,default=45)
    p.add_argument('--port',type=int,default=18766)
    p.add_argument('--sample-comments',type=int,default=5)
    p.add_argument('--output',type=Path)
    args=p.parse_args()
    if not 5<=args.seconds<=120 or not 1024<=args.port<=65535 or not 0<=args.sample_comments<=20:
        p.error('测试5至120秒；端口1024至65535；最多保存20条评论样本')
    executable=VERIFY/'collector/douyinLive-localhost.exe'
    if not executable.is_file(): p.error('尚未准备只监听本机地址的采集器；请先完成本次调研目录内的编译步骤')
    out=args.output.resolve() if args.output else VERIFY/('probe-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    out.mkdir(parents=True,exist_ok=True)
    asyncio.run(probe(args,executable,out))


if __name__=='__main__': main()
