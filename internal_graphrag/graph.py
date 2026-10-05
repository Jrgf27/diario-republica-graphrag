"""Neo4j projection of locally preserved source snapshots."""

from __future__ import annotations

from datetime import datetime, timezone

from neo4j import GraphDatabase

from .extract import Extraction, text_hash
from .ontology import ACTIVITIES, CATEGORIES

CONSTRAINTS = (
    "CREATE CONSTRAINT publication_id IF NOT EXISTS FOR (n:Publication) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT issue_id IF NOT EXISTS FOR (n:Issue) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT instrument_id IF NOT EXISTS FOR (n:LegalInstrument) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT provision_id IF NOT EXISTS FOR (n:Provision) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT evidence_id IF NOT EXISTS FOR (n:EvidenceSpan) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT organization_id IF NOT EXISTS FOR (n:Organization) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT procedure_id IF NOT EXISTS FOR (n:ProcurementProcedure) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT place_id IF NOT EXISTS FOR (n:Place) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT category_id IF NOT EXISTS FOR (n:Category) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT activity_id IF NOT EXISTS FOR (n:ActivityType) REQUIRE n.id IS UNIQUE",
    "CREATE FULLTEXT INDEX evidence_text IF NOT EXISTS FOR (n:EvidenceSpan) ON EACH [n.text]",
)


class Graph:
    def __init__(self, uri: str, user: str, password: str) -> None:
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self._prepared = False

    @classmethod
    def from_driver(cls, driver) -> Graph:
        """Reuse a caller-owned connection for read operations."""
        graph = cls.__new__(cls)
        graph.driver = driver
        graph._prepared = False
        return graph

    def close(self) -> None:
        self.driver.close()

    def project(self, extraction: Extraction) -> None:
        with self.driver.session() as session:
            if not self._prepared:
                for statement in CONSTRAINTS:
                    session.run(statement).consume()
                session.execute_write(self._seed_taxonomy)
                self._prepared = True
            session.execute_write(self._write, extraction)

    @staticmethod
    def _seed_taxonomy(tx) -> None:
        tx.run(
            """UNWIND $rows AS row
            MERGE (c:Category {id:row.id}) SET c.name=row.name""",
            rows=[{"id": key, "name": value} for key, value in CATEGORIES.items()],
        ).consume()
        tx.run(
            """UNWIND $rows AS row
            MERGE (a:ActivityType {id:row.id}) SET a.name=row.name""",
            rows=[{"id": key, "name": value} for key, value in ACTIVITIES.items()],
        ).consume()

    @staticmethod
    def _write(tx, e: Extraction) -> None:
        p = e.publication
        # A changed source is a new snapshot. Replace its derived projection;
        # the raw prior snapshot remains on disk for audit and replay.
        tx.run(
            """MATCH (a:LegalInstrument)-[r:AMENDS|REVOKES|CORRECTS]->()
            WHERE r.publication_id=$pid DELETE r""",
            pid=p.id,
        ).consume()
        tx.run(
            """MATCH (p:Publication {id:$pid})-[r:PUBLISHED_IN|ISSUED_BY|CLASSIFIED_AS|HAS_ACTIVITY|IS_PUBLICATION_OF|ANNOUNCES|MENTIONS]->()
            DELETE r""",
            pid=p.id,
        ).consume()
        tx.run(
            """MATCH (p:Publication {id:$pid})-[:HAS_EVIDENCE|CONTAINS_PROVISION]->(n)
            DETACH DELETE n""",
            pid=p.id,
        ).consume()
        tx.run(
            """MERGE (p:Publication {id:$id})
            SET p.url=$url, p.title=$title, p.summary=$summary, p.full_text=$full_text,
                p.publication_date=$publication_date, p.publication_type=$publication_type,
                p.source_format=$source_format, p.source_hash=$source_hash""",
            id=p.id,
            url=p.url,
            title=p.title,
            summary=p.summary,
            full_text=p.text,
            publication_date=p.publication_date,
            publication_type=p.publication_type,
            source_format=p.source_format,
            source_hash=p.raw_hash or text_hash(p.text),
        ).consume()
        if p.issue_id:
            tx.run(
                """MATCH (p:Publication {id:$pid})
                MERGE (i:Issue {id:$id})
                SET i.series=$series, i.number=$number, i.date=$date, i.supplement=$supplement
                MERGE (p)-[:PUBLISHED_IN]->(i)""",
                pid=p.id,
                id=p.issue_id,
                series=p.issue_series,
                number=p.issue_number,
                date=p.publication_date,
                supplement=p.issue_supplement,
            ).consume()
        if e.organization:
            tx.run(
                """MATCH (p:Publication {id:$pid})
                MERGE (o:Organization {id:$id})
                SET o.name=$name, o.nipc=$nipc, o.identity_status=$identity_status
                MERGE (p)-[r:ISSUED_BY]->(o)
                SET r.source_field='Emitente'""",
                pid=p.id,
                **e.organization,
            ).consume()
        for cid, role in e.categories:
            tx.run(
                """MATCH (p:Publication {id:$pid})
                MERGE (c:Category {id:$id}) SET c.name=$name
                MERGE (p)-[r:CLASSIFIED_AS]->(c)
                SET r.role=$role, r.method='title-summary-rule-v1'""",
                pid=p.id,
                id=cid,
                name=CATEGORIES[cid],
                role=role,
            ).consume()
        for activity in e.activities:
            tx.run(
                """MATCH (p:Publication {id:$pid})
                MERGE (a:ActivityType {id:$id}) SET a.name=$name
                MERGE (p)-[:HAS_ACTIVITY]->(a)""",
                pid=p.id,
                id=activity,
                name=ACTIVITIES[activity],
            ).consume()
        if e.procedure:
            tx.run(
                """MATCH (p:Publication {id:$pid})
                MERGE (q:ProcurementProcedure {id:$id})
                SET q.internal_reference=$internal_reference,
                    q.identity_status=$identity_status
                MERGE (p)-[:ANNOUNCES]->(q)""",
                pid=p.id,
                **e.procedure,
            ).consume()
            if e.organization:
                tx.run(
                    """MATCH (q:ProcurementProcedure {id:$qid}),
                    (o:Organization {id:$oid}) MERGE (q)-[:RUN_BY]->(o)""",
                    qid=e.procedure["id"],
                    oid=e.organization["id"],
                ).consume()
        if e.procedure and e.place:
            tx.run(
                """MATCH (q:ProcurementProcedure {id:$qid})
                MERGE (l:Place {id:$id}) SET l.code=$code, l.scheme=$scheme
                MERGE (q)-[:LOCATED_IN]->(l)""",
                qid=e.procedure["id"],
                **e.place,
            ).consume()
        for instrument in e.instruments:
            tx.run(
                """MERGE (i:LegalInstrument {id:$id})
                SET i.kind=$kind, i.number=$number, i.year=$year
                FOREACH (_ IN CASE WHEN $eli IS NULL THEN [] ELSE [1] END | SET i.eli=$eli)""",
                **instrument.__dict__,
            ).consume()
        own = next((i.id for i in e.instruments if i.eli == p.eli and p.eli), None)
        if not own:
            from .extract import INSTRUMENT, instrument_id

            match = INSTRUMENT.search(p.title)
            own = instrument_id(*match.groups()) if match else None
        if own:
            tx.run(
                """MATCH (p:Publication {id:$pid}), (i:LegalInstrument {id:$iid})
                MERGE (p)-[:IS_PUBLICATION_OF]->(i)""",
                pid=p.id,
                iid=own,
            ).consume()
        for span in e.spans:
            tx.run(
                """MATCH (p:Publication {id:$pid})
                MERGE (s:EvidenceSpan {id:$id})
                SET s.start=$start, s.end=$end, s.text=$text, s.source_kind=$source_kind
                MERGE (p)-[:HAS_EVIDENCE]->(s)""",
                pid=p.id,
                **span.__dict__,
            ).consume()
        for provision in e.provisions:
            tx.run(
                """MATCH (p:Publication {id:$pid})
                MERGE (v:Provision {id:$id})
                SET v.path=$path, v.text=$text, v.start=$start, v.end=$end
                MERGE (p)-[:CONTAINS_PROVISION]->(v)
                WITH v
                OPTIONAL MATCH (i:LegalInstrument {id:$instrument_id})
                FOREACH (_ IN CASE WHEN i IS NULL THEN [] ELSE [1] END |
                    MERGE (v)-[:PART_OF]->(i))""",
                pid=p.id,
                **provision,
            ).consume()
        for iid, eid in e.mentions:
            tx.run(
                """MATCH (p:Publication {id:$pid}), (i:LegalInstrument {id:$iid})
                MERGE (p)-[r:MENTIONS {evidence_id:$eid}]->(i)""",
                pid=p.id,
                iid=iid,
                eid=eid,
            ).consume()
        for effect in e.effects:
            if effect.predicate not in {"AMENDS", "REVOKES", "CORRECTS"}:
                continue
            # Predicate is allowlisted because Cypher relationship names cannot be parameters.
            tx.run(
                f"""MATCH (a:LegalInstrument {{id:$subject_id}}),
                    (b:LegalInstrument {{id:$object_id}})
                MERGE (a)-[r:{effect.predicate} {{evidence_id:$evidence_id,
                    publication_id:$publication_id}}]->(b)
                SET r.method=$method, r.review_status=$review_status""",
                subject_id=effect.subject_id,
                object_id=effect.object_id,
                evidence_id=effect.evidence_id,
                publication_id=p.id,
                method=effect.method,
                review_status=effect.review_status,
            ).consume()

    def trace(self, instrument_id: str) -> list[dict]:
        with self.driver.session() as session:
            result = session.run(
                """MATCH (a:LegalInstrument {id:$id})-[r]->(b:LegalInstrument)
                WHERE type(r) IN ['AMENDS', 'REVOKES', 'CORRECTS']
                OPTIONAL MATCH (p:Publication {id:r.publication_id})
                OPTIONAL MATCH (s:EvidenceSpan {id:r.evidence_id})
                RETURN type(r) AS effect, a.id AS source, b.id AS target,
                    r.evidence_id AS evidence_id, r.review_status AS review_status,
                    r.method AS method,
                    p.url AS publication_url, s.text AS passage
                ORDER BY effect, target""",
                id=instrument_id,
            )
            return [dict(row) for row in result]

    def search(self, question: str, limit: int = 10) -> list[dict]:
        import re

        terms = re.findall(r"[^\W_]+", question, re.UNICODE)
        if not terms:
            return []
        # Lucene syntax is assembled from plain word tokens only.
        query = " OR ".join(terms[:12])
        with self.driver.session() as session:
            result = session.run(
                """CALL db.index.fulltext.queryNodes('evidence_text', $terms)
                YIELD node AS span, score
                MATCH (p:Publication)-[:HAS_EVIDENCE]->(span)
                OPTIONAL MATCH (p)-[:IS_PUBLICATION_OF]->(i:LegalInstrument)
                RETURN p.id AS publication_id, p.title AS title, p.url AS url,
                    p.publication_date AS publication_date, span.text AS passage,
                    span.start AS start, span.end AS end, i.id AS instrument_id,
                    score ORDER BY score DESC LIMIT $limit""",
                terms=query,
                limit=limit,
            )
            return [dict(row) for row in result]

    def review(
        self,
        subject_id: str,
        predicate: str,
        object_id: str,
        evidence_id: str,
        decision: str,
        reviewer: str,
    ) -> bool:
        if predicate not in {"AMENDS", "REVOKES", "CORRECTS"}:
            raise ValueError("Unknown legal effect")
        if decision not in {"confirmed", "rejected"}:
            raise ValueError("Decision must be confirmed or rejected")
        with self.driver.session() as session:
            row = session.run(
                """MATCH (a:LegalInstrument {id:$subject})-[r]->
                (b:LegalInstrument {id:$target})
                WHERE type(r)=$predicate AND r.evidence_id=$evidence
                SET r.review_status=$decision, r.reviewer=$reviewer,
                    r.reviewed_at=$reviewed_at
                RETURN count(r) AS updated""",
                subject=subject_id,
                target=object_id,
                predicate=predicate,
                evidence=evidence_id,
                decision=decision,
                reviewer=reviewer,
                reviewed_at=datetime.now(timezone.utc).isoformat(),
            ).single()
            return bool(row and row["updated"] == 1)
