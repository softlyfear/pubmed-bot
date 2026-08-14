"""Доменные перечисления pubmed-bot."""

from enum import StrEnum


class SearchSort(StrEnum):
    """Сортировка ESearch: relevance = NCBI Best Match."""

    RELEVANCE = "relevance"
    PUB_DATE = "pub_date"


class PublicationStatusLabel(StrEnum):
    """Статус «завершена ли» для списка. Иное pubstatus скрываем."""

    PUBLISHED = "опубл."
    INCOMPLETE = "не завершена"


class IntegrityLabel(StrEnum):
    """Флаг целостности статьи. Важнее типа публикации."""

    RETRACTED = "отозвана"
    CONCERN = "замечание"


class PubTypeLabel(StrEnum):
    """Короткие RU-лейблы типа публикации из PROJECT.md."""

    META_ANALYSIS = "мета-анализ"
    SYSTEMATIC_REVIEW = "сист. обзор"
    RCT = "RCT"
    REVIEW = "обзор"


class TranslationKind(StrEnum):
    """Вид текста в кэше перевода."""

    TITLE = "title"
    ABSTRACT = "abstract"
    FULLTEXT = "fulltext"
