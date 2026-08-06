"""Tests for title_ratio similarity function."""
from ref_checker.similarity import title_ratio


def test_identical():
    assert title_ratio("Attention Is All You Need", "Attention Is All You Need") == 1.0


def test_case_insensitive():
    assert title_ratio("attention is all you need", "ATTENTION IS ALL YOU NEED") == 1.0


def test_punctuation_stripped():
    assert title_ratio("Hello, World!", "Hello World") == 1.0


def test_unicode_normalization_accents():
    assert title_ratio("Résumé", "Resume") == 1.0


def test_unicode_ligature():
    assert title_ratio("efficient computation", "e\ufb00icient computation") == 1.0


def test_completely_different():
    assert title_ratio("Quantum Computing", "Deep Learning for NLP") < 0.4


def test_partial_match():
    ratio = title_ratio("Flow Matching for Generative Modeling",
                        "Flow Matching for Generative Modeling in Latent Space")
    assert 0.7 < ratio < 1.0


def test_none_ref_title():
    assert title_ratio(None, "Some Title") == 0.0


def test_none_cand_title():
    assert title_ratio("Some Title", None) == 0.0


def test_both_none():
    assert title_ratio(None, None) == 0.0


def test_empty_string_ref():
    assert title_ratio("", "Some Title") == 0.0


def test_empty_string_cand():
    assert title_ratio("Some Title", "") == 0.0


def test_whitespace_normalized():
    assert title_ratio("hello   world", "hello world") == 1.0


def test_trailing_period_difference():
    assert title_ratio("MapReduce", "MapReduce.") == 1.0


def test_year_in_title_no_effect():
    r = title_ratio("STELLA 2015", "STELLA 2015")
    assert r == 1.0


def test_symmetric():
    a = "Persistent Homology Algorithms"
    b = "Algorithms for Persistent Homology"
    assert title_ratio(a, b) == title_ratio(b, a)
