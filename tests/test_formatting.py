"""Формат списка и экранирование HTML."""

from pubmed_bot.bot.formatting import (
    TELEGRAM_MESSAGE_LIMIT,
    format_article_messages,
    format_list,
    split_html_messages,
)
from pubmed_bot.bot.keyboards import MAX_CALLBACK_BYTES, article_callback_data
from pubmed_bot.bot.texts import FULLTEXT_IN_FILE, FULLTEXT_UNAVAILABLE
from pubmed_bot.domain.enums import IntegrityLabel, PublicationStatusLabel, PubTypeLabel
from pubmed_bot.domain.models import AbstractSection, Article, ArticleListItem
from pubmed_bot.services.article import OpenedArticle


def test_list_escapes_ncbi_title() -> None:
    items = (
        ArticleListItem(
            pmid="1",
            title_en="A <b>bold</b> & title",
            title_ru="Заголовок <script>",
            date_label="Mar 2024",
            status_label=PublicationStatusLabel.PUBLISHED,
            pub_type_label=PubTypeLabel.RCT,
        ),
    )
    html = format_list(items)
    assert "&lt;b&gt;bold&lt;/b&gt;" in html
    assert "&amp;" in html
    assert "&lt;script&gt;" in html
    assert "<b>A" in html
    assert "Mar 2024 · опубл. · RCT" in html


def test_integrity_overrides_pub_type() -> None:
    item = ArticleListItem(
        pmid="2",
        title_en="X",
        title_ru=None,
        integrity_label=IntegrityLabel.RETRACTED,
        pub_type_label=PubTypeLabel.REVIEW,
    )
    html = format_list((item,))
    assert "отозвана" in html
    assert "обзор" not in html
    assert "перевод недоступен" in html


def test_open_callback_fits_64_bytes() -> None:
    data = article_callback_data("12345678")
    assert data == "o:12345678"
    assert len(data.encode("utf-8")) <= MAX_CALLBACK_BYTES
    long_pmid = "1" * 62
    assert len(article_callback_data(long_pmid).encode("utf-8")) == MAX_CALLBACK_BYTES


def test_format_list_fits_telegram_limit() -> None:
    items = tuple(
        ArticleListItem(
            pmid=str(i),
            title_en=("Very long English title " * 40) + str(i),
            title_ru=("Очень длинный русский заголовок " * 40) + str(i),
            date_label="Mar 2024",
            status_label=PublicationStatusLabel.PUBLISHED,
            pub_type_label=PubTypeLabel.RCT,
        )
        for i in range(1, 11)
    )
    html = format_list(items)
    assert 0 < len(html) <= TELEGRAM_MESSAGE_LIMIT
    assert "1." in html
    assert html.count("<b>") == html.count("</b>")


def test_viewed_marker() -> None:
    item = ArticleListItem(pmid="1", title_en="T", title_ru="Р", viewed=True)
    assert "просмотрено" in format_list((item,))


def test_split_messages_numbers_chunks() -> None:
    blocks = ["a" * 2000, "b" * 2000, "c" * 2000]
    chunks = split_html_messages(blocks)
    assert len(chunks) >= 2
    assert chunks[0].startswith("1/")
    assert chunks[-1].startswith(f"{len(chunks)}/")
    for chunk in chunks:
        assert len(chunk) <= TELEGRAM_MESSAGE_LIMIT


def test_format_list_keeps_tags_with_entities() -> None:
    items = tuple(
        ArticleListItem(
            pmid=str(i),
            title_en="A & B < C " * 80,
            title_ru="и & ещё " * 80,
        )
        for i in range(1, 11)
    )
    html = format_list(items)
    assert len(html) <= TELEGRAM_MESSAGE_LIMIT
    assert html.count("<b>") == html.count("</b>")
    assert "&amp;" in html
    assert not html.endswith("&")
    assert not html.endswith("&amp")


def test_split_does_not_leave_unclosed_tags() -> None:
    block = "<b>" + ("word " * 900) + "</b>"
    chunks = split_html_messages([block])
    assert len(chunks) >= 2
    for chunk in chunks:
        body = chunk.split("\n", 1)[1] if "/" in chunk.split("\n", 1)[0] else chunk
        assert body.count("<b>") == body.count("</b>")
        assert len(chunk) <= TELEGRAM_MESSAGE_LIMIT


def test_compact_card_omits_long_oa_body() -> None:
    opened = OpenedArticle(
        article=Article(
            pmid="9",
            title_en="Title",
            abstract=(AbstractSection(text="Short abstract."),),
            fulltext_paragraphs=("OA " * 20_000,),
        ),
        title_ru="Заголовок",
        abstract_ru=(AbstractSection(text="Короткий абстракт."),),
        fulltext_ru=("RU OA " * 20_000,),
        has_oa=True,
        translation_failed=False,
    )
    chunks = format_article_messages(opened)
    html = "\n".join(chunks)
    assert len(chunks) == 1
    assert FULLTEXT_IN_FILE in html
    assert "<b>Полный текст</b>" not in html
    assert "RU OA" not in html
    assert "<b>Abstract</b>" in html
    assert "Короткий абстракт" in html


def test_structured_abstract_headings_and_blank_line() -> None:
    opened = OpenedArticle(
        article=Article(pmid="1", title_en="T"),
        title_ru="З",
        abstract_ru=(
            AbstractSection(text="One", label="BACKGROUND"),
            AbstractSection(text="Two", label="METHODS"),
        ),
        fulltext_ru=None,
        has_oa=False,
        translation_failed=False,
    )
    html = "\n".join(format_article_messages(opened))
    assert "<b>BACKGROUND</b>" in html
    assert "<b>METHODS</b>" in html
    idx_bg = html.index("<b>BACKGROUND</b>")
    idx_me = html.index("<b>METHODS</b>")
    assert "\n\n" in html[idx_bg:idx_me]
    assert FULLTEXT_UNAVAILABLE in html
    assert FULLTEXT_IN_FILE not in html


def test_notes_list_escapes_title_and_preview() -> None:
    from pubmed_bot.bot.formatting import format_notes_list
    from pubmed_bot.domain.models import NoteListItem

    html = format_notes_list((NoteListItem(pmid="1", title="A <b>t</b>", preview="p & q"),))
    assert format_notes_list(()) == ""
    assert "1. <b>A &lt;b&gt;t&lt;/b&gt;</b>" in html
    assert "p &amp; q" in html
    assert "<b>t</b>" not in html


def test_notes_list_fits_telegram_limit() -> None:
    from pubmed_bot.bot.formatting import format_notes_list
    from pubmed_bot.domain.models import NoteListItem

    items = tuple(
        NoteListItem(
            pmid=str(i),
            title=("Very long English title " * 40) + str(i),
            preview="p" * 120,
        )
        for i in range(1, 11)
    )
    html = format_notes_list(items)
    assert 0 < len(html) <= TELEGRAM_MESSAGE_LIMIT
    assert "1." in html
    assert html.count("<b>") == html.count("</b>")
    heavy = tuple(
        NoteListItem(pmid=str(i), title="<" * 400, preview="p" * 120) for i in range(1, 11)
    )
    heavy_html = format_notes_list(heavy)
    assert 0 < len(heavy_html) <= TELEGRAM_MESSAGE_LIMIT
    assert heavy_html.count("<b>") == heavy_html.count("</b>")
    previews = tuple(
        NoteListItem(pmid=str(i), title=f"PMID {i}", preview="<" * 120) for i in range(1, 11)
    )
    preview_html = format_notes_list(previews)
    assert 0 < len(preview_html) <= TELEGRAM_MESSAGE_LIMIT
    assert preview_html.count("<b>") == preview_html.count("</b>")
