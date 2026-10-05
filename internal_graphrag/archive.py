"""Resumable, browser-rendered historical Diário issue backfill."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from .extract import extract
from .graph import Graph
from .serialization import load_extraction
from .source import official_url, parse_publication
from .storage import Store

ISSUE_TITLE = re.compile(r"Série\s+(I{1,2})\s+de\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
RESULT_COUNT = re.compile(r"\d+\s+de\s+(\d+)\s+resultados?", re.IGNORECASE)
ACT_LINK = "a.diario-s1-diploma-list-title-link"


def eli_issue_url(series: str, year: int, number: int, supplement: int = 0) -> str:
    value = "1" if series == "I" else "2"
    return f"https://data.dre.pt/eli/diario/{value}/{number}/{year}/{supplement}/pt/html"


def parse_issue_date(title: str, series: str) -> date:
    match = ISSUE_TITLE.search(title)
    if not match or match.group(1).upper() != series:
        raise ValueError(f"Unexpected issue title: {title[:160]}")
    return date.fromisoformat(match.group(2))


def issue_act_links(page: Page) -> list[str]:
    page.locator(ACT_LINK).first.wait_for(timeout=30_000)
    status = page.locator('[role="status"]').first.inner_text()
    match = RESULT_COUNT.search(status)
    if not match:
        raise ValueError(f"Could not read issue result count: {status[:100]}")
    expected = int(match.group(1))
    if expected == 0:
        raise ValueError("Issue index returned no acts")
    size = page.locator('select:has(option:text-is("200"))')
    if size.count():
        size.first.select_option(label="200")
        page.wait_for_function(
            "n => document.querySelectorAll('a.diario-s1-diploma-list-title-link').length >= n",
            arg=min(200, expected),
        )
    links: list[str] = []
    while True:
        links.extend(
            urljoin(page.url, href)
            for href in page.locator(ACT_LINK).evaluate_all(
                "els => els.map(a => a.getAttribute('href'))"
            )
        )
        next_button = page.get_by_role("button", name="Ir para a página seguinte")
        if not next_button.count() or next_button.first.is_disabled():
            break
        first_href = page.locator(ACT_LINK).first.get_attribute("href")
        next_button.first.click()
        page.wait_for_function(
            "before => document.querySelector('a.diario-s1-diploma-list-title-link')?.getAttribute('href') !== before",
            arg=first_href,
        )
    links = list(dict.fromkeys(links))
    if len(links) != expected or not all(official_url(url) for url in links):
        raise ValueError(f"Issue advertised {expected} acts but exposed {len(links)} valid links")
    return links


class ArchiveBackfill:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.store = Store(Path(args.data_dir))
        self.graph = Graph(args.neo4j_uri, args.neo4j_user, args.neo4j_password)
        self.client = httpx.Client(timeout=30, follow_redirects=True)
        self.issue_count = 0
        self.publication_count = 0
        self.failures = 0
        self.date_cache: dict[tuple[str, int, int], date | None] = {}

    def close(self) -> None:
        self.client.close()
        self.graph.close()

    def project_pending(self) -> None:
        for path in self.store.pending_projection():
            result = load_extraction(path)
            self.graph.project(result)
            self.store.projected(result.publication.id)

    def resolve(self, series: str, year: int, number: int, supplement: int) -> str | None:
        source = eli_issue_url(series, year, number, supplement)
        response = self.client.get(source)
        response.raise_for_status()
        target = str(response.url)
        if "/dr/detalhe/diario-republica/" not in urlparse(target).path:
            return None
        return target

    def load_issue(
        self, page: Page, series: str, year: int, number: int, supplement: int = 0
    ) -> tuple[date, str] | None:
        url = self.resolve(series, year, number, supplement)
        if not url:
            return None
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_function(
            "() => document.querySelector('h1') || window.location.pathname === '/dr/error'",
            timeout=30_000,
        )
        if urlparse(page.url).path == "/dr/error":
            return None
        heading = page.locator("h1").first
        issue_date = parse_issue_date(heading.inner_text(), series)
        return issue_date, url

    def issue_date(self, page: Page, series: str, year: int, number: int) -> date | None:
        key = (series, year, number)
        if key not in self.date_cache:
            record = self.store.issue_record(series, year, number)
            if record:
                self.date_cache[key] = date.fromisoformat(record[0])
            else:
                info = self.load_issue(page, series, year, number)
                self.date_cache[key] = info[0] if info else None
        return self.date_cache[key]

    def first_issue_on_or_after(self, page: Page, series: str, year: int, start: date) -> int:
        low, high = 1, 367
        while low < high:
            middle = (low + high) // 2
            found = self.issue_date(page, series, year, middle)
            if found is None or found >= start:
                high = middle
            else:
                low = middle + 1
        return low

    def ingest_act(self, page: Page, url: str) -> bool:
        if self.store.source_status(url) == "projected":
            return True
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.locator('div.texto[id*="Texto_Conteudo"]').first.wait_for(timeout=30_000)
            raw = page.content().encode("utf-8")
            result = extract(parse_publication(url, raw))
            changed = self.store.save(result, raw)
            self.graph.project(result)
            self.store.projected(result.publication.id)
            self.publication_count += 1
            print(f"{'stored' if changed else 'unchanged'} {result.publication.id} {url}", flush=True)
            time.sleep(self.args.delay)
            return True
        except (ValueError, httpx.HTTPError, PlaywrightError) as exc:
            self.store.fail(url, str(exc))
            self.failures += 1
            print(f"failed {url}: {exc}", file=sys.stderr, flush=True)
            return False

    def process_issue(
        self, page: Page, series: str, year: int, number: int, supplement: int
    ) -> bool:
        previous = self.store.issue_record(series, year, number, supplement)
        if previous and previous[1] == "complete":
            return True
        info = self.load_issue(page, series, year, number, supplement)
        if not info:
            return False
        issue_date, url = info
        if not self.args.start <= issue_date <= self.args.end:
            return False
        links = issue_act_links(page)
        print(
            f"issue {series} {year}/{number} supplement {supplement}: {issue_date} "
            f"{len(links)} acts {url}", flush=True,
        )
        self.issue_count += 1
        stored = 0
        for act_url in links:
            if self.args.max_publications and self.publication_count >= self.args.max_publications:
                break
            if self.ingest_act(page, act_url):
                stored += 1
        complete = stored == len(links)
        self.store.record_issue(
            series, year, number, supplement, issue_date.isoformat(),
            "complete" if complete else "partial", len(links), stored,
        )
        return complete

    def run(self, page: Page) -> int:
        self.project_pending()
        for series in self.args.series:
            for year in range(self.args.start.year, self.args.end.year + 1):
                first = self.first_issue_on_or_after(page, series, year, self.args.start)
                for number in range(first, 367):
                    if self.args.max_issues and self.issue_count >= self.args.max_issues:
                        return 1 if self.failures else 0
                    if self.args.max_publications and self.publication_count >= self.args.max_publications:
                        return 1 if self.failures else 0
                    current = self.issue_date(page, series, year, number)
                    if current is None or current > self.args.end:
                        break
                    if current < self.args.start:
                        continue
                    self.process_issue(page, series, year, number, 0)
                    for supplement in range(1, self.args.max_supplement + 1):
                        if not self.resolve(series, year, number, supplement):
                            break
                        self.process_issue(page, series, year, number, supplement)
        print(
            f"backfill finished: {self.issue_count} issues, "
            f"{self.publication_count} acts, {self.failures} failures", flush=True,
        )
        return 1 if self.failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    parser.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
    parser.add_argument("--series", action="append", choices=["I", "II"])
    parser.add_argument("--data-dir", default=os.getenv("GRAPHRAG_DATA_DIR", "internal_graphrag/data"))
    parser.add_argument("--neo4j-uri", default=os.getenv("NEO4J_URI", "bolt://localhost:17687"))
    parser.add_argument("--neo4j-user", default=os.getenv("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.getenv("NEO4J_PASSWORD", ""))
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--max-issues", type=int, default=0)
    parser.add_argument("--max-publications", type=int, default=0)
    parser.add_argument("--max-supplement", type=int, default=3)
    args = parser.parse_args()
    if args.start > args.end or args.end > datetime.now(ZoneInfo("Europe/Lisbon")).date():
        parser.error("--from must precede --to, and --to cannot be in the future")
    args.series = args.series or ["I", "II"]
    lock = None
    if os.getenv("GRAPHRAG_ARCHIVE_LOCK_HELD") != "1":
        data_dir = Path(args.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        lock = (data_dir / ".archive.lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            print("Another archive backfill is already running.", file=sys.stderr)
            return 2
    crawler = ArchiveBackfill(args)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(locale="pt-PT")
            page = context.new_page()
            page.set_default_timeout(30_000)
            result = crawler.run(page)
            browser.close()
            return result
    finally:
        crawler.close()
        if lock:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
