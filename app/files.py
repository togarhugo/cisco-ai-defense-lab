"""Uploaded files and images -> Claude message content.

* Images (PNG, JPEG, GIF, WebP) are sent to Claude as image blocks.
* Text/code files, PDF, Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) are converted to TEXT here
  and inlined in the prompt, so Cisco AI Defense inspects exactly what Claude reads.
* Archives (.zip, .tar, .tar.gz/.tgz/.tar.bz2/.tar.xz, and single .gz/.bz2/.xz files) are unpacked IN MEMORY
  (nothing is written to disk), every entry is run through the same rules, and Claude receives a manifest of what
  was read and skipped plus the readable contents. Zip bombs are stopped by hard limits on entries, bytes
  actually decompressed and text size; archives inside archives are not opened.

File types are detected from the bytes (magic numbers), never from the browser-supplied type or the extension.
"""
import base64
import binascii
import bz2
import gzip
import io
import lzma
import re
import tarfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass, field

from .config import Settings

MAX_DOCX_UNCOMPRESSED = 50 * 1024 * 1024
MAX_XML_PART = 30 * 1024 * 1024
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
SUPPORTED = ("images (PNG, JPEG, GIF, WebP), PDF, Word/Excel/PowerPoint (.docx .xlsx .pptx), "
             "archives (.zip .tar .tar.gz .tgz .tar.bz2 .tar.xz .gz), and text or code files")


class AttachmentError(ValueError):
    """A problem with an uploaded file. The message is safe to show to the user; `reason` is the short
    form used when the file is one entry inside an archive."""

    def __init__(self, message: str, reason: str = "unsupported file type"):
        super().__init__(message)
        self.reason = reason


@dataclass
class Processed:
    name: str
    kind: str               # image | text | pdf | docx | xlsx | pptx | archive
    size: int
    blocks: list            # Claude content blocks (one text block, plus images for archives)
    inspect_text: str       # plain-text stand-in used for AI Defense API-mode inspection
    chars: int = 0
    pages: int | None = None
    truncated: bool = False
    entries: int | None = None      # archives only
    read: int | None = None
    skipped: int | None = None
    notes: list = field(default_factory=list)

    @property
    def block(self) -> dict:
        return self.blocks[0]

    def summary(self) -> dict:
        d = {"name": self.name, "kind": self.kind, "bytes": self.size, "chars": self.chars,
             "pages": self.pages, "truncated": self.truncated}
        if self.entries is not None:
            d.update(entries=self.entries, read=self.read, skipped=self.skipped, notes=self.notes[:10])
        return d


# ---------- small helpers ----------
def safe_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f\"<>\\/]", "", name or "").strip()
    return (name[:120] or "file")


def safe_path(path: str) -> str:
    """Display form of a path inside an archive. Nothing is ever written to disk, this only keeps the text tidy."""
    parts = []
    for seg in re.split(r"[\\/]+", path or ""):
        seg = re.sub(r"[\x00-\x1f\x7f\"<>]", "", seg).strip()
        if seg not in ("", ".", ".."):
            parts.append(seg)
    return "/".join(parts)[:200] or "file"


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
        raise AttachmentError(f"'{name}' is too large (max {limit // (1024 * 1024)} MB per file).", "too large")
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise AttachmentError(f"'{name}' could not be read (invalid upload).", "unreadable") from None
    if not data:
        raise AttachmentError(f"'{name}' is empty.", "empty")
    if len(data) > limit:
        raise AttachmentError(f"'{name}' is too large (max {limit // (1024 * 1024)} MB per file).", "too large")
    return data


def _clip(text: str, limit: int) -> tuple[str, bool]:
    text = text.replace("\x00", "")
    if len(text) > limit:
        return text[:limit] + "\n[... file truncated ...]", True
    return text, False


def _wrap(name: str, kind: str, body: str, extra: str = "") -> str:
    return f'<file name="{name}" type="{kind}"{extra}>\n{body}\n</file>'


def _text_processed(name, kind, size, text, s, **kw) -> Processed:
    extra, cut = kw.pop("extra", ""), kw.pop("cut", False)
    text, clipped = _clip(text, s.max_file_text_chars)
    body = _wrap(name, kind, text, extra)
    return Processed(name, kind, size, [{"type": "text", "text": body}], body,
                     chars=len(text), truncated=clipped or cut, **kw)


# ---------- document formats ----------
def _pdf_text(name: str, data: bytes, s: Settings) -> tuple[str, int, bool]:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise AttachmentError(f"'{name}' is password-protected.", "password-protected")
        total = len(reader.pages)
        parts = [page.extract_text() or "" for page in reader.pages[: s.max_pdf_pages]]
    except AttachmentError:
        raise
    except Exception:  # noqa: BLE001 - untrusted file, any parser error means "can't read it"
        raise AttachmentError(f"'{name}' is not a readable PDF.", "unreadable PDF") from None
    return "\n\n".join(parts).strip(), total, total > s.max_pdf_pages


def _docx_text(name: str, data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_DOCX_UNCOMPRESSED:
                raise AttachmentError(f"'{name}' expands to too much data.", "too large when unpacked")
        from docx import Document
        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
    except AttachmentError:
        raise
    except Exception:  # noqa: BLE001
        raise AttachmentError(f"'{name}' is not a readable .docx file.", "unreadable") from None
    return "\n".join(parts).strip()


def _zip_member(z: zipfile.ZipFile, member: str, limit: int = MAX_XML_PART) -> bytes:
    with z.open(member) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise AttachmentError("part too large", "too large when unpacked")
    return data


_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_DRAW = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "")
    n = 0
    for ch in (letters.group(0) if letters else "A"):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _xlsx_text(name: str, data: bytes) -> str:
    """Cell values of every sheet (first 20 sheets, 5000 rows each), tab separated. Formulas show their last value."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_DOCX_UNCOMPRESSED:
                raise AttachmentError(f"'{name}' expands to too much data.", "too large when unpacked")
            names = z.namelist()
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(_zip_member(z, "xl/sharedStrings.xml"))
                shared = ["".join(t.text or "" for t in si.iter(_NS_MAIN + "t")) for si in root.iter(_NS_MAIN + "si")]
            sheet_names = [e.get("name") or "" for e in ET.fromstring(_zip_member(z, "xl/workbook.xml")).iter(_NS_MAIN + "sheet")]
            sheets = sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                            key=lambda n: int(re.findall(r"\d+", n)[-1]))
            out = []
            for i, path in enumerate(sheets[:20]):
                label = sheet_names[i] if len(sheet_names) == len(sheets) else f"sheet{i + 1}"
                rows = []
                for row in ET.fromstring(_zip_member(z, path)).iter(_NS_MAIN + "row"):
                    if len(rows) >= 5000:
                        rows.append("[... more rows not shown ...]")
                        break
                    cells: dict[int, str] = {}
                    for c in row.iter(_NS_MAIN + "c"):
                        t, v = c.get("t"), c.find(_NS_MAIN + "v")
                        if t == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                            val = shared[int(v.text)]
                        elif t == "inlineStr":
                            val = "".join(x.text or "" for x in c.iter(_NS_MAIN + "t"))
                        elif t == "b" and v is not None:
                            val = "TRUE" if v.text == "1" else "FALSE"
                        elif v is not None:
                            val = v.text or ""
                        else:
                            continue
                        cells[_col_index(c.get("r"))] = val.replace("\t", " ").replace("\n", " ")
                    if cells:
                        rows.append("\t".join(cells.get(k, "") for k in range(max(cells) + 1)))
                out.append(f"## Sheet: {label}\n" + "\n".join(rows))
    except AttachmentError:
        raise
    except Exception:  # noqa: BLE001
        raise AttachmentError(f"'{name}' is not a readable .xlsx file.", "unreadable") from None
    return "\n\n".join(out).strip()


def _pptx_text(name: str, data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_DOCX_UNCOMPRESSED:
                raise AttachmentError(f"'{name}' expands to too much data.", "too large when unpacked")
            slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                            key=lambda n: int(re.findall(r"\d+", n)[-1]))
            out = []
            for i, path in enumerate(slides[:200], 1):
                paras = []
                for p in ET.fromstring(_zip_member(z, path)).iter(_NS_DRAW + "p"):
                    line = "".join(t.text or "" for t in p.iter(_NS_DRAW + "t")).strip()
                    if line:
                        paras.append(line)
                out.append(f"## Slide {i}\n" + "\n".join(paras))
    except AttachmentError:
        raise
    except Exception:  # noqa: BLE001
        raise AttachmentError(f"'{name}' is not a readable .pptx file.", "unreadable") from None
    return "\n\n".join(out).strip()


# ---------- archives ----------
def _compression(data: bytes) -> str | None:
    if data[:2] == b"\x1f\x8b":
        return "gzip"
    if data[:3] == b"BZh" and data[3:4] in b"123456789" and data[4:10] == b"1AY&SY":
        return "bzip2"
    if data[:6] == b"\xfd7zXZ\x00":
        return "xz"
    return None


def _decompress(name: str, data: bytes, comp: str, limit: int) -> bytes:
    opener = {"gzip": lambda b: gzip.GzipFile(fileobj=b), "bzip2": bz2.BZ2File, "xz": lzma.LZMAFile}[comp]
    out, total = io.BytesIO(), 0
    try:
        with opener(io.BytesIO(data)) as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:   # zip-bomb guard: stop as soon as the real output is too big
                    raise AttachmentError(
                        f"'{name}' unpacks to more than {limit // (1024 * 1024)} MB and was rejected.",
                        "too large when unpacked")
                out.write(chunk)
    except AttachmentError:
        raise
    except (OSError, EOFError, lzma.LZMAError, zlib.error, ValueError):
        raise AttachmentError(f"'{name}' is corrupt or truncated.", "corrupt") from None
    return out.getvalue()


def _is_tar(data: bytes) -> bool:
    if len(data) < 512:
        return False
    if data[257:262] == b"ustar":
        return True
    try:
        return tarfile.is_tarfile(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        return False


def _read_limited(fobj, limit: int) -> bytes:
    data = fobj.read(limit + 1)
    if len(data) > limit:
        raise AttachmentError("too large", "too large")
    return data


def _zip_entries(z: zipfile.ZipFile):
    for info in z.infolist():
        if info.is_dir():
            continue
        if info.flag_bits & 0x1:
            yield info.filename, info.file_size, None, "encrypted"
            continue

        def reader(limit, info=info):
            try:
                with z.open(info) as f:
                    return _read_limited(f, limit)
            except AttachmentError:
                raise
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError, zlib.error):
                raise AttachmentError("unreadable entry", "unreadable") from None

        yield info.filename, info.file_size, reader, None


def _tar_entries(tf: tarfile.TarFile):
    for m in tf:
        if not m.isfile():          # directories, symlinks, devices: never followed
            continue

        def reader(limit, m=m):
            f = tf.extractfile(m)
            if f is None:
                raise AttachmentError("unreadable entry", "unreadable")
            try:
                return _read_limited(f, limit)
            except (tarfile.TarError, OSError, EOFError):
                raise AttachmentError("unreadable entry", "unreadable") from None

        yield m.name, m.size, reader, None


def _archive(name: str, fmt: str, size: int, entries, s: Settings) -> Processed:
    lines, parts, image_blocks, notes = [], [], [], []
    scanned = read = skipped = total_bytes = text_chars = 0
    truncated = False

    def skip(label: str, reason: str):
        nonlocal skipped
        skipped += 1
        lines.append(f"- {label}: skipped ({reason})")
        notes.append(f"{label}: {reason}")

    try:
        for path, declared, reader, pre in entries:
            label = safe_path(path)
            if label.rsplit("/", 1)[-1].lower() in JUNK_NAMES or label.startswith("__MACOSX/"):
                continue
            scanned += 1
            if scanned > s.max_archive_entries:
                lines.append(f"... more entries not scanned (limit {s.max_archive_entries})")
                truncated = True
                break
            if pre:
                skip(label, pre)
            elif declared > s.max_file_bytes:
                skip(label, f"larger than {s.max_file_bytes // (1024 * 1024)} MB")
            elif read >= s.max_archive_files_read:
                skip(label, f"file limit of {s.max_archive_files_read} reached")
            elif total_bytes + declared > s.max_archive_uncompressed_bytes:
                skip(label, "archive size limit reached")
            else:
                try:
                    raw = reader(min(s.max_file_bytes, s.max_archive_uncompressed_bytes - total_bytes))
                    total_bytes += len(raw)
                    inner = process_bytes(label, raw, s, depth=1)
                except AttachmentError as e:
                    skip(label, e.reason)
                    continue
                if inner.kind == "image":
                    if len(image_blocks) >= s.max_archive_images:
                        skip(label, f"image limit of {s.max_archive_images} reached")
                        continue
                    image_blocks.extend(inner.blocks)
                    lines.append(f"- {label}: image attached")
                    read += 1
                    continue
                budget = s.max_archive_text_chars - text_chars
                if budget <= 0:
                    skip(label, "archive text limit reached")
                    continue
                body = inner.blocks[0]["text"]
                if len(body) > budget:
                    body = body[:budget] + "\n[... archive text limit reached ...]\n</file>"
                    truncated = True
                text_chars += len(body)
                parts.append(body)
                read += 1
                lines.append(f"- {label}: read ({inner.chars} characters)")
    except (tarfile.TarError, zipfile.BadZipFile, EOFError, OSError, zlib.error):
        raise AttachmentError(f"'{name}' is damaged or not a valid {fmt} archive.", "corrupt") from None

    if read == 0:
        raise AttachmentError(
            f"No readable files found in '{name}' ({scanned} entries, {skipped} skipped). Supported inside archives: "
            + SUPPORTED + ".", "nothing readable")
    head = (f'<archive name="{name}" format="{fmt}" entries="{scanned}" read="{read}" skipped="{skipped}">\n'
            "Contents:\n" + "\n".join(lines) + "\n</archive>")
    text = head + "\n\n" + "\n\n".join(parts) if parts else head
    return Processed(name, "archive", size, [{"type": "text", "text": text}] + image_blocks,
                     text + (f"\n[{len(image_blocks)} image(s) from the archive attached]" if image_blocks else ""),
                     chars=text_chars, truncated=truncated, entries=scanned, read=read, skipped=skipped, notes=notes)


def _process_zip(name: str, data: bytes, s: Settings, depth: int) -> Processed:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise AttachmentError(f"'{name}' is not a valid zip file.", "corrupt") from None
    with z:
        names = set(z.namelist())
        if "[Content_Types].xml" in names:       # Office Open XML
            if "word/document.xml" in names:
                text = _docx_text(name, data)
                kind = "docx"
            elif "xl/workbook.xml" in names:
                text = _xlsx_text(name, data)
                kind = "xlsx"
            elif "ppt/presentation.xml" in names:
                text = _pptx_text(name, data)
                kind = "pptx"
            else:
                raise AttachmentError(f"'{name}': this Office file type is not supported (use .docx, .xlsx or .pptx).")
            if not text:
                raise AttachmentError(f"No text found in '{name}'.", "no text")
            return _text_processed(name, kind, len(data), text, s)
        if "mimetype" in names:
            try:
                if _zip_member(z, "mimetype", 200).startswith(b"application/vnd.oasis.opendocument"):
                    raise AttachmentError(f"'{name}': OpenDocument files are not supported. Save as .docx/.xlsx/.pptx.")
            except KeyError:
                pass
        if depth > 0:
            raise AttachmentError(f"'{name}' is an archive inside an archive.", "nested archive")
        return _archive(name, "zip", len(data), _zip_entries(z), s)


def _process_tar(name: str, tar_bytes: bytes, size: int, s: Settings, depth: int, fmt: str = "tar") -> Processed:
    if depth > 0:
        raise AttachmentError(f"'{name}' is an archive inside an archive.", "nested archive")
    try:
        tf = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:")
    except tarfile.TarError:
        raise AttachmentError(f"'{name}' is not a valid tar archive.", "corrupt") from None
    with tf:
        return _archive(name, fmt, size, _tar_entries(tf), s)


# ---------- entry point for one file's bytes ----------
def process_bytes(name: str, data: bytes, s: Settings, depth: int = 0) -> Processed:
    """name must already be display-safe. depth 0 = uploaded file, 1 = entry inside an archive."""
    media = sniff_image(data)
    if media:
        return Processed(name, "image", len(data),
                         [{"type": "image", "source": {"type": "base64", "media_type": media,
                                                       "data": base64.b64encode(data).decode()}}],
                         f"[image attached: {name}]")

    if data.startswith(b"%PDF-"):
        text, pages, cut = _pdf_text(name, data, s)
        if not text:
            raise AttachmentError(f"No text found in '{name}' (scanned PDF?). Upload the pages as images instead.",
                                  "scanned PDF, no text")
        return _text_processed(name, "pdf", len(data), text, s, extra=f' pages="{pages}"', pages=pages, cut=cut)

    if data.startswith(OLE_MAGIC):
        raise AttachmentError(f"'{name}' is an old binary Office file (.doc/.xls/.ppt). "
                              "Save it as .docx, .xlsx or .pptx and upload that.", "old Office format")
    if data.startswith(b"7z\xbc\xaf\x27\x1c") or data.startswith(b"Rar!\x1a\x07"):
        raise AttachmentError(f"'{name}': 7z and RAR archives are not supported. Use .zip or .tar.gz.",
                              "7z/RAR not supported")

    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return _process_zip(name, data, s, depth)

    comp = _compression(data)
    if comp:
        limit = s.max_archive_uncompressed_bytes if depth == 0 else s.max_file_bytes
        inner = _decompress(name, data, comp, limit)
        if _is_tar(inner):
            return _process_tar(name, inner, len(data), s, depth, fmt=f"tar.{ {'gzip': 'gz', 'bzip2': 'bz2', 'xz': 'xz'}[comp] }")
        inner_name = re.sub(r"\.(gz|bz2|xz)$", "", name, flags=re.I) or name
        p = process_bytes(inner_name, inner, s, depth + 1)   # e.g. access.log.gz -> the log text
        p.name, p.size = name, len(data)
        return p

    if _is_tar(data):
        return _process_tar(name, data, len(data), s, depth)

    if b"\x00" not in data[:8192]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None and text.strip():
            return _text_processed(name, "text", len(data), text, s)

    raise AttachmentError(f"'{name}' is not a supported file. Supported: {SUPPORTED}.")


def process_attachment(raw_name: str, b64: str, s: Settings) -> Processed:
    name = safe_name(raw_name)
    return process_bytes(name, decode_b64(name, b64, s.max_file_bytes), s)


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
                blocks.extend(pa.blocks)
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
