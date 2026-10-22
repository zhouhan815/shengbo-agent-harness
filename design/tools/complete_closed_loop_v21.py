from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / 'engineering-v2'

def change(name, pairs):
    p = D/name
    t = p.read_text(encoding='utf-8')
    for a,b in pairs:
        if a not in t:
            raise ValueError(f'missing: {name}: {a[:65]}')
        t=t.replace(a,b)
    p.write_text(t,encoding='utf-8')

change('00_后端工程设计.md', [
 ('observability.py           # JSON事件、关联ID、去敏', 'observability/              # 原始安全载荷、追加事件、SDK callbacks、调试API'),
 ('## 10. 本次修订的闭环和可观察性', '## 11. 本次修订的闭环和可观察性'),
 ('以本目录01—06分册为准', '以本目录01—06与09—11的PRELIVE-2.1合同为准'),
])
change('01_类型与持久化.md', [
 ('`AudioRequirements`与TTS job增加 `approval_ref/review_hash`', '`AudioRequirements`在批准前冻结且不含批准ref；TTS job、音频绑定和Publication携带 `approval_ref/review_hash`'),
 ('Agent运行库新增 `trace_events/trace_payloads/trace_exports`，仅存执行事实', '各服务独立的 `observability.sqlite`新增 `trace_events/trace_payloads/trace_exports`，Agent运行库只保留trace关联refs；仅存执行事实'),
])
change('03_声播业务服务与事务.md', [
 ('expected_generation_epoch: int, mutation: MutationContext, principal: Principal,\n) -> JobReceipt:',
  'expected_generation_epoch: int, approval_ref: str, review_hash: str,\n    mutation: MutationContext, principal: Principal,\n) -> JobReceipt:'),
 ('1. 从已冻结 requirements 读取句子，不从“当前 script”重新读取可变化内容。',
  '1. 同一提交事务验证当前human AudioApprovalReceipt、精确review_hash/requirements/voice/readset与费用上限，再从冻结requirements读句子；缺批准不创建可执行任务。每项claim事务再查批准/授权/epoch并预留请求预算，不从当前script重新读可变内容。'),
 ('5. 事务内对 item lease 与 preparation epoch fence；更新 AudioManifest 需求映射和 job 成功回执。',
  '5. 事务内再次校验批准仍active且精确绑定、item lease/run grant/preparation epoch/readset；更新AudioManifest需求映射和job成功回执。撤销或旧输入仅留缓存，不激活。'),
 ('    audio_manifest_ref: ArtifactRef\n', '    audio_manifest_ref: ArtifactRef\n    approval_ref: str\n'),
 ('3. 核查 draft/完整商品 refs/脚本模块/编译计划/requirements/manifest 相互一致；',
  '3. 核查当前有效AudioApprovalReceipt及其精确绑定，draft/完整商品 refs/脚本模块/编译计划/requirements/manifest 相互一致；'),
 ('`prepare_required_audio(AR4,epoch16)`', '`prepare_required_audio(AR4,epoch16,approval_ref=AP4,review_hash=H4)`'),
 ('| 9 TTS |', '| 8R 人工审核 | `create_text_review → approve_text_for_audio` | human检查实际文本/顺序/音色/费用并提交H4；服务核对当前精确输入 | AP4批准回执；无此步骤不能TTS |\n| 9 TTS |'),
 ('可信控制器再以human身份提交；模型只得到批准回执引用。',
  '声播可信human API使用真实人类会话先提交批准，再由outbox同步QuestionService回答并恢复Agent；Agent服务凭据不能转成人类身份，模型只得到批准回执引用。'),
])
change('04_业务执行与上下文.md', [
 ('保存playlist、编译、补所需文本、只生成required音频。', '保存playlist、编译、补所需文本；展示精确整包审核并等human批准，再只生成required音频。'),
 ('5. 重编译并只补新文本音频；publish新candidate。', '5. 重编译，原批准失效；展示变更并取得新版整包human批准后只补新文本音频；publish新candidate。'),
 ('4. 完整三路+旧稿+决定建立bundle', '4. 完整三路+旧稿+决定+已绑定复盘建立bundle'),
])
change('06_前端对接合同.md', [
 ('| 选择音色/试听 | save_session_draft / preview_audio | 试听不是整场ready；供应商失败不静默换声 |',
  '| 选择音色/试听 | save_session_draft；有缓存示例可播，新TTS须human批准后preview_audio | 试听有独立小样上限，不是整场ready；不静默换声 |'),
 ('| 准备语音/重试 | compile→prepare_required_audio / retry_failed_items | required/ready/failed/stale逐项可见 |',
  '| 准备语音/重试 | compile→create_text_review→human approve_text_for_audio→prepare_required_audio / retry_failed_items | 批准绑定精确文本/voice/顺序；重试也验证批准/预算，状态逐项可见 |'),
])
# There must be one decision-kind enum, not a shorter incompatible copy.
p=D/'06_前端对接合同.md'
t=p.read_text(encoding='utf-8')
t=re.sub(r"kind: Literal\[[^\]]*'identity'[^\]]*\]", 'kind: DecisionKind  # 唯一定义见10，包含text_audio_approval等确认类型', t, flags=re.S)
p.write_text(t,encoding='utf-8')
change('10_人工确认与语音门槛设计.md', [
 ('`audio_approvals`', '`audio_approval_receipts`'),
 ('audio_approval_id: str', 'approval_ref: str'),
 ('AUDIO_APPROVAL_', 'TEXT_APPROVAL_'),
])

# Replace the old schematic Audit body with an explicit non-masking wrapper.
p=D/'02_运行时与中间件.md'
t=p.read_text(encoding='utf-8')
a=t.index('### 6.10 AuditMiddleware')
b=t.index('## 7.',a)
t=t[:a]+'''### 6.10 AuditMiddleware

```python
async def awrap_tool_call(self, request, handler):
    # begin_or_stop持久化start和安全完整args失败时尚未执行业务，停止新动作。
    span = await audit.begin_or_stop(request, kind='tool')
    try:
        result = await handler(request)
    except BaseException as original_error:
        await audit.try_end_or_spool(span, error=original_error)  # 不抛出、不覆盖原异常
        raise
    await audit.try_end_or_spool(span, result=result)  # 失败仅标gap并暂停后续动作
    return result  # 已有真实业务回执不能因日志失败改成可重试业务失败

async def awrap_model_call(self, request, handler):
    span = await audit.begin_or_stop(request, kind='model')
    try:
        result = await handler(request)
    except BaseException as original_error:
        await audit.try_end_or_spool(span, error=original_error)
        raise
    await audit.try_end_or_spool(span, result=result)
    return result

async def aafter_agent(self, state, runtime):
    await audit.try_record_exit(runtime.context, checkpoint_refs=extract_refs(state))
    return None
```

`try_end_or_spool/try_record_exit`内部无抛出：记录异常写应急JSONL和健康gap，保留原结果/原异常。开始记录与结束记录的失败语义不同；原始载荷安全策略、SDK摘要等旁路callbacks、voice后台job和outbox补证全部按[11](11_原始运行记录与调试台设计.md)实现。普通日志只放去敏索引，独立observability.sqlite及payloads保存完整可观察原文；不保存凭据或承诺内部思维链。trace不负责重试或业务状态恢复。

'''+t[b:]
p.write_text(t,encoding='utf-8')

# Update the architecture diagram and add the runtime-control figure.
p=D/'08_架构图与中间件Skill挂载说明.md'
t=p.read_text(encoding='utf-8')
t=t.replace('业务范围、数据权威、主/分析Agent边界和人工采用/开播规则不变。',
            '基于PRELIVE-2.1：新增复盘输入、文案→语音批准及完整调试记录；保留数据权威和主/分析Agent边界。')
t=t.replace('去敏日志、ID/hash、耗时、usage与回执引用；不吞异常',
            '安全完整可观察输入输出、ID/hash/耗时/usage；SDK callbacks及业务job补齐旁路，不因日志失败重做业务')
t=t.replace('稳定action/operation标识、范围与版本校验、未知结果对账；业务提交仍由声播事务校验',
            '稳定action/operation标识、范围/版本及TTS前批准早检查、未知结果对账；业务提交仍由声播事务强制校验')
t=t.replace('完整上下文及模块generate/patch/reuse/disable；终态证据',
            '完整上下文、逐条复盘处置及模块generate/patch/reuse/disable；终态证据')
a=t.index('## 6.')
b=t.index('## 7.',a)
t=t[:a]+'''## 6. 整体架构图：前端、主从、复盘、确认与记录

图为待实现设计；默认DeepSeek模型，语音为本机普通话/MiniMax。具体API/OAuth候选仍以第9节为准。

```mermaid
flowchart TB
    subgraph FRONT["声播前端 · 同一网页"]
        EDIT["商品库／本场配置／话术／编排"]
        CHAT["Agent对话／上传／澄清卡"]
        HUMAN["人工审核文案→批准语音<br/>人工采用→人工开播"]
        DEBUG["开发调试：执行树／时间线<br/>原始请求结果／diff／导出"]
    end
    ENTRY["声播FastAPI同源统一入口"]
    EDIT <--> ENTRY
    CHAT <--> ENTRY
    HUMAN <--> ENTRY
    DEBUG <--> ENTRY
    subgraph HARNESS["本机Agent服务 · Deep Agents Harness"]
        RUN["RunCoordinator：排队／恢复／取消<br/>同一主thread单invoke"]
        MAIN["主Agent · S1 播前统筹<br/>M1/M3/M4/M5/M6/M7/M9"]
        Q["持久分析队列＋Supervisor<br/>并发上限3 · 普通程序调度"]
        subgraph WORK["独立上下文／独立租约 · 每个挂M2/M3/M4/M5/M6/M8/M9"]
            F["商品资料Agent · S2"]
            C["本场配置Agent · S3"]
            A["顾客场景Agent · S4"]
        end
        REPORT["PREPARED报告→完成事件<br/>主读取并accept→声播ACCEPTED"]
        TOOLS["固定业务工具＋HTTP客户端"]
        ADB[("Agent运行库／独立checkpoint<br/>任务、问题、暂存、恢复")]
        RUN --> MAIN -->|按需派发| Q
        Q --> F
        Q --> C
        Q --> A
        F --> REPORT
        C --> REPORT
        A --> REPORT
        REPORT -->|inbox唤醒| RUN
        MAIN --> TOOLS
        Q <--> ADB
        RUN <--> ADB
    end
    subgraph BIZ["本机声播业务后端 · 唯一业务权威"]
        RECAP["复盘服务：原文＋结构化意见<br/>模拟/真实标签 · 精确绑定"]
        CONTEXT["商品＋本场＋研究＋旧稿/决定＋复盘<br/>完整ContextBundle→逐模块计划"]
        GENERATE["文案JobRunner：只生成/修改计划模块"]
        COMPILE["用户选稿顺序→编译<br/>第1/6/11项长开场→AudioRequirements"]
        APPROVAL["人工文案批准服务<br/>精确文本/voice/编排/hash<br/>Agent不能自行批准"]
        TTS["TTS任务：受理/领取/绑定均核验批准<br/>缓存差集→候选→人工采用/开播"]
        VDB[("声播业务库＋受控文件<br/>商品、复盘、稿件、批准、任务、快照")]
        RECAP --> CONTEXT --> GENERATE --> COMPILE --> APPROVAL --> TTS
        CONTEXT <--> VDB
        APPROVAL <--> VDB
        TTS <--> VDB
    end
    subgraph TRACE["各服务本机独立原始记录库 · 开发者只读"]
        CAPTURE["M9＋SDK callbacks＋调度/outbox＋声播job<br/>完整安全载荷 · 追加事件 · 因果ID"]
        STORE[("observability.sqlite＋payloads<br/>树／时间线是投影，原文不被摘要覆盖")]
        CAPTURE --> STORE
    end
    MODEL["模型层：DeepSeek默认；可选适配"]
    VOICE["声音：本机普通话／MiniMax"]
    PLAY["声播播出窗口→OBS浏览器源"]
    FUTURE["未来播后复盘Agent<br/>本期仅模拟输入，不实现"]
    ENTRY <--> RUN
    ENTRY <--> VDB
    ENTRY --> APPROVAL
    TOOLS <--> CONTEXT
    TOOLS <--> RECAP
    MAIN <--> MODEL
    WORK <--> MODEL
    GENERATE <--> MODEL
    TTS <--> VOICE
    TTS --> PLAY
    PLAY -.->|未来真实直播记录| FUTURE
    FUTURE -.->|同一复盘输入合同| RECAP
    MAIN -.-> CAPTURE
    Q -.-> CAPTURE
    GENERATE -.-> CAPTURE
    TTS -.-> CAPTURE
    ENTRY <--> STORE
```

Skill绑定具体Agent，Middleware绑定具体循环钩子；声播自身后台生成/TTS不经过主图M9，必须由自身adapter和outbox记录。APPROVAL是声播后端强制的业务门槛，M5仅提前发现；人工操作不伪称触发主Agent中间件。调试原始记录是单独数据库，图中业务库仍唯一，Agent运行库不持业务正本。

导出图：[总架构PNG](figures/01-architecture.png)、[可编辑SVG](figures/01-architecture.svg)。

'''+t[b:]
t=t.replace('M5动作边界、幂等及异常对账', 'M5动作边界、TTS批准早检查、幂等及异常对账')
t += '''

## 10. 主从控制和并行的执行图

```mermaid
flowchart TB
    U[用户目标与原资料] --> M[主Agent判断本次缺什么]
    M -->|dispatch_analysis_batch| D[持久任务清单＋各自InputManifest]
    D --> S[Supervisor领取任务 · 并发最多3]
    S --> F[2A商品事实 · 独立branch thread]
    S --> C[2B本场配置 · 独立branch thread]
    S --> A[3顾客场景 · 独立branch thread]
    M --> WAIT[WAITING_JOBS · 释放invoke租约]
    F --> RF[PREPARED报告]
    C --> RC[PREPARED报告]
    A --> RA[PREPARED报告]
    RF --> E[inbox完成事件 · 同主thread恢复]
    RC --> E
    RA --> E
    E --> ACCEPT[主Agent逐份校验与接受]
    ACCEPT --> J[按依赖汇合：4A商品／4B话术计划]
    R[复盘导入与绑定 · 可并行] --> J
    J --> G[声播按计划生成或修改]
    G --> O[用户选择顺序→编译所需开场]
    O --> H[6R人工审核文案并批准]
    H -->|批准有效| T[必需TTS→候选检查]
    H -->|修改| J
    T --> AD[人工采用→人工开播]
```

主/子使用同一个Deep Agents SDK，可以使用相同模型；“主/次”描述统筹和专门分析，不是必须使用大/小两种模型。主只负责决策与受控业务写入；三类分析Agent只提交报告。Supervisor不思考经营策略；图上的并行是可观察的真实任务并行。

主等待后台时active-run槽仍保留，不能让同thread另一用户run抢占；子任务自己的租约继续续租。任务完成后inbox唤醒主，逐份按精确readset验证。单个失效报告只重算相关任务，不能因此丢掉另外两份有效报告。实际调用日志能验证start/end区间是否交叠。

导出图：[并行与闭环PNG](figures/02-control-loop.png)、[可编辑SVG](figures/02-control-loop.svg)。

## 11. 复盘与人工确认具体放在哪里

| 位置 | 行为 | 对应设计 |
| --- | --- | --- |
| 输入接收 | import_recap＋显式link；原文完整保存，模拟醒目标记 | 09 |
| 4A保存前 | 同名身份不明/关键事实冲突时问清；明确唯一目标更新不反复问 | 10 H01—H07 |
| 4B定计划 | 全商品/本场/顾客场景/旧稿决定/复盘汇合；逐finding采纳与模块关联 | 09及04 |
| 6R到7 | human批准整包话术；M5早挡，声播提交/claim/commit强制校验 | 10 |
| 候选就绪 | 先人工采用，再单独人工start | 03及10 |
| 全程 | 自动记录调用、分派、汇合、处置、确认与版本diff；调试台查原件 | 11 |

模拟复盘不是新的用户指令来源，它不能擅自降价、换商品、改顺序。主Agent必须把每条意见变成apply/partial/reject/defer/already_covered及具体理由，再决定生成/修改哪些模块。新增复盘不取代既有完整三路输入。
'''
p.write_text(t,encoding='utf-8')
print('Canonical integration and diagrams updated; PNG/SVG render still pending.')
