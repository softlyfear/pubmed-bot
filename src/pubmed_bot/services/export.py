"""Экспорт карточки и перевода в UTF-8 .txt. Без секретов и без Telegram."""

from pubmed_bot.domain.models import Article
from pubmed_bot.services.article import OpenedArticle

PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
DOI_URL = "https://doi.org/{doi}"
PMC_URL = "https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
_TRANSLATION_MISSING = "перевод недоступен"
_TEXT_UNAVAILABLE = "текст недоступен"
_FULLTEXT_UNAVAILABLE = "полный текст недоступен (только PMC Open Access, не PDF журнала)"


def export_filename(pmid: str) -> str:
    return f"pubmed-{pmid}.txt"


def render_export_txt(opened: OpenedArticle) -> str:
    """Карточка и перевод обычным текстом. Без HTML и без ключей API."""
    article = opened.article
    lines: list[str] = [article.title_en]
    if opened.title_ru:
        lines.append(opened.title_ru)
    elif opened.translation_failed:
        lines.append(_TRANSLATION_MISSING)
    if article.authors:
        lines.append("")
        lines.append("Авторы:")
        lines.extend(f"  {author}" for author in article.authors)
    if article.journal:
        lines.append("")
        lines.append(article.journal)
    meta = [
        part
        for part in (article.date_label, _label(article.status_label), _integrity_or_type(article))
        if part
    ]
    if meta:
        lines.append(" · ".join(meta))
    lines.append("")
    lines.append(PUBMED_URL.format(pmid=article.pmid))
    if article.doi:
        lines.append(DOI_URL.format(doi=article.doi))
    if article.pmcid:
        lines.append(PMC_URL.format(pmcid=article.pmcid))
    abstract = opened.abstract_ru if opened.abstract_ru is not None else article.abstract
    if abstract:
        lines.append("")
        lines.append("=" * 50)
        lines.append("Abstract")
        lines.append("=" * 50)
        for section in abstract:
            if section.label:
                lines.append("")
                lines.append(section.label)
            lines.append(section.text)
        if opened.abstract_ru is None and opened.translation_failed:
            lines.append("")
            lines.append(_TRANSLATION_MISSING)
    oa = opened.fulltext_ru if opened.fulltext_ru is not None else article.fulltext_paragraphs
    if opened.has_oa and oa:
        lines.append("")
        lines.append("=" * 50)
        lines.append("Полный текст")
        lines.append("=" * 50)
        lines.append("")
        lines.extend(oa)
        if opened.fulltext_ru is None and opened.translation_failed:
            lines.append("")
            lines.append(_TRANSLATION_MISSING)
    elif abstract:
        lines.append("")
        lines.append(_FULLTEXT_UNAVAILABLE)
    else:
        lines.append("")
        lines.append(_TEXT_UNAVAILABLE)
    return "\n".join(lines).strip() + "\n"


def _label(value: object | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _integrity_or_type(article: Article) -> str | None:
    if article.integrity_label:
        return str(article.integrity_label)
    if article.pub_type_label:
        return str(article.pub_type_label)
    return None
