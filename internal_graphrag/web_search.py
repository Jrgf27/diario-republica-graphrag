"""Small, dependency-free parser for source passage searches."""

from __future__ import annotations

import re

STOPWORDS = {
    "a", "as", "about", "ao", "aos", "da", "das", "de", "do", "dos",
    "em", "e", "for", "in", "is", "no", "na", "nos", "nas", "o", "os",
    "que", "qual", "quais", "sobre", "the", "to", "um", "uma",
    "what", "which", "with", "and", "how", "por", "para", "please",
    "changed", "change", "changes", "mudou", "muda", "mudanças", "alterou",
    "alterações", "aconteceu", "happened", "year", "ano",
}
TERM_ALIASES = {"vat": "iva"}


def parse_search(question: str) -> tuple[list[str], str | None]:
    """Extract substantive words and an optional publication year."""
    words = re.findall(r"[^\W_]+", question.casefold(), re.UNICODE)
    year = next((word for word in words if re.fullmatch(r"(?:19|20)\d{2}", word)), None)
    terms = list(dict.fromkeys(
        TERM_ALIASES.get(word, word) for word in words
        if word not in STOPWORDS and word != year and len(word) > 1
    ))[:6]
    return terms, year
