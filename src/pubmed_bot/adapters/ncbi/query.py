"""Сборка term для NCBI ESearch. HTML PubMed не используется."""

ENGLISH_FILTER = "english[lang]"
HASABSTRACT_FILTER = "hasabstract"
PAGE_SIZE = 10
MAX_SEARCH_WINDOWS = 3
NON_RESEARCH_PT = (
    'letter[pt] OR editorial[pt] OR news[pt] OR comment[pt] OR "newspaper article"[pt]'
)


def retstart_for_page(page: int) -> int:
    """Смещение ESearch: retstart = (page - 1) * 10."""
    if page < 1:
        msg = "page должен быть >= 1"
        raise ValueError(msg)
    return (page - 1) * PAGE_SIZE


def build_term(user_text: str) -> str:
    """Английский query AND язык AND abstract, затем NOT письма/editorial/news."""
    query = user_text.strip()
    if not query:
        msg = "текст запроса не должен быть пустым"
        raise ValueError(msg)
    return f"({query}) AND {ENGLISH_FILTER} AND {HASABSTRACT_FILTER} NOT ({NON_RESEARCH_PT})"
