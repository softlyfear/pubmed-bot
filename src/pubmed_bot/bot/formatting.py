"""HTML-разметка списка и карточки. Без разреза тега, лимит 4096."""

import re
from html import escape

from pubmed_bot.bot.texts import (
    FULLTEXT_IN_FILE,
    FULLTEXT_UNAVAILABLE,
    TEXT_UNAVAILABLE,
    TRANSLATION_MISSING,
)
from pubmed_bot.domain.models import AbstractSection, Article, ArticleListItem, NoteListItem
from pubmed_bot.services.article import OpenedArticle

TELEGRAM_MESSAGE_LIMIT = 4096
_MIN_TITLE = 40
_PREFIX_ROOM = 8
_TAG_CLOSE_ROOM = 16
_HTML_TAG = re.compile(r"<(/?)(a|b|i)(\s[^>]*)?>", re.IGNORECASE)
PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
DOI_URL = "https://doi.org/{doi}"
PMC_URL = "https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"


def format_list(items: tuple[ArticleListItem, ...]) -> str:
    """Одно сообщение списка. Ужимает заголовки, не режет HTML посередине тега."""
    if not items:
        return ""
    max_title = 240
    html = _render_list(items, max_title)
    while len(html) > TELEGRAM_MESSAGE_LIMIT and max_title > _MIN_TITLE:
        max_title = max(_MIN_TITLE, max_title - 40)
        html = _render_list(items, max_title)
    kept = list(items)
    while len(html) > TELEGRAM_MESSAGE_LIMIT and len(kept) > 1:
        kept.pop()
        html = _render_list(tuple(kept), _MIN_TITLE)
    max_title = _MIN_TITLE
    while len(html) > TELEGRAM_MESSAGE_LIMIT and max_title > 8:
        max_title -= 4
        html = _render_list(tuple(kept[:1]), max_title)
    return html


def format_notes_list(items: tuple[NoteListItem, ...]) -> str:
    """Нумерованный список заметок: заголовок и превью, без даты/RCT поиска."""
    if not items:
        return ""
    max_title = 240
    max_preview = 120
    html = _render_notes(items, max_title, max_preview)
    while len(html) > TELEGRAM_MESSAGE_LIMIT and (max_title > 8 or max_preview > 8):
        if max_title > 8:
            max_title = max(8, max_title - 40)
        if max_preview > 8:
            max_preview = max(8, max_preview - 20)
        html = _render_notes(items, max_title, max_preview)
    return html


def _render_notes(items: tuple[NoteListItem, ...], max_title: int, max_preview: int) -> str:
    lines = [
        (
            f"{index}. <b>{escape(_clip(item.title, max_title))}</b>\n"
            f"{escape(_clip(item.preview, max_preview))}"
        )
        for index, item in enumerate(items, start=1)
    ]
    return "\n\n".join(lines)


def format_article_messages(opened: OpenedArticle) -> tuple[str, ...]:
    """Карточка: блоки HTML, нарезка по абзацам с `1/3`."""
    return split_html_messages(article_blocks(opened))


def article_blocks(opened: OpenedArticle) -> list[str]:
    article = opened.article
    blocks: list[str] = [_title_block(article, opened.title_ru, opened.translation_failed)]
    if article.authors or article.journal:
        info_parts: list[str] = []
        if article.authors:
            info_parts.append(escape(", ".join(article.authors)))
        if article.journal:
            info_parts.append(f"<i>{escape(article.journal)}</i>")
        blocks.append("\n".join(info_parts))
    meta = _meta_line(
        ArticleListItem(
            pmid=article.pmid,
            title_en=article.title_en,
            date_label=article.date_label,
            status_label=article.status_label,
            integrity_label=article.integrity_label,
            pub_type_label=article.pub_type_label,
        )
    )
    if meta:
        blocks.append(escape(meta))
    blocks.append(_links(article))
    abstract = opened.abstract_ru if opened.abstract_ru is not None else article.abstract
    if abstract:
        blocks.append("<b>Abstract</b>")
        blocks.extend(_abstract_blocks(abstract))
        if opened.abstract_ru is None and opened.translation_failed:
            blocks.append(f"<i>{TRANSLATION_MISSING}</i>")
    oa_paras = opened.body_paragraphs()
    if opened.has_oa and oa_paras:
        blocks.append(escape(FULLTEXT_IN_FILE))
    elif abstract:
        blocks.append(escape(FULLTEXT_UNAVAILABLE))
    else:
        blocks.append(escape(TEXT_UNAVAILABLE))
    return blocks


def split_html_messages(
    blocks: list[str],
    *,
    limit: int = TELEGRAM_MESSAGE_LIMIT,
) -> tuple[str, ...]:
    """Склеивает блоки, не разрезая тег. Несколько кусков — префикс `1/3`."""
    if not blocks:
        return ()
    budget = max(limit - _PREFIX_ROOM, 64)
    packed: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current_len
        if current:
            packed.append("\n\n".join(current))
            current.clear()
            current_len = 0

    for block in blocks:
        if len(block) > budget:
            flush()
            packed.extend(_split_oversized(block, budget))
            continue
        extra = 2 if current else 0
        if current and current_len + extra + len(block) > budget:
            flush()
        if current:
            current_len += 2
        current.append(block)
        current_len += len(block)
    flush()
    if len(packed) == 1:
        return (packed[0],)
    total = len(packed)
    return tuple(f"{index}/{total}\n{chunk}" for index, chunk in enumerate(packed, start=1))


def _title_block(article: Article, title_ru: str | None, translation_failed: bool) -> str:
    lines = [f"<b>{escape(article.title_en)}</b>"]
    if title_ru:
        lines.append(escape(title_ru))
    elif translation_failed:
        lines.append(f"<i>{TRANSLATION_MISSING}</i>")
    return "\n".join(lines)


def _abstract_blocks(sections: tuple[AbstractSection, ...]) -> list[str]:
    blocks: list[str] = []
    for section in sections:
        body = escape(section.text)
        if section.label:
            blocks.append(f"<b>{escape(section.label)}</b>\n{body}")
        else:
            blocks.append(body)
    return blocks


def _links(article: Article) -> str:
    parts = [f'<a href="{PUBMED_URL.format(pmid=article.pmid)}">PubMed</a>']
    if article.doi:
        doi = escape(article.doi)
        parts.append(f'<a href="{DOI_URL.format(doi=doi)}">{doi}</a>')
    if article.pmcid:
        parts.append(f'<a href="{PMC_URL.format(pmcid=article.pmcid)}">{escape(article.pmcid)}</a>')
    return " · ".join(parts)


def _split_oversized(block: str, budget: int) -> list[str]:
    """Режет длинный блок и закрывает/открывает b/i/a, чтобы кусок оставался валидным HTML."""
    inner = max(budget - _TAG_CLOSE_ROOM, 64)
    parts: list[str] = []
    rest = block
    carry = ""
    while len(carry) + len(rest) > budget:
        text = carry + rest
        cut = _safe_cut(text, min(inner, len(text) - 1))
        if cut <= len(carry):
            cut = min(inner, len(text) - 1)
        piece = text[:cut].rstrip()
        rest = text[cut:].lstrip()
        openers = _open_tags(piece)
        parts.append(piece + _close_tags(openers))
        carry = "".join(openers)
    tail = carry + rest
    if tail:
        parts.append(tail)
    return parts


def _open_tags(html: str) -> list[str]:
    stack: list[str] = []
    for match in _HTML_TAG.finditer(html):
        name = match.group(2).lower()
        if match.group(1):
            for index in range(len(stack) - 1, -1, -1):
                if stack[index].lower().startswith(f"<{name}"):
                    stack.pop(index)
                    break
        else:
            stack.append(match.group(0))
    return stack


def _close_tags(openers: list[str]) -> str:
    closers: list[str] = []
    for tag in reversed(openers):
        name = tag[1:].split(None, 1)[0].rstrip(">").lower()
        closers.append(f"</{name}>")
    return "".join(closers)


def _safe_cut(text: str, budget: int) -> int:
    window = text[:budget]
    cut = budget
    amp = window.rfind("&")
    semi = window.rfind(";")
    if amp > semi:
        cut = amp
        window = text[:cut]
    last_lt = window.rfind("<")
    last_gt = window.rfind(">")
    if last_lt > last_gt:
        cut = last_lt
        window = text[:cut]
    space = window.rfind(" ")
    if space >= 40:
        return space
    return max(cut, 1)


def _render_list(items: tuple[ArticleListItem, ...], max_title: int) -> str:
    blocks: list[str] = []
    for index, item in enumerate(items, start=1):
        title_en = escape(_clip(item.title_en, max_title))
        if item.title_ru:
            title_ru = escape(_clip(item.title_ru, max_title))
        else:
            title_ru = f"<i>{TRANSLATION_MISSING}</i>"
        lines = [f"{index}. <b>{title_en}</b>", title_ru]
        meta = _meta_line(item)
        if meta:
            lines.append(escape(meta))
        if item.viewed:
            lines.append("<i>просмотрено</i>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _meta_line(item: ArticleListItem) -> str | None:
    parts: list[str] = []
    if item.date_label:
        parts.append(item.date_label)
    if item.status_label:
        parts.append(str(item.status_label))
    if item.integrity_label:
        parts.append(str(item.integrity_label))
    elif item.pub_type_label:
        parts.append(str(item.pub_type_label))
    if not parts:
        return None
    return " · ".join(parts)
