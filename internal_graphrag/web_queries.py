"""Read-only queries for the local search and graph workspace."""

from __future__ import annotations

import re

from neo4j import READ_ACCESS, GraphDatabase

from .web_search import parse_search


def _matching_terms(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE)]


def _node(node) -> dict:
    node_id = node.get("id")
    label = next(iter(node.labels), "Node")
    title = node.get("title") or node.get("name")
    if not title and label == "LegalInstrument" and node.get("number"):
        title = f'{node.get("kind") or "Instrument"} {node["number"]}/{node.get("year") or "?"}'
    if not title and label == "Issue":
        title = f'Issue {node.get("series") or ""} {node.get("number") or ""} · {node.get("date") or ""}'.strip()
    return {
        "id": node_id,
        "label": label,
        "title": title or node_id,
        "date": node.get("publication_date"),
        "url": node.get("url") or node.get("eli"),
        "snippet": str(node.get("summary") or node.get("text") or "")[:280],
    }


def _collect(rows) -> tuple[list[dict], list[dict]]:
    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        seed, neighbor, relation = row["seed"], row["neighbor"], row["relation"]
        if seed.get("id"):
            nodes[seed["id"]] = _node(seed)
        if neighbor is None or relation is None or not neighbor.get("id"):
            continue
        nodes[neighbor["id"]] = _node(neighbor)
        source = relation.start_node.get("id")
        target = relation.end_node.get("id")
        if source and target:
            key = (source, target, relation.type)
            edges[key] = {
                "source": source,
                "target": target,
                "type": relation.type,
                "review_status": relation.get("review_status"),
            }
    return list(nodes.values()), list(edges.values())


def search_neighborhood(driver, publication_ids: list[str]) -> tuple[list[dict], list[dict]]:
    """Show matching publications with a small amount of useful context each."""
    ids = list(dict.fromkeys(publication_ids))[:12]
    if not ids:
        return [], []
    with driver.session(default_access_mode=READ_ACCESS) as session:
        rows = session.run(
            """MATCH (seed:Publication) WHERE seed.id IN $ids
            OPTIONAL MATCH (seed)-[relation]-(neighbor)
            WHERE NOT neighbor:EvidenceSpan AND NOT neighbor:Provision
            WITH seed, relation, neighbor
            ORDER BY seed.id,
              CASE type(relation)
                WHEN 'IS_PUBLICATION_OF' THEN 0
                WHEN 'ANNOUNCES' THEN 1
                WHEN 'ISSUED_BY' THEN 2
                WHEN 'MENTIONS' THEN 3
                WHEN 'CLASSIFIED_AS' THEN 4
                ELSE 5 END,
              neighbor.id
            WITH seed, collect({relation: relation, neighbor: neighbor})[..3] AS links
            UNWIND links AS link
            RETURN seed, link.relation AS relation, link.neighbor AS neighbor""",
            ids=ids,
        )
        return _collect(rows)


def neighborhood(driver, node_id: str) -> tuple[list[dict], list[dict]]:
    """Show any selected node and its immediate relationships."""
    with driver.session(default_access_mode=READ_ACCESS) as session:
        rows = session.run(
            """MATCH (seed {id:$id})
            OPTIONAL MATCH (seed)-[relation]-(neighbor)
            WHERE NOT neighbor:EvidenceSpan AND NOT neighbor:Provision
            RETURN seed, relation, neighbor
            ORDER BY type(relation), neighbor.id LIMIT 100""",
            id=node_id,
        )
        return _collect(rows)


def node_detail(driver, node_id: str) -> dict | None:
    with driver.session(default_access_mode=READ_ACCESS) as session:
        row = session.run(
            """MATCH (node {id:$id})
            OPTIONAL MATCH (publication:Publication)-[:HAS_EVIDENCE|CONTAINS_PROVISION]->(node)
            RETURN labels(node) AS labels, properties(node) AS properties,
                   publication.url AS parent_url LIMIT 1""",
            id=node_id,
        ).single()
        return dict(row) if row else None


def search(driver, question: str, limit: int = 12, year: str | None = None) -> dict:
    """Find and group source passages; keep the publication-year filter visible."""
    terms, mentioned_year = parse_search(question)
    publication_year = (year or None) if year is not None else mentioned_year
    response = {"groups": [], "terms": terms, "year": publication_year, "broadened": False}
    if not terms:
        return response

    def find(query: str) -> list[dict]:
        with driver.session(default_access_mode=READ_ACCESS) as session:
            rows = session.run(
                """CALL db.index.fulltext.queryNodes('evidence_text', $lucene)
                YIELD node AS span, score
                MATCH (p:Publication)-[:HAS_EVIDENCE]->(span)
                WHERE $year IS NULL OR p.publication_date STARTS WITH $year
                RETURN p.id AS publication_id, p.title AS title, p.url AS url,
                       p.publication_date AS publication_date,
                       span.text AS passage, score
                ORDER BY score DESC LIMIT 400""",
                lucene=query,
                year=publication_year,
            )
            return [dict(row) for row in rows]

    rows = find(" AND ".join(terms))
    if not rows and len(terms) > 1:
        rows = find(" OR ".join(terms))
        response["broadened"] = bool(rows)

    groups: dict[str, dict] = {}
    for row in rows:
        publication_id = row["publication_id"]
        group = groups.setdefault(publication_id, {
            "publication_id": publication_id,
            "title": row.get("title") or publication_id,
            "url": row.get("url"),
            "publication_date": row.get("publication_date"),
            "score": row["score"],
            "passages": [],
            "matched_terms": set(),
        })
        group["matched_terms"].update(_matching_terms(row.get("passage") or "", terms))
        if len(group["passages"]) < 3:
            group["passages"].append(row.get("passage") or "")

    response["groups"] = sorted(groups.values(), key=lambda item: item["score"], reverse=True)[:limit]
    for group in response["groups"]:
        group["matched_terms"] = sorted(group["matched_terms"])
    return response


def connect(uri: str, user: str, password: str):
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    return driver
