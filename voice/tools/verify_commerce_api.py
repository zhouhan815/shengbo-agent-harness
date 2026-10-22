"""Real HTTP and restart smoke checks in a disposable database; no paid APIs."""
from copy import deepcopy
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
    checks = []
    with tempfile.TemporaryDirectory(prefix='shengbo-config-') as directory:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        env = {**os.environ, 'SHENGBO_RUNTIME': directory}
        process = None
        log = open(Path(directory) / 'http.log', 'ab')

        def stop(proc):
            if proc.poll() is None:
                if os.name == 'nt':
                    # A Windows venv launcher can own a second Python process.
                    subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
                else:
                    proc.terminate()
                proc.wait(timeout=10)

        def request(path, method='GET', data=None, expected=200):
            req = urllib.request.Request(base + path, method=method,
                data=None if data is None else json.dumps(data, ensure_ascii=False).encode(),
                headers={'Content-Type':'application/json'})
            try:
                with OPENER.open(req, timeout=10) as response:
                    status, body = response.status, json.load(response)
            except urllib.error.HTTPError as e:
                status, body = e.code, json.load(e)
            assert status == expected, (path, status, body)
            return body

        def start():
            proc = subprocess.Popen([sys.executable,'-m','uvicorn','app.server:app','--host','127.0.0.1','--port',str(port),'--no-access-log'],
                cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            for _ in range(60):
                if proc.poll() is not None:
                    raise RuntimeError('临时验证服务启动失败')
                try:
                    request('/api/library')
                    return proc
                except (OSError, urllib.error.URLError):
                    time.sleep(.2)
            stop(proc)
            raise RuntimeError('临时验证服务启动超时')

        try:
            process = start()
            p = request('/api/products','POST',{'name':'HTTP验证商品','price':149,'sku_id':'HTTP-SKU',
                'core_selling_points':['轻便','易清洗'],'specifications':[{'name':'颜色','value':'蓝色'}],
                'shipping_time':'72小时发货','material':'EVA','faq':'问：颜色？\n答：蓝色。'})
            assert p['selling_points'] == '轻便\n易清洗'
            checks.append('商品结构化字段经HTTP创建并回显')
            a = request('/api/rooms','POST',{'name':'HTTP A','product_ids':[p['id']]})
            b = request('/api/rooms','POST',{'name':'HTTP B','product_ids':[p['id']]})
            c = deepcopy(a['commerce'])
            c['coupon'].update(mode='coupon',amount=10,minimum_spend=100,condition='满100减10')
            c['product_overrides'][p['id']] = {'mode':'price','price':129,'exclusive':False,
                'shipping_override':True,'shipping_time':'本场12小时发货','gifts':'送收纳袋1个'}
            c['scene'] = 'HTTP验证专场'
            c['knowledge_documents'] = [{'id':'roomdoc','title':'活动说明','content':'本场主题为夏季用品。'}]
            saved = request(f"/api/rooms/{a['id']}",'PUT',{'commerce':c})
            assert saved['commerce']['product_overrides'][p['id']]['shipping_time'] == '本场12小时发货'
            quote = request(f"/api/rooms/{a['id']}/price-preview",'POST',{'product_id':p['id']})
            assert quote['final_price'] == 119 and quote['base_price'] == 129
            assert request(f"/api/rooms/{b['id']}/price-preview",'POST',{'product_id':p['id']})['final_price'] == 149
            checks.append('单品价格、全场券与直播间隔离119/149验证通过')
            knowledge = request(f"/api/rooms/{a['id']}/reply-knowledge")
            assert knowledge['products'][0]['price_quote']['final_price'] == 119
            assert any(d['content']=='本场12小时发货' and d['scope']=='room' for d in knowledge['documents'])
            checks.append('问答读取本场生效报价、发货规则与知识文档')
            request('/api/products','POST',{'name':'','price':1},expected=422)
            bad = deepcopy(c);bad['product_overrides'][p['id']]['price']=150
            request(f"/api/rooms/{a['id']}",'PUT',{'commerce':bad},expected=400)
            bad = deepcopy(c);bad['coupon'].update(mode='discount',discount=1.2)
            request(f"/api/rooms/{a['id']}",'PUT',{'commerce':bad},expected=400)
            checks.append('真实HTTP拒绝必填缺失、超原价与非法折扣')
            schema=request('/api/commerce/schema')
            assert 'sku_id' in schema['product']['properties']
            assert 'product_overrides' in schema['room_commerce']['properties']
            checks.append('配置契约可查询')
            stop(process)
            process=start()
            reloaded=request(f"/api/rooms/{a['id']}/commerce")
            assert reloaded['products'][0]['quote']['final_price']==119
            assert reloaded['products'][0]['shipping_time']=='本场12小时发货'
            checks.append('服务完整重启后引用、优惠和发货覆写保留')
            request('/api/products/'+p['id'],'PUT',{'name':p['name'],'price':149,'sale_status':'unavailable'})
            request('/api/rooms','POST',{'name':'不可售验证','product_ids':[p['id']]},expected=400)
            checks.append('不可售商品不能被新直播间引用')
        finally:
            if process is not None and process.poll() is None:
                stop(process)
            log.close()
    report={'passed':True,'checks':checks,'external_model_calls':0,'production_database_modified':False}
    destination=ROOT/'runtime/prd-reference-20261015/http-verification.json'
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
