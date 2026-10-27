# Progress log

## Status
- Current step: Phase 1 (inventory + archive triage) code complete, 31 tests passing (mocked).
- Waiting for: Confluence details (site, test space, report parent) to do the real
  `run-all --dry-run --space <TEST>` and get user approval of the output.
- Next: after Phase 1 approval, Phase 2 (duplicates & contradictions).
- Repo: https://github.com/AzizDhoukar/use-case-IA-confluence-summarizer (work goes in via PRs;
  user reviews and merges).

## Environment notes
- Dev machine: Windows + WSL2 (Linux 6.6), Python 3.12.3, Claude Code CLI 2.1.310 at ~/.local/bin/claude
- WSL Python lacks ensurepip/python3-venv: the project .venv was created with `python3 -m venv`
  and pip bootstrapped with get-pip.py. (`sudo apt install python3-venv` would fix it properly.)
- gh CLI 2.103.3 installed to ~/.local/bin/gh (no sudo).

## Decisions
- Claude plan: **Pro**. Default cap: 40 LLM calls per run (`llm.max_calls_per_run`).
- Confluence/Jira specifics: to be provided later; assume **Confluence Cloud** (email + API
  token, REST v2) until told otherwise.
- Defaults (no objection raised): comments @mention page owner, falling back to last modifier
  when the owner is inactive; run in WSL via cron; first Jira run looks back 30 days;
  `exclude_spaces` / `exclude_labels` never sent to Claude.
- Claude CLI call (verified output format on CLI 2.1.310):
  `claude -p --output-format json --model <m> --json-schema <schema> --system-prompt <...>
  --no-session-persistence --strict-mcp-config --tools ""`, prompt on stdin, cwd = empty temp dir.
  Answer is read from `structured_output` (fallback: parse `result`). Rate limit detected via
  `api_error_status == 429` or limit wording in an error result. Not using `--bare` (may skip
  subscription OAuth).
- Prompt files are `prompts/<task>.md` with `version` in YAML front matter (not in the filename).
  Cache key = sha256(task, prompt version, model, rendered prompt).
- Triage: candidates = not modified for 540 days AND (if analytics available) 0 views in 365
  days. Analytics probed on the first stale page; unavailable (400/403/404/405/501) -> date-only,
  noted in report. View counts refreshed every 7 days, only for date-stale pages.
- Rule-based `historical_record` (labels / title regex) before any LLM call. `duplicate` in
  triage relies on title signals + up to 5 similar titles (difflib) in the prompt; real
  duplicate detection is Phase 2.
- Labels are fetched only for new pages or pages whose version changed.
- Report: new page per run; if today's title exists, a time suffix is added (never overwrite).
  Run statistics aggregate all runs since the last *published* (non-dry-run) report.

## Open points to check on the real site
- Analytics endpoint `GET /wiki/rest/api/analytics/content/{id}/views?fromDate=YYYY-MM-DD`
  (date format and plan availability).
- `users-bulk` response fields (displayName / accountStatus) for owner fallback.

## Log
- 2026-10-26: Brief saved to docs/BRIEF.md. Clarifying questions sent.
- 2026-10-26: User confirmed Pro plan; Confluence details deferred. Structure proposed and approved.
- 2026-10-27: Phase 1 implemented (config, db, Atlassian client, text, Claude CLI wrapper,
  inventory, triage, report storage+markdown, dry-run publisher, CLI), README written,
  31 tests passing. Pushed as PR for review.
