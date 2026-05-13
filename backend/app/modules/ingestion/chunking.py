import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.core.config import settings


@dataclass(frozen=True)
class IngestionChunk:
    text: str
    section_title: str | None = None
    section_path: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DocumentSection:
    title: str | None
    body: str
    path: list[str] = field(default_factory=list)


class ChunkingStrategy(ABC):
    @abstractmethod
    def split(self, text: str) -> list[IngestionChunk]:
        pass


class SectionAwareChunker(ChunkingStrategy):
    """Prefer semantic sections, then paragraphs/sentences, and use size only as fallback."""

    heading_pattern = re.compile(
        r"^(?P<heading>(?:#{1,6}\s+.+)|(?:[A-Z][A-Z0-9 ,:;()'/-]{6,}))$"
    )

    def __init__(self, chunk_size: int | None = None, overlap: int | None = None) -> None:
        self.chunk_size = chunk_size or settings.DOCUMENT_CHUNK_SIZE
        self.overlap = overlap or settings.DOCUMENT_CHUNK_OVERLAP
        if self.overlap >= self.chunk_size:
            msg = "Document chunk overlap must be smaller than chunk size"
            raise ValueError(msg)

    def split(self, text: str) -> list[IngestionChunk]:
        sections = self._split_sections(text)
        chunks: list[IngestionChunk] = []
        for section in sections:
            chunks.extend(self._chunk_section(section))
        return chunks

    def _split_sections(self, text: str) -> list[DocumentSection]:
        normalized = self._normalize_lines(text)
        if not normalized:
            return []

        lines = normalized.splitlines()
        sections: list[DocumentSection] = []
        current_title: str | None = None
        current_path: list[str] = []
        current_body: list[str] = []

        for line in lines:
            heading = self._parse_heading(line)
            if heading:
                if current_body or current_title:
                    sections.append(
                        DocumentSection(
                            title=current_title,
                            body="\n".join(current_body).strip(),
                            path=current_path,
                        )
                    )
                current_title = heading
                current_path = [heading]
                current_body = [line]
            else:
                current_body.append(line)

        if current_body or current_title:
            sections.append(
                DocumentSection(
                    title=current_title,
                    body="\n".join(current_body).strip(),
                    path=current_path,
                )
            )

        if len(sections) == 1 and sections[0].title is None:
            return self._paragraph_sections(sections[0].body)
        return [section for section in sections if section.body]

    def _paragraph_sections(self, text: str) -> list[DocumentSection]:
        paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text)]
        sections: list[DocumentSection] = []
        current: list[str] = []

        for paragraph in paragraphs:
            if not paragraph:
                continue
            candidate = "\n\n".join([*current, paragraph]).strip()
            if len(candidate) <= self.chunk_size * 2:
                current.append(paragraph)
                continue
            if current:
                sections.append(DocumentSection(title=None, body="\n\n".join(current)))
            current = [paragraph]

        if current:
            sections.append(DocumentSection(title=None, body="\n\n".join(current)))
        return sections

    def _chunk_section(self, section: DocumentSection) -> list[IngestionChunk]:
        if len(section.body) <= self.chunk_size:
            return [
                IngestionChunk(
                    text=section.body,
                    section_title=section.title,
                    section_path=section.path,
                )
            ]

        chunks: list[IngestionChunk] = []
        current = ""
        for part in self._split_by_boundaries(section.body):
            if len(part) > self.chunk_size:
                if current:
                    chunks.append(self._make_chunk(current, section))
                    current = ""
                chunks.extend(self._fixed_window_split(part, section))
                continue

            candidate = f"{current}\n\n{part}".strip() if current else part
            if len(candidate) <= self.chunk_size:
                current = candidate
            else:
                if current:
                    chunks.append(self._make_chunk(current, section))
                current = self._with_overlap(chunks[-1].text, part) if chunks else part

        if current.strip():
            chunks.append(self._make_chunk(current, section))
        return chunks

    def _make_chunk(self, text: str, section: DocumentSection) -> IngestionChunk:
        return IngestionChunk(
            text=text.strip(),
            section_title=section.title,
            section_path=section.path,
        )

    def _normalize_lines(self, text: str) -> str:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(lines).strip()

    def _parse_heading(self, line: str) -> str | None:
        markdown_heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if markdown_heading:
            return markdown_heading.group(2).strip()

        if self.heading_pattern.match(line) and len(line.split()) <= 14:
            return line.strip()
        return None

    def _split_by_boundaries(self, text: str) -> list[str]:
        paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text)]
        parts: list[str] = []
        for paragraph in paragraphs:
            if len(paragraph) <= self.chunk_size:
                parts.append(paragraph)
                continue
            sentences = re.split(r"(?<=[.!?])\s+", paragraph)
            parts.extend(sentence.strip() for sentence in sentences if sentence.strip())
        return [part for part in parts if part]

    def _fixed_window_split(
        self,
        text: str,
        section: DocumentSection,
    ) -> list[IngestionChunk]:
        chunks: list[IngestionChunk] = []
        start = 0
        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            chunk_text = text[start:end].strip()
            if chunk_text:
                chunks.append(self._make_chunk(chunk_text, section))
            if end == len(text):
                break
            start = max(end - self.overlap, 0)
        return chunks

    def _with_overlap(self, previous_chunk: str, next_text: str) -> str:
        overlap_text = previous_chunk[-self.overlap :].strip()
        candidate = f"{overlap_text}\n\n{next_text}".strip()
        if len(candidate) <= self.chunk_size:
            return candidate
        return next_text


class MarkdownHeaderChunker(SectionAwareChunker):
    """Explicit name retained for config compatibility."""


class MarkdownHeadingOnlyChunker(SectionAwareChunker):
    """Split only on Markdown ATX headings (# ..); body split by paragraphs then size windows."""

    def _parse_heading(self, line: str) -> str | None:
        markdown_heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if markdown_heading:
            return markdown_heading.group(2).strip()
        return None


class ParagraphWindowChunker(SectionAwareChunker):
    """No heading detection: paragraphs first, then character windows with overlap."""

    def split(self, text: str) -> list[IngestionChunk]:
        normalized = self._normalize_lines(text)
        if not normalized:
            return []
        sections = self._paragraph_sections(normalized)
        chunks: list[IngestionChunk] = []
        for section in sections:
            chunks.extend(self._chunk_section(section))
        return chunks


_MD_HEADING_LINE = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)


def text_has_markdown_headings(text: str) -> bool:
    return bool(_MD_HEADING_LINE.search(text))


def _is_faq_q_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if re.match(r"^Q\s*[:.)]\s*\S", s, re.IGNORECASE):
        return True
    if re.match(r"^Question:\s*\S", s, re.IGNORECASE):
        return True
    if re.match(r"^\*{0,2}Q\*{0,2}\s*[:.)]\s*\S", s, re.IGNORECASE):
        return True
    if re.match(r"^\d+\.\s*Q\s*[:.)]\s*\S", s, re.IGNORECASE):
        return True
    return False


def _is_faq_a_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if re.match(r"^A\s*[:.)]\s*\S", s, re.IGNORECASE):
        return True
    if re.match(r"^Answer:\s*\S", s, re.IGNORECASE):
        return True
    if re.match(r"^\*{0,2}A\*{0,2}\s*[:.)]\s*\S", s, re.IGNORECASE):
        return True
    if re.match(r"^\d+\.\s*A\s*[:.)]\s*\S", s, re.IGNORECASE):
        return True
    return False


def _strip_faq_q_line(line: str) -> str:
    s = line.strip()
    m = re.match(r"^Q\s*[:.)]\s*(.*)$", s, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.match(r"^Question:\s*(.*)$", s, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.match(r"^\*{0,2}Q\*{0,2}\s*[:.)]\s*(.*)$", s, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.match(r"^\d+\.\s*Q\s*[:.)]\s*(.*)$", s, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return s


def _strip_faq_a_line(line: str) -> str:
    s = line.strip()
    m = re.match(r"^A\s*[:.)]\s*(.*)$", s, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.match(r"^Answer:\s*(.*)$", s, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.match(r"^\*{0,2}A\*{0,2}\s*[:.)]\s*(.*)$", s, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.match(r"^\d+\.\s*A\s*[:.)]\s*(.*)$", s, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return s


def extract_faq_pairs(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    n = len(lines)
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < n:
        if not _is_faq_q_line(lines[i]):
            i += 1
            continue
        q_parts = [_strip_faq_q_line(lines[i])]
        i += 1
        while i < n and not _is_faq_q_line(lines[i]) and not _is_faq_a_line(lines[i]):
            q_parts.append(lines[i])
            i += 1
        if i >= n or not _is_faq_a_line(lines[i]):
            continue
        a_parts = [_strip_faq_a_line(lines[i])]
        i += 1
        while i < n and not _is_faq_q_line(lines[i]):
            a_parts.append(lines[i])
            i += 1
        q = "\n".join(q_parts).strip()
        a = "\n".join(a_parts).strip()
        if q and a:
            pairs.append((q, a))
    return pairs


def is_faq_like_text(text: str) -> bool:
    """At least two Q/A blocks with explicit Q and A line markers."""
    return len(extract_faq_pairs(text)) >= 2


class FAQChunker(SectionAwareChunker):
    """One chunk per question/answer pair; long pairs are windowed like other chunkers."""

    def split(self, text: str) -> list[IngestionChunk]:
        pairs = extract_faq_pairs(text)
        if not pairs:
            return ParagraphWindowChunker(
                chunk_size=self.chunk_size,
                overlap=self.overlap,
            ).split(text)

        chunks: list[IngestionChunk] = []
        for question, answer in pairs:
            body = f"Q: {question}\n\nA: {answer}".strip()
            title = (question.splitlines()[0] if question else None)[:200] or None
            section = DocumentSection(title=title, body=body, path=[])
            chunks.extend(self._chunk_section(section))
        return chunks


RecursiveTextChunker = SectionAwareChunker


def get_chunker(document_type: str | None = None) -> ChunkingStrategy:
    strategy = settings.DOCUMENT_CHUNK_STRATEGY.lower()
    if strategy in ("auto", "content_aware"):
        msg = (
            "DOCUMENT_CHUNK_STRATEGY is 'auto'; use resolve_chunker_for_ingestion(text, "
            "document_type) with extracted document text."
        )
        raise ValueError(msg)
    if strategy in {"section", "sections", "section_aware"}:
        return SectionAwareChunker()
    if strategy == "markdown_headers" or document_type == "markdown":
        return MarkdownHeaderChunker()
    if strategy == "recursive":
        return SectionAwareChunker()

    msg = f"Unsupported document chunk strategy: {settings.DOCUMENT_CHUNK_STRATEGY}"
    raise ValueError(msg)


def resolve_chunker_for_ingestion(text: str, document_type: str | None = None) -> ChunkingStrategy:
    """Upload pipeline: FAQ-like → Markdown # headings → paragraph + size windows."""
    strategy = settings.DOCUMENT_CHUNK_STRATEGY.lower()
    if strategy not in ("auto", "content_aware"):
        return get_chunker(document_type)

    if is_faq_like_text(text):
        return FAQChunker()
    if text_has_markdown_headings(text):
        return MarkdownHeadingOnlyChunker()
    return ParagraphWindowChunker()
