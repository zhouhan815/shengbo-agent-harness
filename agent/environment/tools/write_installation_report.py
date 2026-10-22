"""Summarize local SDK preparation evidence, without network or provider clients."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import distribution, version
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[2]
REPORTS = PROJECT / "environment" / "reports"
probe = json.loads((REPORTS / "offline-probe.json").read_text(encoding="utf-8"))
installed = json.loads((REPORTS / "installed-packages.json").read_text(encoding="utf-8-sig"))
lock = PROJECT / "environment" / "requirements.lock.txt"
graph_source = Path(distribution("deepagents").locate_file("deepagents/graph.py"))
check_file = REPORTS / "pip-check.txt"
check_raw = check_file.read_bytes()
check_text = check_raw.decode("utf-16") if check_raw.startswith((b"\xff\xfe", b"\xfe\xff")) else check_raw.decode("utf-8-sig")
dependency_passed = "No broken requirements found." in check_text
probe_passed = probe["status"] == "passed"
round_trip = next(item["evidence"] for item in probe["checks"] if item["name"] == "graph_model_tool_round_trip")
tool_calls = next(item["evidence"] for item in probe["checks"] if item["name"] == "local_tool_execution")
report = {
    "schema_version": 1,
    "status": "ready" if dependency_passed and probe_passed else "failed",
    "created_at_utc": datetime.now(timezone.utc).isoformat(),
    "scope": "SDK/environment preparation and offline technical probes only; no ecommerce business Agent",
    "project_directory": str(PROJECT),
    "python": {
        "executable": sys.executable,
        "version": sys.version,
        "base_prefix": sys.base_prefix,
        "pip_version": version("pip"),
    },
    "requested_sdk_versions": probe["versions"],
    "additional_core_versions": {name: version(name) for name in ["langchain-core", "mcp"]},
    "installed_package_count_including_pip": len(installed),
    "locked_runtime_dependency_count": len([line for line in lock.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]),
    "validation": {
        "passed": probe["passed_checks"] + int(dependency_passed),
        "total": probe["total_checks"] + 1,
        "pip_check": {"passed": dependency_passed, "evidence_file": str(check_file), "output": check_text.strip()},
        "offline_probe": {"passed": probe_passed, "evidence_file": str(REPORTS / "offline-probe.json"), "passed_checks": probe["passed_checks"], "total_checks": probe["total_checks"]},
        "stub_model_calls": round_trip["stub_model_calls"],
        "local_tool_calls": len(tool_calls),
        "final_message": round_trip["final_message"],
        "network_connection_or_dns_attempts": probe["network_attempts"],
        "hosted_model_clients_constructed": probe["hosted_model_clients_constructed"],
        "inherited_environment_discarded": probe["inherited_environment_discarded"],
        "tracing_disabled": probe["tracing_disabled"],
    },
    "fingerprints": {
        "requirements_lock_sha256": sha256(lock.read_bytes()).hexdigest(),
        "deepagents_graph_py": str(graph_source),
        "deepagents_graph_sha256": sha256(graph_source.read_bytes()).hexdigest(),
    },
    "official_sources": [
        "https://docs.langchain.com/oss/python/deepagents/quickstart",
        "https://reference.langchain.com/python/deepagents/graph/create_deep_agent",
        "https://pypi.org/pypi/deepagents/json",
        "https://docs.langchain.com/oss/python/integrations/chat/deepseek",
        "https://docs.langchain.com/oss/python/langchain/mcp",
    ],
    "scope_boundaries": {
        "business_agent_implemented": False,
        "credentials_loaded_or_created": False,
        "paid_model_api_calls": 0,
        "existing_ecommerce_voice_agent_modified": False,
        "user_global_skills_modified": False,
        "global_python_packages_modified": False,
    },
    "not_validated": ["Real model inference", "External MCP connectivity", "Production credentials", "Ecommerce business workflows"],
    "remaining_required_work_for_sdk_preparation": [],
}
target = REPORTS / "installation-report.json"
target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"report": str(target), "status": report["status"], "validation": report["validation"]}, ensure_ascii=False, indent=2))
raise SystemExit(0 if report["status"] == "ready" else 1)
