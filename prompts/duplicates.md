---
task: duplicates
version: 1
---
The Confluence pages below were grouped automatically because their content is semantically similar. Analyze them together.

Tasks:
1. topic: name the shared topic in a few words.
2. relationship:
   - duplicate: the pages substantially repeat each other (copies, older/newer versions, drafts);
   - overlapping: they cover the same subject with significant shared content but each has its own material;
   - related_but_distinct: same domain but different subjects; merging would not make sense.
3. conflicts: list statements that CONTRADICT each other between two pages (different values, versions, URLs, procedures, owners, rules, dates for the same thing). Quote each statement exactly as written in its page (shortened with "..." if long). Use the page id from the <page id="..."> tag for page_a and page_b. Only real contradictions: not differences in level of detail, not information present in only one page. Empty list if none.
4. canonical_page: the id of the single page that should be kept as the reference and receive merged content. Prefer the most complete, most recently maintained and most viewed page. canonical_reason: one sentence.
5. merge_notes: concrete instructions for the page owner: what to move from which page into the canonical page, which statements to verify first (the conflicts), and which pages could then be archived. Empty string if relationship is related_but_distinct.

Rules:
- Use only the information in the pages. Do not invent facts.
- Write topic, explanations, reasons and merge notes in the language of the pages.

Pages:
$pages

Answer with a JSON object: {"topic": ..., "relationship": ..., "conflicts": [{"page_a": ..., "statement_a": ..., "page_b": ..., "statement_b": ..., "explanation": ...}], "canonical_page": ..., "canonical_reason": ..., "merge_notes": ...}
