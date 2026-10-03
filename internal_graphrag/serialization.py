"""Restore typed extraction objects from immutable JSON snapshots."""

from __future__ import annotations

import json
from pathlib import Path

from .extract import Effect, Extraction, Instrument, Span
from .source import Record


def load_extraction(path: Path) -> Extraction:
    data = json.loads(path.read_text(encoding="utf-8"))
    publication = data["publication"]
    publication["metadata_relations"] = tuple(
        tuple(x) for x in publication["metadata_relations"]
    )
    return Extraction(
        Record(**publication),
        tuple(Instrument(**x) for x in data["instruments"]),
        tuple(tuple(x) for x in data["mentions"]),
        tuple(data["provisions"]),
        tuple(Span(**x) for x in data["spans"]),
        tuple(Effect(**x) for x in data["effects"]),
        data.get("organization"),
        data.get("procedure"),
        data.get("place"),
        tuple(tuple(x) for x in data.get("categories", [])),
        tuple(data.get("activities", [])),
    )
