---
task: triage
version: 1
---
You are reviewing a Confluence page that nobody has read or edited for a long time, to decide whether it should be kept, archived, or labeled.

Classify the page into exactly one category:
- relevant: still accurate and useful reference material (e.g. stable how-tos, architecture still in use, onboarding, policies still in force).
- obsolete: describes things that are clearly outdated, superseded, abandoned, finished temporary work, or is empty / a placeholder with no useful content.
- duplicate: substantially repeats another page (e.g. a "Copy of" page, an older version of a page listed in "Similar titles", a draft of a page that exists elsewhere).
- historical_record: worth keeping as a record of the past even though not current: meeting notes, decision logs, retrospectives, release notes, incident post-mortems, status reports.

Rules:
- Judge only from the information below. Do not invent facts.
- Prefer historical_record over obsolete for dated records of meetings, decisions, retrospectives or incidents.
- Use duplicate only with a concrete signal (title, "copy of", near-identical similar title, or content saying it moved).
- confidence is your probability (0 to 1) that the category is correct; use lower values when the content is short or ambiguous.
- reason: one sentence, in the same language as the page content, citing the concrete signal.

Page metadata:
- Title: $title
- Space: $space
- Labels: $labels
- Created: $created
- Last modified: $modified (by $last_modifier)
- Views: $views
- Similar titles in the same space: $similar_titles

Page content (may be truncated):
<<<
$content
>>>

Answer with a JSON object: {"category": ..., "confidence": ..., "reason": ...}
