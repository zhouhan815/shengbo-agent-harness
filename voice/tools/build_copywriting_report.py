"""Build the reviewable report from saved actual experiments; never reads credentials."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / 'docs'
LAB = DOCS / '话术提示词实验'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def build():
    runs = []
    for directory in sorted(LAB.iterdir()):
        if not directory.is_dir() or not (directory/'input.json').exists(): continue
        value = read(directory/'input.json')
        record = {'run': directory.name, 'case': value.get('case','reference-calibration'), 'requested_versions': value.get('version_count',2)}
        for filename, key in [('scorecard.json', 'scorecard'), ('metrics.json', 'metrics'), ('failure.json', 'failure'), ('judge-failure.json','judge_failure')]:
            if (directory/filename).exists(): record[key] = read(directory/filename)
        if (directory/'scripts.json').exists(): record['completed_versions'] = len(read(directory/'scripts.json'))
        if (directory/'system-prompt.txt').exists():
            record['prompt_sha256'] = hashlib.sha256((directory/'system-prompt.txt').read_text(encoding='utf-8').encode()).hexdigest()
        runs.append(record)
    (LAB/'summary.json').write_text(json.dumps(runs, ensure_ascii=False, indent=2),encoding='utf-8')
    by_name = {r['run']: r for r in runs}
    prompt = (ROOT/'prompts/直播口播_当前.txt').read_text(encoding='utf-8')
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    original = (ROOT/'prompts/直播口播_原提示词_v1.2.txt').read_text(encoding='utf-8')
    final_dir = LAB/'release11_slippers'
    scripts = read(final_dir/'scripts.json')
    final_score = read(final_dir/'scorecard.json')
    final_metrics = read(final_dir/'metrics.json')
    product = read(final_dir/'input.json')['product']

    example = ['# 声播：最终输入与两版实际生成话术（v1.3）',
        '这是 DeepSeek 实际返回、未经手工润色的展示文本；完整 JSON、朗读文本和每句事实引用保存在实验目录。简体中文、普通话。',
        '## 本次输入',
        f"- 商品名称：{product['name']}\n- 原价：178 新台币\n- 促销：{product['promotion']}\n- 版本数量：2（后台按版生成，第二版带本房间已有版本的路线摘要）",
        '### 卖点与规格（根据用户原资料整理的测试输入）', product['selling_points'],
        '为实验做了简体化和轻度归纳；用户原始商品字段另存于data/samples/slippers.product.json，不声称逐字相同。',
        '知识库没有发送给话术生成 API。物流、优惠券、售后等未从知识库带入口播；保留给以后智能问答。',
        '## 本组测评',
        '| 版本 | 总分 | 卖点组覆盖 | 汉字数 | 播报句数 | 自动针对性修订次数 |\n|---|---:|---:|---:|---:|---:|']
    for i, script in enumerate(scripts):
        m = final_metrics['versions'][i]; score = final_score['versions'][i]
        example.append(f"| 第{i+1}版 | {score['total']:.2f}/100 | {m['coverage_rate']:.0%} | {m['han_characters']} | {m['sentences']} | {script.get('quality_rewrite_attempts',0)} |")
    example.extend([f"版本差异：{final_score['pair_diversity']['score']}/5。{final_score['pair_diversity']['reason']}",
        '字数不是播出时长。示例较原参考稿更短的部分、语义评审的小问题，见报告；分数不是转化率。'])
    for i, script in enumerate(scripts):
        example.extend([f'## 第{i+1}版完整话术', '\n\n'.join(s['text'] for s in script['sentences'])])
    example.extend(['## 提示词与输入的可追溯信息',
        f"- 使用的提示词：`prompts/直播口播_当前.txt`，R11\n- SHA-256：`{digest}`\n- 实际模型：`{scripts[0].get('model')}`\n- 原始记录：`docs/话术提示词实验/release11_slippers/`",
        '本文件不包含 API Key。生成时使用商品事实，样稿不是对现有公司的内部提示词的还原证明；这是根据输入输出推导并验证的独立实现。'])
    (DOCS/'最终输入与两版话术_v1.3.md').write_text('\n\n'.join(example)+'\n',encoding='utf-8')

    edits = {
        '这双鞋有足弓支撑，位置刚好承托脚掌内侧那条弧线。':'这双鞋有足弓支撑设计，针对女性足部曲线做了承托。',
        '比如你在厨房切菜、煲汤，站的时间比平时长，脚底有支撑和没支撑，感受差别挺明显。':'比如平时在厨房切菜、准备饭菜，站立时间比较长，就可以把足弓支撑作为选鞋时重点关注的一项。',
        '像早起烧水、夜里去倒水，脚步轻一点，家里人也睡得更安稳。':'像早起烧水、夜里去倒水，走动声音小一些，也能减少对休息中的家人的打扰。',
        '浴室地面湿的时候，自己走稳一点、扶好把手，防滑特点就是多一层日常保障。':'浴室地面湿的时候，还是要放慢脚步，小心行走。',
        '平时脏了直接用水冲一冲，一冲即净，不用专门拿刷子费劲打理。':'平时脏了可以用水冲洗，日常打理比较方便。',
        '这双鞋很轻便，出门散步、去楼下倒垃圾、到海边走走，带着穿都不累脚。':'这双鞋很轻便，出门散步、去楼下倒垃圾、到海边走走，都可以考虑穿着它。',
        '不过浴室地面湿的时候，大家还是要自己走稳一点、扶好把手，防滑特性是让日常多一分踏实，不是随便怎么走都行。':'不过浴室地面湿的时候，仍要放慢脚步，小心行走。',
        '平时脏了直接用水冲一冲，一冲即净，不用专门拿刷子蹲在那里刷半天。':'平时可以用水冲洗，日常清洁比较方便。',
        '这双鞋有足弓支撑，针对女性足部曲线做了考虑，脚掌内侧那条弧线能被托住一点。':'这双鞋考虑了女性足部曲线，设计了足弓支撑，提供脚底承托。',
        '像你在厨房切菜、煲汤，一站就是大半个小时，脚底有支撑，比完全平底拖鞋多一分承托感。':'如果平时在厨房准备饭菜，站立时间比较长，可以重点关注这双的足弓支撑设计。',
    }
    reviewed = ['# 两版话术：人工审校推荐稿 v1.3',
        '输入、原始API结果与量化评分见《最终输入与两版话术》。这里在原始结果基础上进行了明确的文字审校，属于人工修订稿，不能把原始模型评分直接当作此稿评分；没有伪装成新的API输出。',
        '审校只收紧无依据体验或构造表述、删除未验证比较与过满承诺，不增加商品事实。两版均保留商品名称、原价178新台币、活动价107新台币及输入中的限时条件。']
    changes=[]
    for i,script in enumerate(scripts):
        lines=[]
        for s in script['sentences']:
            before=s['text']; after=edits.get(before,before)
            if after.startswith('尺码从') and after.endswith('都有。'): after=after[:-3]+'。'
            if before!=after: changes.append(f'- 第{i+1}版：{before}\n  → {after}')
            lines.append(after)
        reviewed.extend([f'## 第{i+1}版', '\n\n'.join(lines)])
    reviewed.extend(['## 审校变更记录', '\n\n'.join(changes)])
    (DOCS/'两版话术_人工审校推荐稿_v1.3.md').write_text('\n\n'.join(reviewed)+'\n',encoding='utf-8')

    main_order = ['r0_baseline','r1_expansion','r2_complete_varied','r3_no_copy','r4_contract_route',
        'r5_compact_summary','r6_final_candidate','r6_repeat','r7_expanded_clean','r8_balanced','r8_repeat',
        'release_slippers','release_slippers_repeat','release81_slippers','release82_slippers','release9_slippers','release9_repeat','release10_slippers','release10_repeat','release11_slippers','release11_repeat']
    lessons = {
        'r0_baseline':'结构约束多，卖点如何展开约束少；原请求含知识库。',
        'r1_expansion':'加入特点→场景→收益，仍有漏讲及两版雷同。',
        'r2_complete_varied':'单版高分不代表版本差异合格；加入跨版近似句检查。',
        'r3_no_copy':'实际出现要求1版却返回0版，保留失败响应。',
        'r4_contract_route':'明确JSON仍有近似场景句；不能只看单版高分。',
        'r5_compact_summary':'缩短提示词并减少全文参考；其他品类发现未知功能被说成没有。',
        'r6_final_candidate':'加入未知不等于没有；跨版雷同仍有波动。',
        'r6_repeat':'同一候选重复运行，观察随机波动。',
        'r7_expanded_clean':'约束较长；语义评审一次JSON失败后重试，不使用坏分数。',
        'r8_balanced':'压缩重复规则，保留场景展开与准确事实。',
        'r8_repeat':'同一候选重复测试。',
        'release_slippers':'加入重要事实与近似句检查，最多重写一次。',
        'release_slippers_repeat':'重复运行验证；仍不代表任意商品稳定。',
        'release81_slippers':'消除其他品类串入浴室提醒；主商品遗漏耐磨，拒收。',
        'release82_slippers':'加显式必讲清单；第二版重写后过短，拒收；残缺组合不算总分。',
        'release9_slippers':'反馈包含本次草稿、具体雷同句与遗漏，针对性修订而非盲目重写。',
        'release9_repeat':'最终流程的独立重复运行，失败也计入结果。',
        'release10_slippers':'补无依据比较规则后，人工仍发现不磨脚被擅自加入，继续拒收。',
        'release10_repeat':'重复运行发现未接入的下单入口被擅自描述，补入检查。',
        'release11_slippers':'最终采用候选，补不磨脚/已开通入口检查并移除饮料价格锚定。',
        'release11_repeat':'最终R11独立重复运行，检验波动。',
    }
    table = ['| 实验 | 两版单版分数 | 整组判定 | 本轮发现或改变 |','|---|---|---|---|']
    for name in main_order:
        r = by_name.get(name)
        if not r: continue
        card = r.get('scorecard',{}); versions = card.get('versions',[])
        incomplete = r.get('completed_versions',0) != r['requested_versions']
        score_text = ' / '.join(f"{s['total']:.2f}" for s in versions) if versions and not incomplete else '未完成，不计算整组分数'
        verdict = '通过' if versions and not incomplete and all(s['pass'] for s in versions) else '未通过'
        table.append(f"| {name} | {score_text} | {verdict} | {lessons.get(name,'')} |")

    cross = ['| 回归样品 | 两版分数 | 整组结果 | 说明 |','|---|---|---|---|']
    for name in ['release9_bag','release9_mug','release9_notebook','release10_mug']:
        r=by_name.get(name)
        if not r: continue
        versions=r.get('scorecard',{}).get('versions',[])
        passed=versions and len(versions)==2 and all(s['pass'] for s in versions)
        why='；'.join(r.get('scorecard',{}).get('pair_diversity',{}).get('reason','').splitlines())
        if r.get('failure'): why=r['failure']['message']
        if r.get('judge_failure') and not versions: why='两版生成完成，但两次语义评审JSON格式失败；不使用无法解析的分数，保留原响应。'
        cross.append(f"| {name}（{r['case']}） | {' / '.join(str(v['total']) for v in versions) or '没有有效整组评分'} | {'通过' if passed else '未通过/未完成评审'} | {why} |")

    report=f'''# 声播话术提示词优化与量化评测报告 v1.3

更新：2026-10-07。范围：移除外部话术入口、直播间话术归属、DeepSeek生成和多轮实际评测。没有生成运营Agent代码，也没有重新生成你现有房间的文本或音频。

## 1. 已改动的产品行为

- 商品库选品页移除“加入已有话术”；商品卡不再展示全局话术数量。商品库只保存/提供商品资料，房间生成的话术自动加入本场。
- 新话术记录带room_id；版本号和历史讲解路线按“房间＋商品”管理。监控台替换候选也限制在本房间。
- 物理存储仍是`runtime/library.sqlite3`共享数据库，话术按room_id及本场引用归属房间，不是每房间一份独立数据库。旧稿保留，删除本场条目不自动导入外部稿；历史记录不会因本次升级丢失。
- 新稿生成只传NAME、PRICE、SELLING、PROMOTION四类事实，知识库保留给问答；新稿不会仅因知识库更新就失效。确实引用知识库的旧稿继续保留原来的过期检查。
- DeepSeek生成后检查必讲特点、明确优惠条件、跨版近似句及有限的异常内容。失败后最多针对性修订一次；仍不合格则报错，不把失败草稿存为可用版本。任务按版保存，前面成功的版本保留，后台面板显示失败原因。
- 规则检查不替代完整事实审核，现有“核对并确认文本→生成语音→开播”步骤保留。

## 2. 从你给出的两套案例学到了什么

参考稿两版分别约1035、1170个汉字，均是完整销售讲解，不是一句话摘要。共同骨架是：欢迎/轻互动→生活困扰→商品身份→具体卖点展开→规格选择→价格优惠→购买引导。

关键不是每次都写同一个标题，而是把“特点”变成能听懂的生活画面。例如足弓支撑对应站着做家务时的承托；静音对应家人休息时的走动；轻便对应旅行收纳。每组要交代特点、情境及有限度收益，不能只重复“舒服、方便、好用”。

两版差异应落实到主要生活问题、前两组展开顺序、情境和表达节奏，而不是只改欢迎词。事实、价格与规格可以重复，因为它们不能随版本变化。

原示例中“自己穿了半年”“库存不多”“下播恢复原价”“零风险”“售后全程保障”等并没有你给出的商品字段支持。只学习叙述方法，不把这些话当作已知事实复制。所谓反推，是建立能生成相似风格的独立提示词，不能证明还原了原产品内部提示词。

## 3. 当前提示词的核心方法

先隔离事实，再规定口播展开方法，然后给每版不同路线，最后用结构化输出和检查闭环约束结果。

1. 输入清洗：商品名称、价格币种、卖点规格、促销条件；知识库完全不进入生成请求。
2. 必讲清单：程序从明确输入提取部分可识别特点，如EVA、一体成型、足弓、厚底、静音、防滑、轻便、耐磨、快干、防臭。清单不是全部商品事实的替代品。
3. 内容展开：每组用2至4个自然句说明“特点→生活情境→有限度收益”；资料少就缩短，不凭空补功能。
4. 版本规划：已有稿只给路线、开场、话题摘要，完整历史句子留在本地用于比对，减少模型复述参考全文。
5. JSON输出：每句保留展示文本、普通话朗读文本、事实引用和段落类型；房间只展示商品编号和版本序号。
6. 有界修订：若检查不通过，把本次草稿、具体雷同句、遗漏项反馈给模型，在草稿上修订，避免重写时缩成摘要。最多一次修订，不无限消耗API。

完整提示词：[当前提示词](../prompts/直播口播_当前.txt)。原提示词见本文末尾。R1至R11各候选及每轮实际系统提示词都已保留。

当前SHA-256：`{digest}`。实际调用模型为deepseek-flash；这不是原样输出价格/时长的承诺。

## 4. 量化评价体系

总分100分，程序40分，语义盲评60分。总分并不覆盖所有风险，因此另有必须满足的门槛。

| 项目 | 分值 | 计算/判断方式 |
|---|---:|---|
| 卖点覆盖 | 12 | 实际覆盖卖点组数/应覆盖组数×12；一个组包含多个关键词时须全部满足 |
| 完整流程 | 8 | 开场、身份、卖点情境、价格优惠、收尾五部分出现比例×8；统一旧段落命名后比较 |
| 逐句可播 | 10 | 8至85字符的自然句比例×10；这只是长度代理，口语自然度另评 |
| 展开长度匹配 | 5 | 丰富拖鞋资料参考850至1450汉字；其他丰富资料350至900，稀疏资料120至500；偏离按比例扣分 |
| 事实引用 | 5 | 商品断言有输入中存在的fact_id比例×5；引用合法不代表内容一定真实 |
| 口语自然度 | 15 | 是否像消费者能听懂的讲解，少说明书和后台话术 |
| 特点场景展开 | 20 | 是否将主要特点放入具体情境并说明有限度收益；资料少按有效展开比例判断 |
| 连贯性 | 10 | 前后承接、节奏，是否机械拼接小节 |
| 不重复/不凑字 | 10 | 单版是否反复堆同义空话 |
| 选择及购买引导 | 5 | 真实价格条件、明确规格建议和自然收口 |

整组通过须同时满足：每版≥80、语义部分≥45、覆盖率≥90%、价格条件正确、没有严重无依据断言/知识库混入/无效事实引用，且版本差异≥3/5。程序补充检查明确材质/核心特点遗漏、不可叠加条件、省略限时依据及本商品不适用的浴室示例等。

跨版近似句：对24字符及以上的叙述句计算SequenceMatcher相似度；≥0.84计为近似，后一版近似句比例≤15%，完全相同长叙述句≤1。价格、商品身份和规格等固定事实段落豁免。它能发现换词复述，但不能完整理解两个情境是否相同，因此另做语义差异评分。

语义评审仍由DeepSeek完成，但评审只看匿名文本与商品核心事实，看不到提示词版本、实验名称或旧分数，并倒序呈现版本。它不是独立人工评审，仍有误报；报告没有把模型所有建议都当成正确结论。

评价校准：早期计算漏认旧段落别名及“一冲即净”等同义表达，已修正并对保存样本统一重算；早期评审受到原参考稿夸大话术影响，改成只给事实和风格摘要。下表使用修正后的统一程序与v2评审协议。不能把中途临时分数与最终分数混比。

## 5. 实际多轮结果与修改依据

{'chr-placeholder'}

高分也可能未通过：R2/R4的单版分数高，但两版重复；R9自动语义分很高，人工却发现“比普通平底更稳/多一层抓地”未经输入支持，因此最终统一检查将其判为未通过。R10继续发现“不磨脚”和“直播间就能拍”等问题，补入检查。R8.1/R8.2的失败也保留，没有只展示成功样本。R11在当前范围采用，含覆盖清单、针对性修订和新增性能检查，不声称全局最优。

## 6. 最终样本与其他品类回归

[完整输入与两版原始话术](最终输入与两版话术_v1.3.md)包含商品输入、每版全部播报句和实际字数。输出未手工改写。

[两版人工审校推荐稿](两版话术_人工审校推荐稿_v1.3.md)另列最终可供你审阅的文字版本及逐项修改记录。R11自动规则通过仍有“带着穿都不累脚”、脚掌内侧位置、与完全平底比较等偏满表述，人工稿将其改成输入支持的承托、轻便和场景选择。原始评分仅适用于原始API输出，没有把手工修订稿冒充新生成样本。实际生产仍保留人工确认，自动规则不能证明任意句子完全有据。

{'cross-placeholder'}

包、杯子、笔记本是人为设计的合成回归样品，不是真实商家验证。它们检查知识库隔离、无活动价、条件优惠、未知性能及版本差异。早期R8包稿曾混入浴室提醒和未经提供的“限时”，已加入条件提示与规则检查；这些失败不算通过。资料少时，重复事实较多，语义版本差异仍是薄弱点，不能从拖鞋结果推导所有品类都稳定。

人工复核：没有采纳评审提出的“必须补未知售后/截止”“颜色必须全背”等错误建议。R9拖鞋稿还存在“缓解足部疲劳感”较输入“缓解足部压力”的轻微延伸，以及未经支持的普通拖鞋对比；R9杯子稿出现“不烫手”。这些不足促成R10，并在统一评分中补入检查，旧高分没有当成最终通过证明；原始生成结果照实保留。

## 7. 验收、运行与复现

- 78项Python回归测试通过，覆盖房间归属、版本编号、知识库隔离、事实更新过期、近似句触发一次修订、连续失败不入库、原商品繁体卖点检查、无依据比较/防烫/不磨脚/下单入口、后台任务和原播放链路。
- 5项音频播放器检查通过；preparation.js、control.js语法检查通过。没有重新合成你的既有音频，也不声称本轮测试过OBS或真实平台转化。
- 网页验收和桌面服务更新的最终证据保存在`docs/话术提示词实验/验收记录.json`及截图。失败后的修订及恢复也会记录。
- 原始输出、逐句引用、每轮输入、评分、失败原因位于`docs/话术提示词实验/`；请求记录只保存消息体，不保存Authorization。汇总为`summary.json`。

复现命令（在项目根目录）：

```powershell
.venv\\Scripts\\python.exe tools/script_prompt_lab.py generate --run my_check --case slippers --prompt prompts/直播口播_当前.txt
# 仅生成成功后执行评测
.venv\\Scripts\\python.exe tools/script_prompt_lab.py judge --run my_check
.venv\\Scripts\\python.exe tools/script_prompt_lab.py score --run my_check
.venv\\Scripts\\python.exe -m unittest discover -s tests -q
D:\\nodejs\\node.exe --test tests/test_audio_player.cjs
```

这些实验会调用你已配置的DeepSeek API，但不写入实际商品库/直播间。模型输出存在波动，不保证复现逐字相同结果。

## 8. 当前限制及后续改进

现有评估验证的是事实边界、写作质量和版本差异，尚无真实观众、点击、成交或留存数据，不能以94分推导转化提升。覆盖词典只覆盖部分明确特征，新增品类仍需扩展测试；字数检查不是时长控制。

下一步应积累更多真实商品和人类偏好对比，形成独立保留测试集。当前样品经过迭代使用，已不是完全未见的测试集。可进一步把版本规划拆成先规划不同情境、再写稿、再用独立语义评审检查；本次未把付费语义评分模型放入在线生产路径，以控制延迟和费用。

不要直接把自动生成稿当作最终承诺。自动检查通过后仍通过现有确认步骤审稿，再生成语音。每版最多两次生成请求；现场正常播出仍只读本地音频。

## 附录：修改前实际系统提示词（完整）

```text
{original.strip()}
```
'''
    report=report.replace('chr-placeholder','\n'.join(table)).replace('cross-placeholder','\n'.join(cross))
    (DOCS/'话术提示词优化与量化评测报告_v1.3.md').write_text(report,encoding='utf-8')
    print(json.dumps({'report':'docs/话术提示词优化与量化评测报告_v1.3.md','examples':'docs/最终输入与两版话术_v1.3.md','experiment_runs':len(runs),'prompt_revision':digest[:12]},ensure_ascii=False))


if __name__ == '__main__': build()
