from pathlib import Path

from pypdf import PdfReader

from app.core.exceptions import BadRequestException


class PdfTextExtractor:
    def extract_text(self, file_path: Path) -> str:
        try:
            reader = PdfReader(str(file_path))
        except Exception as exc:
            raise BadRequestException("Uploaded file is not a readable PDF") from exc

        page_texts: list[str] = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                page_texts.append(page_text.strip())

        text = "\n\n".join(page_texts).strip()
        if not text:
            raise BadRequestException("PDF does not contain extractable text")
        return text
