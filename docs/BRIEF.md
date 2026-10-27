# Project brief: Confluence Documentation Health Tool

## My environment (fill in before starting)
- Confluence: [Cloud / Data Center], site URL: [https://yourcompany.atlassian.net]
- Confluence plan: [Free / Standard / Premium / Enterprise] (affects whether page view analytics are available)
- Jira: [Cloud / Data Center / not used], projects to watch: [PROJ1, PROJ2]
- Spaces to analyze: [SPACE1, SPACE2]; test space for development: [TEST]
- Report location: space [KEY], parent page: [page title or ID]
- My OS: [macOS / Linux / Windows]
- Claude plan: [Pro / Max]

## Context
In our Confluence, about 90% of pages are never read and many are obsolete because requirements change and features evolve. Even the most-read pages may contain outdated information. Atlassian Rovo already provides AI search and chat, so this tool must NOT include a chatbot. Its output is a report page published to Confluence, plus suggested edits posted as comments.

## The three features
1. Archive triage: find unread or stale pages and classify each as relevant, obsolete, duplicate, or historical_record (meeting notes, decision logs, and retrospectives are worth keeping but should be labeled as such).
2. Duplicates and contradictions: cluster pages covering the same topic with embeddings, have the LLM highlight conflicting statements, and recommend one canonical page to merge into.
3. Change detection with proposed edits: when Jira issues are resolved, find pages they make outdated, draft replacement text, and post it as a comment for the page owner to approve. A human always stays in the loop.

## Hard constraints
- Free and open-source tools only. No paid services, no pay-as-you-go APIs, no free tiers of paid services.
- The only LLM is Claude, accessed through the Claude Code CLI in headless mode (claude -p) using my subscription, invoked as a subprocess. Never call the Anthropic API directly, never read or reuse Claude Code's credentials, and exit with a clear error at startup if ANTHROPIC_API_KEY is set.
- Runs locally on my machine; I will schedule it with cron or Task Scheduler.
- Never archive, delete, move, or edit existing pages. The tool only reports and comments.
- All writes to Confluence (report page, comments) must support --dry-run, which writes outputs to local files instead.
- Subscription usage limits are shared with my normal Claude use, so minimize LLM calls: rule-based prefiltering before any LLM call, incremental processing using content hashes, caching of every LLM result, sensible truncation of page content, a cheaper model (haiku) for classification and a stronger one (sonnet) for comparison and drafting, both configurable. If a rate limit is hit, stop gracefully, save progress, and resume on the next run.

## Tech stack
- Python 3.11+, requests for Confluence and Jira REST APIs (email + API token from a .env file), with pagination, retries, and backoff on 429.
- SQLite (stdlib) for page inventory, content hashes, embeddings, LLM results, posted comments, and run history.
- sentence-transformers running locally (default all-MiniLM-L6-v2, configurable), numpy and scikit-learn for similarity and clustering.
- beautifulsoup4 or markdownify to turn page storage format into clean text.
- config.yaml for spaces, thresholds, models, and JQL; a CLI with commands per phase (inventory, triage, duplicates, changes, report, run-all).
- pytest, with Atlassian and Claude calls mocked.

## LLM wrapper requirements
- Prompt templates stored as separate files in /prompts so I can tune them.
- Call claude -p with --output-format json and the configured --model; require the model to answer in a strict JSON schema; validate the output and retry once on invalid JSON.
- Cache results keyed by a hash of (prompt template version + input content).
- Log number of calls, cache hits, and failures per run.

## Phase 1: Inventory and archive triage
- Fetch all pages in configured spaces: id, title, space, URL, creator, last modifier, created and modified dates, version, labels, parent, body.
- Fetch view counts and last-viewed dates if the analytics API is available on our plan; if not, degrade gracefully to date-based rules and say so in the report.
- Prefilter candidates with configurable rules (default: no views in 365 days AND not modified in 540 days; without analytics, modification date only).
- For each candidate, Claude returns JSON: category (relevant | obsolete | duplicate | historical_record), confidence (0 to 1), one-sentence reason.
- Report section: summary counts, then a table per category sorted by confidence, with page links, owner, and last modified date.

## Phase 2: Duplicates and contradictions
- Split pages into heading-aware chunks (~500 tokens), embed them, and compute page-to-page similarity.
- Group pages above a similarity threshold (default 0.85). Provide a command that prints sample pairs at several thresholds so I can tune it.
- For each cluster (max 5 pages, split bigger ones), Claude returns JSON: topic, conflicts (statement A with page, statement B with page, explanation), recommended canonical page with reason, merge notes.
- Skip clusters whose pages are unchanged since the last analysis.

## Phase 3: Change detection and proposed edits
- Fetch Jira issues resolved since the last run using a configurable JQL (summary, description, resolution, fix version, linked issues).
- For each issue, find the top 5 related page chunks via embeddings, above a configurable threshold.
- Claude returns JSON: affected (bool), confidence, exact current text from the page, proposed replacement text, explanation.
- If affected and confidence is above the threshold, post a footer comment on the page mentioning the page owner, containing the current text, the proposed text, a link to the Jira issue, and the line "AI-generated suggestion, please verify before applying." Never post the same suggestion twice (track in SQLite).

## Report page
- One new page per run under the configured parent, titled "Documentation Health Report – YYYY-MM-DD".
- Sections: summary metrics, archive triage, duplicates and contradictions, change suggestions, run statistics (pages scanned, LLM calls, cache hits, errors).
- Confluence storage format with tables and status lozenge macros. Also save a local Markdown copy.

## How I want us to work
1. Read this whole brief, then ask me only the essential clarifying questions.
2. Propose the project structure and wait for my approval before writing code.
3. Build one phase at a time. A phase is done when it runs end to end with --dry-run on the test space, tests pass, and I have approved the output. Do not start the next phase before that.
4. Write a README covering setup (API token, .env, installation, first run, scheduling).
5. Save this brief as docs/BRIEF.md and keep docs/PROGRESS.md updated after each step, so we can continue in later sessions.
