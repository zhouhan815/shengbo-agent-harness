"""Loopback service entry. Secrets stay inside provider / service credential stores."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def main():
    # This application records locally. Do not inherit unrelated cloud tracing
    # credentials from a developer shell and export merchant documents silently.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    import uvicorn
    from .api import create_app
    from .client import ShengboClient
    from .runtime import RuntimeEngine
    from .security import service_tokens

    project = Path(__file__).resolve().parents[2]
    root = Path(os.environ.get("SHENGBO_AGENT_RUNTIME", str(project / ".runtime")))
    # The inference endpoint is selected inside the provider. This URL is only
    # the sibling voice application; never the OAuth connection test service.
    tokens = service_tokens()
    client = ShengboClient("http://127.0.0.1:8765", tokens["agent_to_voice"], tokens["controller_to_voice"])
    engine = RuntimeEngine(root, client, model_name="gpt-6.1-sol",
        tool_presentation_enabled=os.environ.get("SHENGBO_PROGRESSIVE_TOOLS", "1") == "1")
    app = create_app(engine, tokens["voice_to_agent"])
    uvicorn.run(app, host="127.0.0.1", port=8791, access_log=False, log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        # Exception messages from an SDK or transport may contain request data.
        # Operational detail belongs in the redacted trace, never stdout.
        print("声播播前助手启动失败，请检查本机运行环境和安全运行记录。", file=sys.stderr)
        raise SystemExit(1) from None
