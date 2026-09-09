"""Embedded-file attachment pass: metadata inside PDF paperclip files.

Document-level metadata (``exiftool``) and the Ghostscript deep-image pass stop
at the document/page; an AI/C2PA marker carried by an embedded file survives
both. ``clean_pdf`` now extracts each attachment, recursively cleans it, and
re-embeds the cleaned bytes via qpdf.
"""

from __future__ import annotations

import base64
import struct
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service" / "scripts"))

import container_meta
from common import which
from container_meta import clean_pdf

QPDF = which("qpdf")
needs_qpdf = pytest.mark.skipif(QPDF is None, reason="qpdf not installed")


# 16x16 solid-colour JPEG for the ordinary-EXIF fixture, inlined to avoid a PIL
# import (another test installs a PIL stub into sys.modules).
_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYI"
    "DAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkF"
    "BQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQU"
    "FBQUFBT/wAARCAAQABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQF"
    "BgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEI"
    "I0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNk"
    "ZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLD"
    "xMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEB"
    "AQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJB"
    "UQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZH"
    "SElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaan"
    "qKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oA"
    "DAMBAAIRAxEAPwDxSiiivzc/tU//2Q=="
)


def _exif_only_jpeg() -> bytes:
    """A JPEG carrying camera-style EXIF and nothing an AI scan would flag."""
    exif = b"Exif\x00\x00MM\x00*\x00\x00\x00\x08 Camera Model X"
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
    return _TINY_JPEG[:2] + app1 + _TINY_JPEG[2:]


def _show_attachment(root: Path, key: str, pdf: Path) -> bytes:
    path = root / "extract.bin"
    with path.open("wb") as fh:
        subprocess.run([QPDF, f"--show-attachment={key}", str(pdf)], stdout=fh, check=True)
    return path.read_bytes()


def _pdf_with_attachments(root: Path, items: list[tuple[str, bytes]]) -> Path:
    """Build a PDF that embeds each ``(name, data)`` by chaining qpdf."""
    cur: Path | None = None
    for i, (name, data) in enumerate(items):
        src = root / f"src-{i}{Path(name).suffix}"
        src.write_bytes(data)
        out = root / f"step-{i}.pdf"
        attach_opts = [f"--filename={name}", f"--key={name}", "--"]
        cmd: list[str] = []
        if cur is None:
            cmd = [QPDF, "--empty", "--add-attachment", str(src)]
            cmd += [*attach_opts, str(out)]
        else:
            cmd = [QPDF, "--add-attachment", str(src)]
            cmd += [*attach_opts, str(cur), str(out)]
        subprocess.run(cmd, check=True, capture_output=True)
        cur = out
    assert cur is not None, "no attachments to embed"
    return cur


def _empty_pdf(path: Path) -> Path:
    """A valid, attachment-free PDF produced by qpdf."""
    subprocess.run([QPDF, "--empty", "--", str(path)], check=True)
    return path


@needs_qpdf
def test_no_attachments_is_a_noop(tmp_path):
    src = _empty_pdf(tmp_path / "in.pdf")
    actions, meta = clean_pdf(src, tmp_path / "out.pdf", clean_attachments="auto")

    assert meta["attachments_processed"] is False
    assert meta["attachments"] == []
    assert any("no embedded attachments" in a for a in actions)


@needs_qpdf
def test_default_clean_attachments_is_always(tmp_path):
    """The option defaults to `always`, so a marked attachment is stripped."""
    zwsp = "\u200b".encode("utf-8")
    src = _pdf_with_attachments(tmp_path, [("note.txt", b"hello" + zwsp + b"world")])

    # Deliberately omit clean_attachments to exercise the default.
    _actions, meta = clean_pdf(src, tmp_path / "out.pdf")

    assert meta["attachments_processed"] is True
    assert meta["attachments"][0]["cleaned"] is True
    assert zwsp not in _show_attachment(tmp_path, "note.txt", tmp_path / "out.pdf")


@needs_qpdf
def test_auto_cleans_a_marked_attachment(tmp_path):
    zwsp = "\u200b".encode("utf-8")
    src = _pdf_with_attachments(tmp_path, [("note.txt", b"hello" + zwsp + b"world")])

    _actions, meta = clean_pdf(src, tmp_path / "out.pdf", clean_attachments="auto")

    assert meta["attachments_processed"] is True
    att = meta["attachments"][0]
    assert att["kind"] == "text"
    assert att["cleaned"] is True
    assert att["still_has_ai_metadata"] is False
    extracted = _show_attachment(tmp_path, "note.txt", tmp_path / "out.pdf")
    assert zwsp not in extracted


@needs_qpdf
def test_auto_leaves_a_clean_attachment_alone(tmp_path):
    plain = b"no hidden markers here"
    src = _pdf_with_attachments(tmp_path, [("c.txt", plain)])

    _actions, meta = clean_pdf(src, tmp_path / "out.pdf", clean_attachments="auto")

    assert meta["attachments_processed"] is False
    att = meta["attachments"][0]
    assert att["cleaned"] is False
    assert _show_attachment(tmp_path, "c.txt", tmp_path / "out.pdf") == plain


@needs_qpdf
def test_auto_leaves_ordinary_image_exif_alone(tmp_path):
    src = _pdf_with_attachments(tmp_path, [("shot.jpg", _exif_only_jpeg())])

    _actions, meta = clean_pdf(src, tmp_path / "out.pdf", clean_attachments="auto")

    att = meta["attachments"][0]
    assert att["cleaned"] is False
    assert _show_attachment(tmp_path, "shot.jpg", tmp_path / "out.pdf") == _exif_only_jpeg()


@needs_qpdf
def test_always_strips_every_attachment_metadata(tmp_path):
    src = _pdf_with_attachments(tmp_path, [("shot.jpg", _exif_only_jpeg())])

    _actions, meta = clean_pdf(src, tmp_path / "out.pdf", clean_attachments="always")

    att = meta["attachments"][0]
    assert att["cleaned"] is True
    extracted = _show_attachment(tmp_path, "shot.jpg", tmp_path / "out.pdf")
    assert extracted != _exif_only_jpeg()
    # The EXIF APP1 segment is gone.
    assert b"Exif" not in extracted


@needs_qpdf
def test_never_leaves_attachments_untouched(tmp_path):
    zwsp = "\u200b".encode("utf-8")
    src = _pdf_with_attachments(tmp_path, [("note.txt", b"hello" + zwsp + b"world")])

    _actions, meta = clean_pdf(src, tmp_path / "out.pdf", clean_attachments="never")

    assert meta["attachments_processed"] is False
    assert meta["attachments"] == []
    assert zwsp in _show_attachment(tmp_path, "note.txt", tmp_path / "out.pdf")


@needs_qpdf
def test_nested_attachment_recurses(tmp_path):
    zwsp = "\u200b".encode("utf-8")
    # inner.pdf carries a marked text attachment; outer.pdf embeds inner.pdf.
    inner = _pdf_with_attachments(tmp_path, [("inner.txt", b"hi" + zwsp + b"there")])
    outer = _pdf_with_attachments(tmp_path, [("inner.pdf", inner.read_bytes())])

    _actions, meta = clean_pdf(outer, tmp_path / "out.pdf", clean_attachments="auto")

    att = meta["attachments"][0]
    assert att["kind"] == "container"
    assert att["cleaned"] is True
    # The inner PDF attachment was re-embedded cleaned; its own attachment's
    # marker must be gone.
    inner_out = tmp_path / "inner2.pdf"
    with inner_out.open("wb") as fh:
        subprocess.run(
            [QPDF, "--show-attachment=inner.pdf", str(tmp_path / "out.pdf")],
            stdout=fh,
            check=True,
        )
    assert zwsp not in _show_attachment(tmp_path, "inner.txt", inner_out)


@needs_qpdf
def test_recursion_depth_cap_stops_descent(tmp_path):
    """Deeper than MAX_ATTACHMENT_DEPTH must stop before extracting anything."""
    src = tmp_path / "in.pdf"
    src.write_bytes(b"%PDF-1.4\n%%EOF\n")
    actions: list[str] = []
    res = container_meta._pdf_clean_attachments(
        src,
        tmp_path / "out.pdf",
        actions,
        None,
        "auto",
        depth=container_meta.MAX_ATTACHMENT_DEPTH + 1,
    )

    assert res["processed"] is False
    assert any("recursion capped" in a for a in actions)


@needs_qpdf
def test_rejects_an_unknown_clean_attachments_value(tmp_path):
    src = _empty_pdf(tmp_path / "in.pdf")
    with pytest.raises(ValueError, match="clean_attachments"):
        clean_pdf(src, tmp_path / "out.pdf", clean_attachments="sometime")


@needs_qpdf
def test_oversized_attachment_kept_and_warned(tmp_path, monkeypatch):
    monkeypatch.setattr(container_meta, "MAX_ATTACHMENT_BYTES", 4)
    src = _pdf_with_attachments(tmp_path, [("big.txt", b"0123456789")])

    actions, meta = clean_pdf(src, tmp_path / "out.pdf", clean_attachments="always")

    assert meta["attachments_processed"] is False
    att = meta["attachments"][0]
    assert att["cleaned"] is False
    assert att["error"] == "attachment too large"
    assert any("too large" in a for a in actions)
    assert _show_attachment(tmp_path, "big.txt", tmp_path / "out.pdf") == b"0123456789"


@needs_qpdf
def test_invalid_utf8_text_attachment_cleans_without_error(tmp_path):
    """Invalid UTF-8 in a text attachment must not abort clean_pdf."""
    src = _pdf_with_attachments(tmp_path, [("bin.txt", b"hello\xff\xfegoodbye")])

    # Default is `always`; the text pass re-encodes with surrogateescape.
    _actions, meta = clean_pdf(src, tmp_path / "out.pdf")

    assert meta["attachments_processed"] is True
    # surrogateescape must round-trip the invalid bytes, not just avoid a crash.
    assert _show_attachment(tmp_path, "bin.txt", tmp_path / "out.pdf") == b"hello\xff\xfegoodbye"


@needs_qpdf
def test_attachment_dates_survive_reembed(tmp_path):
    """Re-embedding must keep the original timestamps, not stamp "now"."""
    src = tmp_path / "base.pdf"
    note = tmp_path / "note.txt"
    note.write_bytes(b"hello")
    subprocess.run(
        [
            QPDF,
            "--empty",
            "--add-attachment",
            str(note),
            "--filename=note.txt",
            "--key=note.txt",
            "--creationdate=D:20200101120000+00'00'",
            "--",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    original = container_meta._pdf_attachment_list(src)[0]["creationdate"]

    _actions, meta = clean_pdf(src, tmp_path / "out.pdf")

    assert meta["attachments_processed"] is True
    assert container_meta._pdf_attachment_list(tmp_path / "out.pdf")[0]["creationdate"] == original


needs_gs = pytest.mark.skipif(
    container_meta.which_ghostscript() is None, reason="ghostscript not installed"
)


@needs_qpdf
@needs_gs
def test_never_restores_attachments_after_deep_image_pass(tmp_path):
    """A re-distill can drop attachments; `never` must still bring them back."""
    payload = b"do not clean me"
    src = _pdf_with_attachments(tmp_path, [("keep.txt", payload)])

    _actions, meta = clean_pdf(
        src,
        tmp_path / "out.pdf",
        deep_images="always",
        clean_attachments="never",
    )

    # The deep pass must actually run, or this test proves nothing.
    assert meta["deep_image_pass"] is True
    assert meta["attachments_processed"] is False
    assert _show_attachment(tmp_path, "keep.txt", tmp_path / "out.pdf") == payload


@needs_qpdf
def test_inspect_attachments_respects_deadline(tmp_path):
    """`/inspect` must bound the attachment scan and report truncation."""
    src = _pdf_with_attachments(tmp_path, [("note.txt", b"hi")])

    attachments, truncated = container_meta._pdf_inspect_attachments(
        src, container_meta._Deadline(0), depth=0
    )

    assert attachments == []
    assert truncated is True
