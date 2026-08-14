"""Доменные модели статьи. Без Telegram и SQL."""

from dataclasses import dataclass

from pubmed_bot.domain.enums import IntegrityLabel, PublicationStatusLabel, PubTypeLabel


@dataclass(frozen=True, slots=True)
class AbstractSection:
    """Фрагмент abstract: именованный раздел или сплошной блок без label."""

    text: str
    label: str | None = None


@dataclass(frozen=True, slots=True)
class ArticleListItem:
    """Карточка строки списка: заголовок и короткие метаданные."""

    pmid: str
    title_en: str
    title_ru: str | None = None
    date_label: str | None = None
    status_label: PublicationStatusLabel | None = None
    integrity_label: IntegrityLabel | None = None
    pub_type_label: PubTypeLabel | None = None
    viewed: bool = False


@dataclass(frozen=True, slots=True)
class NoteRecord:
    """Строка notes: тело и снимок заголовков."""

    pmid: str
    body: str
    title_en: str | None
    title_ru: str | None


@dataclass(frozen=True, slots=True)
class NoteListItem:
    """Строка списка заметок: заголовок и превью тела."""

    pmid: str
    title: str
    preview: str


@dataclass(frozen=True, slots=True)
class Article:
    """Статья после выбора: идентификаторы, авторы, abstract, опционально PMC."""

    pmid: str
    title_en: str
    authors: tuple[str, ...] = ()
    journal: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    date_label: str | None = None
    status_label: PublicationStatusLabel | None = None
    integrity_label: IntegrityLabel | None = None
    pub_type_label: PubTypeLabel | None = None
    abstract: tuple[AbstractSection, ...] = ()
    fulltext_paragraphs: tuple[str, ...] = ()
