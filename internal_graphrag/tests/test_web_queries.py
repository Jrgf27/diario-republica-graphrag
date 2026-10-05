from internal_graphrag.web_search import parse_search


def test_portuguese_question_uses_topic_and_publication_year():
    assert parse_search("O que mudou no IVA em 2025?") == (["iva"], "2025")


def test_english_vat_question_maps_to_the_indexed_portuguese_term():
    assert parse_search("What changed about VAT in 2025?") == (["iva"], "2025")


def test_multiple_topic_words_are_kept_for_narrow_search():
    assert parse_search("IVA construção") == (["iva", "construção"], None)
