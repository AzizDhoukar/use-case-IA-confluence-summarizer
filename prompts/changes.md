---
task: changes
version: 3
---
A Jira issue was completed. Below it are excerpts of Confluence documentation pages retrieved automatically because they are semantically close to the issue. Check whether the documentation still matches what was actually done in the issue (summary, description and comments; the most recent comments describe the final state best).

Jira issue (done on $done_at):
<issue>
$issue
</issue>

Excerpts:
$excerpts

For each excerpt, return one item:
- excerpt: the id from the <excerpt id="..."> tag.
- verdict:
  - discrepancy: the excerpt describes a behaviour, value, rule, procedure, URL, version, screen or responsibility differently from what the issue says was done or decided. The documentation is now wrong or outdated on that point.
  - consistent: the excerpt describes the same thing as the issue, and they agree (including a bug fix that makes the software behave as the documentation already says).
  - unrelated: the excerpt is about something else.
- confidence: 0 to 1, the probability that there really is a discrepancy (close to 0 for consistent or unrelated).
- doc_text: for a discrepancy, the exact passage of the excerpt that is wrong, copied character for character (one sentence to one short paragraph; no "...", no reformatting). Empty string otherwise.
- ticket_text: for a discrepancy, what the issue says was done or decided, quoted or closely paraphrased from the issue or its comments, with the comment author and date if it comes from a comment. Empty string otherwise.
- proposed_text: for a discrepancy, the corrected replacement for doc_text, in the same language and style as the page, changing only what the issue requires. If the issue does not give enough detail to write it, an empty string (the discrepancy is still reported). Empty string otherwise.
- explanation: one or two sentences, in the language of the page, saying what differs and why.

Rules:
- Use only the information in the issue and the excerpts. Do not invent details.
- A difference in scope or level of detail is not a discrepancy; only a real contradiction is.
- When in doubt, choose consistent or unrelated: a false alert costs the page owner more than a missed one.

Answer with a JSON object: {"excerpts": [{"excerpt": ..., "verdict": ..., "confidence": ..., "doc_text": ..., "ticket_text": ..., "proposed_text": ..., "explanation": ...}]}
