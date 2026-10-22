"""Explicit, bounded live smoke tests. Never print credential or model payloads.

The root task must stop the old connection probe first. Example:
  python tests/probe_provider_live.py --stage tool --confirm-live
One stage is run per invocation; this is not an unattended load test.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from shengbo_prelive.providers import build_chatgpt_model
from shengbo_prelive.providers.chatgpt import ModelProviderError
from shengbo_prelive.providers.credentials import CredentialError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["text", "tool", "deepagent", "async"], required=True)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    args = parser.parse_args()
    if not args.confirm_live:
        print(json.dumps({"ok": False, "code": "explicit_live_flag_required"}))
        return 2
    events, executed = [], []
    model = build_chatgpt_model(observer=events.append, timeout=120)
    @tool
    def safe_add(left: int, right: int) -> int:
        """Add exactly two supplied integers; this probe has no side effects."""
        executed.append((left, right))
        return left + right
    try:
        if args.stage == "text":
            result = model.invoke([SystemMessage("你正在执行独立连接验收，请只用中文简短回答。"), HumanMessage("只回复：声播模型适配成功")])
        elif args.stage == "tool":
            history = [SystemMessage("必须调用safe_add，使用工具的真实结果回答。"), HumanMessage("调用safe_add计算19加23。")]
            first = model.bind_tools([safe_add], tool_choice="safe_add").invoke(history)
            if len(first.tool_calls) != 1 or first.tool_calls[0]["name"] != "safe_add":
                raise ModelProviderError("probe_tool_call_contract_failed")
            call = first.tool_calls[0]
            value = safe_add.invoke(call["args"])
            result = model.bind_tools([safe_add]).invoke([*history, first, ToolMessage(str(value), tool_call_id=call["id"])])
            if "42" not in result.content or executed != [(19, 23)] or result.tool_calls:
                raise ModelProviderError("probe_tool_result_not_used")
        elif args.stage == "async":
            async def run():
                chunks = [c async for c in model.astream([SystemMessage("只给简短中文回答。"), HumanMessage("只回复：异步适配成功")])]
                return chunks[-1]
            result = asyncio.run(run())
        else:
            from deepagents import create_deep_agent
            agent = create_deep_agent(model=model, tools=[safe_add], system_prompt=(
                "这是无副作用的模型集成验收。只调用一次safe_add计算19+23，然后输出结果。"
                "不要调用文件、任务、规划或其他工具；不读取业务资料。"))
            state = agent.invoke({"messages": [{"role": "user", "content": "请调用safe_add计算19+23并给出数字结果。"}]}, {"recursion_limit": 8})
            result = state["messages"][-1]
            if executed != [(19, 23)] or "42" not in result.content or result.tool_calls:
                raise ModelProviderError("probe_deepagent_tool_contract_failed")
        completed = bool(result.response_metadata.get("response_completed"))
        if not completed or not result.content:
            raise ModelProviderError("probe_response_not_completed")
        print(json.dumps({"ok": True, "stage": args.stage, "model": "gpt-6.1-sol", "response_completed": True,
                          "model_calls": len([e for e in events if e["type"] == "model.sent"]),
                          "local_tool_executions": len(executed), "nonempty_output": True}))
        return 0
    except BaseException as error:
        code = error.code if isinstance(error, (ModelProviderError, CredentialError)) else "probe_failed_without_sensitive_details"
        print(json.dumps({"ok": False, "stage": args.stage, "code": code,
                          "http_status": getattr(error, "http_status", None),
                          "model_calls": len([e for e in events if e["type"] == "model.sent"])}))
        return 1
    finally:
        if args.diagnose:
            destination = Path(__file__).resolve().parents[1] / ".runtime" / "provider_protocol_diagnostic.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Event types and schema field names only, no prompts/model output,
            # tool argument values, IDs, response bodies or credentials.
            shapes = [event["shape"] for event in events if event["type"] == "model.protocol"]
            destination.write_text(json.dumps({"stage": args.stage, "protocol_shapes": shapes}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
