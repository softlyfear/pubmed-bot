"""Разбор ESummary JSON и EFetch XML PubMed/PMC в доменные модели."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from xml.etree import ElementTree as ET

from pubmed_bot.domain.enums import IntegrityLabel, PublicationStatusLabel, PubTypeLabel
from pubmed_bot.domain.models import AbstractSection, Article, ArticleListItem

_DOCTYPE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)

# ESummary JSON отдаёт pubstatus числом (NCBI EPubStatus).
_PUBSTATUS_BY_CODE = {
    "3": "epublish",
    "4": "ppublish",
    "10": "aheadofprint",
}
_FINAL_PUBSTATUS = frozenset({"epublish", "ppublish"})
_AHEAD_PUBSTATUS = "aheadofprint"

_MONTH_TO_ABBR = {
    "1": "Jan",
    "01": "Jan",
    "jan": "Jan",
    "january": "Jan",
    "2": "Feb",
    "02": "Feb",
    "feb": "Feb",
    "february": "Feb",
    "3": "Mar",
    "03": "Mar",
    "mar": "Mar",
    "march": "Mar",
    "4": "Apr",
    "04": "Apr",
    "apr": "Apr",
    "april": "Apr",
    "5": "May",
    "05": "May",
    "may": "May",
    "6": "Jun",
    "06": "Jun",
    "jun": "Jun",
    "june": "Jun",
    "7": "Jul",
    "07": "Jul",
    "jul": "Jul",
    "july": "Jul",
    "8": "Aug",
    "08": "Aug",
    "aug": "Aug",
    "august": "Aug",
    "9": "Sep",
    "09": "Sep",
    "sep": "Sep",
    "sept": "Sep",
    "september": "Sep",
    "10": "Oct",
    "oct": "Oct",
    "october": "Oct",
    "11": "Nov",
    "nov": "Nov",
    "november": "Nov",
    "12": "Dec",
    "dec": "Dec",
    "december": "Dec",
}

_NAMED_MONTHS = {key: value for key, value in _MONTH_TO_ABBR.items() if not key.isdigit()}
_PUNCT_SPACE = re.compile(r"\s+([.,;:!?])")
_WHITESPACE = re.compile(r"\s+")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")

_RETRACTED_PUBTYPES = frozenset(
    {
        "retracted publication",
        "withdrawn publication",
    }
)
_CONCERN_PUBTYPES = frozenset({"expression of concern"})
_RETRACTED_REFTYPES = frozenset(
    {"retractionin", "partialretractionin", "retractedandrepublishedin"}
)
_CONCERN_REFTYPES = frozenset({"expressionofconcernin"})

_SKIP_PMC_SUBTREES = frozenset(
    {
        "fig",
        "fig-group",
        "table-wrap",
        "table-wrap-group",
        "table",
        "supplementary-material",
        "inline-supplementary-material",
        "floats-group",
        "boxed-text",
        "disp-formula",
        "disp-formula-group",
    }
)

_PUB_TYPE_RULES: tuple[tuple[str, PubTypeLabel], ...] = (
    ("meta-analysis", PubTypeLabel.META_ANALYSIS),
    ("systematic review", PubTypeLabel.SYSTEMATIC_REVIEW),
    ("randomized controlled trial", PubTypeLabel.RCT),
    ("review", PubTypeLabel.REVIEW),
)


def parse_esummary(payload: Mapping[str, Any]) -> tuple[ArticleListItem, ...]:
    """Карточки списка из JSON ESummary. Порядок как в result.uids."""
    result = payload.get("result")
    if not isinstance(result, dict):
        return ()
    uids = result.get("uids") or []
    items: list[ArticleListItem] = []
    for uid in uids:
        record = result.get(str(uid))
        if not isinstance(record, dict) or record.get("error"):
            continue
        pmid = str(record.get("uid") or uid).strip()
        title = str(record.get("title") or "").strip()
        if not pmid or not title:
            continue
        pubtypes = _as_str_tuple(record.get("pubtype"))
        items.append(
            ArticleListItem(
                pmid=pmid,
                title_en=title,
                date_label=_date_label_from_esummary(record),
                status_label=_status_label(_normalize_pubstatus(record.get("pubstatus"))),
                integrity_label=_integrity_label(pubtypes, (), title),
                pub_type_label=_pub_type_label(pubtypes),
            )
        )
    return tuple(items)


def has_nonempty_abstract(article: Article) -> bool:
    """True, если разобранный abstract содержит хотя бы одну непустую секцию."""
    return any(section.text.strip() for section in article.abstract)


def parse_pubmed_xml(xml: str) -> tuple[Article, ...]:
    """Статьи из EFetch db=pubmed. Пустой набор — пустой кортеж, не ошибка."""
    root = _parse_xml(xml)
    if root is None:
        return ()
    articles: list[Article] = []
    for node in _iter_local(root, "PubmedArticle"):
        parsed = _article_from_pubmed(node)
        if parsed is not None:
            articles.append(parsed)
    return tuple(articles)


def parse_pmc_xml(xml: str) -> tuple[str, ...]:
    """Текстовые параграфы PMC OA. Фигуры и таблицы отбрасываются. Нет body — пусто."""
    root = _parse_xml(xml)
    if root is None:
        return ()
    paragraphs: list[str] = []
    for body in _iter_local(root, "body"):
        paragraphs.extend(_paragraphs_from_body(body))
    return tuple(paragraphs)


def _article_from_pubmed(node: ET.Element) -> Article | None:
    pmid = _first_text(node, "PMID")
    title = _first_text(node, "ArticleTitle")
    if not pmid or not title:
        return None
    pubtypes = tuple(
        text for item in _iter_local(node, "PublicationType") if (text := _element_text(item))
    )
    reftypes = tuple(
        ref for item in _iter_local(node, "CommentsCorrections") if (ref := item.get("RefType"))
    )
    ids = _article_ids(node)
    return Article(
        pmid=pmid,
        title_en=title,
        authors=_authors(node),
        journal=(
            _first_text_under(node, "Title", under="Journal")
            or _first_text(node, "ISOAbbreviation")
        ),
        pmcid=_normalize_pmcid(ids.get("pmc") or ids.get("pmcid")),
        doi=_normalize_doi(ids.get("doi")),
        date_label=_date_label_from_pubmed(node),
        status_label=_status_label(_first_text(node, "PublicationStatus")),
        integrity_label=_integrity_label(pubtypes, reftypes, title),
        pub_type_label=_pub_type_label(pubtypes),
        abstract=_abstract_sections(node),
    )


def _date_label_from_esummary(record: Mapping[str, Any]) -> str | None:
    return _format_pubdate_string(str(record.get("pubdate") or ""))


def _date_label_from_pubmed(node: ET.Element) -> str | None:
    pub_date = _first_child_under(node, "PubDate", under="JournalIssue")
    if pub_date is not None:
        label = _format_date_parts(
            _direct_text(pub_date, "Year"),
            _direct_text(pub_date, "Month"),
            medline=_direct_text(pub_date, "MedlineDate"),
        )
        if label:
            return label
    article_date = _first_child(node, "ArticleDate")
    if article_date is not None:
        return _format_date_parts(
            _direct_text(article_date, "Year"),
            _direct_text(article_date, "Month"),
        )
    return None


def _format_pubdate_string(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    parts = text.replace(",", " ").split()
    year: str | None = None
    month: str | None = None
    for part in parts:
        if _YEAR.fullmatch(part):
            year = part
            continue
        abbr = _NAMED_MONTHS.get(part.lower())
        if abbr:
            month = abbr
    if year and month:
        return f"{month} {year}"
    if year:
        return year
    return None


def _format_date_parts(
    year: str | None,
    month: str | None,
    *,
    medline: str | None = None,
) -> str | None:
    if year and month:
        abbr = _MONTH_TO_ABBR.get(month.strip().lower())
        if abbr:
            return f"{abbr} {year.strip()}"
        if _YEAR.fullmatch(year.strip()):
            return year.strip()
    if year and _YEAR.fullmatch(year.strip()):
        return year.strip()
    if not medline:
        return None
    return _format_pubdate_string(medline)


def _normalize_pubstatus(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return _PUBSTATUS_BY_CODE.get(text, text)


def _status_label(pubstatus: str | None) -> PublicationStatusLabel | None:
    if not pubstatus:
        return None
    if pubstatus in _FINAL_PUBSTATUS:
        return PublicationStatusLabel.PUBLISHED
    if pubstatus == _AHEAD_PUBSTATUS:
        return PublicationStatusLabel.INCOMPLETE
    return None


def _integrity_from_pubtypes(pubtypes: Sequence[str]) -> IntegrityLabel | None:
    return _integrity_label(pubtypes, ())


def _title_is_withdrawn(title: str) -> bool:
    return title.lstrip().upper().startswith("WITHDRAWN:")


def _integrity_label(
    pubtypes: Sequence[str],
    reftypes: Sequence[str],
    title: str = "",
) -> IntegrityLabel | None:
    if _title_is_withdrawn(title):
        return IntegrityLabel.RETRACTED
    lowered_types = {item.strip().lower() for item in pubtypes}
    lowered_refs = {item.strip().lower() for item in reftypes}
    if lowered_types & _RETRACTED_PUBTYPES or lowered_refs & _RETRACTED_REFTYPES:
        return IntegrityLabel.RETRACTED
    if lowered_types & _CONCERN_PUBTYPES or lowered_refs & _CONCERN_REFTYPES:
        return IntegrityLabel.CONCERN
    return None


def _pub_type_label(pubtypes: Sequence[str]) -> PubTypeLabel | None:
    lowered = [item.strip().lower() for item in pubtypes]
    found: PubTypeLabel | None = None
    found_rank = len(_PUB_TYPE_RULES)
    for text in lowered:
        for rank, (needle, label) in enumerate(_PUB_TYPE_RULES):
            if needle == "review":
                matched = text == "review"
            else:
                matched = needle in text
            if matched and rank < found_rank:
                found = label
                found_rank = rank
    return found


def _abstract_sections(node: ET.Element) -> tuple[AbstractSection, ...]:
    abstract = None
    for candidate in _iter_local(node, "Abstract"):
        # MedlineCitation/Article/Abstract, не OtherAbstract.
        abstract = candidate
        break
    if abstract is None:
        return ()
    sections: list[AbstractSection] = []
    for item in abstract:
        if _local(item.tag) != "AbstractText":
            continue
        text = _element_text(item)
        if not text:
            continue
        label = _abstract_heading(item)
        sections.append(AbstractSection(text=text, label=label))
    if not sections:
        return ()
    if all(section.label is None for section in sections):
        return (AbstractSection(text="\n\n".join(section.text for section in sections)),)
    return tuple(sections)


def _abstract_heading(item: ET.Element) -> str | None:
    label = (item.get("Label") or "").strip()
    if label:
        return label
    category = (item.get("NlmCategory") or "").strip()
    if category and category.casefold() != "unassigned":
        return category
    return None


def _authors(node: ET.Element) -> tuple[str, ...]:
    names: list[str] = []
    for author in _iter_local(node, "Author"):
        collective = _direct_text(author, "CollectiveName")
        if collective:
            names.append(collective)
            continue
        last = _direct_text(author, "LastName")
        initials = _direct_text(author, "Initials")
        fore = _direct_text(author, "ForeName")
        if last and initials:
            names.append(f"{last} {initials}")
        elif last and fore:
            names.append(f"{last} {fore}")
        elif last:
            names.append(last)
    return tuple(names)


def _article_ids(node: ET.Element) -> dict[str, str]:
    ids: dict[str, str] = {}
    for item in _iter_local(node, "ArticleId"):
        id_type = (item.get("IdType") or "").strip().lower()
        value = _element_text(item)
        if id_type and value and id_type not in ids:
            ids[id_type] = value
    doi = _first_elocation_doi(node)
    if doi and "doi" not in ids:
        ids["doi"] = doi
    return ids


def _first_elocation_doi(node: ET.Element) -> str | None:
    for item in _iter_local(node, "ELocationID"):
        if (item.get("EIdType") or "").strip().lower() == "doi":
            text = _element_text(item)
            if text:
                return text
    return None


def _normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    lower = text.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if lower.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    return text or None


def _normalize_pmcid(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    core = text.split(".")[0]
    digits = core[3:] if core[:3].upper() == "PMC" else core
    if not digits.isdigit():
        return core
    return f"PMC{digits}"


def _paragraphs_from_body(body: ET.Element) -> list[str]:
    paragraphs: list[str] = []

    def walk(elem: ET.Element) -> None:
        if _local(elem.tag) in _SKIP_PMC_SUBTREES:
            return
        if _local(elem.tag) == "p":
            text = _element_text(elem)
            if text:
                paragraphs.append(text)
            return
        for child in elem:
            walk(child)

    walk(body)
    return paragraphs


def _parse_xml(xml: str) -> ET.Element | None:
    text = xml.strip()
    if not text:
        return None
    cleaned = _DOCTYPE.sub("", text, count=1)
    try:
        return ET.fromstring(cleaned)
    except ET.ParseError:
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _iter_local(root: ET.Element, name: str) -> Iterable[ET.Element]:
    for elem in root.iter():
        if _local(elem.tag) == name:
            yield elem


def _first_child(root: ET.Element, name: str) -> ET.Element | None:
    for elem in root.iter():
        if _local(elem.tag) == name:
            return elem
    return None


def _first_child_under(root: ET.Element, name: str, *, under: str) -> ET.Element | None:
    for parent in _iter_local(root, under):
        for elem in parent.iter():
            if elem is parent:
                continue
            if _local(elem.tag) == name:
                return elem
    return None


def _first_text(root: ET.Element, name: str) -> str | None:
    node = _first_child(root, name)
    if node is None:
        return None
    text = _element_text(node)
    return text or None


def _first_text_under(root: ET.Element, name: str, *, under: str) -> str | None:
    node = _first_child_under(root, name, under=under)
    if node is None:
        return None
    text = _element_text(node)
    return text or None


def _direct_text(parent: ET.Element, name: str) -> str | None:
    for child in parent:
        if _local(child.tag) == name:
            text = _element_text(child)
            return text or None
    return None


def _element_text(elem: ET.Element) -> str:
    collapsed = _WHITESPACE.sub(" ", "".join(elem.itertext())).strip()
    return _PUNCT_SPACE.sub(r"\1", collapsed)


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value if str(item).strip())
    return ()
