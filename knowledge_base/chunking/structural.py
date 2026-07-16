from typing import Dict, List

from ..parsers import ParsedBlock


def _split_long(text: str, size: int, overlap: int):
    if len(text) <= size:
        return [text]
    parts, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind("。", start, end), text.rfind(". ", start, end))
            if boundary > start + size // 2:
                end = boundary + 1
        parts.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return [part for part in parts if part]


def chunk_blocks(blocks: List[ParsedBlock], size: int = 1800, overlap: int = 220) -> List[Dict]:
    chunks, buffer, section, page = [], [], "", None

    def flush():
        nonlocal buffer
        text = "\n\n".join(buffer).strip()
        if text:
            for part in _split_long(text, size, overlap):
                chunks.append({"text": part, "section": section, "page": page})
        buffer = []

    for block in blocks:
        if buffer and (block.section != section or block.page != page or sum(len(item) for item in buffer) + len(block.text) > size):
            flush()
        section, page = block.section or section, block.page
        buffer.append(block.text)
    flush()
    return chunks
