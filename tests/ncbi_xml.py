"""Минимальный Medline XML для фейкового EFetch. Без сети."""

from collections.abc import Collection, Sequence


def pubmed_articles_xml(
    pmids: Sequence[str],
    *,
    empty_abstract: Collection[str] = (),
) -> str:
    """Собрать PubmedArticleSet: у pmid из empty_abstract нет AbstractText."""
    skipped = frozenset(empty_abstract)
    parts: list[str] = ["<PubmedArticleSet>"]
    for pmid in pmids:
        if pmid in skipped:
            abstract = ""
        else:
            abstract = f"<Abstract><AbstractText>Abstract {pmid}</AbstractText></Abstract>"
        parts.append(
            "<PubmedArticle><MedlineCitation>"
            f"<PMID>{pmid}</PMID>"
            f"<Article><ArticleTitle>Title {pmid}</ArticleTitle>{abstract}</Article>"
            "</MedlineCitation></PubmedArticle>"
        )
    parts.append("</PubmedArticleSet>")
    return "".join(parts)
