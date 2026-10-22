"""Actual HTTP-body checks for focused live replies; no network/model calls."""
import io
import json

import pytest

from app import live_reply, reply_lab, server


def fake_transport(monkeypatch, answer):
    requests = []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-placeholder")

    def urlopen(request, timeout):
        body = json.loads(request.data)
        requests.append((body, timeout))
        content = {"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}],
                   "usage": {"total_tokens": 20}}
        return io.BytesIO(json.dumps(content, ensure_ascii=False).encode())

    monkeypatch.setattr(reply_lab.urllib.request, "urlopen", urlopen)
    return requests


@pytest.mark.parametrize("comment,required_example", [
    ("多少钱？", "这套是二十三元五角八分。"),
    ("孩子下课后还能练吗？", "课程以外能安排自主练习。"),
    ("家里能练吗？", "目前只确认适合户外，室内是否适用还需要核实。"),
])
def test_real_request_contains_question_scope_and_location_boundary(monkeypatch, comment, required_example):
    response = {"answer": "待核实。", "status": "needs_confirmation", "citations": [], "missing_information": []}
    requests = fake_transport(monkeypatch, response)
    context = {"current_product_id": "p", "comment": comment, "evidence": []}
    reply_lab.complete_remote(context, max_tokens=600, timeout=20)
    assert len(requests) == 1
    body, timeout = requests[0]
    assert timeout == 20 and body["max_tokens"] == 600
    system, user = body["messages"]
    assert system["role"] == "system" and system["content"] == reply_lab.prompt()
    assert json.loads(user["content"]) == context
    assert required_example in system["content"]
    for rule in ("只正面回答当前问题所需的已知事实", "回答完就结束", "不必在末尾加建议或推荐",
                 "不朗读后台配置", "证据只支持户外，就不能推成室内或家里也适用",
                 "课外指课程以外的时间，不代表家里能用", "未被问到的地点、安全、效果或使用推荐不作为结尾补充"):
        assert rule in system["content"]
    assert "先正面解决问题，再在合适时给出轻量建议" not in system["content"]
    assert "这款现在资料里的活动价" not in system["content"]


@pytest.mark.asyncio
async def test_production_live_reply_uses_same_prompt_and_real_retrieval(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "RUNTIME", tmp_path)
    product = server.ProductInput(name="单人训练套装", price=23.58,
        knowledge="儿童可以使用。可在课程以外安排自主练习。适用场景：户外。").record("p-outdoor")
    config = {"id": "r", "name": "测试间", "commerce": server.commerce.parse_room({})}
    event = {"id": "e", "room_id": "r", "type": "comment", "text": "孩子下课后还能练吗？"}
    seen = []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-placeholder")

    def urlopen(request, timeout):
        body = json.loads(request.data)
        context = json.loads(body["messages"][1]["content"])
        seen.append((body, context))
        chunk = next(chunk for chunk in context["evidence"] if "课程以外" in chunk["text"])
        answer = {"answer": "可以呀，儿童也可以使用，课程以外能安排自主练习。", "status": "answered",
                  "citations": [{"chunk_id": chunk["id"], "quote": chunk["text"]}], "missing_information": []}
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode())

    monkeypatch.setattr(reply_lab.urllib.request, "urlopen", urlopen)
    result = await live_reply.build_live_answer(event, product, config)
    assert len(seen) == 1 and result["answer_status"] == "answered"
    assert seen[0][0]["messages"][0]["content"] == reply_lab.prompt()
    assert "证据只支持户外，就不能推成室内或家里也适用" in seen[0][0]["messages"][0]["content"]
    assert all(chunk["product_id"] == "p-outdoor" for chunk in seen[0][1]["evidence"])
    assert "家里" not in result["answer"] and "配置" not in result["answer"]
