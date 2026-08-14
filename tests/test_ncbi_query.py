"""Сборка term NCBI и пагинация. Без сети."""

import pytest

from pubmed_bot.adapters.ncbi.query import (
    ENGLISH_FILTER,
    HASABSTRACT_FILTER,
    NON_RESEARCH_PT,
    PAGE_SIZE,
    build_term,
    retstart_for_page,
)
from pubmed_bot.domain.script import contains_cyrillic


def test_page_offset() -> None:
    assert PAGE_SIZE == 10
    assert retstart_for_page(1) == 0
    assert retstart_for_page(2) == 10
    assert retstart_for_page(3) == 20


def test_page_must_be_positive() -> None:
    with pytest.raises(ValueError):
        retstart_for_page(0)


def test_build_term_hasabstract_and_research_filters() -> None:
    term = build_term("gluteus hypertrophy")
    assert term == (
        "(gluteus hypertrophy) AND english[lang] AND hasabstract NOT "
        "(letter[pt] OR editorial[pt] OR news[pt] OR comment[pt] OR "
        '"newspaper article"[pt])'
    )
    assert HASABSTRACT_FILTER in term
    assert ENGLISH_FILTER in term
    for tag in ("letter[pt]", "editorial[pt]", "news[pt]", "comment[pt]", "newspaper article"):
        assert tag in term
    assert "sport" not in term
    assert "humans[MeSH]" not in term
    assert not contains_cyrillic(term)
    assert NON_RESEARCH_PT in term


def test_build_term_does_not_use_and_not_before_pt() -> None:
    term = build_term("gluteal hypertrophy")
    assert term == (
        "(gluteal hypertrophy) AND english[lang] AND hasabstract NOT "
        "(letter[pt] OR editorial[pt] OR news[pt] OR comment[pt] OR "
        '"newspaper article"[pt])'
    )
    assert "AND NOT (" not in term
    assert HASABSTRACT_FILTER in term
    assert ENGLISH_FILTER in term
    for tag in ("letter[pt]", "editorial[pt]", "news[pt]", "comment[pt]", "newspaper article"):
        assert tag in term


def test_empty_query_rejected() -> None:
    with pytest.raises(ValueError):
        build_term("   ")
