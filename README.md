# dochealth: Confluence Documentation Health Tool

Finds stale, obsolete, duplicate and outdated Confluence pages and reports them. Output is
a **report page published to Confluence** (plus a local Markdown copy) and, in a later phase,
**suggested edits posted as page comments**. There is no chatbot.

The tool **never edits, moves, archives or deletes pages**: it only creates the report page
and (Phase 3) adds comments. Every write can be redirected to local files with `--dry-run`.

The only LLM is Claude, through the **Claude Code CLI in headless mode (`claude -p`)** using
your Claude subscription. The tool never calls the Anthropic API and refuses to start if
`ANTHROPIC_API_KEY` is set.

| Phase | Feature | Status |
|---|---|---|
| 1 | Inventory + archive triage (relevant / obsolete / duplicate / historical_record) | built |
| 2 | Duplicates and contradictions (embeddings + clustering) | planned |
| 3 | Change detection from resolved Jira issues, proposed edits as comments | planned |

See `docs/BRIEF.md` for the full specification and `docs/PROGRESS.md` for status.

## Requirements

- Python 3.11+ (Linux, macOS or WSL on Windows)
- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and logged in with
  your subscription (`claude` works in a terminal)
- A Confluence Cloud account with read access to the spaces to analyze, and permission to
  create pages in the report space

## Setup

### 1. Create an Atlassian API token

1. Go to <https://id.atlassian.com/manage-profile/security/api-tokens>.
2. **Create API token**, name it `dochealth`, copy the value.
3. The token has your permissions; consider a dedicated service account with read access
   to the analyzed spaces and write access to the report space only.

### 2. Install

```bash
git clone https://github.com/AzizDhoukar/use-case-IA-confluence-summarizer.git dochealth
cd dochealth
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

On Debian/Ubuntu (including WSL), if `python3 -m venv` fails with "ensurepip is not
available", install `sudo apt install python3-venv` first.

### 3. Configure

```bash
cp .env.example .env              # then edit: site URL, email, API token
cp config.example.yaml config.yaml
```

`.env`:

```
ATLASSIAN_SITE_URL=https://yourcompany.atlassian.net
ATLASSIAN_EMAIL=you@yourcompany.com
ATLASSIAN_API_TOKEN=...
```

`config.yaml`: the essentials are

- `spaces`: space keys to analyze (start with your test space). Use the **key** from the
  space URL (`/wiki/spaces/<KEY>/...`), quoted; personal space keys start with `~`.
  To analyze only part of a space, give page subtrees (page ID or exact title; each selects
  the page and all its descendants):

  ```yaml
  spaces:
    - key: "CDP"
      include_pages: ["Espace Team PO/CPO", "1590099999"]
      exclude_pages: ["Archives"]
  ```
- `report.space` and `report.parent_id` or `report.parent_title`: where report pages go
  (create the parent page first, e.g. "Documentation Health")
- `exclude_spaces` / `exclude_labels`: content that must never be sent to Claude
- `llm.max_calls_per_run`: hard cap on Claude calls per run (default 40, suitable for Pro)
- `llm.models`: `haiku` for classification, `sonnet` for comparison and drafting
- `triage.*`: prefilter thresholds (defaults: not modified for 540 days, and, when page
  analytics are available, no views in 365 days)

Both `.env` and `config.yaml` are git-ignored.

## First run

Always start with a dry run on a test space. Reads from Confluence are real; the report
page is written to `output/dry-run/<timestamp>/` instead of Confluence.

```bash
source .venv/bin/activate        # once per terminal; otherwise: dochealth: command not found
dochealth run-all --dry-run --space TEST
```

Then look at:

- `output/dry-run/<timestamp>/report-page.storage.html`: the page that would be created
- `output/reports/<date>-<time>.md`: local Markdown copy of the report
- `output/logs/dochealth-<date>.log`: run log

When you are happy with it, drop `--dry-run` to publish the report page.

## Commands

| Command | What it does |
|---|---|
| `dochealth inventory` | Fetch pages of the configured spaces into `data/dochealth.sqlite` |
| `dochealth triage` | Prefilter stale pages by rules, classify the candidates with Claude |
| `dochealth report` | Build the report page and the Markdown copy |
| `dochealth run-all` | inventory, then triage, then report |
| `dochealth duplicates`, `tune-threshold`, `changes` | Phases 2 and 3 (not built yet) |

Common options: `--dry-run`, `--config PATH`, `--space KEY` (repeatable, overrides config),
`--page ID_OR_TITLE` / `--exclude-page ID_OR_TITLE` (repeatable, with a single `--space`:
only that page subtree),
`--max-llm-calls N`, `-v`.

Exit codes: `0` ok, `1` error, `2` configuration error, `75` Claude usage limit reached
(progress saved; the next run resumes).

## How Claude usage is kept low

- **Rule-based prefilter**: only stale (and, with analytics, unread) pages are candidates.
  Pages labeled or titled like meeting notes, retrospectives or decision logs are classified
  `historical_record` by rule, without an LLM call.
- **Incremental**: each page has a content hash; a page is re-classified only if its content
  or the prompt version changed.
- **Cache**: every Claude answer is cached in SQLite, keyed by task, prompt version, model and
  the exact prompt.
- **Truncation**: page text sent to Claude is capped (`triage.max_chars`).
- **Cheap model first**: `haiku` for triage.
- **Caps and limits**: a per-run call cap; if the subscription usage limit is hit, the run
  stops cleanly, everything done so far is kept, and the next run continues. The report shows
  how many candidates are still waiting.

## Page view analytics

Confluence page analytics are only available on some plans. The tool checks once per run;
if the analytics API is not available, it falls back to modification dates only and the
report says so. Set `triage.use_analytics: false` to skip the check.

## Prompts

Prompt templates live in `prompts/*.md`. Edit them freely; **bump the `version`** in the
front matter when you change one, so cached answers from the old prompt are not reused.

## Scheduling

### Linux / WSL (cron)

```bash
crontab -e
```

```
# Every Monday at 07:00
0 7 * * 1 cd /path/to/dochealth && .venv/bin/dochealth run-all >> output/logs/cron.log 2>&1
```

cron runs with a minimal `PATH`. If `claude` is not found, set `llm.claude_bin` in
`config.yaml` to its full path (`which claude`). On WSL, cron must be running:
`sudo service cron start`, or enable systemd in `/etc/wsl.conf`.

### Windows Task Scheduler (running the tool inside WSL)

Create a task whose action is:

- Program: `wsl.exe`
- Arguments: `-e bash -lc "cd /path/to/dochealth && .venv/bin/dochealth run-all"`

### macOS

Use cron as above, or a launchd agent.

## Tests

```bash
pytest
```

Atlassian HTTP calls are mocked with `responses`, and the Claude CLI is replaced by a fake;
the tests make no network calls and use no Claude quota.

## Data and privacy

Page content of triage candidates (truncated) is sent to Claude through your Claude Code
subscription. Use `exclude_spaces` and `exclude_labels` for anything that must not leave
Confluence. Local data (`data/`, `output/`) contains page text; keep it on your machine.
