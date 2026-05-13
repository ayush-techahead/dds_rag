from pathlib import Path

from app.core.exceptions import BadRequestException


class MarkdownTextExtractor:
    def extract_text(self, file_path: Path) -> str:
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise BadRequestException("Markdown file must be UTF-8 encoded") from exc
        except OSError as exc:
            raise BadRequestException("Markdown file could not be read") from exc

        text = text.strip()
        if not text:
            raise BadRequestException("Markdown file is empty")
        return text
