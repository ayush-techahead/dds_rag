from app.modules.ingestion.chunking import (
    MarkdownHeaderChunker,
    ParagraphWindowChunker,
    RecursiveTextChunker,
    SectionAwareChunker,
    extract_faq_pairs,
    resolve_chunker_for_ingestion,
)


def test_recursive_chunker_preserves_paragraphs() -> None:
    chunker = RecursiveTextChunker(chunk_size=80, overlap=10)
    text = "First paragraph has useful context.\n\nSecond paragraph has different context."

    chunks = chunker.split(text)

    assert len(chunks) == 1
    assert "First paragraph" in chunks[0].text
    assert "Second paragraph" in chunks[0].text


def test_markdown_header_chunker_splits_on_headings() -> None:
    chunker = MarkdownHeaderChunker(chunk_size=200, overlap=20)
    text = "# Intro\nSome intro text.\n\n## Setup\nSetup text.\n\n## Usage\nUsage text."

    chunks = chunker.split(text)

    assert len(chunks) == 3
    assert chunks[0].section_title == "Intro"
    assert chunks[1].section_title == "Setup"
    assert chunks[2].section_title == "Usage"
    assert chunks[0].text.startswith("# Intro")


def test_section_aware_chunker_detects_plain_text_headings() -> None:
    chunker = SectionAwareChunker(chunk_size=200, overlap=20)
    text = "INSTALLATION GUIDE\n\nInstall the app.\n\nUSAGE NOTES\n\nRun the app."

    chunks = chunker.split(text)

    assert len(chunks) == 2
    assert chunks[0].section_title == "INSTALLATION GUIDE"
    assert chunks[1].section_title == "USAGE NOTES"


def test_paragraph_window_chunker_ignores_all_caps_headings() -> None:
    chunker = ParagraphWindowChunker(chunk_size=200, overlap=20)
    text = "INSTALLATION GUIDE\n\nInstall the app.\n\nUSAGE NOTES\n\nRun the app."

    chunks = chunker.split(text)

    assert len(chunks) == 1
    assert chunks[0].section_title is None
    assert "INSTALLATION GUIDE" in chunks[0].text
    assert "USAGE NOTES" in chunks[0].text


def test_extract_faq_pairs_two_blocks() -> None:
    text = (
        "Intro line\n\n"
        "Q: First question?\n"
        "A: First answer.\n\n"
        "Q: Second question?\n"
        "A: Second answer."
    )
    pairs = extract_faq_pairs(text)
    assert len(pairs) == 2
    assert pairs[0][0] == "First question?"
    assert pairs[0][1] == "First answer."
    assert pairs[1][0] == "Second question?"


def test_resolve_auto_prefers_faq_when_two_pairs_even_with_headings() -> None:
    text = (
        "# FAQ Page\n\n"
        "Q: One?\n"
        "A: A1.\n\n"
        "Q: Two?\n"
        "A: A2.\n"
    )
    chunker = resolve_chunker_for_ingestion(text, "markdown")
    chunks = chunker.split(text)
    assert len(chunks) == 2
    assert "Q: One?" in chunks[0].text or chunks[0].text.startswith("Q:")
    assert "Two?" in chunks[1].text


def test_resolve_auto_markdown_headings_without_faq() -> None:
    text = "# Intro\n\nIntro body.\n\n## Details\n\nMore here."
    chunker = resolve_chunker_for_ingestion(text, "markdown")
    chunks = chunker.split(text)
    titles = {c.section_title for c in chunks}
    assert "Intro" in titles
    assert "Details" in titles


def test_resolve_auto_plain_text_no_headings_no_faq() -> None:
    text = "First paragraph here.\n\nSecond paragraph continues the thought."
    chunker = resolve_chunker_for_ingestion(text, "markdown")
    assert isinstance(chunker, ParagraphWindowChunker)
    chunks = chunker.split(text)
    assert len(chunks) >= 1
    assert all(c.section_title is None for c in chunks)
