"""Разбор ESummary/EFetch: дата, статус, целостность, abstract, PMC. Без сети."""

import json
from pathlib import Path

from pubmed_bot.adapters.ncbi.parse import (
    has_nonempty_abstract,
    parse_esummary,
    parse_pmc_xml,
    parse_pubmed_xml,
)
from pubmed_bot.domain.enums import IntegrityLabel, PublicationStatusLabel, PubTypeLabel
from pubmed_bot.domain.models import AbstractSection

FIXTURES = Path(__file__).parent / "fixtures" / "ncbi"


def _json(name: str) -> dict[str, object]:
    loaded = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        msg = "фикстура ESummary должна быть JSON-объектом"
        raise TypeError(msg)
    return {str(key): value for key, value in loaded.items()}


def _xml(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_esummary_ahead_of_print_date_and_status() -> None:
    items = parse_esummary(_json("esummary_ahead.json"))
    assert len(items) == 1
    item = items[0]
    assert item.pmid == "1001"
    assert item.date_label == "Mar 2024"
    assert item.status_label is PublicationStatusLabel.INCOMPLETE
    assert item.integrity_label is None
    assert item.pub_type_label is None


def test_esummary_retracted() -> None:
    item = parse_esummary(_json("esummary_retracted.json"))[0]
    assert item.integrity_label is IntegrityLabel.RETRACTED
    assert item.status_label is PublicationStatusLabel.PUBLISHED
    assert item.date_label == "Jan 2023"


def test_esummary_rct_year_only() -> None:
    item = parse_esummary(_json("esummary_rct.json"))[0]
    assert item.pub_type_label is PubTypeLabel.RCT
    assert item.date_label == "2024"
    assert item.status_label is PublicationStatusLabel.PUBLISHED


def test_esummary_no_date_is_absent_not_placeholder() -> None:
    item = parse_esummary(_json("esummary_no_date.json"))[0]
    assert item.date_label is None
    assert item.status_label is None
    assert "н/д" not in (item.date_label or "")


def test_esummary_follows_uids_and_skips_errors() -> None:
    payload = {
        "result": {
            "uids": ["1", "2"],
            "1": {"error": "cannot get document summary"},
            "2": {
                "uid": "2",
                "title": "Ok",
                "pubdate": "2022 May 1",
                "pubstatus": "4",
                "pubtype": ["Meta-Analysis", "Review"],
            },
        }
    }
    items = parse_esummary(payload)
    assert [item.pmid for item in items] == ["2"]
    assert items[0].pub_type_label is PubTypeLabel.META_ANALYSIS
    assert items[0].date_label == "May 2022"


def test_esummary_expression_of_concern() -> None:
    payload = {
        "result": {
            "uids": ["7"],
            "7": {
                "uid": "7",
                "title": "Concerned paper",
                "pubdate": "2021",
                "pubstatus": "4",
                "pubtype": ["Expression of Concern", "Journal Article"],
            },
        }
    }
    item = parse_esummary(payload)[0]
    assert item.integrity_label is IntegrityLabel.CONCERN


def test_pubmed_ahead_structured_abstract_and_ids() -> None:
    articles = parse_pubmed_xml(_xml("pubmed_ahead.xml"))
    assert len(articles) == 1
    article = articles[0]
    assert article.pmid == "2001"
    assert article.date_label == "Mar 2024"
    assert article.status_label is PublicationStatusLabel.INCOMPLETE
    assert article.journal == "Sports Medicine"
    assert article.authors == ("Smith J", "Doe J")
    assert article.doi == "10.1000/ahead.1"
    assert article.pmcid is None
    assert article.abstract == (
        AbstractSection(text="Training load is debated.", label="BACKGROUND"),
        AbstractSection(text="We randomized 40 athletes.", label="METHODS"),
    )
    assert article.fulltext_paragraphs == ()


def test_pubmed_retracted_doi_pmcid_and_unstructured_abstract() -> None:
    article = parse_pubmed_xml(_xml("pubmed_retracted.xml"))[0]
    assert article.integrity_label is IntegrityLabel.RETRACTED
    assert article.status_label is PublicationStatusLabel.PUBLISHED
    assert article.date_label == "Jan 2023"
    assert article.doi == "10.1000/retracted"
    assert article.pmcid == "PMC555"
    assert article.abstract == (AbstractSection(text="This paper was later retracted."),)
    assert has_nonempty_abstract(article) is True
    assert article.authors == ("Rogue A",)


def test_pubmed_rct_no_abstract_prefers_rct_over_review() -> None:
    article = parse_pubmed_xml(_xml("pubmed_rct_no_abstract.xml"))[0]
    assert article.abstract == ()
    assert has_nonempty_abstract(article) is False
    assert article.pub_type_label is PubTypeLabel.RCT
    assert article.status_label is PublicationStatusLabel.PUBLISHED
    assert article.date_label == "2024"
    assert article.authors == ("SPORTS Group",)
    assert article.doi == "10.1136/rct.1"


def test_whitespace_only_abstract_is_empty() -> None:
    xml = (
        "<PubmedArticleSet><PubmedArticle><MedlineCitation>"
        "<PMID>9</PMID><Article><ArticleTitle>T</ArticleTitle>"
        "<Abstract><AbstractText>   </AbstractText></Abstract>"
        "</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
    )
    article = parse_pubmed_xml(xml)[0]
    assert article.abstract == ()
    assert has_nonempty_abstract(article) is False


def test_nlm_category_heading_without_label() -> None:
    xml = (
        "<PubmedArticleSet><PubmedArticle><MedlineCitation>"
        "<PMID>11</PMID><Article><ArticleTitle>T</ArticleTitle>"
        "<Abstract>"
        '<AbstractText NlmCategory="RESULTS">Found an effect.</AbstractText>'
        "</Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
    )
    article = parse_pubmed_xml(xml)[0]
    assert article.abstract == (AbstractSection(text="Found an effect.", label="RESULTS"),)


def test_unassigned_nlm_category_is_not_heading() -> None:
    xml = (
        "<PubmedArticleSet><PubmedArticle><MedlineCitation>"
        "<PMID>12</PMID><Article><ArticleTitle>T</ArticleTitle>"
        "<Abstract>"
        '<AbstractText NlmCategory="UNASSIGNED">Plain text.</AbstractText>'
        "</Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
    )
    article = parse_pubmed_xml(xml)[0]
    assert article.abstract == (AbstractSection(text="Plain text."),)


def test_pubmed_no_date_and_hidden_status() -> None:
    article = parse_pubmed_xml(_xml("pubmed_no_date.xml"))[0]
    assert article.date_label is None
    assert article.status_label is None
    assert article.pub_type_label is None
    assert article.abstract == (AbstractSection(text="Plain unstructured abstract."),)


def test_pmc_drops_figures_and_tables() -> None:
    paragraphs = parse_pmc_xml(_xml("pmc_with_fig_table.xml"))
    assert paragraphs == (
        "First paragraph with emphasis and a citation 1.",
        "Second paragraph after the figure.",
        "Third paragraph after the table.",
    )
    joined = " ".join(paragraphs)
    assert "Figure caption" not in joined
    assert "Table caption" not in joined
    assert "cell" not in joined


def test_missing_oa_and_empty_xml_are_not_errors() -> None:
    assert parse_pmc_xml("") == ()
    assert parse_pmc_xml("<pmc-articleset></pmc-articleset>") == ()
    assert parse_pubmed_xml("") == ()
    assert parse_pubmed_xml("<PubmedArticleSet></PubmedArticleSet>") == ()
    assert parse_esummary({}) == ()


def test_systematic_review_not_generic_review() -> None:
    payload = {
        "result": {
            "uids": ["8"],
            "8": {
                "uid": "8",
                "title": "A systematic review",
                "pubdate": "2020",
                "pubstatus": "4",
                "pubtype": ["Systematic Review", "Review"],
            },
        }
    }
    assert parse_esummary(payload)[0].pub_type_label is PubTypeLabel.SYSTEMATIC_REVIEW


def test_retracted_and_republished_in_xml() -> None:
    xml = """
    <PubmedArticleSet>
      <PubmedArticle>
        <MedlineCitation>
          <PMID>3001</PMID>
          <Article>
            <Journal><Title>J</Title>
              <JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue>
            </Journal>
            <ArticleTitle>Republished paper</ArticleTitle>
            <CommentsCorrectionsList>
              <CommentsCorrections RefType="RetractedandRepublishedIn"/>
            </CommentsCorrectionsList>
          </Article>
        </MedlineCitation>
        <PubmedData><PublicationStatus>ppublish</PublicationStatus></PubmedData>
      </PubmedArticle>
    </PubmedArticleSet>
    """
    article = parse_pubmed_xml(xml)[0]
    assert article.integrity_label is IntegrityLabel.RETRACTED


def test_withdrawn_prefix_in_title() -> None:
    payload = {
        "result": {
            "uids": ["9"],
            "9": {
                "uid": "9",
                "title": "WITHDRAWN: Old protocol",
                "pubdate": "2019",
                "pubstatus": "4",
                "pubtype": ["Journal Article"],
            },
        }
    }
    assert parse_esummary(payload)[0].integrity_label is IntegrityLabel.RETRACTED


def test_withdrawn_prefix_in_pubmed_xml() -> None:
    xml = """
    <PubmedArticleSet>
      <PubmedArticle>
        <MedlineCitation>
          <PMID>3002</PMID>
          <Article>
            <Journal><Title>J</Title>
              <JournalIssue><PubDate><Year>2019</Year></PubDate></JournalIssue>
            </Journal>
            <ArticleTitle>WITHDRAWN: Old protocol</ArticleTitle>
          </Article>
        </MedlineCitation>
        <PubmedData><PublicationStatus>ppublish</PublicationStatus></PubmedData>
      </PubmedArticle>
    </PubmedArticleSet>
    """
    assert parse_pubmed_xml(xml)[0].integrity_label is IntegrityLabel.RETRACTED
