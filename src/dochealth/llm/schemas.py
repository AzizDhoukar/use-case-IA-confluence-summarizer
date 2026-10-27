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
