"""Deterministic, evidence-first extraction; legal effects remain review candidates."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from hashlib import sha256
from urllib.parse import urlparse

from .ontology import CATEGORIES
from .source import Record

INSTRUMENT = re.compile(
    r"\b(Lei|Decreto-Lei|Decreto Regulamentar|Portaria|Resolução do Conselho de Ministros)\s+"
    r"n\.\s*[ºo°]?\s*(\d+[A-Z]?(?:-\d+)?)\s*/\s*(\d{4})\b",
    re.IGNORECASE,
)
ARTICLE = re.compile(r"(?:^|\n)(Artigo\s+(\d+[A-Z]?)[.º°]?)\b", re.IGNORECASE)
EFFECT = re.compile(r"\b(altera(?:m|do|da)?|revoga(?:m|do|da)?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Span:
    id: str
    start: int
    end: int
    text: str
    source_kind: str = "text"


@dataclass(frozen=True)
class Instrument:
    id: str
    kind: str
    number: str
    year: str
    eli: str | None = None


@dataclass(frozen=True)
class Effect:
    subject_id: str
    predicate: str
    object_id: str
    evidence_id: str
    method: str
    review_status: str = "candidate"


@dataclass(frozen=True)
class Extraction:
    publication: Record
    instruments: tuple[Instrument, ...]
    mentions: tuple[tuple[str, str], ...]
    provisions: tuple[dict, ...]
    spans: tuple[Span, ...]
    effects: tuple[Effect, ...]
    organization: dict | None = None
    procedure: dict | None = None
    place: dict | None = None
    categories: tuple[tuple[str, str], ...] = ()
    activities: tuple[str, ...] = ()

    def as_json(self) -> dict:
        return asdict(self)


def instrument_id(kind: str, number: str, year: str) -> str:
    normalized = re.sub(r"\s+", "-", kind.strip().lower())
    return f"pt:{normalized}:{number.upper()}:{year}"


def instrument_from_eli(url: str) -> Instrument:
    pieces = urlparse(url).path.strip("/").split("/")
    types = {
        "lei": "Lei",
        "dec-lei": "Decreto-Lei",
        "dec-reg": "Decreto Regulamentar",
        "port": "Portaria",
        "resolconsmin": "Resolução do Conselho de Ministros",
    }
    if (
        len(pieces) >= 4
        and pieces[0] == "eli"
        and pieces[1] in types
        and re.fullmatch(r"\d{4}", pieces[3])
    ):
        kind, number, year = types[pieces[1]], pieces[2], pieces[3]
        iid = instrument_id(kind, number, year)
        return Instrument(iid, kind, number, year, url)
    return Instrument(f"eli:{url.rstrip('/')}", "ELI", url, "", url)


def extract(record: Record) -> Extraction:
    instruments: dict[str, Instrument] = {}
    mentions: list[tuple[str, str]] = []
    spans: dict[str, Span] = {}
    effects: list[Effect] = []
    own = INSTRUMENT.search(record.title)
    own_id = None
    if own:
        own_id = instrument_id(*own.groups())
        instruments[own_id] = Instrument(own_id, *own.groups(), record.eli)
    for match in INSTRUMENT.finditer(record.text):
        kind, number, year = match.groups()
        iid = instrument_id(kind, number, year)
        instruments.setdefault(iid, Instrument(iid, kind, number, year))
        start = max(0, record.text.rfind("\n", 0, match.start()) + 1)
        end_pos = record.text.find("\n", match.end())
        end = len(record.text) if end_pos < 0 else end_pos
        passage = record.text[start:end]
        eid = f"{record.id}:{start}:{end}"
        spans[eid] = Span(eid, start, end, passage)
        mentions.append((iid, eid))
        if own_id and iid != own_id:
            # Only a local, explicit verb produces a legal-effect candidate.
            # Human review is needed to establish direction and legal scope.
            prefix = passage[: match.start() - start]
            verb = list(EFFECT.finditer(prefix))
            if verb:
                predicate = (
                    "REVOKES"
                    if verb[-1].group().lower().startswith("revog")
                    else "AMENDS"
                )
                effects.append(Effect(own_id, predicate, iid, eid, "explicit-text"))
    # Index ordinary source paragraphs too; retrieval cannot depend on a legal
    # citation occurring in every relevant passage.
    for paragraph in re.finditer(r"[^\n]+", record.text):
        if len(paragraph.group().strip()) < 30:
            continue
        start, end = paragraph.span()
        eid = f"{record.id}:{start}:{end}"
        spans.setdefault(eid, Span(eid, start, end, paragraph.group()))
    for predicate, target in record.metadata_relations:
        if predicate not in {"amends", "cites", "corrects"}:
            continue
        # ELI targets may not be in the local corpus. Keep a stable external stub.
        target_instrument = instrument_from_eli(target)
        target_id = target_instrument.id
        if target_id not in instruments or not instruments[target_id].eli:
            instruments[target_id] = target_instrument
        metadata_eid = (
            f"{record.id}:rdfa:{predicate}:{sha256(target.encode()).hexdigest()[:16]}"
        )
        spans[metadata_eid] = Span(
            metadata_eid, -1, -1, f"ELI RDFa {predicate}: {target}", "rdfa"
        )
        if predicate == "cites":
            mentions.append((target_id, metadata_eid))
        elif own_id and own_id != target_id:
            effects.append(
                Effect(
                    own_id, predicate.upper(), target_id, metadata_eid, "eli-metadata"
                )
            )
    provisions = []
    article_matches = list(ARTICLE.finditer(record.text))
    for index, match in enumerate(article_matches):
        end = (
            article_matches[index + 1].start()
            if index + 1 < len(article_matches)
            else len(record.text)
        )
        start = match.start(1)
        body = record.text[start:end].strip()
        if not body:
            continue
        # The occurrence suffix prevents collision in annexes/republications.
        ordinal = (
            sum(
                m.group(2).upper() == match.group(2).upper()
                for m in article_matches[:index]
            )
            + 1
        )
        path = f"article:{match.group(2).upper()}:{ordinal}"
        provisions.append(
            {
                "id": f"{record.id}:{path}",
                "path": path,
                "text": body,
                "start": start,
                "end": end,
                "instrument_id": own_id,
            }
        )
    # Duplicate effects from repeated citations collapse at one evidence location.
    effects = list(
        {
            (e.subject_id, e.predicate, e.object_id, e.evidence_id): e for e in effects
        }.values()
    )
    organization = None
    if record.issuer_name:
        contracting_name = re.search(
            r"(?mi)^Designação da entidade adjudicante:\s*([^\n]+)", record.text
        )
        same_authority = bool(
            contracting_name
            and contracting_name.group(1).strip().casefold()
            == record.issuer_name.strip().casefold()
        )
        nipc_match = (
            re.search(r"(?m)^NIPC:\s*(\d{9})\b", record.text)
            if same_authority
            else None
        )
        nipc = nipc_match.group(1) if nipc_match else None
        normalized = re.sub(r"\W+", "", record.issuer_name.casefold())
        provisional = sha256(normalized.encode()).hexdigest()[:16]
        organization = {
            "id": f"nipc:{nipc}" if nipc else f"org:provisional:{provisional}",
            "name": record.issuer_name,
            "nipc": nipc,
            "identity_status": "verified-key" if nipc else "provisional",
        }
    procedure = None
    place = None
    normalized_type = "".join(
        ch
        for ch in unicodedata.normalize("NFKD", record.publication_type or "")
        if not unicodedata.combining(ch)
    )
    if re.search(r"\banuncio(?: de)? procedimento\b", normalized_type):
        reference = re.search(
            r"(?mi)^Número de referência interna:\s*([^\n]+)", record.text
        )
        scope = organization["id"] if organization else record.id
        key = reference.group(1).strip() if reference else record.id
        procedure = {
            "id": f"proc:provisional:{scope}:{key}",
            "internal_reference": key if reference else None,
            "identity_status": "provisional",
        }
        location_section = re.search(
            r"(?ms)^9\s*-\s*LOCAL DA EXECUÇÃO.*?(?=^10\s*-|\Z)", record.text
        )
        nut = re.search(
            r"(?m)^NUT III:\s*(PT\w+)\b",
            location_section.group() if location_section else "",
        )
        if nut:
            place = {
                "id": f"nuts3:{nut.group(1)}",
                "code": nut.group(1),
                "scheme": "NUTS3",
            }
    activities = set()
    if procedure:
        activities.add("procurement")
    if effects:
        activities.update(
            {
                "amendment"
                if e.predicate == "AMENDS"
                else "revocation"
                if e.predicate == "REVOKES"
                else "correction"
                for e in effects
            }
        )
    if record.publication_type and "regulamento" in record.publication_type:
        activities.add("regulation")
    if re.search(r"\b(nomeia|nomeação)\b", record.title, re.IGNORECASE):
        activities.add("appointment")
    # Only strong title/summary cues get a v1 category. A missing category is
    # preferable to treating every citation in the body as the subject.
    topic = f"{record.title} {record.summary}".casefold()
    rules = (
        ("finance-taxation", r"\b(irs|iva|imposto|tributári|fiscal)"),
        ("economy-business", r"\b(empresa|comércio|investimento)"),
        ("construction-infrastructure", r"\b(construção|empreitada|infraestrutura)"),
        ("housing-urbanism", r"\b(habitação|urbanismo|urbanização)"),
        ("public-administration", r"\b(administração pública|autarquia|município)"),
        ("justice-legal-affairs", r"\b(tribunal|justiça|processo penal)"),
        ("employment-social-protection", r"\b(emprego|segurança social|laboral)"),
        ("health", r"\b(saúde|hospital)"),
        ("education-research", r"\b(educação|ensino|investigação científica)"),
        ("environment-energy", r"\b(ambiente|energia|climátic)"),
        ("transport-mobility", r"\b(transporte|mobilidade|ferroviári)"),
        ("culture-sport-heritage", r"\b(cultura|desporto|património cultural)"),
        ("agriculture-fisheries", r"\b(agricultura|pesca|floresta)"),
        ("security-defence", r"\b(defesa nacional|forças armadas|segurança interna)"),
        ("technology-communications", r"\b(tecnologia|comunicações|digital)"),
    )
    hits = []
    for cid, pattern in rules:
        match = re.search(pattern, topic)
        if match and cid in CATEGORIES:
            hits.append((match.start(), cid))
    hits.sort()
    categories = tuple(
        (cid, "primary" if index == 0 else "secondary")
        for index, (_, cid) in enumerate(hits)
    )
    return Extraction(
        record,
        tuple(instruments.values()),
        tuple(dict.fromkeys(mentions)),
        tuple(provisions),
        tuple(spans.values()),
        tuple(effects),
        organization,
        procedure,
        place,
        categories,
        tuple(sorted(activities)),
    )


def text_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()
