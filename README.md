# dochealth: Confluence Documentation Health Tool

Finds stale, obsolete, duplicate and outdated Confluence pages and reports them. Output is
a **report page published to Confluence** (plus a local Markdown copy) and **suggested edits
posted as page comments**. There is no chatbot.

The tool **never edits, moves, archives or deletes pages**: it only creates the report page
and adds comments. Every write can be redirected to local files with `--dry-run`.

The only LLM is Claude, through the **Claude Code CLI in headless mode (`claude -p`)** using
your Claude subscription. The tool never calls the Anthropic API and refuses to start if
`ANTHROPIC_API_KEY` is set.

| Phase | Feature | Status |
|---|---|---|
| 1 | Inventory + archive triage (relevant / obsolete / duplicate / historical_record) | built |
| 2 | Duplicates and contradictions (embeddings + clustering) | built |
| 3 | Jira vs documentation: discrepancies with done issues, alerts as comments | built |

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
# CPU-only PyTorch first (a few hundred MB instead of several GB of CUDA libraries)
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -e ".[dev]"
```

The first `duplicates` run downloads the embedding model (~470 MB, from Hugging Face) into
`~/.cache/huggingface`; after that everything runs offline on your CPU.

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
| `dochealth duplicates` | Embed pages, group similar ones (contradictions, canonical page), then check close sections of different pages for contradictions |
| `dochealth tune-threshold` | Print page pairs at several similarity thresholds (no LLM calls) |
| `dochealth changes` | Check Jira issues done since a date (summary, description, comments) against the docs; alert discrepancies as page comments |
| `dochealth run-all` | inventory, triage, duplicates, changes (if `jira.jql` is set), then report |

Common options: `--dry-run`, `--config PATH`, `--space KEY` (repeatable, overrides config),
`--page ID_OR_TITLE` / `--exclude-page ID_OR_TITLE` (repeatable, with a single `--space`:
only that page subtree),
`--max-llm-calls N`, `--jql JQL` (overrides `jira.jql`), `--since YYYY-MM-DD` (Jira start date),
`-v`.

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

## Duplicates and contradictions

1. Each in-scope page with at least `duplicates.min_chars` characters is split into
   heading-aware chunks (~500 tokens) and embedded locally with
   `paraphrase-multilingual-MiniLM-L12-v2` (French + English). Embeddings are stored in
   SQLite and recomputed only when a page changes.
2. A page vector is the mean of its chunk vectors. Pages whose cosine similarity is at least
   `duplicates.threshold` (default 0.85) are grouped (average-linkage clustering); groups
   bigger than 5 pages are split.
3. Each group goes to Claude (`sonnet`) with the page texts. It returns the topic, whether the
   pages are duplicates / overlapping / related but distinct, contradicting statements quoted
   from each page, the page to keep (canonical) and merge notes.
4. A group whose pages have not changed since its last analysis is not sent again.
5. **Section level**: pages can disagree on one point while being about different things
   overall (a spec and a support guide). Sections (chunks) of different pages with similarity
   >= `conflicts.passage_threshold` are paired, the best 2 pairs per page pair are sent to
   Claude in one call, and only contradictions quoted literally from both pages are kept
   ("Contradictions between pages" in the report). Page pairs already grouped in step 2, the
   tool's own report pages and historical records are left out; at most
   `conflicts.max_calls_per_run` calls per run, the rest waits for the next run.

Tune the threshold on your own content before relying on it:

```bash
dochealth inventory --space CDP --page "Espace Team PO/CPO"
dochealth tune-threshold --space CDP --page "Espace Team PO/CPO"
```

It prints, per threshold, how many pairs and groups you would get and sample pairs just above
it. Pick the lowest threshold whose samples still look like the same topic, and set
`duplicates.threshold`. Each group costs one Claude call.

## Jira vs documentation (discrepancies)

Uses the same Atlassian API token (it needs read access to the Jira projects).

1. **Fetch**: issues matching `jira.jql` whose `jira.date_field` (default
   `statusCategoryChangedDate`, i.e. when the issue reached a Done status) is after the start
   date: `--since YYYY-MM-DD`, else the end of the previous run, else `jira.since`, else
   `jira.first_run_lookback_days`. Issues are stored, so an interrupted run resumes where it
   stopped, and an issue already checked is never sent to Claude again.
2. **Rules (free)**: sub-tasks, issues abandoned / refused / closed as duplicates, and
   `changes.skip_issue_types` are dropped.
3. **Comments**: fetched only for the issues left after the rules (most recent first, capped
   by `changes.max_comment_chars`): they often hold the final decision.
4. **Retrieval (free, local)**: the issue (summary, description, comments, fix versions, links)
   is embedded and compared with the page chunks. Historical records (sprint pages, meeting
   notes... see `triage.historical_*`) and pages triaged obsolete are left out: they should not
   be updated. Issues with no chunk above `changes.similarity_threshold` stop here.
5. **Claude (`sonnet`, one call per issue, closest matches first)**: for each of the top 5
   excerpts, verdict *consistent*, *discrepancy* or *unrelated*. For a discrepancy: what the
   page says (exact quote; rejected if not literally in the page), what the issue says was
   done, and replacement text when the issue gives enough detail.
6. **Alert**: every discrepancy is listed in the report ("Jira vs documentation"). Those with
   confidence >= `changes.min_confidence` are also posted as a footer comment that @mentions
   the page owner (or the last editor if the owner left), with what the page says, what the
   issue says, the proposed text (or a request to update it), the Jira link and
   *"AI-generated suggestion, please verify before applying."* An alert is never posted twice
   (tracked in SQLite), nor if the owner already changed that text. At most
   `changes.max_comments_per_run` per run.

With `--dry-run`, comments are written to `output/dry-run/<timestamp>/comments/` instead;
a later real run posts them (if still valid).

```bash
dochealth changes --dry-run --since 2026-09-01 --jql 'project = CDP AND statusCategory = Done' --max-llm-calls 6
```

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

Page content of triage candidates, duplicate groups, close sections of different pages and
excerpts matched to Jira issues (truncated), and the text and comments of those Jira issues,
is sent to Claude through your Claude Code
subscription. Use `exclude_spaces` and `exclude_labels` for anything that must not leave
Confluence. Local data (`data/`, `output/`) contains page text; keep it on your machine.
