"""File tools: read (with :N / :N-M line selectors), write, edit (unique-match replace)."""

import base64
import html
import io
import re
import zipfile
from pathlib import Path

TRUNCATE_READ = 20000

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
IMAGE_MAX_DIM = 1568  # vision sweet spot; bigger just burns tokens
IMAGE_RAW_KEEP = 512 * 1024  # originals up to this ride as-is, no re-encode
IMAGE_MAX_BYTES = 64 * 1024 * 1024  # refuse to buffer absurd files


class ImageRead(str):
    """Read result carrying attached images: a plain string summary (renders,
    saves, truncates like any tool output) plus the pixel data URLs, so the
    agent loop can upgrade the tool message to multimodal content. One URL
    for an image file; several for a video's keyframes."""

    data_urls: list[str]

    def __new__(cls, summary: str, data_urls: list[str]):
        obj = super().__new__(cls, summary)
        obj.data_urls = list(data_urls)
        return obj

    @property
    def data_url(self) -> str:
        return self.data_urls[0]


def _human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} bytes"  # unreachable


_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF8", "GIF image"),
    (b"%PDF", "PDF document"),
    (b"PK\x03\x04", "ZIP archive (docx/xlsx/pptx/jar/apk are ZIPs — unzip for the XML)"),
    (b"\x1f\x8b", "gzip archive"),
    (b"7z\xbc\xaf\x27\x1c", "7z archive"),
    (b"Rar!", "RAR archive"),
    (b"MZ", "Windows executable (PE)"),
    (b"\x7fELF", "Linux executable (ELF)"),
    (b"\x1aE\xdf\xa3", "Matroska/WebM video"),
    (b"OggS", "Ogg media"),
    (b"fLaC", "FLAC audio"),
    (b"ID3", "MP3 audio"),
    (b"SQLite format 3\x00", "SQLite database"),
]


def _sniff(raw: bytes) -> str:
    if raw[4:8] == b"ftyp":
        return "MP4/MOV video"
    if raw[:4] == b"RIFF":
        return {"WEBP": "WEBP image", "WAVE": "WAV audio"}.get(
            raw[8:12].decode("latin-1"), "RIFF container"
        )
    for magic, label in _MAGIC:
        if raw.startswith(magic):
            return label
    return "unknown binary format"


_BINARY_HINT = (
    "Bytes are not readable as text; do NOT re-read this file as text. "
    "To extract content use `bash`: write a one-off .py script first (inline "
    "multi-line `python -c` breaks under cmd.exe quoting), then run it — "
    "e.g. zipfile for OOXML, pypdf/pdftotext for PDFs, strings for fallbacks."
)


def _read_image(file: Path) -> str:
    """Images ride along as data URLs. The old text path was worse than
    useless here: errors="replace" mojibake meant the model saw neither the
    bytes nor the picture."""
    try:
        raw = file.read_bytes()
    except OSError as exc:
        return f"ERROR: {exc}"
    if len(raw) > IMAGE_MAX_BYTES:
        return (
            f"ERROR: {file.name} is {len(raw)} bytes — too large to attach (cap {IMAGE_MAX_BYTES})"
        )
    url, mime, dims = image_data_url(file.suffix.lower(), raw)
    if url is None:
        return (
            f"IMAGE: {file.name} ({len(raw)} bytes, extension says {file.suffix}) "
            "— could not be decoded as an image, so no pixels are attached"
        )
    return ImageRead(
        f"IMAGE: {file.name} — attached to this result as {mime}, {dims} "
        "(vision models can see it; describe the content directly)",
        [url],
    )


_OOXML_PARTS = {
    ".docx": ["word/document.xml"],
    ".xlsx": ["xl/sharedStrings.xml"],
}
_T_RE = re.compile(r"<(?:\w+:)?t(?:\s[^>]*)?>(.*?)</(?:\w+:)?t>", re.S)
_P_SPLIT = re.compile(r"</(?:\w+:)?p>")


def _read_ooxml(file: Path) -> str:
    """Office files are ZIPs of XML — read's job is text, so extract it in one
    step instead of bouncing the agent through bash + zipfile scripts. Text
    nodes keep their namespace-agnostic local names (some exporters use odd
    prefixes); docx/pptx yield one line per paragraph, xlsx one per string."""
    suffix = file.suffix.lower()
    try:
        zf = zipfile.ZipFile(file)
    except (OSError, zipfile.BadZipFile) as exc:
        return f"ERROR: unreadable OOXML package: {exc}"
    try:
        if suffix == ".pptx":
            names = sorted(n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        else:
            names = _OOXML_PARTS[suffix]
        lines = []
        for name in names:
            data = zf.read(name).decode("utf-8", errors="replace")
            blocks = _P_SPLIT.split(data) if suffix != ".xlsx" else [data]
            for block in blocks:
                text = html.unescape("".join(_T_RE.findall(block)))
                if text.strip():
                    lines.append(text)
        if not lines:
            return f"OFFICE: {file.name} — package readable but contains no text nodes"
        return _truncate("\n".join(lines), TRUNCATE_READ)
    except KeyError as exc:
        return (
            f"ERROR: OOXML package is missing {exc} (nonstandard export). "
            "Fall back to `bash` with a python script over zipfile."
        )
    finally:
        zf.close()


def image_data_url(ext: str, raw: bytes) -> tuple[str | None, str, str]:
    """Return (data_url, mime, "WxH"). Small originals ride as-is; anything
    bigger is downscaled and re-encoded JPEG (a 2448px phone photo base64s
    into an endpoint-killing payload). The mime comes from the decoded format,
    NOT the extension — files routinely wear the wrong one (.jpg that is a PNG)."""
    try:
        from PIL import Image  # noqa: PLC0415 (optional dependency)
    except ImportError:
        if len(raw) > IMAGE_RAW_KEEP:
            return None, "", ""
        mime = _IMAGE_MIME.get(ext, "application/octet-stream")
        return f"data:{mime};base64,{base64.b64encode(raw).decode()}", mime, "unknown size"
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None, "", ""
    dims = f"{img.width}x{img.height}"
    if len(raw) <= IMAGE_RAW_KEEP and max(img.size) <= IMAGE_MAX_DIM:
        fmt = (img.format or "").lower()
        mime = f"image/{fmt}" if fmt else _IMAGE_MIME.get(ext, "application/octet-stream")
        return f"data:{mime};base64,{base64.b64encode(raw).decode()}", mime, dims
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))  # plain convert("RGB")
        bg.paste(img, mask=img.split()[3])  # composites alpha onto black
        img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail((IMAGE_MAX_DIM, IMAGE_MAX_DIM))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    out = buf.getvalue()
    return (
        "data:image/jpeg;base64," + base64.b64encode(out).decode(),
        "image/jpeg",
        f"{img.width}x{img.height}",
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [truncated {len(text) - limit} chars] ...\n{text[-half:]}"


def tool_read(path: str) -> str:
    """Read a file, numbering lines. Supports `path:N` and `path:N-M` selectors."""
    selector: tuple[int, int] | None = None
    base = path
    if ":" in path:
        head, _, tail = path.rpartition(":")
        parts = tail.split("-", maxsplit=1)
        if parts and parts[0].isdigit() and all(p.isdigit() for p in parts):
            start = int(parts[0])
            end = int(parts[1]) if len(parts) == 2 else start
            selector = (start, end)
            base = head

    file = Path(base)
    if not file.is_file():
        return f"ERROR: File not found: {base}"
    if file.suffix.lower() in IMAGE_EXTS:
        if selector is not None:
            return "ERROR: Images are attached whole — drop the :N line selector"
        return _read_image(file)
    if file.suffix.lower() in {".docx", ".pptx", ".xlsx"}:
        return _read_ooxml(file)
    try:
        raw = file.read_bytes()
    except OSError as exc:
        return f"ERROR: {exc}"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        content = raw.decode("utf-16", errors="replace")  # BOM: real text
    elif b"\x00" in raw[:8192]:  # git's null-byte heuristic
        return (
            f"BINARY: {file.name} — {_human_size(len(raw))}, detected {_sniff(raw)}. {_BINARY_HINT}"
        )
    else:
        content = raw.decode("utf-8-sig", errors="replace")

    lines = content.splitlines()
    if selector is not None:
        start, end = selector
        if start < 1:
            return "ERROR: Line numbers start at 1"
        if start > len(lines):
            return f"ERROR: Line {start} past end ({len(lines)} lines)"
        end = min(end, len(lines))
        numbered = [f"{i}:{lines[i - 1]}" for i in range(start, end + 1)]
        return _truncate("\n".join(numbered), TRUNCATE_READ)

    numbered = [f"{i + 1}:{line}" for i, line in enumerate(lines)]
    return _truncate("\n".join(numbered), TRUNCATE_READ)


def tool_write(path: str, content: str) -> str:
    file = Path(path)
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8", newline="")
    except OSError as exc:
        return f"ERROR: {exc}"
    return f"Wrote {file} ({file.stat().st_size} bytes)"


def tool_edit(path: str, old_string: str, new_string: str) -> str:
    file = Path(path)
    if not file.is_file():
        return f"ERROR: File not found: {path}"
    try:
        content = file.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return f"ERROR: {exc}"
    count = content.count(old_string)
    if count == 0:
        return f"ERROR: old_string not found in {path}"
    if count > 1:
        return f"ERROR: old_string matches {count} times — must be unique. Include more context."
    file.write_text(content.replace(old_string, new_string, 1), encoding="utf-8", newline="")
    return f"Edited {path} (1 replacement)"
