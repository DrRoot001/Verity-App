"""PDF text must keep its line structure (PRD FR-RES-002).

Every downstream parser is line-based — section headings, role headers, dates
and bullets are all recognised by being on their own line. A PDF that extracts
as one continuous string therefore yields zero facts while reporting a
successful upload, which is the worst possible failure: silent.

Resumes exported from design tools position each span absolutely, and pypdf's
default mode emits those in content-stream order with no newlines at all. Real
example measured: 3571 characters, 0 newlines in default mode; 3967 characters
and 49 newlines in layout mode.
"""

from __future__ import annotations

from typing import Any

from verity.modules.documents.extraction.text import _page_text


class FakePage:
    """A page whose two extraction modes disagree, as real ones do."""

    def __init__(self, *, default: str, layout: str | None, raises: bool = False) -> None:
        self._default = default
        self._layout = layout
        self._raises = raises

    def extract_text(self, **kwargs: Any) -> str:
        if kwargs.get("extraction_mode") == "layout":
            if self._raises:
                raise ValueError("layout mode not supported for this page")
            return self._layout or ""
        return self._default


def test_layout_mode_is_preferred_when_it_finds_lines() -> None:
    page = FakePage(
        default="SABIH HAIDER Software Engineer PROFESSIONAL SUMMARY Built things",
        layout="SABIH HAIDER\nSoftware Engineer\n\nPROFESSIONAL SUMMARY\nBuilt things",
    )

    assert _page_text(page).count("\n") == 4


def test_the_default_mode_is_used_when_layout_returns_one_line() -> None:
    """Layout mode is not always better; a plain text PDF may lose nothing."""
    page = FakePage(default="Line one\nLine two", layout="Line one Line two")

    assert _page_text(page) == "Line one\nLine two"


def test_a_page_that_cannot_do_layout_mode_still_extracts() -> None:
    page = FakePage(default="Fallback text", layout=None, raises=True)

    assert _page_text(page) == "Fallback text"


def test_an_empty_page_yields_empty_text() -> None:
    page = FakePage(default="", layout="")

    assert _page_text(page) == ""
