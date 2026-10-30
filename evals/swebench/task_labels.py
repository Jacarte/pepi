"""Public issue titles for reporting only; never task inputs or frozen preset identity."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from .common import SECRET_PATTERN

CATALOG = json.loads(Path(__file__).with_name("task-labels.json").read_text(encoding="utf-8"))
ISSUE_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*")
LABEL_MAX_CHARS = 300


def display_metadata(instance_id: str, row: dict | None = None) -> dict:
    """Prefer saved report metadata; older summaries use the public catalog, unknown IDs stay unnamed."""
    for entry in (row, CATALOG.get(instance_id)):
        if not isinstance(entry, dict):
            continue
        label, url = entry.get("display_label"), entry.get("source_url")
        if (not isinstance(label, str) or not label.strip() or len(label) > LABEL_MAX_CHARS
                or any(unicodedata.category(char).startswith("C") or char in "\u2028\u2029" for char in label)
                or SECRET_PATTERN.search(label)):
            continue
        return {"display_label": label.strip(),
                "source_url": url if isinstance(url, str) and ISSUE_URL.fullmatch(url) else None}
    return {"display_label": None, "source_url": None}


def csv_label(label: str | None) -> str | None:
    """CSV quoting alone does not prevent spreadsheets from executing a title as a formula."""
    return "'" + label if label and label.lstrip().startswith(("=", "+", "-", "@")) else label
