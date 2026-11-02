"""Uploaded files and images -> Claude message content.

* Images (PNG, JPEG, GIF, WebP) are sent to Claude as image blocks.
* Text-like files, PDFs and Word (.docx) files are converted to TEXT here and inlined in the prompt,
  so Cisco AI Defense inspects exactly what Claude reads (and scanned PDFs are rejected instead of
  silently sending nothing).

File types are detected from the bytes (magic numbers), never from the browser-supplied type.
"""
import base64
import binascii
import io
import re
import zipfile
from dataclasses import dataclass

from .config import Settings

IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
TEXT_EXTENSIONS = {
    "txt", "md", "csv", "tsv", "json", "jsonl", "yaml", "yml", "xml", "html", "htm", "svg", "log", "ini",
    "conf", "cfg", "toml", "env", "sql", "py", "js", "ts", "sh", "bash", "ps1", "java", "go", "c", "cpp",
    "h", "rs", "rb", "php", "css", "tf", "rst",
}
# value for <input type="file" accept="...">
ACCEPT = ",".join(["image/png", "image/jpeg", "image/gif", "image/webp", ".pdf", ".docx"]
                  + [f".{e}" for e in sorted(TEXT_EXTENSIONS)])
MAX_DOCX_UNCOMPRESSED = 50 * 1024 * 1024


class AttachmentError(ValueError):
    """A problem with an uploaded file; the message is safe to show to the user."""


@dataclass
class Processed:
    name: str
    kind: str               # image | text | pdf | docx
    size: int
    block: dict             # Claude content block
    inspect_text: str       # plain-text stand-in used for AI Defense API-mode inspection
    chars: int = 0
    pages: int | None = None
    truncated: bool = False

    def summary(self) -> dict:
        return {"name": self.name, "kind": self.kind, "bytes": self.size, "chars": self.chars,
                "pages": self.pages, "truncated": self.truncated}


def safe_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f\"<>\\/]", "", name or "").strip()
    return (name[:120] or "file")


def sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def decode_b64(name: str, b64: str, limit: int) -> bytes:
    if len(b64) > limit * 4 // 3 + 8:
        raise AttachmentError(f"'{name}' is too large (max {limit // (1024 * 1024)} MB per file).")
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise AttachmentError(f"'{name}' could not be read (invalid upload).") from None
    if not data:
        raise AttachmentError(f"'{name}' is empty.")
    if len(data) > limit:
        raise AttachmentError(f"'{name}' is too large (max {limit // (1024 * 1024)} MB per file).")
    return data


def _clip(text: str, limit: int) -> tuple[str, bool]:
    text = text.replace("\x00", "")
    if len(text) > limit:
        return text[:limit] + "\n[... file truncated ...]", True
    return text, False


def _wrap(name: str, kind: str, body: str, extra: str = "") -> str:
    return f'<file name="{name}" type="{kind}"{extra}>\n{body}\n</file>'


def _pdf_text(name: str, data: bytes, s: Settings) -> tuple[str, int, bool]:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise AttachmentError(f"'{name}' is password-protected.")
        total = len(reader.pages)
        parts = []
        for page in reader.pages[: s.max_pdf_pages]:
            parts.append(page.extract_text() or "")
    except AttachmentError:
        raise
    except Exception:  # noqa: BLE001 - untrusted file, any parser error means "can't read it"
        raise AttachmentError(f"'{name}' is not a readable PDF.") from None
    return "\n\n".join(parts).strip(), total, total > s.max_pdf_pages


def _docx_text(name: str, data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_DOCX_UNCOMPRESSED:
                raise AttachmentError(f"'{name}' expands to too much data.")
        from docx import Document
        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
    except AttachmentError:
        raise
    except Exception:  # noqa: BLE001
        raise AttachmentError(f"'{name}' is not a readable .docx file.") from None
    return "\n".join(parts).strip()


def process_attachment(raw_name: str, b64: str, s: Settings) -> Processed:
    name = safe_name(raw_name)
    data = decode_b64(name, b64, s.max_file_bytes)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

    media = sniff_image(data)
    if media:
        return Processed(name, "image", len(data),
                         {"type": "image", "source": {"type": "base64", "media_type": media,
                                                      "data": base64.b64encode(data).decode()}},
                         f"[image attached: {name}]")

    if data.startswith(b"%PDF-"):
        text, pages, cut = _pdf_text(name, data, s)
        if not text:
            raise AttachmentError(f"No text found in '{name}' (scanned PDF?). Upload the pages as images instead.")
        text, clipped = _clip(text, s.max_file_text_chars)
        body = _wrap(name, "pdf", text, f' pages="{pages}"')
        return Processed(name, "pdf", len(data), {"type": "text", "text": body}, body,
                         chars=len(text), pages=pages, truncated=cut or clipped)

    if data.startswith(b"PK\x03\x04"):
        if ext != "docx":
            raise AttachmentError(f"'{name}': archives and Office files other than .docx are not supported.")
        text = _docx_text(name, data)
        if not text:
            raise AttachmentError(f"No text found in '{name}'.")
        text, clipped = _clip(text, s.max_file_text_chars)
        body = _wrap(name, "docx", text)
        return Processed(name, "docx", len(data), {"type": "text", "text": body}, body,
                         chars=len(text), truncated=clipped)

    if b"\x00" not in data[:8192]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None and text.strip():
            text, clipped = _clip(text, s.max_file_text_chars)
            body = _wrap(name, "text", text)
            return Processed(name, "text", len(data), {"type": "text", "text": body}, body,
                             chars=len(text), truncated=clipped)

    raise AttachmentError(
        f"'{name}' is not a supported file. Supported: images (PNG, JPEG, GIF, WebP), PDF, .docx and text files.")


def build_conversation(messages, s: Settings):
    """Return (llm_messages, inspection_messages, summaries_of_last_message_files).

    llm_messages    - what Claude receives (content blocks when files are attached)
    inspect_messages- plain-text version used for AI Defense API-mode inspection
    """
    llm, inspect, summaries = [], [], []
    total = 0
    last = len(messages) - 1
    for i, m in enumerate(messages):
        if m.role == "user" and m.attachments:
            if len(m.attachments) > s.max_files_per_message:
                raise AttachmentError(f"At most {s.max_files_per_message} files per message.")
            blocks, itexts = [], []
            for a in m.attachments:
                pa = process_attachment(a.name, a.data, s)
                total += pa.size
                if total > s.max_total_attachment_bytes:
                    raise AttachmentError(
                        f"This chat has more than {s.max_total_attachment_bytes // (1024 * 1024)} MB of attachments. "
                        "Click Reset to start a new chat.")
                blocks.append(pa.block)
                itexts.append(pa.inspect_text)
                if i == last:
                    summaries.append(pa.summary())
            text = m.content.strip() or "Please look at the attached file(s)."
            blocks.append({"type": "text", "text": text})
            llm.append({"role": "user", "content": blocks})
            inspect.append({"role": "user", "content": "\n\n".join(itexts + [text])})
        else:
            llm.append({"role": m.role, "content": m.content})
            inspect.append({"role": m.role, "content": m.content})
    return llm, inspect, summaries
