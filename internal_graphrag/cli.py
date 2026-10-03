"""Run bounded RSS/direct-URL ingestion and inspect amendment evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from xml.etree.ElementTree import ParseError

import httpx
from pypdf.errors import DependencyError, PdfReadError

from .extract import extract
from .graph import Graph
from .source import (
    FEEDS,
    feed_items,
    fetch,
    official_url,
    parse_pdf_publication,
    parse_publication,
    required_issue_links,
)
from .storage import Store


def ingest(args: argparse.Namespace) -> int:
    store = Store(Path(args.data_dir))
    items = {url: None for url in args.url or []}
    failures = 0
    with httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={
            "User-Agent": "DiarioRepublicaPersonalGraphRAG/0.1"
        },
    ) as client:
        if args.feed:
            for series in args.feed:
                try:
                    feed = feed_items(fetch(client, FEEDS[series]))
                    for item in feed[: args.max_feed_items]:
                        if "/dr/detalhe/diario-republica/" in item.url:
                            for child in required_issue_links(
                                fetch(client, item.url), item.url
                            ):
                                items.setdefault(child, None)
                        else:
                            items.setdefault(item.url, item)
                except (httpx.HTTPError, ValueError, ParseError) as exc:
                    print(f"Feed {series} failed: {exc}", file=sys.stderr)
                    failures += 1
        urls = list(items)[: args.limit]
        for url in urls:
            try:
                if not official_url(url):
                    raise ValueError("URL is not an official DR HTTPS URL")
                raw = fetch(client, url)
                if "/dr/detalhe/diario-republica/" in url:
                    # Direct issue URLs are expanded in the same bounded run.
                    for child in required_issue_links(raw, url):
                        if child not in items and len(urls) < args.limit:
                            urls.append(child)
                            items[child] = None
                    continue
                record = (
                    parse_pdf_publication(url, raw, items[url])
                    if url.lower().endswith(".pdf")
                    else parse_publication(url, raw)
                )
                result = extract(record)
                changed = store.save(result, raw)
                print(
                    f"{'stored' if changed else 'unchanged'} {result.publication.id} {url}"
                )
            except (httpx.HTTPError, ValueError, PdfReadError, DependencyError) as exc:
                store.fail(url, str(exc))
                print(f"failed {url}: {exc}", file=sys.stderr)
                failures += 1
            time.sleep(args.delay)
    if not args.no_graph:
        project(store, args)
    return 1 if failures else 0


def project(store: Store, args: argparse.Namespace) -> None:
    from .serialization import load_extraction

    graph = Graph(args.neo4j_uri, args.neo4j_user, args.neo4j_password)
    try:
        for path in store.pending_projection():
            item = load_extraction(path)
            graph.project(item)
            store.projected(item.publication.id)
            print(f"projected {item.publication.id}")
    finally:
        graph.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", default=os.getenv("GRAPHRAG_DATA_DIR", "internal_graphrag/data")
    )
    parser.add_argument(
        "--neo4j-uri", default=os.getenv("NEO4J_URI", "bolt://localhost:17687")
    )
    parser.add_argument("--neo4j-user", default=os.getenv("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.getenv("NEO4J_PASSWORD", ""))
    sub = parser.add_subparsers(dest="command", required=True)
    ingest_parser = sub.add_parser("ingest")
    ingest_parser.add_argument("--feed", action="append", choices=list(FEEDS))
    ingest_parser.add_argument("--url", action="append")
    ingest_parser.add_argument("--max-feed-items", type=int, default=500)
    ingest_parser.add_argument("--limit", type=int, default=500)
    ingest_parser.add_argument("--delay", type=float, default=0.5)
    ingest_parser.add_argument("--no-graph", action="store_true")
    sub.add_parser("project")
    trace_parser = sub.add_parser("trace")
    trace_parser.add_argument("instrument_id")
    search_parser = sub.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=10)
    review_parser = sub.add_parser("review")
    review_parser.add_argument("subject_id")
    review_parser.add_argument("predicate", choices=["AMENDS", "REVOKES", "CORRECTS"])
    review_parser.add_argument("object_id")
    review_parser.add_argument("evidence_id")
    review_parser.add_argument("decision", choices=["confirmed", "rejected"])
    review_parser.add_argument("--reviewer", required=True)
    args = parser.parse_args()
    if args.command == "ingest":
        if not args.feed and not args.url:
            parser.error("ingest requires --feed or --url")
        return ingest(args)
    if args.command == "project":
        project(Store(Path(args.data_dir)), args)
        return 0
    graph = Graph(args.neo4j_uri, args.neo4j_user, args.neo4j_password)
    try:
        if args.command == "review":
            updated = graph.review(
                args.subject_id,
                args.predicate,
                args.object_id,
                args.evidence_id,
                args.decision,
                args.reviewer,
            )
            print("reviewed" if updated else "no matching effect")
            return 0 if updated else 1
        rows = (
            graph.trace(args.instrument_id)
            if args.command == "trace"
            else graph.search(args.query, args.limit)
        )
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    finally:
        graph.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
