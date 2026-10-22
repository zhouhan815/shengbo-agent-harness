"""Technical SDK probe only: deterministic stub, local tool, no business Agent.

Run with the project's isolated interpreter. No API credentials are needed.
This process discards inherited environment values before SDK imports and blocks
network activity. It never constructs a hosted model/provider client.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import socket
import sys
import traceback
from datetime import datetime, timezone
from importlib.metadata import version
from typing import Any, Sequence


# Do not inspect or retain inherited credentials. This affects this process only.
os.environ.clear()
os.environ.update(
    {
        "SYSTEMROOT": r"C:\Windows",
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
        "LANGSMITH_OTEL_ENABLED": "false",
    }
)

NETWORK_ATTEMPTS: list[dict[str, Any]] = []


def deny_network(*args: Any, **kwargs: Any) -> Any:
    NETWORK_ATTEMPTS.append({"blocked": True, "call_stack": [{"file": frame.filename, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_stack(limit=12)[:-1]]})
    raise RuntimeError("Network disabled for this offline SDK probe")


def audit_network(event: str, args: tuple[Any, ...]) -> None:
    # A local bind is not outbound I/O: urllib3 binds IPv6 loopback once at
    # import time to detect platform capability. Connections and DNS are denied.
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo"}:
        deny_network()


sys.addaudithook(audit_network)
socket.create_connection = deny_network
socket.getaddrinfo = deny_network
socket.gethostbyname = deny_network
socket.gethostbyname_ex = deny_network
socket.gethostbyaddr = deny_network
socket.getnameinfo = deny_network
socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.socket.sendto = deny_network

PROJECT = Path(__file__).resolve().parents[2]
REPORT = PROJECT / "environment" / "reports" / "offline-probe.json"
CHECKS: list[dict[str, Any]] = []


def check(name: str, condition: bool, evidence: Any) -> None:
    CHECKS.append({"name": name, "passed": bool(condition), "evidence": evidence})
    if not condition:
        raise AssertionError(name)


def main() -> int:
    report: dict[str, Any] = {
        "purpose": "Technical SDK compatibility probe; not a business Agent implementation",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "interpreter": sys.executable,
        "python": sys.version,
        "inherited_environment_discarded": True,
        "tracing_disabled": True,
        "hosted_model_clients_constructed": 0,
        "checks": CHECKS,
    }
    try:
        expected_prefix = PROJECT / ".venv"
        check(
            "isolated_virtual_environment",
            Path(sys.prefix).resolve() == expected_prefix.resolve() and sys.prefix != sys.base_prefix,
            {"prefix": sys.prefix, "base_prefix": sys.base_prefix},
        )

        # Guard self-test is blocked before any network action; reset the count
        # so the final count describes SDK execution alone.
        try:
            socket.create_connection(("127.0.0.1", 9))
        except RuntimeError:
            report["network_guard_self_test"] = "blocked as expected"
        else:
            raise AssertionError("network guard did not block connection")
        NETWORK_ATTEMPTS.clear()

        from deepagents import create_deep_agent
        from langchain.tools import tool
        from langchain_core.callbacks.manager import CallbackManagerForLLMRun
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain_deepseek import ChatDeepSeek
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langgraph.graph.state import CompiledStateGraph
        from pydantic import Field

        packages = ["deepagents", "langchain", "langgraph", "langchain-deepseek", "langchain-mcp-adapters"]
        report["versions"] = {name: version(name) for name in packages}
        check(
            "sdk_imports",
            callable(create_deep_agent) and callable(tool) and ChatDeepSeek is not None and MultiServerMCPClient is not None,
            report["versions"],
        )
        signature = inspect.signature(create_deep_agent)
        check(
            "factory_api_signature",
            {"model", "tools", "system_prompt", "middleware", "backend"}.issubset(signature.parameters),
            str(signature),
        )

        local_tool_calls: list[dict[str, int]] = []

        @tool
        def sdk_probe_add(a: int, b: int) -> str:
            """Add two integers locally for a technical SDK probe."""
            local_tool_calls.append({"a": a, "b": b})
            return str(a + b)

        class OfflineToolCallingStub(BaseChatModel):
            """A deterministic in-process test double; no provider SDK calls."""

            call_count: int = 0
            bound_tool_names: list[str] = Field(default_factory=list)

            @property
            def _llm_type(self) -> str:
                return "offline-sdk-probe-stub"

            def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
                self.bound_tool_names = [getattr(item, "name", str(type(item).__name__)) for item in tools]
                return self

            def get_num_tokens_from_messages(self, messages: list[BaseMessage], tools: Any = None) -> int:
                # Avoid optional tokenizer downloads; only a tiny probe is used.
                return sum(max(1, len(str(message.content)) // 4) for message in messages)

            def _generate(
                self,
                messages: list[BaseMessage],
                stop: list[str] | None = None,
                run_manager: CallbackManagerForLLMRun | None = None,
                **kwargs: Any,
            ) -> ChatResult:
                self.call_count += 1
                if self.call_count == 1:
                    response = AIMessage(
                        content="",
                        tool_calls=[{"name": "sdk_probe_add", "args": {"a": 17, "b": 25}, "id": "offline-add-1", "type": "tool_call"}],
                    )
                else:
                    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
                    if len(tool_messages) != 1 or tool_messages[0].content != "42":
                        raise AssertionError("Stub did not receive the expected local tool result")
                    response = AIMessage(content="SDK_OFFLINE_OK:42")
                return ChatResult(generations=[ChatGeneration(message=response)])

        model = OfflineToolCallingStub()
        graph = create_deep_agent(
            model=model,
            tools=[sdk_probe_add],
            system_prompt="Technical SDK probe only. Call the local probe tool once, then finish.",
            name="offline-sdk-compatibility-probe",
        )
        check("graph_compilation", isinstance(graph, CompiledStateGraph), type(graph).__name__)
        result = graph.invoke({"messages": [{"role": "user", "content": "Add 17 and 25 using sdk_probe_add."}]})
        check("local_tool_execution", local_tool_calls == [{"a": 17, "b": 25}], local_tool_calls)
        check(
            "graph_model_tool_round_trip",
            result["messages"][-1].content == "SDK_OFFLINE_OK:42" and model.call_count == 2,
            {
                "final_message": result["messages"][-1].content,
                "stub_model_calls": model.call_count,
                "message_types": [message.type for message in result["messages"]],
                "bound_tool_count": len(model.bound_tool_names),
            },
        )
        check("zero_sdk_network_attempts", not NETWORK_ATTEMPTS, {"sdk_network_attempts": len(NETWORK_ATTEMPTS)})
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    report["passed_checks"] = sum(item["passed"] for item in CHECKS)
    report["total_checks"] = len(CHECKS)
    report["network_attempts"] = len(NETWORK_ATTEMPTS)
    report["blocked_network_operations"] = NETWORK_ATTEMPTS
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
