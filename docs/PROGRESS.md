# Progress log

## Status
- Phase 1 (inventory + triage): accepted by user ("more or less done") after real runs on the
  personal space and CDP. Follow-up delivered: page-subtree selection (branch
  `feature/subtree-selection`).
- Phase 2 (duplicates & contradictions): accepted by user ("phase 2 is ok"). torch 2.14.1+cpu
  and sentence-transformers 6.2.0 work in the /mnt/c .venv (importing torch from /mnt/c takes
  ~3 min per run; a venv on the WSL disk would be faster).
- Phase 3 (Jira change detection): built on branch `feature/phase-3-changes`. First version
  validated on a scratch copy of CDP "Espace Team PO/CPO". Then reworked at the user's request:
  Jira from a start date (`jira.since` / `--since`), issue comments, verdict
  consistent | discrepancy | unrelated with "page says / issue says / proposed text (optional)",
  alerts = report section + owner comment. Plus section-level contradictions between pages
  (`phases/conflicts.py`, runs with `duplicates`). 74 tests pass. Real dry run on the personal
  space only: 213 CDP issues since 2026-09-01 -> 61 after rules -> 6 analyzed (cap), all
  unrelated; duplicates found a real contradiction between the two ngrok pages (.env
  variables). Awaiting user approval; the user runs it on CDP themselves.
- **Testing rule (user, 2026-10-27): real runs by Claude only on the personal space
  `~7120202e66b1f59d6249ea8d6ed85d67c677b5`; tests on team spaces are done by the user.**
- The tool ignores its own report pages (title "Documentation Health Report – ...") in triage,
  duplicates, conflicts and Jira matching (they were being analyzed in the personal space).
- Report: triage sections now ordered relevant, duplicate, obsolete, historical, with a
  category definitions table after the summary (user request).
- Repo: https://github.com/AzizDhoukar/use-case-IA-confluence-summarizer. Phase 1 landed
  directly on main (user pushed). Later work goes on branches + PRs; pushing from this machine
  needs `gh auth login` + `gh auth setup-git` (not done yet).

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

- Selection: `spaces` entries may be `{key, include_pages, exclude_pages}` (page IDs or exact
  titles, each = page + descendants); CLI `--page` / `--exclude-page` with one `--space`.
  Scoped inventory = one CQL query (`space = K AND type = page AND (id = X OR ancestor = X)`,
  expand version + labels) then bulk v2 `GET /pages?id=...` only for new/changed versions.
  Verified on CDP: 144 pages, second run downloads 0 bodies. Whole-space mode unchanged.
- Phase 2 embedding model: `paraphrase-multilingual-MiniLM-L12-v2` (content is French/mixed,
  user choice). Its max_seq_length is 128 tokens, so a ~500-token chunk is embedded as the mean
  of ~128-token windows. Page vector = normalized mean of chunk vectors; average-linkage
  clustering at `duplicates.threshold`; groups > 5 split greedily around the most central page.
- Duplicates prompt returns relationship (duplicate | overlapping | related_but_distinct);
  related_but_distinct groups are counted but not listed. Conflicts citing pages outside the
  cluster are dropped. Cluster signature = hash of (page id, content hash) pairs -> unchanged
  clusters are skipped.
- CPU-only torch (download.pytorch.org/whl/cpu) to keep the install small.

- Phase 3: Jira search via POST /rest/api/3/search/jql (nextPageToken). CDP's workflow never
  sets resolution/resolutiondate, so the date filter is `statusCategoryChangedDate` (configurable:
  resolved / updated). Cursor = meta `jira_since` (run time), queried with 1 day overlap; issues
  stored in `jira_issues` (pending | done | skipped | no_match | failed), re-pending if done again.
- Phase 3 prefilter: sub-tasks (~2/3 of CDP issues), French/English abandoned / refused / duplicate
  statuses and resolutions, optional issue types. Target pages exclude historical records (rule
  or triage) and triage-obsolete pages. Issues sorted by best chunk similarity before LLM calls.
- Phase 3 LLM: one sonnet call per issue with top-5 chunks (`prompts/changes.md`); current_text
  must be found literally (whitespace-normalized) in the chunk, else rejected.
- Suggestions table: fingerprint = hash(page, current, proposed) UNIQUE -> never posted twice,
  also skipped if the same issue already has a posted comment on the page; re-checked against the
  current page text before posting (status outdated). Dry-run writes comments to
  output/dry-run/<stamp>/comments and marks dry_run; a later real run posts them.
- Section-level conflicts: chunk x chunk cosine across different pages (blockwise), grouped by
  page pair (top 2 passage pairs), pairs already in a Phase 2 cluster skipped, one sonnet call
  per page pair (`prompts/conflicts.md`), statements must be literal in the passages.
  Table `passage_pairs`, signature = hash of both passages -> unchanged pairs not re-sent.
- Similarity of issue vs chunk with the multilingual MiniLM is compressed (0.49-0.77 on CDP):
  default threshold 0.65.

## Open points to check on the real site
- Analytics endpoint `GET /wiki/rest/api/analytics/content/{id}/views?fromDate=YYYY-MM-DD`
  (date format and plan availability).
- `users-bulk` response fields (displayName / accountStatus) for owner fallback.

## Log
- 2026-10-26: Brief saved to docs/BRIEF.md. Clarifying questions sent.
- 2026-10-26: User confirmed Pro plan; Confluence details deferred. Structure proposed and approved.
- 2026-10-27: Phase 1 implemented (config, db, Atlassian client, text, Claude CLI wrapper,
  inventory, triage, report storage+markdown, dry-run publisher, CLI), README written,
  31 tests passing. User pushed it to main.
- 2026-10-27: Real runs OK (personal space, CDP; page analytics available on this plan).
  Added page-subtree selection + friendlier unknown-space error. Implemented Phase 2
  (lazy imports so Phase 1 runs without its deps). Selection + Phase 2 merged into main.
- 2026-10-27: Phase 2 accepted. Phase 3 implemented (Jira client, change detection, footer
  comments via publisher, report section), validated with capped dry runs on a scratch DB.
  Report: category order + definitions.
- 2026-10-27: Phase 3 reworked (since date, comments, discrepancies, alerts) + section-level
  page contradictions; own report pages excluded. Real tests restricted to the personal space.
