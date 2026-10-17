"""Preserve OOXML relationships whose targets contain XML character references."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service" / "scripts"))

from container_meta import _decode_xml_entities, clean_docx

DOCUMENT = b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>'
IMAGE = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPE_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _make_docx(rels_name: str, relationships: str, extra_parts: dict[str, bytes]) -> bytes:
    """Build a synthetic DOCX with caller-supplied relationship XML kept unescaped."""
    parts = {
        "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="svg" ContentType="image/svg+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "word/document.xml": DOCUMENT,
        "_rels/.rels": f'<Relationships xmlns="{REL_NS}"><Relationship Id="main" Type="{TYPE_NS}/officeDocument" Target="word/document.xml"/></Relationships>'.encode(),
        **extra_parts,
    }
    parts[rels_name] = (
        f'<?xml version="1.0"?><Relationships xmlns="{REL_NS}">{relationships}</Relationships>'
    ).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize(
    ("rels_name", "target", "part_name"),
    [
        ("_rels/.rels", "word/document.xml", "word/document.xml"),
        ("_rels/.rels", "word/&#100;ocument.xml", "word/document.xml"),
        ("_rels/.rels", "word/&#x64;ocument.xml", "word/document.xml"),
        pytest.param(
            "word/_rels/document.xml.rels",
            f"media/logo&#{'0' * 5000}97;.svg",
            "word/media/logoa.svg",
            id="decimal-leading-zeroes",
        ),
        pytest.param(
            "word/_rels/document.xml.rels",
            f"media/logo&#x{'0' * 5000}61;.svg",
            "word/media/logoa.svg",
            id="hex-leading-zeroes",
        ),
        (
            "word/_rels/document.xml.rels",
            "media/logo&amp;mark.svg",
            "word/media/logo&mark.svg",
        ),
        (
            "word/_rels/document.xml.rels",
            "media/logo&#38;mark.svg",
            "word/media/logo&mark.svg",
        ),
        (
            "word/_rels/document.xml.rels",
            "media/logo&#x26;mark.svg",
            "word/media/logo&mark.svg",
        ),
        (
            "word/_rels/document.xml.rels",
            "media/logo&amp;amp;mark.svg",
            "word/media/logo&amp;mark.svg",
        ),
    ],
)
def test_docx_preserves_xml_encoded_relationship_target(quote, rels_name, target, part_name):
    """Decode targets once for lookup without rewriting retained XML or part bytes."""
    is_document = part_name == "word/document.xml"
    rel_type = "officeDocument" if is_document else "image"
    relationship = (
        f'<Relationship Id="kept" Type="{TYPE_NS}/{rel_type}" Target={quote}{target}{quote}/>'
    )
    data = _make_docx(rels_name, relationship, {} if is_document else {part_name: IMAGE})

    cleaned, actions = clean_docx(data)

    with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(io.BytesIO(cleaned)) as zf:
        assert zf.read(rels_name) == original.read(rels_name)
        assert zf.read(part_name) == original.read(part_name)
        assert zf.read("word/document.xml") == DOCUMENT
    assert not any("prune dangling relationships" in action for action in actions)


@pytest.mark.parametrize(
    "reference",
    [
        pytest.param(f"&#{'9' * 5000};", id="oversized-decimal"),
        pytest.param(f"&#x{'F' * 5000};", id="oversized-hex"),
        pytest.param(f"&#{'0' * 5000};", id="zero"),
    ],
)
def test_long_invalid_character_reference_stays_literal(reference):
    """Invalid references retain their spelling even beyond Python's integer limit."""
    assert _decode_xml_entities(reference) == reference


@pytest.mark.parametrize("quote", ['"', "'"])
def test_docx_prunes_removed_and_missing_encoded_targets(quote):
    """Entity decoding preserves external links but still prunes absent internal parts."""
    rels_name = "word/_rels/document.xml.rels"
    removed = (
        f'<Relationship Id="removed" Type="{TYPE_NS}/customXml" '
        f"Target={quote}../customXml/&#105;tem1.xml{quote}/>"
    )
    missing = (
        f'<Relationship Id="missing" Type="{TYPE_NS}/image" '
        f"Target={quote}media/missing&amp;image.svg{quote}/>"
    )
    external = (
        f'<Relationship Id="external" Type="{TYPE_NS}/hyperlink" '
        f"Target={quote}https://example.com/?a=1&amp;b=2{quote} TargetMode={quote}External{quote}/>"
    )
    data = _make_docx(rels_name, removed + missing + external, {"customXml/item1.xml": b"<root/>"})

    cleaned, actions = clean_docx(data)

    with zipfile.ZipFile(io.BytesIO(cleaned)) as zf:
        assert "customXml/item1.xml" not in zf.namelist()
        assert zf.read("word/document.xml") == DOCUMENT
        assert zf.read(rels_name).decode() == (
            f'<?xml version="1.0"?><Relationships xmlns="{REL_NS}">{external}</Relationships>'
        )
    assert f"prune dangling relationships x2 in {rels_name}" in actions
