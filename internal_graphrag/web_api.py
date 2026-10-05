"""Small localhost JSON API for agents inspecting the knowledge graph."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from neo4j.exceptions import Neo4jError, ServiceUnavailable

from . import web_queries as queries

OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "Diário Graph API", "version": "0.1.0"},
    "paths": {
        "/query": {
            "get": {
                "summary": "Find source passages and a compact graph",
                "parameters": [
                    {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 12, "minimum": 1, "maximum": 20}},
                ],
                "responses": {"200": {"description": "Grouped source passages and graph"}},
            },
            "post": {
                "summary": "Find source passages from a JSON query",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["query"],
                    "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 12}},
                }}}},
                "responses": {"200": {"description": "Grouped source passages and graph"}},
            },
        },
        "/node": {"get": {"summary": "Read one graph node", "parameters": [
            {"name": "id", "in": "query", "required": True, "schema": {"type": "string"}},
        ], "responses": {"200": {"description": "Node properties"}}}},
        "/graph": {"get": {"summary": "Read one node's immediate graph", "parameters": [
            {"name": "id", "in": "query", "required": True, "schema": {"type": "string"}},
        ], "responses": {"200": {"description": "Nodes and relationships"}}}},
        "/health": {"get": {"summary": "Check Neo4j connectivity", "responses": {
            "200": {"description": "Ready"}, "503": {"description": "Neo4j unavailable"},
        }}},
    },
}


class GraphApiHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _query(self, question: str, limit: int) -> None:
        if not question.strip() or len(question) > 500:
            self._json({"error": "Provide a query of 1 to 500 characters."}, HTTPStatus.BAD_REQUEST)
            return
        response = queries.search(self.server.driver, question.strip(), limit)
        nodes, edges = queries.search_neighborhood(
            self.server.driver, [item["publication_id"] for item in response["groups"]]
        )
        self._json({
            "kind": "source_evidence",
            "query": question.strip(),
            "terms": response["terms"],
            "publication_year": response["year"],
            "broadened": response["broadened"],
            "results": response["groups"],
            "graph": {"nodes": nodes, "edges": edges},
        })

    def _limit(self, raw: object) -> int | None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if 1 <= value <= 20 else None

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                self.server.driver.verify_connectivity()
                self._json({"status": "ok"})
            elif parsed.path == "/openapi.json":
                self._json(OPENAPI)
            elif parsed.path == "/query":
                limit = self._limit(params.get("limit", [12])[0])
                if limit is None:
                    self._json({"error": "limit must be between 1 and 20."}, HTTPStatus.BAD_REQUEST)
                else:
                    self._query(params.get("q", [""])[0], limit)
            elif parsed.path in {"/node", "/graph"}:
                node_id = params.get("id", [""])[0]
                if not node_id or len(node_id) > 500:
                    self._json({"error": "Provide a node id of 1 to 500 characters."}, HTTPStatus.BAD_REQUEST)
                    return
                if parsed.path == "/node":
                    detail = queries.node_detail(self.server.driver, node_id)
                    if detail is None:
                        self._json({"error": "Node not found."}, HTTPStatus.NOT_FOUND)
                    else:
                        detail["properties"].pop("full_text", None)
                        self._json({"id": node_id, **detail})
                else:
                    nodes, edges = queries.neighborhood(self.server.driver, node_id)
                    if not nodes:
                        self._json({"error": "Node not found."}, HTTPStatus.NOT_FOUND)
                    else:
                        self._json({"id": node_id, "nodes": nodes, "edges": edges})
            else:
                self._json({"error": "Unknown endpoint."}, HTTPStatus.NOT_FOUND)
        except (Neo4jError, ServiceUnavailable, OSError):
            self._json({"error": "Neo4j is unavailable or the query failed."}, HTTPStatus.SERVICE_UNAVAILABLE)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/query":
            self._json({"error": "Unknown endpoint."}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("Invalid body size")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise TypeError("JSON object required")
            limit = self._limit(request.get("limit", 12))
            if limit is None:
                raise ValueError("limit must be between 1 and 20")
            question = request.get("query", "")
            if not isinstance(question, str):
                raise TypeError("query must be text")
        except (TypeError, ValueError):
            self._json({"error": "Send JSON with query text and optional limit (1–20)."}, HTTPStatus.BAD_REQUEST)
            return
        try:
            self._query(question, limit)
        except (Neo4jError, ServiceUnavailable, OSError):
            self._json({"error": "Neo4j is unavailable or the query failed."}, HTTPStatus.SERVICE_UNAVAILABLE)


def main() -> None:
    driver = queries.connect(
        os.getenv("NEO4J_URI", "bolt://localhost:17687"),
        os.getenv("NEO4J_USER", "neo4j"),
        os.getenv("NEO4J_PASSWORD", ""),
    )
    server = ThreadingHTTPServer(("0.0.0.0", 8502), GraphApiHandler)
    server.driver = driver
    try:
        server.serve_forever()
    finally:
        driver.close()
        server.server_close()


if __name__ == "__main__":
    main()
