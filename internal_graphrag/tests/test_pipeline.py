from pathlib import Path

import pytest

from internal_graphrag.extract import extract
from internal_graphrag.serialization import load_extraction
from internal_graphrag.source import (
    feed_items,
    feed_links,
    issue_links,
    parse_publication,
    required_issue_links,
)
from internal_graphrag.storage import Store

URL = "https://diariodarepublica.pt/dr/detalhe/lei/68-2021-170221049"
HTML = """<html><head><title>Lei n.º 68/2021, de 26 de agosto | DR</title>
<meta name="description" content="Dados abertos" /></head><body>
<nav>Navigation should not be source text</nav>
<main><p>Publicação: Diário da República n.º 166/2021, Série I de 2021-08-26</p>
<p>Lei n.º 68/2021 altera o Decreto-Lei n.º 1/2020.</p>
<p>Artigo 1.º</p><p>Objeto da presente lei e mais palavras de texto para o extrator.</p>
<p>Artigo 2.º</p><p>Aplicação da presente lei no território nacional.</p>
</main></body></html>""".encode()


def test_extract_and_snapshot(tmp_path: Path):
    record = parse_publication(URL, HTML)
    assert record.issue_id == "I:2021-08-26:166:0"
    assert record.publication_type == "lei"
    assert "Navigation" not in record.text
    result = extract(record)
    assert len(result.provisions) == 2
    assert any(
        e.predicate == "AMENDS" and e.review_status == "candidate"
        for e in result.effects
    )
    store = Store(tmp_path)
    assert store.save(result, HTML)
    assert not store.save(result, HTML)
    restored = load_extraction(store.pending_projection()[0])
    assert restored == result


def test_feed_and_issue_discovery():
    feed = b"""<rss><channel><item><title>Act title</title><description>Summary</description><link>https://files.diariodarepublica.pt/1s/2026/10/19200/0000200031.pdf</link></item></channel></rss>"""
    assert len(feed_links(feed)) == 1
    assert feed_items(feed)[0].summary == "Summary"
    issue = f'<main><a href="{URL}">Lei</a><a href="https://example.com/x">Outside</a></main>'.encode()
    assert issue_links(issue) == [URL]
    with pytest.raises(ValueError, match="exposed no act links"):
        required_issue_links(b'<div id="reactContainer"></div>', URL)


def test_no_effect_from_citation_alone():
    html = HTML.replace(b"altera o Decreto-Lei", b"cita o Decreto-Lei")
    result = extract(parse_publication(URL, html))
    assert not result.effects


def test_current_dr_text_container_and_relative_issue_link():
    html = """<html><head><title>Notice | DR</title></head><body>
    <div>Emitente:</div><button>Agência para o Desenvolvimento e Coesão, IP</button>
    <div>Publicação: Diário da República n.º 44/2026, Série II de 2026-03-04</div>
    <a href="/dr/detalhe/lei/68-2021-170221049">link</a>
    <div class="texto int-links" id="b6-Texto_Conteudo3"><div>
    1 - IDENTIFICAÇÃO E CONTACTOS\nDesignação da entidade adjudicante: Agência para o Desenvolvimento e Coesão, IP\nNIPC: 510928374\nNúmero de referência interna: R/025/2026
    Designação do contrato: Aquisição de serviços de arquitetura de dados.
    9 - LOCAL DA EXECUÇÃO DO CONTRATO\nNUT III: PT170\n10 - PRAZO DE EXECUÇÃO
    </div></div></body></html>""".encode()
    record = parse_publication(
        "https://diariodarepublica.pt/dr/detalhe/anuncio-procedimento/5219-2026-1066940275",
        html,
    )
    assert record.issue_id == "II:2026-03-04:44:0"
    assert "NIPC: 510928374" in record.text
    assert issue_links(html) == [URL]
    result = extract(record)
    assert result.organization["id"] == "nipc:510928374"
    assert result.procedure["internal_reference"] == "R/025/2026"
    assert result.place["code"] == "PT170"
    assert result.activities == ("procurement",)


def test_eli_rdfa_uses_resource_as_target():
    own = "http://data.dre.pt/eli/dec-lei/48/2020/08/03/p/dre"
    target = "http://data.dre.pt/eli/dec-lei/8/2007/01/17/p/dre"
    html = (
        f"<html><head><title>Decreto-Lei n.º 48/2020 | DR</title></head><body>"
        f'<span about="{own}" typeof="eli:LegalResource"></span>'
        f'<span about="{own}" property="eli:amends" resource="{target}"></span>'
        '<div class="texto" id="x-Texto_Conteudo3">'
        "Decreto-Lei n.º 48/2020 altera o Decreto-Lei n.º 8/2007. "
        "Esta publicação contém texto suficiente para permitir a extração da fonte oficial."
        "</div></body></html>"
    ).encode()
    record = parse_publication(
        "https://diariodarepublica.pt/dr/detalhe/decreto-lei/48-2020-139348418", html
    )
    assert record.eli == own
    assert record.metadata_relations == (("amends", target),)
