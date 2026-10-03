"""Fetch official DR pages and extract conservative, replayable source records."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx

FEEDS = {
    "I": "https://files.diariodarepublica.pt/rss/serie1.xml",
    "II": "https://files.diariodarepublica.pt/rss/serie2.xml",
    "II-L": "https://files.diariodarepublica.pt/rss/serie2&parte=l.xml",
}
ALLOWED_HOSTS = {
    "diariodarepublica.pt",
    "www.diariodarepublica.pt",
    "data.diariodarepublica.pt",
    "data.dre.pt",
    "files.diariodarepublica.pt",
}
ISSUE_RE = re.compile(
    r"Diário da República\s+n\.º\s*(\d+)\s*/\s*(\d{4}),?\s*(Suplemento(?:\s*\d+)?\s*,?\s*)?Série\s*(I{1,2})\s+de\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
DETAIL_RE = re.compile(r"/dr/detalhe/(?!diario-republica/)[^?#]+/\d+-\d{4}-\d+$")


def official_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in ALLOWED_HOSTS
        and not parsed.username
    )


def normalize_eli(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in ALLOWED_HOSTS:
        return None
    if "/eli/" not in parsed.path:
        return None
    return parsed._replace(netloc=parsed.hostname, fragment="").geturl()


def fetch(client: httpx.Client, url: str) -> bytes:
    if not official_url(url):
        raise ValueError(f"Not an official HTTPS DR URL: {url}")
    response = client.get(url)
    response.raise_for_status()
    if not official_url(str(response.url)):
        raise ValueError(f"Redirect left the official DR hosts: {response.url}")
    if len(response.content) > 10_000_000:
        raise ValueError("Source exceeds the 10 MB HTML/XML limit")
    return response.content


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.links: list[str] = []
        self.elis: list[str] = []
        self.relations: list[tuple[str, str]] = []
        self.parts: list[str] = []
        self.page_parts: list[str] = []
        self._stack: list[str] = []
        self._text_depth = 0
        self._source_depth = 0
        self._in_title = False
        self._in_script = False
        self.json_ld: list[str] = []
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self._stack.append(tag)
        if tag == "title":
            self._in_title = True
        if tag == "script" and a.get("type") == "application/ld+json":
            self._in_script = True
            self._script_parts = []
        if tag == "meta" and a.get("name", "").lower() == "description":
            self.description = a.get("content") or ""
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        for key in ("about", "resource", "href"):
            value = a.get(key)
            normalized = normalize_eli(value) if value else None
            if normalized:
                if key == "about" or not a.get("property"):
                    self.elis.append(normalized)
                if key in {"resource", "href"} and a.get("property"):
                    self.relations.append((a["property"].split(":")[-1], normalized))
        if tag in ("main", "article"):
            self._text_depth += 1
        if (
            tag == "div"
            and "texto" in (a.get("class") or "").split()
            and "Texto_Conteudo" in (a.get("id") or "")
        ):
            self._source_depth = len(self._stack)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._in_script:
            self.json_ld.append("".join(self._script_parts))
            self._in_script = False
        if tag in ("main", "article") and self._text_depth:
            self._text_depth -= 1
        if (
            self._source_depth
            and len(self._stack) == self._source_depth
            and tag == "div"
        ):
            self._source_depth = 0
        if tag in self._stack:
            self._stack = self._stack[
                : len(self._stack) - 1 - self._stack[::-1].index(tag)
            ]

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_parts.append(data)
        elif self._in_title:
            self.title += data
        elif "body" in self._stack and not any(
            x in self._stack for x in ("script", "style", "nav", "footer")
        ):
            clean = " ".join(data.split())
            if clean:
                self.page_parts.append(clean)
                if self._source_depth or self._text_depth:
                    self.parts.append(
                        "\n".join(
                            " ".join(line.split())
                            for line in data.splitlines()
                            if line.strip()
                        )
                    )


@dataclass(frozen=True)
class Record:
    id: str
    url: str
    title: str
    summary: str
    text: str
    publication_date: str | None
    issue_id: str | None
    issue_series: str | None
    issue_number: int | None
    issue_supplement: str | None
    publication_type: str | None
    eli: str | None
    metadata_relations: tuple[tuple[str, str], ...]
    issuer_name: str | None = None
    source_format: str = "html"
    raw_hash: str | None = None


@dataclass(frozen=True)
class FeedItem:
    url: str
    title: str
    summary: str


def parse_publication(url: str, raw: bytes) -> Record:
    if not official_url(url):
        raise ValueError("Publication URL is not an official DR URL")
    page = Page()
    page.feed(raw.decode("utf-8", errors="replace"))
    title = " ".join(page.title.split()).split(" | DR")[0]
    text = "\n".join(page.parts)
    if len(text) < 100:
        raise ValueError("No substantial publication text found; inspect source layout")
    match = ISSUE_RE.search("\n".join(page.page_parts))
    series = match.group(4).upper() if match else None
    number = int(match.group(1)) if match else None
    publication_date = match.group(5) if match else None
    supplement_match = (
        re.search(
            r"\b(?:suplemento|supl\.)\s*(\d+)?", match.group(3) or "", re.IGNORECASE
        )
        if match
        else None
    )
    supplement = (supplement_match.group(1) or "1") if supplement_match else None
    issue_id = (
        f"{series}:{publication_date}:{number}:{supplement or '0'}" if match else None
    )
    path = urlparse(url).path
    official_id = (
        f"eli-page:{sha256(url.encode()).hexdigest()[:20]}"
        if path.startswith("/eli/")
        else path.rstrip("/").split("/")[-1]
    )
    ptype = (
        path.split("/")[3].replace("-", " ")
        if path.startswith("/dr/detalhe/") and len(path.split("/")) > 3
        else None
    )
    eli = next(
        (e for e in page.elis if "/eli/" in e and "/dr/" not in urlparse(e).path), None
    )
    if not eli:
        eli = next(iter(page.elis), None)
    for blob in page.json_ld:
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        objects = data if isinstance(data, list) else [data]
        for obj in objects:
            if isinstance(obj, dict):
                publication_date = publication_date or obj.get("datePublished")
                eli = eli or (
                    obj.get("identifier")
                    if isinstance(obj.get("identifier"), str)
                    and "/eli/" in obj["identifier"]
                    else None
                )
    issuer_name = None
    for index, part in enumerate(page.page_parts[:-1]):
        if part.strip().lower() == "emitente:":
            issuer_name = page.page_parts[index + 1]
            break
    return Record(
        official_id,
        url,
        title,
        page.description,
        text,
        publication_date,
        issue_id,
        series,
        number,
        supplement,
        ptype,
        eli,
        tuple(sorted(set(page.relations))),
        issuer_name,
        "html",
        sha256(raw).hexdigest(),
    )


def parse_pdf_publication(url: str, raw: bytes, item: FeedItem | None = None) -> Record:
    from pypdf import PdfReader

    if not official_url(url) or not urlparse(url).path.lower().endswith(".pdf"):
        raise ValueError("Not an official DR PDF URL")
    reader = PdfReader(BytesIO(raw))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("Encrypted PDF cannot be ingested")
    pages = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
    if not pages or sum(map(len, pages)) < 100:
        raise ValueError("PDF has no extractable text; OCR review needed")
    title = (
        re.split(r"\s+-\s+Diário da República", item.title, maxsplit=1)[0].strip(" -")
        if item
        else str((reader.metadata or {}).get("/Title") or "")
    )
    if not title:
        match = INSTRUMENT_HEADING.search(pages[0][:1000])
        title = match.group() if match else urlparse(url).path.rsplit("/", 1)[-1]
    is_notice = bool(re.search(r"anúncio\s+de\s+procedimento", title, re.IGNORECASE))
    paragraphs = []
    for page in pages:
        cleaned = page.replace("\u00a0", " ")
        cleaned = re.sub(r"(?<=\w)-\n(?=\w)", "", cleaned)
        if is_notice:
            paragraphs.extend(
                " ".join(line.split()) for line in cleaned.splitlines() if line.strip()
            )
            continue
        for block in re.split(r"\n\s*\n", cleaned):
            lines = [
                " ".join(line.split()) for line in block.splitlines() if line.strip()
            ]
            if lines:
                paragraphs.append(" ".join(lines))
    text = "\n".join(paragraphs)
    issue = ISSUE_RE.search(item.title) if item else None
    series = issue.group(4).upper() if issue else None
    number = int(issue.group(1)) if issue else None
    publication_date = issue.group(5) if issue else None
    supplement = "1" if issue and issue.group(3) else None
    issue_id = (
        f"{series}:{publication_date}:{number}:{supplement or '0'}" if issue else None
    )
    ptype = title.split(" n.º", 1)[0].lower() if " n.º" in title else None
    issuer = next(
        (
            line
            for raw_line in pages[0].splitlines()
            if (line := " ".join(raw_line.split())).isupper()
            and len(line) < 120
            and not line.startswith(("PARTE ", "N.º", "DIÁRIO "))
        ),
        None,
    )
    return Record(
        f"pdf:{sha256(url.encode()).hexdigest()[:24]}",
        url,
        title,
        item.summary if item else "",
        text,
        publication_date,
        issue_id,
        series,
        number,
        supplement,
        ptype,
        None,
        (),
        issuer,
        "pdf",
        sha256(raw).hexdigest(),
    )


INSTRUMENT_HEADING = re.compile(
    r"(?:Lei|Decreto-Lei|Decreto Regulamentar|Portaria)\s+n\.º\s*\d+[A-Z]?(?:-\d+)?/\d{4}",
    re.IGNORECASE,
)


def feed_items(raw: bytes) -> list[FeedItem]:
    root = ElementTree.fromstring(raw)
    items: list[FeedItem] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "item":
            continue
        fields = {
            child.tag.rsplit("}", 1)[-1]: (child.text or "").strip()
            for child in element
        }
        url = fields.get("link", "")
        if official_url(url):
            items.append(
                FeedItem(url, fields.get("title", ""), fields.get("description", ""))
            )
    return items


def feed_links(raw: bytes) -> list[str]:
    return list(dict.fromkeys(item.url for item in feed_items(raw)))


def issue_links(
    raw: bytes, base_url: str = "https://diariodarepublica.pt/"
) -> list[str]:
    page = Page()
    page.feed(raw.decode("utf-8", errors="replace"))
    links = (urljoin(base_url, link) for link in page.links)
    return list(
        dict.fromkeys(
            link
            for link in links
            if official_url(link) and DETAIL_RE.search(urlparse(link).path)
        )
    )


def required_issue_links(raw: bytes, base_url: str) -> list[str]:
    links = issue_links(raw, base_url)
    if not links:
        raise ValueError(
            "Issue page exposed no act links; use individual official PDF URLs"
        )
    return links
