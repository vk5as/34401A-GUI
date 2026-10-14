"""Claude Code PreToolUse hook: refuse the shell commands that hurt an agent working in this repository.

`pkill -f PATTERN` matches the agent's own shell, whose command line contains the pattern, and kills it. A bare
`git stash` (or `stash pop`) uses the one stash stack every git worktree shares, so it can take another agent's
work. Wired up in `.claude/settings.json`; reads the hook's JSON on stdin and prints a deny decision, or nothing.
"""

import json
import re
import sys

_PKILL_F = re.compile(r"(?:^|[\s;&|(])pkill\b(?:\s+-\S+)*?\s+-\w*f\w*\b")
# `git [-C dir] stash` followed by nothing, flags, `pop` or a bare `push` without a message: the forms that touch
# the shared stack blindly. Inspecting it, applying or dropping by name, and a tagged push stay allowed.
_GIT_STASH = re.compile(r"(?:^|[\s;&|(])git(?:\s+-C\s+\S+)?\s+stash\b(?P<rest>[^;&|]*)")
_SAFE_STASH = re.compile(r"^\s*(?:list|show|apply|drop)\b|^\s*push\b.*\s-m\s")
# Quoted text is an argument, not a command: blank it so `echo 'git stash'` passes while the flags outside stay.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def verdict(command: str) -> str | None:
    """Return why `command` is refused, or None when it may run."""
    command = _QUOTED.sub("''", command)
    if _PKILL_F.search(command):
        return (
            "pkill -f matches this shell's own command line and kills it. Find the process with `pgrep -fa PATTERN` "
            "and stop it by PID, or use `pkill` without -f on the exact process name."
        )
    for match in _GIT_STASH.finditer(command):
        if not _SAFE_STASH.search(match.group("rest")):
            return (
                "The stash stack is shared by every git worktree, so a bare `git stash` or `stash pop` can take "
                "another agent's work. Commit a WIP instead, or `git stash push -u -m <tag>` and apply it by SHA."
            )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        command = payload["tool_input"]["command"]
    except (ValueError, KeyError, TypeError):
        return 0
    reason = verdict(command) if isinstance(command, str) else None
    if reason is not None:
        decision = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}
        sys.stdout.write(json.dumps({"hookSpecificOutput": decision}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
