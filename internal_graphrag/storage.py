"""Local immutable snapshots and retry state, independent of the Neo4j projection."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .extract import Extraction


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "manifest.sqlite3")
        self.db.execute("""CREATE TABLE IF NOT EXISTS sources (
            url TEXT PRIMARY KEY, publication_id TEXT, content_hash TEXT,
            fetched_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            content_hash TEXT PRIMARY KEY, publication_id TEXT NOT NULL,
            raw_path TEXT NOT NULL, extracted_path TEXT NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS archive_issues (
            series TEXT NOT NULL, year INTEGER NOT NULL, issue_number INTEGER NOT NULL,
            supplement INTEGER NOT NULL, issue_date TEXT NOT NULL, status TEXT NOT NULL, expected_count INTEGER,
            stored_count INTEGER, error TEXT, updated_at TEXT NOT NULL,
            PRIMARY KEY (series, year, issue_number, supplement)
        )""")
        self.db.commit()

    def source_status(self, url: str) -> str | None:
        row = self.db.execute(
            "SELECT status FROM sources WHERE url=?", (url,)
        ).fetchone()
        return row[0] if row else None

    def issue_record(
        self, series: str, year: int, number: int, supplement: int = 0
    ) -> tuple[str, str] | None:
        row = self.db.execute(
            """SELECT issue_date, status FROM archive_issues
            WHERE series=? AND year=? AND issue_number=? AND supplement=?""",
            (series, year, number, supplement),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def record_issue(
        self, series: str, year: int, number: int, supplement: int, issue_date: str,
        status: str, expected_count: int, stored_count: int, error: str = "",
    ) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO archive_issues VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(series,year,issue_number,supplement) DO UPDATE SET
                issue_date=excluded.issue_date, status=excluded.status,
                expected_count=excluded.expected_count,
                stored_count=excluded.stored_count, error=excluded.error,
                updated_at=excluded.updated_at""",
                (
                    series, year, number, supplement, issue_date, status, expected_count,
                    stored_count, error[:500], datetime.now(timezone.utc).isoformat(),
                ),
            )

    def save(self, extraction: Extraction, raw: bytes) -> bool:
        digest = sha256(raw).hexdigest()
        if (
            extraction.publication.raw_hash
            and extraction.publication.raw_hash != digest
        ):
            raise ValueError("Extracted record does not match raw source bytes")
        row = self.db.execute(
            "SELECT content_hash FROM sources WHERE url=?",
            (extraction.publication.url,),
        ).fetchone()
        if row and row[0] == digest:
            return False
        raw_path = (
            self.root / "raw" / f"{digest}.{extraction.publication.source_format}"
        )
        data_path = self.root / "extracted" / f"{digest}.json"
        raw_path.parent.mkdir(exist_ok=True)
        data_path.parent.mkdir(exist_ok=True)
        if not raw_path.exists():
            raw_path.write_bytes(raw)
        data_path.write_text(
            json.dumps(extraction.as_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?)",
                (digest, extraction.publication.id, str(raw_path), str(data_path)),
            )
            self.db.execute(
                """INSERT INTO sources VALUES (?,?,?,?,?,NULL)
                ON CONFLICT(url) DO UPDATE SET publication_id=excluded.publication_id,
                content_hash=excluded.content_hash, fetched_at=excluded.fetched_at,
                status=excluded.status, error=NULL""",
                (
                    extraction.publication.url,
                    extraction.publication.id,
                    digest,
                    datetime.now(timezone.utc).isoformat(),
                    "stored",
                ),
            )
        return True

    def fail(self, url: str, error: str) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO sources VALUES (?,NULL,NULL,?,?,?)
                ON CONFLICT(url) DO UPDATE SET fetched_at=excluded.fetched_at,
                status=excluded.status, error=excluded.error""",
                (url, datetime.now(timezone.utc).isoformat(), "failed", error[:500]),
            )

    def pending_projection(self) -> list[Path]:
        return [
            Path(row[0])
            for row in self.db.execute(
                """SELECT s.extracted_path FROM snapshots s JOIN sources u
               ON u.content_hash=s.content_hash WHERE u.status='stored'"""
            )
        ]

    def projected(self, publication_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE sources SET status='projected' WHERE publication_id=?",
                (publication_id,),
            )
