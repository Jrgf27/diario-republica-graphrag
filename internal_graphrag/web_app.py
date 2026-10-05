"""Local Streamlit explorer for the Diário da República evidence graph."""

from __future__ import annotations

import html
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from streamlit_agraph import Config, Edge, Node, agraph

from internal_graphrag import backfill_jobs
from internal_graphrag import web_queries as queries

st.set_page_config(page_title="Diário Graph", page_icon=":material/account_tree:", layout="wide")
st.markdown(
    """<style>
    .stApp { background: #f7f5ef; color: #202b29; }
    .block-container { max-width: 1560px; padding-top: 1.7rem; }
    h1, h2, h3 { color: #203b36; letter-spacing: -.025em; }
    h1 { font-family: Georgia, serif; font-size: 2.5rem; }
    a { color: #1c655d; text-underline-offset: 3px; }
    .source-row { padding: .8rem 0 .35rem; border-top: 1px solid #d8ded7; }
    .source-meta { color: #53635e; font-size: .88rem; }
    .passage { white-space: pre-wrap; line-height: 1.5; font-size: .94rem; }
    </style>""",
    unsafe_allow_html=True,
)

PALETTE = {
    "Publication": "#1c655d", "LegalInstrument": "#b36730",
    "Issue": "#66759b", "Organization": "#8b658b",
    "ProcurementProcedure": "#a04f5c", "Category": "#697b42",
    "ActivityType": "#697b42", "EvidenceSpan": "#82928a",
    "Provision": "#82928a", "Place": "#66759b",
}
RELATION_LABELS = {
    "PUBLISHED_IN": "Published in issue",
    "ISSUED_BY": "Issued by",
    "CLASSIFIED_AS": "Classified as",
    "HAS_ACTIVITY": "Concerns activity",
    "IS_PUBLICATION_OF": "Publishes instrument",
    "ANNOUNCES": "Announces procedure",
    "MENTIONS": "Mentions instrument",
    "RUN_BY": "Run by",
    "LOCATED_IN": "Located in",
    "AMENDS": "Amends",
    "REVOKES": "Revokes",
    "CORRECTS": "Corrects",
}


@st.cache_resource(show_spinner=False)
def driver():
    return queries.connect(
        os.getenv("NEO4J_URI", "bolt://localhost:17687"),
        os.getenv("NEO4J_USER", "neo4j"),
        os.getenv("NEO4J_PASSWORD", ""),
    )


def source_link(url: str | None) -> str:
    if url and url.startswith("eli:http"):
        url = url[4:]
    if url and url.startswith(("https://", "http://")):
        safe = html.escape(url, quote=True)
        return f'<a href="{safe}" target="_blank" rel="noopener noreferrer">Official source ↗</a>'
    return ""


def show_details(detail: dict) -> None:
    properties = detail["properties"]
    label = detail["labels"][0] if detail["labels"] else "Node"
    title = properties.get("title") or properties.get("name")
    if not title and label == "LegalInstrument" and properties.get("number"):
        title = f'{properties.get("kind") or "Instrument"} {properties["number"]}/{properties.get("year") or "?"}'
    if not title and label == "Issue":
        title = f'Issue {properties.get("series") or ""} {properties.get("number") or ""} · {properties.get("date") or ""}'.strip()
    title = title or properties.get("id") or label
    st.subheader(str(title))
    st.caption(label.replace("LegalInstrument", "Legal instrument"))
    url = properties.get("url") or properties.get("eli") or detail.get("parent_url")
    link = source_link(url)
    if link:
        st.markdown(link, unsafe_allow_html=True)
    for key in ("summary", "text"):
        value = properties.get(key)
        if value:
            st.markdown(f"**{key.replace('_', ' ').title()}**")
            st.write(value)
    for key, title in (
        ("publication_date", "Publication date"), ("publication_type", "Type"),
        ("kind", "Instrument type"), ("number", "Number"),
        ("year", "Year"), ("series", "Series"),
    ):
        if properties.get(key) is not None:
            st.markdown(f"**{title}:** {html.escape(str(properties[key]))}")
    if properties.get("full_text"):
        with st.expander("Full publication text"):
            st.write(properties["full_text"])
    with st.expander("Technical details"):
        st.code(str(properties.get("id") or ""))


def highlighted_passage(passage: str, terms: list[str]) -> str:
    pattern = re.compile(r"(?<!\w)(" + "|".join(map(re.escape, terms)) + r")(?!\w)", re.IGNORECASE)
    return "".join(
        f"<mark>{html.escape(part)}</mark>" if pattern.fullmatch(part) else html.escape(part)
        for part in pattern.split(passage)
    ) if terms else html.escape(passage)


def relationship_label(edge: dict) -> str:
    text = RELATION_LABELS.get(edge["type"], edge["type"].replace("_", " ").title())
    return text + (" (unconfirmed)" if edge["review_status"] == "candidate" else "")


def draw_graph(nodes: list[dict], edges: list[dict], focus: str | None) -> str | None:
    graph_nodes = []
    for node in nodes:
        title = str(node["title"] or node["id"])
        display = title if len(title) <= 23 else title[:20] + "…"
        tooltip = f'{node["label"]}: {title}\nID: {node["id"]}'
        if node.get("date"):
            tooltip += f'\nDate: {node["date"]}'
        if node.get("snippet"):
            tooltip += f'\n\n{node["snippet"]}'
        graph_nodes.append(Node(
            id=node["id"], label=display, title=tooltip,
            color=PALETTE.get(node["label"], "#60756e"),
            size=30 if node["id"] == focus else 19,
            shape="dot",
            font={"size": 11, "face": "Arial", "color": "#203b36"},
        ))
    graph_edges = [Edge(
        source=edge["source"], target=edge["target"],
        label="",
        title=relationship_label(edge),
        color="#8c9a94", dashes=edge["review_status"] == "candidate",
    ) for edge in edges]
    config = Config(width=700, height=650, directed=True, physics=True, groups={})
    config.width = "100%"
    config.interaction = {"hover": True, "tooltipDelay": 150}
    return agraph(nodes=graph_nodes, edges=graph_edges, config=config)


st.title("Diário Graph")
st.caption("Find source passages in official publications, then explore how the publications connect.")

try:
    db = driver()
except (Neo4jError, ServiceUnavailable, OSError) as exc:
    st.error("Neo4j is unavailable. Start the local stack, then reload this page.")
    st.code(str(exc))
    st.stop()

with st.form("search_form"):
    question = st.text_input("Topic or question", placeholder="e.g. O que mudou no IVA em 2025?")
    all_years = st.checkbox("Include publications from other years", value=False)
    submitted = st.form_submit_button("Find passages", type="primary")

if submitted:
    query = question.strip()
    st.session_state["search_response"] = (
        queries.search(db, query, year="" if all_years else None) if query else {}
    )
    st.session_state["search_query"] = query
    st.session_state["graph_focus"] = None
    st.session_state["left_detail"] = None

with st.expander("Backfill earlier publications"):
    st.write("Choose a publication date range to add its archived issues to the graph.")
    job = backfill_jobs.backfill_status()
    running = bool(job and (job["state"] in {"starting", "running", "external"} or job.get("other_run_active")))
    today = datetime.now(ZoneInfo("Europe/Lisbon")).date()
    with st.form("backfill_form"):
        first, last = st.columns(2)
        start_date = first.date_input("From", value=today - timedelta(days=30))
        end_date = last.date_input("To", value=today, max_value=today)
        series = st.multiselect("Series", ["I", "II"], default=["I", "II"])
        max_publications = st.number_input(
            "Maximum new publications (0 = complete date range)",
            min_value=0, value=0, step=1,
        )
        launch = st.form_submit_button("Start backfill", disabled=running)
    if launch:
        try:
            job = backfill_jobs.start_backfill(start_date, end_date, series, int(max_publications))
            st.success("Backfill started. Use Refresh progress to check it.")
        except (ValueError, backfill_jobs.BackfillBusy, OSError) as exc:
            st.error(str(exc))
    if job:
        if job["state"] == "external":
            st.info("A backfill started outside this page is running.")
        else:
            st.markdown(
                f'**Latest backfill:** {job["state"].title()} · '
                f'{job["start"]} to {job["end"]} · Series {", ".join(job["series"])}'
            )
            if job.get("other_run_active"):
                st.info("Another archive run is now active outside this page.")
            if job.get("log_tail"):
                st.code(job["log_tail"], language="text")
        st.button("Refresh progress")

response = st.session_state.get("search_response", {})
results = response.get("groups", [])
focus = st.session_state.get("graph_focus")
left, right = st.columns([0.42, 0.58], gap="large")

with left:
    detail_id = st.session_state.get("left_detail")
    if detail_id:
        if st.button("← Back to results"):
            st.session_state["left_detail"] = None
            st.rerun()
        detail = queries.node_detail(db, detail_id)
        if detail:
            show_details(detail)
        else:
            st.warning("This node is no longer available.")
    else:
        st.subheader("Source passages")
        if not st.session_state.get("search_query"):
            st.info("Enter a topic or question to see matching publications and their graph.")
        elif not response.get("terms"):
            st.info("Add a topic word, such as IVA or contratação, to your question.")
        elif not results:
            st.info("No matching passages. Try fewer words or include publications from other years.")
        else:
            st.caption(f"{len(results)} publications with matching passages")
            filters = f'Matched topic: {", ".join(response["terms"])}'
            if response.get("year"):
                filters += f' · Published in {response["year"]}'
            st.caption(filters)
            if response.get("broadened"):
                st.info("No passages contained all topic words. These broader results contain some of them.")
            st.caption("These passages are sources to inspect, not a generated answer.")
            for index, row in enumerate(results):
                title = html.escape(row.get("title") or row["publication_id"])
                date = html.escape(str(row.get("publication_date") or "Date unknown"))
                st.markdown(
                    f'<div class="source-row"><strong>{title}</strong><br>'
                    f'<span class="source-meta">{date} · {source_link(row.get("url"))}</span></div>',
                    unsafe_allow_html=True,
                )
                matched = ", ".join(row["matched_terms"]) or ", ".join(response["terms"])
                st.caption(f"Why shown: passage mentions {matched}")
                st.markdown(
                    f'<div class="passage">{highlighted_passage(row["passages"][0], response["terms"])}</div>',
                    unsafe_allow_html=True,
                )
                if len(row["passages"]) > 1:
                    with st.expander(f'{len(row["passages"]) - 1} more matching passages'):
                        for passage in row["passages"][1:]:
                            st.markdown(
                                f'<div class="passage">{highlighted_passage(passage, response["terms"])}</div>',
                                unsafe_allow_html=True,
                            )
                if st.button("Explore connections", key=f"explore_result_{index}"):
                    st.session_state["graph_focus"] = row["publication_id"]
                    st.rerun()

with right:
    st.subheader("Graph")
    if not results:
        st.info("The graph will appear after a search.")
    else:
        if focus:
            st.caption("Connections for the selected publication")
            if st.button("← Show all matching publications"):
                st.session_state["graph_focus"] = None
                st.rerun()
            nodes, edges = queries.neighborhood(db, focus)
        else:
            st.caption("Connections among matching publications")
            nodes, edges = queries.search_neighborhood(
                db, [row["publication_id"] for row in results]
            )
        if nodes:
            st.caption(f"{len(nodes)} nodes · {len(edges)} connections · hover or click a node")
            selected_summary = st.empty()
            selected = draw_graph(nodes, edges, focus)
            if selected and selected in {node["id"] for node in nodes}:
                preview = next(node for node in nodes if node["id"] == selected)
                with selected_summary.container(border=True):
                    st.markdown(f'**{html.escape(str(preview["title"]))}** · {preview["label"]}')
                    if preview.get("snippet"):
                        st.write(preview["snippet"])
                    related = [edge for edge in edges if selected in (edge["source"], edge["target"])]
                    node_by_id = {node["id"]: node for node in nodes}
                    for edge in related[:6]:
                        other_id = edge["target"] if edge["source"] == selected else edge["source"]
                        other = node_by_id.get(other_id)
                        if other:
                            direction = "→" if edge["source"] == selected else "←"
                            st.caption(f'{direction} {relationship_label(edge)}: {other["title"]}')
                    st.button(
                        "Show full details on left", key="show_node_details",
                        on_click=lambda node_id=selected: st.session_state.update(left_detail=node_id),
                    )
            st.caption("Teal: publication · copper: legal instrument · dashed line: unconfirmed connection")
        else:
            st.info("No graph nodes were found for these results.")
