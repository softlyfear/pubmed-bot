"""Доменные исключения. Без Telegram и SQL."""


class PubmedUnavailable(Exception):
    """NCBI E-utilities недоступен после повторов (429/5xx)."""


class TranslationUnavailable(Exception):
    """DeepL недоступен, квота исчерпана или ниже порога для fulltext."""


class QueryRewriteUnavailable(Exception):
    """GPT-rewriter поискового term недоступен (сеть, timeout, пустой выход)."""


class NoteRejected(Exception):
    """Пустое тело заметки или длиннее лимита; в БД не пишем."""
