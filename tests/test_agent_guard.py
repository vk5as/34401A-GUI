import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "agent_guard_bash.py"
_spec = importlib.util.spec_from_file_location("agent_guard_bash", _SCRIPT)
assert _spec is not None
assert _spec.loader is not None
_guard = importlib.util.module_from_spec(_spec)
sys.modules["agent_guard_bash"] = _guard
_spec.loader.exec_module(_guard)
main = _guard.main
verdict = _guard.verdict


@pytest.mark.parametrize(
    "command",
    [
        "pkill -f pytest",
        "pkill -9 -f 'xvfb-run pytest'",
        "sleep 1; pkill -f sim_server",
        "git stash",
        "git stash -q",
        "git stash pop",
        "cd x && git stash push",
        "git -C /repo stash",
    ],
)
def test_commands_that_hurt_the_agent_or_other_worktrees_are_refused(command):
    assert verdict(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "pkill pytest",
        "pgrep -fa pytest",
        "git stash list",
        "git stash show stash@{0}",
        "git stash apply 457b8c1",
        "git stash drop stash@{1}",
        'git stash push -u -m "wip-tag"',
        "git status",
        "echo 'do not use git stash here'",
    ],
)
def test_ordinary_commands_pass(command):
    assert verdict(command) is None


def test_a_refusal_is_reported_as_a_deny_decision_with_the_reason(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "git stash"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert main() == 0

    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert "stash" in output["permissionDecisionReason"]


@pytest.mark.parametrize("stdin", ['{"tool_name": "Bash", "tool_input": {"command": "ls"}}', "not json", "{}"])
def test_anything_else_passes_silently(monkeypatch, capsys, stdin):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))

    assert main() == 0

    assert capsys.readouterr().out == ""
