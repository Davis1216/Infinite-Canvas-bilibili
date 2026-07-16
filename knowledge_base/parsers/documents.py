import csv
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from xml.etree import ElementTree

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


@dataclass
class ParsedBlock:
    text: str
    section: str = ""
    page: Optional[int] = None
    kind: str = "paragraph"


TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".json", ".csv", ".log", ".py", ".js", ".ts",
                   ".tsx", ".jsx", ".html", ".css", ".xml", ".yaml", ".yml"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".docx", ".xlsx"}


def _paragraph_blocks(text: str, page=None) -> List[ParsedBlock]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks, section = [], ""
    for raw in re.split(r"\n\s*\n", text):
        value = raw.strip()
        if not value:
            continue
        first = value.splitlines()[0].strip()
        if re.match(r"^#{1,6}\s+", first):
            section = re.sub(r"^#{1,6}\s+", "", first).strip()
        blocks.append(ParsedBlock(value, section=section, page=page,
                                  kind="heading" if first == value and section == first.lstrip("# ") else "paragraph"))
    return blocks


def _read_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs = []
    for paragraph in root.iter():
        if paragraph.tag.endswith("}p"):
            text = "".join(node.text or "" for node in paragraph.iter() if node.tag.endswith("}t"))
            if text.strip():
                paragraphs.append(text.strip())
    return "\n\n".join(paragraphs)


def _read_xlsx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root:
                shared.append("".join(node.text or "" for node in item.iter() if node.tag.endswith("}t")))
        rows = []
        sheets = sorted(name for name in archive.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
        for sheet_index, name in enumerate(sheets, 1):
            root = ElementTree.fromstring(archive.read(name))
            rows.append(f"## 工作表 {sheet_index}")
            for row in root.iter():
                if not row.tag.endswith("}row"):
                    continue
                values = []
                for cell in list(row):
                    if not cell.tag.endswith("}c"):
                        continue
                    cell_type = cell.attrib.get("t")
                    value_node = next((node for node in cell.iter() if node.tag.endswith("}v")), None)
                    value = value_node.text if value_node is not None else ""
                    if cell_type == "s" and str(value).isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    values.append(str(value or ""))
                if any(values):
                    rows.append("\t".join(values))
    return "\n".join(rows)


def parse_document(path: str, original_name: str = "") -> List[ParsedBlock]:
    source = Path(path)
    extension = Path(original_name or source.name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError("不支持该文件格式，请使用 PDF、DOCX、XLSX 或文本格式")
    if extension == ".pdf":
        if PdfReader is None:
            raise RuntimeError("当前环境未安装 PDF 文本解析组件")
        blocks = []
        reader = PdfReader(str(source))
        for index, page in enumerate(reader.pages, 1):
            blocks.extend(_paragraph_blocks(page.extract_text() or "", page=index))
        return blocks
    if extension == ".docx":
        return _paragraph_blocks(_read_docx(source))
    if extension == ".xlsx":
        return _paragraph_blocks(_read_xlsx(source))
    text = source.read_text(encoding="utf-8", errors="replace")
    if extension == ".json":
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except Exception:
            pass
    return _paragraph_blocks(text)
