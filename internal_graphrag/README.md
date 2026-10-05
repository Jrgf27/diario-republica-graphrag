# Diário da República GraphRAG

This is a personal, PC-local ingest and retrieval service for Diário da
República publications.

## What works

- Reads the official Série I, Série II, and Série II Parte L RSS indexes (whose
  items link to PDF acts and notices) or
  explicit official publication URLs. The PDF parser handles the official
  PDFs that permit extraction with an empty decryption password; documents
  without extractable text are recorded as failures for OCR review.
- Discovers older Série I and II issue indexes by official ELI URLs, including
  supplements, and renders their paginated act lists in a local headless
  browser. Each act is ingested from its rendered official page. The SQLite
  manifest records completed issues so interrupted date-range runs resume.
- Keeps content-addressed raw PDF/HTML and extracted JSON snapshots, with a SQLite
  manifest for retries and idempotent reruns.
- Projects the v1 node types into Neo4j: `Publication`, `Issue`,
  `LegalInstrument`, publication-scoped `Provision`, `EvidenceSpan`,
  `Organization`, `ProcurementProcedure`, `Place`, `Category`, and
  `ActivityType`. Strong title/summary cues drive provisional categories;
  no category is assigned when the evidence is weak. Explicit citation and
  legal-effect candidates include an evidence ID and review status.
- Supports full-text passage search and amendment tracing with official URLs.

The extractor is deliberately conservative. `AMENDS`, `REVOKES`, and `CORRECTS`
are candidates pending review, not authoritative legal conclusions. The source
record and extracted snapshot are preserved so a parser change can be audited.
PDF evidence offsets refer to the normalized extracted text kept in the JSON
snapshot, while the original PDF remains available for checking.

## Local start

Run the local setup once from this PC. It creates an ignored, private
`.env.graphrag` password file, starts the dedicated Neo4j container, and
installs a user timer for 20:30 Europe/Lisbon each day:

```sh
./internal_graphrag/setup-local.sh
```

Run an ingestion immediately with `./internal_graphrag/run-local.sh`. The
timer catches up at next login if the PC was off at its scheduled time.
Repeated runs are safe. The RSS indexes are recent-item feeds, not a
historical archive. The timer only runs while the user's systemd manager
is active; the setup does not enable lingering or start work while the PC is off.
After changing the Python ingestion code, rerun `setup-local.sh` to rebuild
the local ingest image.
The internal Neo4j Browser is bound to `http://127.0.0.1:17474`; Bolt is bound
to `127.0.0.1:17687` so this stack does not occupy the main application's ports.

Check the local schedule with `systemctl --user list-timers
diario-republica-graphrag.timer` and past runs with `journalctl --user -u
diario-republica-graphrag.service`. Stop the schedule with `systemctl --user disable
--now diario-republica-graphrag.timer`, and stop the database with `docker compose
--env-file .env.graphrag -f docker-compose.graphrag.yml down` (this retains
its local volumes).

The same wrapper can query the local graph without putting credentials in your
shell. From the repository root:

```sh
./internal_graphrag/run-local.sh python -m internal_graphrag.cli ingest --feed I --limit 20
./internal_graphrag/run-local.sh python -m internal_graphrag.cli ingest --url https://files.diariodarepublica.pt/1s/2021/08/16600/0000200035.pdf
./internal_graphrag/run-local.sh python -m internal_graphrag.cli search "IVA construção"
./internal_graphrag/run-local.sh python -m internal_graphrag.cli trace pt:decreto-lei:48:2020
```

## Historical backfill

Start an automatic, resumable local backfill by giving it a date range:

```sh
./internal_graphrag/setup-backfill-local.sh 2025-10-03 2026-10-03
```

The setup builds the browser worker and starts a user service. It resumes on
the next login after a shutdown and retries failures after ten minutes. It
covers both series and the supplements exposed by official ELI issue indexes.
The full range can contain many thousands of acts and take substantial time.
Check its progress with:

```sh
systemctl --user status diario-republica-graphrag-backfill.service
journalctl --user -fu diario-republica-graphrag-backfill.service
```

For a bounded manual test, use `./internal_graphrag/run-archive-local.sh --from
2025-01-02 --to 2025-01-02 --series I --max-publications 2`. To stop an active
backfill, use `systemctl --user stop diario-republica-graphrag-backfill.service`;
disable it to prevent restart at login. The snapshot volume is retained.

The archive page layout is controlled by Diário da República and can change.
The worker checks every issue's advertised act count against collected links
and stops on a mismatch. It keeps failed act URLs for inspection and retries
them when the incomplete issue is rerun.

You can also supply individual official PDF URLs. For a list of URLs, put one
per line in a UTF-8 file and run `./internal_graphrag/backfill-local.sh
backfill-urls.txt`. Blank lines and lines beginning with `#` are ignored.

## View the graph

For a local web interface, start the Streamlit explorer:

```sh
./internal_graphrag/run-web-local.sh
```

Open `http://127.0.0.1:18501`. Enter a topic or question to see matching
passages grouped by publication on the left and their graph on the right.
A year in the question filters publications to that year unless you select
"Include publications from other years". Explore a publication to focus the
graph on its immediate relationships. Hover over a node for a preview, then
click it to see its connections or move its full details to the left column.
The overview graph shows up to three connections per publication so it remains
readable. To stop the interface and API, run `docker compose --env-file
.env.graphrag -f docker-compose.graphrag.yml --profile web --profile api stop web api`.

The **Backfill earlier publications** section starts a resumable archive run
for a chosen date range and series. It shows the latest run and recent worker
output. Only one archive run can use the snapshot volume at a time; the
existing command-line and scheduled workers use the same lock. A date range
can contain many publications, so use the optional maximum publication count
for a smaller first run.

The same startup script also starts a local JSON API for AI agents at
`http://127.0.0.1:18502`. It is bound to localhost and returns source
evidence, not generated legal conclusions. For example:

```sh
curl -G 'http://127.0.0.1:18502/query' --data-urlencode 'q=O que mudou no IVA em 2025?'
curl -X POST 'http://127.0.0.1:18502/query' -H 'Content-Type: application/json' \
  -d '{"query":"IVA construção","limit":5}'
curl -G 'http://127.0.0.1:18502/graph' --data-urlencode 'id=62-2025-942401300'
curl -G 'http://127.0.0.1:18502/node' --data-urlencode 'id=62-2025-942401300'
```

`/query` returns grouped passages plus a compact graph. `/node` returns node
properties without full publication text; `/graph` returns a node's immediate
relationships. `/health` checks Neo4j, and `/openapi.json` describes the API.

The web interface does not generate legal answers; it presents indexed evidence
and official source links for inspection.

The original Neo4j Browser is also available:

Open `http://127.0.0.1:17474/browser/`, select `neo4j://` or `bolt://`, and
set the connection URL to `127.0.0.1:17687` (Neo4j Browser's default port
7687 belongs to a different stack). Sign in as `neo4j` with the password in
the ignored local `.env.graphrag` file. To see publication connections:

```cypher
MATCH (p:Publication)-[r]->(n) RETURN p,r,n LIMIT 50
```

To see the node and relationship types, run `CALL db.schema.visualization()`.

To record a human decision, pass the `source`, `effect`, `target`, and
`evidence_id` returned by `trace` to `review`, followed by `confirmed` or
`rejected` and `--reviewer YOUR_NAME`. A changed source snapshot resets its
derived effects to candidate status for renewed review.

Neo4j connection options are `NEO4J_URI`, `NEO4J_USER` and `NEO4J_PASSWORD`.
`GRAPHRAG_DATA_DIR` controls local snapshot storage; its default is
`internal_graphrag/data`. A `--no-graph` ingestion stores source snapshots
without connecting to Neo4j. Restore the graph later with `project` once the
database is available.

## Scope and next gates

The first milestone covers the source/evidence path and graph retrieval. Before
using it for rule-change monitoring, validate a dated sample from both series,
review organization and procurement entity matches, and measure extraction
recall and precision against hand-labeled examples. The identifier for a
procedure derived from an internal reference remains provisional. Legal
effect candidates require human review before operational use.
