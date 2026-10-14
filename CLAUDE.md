# 34401A-GUI

Before changing code, read `docs/agents/implementing.md`: the checks, threading and Tk rules, Windows portability, test seams and extension points.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues on vk5as/34401A-GUI, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

`gh` is not logged in: authenticate each call as the **Auth** section of `docs/agents/issue-tracker.md` shows. Keep the token out of output, files, commits and GitHub text.

### Triage labels

Default vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
