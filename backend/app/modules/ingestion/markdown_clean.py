import re


def clean_markdown_for_ingestion(raw: str) -> str:
    """Normalize and lightly clean Markdown before chunking (ZIP / bulk paths)."""
    text = raw.replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    text = "\n".join(lines).strip()
    return text
