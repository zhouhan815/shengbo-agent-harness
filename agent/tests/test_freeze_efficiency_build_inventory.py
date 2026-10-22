"""The benchmark freeze covers new backend modules without scanning secrets."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest


@pytest.fixture
def freeze_module(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "verification/freeze_efficiency_build.py"
    spec = importlib.util.spec_from_file_location("freeze_efficiency_inventory_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent, voice = tmp_path / "agent", tmp_path / "voice"
    (agent / "src/pkg").mkdir(parents=True)
    (voice / "app").mkdir(parents=True)
    (agent / "verification").mkdir()
    (agent / "src/pkg/original.py").write_text("original = True\n", encoding="utf-8")
    (voice / "app/main.py").write_text("voice = True\n", encoding="utf-8")
    (agent / "verification/run_efficiency_live_v1.py").write_text("pass\n", encoding="utf-8")
    # These files are deliberately outside the application source inventory.
    (agent / ".env").write_text("DO_NOT_READ\n", encoding="utf-8")
    (voice / "credential.bin").write_bytes(b"DO_NOT_READ")
    monkeypatch.setattr(module, "ROOT", agent)
    monkeypatch.setattr(module, "PROJECTS", {"agent": agent, "voice": voice})
    monkeypatch.setattr(module, "version", lambda name: "test-version")
    return module, agent, voice


def test_new_backend_module_is_frozen_and_inventory_change_is_rejected(freeze_module, monkeypatch, capsys):
    module, agent, voice = freeze_module
    base = agent / "base.json"
    base.write_text(json.dumps({"files": [{"project": "agent", "path": "src/pkg/original.py", "benchmark_source": True}], "sdks": {"deepagents": "test-version"}}), encoding="utf-8")
    (agent / "src/pkg/new_helper.py").write_text("helper = True\n", encoding="utf-8")
    frozen = agent / "frozen.json"
    monkeypatch.setattr(sys, "argv", ["freeze", "--base", str(base), "--version", "TEST", "--benchmark-root", "verification/test", "--output", str(frozen)])
    module.main()
    record = json.loads(frozen.read_text(encoding="utf-8"))
    assert {(r["project"], r["path"]) for r in record["files"]} == {
        ("agent", "src/pkg/original.py"), ("agent", "src/pkg/new_helper.py"), ("voice", "app/main.py")}
    assert all(row["benchmark_source"] and len(row["sha256"]) == 64 for row in record["files"])
    assert len(record["python_source_inventory"]) == 3
    monkeypatch.setattr(sys, "argv", ["freeze", "--verify", str(frozen)])
    with pytest.raises(SystemExit) as passed:
        module.main()
    assert passed.value.code == 0
    (voice / "app/added_after_freeze.py").write_text("changed = True\n", encoding="utf-8")
    with pytest.raises(SystemExit) as failed:
        module.main()
    assert failed.value.code == 1
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["inventory_changed"] is True


def test_deleted_or_changed_source_is_reported_without_crashing(freeze_module, monkeypatch, capsys):
    module, agent, voice = freeze_module
    rows = module.python_source_inventory()
    for row in rows:
        row.update(benchmark_source=True, sha256=module.digest(module.PROJECTS[row["project"]] / row["path"]))
    frozen = agent / "frozen.json"
    frozen.write_text(json.dumps({"files": rows, "sdks": {}, "python_source_inventory": module.python_source_inventory(), "runner_sha256": module.digest(agent / "verification/run_efficiency_live_v1.py")}), encoding="utf-8")
    (agent / "src/pkg/original.py").unlink()
    (voice / "app/main.py").write_text("voice = False\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["freeze", "--verify", str(frozen)])
    with pytest.raises(SystemExit) as failed:
        module.main()
    assert failed.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["inventory_changed"] is True
    assert len(result["changed_sources"]) == 2
