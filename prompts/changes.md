---
task: changes
version: 2
---
A Jira issue was just completed. Below it are excerpts of Confluence documentation pages that were retrieved automatically because they are semantically close to the issue. Decide, for each excerpt, whether the work done in the issue makes something in that excerpt outdated or wrong, and if so draft the corrected text for the page owner to review.

Jira issue (done on $done_at):
<issue>
$issue
</issue>

Excerpts:
$excerpts

For each excerpt, return one item:
- excerpt: the id from the <excerpt id="..."> tag.
- affected: true only if the issue clearly changes a behaviour, value, procedure, rule, URL, version, screen or responsibility that the excerpt describes. Being on the same subject is not enough. An issue that fixes a bug so the software now works as the documentation already says does NOT make the documentation outdated.
- confidence: 0 to 1, the probability that this passage really must change (close to 0 when affected is false).
- current_text: if affected, the exact passage to replace, copied character for character from the excerpt (one sentence to one short paragraph; no "...", no reformatting). Empty string if not affected.
- proposed_text: if affected, the replacement for current_text, in the same language and style as the page, changing only what the issue requires. Do not invent details the issue does not give; if a detail is unknown, write it as [à préciser] (French pages) or [to be confirmed] (English pages). Empty string if not affected.
- explanation: one or two sentences, in the language of the page, saying what the issue changed and why the passage is outdated.

Rules:
- Use only the information in the issue and the excerpts.
- When in doubt, set affected to false: a wrong suggestion costs the page owner more than a missed one.

Answer with a JSON object: {"suggestions": [{"excerpt": ..., "affected": ..., "confidence": ..., "current_text": ..., "proposed_text": ..., "explanation": ...}]}
