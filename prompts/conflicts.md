---
task: conflicts
version: 1
---
Below are passages from two different Confluence pages (A and B). They were paired automatically because they are semantically close. Find statements that CONTRADICT each other between page A and page B: the same thing (a rule, value, threshold, procedure step, URL, version, owner, date, behaviour) described differently, so that a reader following one page would act differently than a reader following the other.

$passages

Rules:
- Only real contradictions about the same thing. Not: different scope, different level of detail, information present in only one page, different products/portals/clients clearly identified as such, or an older plan versus a later decision when the pages say so.
- statement_a: quoted exactly, character for character, from a passage of page A (ids A1, A2...). statement_b: same for page B. One sentence or a short phrase each; no "..." and no reformatting.
- explanation: one sentence, in the language of the pages, saying what differs.
- Return an empty list if there is no contradiction (the usual case).

Answer with a JSON object: {"conflicts": [{"statement_a": ..., "statement_b": ..., "explanation": ...}]}
