"""Strict JSON schemas the model must answer with (passed to claude --json-schema and re-validated)."""

TRIAGE_CATEGORIES = ["relevant", "obsolete", "duplicate", "historical_record"]

TRIAGE = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": TRIAGE_CATEGORIES},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "minLength": 1, "maxLength": 400},
    },
    "required": ["category", "confidence", "reason"],
    "additionalProperties": False,
}

RELATIONSHIPS = ["duplicate", "overlapping", "related_but_distinct"]

DUPLICATES = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "minLength": 1, "maxLength": 200},
        "relationship": {"type": "string", "enum": RELATIONSHIPS},
        "conflicts": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "page_a": {"type": "string"},
                    "statement_a": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "page_b": {"type": "string"},
                    "statement_b": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "explanation": {"type": "string", "minLength": 1, "maxLength": 400},
                },
                "required": ["page_a", "statement_a", "page_b", "statement_b", "explanation"],
                "additionalProperties": False,
            },
        },
        "canonical_page": {"type": "string"},
        "canonical_reason": {"type": "string", "maxLength": 400},
        "merge_notes": {"type": "string", "maxLength": 3000},
    },
    "required": ["topic", "relationship", "conflicts", "canonical_page", "canonical_reason",
                 "merge_notes"],
    "additionalProperties": False,
}

CONFLICTS = {
    "type": "object",
    "properties": {
        "conflicts": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "statement_a": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "statement_b": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "explanation": {"type": "string", "minLength": 1, "maxLength": 400},
                },
                "required": ["statement_a", "statement_b", "explanation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["conflicts"],
    "additionalProperties": False,
}

CHANGE_VERDICTS = ["consistent", "discrepancy", "unrelated"]

CHANGES = {
    "type": "object",
    "properties": {
        "excerpts": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "excerpt": {"type": "string"},
                    "verdict": {"type": "string", "enum": CHANGE_VERDICTS},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "doc_text": {"type": "string", "maxLength": 1500},
                    "ticket_text": {"type": "string", "maxLength": 1500},
                    "proposed_text": {"type": "string", "maxLength": 3000},
                    "explanation": {"type": "string", "maxLength": 600},
                },
                "required": ["excerpt", "verdict", "confidence", "doc_text", "ticket_text",
                             "proposed_text", "explanation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["excerpts"],
    "additionalProperties": False,
}
