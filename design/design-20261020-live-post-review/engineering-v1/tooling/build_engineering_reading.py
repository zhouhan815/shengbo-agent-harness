"""Render/verify the engineering documentation only; never imports product runtime."""
from pathlib import Path
from html import escape
from datetime import datetime, timezone
from xml.etree import ElementTree
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    '00_总开发计划与架构.md', '01_数据契约与持久化.md',
    '02_AgentHarness与上下文中间件.md', '03_工具与可调用工作流.md',
    '04_API前端与播前交接.md', '05_验收与实施任务包.md',
    '06_播前迭代教训预防矩阵.md', '07_评价体系执行配方.md',
]

CSS = """
:root{--ink:#24304a;--muted:#626e80;--line:#dbe0e7;--blue:#475ed7;--paper:#fff;--bg:#f5f5f1}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.85 'Segoe UI','Microsoft YaHei',sans-serif}
nav{position:fixed;left:0;top:0;bottom:0;width:266px;padding:28px 20px;background:#eeefe9;border-right:1px solid var(--line);overflow:auto}
nav b{display:block;font-size:21px;margin-bottom:12px}nav small{display:block;color:var(--muted);line-height:1.65;margin-bottom:20px}
nav a{display:block;color:#35426b;text-decoration:none;padding:10px 8px;border-radius:8px;font-size:14px;line-height:1.6}nav a:hover{background:#fff}
main{margin:28px 28px 80px 294px;max-width:1600px}.hero,article{background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:28px 32px;margin-bottom:26px}
.hero{border-top:5px solid var(--blue)}.badge{color:#52609c;font-size:13px;letter-spacing:.08em}.note{padding:14px 18px;background:#f1f4ff;border-left:4px solid #6b7ae0;border-radius:6px}
h1{font-size:30px;line-height:1.45;margin:8px 0 22px}h2{font-size:23px;line-height:1.5;margin:34px 0 16px;border-bottom:1px solid #e6e9ee;padding-bottom:10px}h3{font-size:19px;line-height:1.6;margin:26px 0 12px}p{margin:13px 0}a{color:#425dc7;text-decoration:none}a:hover{text-decoration:underline}
.table{overflow-x:auto;border:1px solid var(--line);border-radius:10px;margin:18px 0}table{width:100%;border-collapse:collapse;min-width:850px;font-size:14px;line-height:1.8}th,td{padding:13px 15px;text-align:left;vertical-align:top;border-bottom:1px solid #e1e6ed;border-right:1px solid #e1e6ed}th{background:#eef1f7;font-weight:650;position:sticky;top:0}tr:nth-child(even) td{background:#fafbf9}td:first-child{min-width:100px;font-weight:550}tr:last-child td{border-bottom:0}
code{background:#eef1f7;padding:2px 5px;border-radius:4px;font:13px/1.7 Consolas,'Microsoft YaHei',monospace;overflow-wrap:anywhere}pre{background:#20283a;color:#f0f3f8;padding:20px;border-radius:10px;overflow:auto;font:13px/1.75 Consolas,'Microsoft YaHei',monospace}pre code{background:none;color:inherit;padding:0;white-space:pre}
.figure{overflow:auto;background:#fafbf8;border:1px solid var(--line);border-radius:12px;padding:8px;margin:20px 0}.figure img{display:block;width:100%;min-width:1080px;height:auto}.caption{font-size:13px;color:var(--muted)}details{margin:12px 0;color:var(--muted)}summary{cursor:pointer}li{margin:7px 0}blockquote{margin:15px 0;padding:10px 18px;border-left:4px solid #adb8ce;background:#f8f9fb}
@media(max-width:960px){nav{position:static;width:auto}nav a{display:inline-block}main{margin:15px}.hero,article{padding:20px}h1{font-size:25px}}
@media print{nav{display:none}main{margin:0}.hero,article{border:0;padding:0}.table{overflow:visible}table{min-width:0;font-size:10px}h2{break-after:avoid}tr{break-inside:avoid}.figure img{min-width:0}article{break-before:page}}
"""


def inline(raw):
    text = escape(raw)
    text = re.sub(r'`([^`]+)`', lambda m: '<code>'+m[1]+'</code>', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', lambda m: '<a href="'+m[2].removeprefix('&lt;').removesuffix('&gt;')+'">'+m[1]+'</a>', text)
    return text


def cells(line):
    return [x.strip().replace(r'\|', '|') for x in re.split(r'(?<!\\)\|', line.strip().strip('|'))]


def render(text, stem):
    lines, output, paragraph = text.splitlines(), [], []
    def flush():
        if paragraph:
            output.append('<p>'+inline(' '.join(paragraph))+'</p>')
            paragraph.clear()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('```'):
            flush(); language=line[3:].strip(); body=[]; i+=1
            while i < len(lines) and not lines[i].startswith('```'):
                body.append(lines[i]); i+=1
            raw='\n'.join(body)
            if language == 'mermaid' and stem.startswith(('00_', '02_')):
                asset='harness-architecture.svg' if stem.startswith('00_') else 'harness-model-cycle.svg'
                output.append('<div class="figure"><img src="assets/'+asset+'" alt="Harness结构图"></div>')
                output.append('<p class="caption"><a href="assets/'+asset+'">单独打开矢量图，可放大查看</a> · 图中组件是待实施设计。</p>')
                output.append('<details><summary>查看可编辑Mermaid源图</summary><pre>'+escape(raw)+'</pre></details>')
            else:
                output.append('<pre><code>'+escape(raw)+'</code></pre>')
            i+=1; continue
        if line.lstrip().startswith('|'):
            flush(); rows=[]
            while i < len(lines) and lines[i].lstrip().startswith('|'):
                row=cells(lines[i]); i+=1
                if all(re.fullmatch(r':?-{3,}:?', x) for x in row): continue
                rows.append(row)
            output.append('<div class="table"><table>')
            for j,row in enumerate(rows):
                tag='th' if j == 0 else 'td'
                output.append('<tr>'+''.join('<'+tag+'>'+inline(x)+'</'+tag+'>' for x in row)+'</tr>')
            output.append('</table></div>'); continue
        if re.match(r'^#{1,6}\s', line):
            flush(); n=len(line)-len(line.lstrip('#'))
            output.append(f'<h{n}>'+inline(line[n:].strip())+f'</h{n}>'); i+=1; continue
        if re.match(r'^\s*(?:[-*]|\d+[.、])\s+', line):
            flush(); ordered=bool(re.match(r'^\s*\d+',line)); tag='ol' if ordered else 'ul'
            output.append('<'+tag+'>')
            while i < len(lines) and re.match(r'^\s*(?:[-*]|\d+[.、])\s+',lines[i]):
                output.append('<li>'+inline(re.sub(r'^\s*(?:[-*]|\d+[.、])\s+', '', lines[i]))+'</li>'); i+=1
            output.append('</'+tag+'>'); continue
        if line.startswith('> '):
            flush(); output.append('<blockquote>'+inline(line[2:])+'</blockquote>'); i+=1; continue
        if not line.strip(): flush()
        else: paragraph.append(line.strip())
        i+=1
    flush()
    return '\n'.join(output)


def svg_start(width, height, title):
    return [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img"><title>{escape(title)}</title>',
      '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#66758b"/></marker></defs>',
      '<rect width="100%" height="100%" fill="#f7f8f4"/>',
      '<style>text{font-family:Segoe UI,Microsoft YaHei,sans-serif;fill:#25334a} .line{fill:none;stroke:#758399;stroke-width:2;marker-end:url(#arr)} .dash{stroke-dasharray:7 5}</style>']


def text_svg(out,x,y,txt,size=17,weight=400):
    out.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}">{escape(txt)}</text>')


def zone(out,x,y,w,h,title):
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="#fffdf3" stroke="#c7ceb9" stroke-width="2"/>')
    text_svg(out,x+18,y+33,title,20,650)


def box(out,x,y,w,h,title,rows=(),fill='#eef1ff'):
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="#aebad0" stroke-width="1.6"/>')
    text_svg(out,x+14,y+29,title,18,650)
    for i,row in enumerate(rows): text_svg(out,x+14,y+55+i*25,row,15)


def arrow(out,points,label=None,labelpos=None,dashed=False):
    out.append('<polyline class="line'+(' dash' if dashed else '')+'" points="'+points+'"/>')
    if label and labelpos:
        x,y=labelpos
        text_svg(out,x,y,label,14,550)


def architecture():
    out=svg_start(1800,1360,'播中播后综合Agent Harness总架构')
    text_svg(out,30,44,'播中只采集 · 播后自主分析 · 人工采纳后交给播前',30,700)
    text_svg(out,30,76,'所有Review组件为待实施设计；复用现有8765／8791，不增加第三个服务。',17)
    zone(out,30,110,310,320,'① Agent工作台')
    box(out,50,175,270,90,'右侧：对话与任务',('过程开关默认关闭','展开安全参数／进度／耗时'))
    box(out,50,295,270,100,'左侧：数据与报告',('来源／映射／四类评分','筛选、微调、确认采纳'))
    zone(out,380,110,320,320,'② 既有服务入口')
    box(out,400,175,280,90,'声播8765同源桥',('身份／Origin／固定路径',))
    box(out,400,300,280,95,'Agent8791 Review API',('run／job／workspace／SSE','共享app，独立Review资源'))
    zone(out,740,110,650,700,'③ Deep Agents Harness')
    box(out,760,175,610,90,'ReviewRuntime + 可信Scope + 共享Provider账本',('租约／Goal／等待／恢复／取消；包住整个ainvoke',))
    box(out,760,300,610,125,'四个业务中间件',('Scope：角色与引用权限；EvidenceContext：本轮证据投影','TraceBudget：调用／耗时／预算；Completion：完成或等待门','SDK另配置：TodoList、受控文件、Summary、受限SubAgent'), '#f0eaff')
    box(out,760,465,260,100,'主复盘Agent',('自主选工具／追加调查','不强制五段流程走一遍'))
    box(out,1080,455,290,80,'内容专题（按需）',('原稿／顾虑／四项内容评级',))
    box(out,1080,570,290,80,'经营专题（按需）',('漏斗／基线／退款／反例',))
    box(out,760,600,260,150,'四份按需Skill',('数据资格；经营诊断','内容与顾客反馈','下场实验与反馈','按角色加载，不替代权限'), '#e7f1ff')
    box(out,1050,700,320,75,'K00–K13 + 五个F工具流',('typed契约；内部机械步骤复用',), '#e5f4ee')
    zone(out,1430,110,340,320,'④ 既有模型来源')
    box(out,1450,175,300,170,'chatgpt_plan / gpt-6.1-sol',('沿用安全授权／自动续期','真实发送前核最终wire预算','主／子／摘要／分类共用账本','不输出令牌／隐藏思维链'), '#f9e9df')
    zone(out,30,490,310,710,'⑤ 播中与人工资料')
    box(out,50,565,270,125,'唯一抖音采集源',('已知公开事件＋未知／健康','原事件与outbox同事务','Review仅持自己的订阅'), '#e6f4ef')
    box(out,50,760,270,135,'真实播放journal',('句子／模块／商品／版本','owner／epoch／attempt','暂停切段，取声等级分开'), '#e6f4ef')
    box(out,50,970,270,145,'授权经营报表',('CSV／XLSX预览与映射','人工只确认关键歧义','订单／金额／退款cohort'), '#e6f4ef')
    zone(out,380,860,1010,340,'⑥ 业务后端：数据、计算、正式产物')
    box(out,400,935,285,210,'Review独立持久层',('review.sqlite + artifacts','双层去重／冻结场次','producer fence／归档修订','不可变证据／原行可回读','checkpoint另库'), '#e5f4ee')
    box(out,725,935,290,210,'确定性指标与四类评分',('同口径分子／分母／as_of','目标／历史／内容／覆盖分','资格不足保留null和原因','固定基线，禁伪造因果','统一定义／cache／receipt'), '#e5f4ee')
    box(out,1055,935,315,210,'正式报告与安全Trace',('Goal绑定DeliveryReceipt','新算／复用有执行证明','用量先落账再校内容','模型／工具／阶段精细计时','关闭UI开关仍持续记录'), '#e5f4ee')
    zone(out,1430,490,340,710,'⑦ 人工采纳闭环')
    box(out,1450,565,300,130,'用户选建议并确认',('精确报告／下一场快照','预览变化与原有Recap','模型没有业务采纳工具'), '#f9eef2')
    box(out,1450,760,300,140,'Recap适配与对账',('分阶段幂等子ID','全局finding命名空间','保留旧Refs＋冻结CAS版本'), '#f9eef2')
    box(out,1450,970,300,140,'既有播前Agent',('结合商品／顾客／本场配置','形成下一次改稿计划','继续使用原人工批准门'), '#f9eef2')
    arrow(out,'320,215 400,215','同源API',(343,200))
    arrow(out,'540,265 540,300')
    arrow(out,'680,345 720,345 720,220 760,220')
    arrow(out,'1065,265 1065,300')
    arrow(out,'890,425 890,465')
    arrow(out,'1020,495 1080,495','按需',(1030,481),True)
    arrow(out,'1020,525 1050,525 1050,610 1080,610',dashed=True)
    arrow(out,'890,565 890,585 1035,585 1035,737 1050,737')
    arrow(out,'1210,775 1210,845 870,845 870,935')
    arrow(out,'1370,205 1410,205 1450,205','模型请求',(1370,188))
    arrow(out,'320,620 357,620 357,985 400,985')
    arrow(out,'320,820 345,820 345,1020 400,1020')
    arrow(out,'320,1045 400,1045','确认导入',(330,1028))
    arrow(out,'685,1040 725,1040')
    arrow(out,'1015,1040 1055,1040')
    arrow(out,'1370,1000 1410,1000 1410,630 1450,630','报告候选',(1378,941))
    arrow(out,'1600,695 1600,760')
    arrow(out,'1600,900 1600,970')
    text_svg(out,38,1250,'模型自主规划：调查对象、取证路径、是否委派、如何解释与提出建议。',21,650)
    text_svg(out,38,1287,'程序固定保证：同场归属、权限、统计口径、版本、幂等、人工确认和真实完成／等待状态。',20)
    text_svg(out,38,1324,'图示是职责分层；SDK内部hook先后以固定版本探针和实际发送边界核验。',16)
    out.append('</svg>'); return '\n'.join(out)


def cycle():
    out=svg_start(1580,1010,'一轮Agent模型工具循环的中间件与Skill位置')
    text_svg(out,30,44,'一轮模型／工具循环：谁自动触发，谁由模型选择',28,700)
    text_svg(out,30,78,'四个业务中间件不会作为工具等待模型调用；Skill和工具由模型按需选择。',17)
    nodes=[
      (30,125,'1 Runtime进入',('绑定可信run／role／budget','try/finally覆盖整个ainvoke'), '#eef1ff'),
      (560,125,'2 Completion控制门',('查正式完成／等待回执','可停止invocation，等待不等于完成'), '#f0eaff'),
      (1090,125,'3 EvidenceContext',('固定Goal、成果、缺项与精确reader','按需工作集，历史过大才摘要'), '#f0eaff'),
      (1090,375,'4 SDK机制 + Provider发送边界',('Skill按角色读取；仅一套Summary','最终wire核预算；所有模型共享账本'), '#f9e9df'),
      (560,375,'5 模型自主选择',('原子工具／F组合工具／子任务','补问／提交；可追加新调查路径'), '#eef1ff'),
      (30,375,'6 工具边界：Scope + TraceBudget',('身份／ref／副作用／取消／额度','记录安全参数、缓存、耗时与结果'), '#f0eaff'),
      (30,625,'7 typed服务执行',('机械步骤固定；资格、事务单一正本','K10／K13保存四项审核并程序算分'), '#e5f4ee'),
      (560,625,'8 持久结果 + ToolMessage',('正式refs／缺项／等待／错误可纠正','Provider响应先落用量再验内容'), '#e5f4ee'),
      (1090,625,'9 下次进入模型前再次检查',('完成→结束并展示绑定交付','等待→停止并等待持久Inbox唤醒'), '#f0eaff'),
    ]
    for x,y,title,rows,fill in nodes: box(out,x,y,460,145,title,rows,fill)
    for points in ['490,195 560,195','1020,195 1090,195','1320,270 1320,375','1090,445 1020,445','560,445 490,445','260,520 260,625','490,695 560,695','1020,695 1090,695']:
        arrow(out,points)
    arrow(out,'1320,770 1320,825 530,825 530,195 560,195','未结束则继续；不补一轮无用收尾',(550,812),True)
    text_svg(out,30,894,'输入／Job等待：保存WaitReceipt → 控制门停本次图 → 协调器原子消费Inbox → 同Goal同thread新invocation续办。',18)
    text_svg(out,30,931,'成功：finish非return_direct工具保存DeliveryReceipt → before_model检查后结束；参数错仍留给模型纠正。',18)
    text_svg(out,30,970,'Skill供选择方法；中间件自动守边界；确定性服务处理数据；模型保留业务规划和调查能力。',18,650)
    out.append('</svg>'); return '\n'.join(out)


def page(title, body, nav, intro=''):
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+escape(title)+'</title><style>'+CSS+'</style></head><body><nav><b>综合复盘 Agent</b><small>Harness工程计划<br>LIVE-REVIEW-ENG-1.0<br>设计完成 · 业务尚未实现</small>'+nav+'</nav><main>'+intro+body+'</main></body></html>'


def verify(texts):
    checks=[]
    def check(name, ok, detail=None): checks.append({'name':name,'passed':bool(ok),'detail':detail})
    for name,text in texts.items():
        check(name+'：代码围栏成对', sum(line.startswith('```') for line in text.splitlines())%2==0)
        widths=[]; errors=[]; expected=None
        for n,line in enumerate(text.splitlines(),1):
            if line.lstrip().startswith('|'):
                width=len(cells(line)); widths.append(width)
                if expected is None: expected=width
                elif width != expected: errors.append({'line':n,'expected':expected,'actual':width})
            else: expected=None
        check(name+'：表格列数一致', not errors, errors)
        links=[]
        for link in re.findall(r'\[[^\]]+\]\(([^)]+)\)',text):
            if re.match(r'^(?:https?://|#|app:|codex:)',link): continue
            target=link.strip('<>').split('#')[0]
            if target and not (ROOT/target).exists(): links.append(target)
        check(name+'：本地链接存在', not links,links)
    tools=texts[DOCS[3]]
    for i in range(14): check(f'K{i:02d}工具有定义',bool(re.search(r'\| K'+f'{i:02d}'+r'\s',tools)))
    for i in range(1,6): check(f'F{i:02d}工具流有详细步骤',f'### F{i:02d} ' in tools)
    recipe=texts[DOCS[7]]
    metric_list=recipe.split('## 12 34个metric_id唯一清单',1)[1].split('```text',1)[1].split('```',1)[0].split()
    metric_table=tools.split('## 5 指标注册',1)[1].split('## 6 模型任务',1)[0]
    registry_metrics=[]
    for line in metric_table.splitlines():
        if line.startswith('|'):
            registry_metrics.extend(re.findall(r'\b[a-z]+(?:_[a-z0-9]+)+\b',cells(line)[0]))
    check('评价配方34个metric_id唯一',len(metric_list)==34 and len(set(metric_list))==34)
    check('工具注册目录与评价配方逐项一致',set(metric_list)==set(registry_metrics),{'missing_in_registry':sorted(set(metric_list)-set(registry_metrics)),'extra_in_registry':sorted(set(registry_metrics)-set(metric_list))})
    tests=texts[DOCS[5]]
    for i in range(1,51): check(f'R{i:02d}验收用例存在',bool(re.search(r'\bR'+f'{i:02d}'+r'\b',tests)))
    lessons=texts[DOCS[6]]
    for i in range(1,19): check(f'PL{i:02d}旧教训反例存在',f'PL{i:02d}' in lessons)
    contracts=texts[DOCS[1]]; harness=texts[DOCS[2]]; api=texts[DOCS[4]]
    check('重算与复用义务有正式回执','CalculationReceipt' in contracts and 'refresh_mode' in tools and 'required_calculations' in harness)
    check('内容评级主子提交路径一致','content_assessments:ContentAssessmentInput[]' in tools and 'persist_content_assessments' in contracts)
    check('等待有真正停止与续办协议','WaitReceipt' in harness and 'jump_to' in harness and 'Inbox' in api+harness)
    check('SDK工具显式白名单与计划配置','SDK_TOOL_POLICY' in tools and '显式添加' in harness and 'TodoListMiddleware' in harness)
    check('采纳分阶段幂等和命名空间','uuid5' in api and 'recap_finding_id' in api)
    check('生产者fence与语义去重明确','accepted fence' in contracts and 'logical_event_key' in contracts)
    check('Job查询及超时可达','GET /review/v1/jobs/{id}' in api and '响应预算8s' in tools)
    for filename in ['harness-architecture.svg','harness-model-cycle.svg']:
        ElementTree.parse(ROOT/'assets'/filename); check(filename+'：合法SVG',True)
    baseline=json.loads((ROOT/'源码核查基线.json').read_text(encoding='utf-8-sig'))
    for item in baseline:
        p=Path(item['path']); actual=hashlib.sha256(p.read_bytes()).hexdigest()
        check('受保护业务源码未变：'+str(p),actual==item['sha256'],{'expected':item['sha256'],'actual':actual})
    result={'checked_at_utc':datetime.now(timezone.utc).isoformat(),'scope':'文档/链接/目录/负例与结构契约静态核对；不是新Review运行验收','checks':checks,'total':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks)}
    (ROOT/'工程计划静态核对记录.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    inventory={'schema':'design-inventory-1.0','implementation_status':'planned_not_registered_runtime_api',
      'metric_ids':metric_list,
      'custom_middleware':['ReviewScopeMiddleware','EvidenceContextMiddleware','TraceBudgetMiddleware','ReviewCompletionMiddleware'],
      'skills':['review_scope_and_data_eligibility','commercial_funnel_diagnosis','content_and_customer_feedback','next_session_experiment_and_feedback'],
      'atomic_tools':[], 'optional_flows':[], 'work_packages':[f'W{i}' for i in range(7)],
      'planned_acceptance_ids':[f'R{i:02d}' for i in range(1,51)],'planned_prevention_ids':[f'PL{i:02d}' for i in range(1,19)]}
    for line in tools.splitlines():
        m=re.match(r'^\| (K\d{2}) ([a-z_]+) \|',line)
        if m:
            row=cells(line)
            inventory['atomic_tools'].append({'id':m[1],'name':m[2],'signature_and_result':row[1],'behavior':row[2],'timeout_cache_error':row[3]})
    for m in re.finditer(r'^### (F\d{2}) ([a-z_]+)$',tools,re.M):
        inventory['optional_flows'].append({'id':m[1],'name':m[2],'source_document':DOCS[3]})
    (ROOT/'工程接口索引.json').write_text(json.dumps(inventory,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def main():
    (ROOT/'assets').mkdir(exist_ok=True)
    (ROOT/'assets'/'harness-architecture.svg').write_text(architecture(),encoding='utf-8')
    (ROOT/'assets'/'harness-model-cycle.svg').write_text(cycle(),encoding='utf-8')
    texts={name:(ROOT/name).read_text(encoding='utf-8-sig') for name in DOCS}
    nav=''.join('<a href="#section-'+str(i)+'">'+escape(name[3:-3])+'</a>' for i,name in enumerate(DOCS))
    articles=[]
    for i,(name,text) in enumerate(texts.items()):
        body=render(text,Path(name).stem)
        articles.append(f'<article id="section-{i}">'+body+'</article>')
        separate_nav='<a href="播中播后综合Agent完整工程计划.html">返回完整计划</a>'+''.join('<a href="'+Path(n).with_suffix('.html').name+'">'+escape(n[3:-3])+'</a>' for n in DOCS)
        (ROOT/Path(name).with_suffix('.html')).write_text(page(name[:-3],'<article>'+body+'</article>',separate_nav),encoding='utf-8')
    hero='<section class="hero"><div class="badge">2026-10-20 · LIVE-REVIEW-ENG-1.0</div><h1>播中播后综合 Agent<br>完整 Harness 工程计划</h1><p>从数据归档、数值评价到Agent自主复盘、人工采纳和下一场交接。</p><div class="note">一主、两个可选专题；四个业务中间件、四份Skill、十四个原子工具、五个可调用工具流。<br>本次交付工程设计，未修改业务代码；W／R／PL为后续实施和验收要求。</div></section>'
    (ROOT/'播中播后综合Agent完整工程计划.html').write_text(page('播中播后综合Agent完整工程计划',''.join(articles),nav,hero),encoding='utf-8')
    result=verify(texts)
    files=[p for p in ROOT.rglob('*') if p.is_file() and p.name not in {'工程设计文件清单.json'} and '__pycache__' not in p.parts]
    manifest={'version':'LIVE-REVIEW-ENG-1.0','scope':'design_only','files':[{'path':str(p.relative_to(ROOT)).replace('\\','/'),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size} for p in sorted(files)]}
    (ROOT/'工程设计文件清单.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'documents':len(texts),'static_checks':result['total'],'passed':result['passed'],'failed':result['failed'],'html':str(ROOT/'播中播后综合Agent完整工程计划.html')},ensure_ascii=False))
    if result['failed']: raise SystemExit(1)


if __name__=='__main__': main()
