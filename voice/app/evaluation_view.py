"""Read-only publication of reproducible experiments; never exposes API credentials."""
import json


def read(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def evaluation_report(root):
    current = root / "docs/话术评测_v1.4"
    legacy = root / "docs/话术提示词实验"
    runs = []
    seen = set()
    for folder in [current, legacy]:
        if not folder.exists():
            continue
        for directory in sorted(folder.iterdir(), reverse=True):
            if not directory.is_dir() or not (directory / "input.json").exists():
                continue
            if directory.name in seen:
                continue
            seen.add(directory.name)
            data = read(directory / "input.json", {})
            scripts = read(directory / "scripts.json", [])
            runs.append({"run": directory.name, "case": data.get("case"),
                         "requested_versions": data.get("version_count"), "completed_versions": len(scripts),
                         "scorecard": read(directory / "scorecard.json"),
                         "failure": read(directory / "failure.json"), "judge_failure": read(directory / "judge-failure.json"),
                         "reviews": read(directory / "reviews.json"), "independent_reviews": read(directory / "independent-reviews.json"),
                         "speech_peer_review": read(directory / "speakability-peer-review.json"),
                         "adjudication": read(directory / "adjudication.json"),
                         "review_history": [read(p) for p in sorted(directory.glob("scorecard-previous-*.json"))],
                         "generation": "v1.4" if folder == current or directory.name.startswith("v14-") else "v1.3历史试验"})
    samples = read(root / "docs/语音试听_v1.4/results.json", [])
    for sample in samples:
        if sample.get("file"):
            sample["url"] = "/tts-samples/" + sample["file"]
    return {"rubric": read(current / "rubric.json", read(legacy / "rubric.json", {})), "runs": runs, "audio_samples": samples,
            "methodology": {"流程": "先冻结指标，再生成、程序检查、分角色盲评、记录缺点、修订和复测。失败轮次也保留。",
                "角色": ["事实与条件审查", "电商表达与讲品结构", "口语自然度与可播性"],
                "模型评分局限": "同一模型分角色评审可能有共同偏差，独立AI复核也不等于真人测试。",
                "语音验收": "文本评分不证明音频无唱腔、无杂音，需分别试听；音频波形检测只排除明显文件损坏和削波。",
                "事实优先": "虚构功能、价格、库存、截止、体验和履约承诺不能以高总分抵消。",
                "未知": "尚无真实观众转化率、真人主播试播或听感MOS数据。"}}
