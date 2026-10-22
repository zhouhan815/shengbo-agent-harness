"""Render editable SVG and PNG views of the design. No app/runtime invocation."""
from pathlib import Path
from html import escape
from math import atan2, cos, sin, pi
import json
from PIL import Image, ImageDraw, ImageFont

OUT=Path(__file__).resolve().parents[1]/'engineering-v2'/'figures'
OUT.mkdir(exist_ok=True)
FONT='C:/Windows/Fonts/msyh.ttc'
PALETTE={'text':'#193447','edge':'#617889','bg':'#FFFFFF','group':'#FFFBEA',
         'blue':'#E6EDFF','green':'#E3F3E8','purple':'#F1E7FB','orange':'#FFF0DA','neutral':'#F0F4F6'}
checks=[]

class Figure:
    def __init__(self,w,h,title,subtitle):
        self.w,self.h=w,h
        self.im=Image.new('RGB',(w,h),PALETTE['bg'])
        self.d=ImageDraw.Draw(self.im)
        self.svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
                  '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="#617889"/></marker></defs>',
                  f'<rect width="{w}" height="{h}" fill="white"/>']
        self.text(45,30,[title],40)
        self.text(45,84,[subtitle],24)
        self.bounds=[]
    def text(self,x,y,lines,size=26,center=None):
        font=ImageFont.truetype(FONT,size)
        for n,t in enumerate(lines):
            wid=self.d.textlength(t,font=font)
            tx=x-wid/2 if center else x
            yy=y+n*(size+12)
            self.d.text((tx,yy),t,font=font,fill=PALETTE['text'])
            self.svg.append(f'<text x="{tx:.1f}" y="{yy+size}" font-family="Microsoft YaHei,sans-serif" font-size="{size}" fill="{PALETTE["text"]}">{escape(t)}</text>')
    def group(self,x,y,w,h,title):
        self.d.rounded_rectangle((x,y,x+w,y+h),radius=14,fill=PALETTE['group'],outline='#CFCBAC',width=2)
        self.svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{PALETTE["group"]}" stroke="#CFCBAC" stroke-width="2"/>')
        self.text(x+22,y+15,[title],28)
    def node(self,x,y,w,h,lines,color='blue',size=26):
        requested_size=size
        while True:
            font=ImageFont.truetype(FONT,size)
            maxw=max(self.d.textlength(s,font=font) for s in lines)
            totalh=len(lines)*(size+12)-12
            if (maxw<=w-24 and totalh<=h-20) or size<=22: break
            size-=1
        if maxw>w-24 or totalh>h-20:
            raise ValueError(f'overflow {lines}: {maxw}/{w}, {totalh}/{h}')
        self.d.rounded_rectangle((x,y,x+w,y+h),radius=9,fill=PALETTE[color],outline='#A7B7C3',width=2)
        self.svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{PALETTE[color]}" stroke="#A7B7C3" stroke-width="2"/>')
        self.text(x+w/2,y+(h-totalh)/2-3,lines,size,center=True)
        self.bounds.append((x,y,w,h,lines[0]))
    def arrow(self,points,dashed=False,label=None,lpos=None):
        fill=PALETTE['edge']
        if dashed:
            for (x1,y1),(x2,y2) in zip(points,points[1:]):
                length=((x2-x1)**2+(y2-y1)**2)**.5
                for k in range(0,int(length),20):
                    e=min(k+10,length)
                    self.d.line((x1+(x2-x1)*k/length,y1+(y2-y1)*k/length,
                                 x1+(x2-x1)*e/length,y1+(y2-y1)*e/length),fill=fill,width=3)
        else: self.d.line(points,fill=fill,width=3)
        x,y=points[-1]; px,py=points[-2]; a=atan2(y-py,x-px)
        self.d.polygon([(x,y),(x-15*cos(a-.4),y-15*sin(a-.4)),(x-15*cos(a+.4),y-15*sin(a+.4))],fill=fill)
        pts=' '.join(f'{a},{b}' for a,b in points)
        dash=' stroke-dasharray="10 8"' if dashed else ''
        self.svg.append(f'<polyline points="{pts}" fill="none" stroke="{fill}" stroke-width="3"{dash} marker-end="url(#a)"/>')
        if label: self.text(*lpos,[label],22)
    def save(self,name):
        # No nodes overlap; grouping rectangles are deliberately excluded.
        for i,(x,y,w,h,n) in enumerate(self.bounds):
            for xx,yy,ww,hh,nn in self.bounds[i+1:]:
                if min(x+w,xx+ww)>max(x,xx) and min(y+h,yy+hh)>max(y,yy):
                    raise ValueError(f'overlapping nodes: {n}/{nn}')
        self.im.save(OUT/f'{name}.png')
        (OUT/f'{name}.svg').write_text('\n'.join(self.svg)+ '\n</svg>',encoding='utf-8')
        checks.append({'figure':name,'dimensions':[self.w,self.h],'nodes':len(self.bounds),'text_fit':'pass','node_overlap':'pass'})

f=Figure(2200,1600,'声播播前 Agent · 整体架构（PRELIVE-2.3）','工程设计待实现｜任务识别、并行分析、人工选择与审稿、可恢复收尾')
f.group(40,135,390,1360,'声播前端 / 同源入口')
f.group(490,135,1110,865,'本机 Agent 服务 / Deep Agents Harness')
f.group(1660,135,500,355,'模型与语音供应商')
f.group(1660,555,500,445,'本机原始调试记录')
f.group(490,1070,1670,425,'声播业务后端 / 业务真值唯一')
f.node(75,215,320,165,['商品库 / 本场配置','卡片拖拽 / 三模式重排','素材清单 / 本次成果'])
f.node(75,435,320,150,['Agent 对话 / 上传','补资料 / 身份澄清','查看运行与成果'])
f.node(75,655,320,190,['人工文案审核','批准精确文本/音色/顺序','人工采用 / 人工开播'],'orange',25)
f.node(75,930,320,220,['开发调试入口','执行树 / 并行时间线','原始调用 / 上下文','复盘影响 / diff / 导出'],'neutral')
f.node(530,200,1030,85,['RunCoordinator + Finalizer：排队 / 排他执行 / 撤权收尾 / 恢复'],'neutral',27)
f.node(530,330,1030,110,['同主 Agent：只读识别 → 首次冻结 Goal → 按需执行','S1 · M1 / M3 / M4 / M5 / M6 / M7 / M9'],'blue',29)
f.node(530,485,1030,80,['持久分析队列 + Supervisor：按需派发；最多同时运行 3 个分析任务'],'neutral',27)
for x,lines in [(530,['商品资料 Agent','S2 · 事实 / 身份 / 缺项']),
                (890,['本场配置 Agent','S3 · 活动 / 继承 / 设置']),
                (1250,['顾客场景 Agent','S4 · 人群 / 场景 / 研究'])]:
    f.node(x,615,310,130,lines,'green',25)
f.node(530,790,1030,75,['各自 PREPARED 报告 → 完成事件 → 主读取 / 校验 → 声播 ACCEPTED'],'green',27)
f.node(530,910,1030,60,['固定业务工具 + ShengboClient：类型化 HTTP 命令 / 查询 / 回执'],'neutral',27)
f.node(1700,205,420,100,['Agent：gpt-6.1-sol（待接）','文案：DeepSeek（独立配置）'],'purple',27)
f.node(1700,350,420,90,['本机免费普通话 / MiniMax'],'purple',27)
f.node(1700,635,420,120,['M9 + SDK callbacks','调度检查 + 业务 Job / outbox','认证信息在采集前去除'],'neutral',25)
f.node(1700,815,420,135,['独立 observability.sqlite','完整安全原文 + 追加事件','树 / 时间线是只读投影'],'neutral',26)
f.node(530,1155,280,175,['完整上下文 + 复盘','商品 / 本场 / 研究','旧稿 + 用户决定','逐条复盘处置 → 计划'],'blue',25)
f.node(850,1155,250,175,['文案 JobRunner','生成 / 局部修改','只处理计划模块','保留未变引用'],'blue',25)
f.node(1140,1155,260,175,['拖拽 / 三模式重排','应用 → 编译开场','第 1 / 6 / 11 项','AudioRequirements'],'blue',25)
f.node(1440,1155,300,175,['人工文案→语音批准','精确 hash / voice / 编排','M5 提前检查','声播后端强制验证'],'orange',25)
f.node(1780,1155,330,175,['TTS JobRunner','受理 / 领取 / 绑定查批准','只补缺失音频','候选 → 人工采用 / 开播'],'blue',25)
f.node(530,1390,1210,70,['声播业务库：商品 / 历史研究 / 配置 / 复盘 / 素材 / 话术 / 批准 / 快照'],'green',27)
f.node(1780,1390,330,70,['播出链接 → OBS 浏览器源'],'green',25)
f.arrow([(395,505),(465,505),(465,385),(530,385)])
f.arrow([(1045,285),(1045,330)])
f.arrow([(1045,440),(1045,485)])
for c in (685,1045,1405):
    f.arrow([(c,565),(c,615)])
    f.arrow([(c,745),(c,790)])
f.arrow([(1045,865),(1045,910)])
f.arrow([(530,825),(510,825),(510,242),(530,242)],dashed=True)
f.arrow([(1560,380),(1700,255)],label='模型请求',lpos=(1570,300))
f.arrow([(1560,940),(1660,940)],dashed=True)
f.arrow([(1910,755),(1910,815)])
f.arrow([(670,970),(670,1155)],label='正式命令 / 完整 refs',lpos=(700,1012))
for x1,x2 in ((810,850),(1100,1140),(1400,1440),(1740,1780)):
    f.arrow([(x1,1240),(x2,1240)])
f.arrow([(1945,1330),(1945,1390)])
f.arrow([(395,745),(450,745),(450,1048),(1590,1048),(1590,1155)],dashed=True)
f.text(55,1205,['主栈：M1/M3/M4/M5/M6/M7/M9','分析栈：M2/M3/M4/M5/M6/M8/M9'],21)
f.text(55,1325,['Skill 是角色方法，按需读取；','Middleware 随模型/工具循环触发；','声播权限 / CAS / 批准不能绕过。'],22)
f.text(45,1530,['未来播后 Agent 通过同一复盘接口接回；本期先用模拟复盘，不实施播中 / 播后 Agent。'],26)
f.save('01-architecture')

f=Figure(1700,1720,'主从并行、复盘汇合与人工门槛','箭头代表依赖与控制位置；运行时按目标选择所需能力，单次修改不必全流程重跑。')
f.node(620,150,460,95,['用户目标 / 原资料 / 明确对象'],'blue',28)
f.node(620,300,460,100,['同主 Agent 识别 / 按需执行','先只读，后按冻结 Goal 行动'],'blue',29)
f.node(1170,285,450,150,['主图 WAITING_JOBS','释放 invoke 租约，保留任务槽','每个子任务自己持租约'],'neutral',25)
f.node(75,150,460,240,['上场复盘：原文完整保存','先导入，显式关联本场','模拟 / 真实明确标记','不自动改价格或商品顺序','与分析并行准备，4B 汇合'],'purple',26)
f.node(620,455,460,80,['持久 batch → Supervisor 领取'],'neutral',28)
for x,lines in [(100,['2A 商品事实分析','独立上下文 / thread / 报告']),
                (620,['2B 本场配置分析','独立上下文 / thread / 报告']),
                (1140,['3 顾客场景研究','独立上下文 / thread / 报告'])]:
    f.node(x,610,460,125,lines,'green',27)
f.node(320,820,1060,100,['完成事件 → inbox 唤醒主 Agent → 逐份校验 / 接受','事实 + 研究形成全字段商品；独立有效报告不因另一分支失败而重做'],'green',26)
f.node(320,980,1060,120,['4B：完整商品 + 本场 + 研究 + 旧稿/用户决定 + 复盘','每条复盘：采纳 / 部分采纳 / 拒绝 / 推迟 / 已覆盖 + 理由','形成各模块生成 / 修改 / 复用 / 禁用计划'],'blue',27)
f.node(320,1160,1060,100,['执行话术计划 → 用户选稿 / 拖拽或重排 → 编译所需长短开场','未改模块保留；只排已选项；预览不失效，实际应用才更新编排'],'blue',26)
f.node(320,1320,1060,100,['6R 人工审核精确整包文案 / 顺序 / voice / 费用范围','声播生成批准回执；Agent 不能自行批准；改稿后旧批准失效'],'orange',27)
f.node(320,1480,1060,100,['必需 TTS → 音频检查 → 未采用候选 → 人工采用 → 人工开播','提交 / 领取 / 绑定验证批准；同文本音频缓存可复用'],'blue',27)
f.arrow([(850,245),(850,300)])
f.arrow([(850,400),(850,455)])
f.arrow([(1080,350),(1170,350)],dashed=True)
for c in (330,850,1370):
    f.arrow([(850,535),(850,570),(c,570),(c,610)])
    f.arrow([(c,735),(c,775),(850,775),(850,820)])
f.arrow([(850,920),(850,980)])
f.arrow([(850,1100),(850,1160)])
f.arrow([(850,1260),(850,1320)])
f.arrow([(850,1420),(850,1480)])
f.arrow([(75,270),(45,270),(45,1040),(320,1040)],dashed=True)
f.arrow([(1380,1370),(1640,1370),(1640,1040),(1380,1040)],dashed=True)
f.text(1405,1185,['编辑后重新','计划 / 审核'],24)
f.text(90,1630,['全程：M9 + SDK callbacks + 程序检查 / 业务任务埋点 → 原始记录 → 执行树 / 时间线 / diff'],27)
f.save('02-control-loop')

f=Figure(2000,1880,'任务判断、上下文与可复用工具流','PRELIVE-2.3 · Agent 选择能力，程序执行确定步骤；按本次目标验收，不强制每轮全跑。')
f.node(50,150,1900,90,['真实用户指令 + 精确选区：更新商品 / 只做分析 / 修改本场 / 改稿 / 重排 / 完整准备'],'blue',30)
f.node(460,310,1080,155,['同一个主 Deep Agent · S1','只读意图识别 → 必要澄清 → Coordinator 首次冻结 Goal / 授权','TaskAssessment：读哪些历史、复用或派谁、调用什么工具流、怎样验收'],'blue',29)
f.group(50,550,410,520,'业务历史 / 版本化保存')
f.node(80,635,350,365,['商品完整修订','顾客场景研究 / 证据','本场配置与历史差异','旧稿 / 使用记录 / 复盘','素材清单 / 已有文件','按精确 ID 和依赖查回','未变化复用，变化才补分析'],'green',26)
f.group(520,550,960,520,'多 Agent 协调 / 普通程序调度')
f.node(560,620,880,75,['持久 batch / Supervisor：仅执行已授权、必要的分析'],'neutral',27)
for x,lines in [(560,['S2 商品事实','来源 / 冲突 / 缺项']),
                (870,['S3 本场配置','主题 / 活动 / 历史']),
                (1180,['S4 顾客场景','研究复用 / 增量'])]:
    f.node(x,755,260,115,lines,'green',25)
f.node(560,930,880,85,['独立上下文 → PREPARED → 主校验 → 声播 ACCEPTED','必要时由主带精确引用定向 refine，不互传聊天'],'green',25)
f.group(1540,550,410,520,'上下文管理 / M3 + SDK')
f.node(1570,635,350,365,['始终：目标 / 禁止事项','关键条件 / 版本 / 待确认','按需：相关完整原文','过长：offload + 摘要','压缩后补当前关键约束','原文与调试记录不覆盖','不足时按商品 / 模块拆分'],'purple',26)
f.group(50,1140,1900,410,'可复用工具流：固定 typed 入口 + 声播服务 / JobRunner')
f.node(90,1230,400,230,['F01—F04','材料解析 / 历史影响','商品校验与保存','本场草案保存','CAS / 回执 / 缺项'],'blue',29)
f.node(550,1230,400,230,['F05—F06','完整上下文与模块计划','按冻结计划生成 / 局改','保留未改模块','后台 job ≠ 自主 Agent'],'blue',28)
f.node(1010,1230,400,230,['F07','卡片拖拽 / 同商品连续','尽量穿插 / 完全随机','预览 → 用户应用 → 编译','精确版本和重复项不丢'],'blue',27)
f.node(1470,1230,400,230,['F08—F10','人工批准后必需 TTS','复盘导入与精确绑定','素材需求 / 已有文件','不生成视频，不自动开播'],'blue',28)
f.node(50,1630,1900,140,['F11 · 按原 Goal 回读验收真实成果','只更新商品：商品 + 有效研究维护 + 话术影响清单；不自动改稿','要求生稿：必须有实际正文；整场才检查批准 / 音频 / 候选；可选视觉缺失不挡声音'],'orange',28)
f.arrow([(1000,240),(1000,310)])
f.arrow([(1000,465),(1000,620)])
f.arrow([(1510,465),(1510,1140)])
f.text(1030,495,['需要独立分析时'],22)
f.arrow([(255,550),(255,500),(600,500),(600,465)])
f.arrow([(1400,465),(1400,505),(1745,505),(1745,550)],dashed=True)
for c in (690,1000,1310):
    f.arrow([(1000,695),(1000,720),(c,720),(c,755)])
    f.arrow([(c,870),(c,900),(1000,900),(1000,930)])
f.arrow([(560,970),(490,970),(490,490),(700,490),(700,465)],dashed=True)
f.arrow([(1000,1070),(1000,1140)])
for c in (290,750,1210,1670):
    f.arrow([(c,1460),(c,1630)])
f.text(60,1810,['全程记录：原始模型与工具输入输出、许可与版本检查、主从时间线、实际成果和失败；不依赖模型自述完成。'],27)
f.save('03-toolflows-context')

f=Figure(2200,1580,'最终复查 · 执行排他、人工修改与历史版本选择','PRELIVE-2.3 · 这些是自动运行的服务边界与人工决定；没有新增自主 Agent 或第十类中间件。')
f.group(40,145,600,1310,'前端 / 商家明确决定')
f.group(690,145,820,1310,'Agent Harness / 运行控制')
f.group(1560,145,600,1310,'声播业务服务 / 唯一业务真值')
f.node(80,225,520,130,['对话 / 上传 / 编排 / 配置','仅更新资料：不自动改话术','原始要求与目标保存'],'blue',29)
f.node(730,225,740,130,['数据根 OS 独占锁 → Coordinator','每 thread 锁覆盖 SDK 调用至真实退出','业务租约不代替 checkpoint 排他'],'neutral',28)
f.node(1600,225,520,130,['商品 / 配置 / 研究 / 原资料','精确版本引用与历史证据','固定旧版不自动换成最新'],'green',28)
f.node(730,430,740,165,['同一主 Agent + 按需三路分析','最终 provider payload 预算检查','每次模型请求：reserved → sent → 结算','未知 usage / 费用明确保留'],'blue',27)
f.node(80,440,520,155,['审稿卡选择修改','原 Goal 内恢复同 run 改稿','超出 Goal 则明确 replace','修改不等于批准'],'orange',27)
f.node(1600,445,520,135,['商品新旧差异 / 当前控制','普通差异：保留或刷新由用户选','停售 / 明确撤回必须拦截'],'green',27)
f.node(80,685,520,150,['选择保留旧版或刷新','决定绑定精确新旧 ref / diff','保留不重 TTS、不改内容 epoch'],'orange',27)
f.node(730,695,740,140,['提出修改计划 → 执行授权内模块','失效问题用 typed system resolution 唤醒','随后重新审稿；人工批准才进入新 TTS'],'blue',27)
f.node(1600,685,520,150,['currentness 中央检查','发布 / 审稿 / 采用 / start 共用','keep 保存独立选择回执','refresh 才分析必要内容失效'],'green',27)
f.node(80,950,520,145,['查看成果 / 部分成果 / 继续入口','收尾时显示 FINALIZING / 对账','不假称全部停止或已经开播'],'neutral',27)
f.node(730,925,740,190,['成功 / 失败 / 阻塞 / 取消 → RunFinalizer','冻结派发 → 撤权对账 → 图执行者退出','复验业务成果 → ClosureReceipt → 真实终态','未收拢前保留冲突任务槽'],'neutral',27)
f.node(1600,950,520,145,['撤权仅禁止未来写入 / 绑定','已合法保存产物继续保留','人工采用 / 开播不依赖旧 run 活跃'],'green',27)
f.node(80,1210,520,150,['复盘建议：二号商品先讲','商品身份不等于第2版 / 第2项','未采纳不改序，不自动加商品'],'purple',27)
f.node(730,1210,740,150,['终态后的继续：新合法 Goal 复用真实成果','只补剩余部分，不继承旧 grant','未知付费请求先对账，不自动重复执行'],'neutral',27)
f.node(1600,1210,520,150,['商品 / 话术 / 音频回执仍可查','未变音频可复用','采用与 start 仍是独立人工动作'],'green',27)
f.arrow([(600,290),(730,290)])
f.arrow([(1100,355),(1100,430)])
f.arrow([(1470,290),(1600,290)],dashed=True)
f.arrow([(600,515),(680,515),(680,765),(730,765)])
f.arrow([(1860,580),(1860,685)])
f.arrow([(600,760),(650,760),(650,870),(1535,870),(1535,760),(1600,760)])
f.arrow([(1100,595),(1100,695)])
f.arrow([(1100,835),(1100,925)])
f.arrow([(730,1020),(600,1020)],dashed=True)
f.arrow([(1470,1020),(1600,1020)])
f.arrow([(1100,1115),(1100,1210)])
f.text(55,1500,['完整安全原始记录：模型载荷、工具输入输出、检查决定、并行执行、人工答复、收尾与成果；不声称记录模型私有思维。'],27)
f.save('04-final-review-boundaries')
(OUT/'render-validation.json').write_text(json.dumps({'kind':'diagram_layout_validation','checks':checks,'scope':'text bounds and node rectangles, plus human visual inspection required'},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'output':str(OUT),'checks':checks},ensure_ascii=False))
