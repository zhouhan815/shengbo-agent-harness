"""Stopping after an observed defect must not launch another paid round."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_live_runner_v1_safety import runner


@pytest.mark.asyncio
async def test_suite_stops_between_rounds_and_preserves_completed_observation(tmp_path):
    root = tmp_path / "fresh-suite"
    args = SimpleNamespace(root=root, rounds=3)
    launched = []
    def launch(current, processes):
        current.root.mkdir(parents=True)
        launched.append(current.root.name)
    async def drive(current):
        (root / "stop-suite.flag").write_text("known defect; cancel observed", encoding="utf-8")
        result = {"status": "CANCELLED", "benchmark_eligible": False}
        runner.save(current.root / "result.json", result)
        return {"complete": True, "statuses": {"test": "CANCELLED"}, "overall": {"machine_elapsed_seconds": 42}}
    with patch.object(runner, "launch", launch), patch.object(runner, "drive", drive):
        reports = await runner.suite(args)
    assert launched == ["round-01"]
    assert len(reports) == 1
    assert not (root / "round-02").exists()
    assert json.loads((root / "round-01/result.json").read_text(encoding="utf-8"))["status"] == "CANCELLED"
    stop = json.loads((root / "suite-stop.json").read_text(encoding="utf-8"))
    assert stop["observed_rounds"] == 1 and stop["not_a_complete_suite"] is True
    assert json.loads((root / "comparison.json").read_text(encoding="utf-8"))["successful_terminal_rounds"] == 0
