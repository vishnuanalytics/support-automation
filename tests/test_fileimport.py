"""P7b — text extraction for KB file upload."""

from __future__ import annotations

import io
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from interpreter import fileimport


def test_plain_text_and_markdown():
    title, text = fileimport.extract("Runbook.md", b"# Heading\n\nsteps here")
    assert title == "Runbook" and text == "# Heading\n\nsteps here"
    assert fileimport.extract("notes.txt", b"hello")[1] == "hello"
    assert fileimport.extract("data.csv", b"a,b\n1,2")[1] == "a,b\n1,2"


def test_unsupported_extension_and_empty_and_oversize():
    with pytest.raises(ValueError):
        fileimport.extract("image.png", b"\x89PNG")
    with pytest.raises(ValueError):
        fileimport.extract("blank.txt", b"   \n  ")
    with pytest.raises(ValueError):
        fileimport.extract("big.txt", b"x" * (fileimport.MAX_BYTES + 1))


def test_text_is_capped():
    long = ("word " * 100_000).encode()
    assert len(fileimport.extract("big.md", long)[1]) == fileimport.MAX_CHARS


def test_pdf_roundtrip():
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    # a blank page extracts to "" -> extract() rejects an empty document
    with pytest.raises(ValueError):
        fileimport.extract("blank.pdf", buf.getvalue())


def test_docx_roundtrip():
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_paragraph("First paragraph.")
    d.add_paragraph("Second one.")
    buf = io.BytesIO()
    d.save(buf)
    title, text = fileimport.extract("Policy.docx", buf.getvalue())
    assert title == "Policy"
    assert "First paragraph." in text and "Second one." in text
