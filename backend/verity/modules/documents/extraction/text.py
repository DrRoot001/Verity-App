"""File validation and text extraction (PRD §10.5 FR-RES-001..003).

Validation is by magic bytes, not extension: an attacker controls the filename,
never the leading bytes. Extraction produces both text and a quality score, and
a low score routes the document to a fallback rather than silently feeding
garbage into entity extraction.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from enum import StrEnum

from verity.platform.errors import AppError, ErrorCode, RecoveryAction
from verity.platform.logging import get_logger

log = get_logger("documents.extraction")

MAX_FILE_BYTES = 10 * 1024 * 1024  # PRD FR-RES-001
MAX_PDF_PAGES = 30
#: A DOCX that expands beyond this ratio is a decompression bomb (PRD §28.3).
MAX_DECOMPRESSION_RATIO = 120


class DocumentFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    RTF = "rtf"


_MAGIC: tuple[tuple[bytes, DocumentFormat], ...] = (
    (b"%PDF-", DocumentFormat.PDF),
    (b"PK\x03\x04", DocumentFormat.DOCX),  # OOXML is a zip container
    (b"{\\rtf", DocumentFormat.RTF),
)


@dataclass(frozen=True, slots=True)
class ExtractedText:
    text: str
    format: DocumentFormat
    page_count: int
    #: 0-1. Below ``QUALITY_THRESHOLD`` the document needs OCR or manual entry.
    quality: float
    warnings: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return self.quality >= QUALITY_THRESHOLD and len(self.text.strip()) >= 200


QUALITY_THRESHOLD = 0.6

# Common English words used as a cheap "is this real prose" signal. A scanned
# page that OCR'd badly, or a PDF whose text layer is ligature soup, scores low.
# Cheap "is this real prose" signal. A scanned page that OCR'd badly, or a PDF
# whose text layer is ligature soup, matches almost none of these.
_COMMON_WORDS: frozenset[str] = frozenset(
    """
    the and for with to of in on at by from as an a is was were be been
    experience work team project management development engineer senior lead
    university degree skills responsible developed managed built designed
    """.split()
)


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+")


def detect_format(data: bytes, *, declared_content_type: str | None = None) -> DocumentFormat:
    """Identify by leading bytes. Filenames and content types are not trusted."""
    for magic, fmt in _MAGIC:
        if data.startswith(magic):
            if fmt is DocumentFormat.DOCX:
                return _classify_ooxml(data)
            return fmt

    # No magic number: accept only if it decodes as text.
    try:
        data[:4096].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AppError(
            ErrorCode.FILE_TYPE_REJECTED,
            "We couldn't recognise that file type. Upload a PDF, DOCX or text file.",
            recovery_action=RecoveryAction(type="edit_input", label="Choose another file"),
        ) from exc
    return DocumentFormat.TXT


def _classify_ooxml(data: bytes) -> DocumentFormat:
    """A zip container is only a DOCX if it holds a word document part."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            total_uncompressed = sum(i.file_size for i in archive.infolist())
            if total_uncompressed > len(data) * MAX_DECOMPRESSION_RATIO:
                raise AppError(
                    ErrorCode.FILE_TYPE_REJECTED,
                    "That file expands to an unsafe size and was rejected.",
                )
            if "word/document.xml" in names:
                return DocumentFormat.DOCX
    except zipfile.BadZipFile as exc:
        raise AppError(ErrorCode.FILE_TYPE_REJECTED, "That file appears to be corrupted.") from exc

    raise AppError(
        ErrorCode.FILE_TYPE_REJECTED,
        "That looks like an archive rather than a document.",
        recovery_action=RecoveryAction(type="edit_input", label="Choose another file"),
    )


def validate_upload(data: bytes, *, declared_content_type: str | None = None) -> DocumentFormat:
    if not data:
        raise AppError(ErrorCode.VALIDATION_FAILED, "That file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise AppError(
            ErrorCode.FILE_TOO_LARGE,
            f"Files must be under {MAX_FILE_BYTES // (1024 * 1024)} MB.",
            detail={"size_bytes": len(data), "limit_bytes": MAX_FILE_BYTES},
            recovery_action=RecoveryAction(type="edit_input", label="Choose a smaller file"),
        )
    return detect_format(data, declared_content_type=declared_content_type)


def score_quality(text: str, page_count: int) -> tuple[float, list[str]]:
    """Heuristic confidence that extraction produced real prose.

    Three independent signals, because each alone has a blind spot: character
    density catches empty text layers, dictionary ratio catches encoding
    garbage, and section headings catch content that is text but not a resume.
    """
    warnings: list[str] = []
    stripped = text.strip()
    if not stripped:
        return 0.0, ["no_text_extracted"]

    words = _WORD_RE.findall(stripped.lower())
    if not words:
        return 0.0, ["no_words_extracted"]

    chars_per_page = len(stripped) / max(page_count, 1)
    density = min(1.0, chars_per_page / 1200)
    if chars_per_page < 200:
        warnings.append("low_text_density")

    known = sum(1 for w in words if w in _COMMON_WORDS)
    dictionary_ratio = min(1.0, (known / len(words)) / 0.12)
    if known / len(words) < 0.03:
        warnings.append("low_dictionary_match")

    lowered = stripped.lower()
    heading_hits = sum(
        1
        for h in ("experience", "education", "skills", "projects", "summary", "employment")
        if h in lowered
    )
    heading_score = min(1.0, heading_hits / 3)
    if heading_hits == 0:
        warnings.append("no_recognisable_sections")

    quality = 0.4 * density + 0.35 * dictionary_ratio + 0.25 * heading_score
    return round(min(1.0, quality), 3), warnings


def extract_text(data: bytes, fmt: DocumentFormat) -> ExtractedText:
    match fmt:
        case DocumentFormat.PDF:
            text, pages, warnings = _extract_pdf(data)
        case DocumentFormat.DOCX:
            text, pages, warnings = _extract_docx(data)
        case DocumentFormat.RTF:
            text, pages, warnings = _extract_rtf(data)
        case DocumentFormat.TXT:
            text = data.decode("utf-8", errors="replace")
            pages, warnings = 1, []

    text = _normalize_whitespace(text)
    quality, quality_warnings = score_quality(text, pages)
    return ExtractedText(
        text=text,
        format=fmt,
        page_count=pages,
        quality=quality,
        warnings=[*warnings, *quality_warnings],
    )


def _extract_pdf(data: bytes) -> tuple[str, int, list[str]]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    warnings: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as exc:
        raise AppError(
            ErrorCode.EXTRACTION_FAILED,
            "We couldn't read that PDF. It may be corrupted.",
            recovery_action=RecoveryAction(type="edit_input", label="Paste the text instead"),
        ) from exc

    if reader.is_encrypted:
        # An empty-password decrypt covers "protected but not really"; anything
        # else needs a password we will never ask the user to hand over.
        try:
            if reader.decrypt("") == 0:
                raise AppError(
                    ErrorCode.FILE_TYPE_REJECTED,
                    "That PDF is password protected. Remove the password and try again.",
                    recovery_action=RecoveryAction(
                        type="edit_input", label="Upload an unprotected file"
                    ),
                )
        except NotImplementedError as exc:
            raise AppError(
                ErrorCode.FILE_TYPE_REJECTED, "That PDF uses unsupported encryption."
            ) from exc

    pages = reader.pages[:MAX_PDF_PAGES]
    if len(reader.pages) > MAX_PDF_PAGES:
        warnings.append("truncated_to_page_limit")

    chunks: list[str] = []
    for page in pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            warnings.append("page_extraction_failed")
    return "\n".join(chunks), len(pages), warnings


def _extract_docx(data: bytes) -> tuple[str, int, list[str]]:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise AppError(ErrorCode.EXTRACTION_FAILED, "We couldn't read that Word document.") from exc

    parts = [p.text for p in document.paragraphs]

    # Table cells hold real content in many resume templates. Linearizing by row
    # preserves the reading order that a naive paragraph-only pass loses
    # entirely (PRD FR-RES-007).
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(dict.fromkeys(cells)))

    text = "\n".join(parts)
    # DOCX has no fixed pagination; approximate for the density heuristic.
    pages = max(1, len(text) // 3000)
    return text, pages, []


def _extract_rtf(data: bytes) -> tuple[str, int, list[str]]:
    """Minimal RTF de-controlling. Good enough for resume text."""
    raw = data.decode("latin-1", errors="replace")
    without_groups = re.sub(r"\\\*?\{[^}]*\}", " ", raw)
    without_controls = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", without_groups)
    text = without_controls.replace("{", " ").replace("}", " ")
    return text, max(1, len(text) // 3000), ["rtf_basic_extraction"]


def _normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\ufb01", "fi").replace("\ufb02", "fl")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
