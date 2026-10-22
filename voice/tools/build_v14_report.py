"""Publish saved results and exact input/output without reading credentials."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/话术评测_v1.4"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    final = OUT / "v14-R17-slippers"
    scripts = read(final / "scripts.json")
    product = read(final / "input.json")["product"]
    prompt = (ROOT / "prompts/直播口播_当前.txt").read_text(encoding="utf-8")
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    adjudication = {
        "性质": "开发代理与独立AI事实复核；非真人审稿，保留自动评审原记录",
        "自动评审原结果": "93/92分，但两版原始hard_gate均为false；原记录未覆盖改写",
        "争议一": {"原句": "出门旅行想带上自己的拖鞋，塞进行李不占太多分量。",
                  "自动意见": "误当成体积/收纳占用保证", "复核依据": "分量指重量，输入明确轻便且适合旅行；不是说不占空间。该句可视为有限度转述。"},
        "争议二": {"原句": "出门旅行住酒店，很多人想换上一双自己的拖鞋，但行李空间有限，太重的鞋子塞进去就成了负担。",
                  "自动意见": "认为增加本品携行负担比较", "复核依据": "这是一般选择情境，不断言本品体积、不具体比较其他型号、不报告实测；独立复核认为不构成硬事实错误。可改得更直接。"},
        "复核结论": "两版通过本次有限文本事实检查；保留引入泛化、长尺码句等措辞建议。",
        "没有证明": ["其他商品都能通过", "提示词全局最优", "真实声音无唱腔/无杂音", "真实转化率提升"],
    }
    (final / "adjudication.json").write_text(json.dumps(adjudication,ensure_ascii=False,indent=2),encoding="utf-8")
    example = ["# 声播 v1.4：确切输入与两版实际输出", "",
        "下列正文为R17这次DeepSeek实际输出，未手工润色。每句原始朗读文本和事实引用见同名实验目录scripts.json。", "",
        "## 输入（知识库不送给话术生成模型）", "", "```json",
        json.dumps({k:v for k,v in product.items() if k not in {"knowledge","id"}},ensure_ascii=False,indent=2),"```", "",
        "## 生成设置", "", f"简体中文、普通话；顺序生成两版，第二版读取第一版的讲解路线以避免雷同。当前提示词SHA256：{digest}。",
        "完整提示词：`prompts/直播口播_当前.txt`；实际请求/响应：`docs/话术提示词实验/v14-R17-slippers/`。请求保存不含Authorization。",
        "评分93/92是AI辅助结果。原始自动事实门槛未通过的两项，经独立AI复核认为是误判；没有隐藏或覆盖原判断。", ""]
    labels = {"welcome":"开场欢迎","pain":"场景引入","identity":"商品介绍","benefit":"卖点讲解","scene":"使用场景","specification":"规格选购","price":"价格说明","promotion":"促销优惠","cta":"购买引导"}
    for index,script in enumerate(scripts,1):
        example += [f"## 第{index}版", "", f"{len(script['blocks'])}段 / {len(script['sentences'])}句。", ""]
        for block in script["blocks"]:
            example += [f"### {labels.get(block['stage'],block['stage'])} · {block.get('focus','')}", ""]
            example += [s["text"] for s in script["sentences"] if s["block_id"]==block["id"]]
            example.append("")
    (ROOT / "docs/最终输入与两版话术_v1.4.md").write_text("\n".join(example),encoding="utf-8")
    rows=[]
    for directory in sorted(OUT.iterdir()):
        if directory.is_dir() and (directory/"scorecard.json").exists():
            data=read(directory/"scorecard.json")
            rows.append("| "+directory.name+" | "+" / ".join(str(v["total"]) for v in data["versions"])+" | "+" / ".join("通过" if v["hard_gate"] else "未通过" for v in data["versions"])+" | "+data.get("judge_protocol","早期校准口径（不与最终分数直接比较）")+" |")
    report = """# 声播 v1.4：分段口播、语气配置与透明评测

本轮已修改应用并更新本机服务。直播间文字不再需要确认；商品有效、资料没有过期且本场语音齐全即可开播。正常直播只播本地音频，插播仍在线合成。没有把评测分数或AI审查增加成用户必须点击的审批步骤。

## 1. 本轮实际改了什么

- 话术详情分段展示开场、场景引入、商品介绍、卖点、规格、价格、促销、购买引导，每句仍保留独立编号、编辑和试听。
- 分段保存不再把全部段落打散；保留stage/focus和句子归属，编辑创建新快照，不修改已排入播放的旧文本。
- MiniMax按段设置情绪和语速；说明不进入朗读正文。参数、音色、模型和风格版本参与缓存，不能把旧中性音频误当成新风格音频。旧文件不删除。
- 修复自动重写反馈只给段落元数据、不含草稿句子的缺陷。现在带原句和事实引用做针对性修订，仍只自动重写一次，失败保留已有稿件。
- 新增“话术评测”，公开量表、真实轮次、失败、分角色AI意见、独立AI复核、争议裁决和语音A/B试听。

更新后打开桌面“声播”，进入直播间，查看分段话术，再点击“生成语音”。如果音色/模型/段落风格变化，显示待生成是正常的缓存失效；本场全部语音完成即可开播。已经打开的旧页面需刷新。

## 2. 电商脚本和TTS分别怎么控制

文本模型仍用DeepSeek。输入只包括商品名称、价格/币种、卖点规格、促销条件；知识库留给后续问答。主线是简短欢迎→具体选择情境→商品→特点与场景价值→规格→准确价格→优惠→明确选择动作。

这次没有把“更有渲染力”理解成虚构库存和倒计时。重点是尽快讲清商品、说明为何值得关注、结尾给出选色核码下单动作。减少“特点都讲清楚了”这种后台总结。R12至R17共六轮拖鞋候选，每轮两版；另有两轮杯子回归。实际请求、模型响应和失败全部保存。

MiniMax HTTP T2A没有已确认的通用自然语言prompt字段。真正发送的是voice_setting里的emotion/speed/pitch/vol。direction只是界面解释，不会念进声音。pitch是整句音高偏移，不是逐字升降曲线；没有已确认的“禁止唱歌”“去噪”开关。

| 段落 | MiniMax参数 | 设计意图 |
|---|---|---|
| 欢迎 | happy、1.02倍速 | 有精神，避免喊叫 |
| 引入/商品介绍 | 自动情绪、0.98倍速 | 聊天式讲清楚 |
| 卖点/场景 | 自动情绪、1.00倍速 | 清楚自然，有轻重 |
| 规格 | calm、0.94倍速 | 数字清楚 |
| 价格 | 自动情绪、0.94倍速 | 币种数额听得清 |
| 促销/购买引导 | happy、1.02倍速 | 适度热情，明确行动 |

全部pitch=0、vol=1，不靠抬高音调和音量制造销售感。本机SAPI维持默认普通话读法，不支持与MiniMax相同的情绪控制。效果需试听，不是参数一设置就保证有感染力。

官方依据：[MiniMax HTTP语音生成](https://platform.minimax.cn/docs/api-reference/speech-t2a-http)。电商组织方式参考[TikTok Shop LIVE脚本教学](https://seller-us.tiktok.com/university/essay?knowledge_id=2799697140090667)及[LIVE Script说明](https://seller-fr.tiktok.com/university/essay?knowledge_id=3535915235526420)。详见两份独立研究文档；抖音课程仅看到目录，没有声称看过正文。

## 3. 评分怎样构成

冻结100分：程序30分=覆盖10、结构8、分句与引用8、机械差异4；AI70分=事实10、展开15、销售10、连贯10、语义差异10、自然口语15。通过线总分80、语义52；价格规格条件错误、无依据商品/履约承诺、知识库泄漏等不能靠高分抵消。

每批分别调用事实、电商表达、口语三个评审角色。它们是同模型分别调用，可能共同偏差，并非三位真人。独立AI另做盲核，不读取已有评分。评测中曾把两版误认一版，也曾把重量误判体积、把审美建议判成性能；原记录保留，校准过程和复核意见公开。校准前后的分数不能当作可直接比较的增长。

| 样本 | 两版AI总分 | 原始自动事实门槛 | 评审协议 |
|---|---|---|---|
""" + "\n".join(rows) + """

最终R17为93/92。自动门槛原始未通过：一条把“分量”误解成体积，一条把一般行李选择情境当成本品实测比较。独立AI逐句核对后，两版通过本次有限文本事实检查；裁决单独保存，不覆盖原hard_gate。仍建议缩短长尺码串和泛化引入。不能据此称提示词全局最好。

杯子回归仍发现把材质推成手感/稳固比较、把容量推成占地优势等问题，已保留失败及独立复核。当前有限规则不是通用语义事实审核，任意新商品仍可能有外推，不能把拖鞋通过当成全品类验证完成。

## 4. 唱腔与杂音的实际验证边界

对原投诉句、卖点句、促销句各生成原参数A和分段参数B，共六段真实MiniMax WAV。均可解码、32000Hz单声道、长度正常，采样削波比例为0。生成具有随机性，时长差异不能单独证明语气改善。

没有完成真人试听，也没有原投诉音频用于逐秒比对。不能宣称唱腔或杂音已经解决。网页“话术评测→语音A/B试听”可听六个样本。应检查说话而非唱腔、可辨背景噪声、价格可懂度、情绪与衔接；若原始WAV正常但OBS有杂音，再检查播放和采集链路。不要用波形检查或文本评分代替听感判断。

## 5. 工程验收与限制

- 92项Python检查通过，5项播放器回归通过；三个修改后的JS语法检查通过。
- 隔离QA服务完成真实本机TTS生成、结构化编辑、未确认文字直接开播、本地音频就绪检查、六个A/B文件读取。未改正式商品库测试数据。
- 前端以真实接口JSON做渲染烟测，包含分段、当前供应商语气说明、评测记录及音频控件。
- Computer Use浏览器点击验收被工具中止，理由是无法可靠确认浏览器当前URL；未绕过此限制。浏览器真实点击、扬声器听感和OBS接入不在本轮已通过列表。
- 正式库备份：runtime/backups/20261007-segments-v1.4/library.sqlite3。桌面快捷方式和8765端口保留，更新前确认无正在播出或生成任务。

## 6. 后续最值得改进的地方

优先收集六个样本的实际试听结果，再只改变一个变量比较音色或HD模型。针对长尺码列表做更细的朗读切分。补充服装、箱包等真实商品回归，建立带正反例的语义事实审查数据集，减少规则漏检和评审误报。上线播放不再要求文本确认，但这不等于内容已经有真人审校或经营效果验证。

## 可复用文件

- prompts/直播口播_当前.txt：当前完整提示词，R17。
- docs/最终输入与两版话术_v1.4.md：确切输入和按段显示的两版实际输出。
- docs/电商讲品与透明评测研究_v1.4.md：第一方平台来源与方法。
- docs/TTS分段表达调研_v1.4.md：MiniMax文档、能力边界和试听量表。
- docs/话术评测_v1.4/：冻结量表、评分、各角色意见、AI盲核和裁决。
- docs/语音试听_v1.4/：六段真实WAV、参数和机器文件检查。
"""
    (ROOT / "docs/分段话术与语音优化报告_v1.4.md").write_text(report,encoding="utf-8")
    print(json.dumps({"prompt_sha256":digest,"scored_batches":len(rows),"report_written":True},ensure_ascii=False))


if __name__ == "__main__":
    main()
