from __future__ import annotations

import stat
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fixtures import (
    basic_epub,
    opf_document,
    patch_all_entries_encrypted,
    write_epub,
    xhtml_document,
)

from cove_book_forge.contracts import BookFormat
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.extractors import EpubExtractor
from cove_book_forge.extractors.base import BookExtractorRegistry
from cove_book_forge.extractors.security import ExtractionLimits

FINGERPRINT = "a" * 64


def _nested_zip_payload() -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("inside.txt", "nested")
    return stream.getvalue()


def _unsafe_reference_epub(tmp_path: Path, *, location: str, reference: str) -> Path:
    chapter = xhtml_document("<p>Readable.</p>")
    if location == "container":
        opf = opf_document(
            manifest='<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>',
            spine='<itemref idref="c"/>',
        )
        container = f"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="{reference}"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
        return write_epub(
            tmp_path / "unsafe-container-reference.epub",
            opf=opf,
            container=container,
            members={"OEBPS/c.xhtml": chapter},
        )
    if location == "manifest":
        opf = opf_document(
            manifest=(f'<item id="c" href="{reference}" media-type="application/xhtml+xml"/>'),
            spine='<itemref idref="c"/>',
        )
        return write_epub(tmp_path / "unsafe-manifest-reference.epub", opf=opf, members={})
    if location == "nav":
        opf = opf_document(
            manifest="""
              <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
              <item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>
            """,
            spine='<itemref idref="c"/>',
        )
        nav = xhtml_document(f'<nav epub:type="toc"><a href="{reference}">Unsafe</a></nav>')
        return write_epub(
            tmp_path / "unsafe-nav-reference.epub",
            opf=opf,
            members={"OEBPS/nav.xhtml": nav, "OEBPS/c.xhtml": chapter},
        )
    if location == "ncx":
        opf = opf_document(
            version="2.0",
            manifest="""
              <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
              <item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>
            """,
            spine='<itemref idref="c"/>',
            spine_attributes='toc="ncx"',
        )
        ncx = f"""<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint id="n1">
  <navLabel><text>Unsafe</text></navLabel><content src="{reference}"/>
</navPoint></navMap></ncx>"""
        return write_epub(
            tmp_path / "unsafe-ncx-reference.epub",
            opf=opf,
            members={"OEBPS/toc.ncx": ncx, "OEBPS/c.xhtml": chapter},
        )
    raise AssertionError(f"unknown test location: {location}")


def test_epub_uses_nested_opf_spine_order_and_navigation_titles(tmp_path: Path) -> None:
    opf = opf_document(
        manifest="""
          <item id="late" href="text/99-last.xhtml" media-type="application/xhtml+xml"/>
          <item id="early" href="text/01-first.xhtml" media-type="application/xhtml+xml"/>
          <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
        """,
        spine='<itemref idref="late"/><itemref idref="early"/>',
    )
    nav = xhtml_document("""
      <nav epub:type="toc"><ol>
        <li><a href="text/01-first.xhtml">Navigation First</a></li>
        <li><a href="text/99-last.xhtml#top">Navigation Last</a></li>
      </ol></nav>
    """)
    source = write_epub(
        tmp_path / "nested.epub",
        opf=opf,
        opf_path="EPUB/package/book.opf",
        members={
            "EPUB/package/nav.xhtml": nav,
            "EPUB/package/text/01-first.xhtml": xhtml_document("<h1>Filename First</h1>"),
            "EPUB/package/text/99-last.xhtml": xhtml_document("<h1>Filename Last</h1>"),
        },
    )

    extracted = EpubExtractor().extract(source, FINGERPRINT)

    assert [chapter.title for chapter in extracted.chapters] == [
        "Navigation Last",
        "Navigation First",
    ]
    assert [chapter.source_locator for chapter in extracted.chapters] == [
        "epub:EPUB/package/text/99-last.xhtml",
        "epub:EPUB/package/text/01-first.xhtml",
    ]
    assert [chapter.index for chapter in extracted.chapters] == [0, 1]


def test_epub_preserves_unicode_metadata_and_supplied_fingerprint(tmp_path: Path) -> None:
    opf = opf_document(
        title="海辺のカフカ",
        author="村上 春樹",
        language="ja-JP",
        manifest='<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>',
        spine='<itemref idref="c"/>',
    )
    source = write_epub(
        tmp_path / "unicode.epub",
        opf=opf,
        members={"OEBPS/c.xhtml": xhtml_document("<p>入口。</p>")},
    )

    extracted = EpubExtractor().extract(source, FINGERPRINT)

    assert extracted.format is BookFormat.EPUB
    assert extracted.metadata.title == "海辺のカフカ"
    assert extracted.metadata.author == "村上 春樹"
    assert extracted.metadata.language == "ja-JP"
    assert extracted.metadata.total_chapters == 1
    assert extracted.source_fingerprint == FINGERPRINT


def test_epub_ignores_foreign_namespace_metadata_manifest_and_spine_nodes(
    tmp_path: Path,
) -> None:
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf"
         xmlns:dc="http://purl.org/dc/elements/1.1/"
         xmlns:foreign="urn:foreign" version="3.0">
  <metadata>
    <foreign:title>Poisoned title</foreign:title>
    <dc:title>Legal title</dc:title>
    <dc:creator>Legal author</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <foreign:item id="poison" href="missing.xhtml" media-type="application/xhtml+xml"/>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <foreign:itemref idref="poison"/>
    <itemref idref="chapter"/>
  </spine>
</package>"""
    source = write_epub(
        tmp_path / "foreign-nodes.epub",
        opf=opf,
        members={"OEBPS/chapter.xhtml": xhtml_document("<p>Legal chapter.</p>")},
    )

    extracted = EpubExtractor().extract(source, FINGERPRINT)

    assert extracted.metadata.title == "Legal title"
    assert [chapter.content for chapter in extracted.chapters] == ["Legal chapter."]


def test_epub_rejects_opf_elements_outside_their_required_direct_hierarchy(
    tmp_path: Path,
) -> None:
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf"
         xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0">
  <wrapper>
    <metadata><dc:title>Misplaced</dc:title></metadata>
    <manifest><item id="chapter" href="chapter.xhtml"
      media-type="application/xhtml+xml"/></manifest>
    <spine><itemref idref="chapter"/></spine>
  </wrapper>
</package>"""
    source = write_epub(
        tmp_path / "misplaced-opf.epub",
        opf=opf,
        members={"OEBPS/chapter.xhtml": xhtml_document("<p>Must not parse.</p>")},
    )

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


@pytest.mark.parametrize("foreign_root", ["container", "package"])
def test_epub_rejects_foreign_namespace_container_and_package_roots(
    tmp_path: Path, foreign_root: str
) -> None:
    opf = opf_document(
        manifest='<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>',
        spine='<itemref idref="c"/>',
    )
    container: str | None = None
    if foreign_root == "container":
        container = """<?xml version="1.0"?>
<container xmlns="urn:foreign" version="1.0"><rootfiles>
  <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
</rootfiles></container>"""
    else:
        opf = opf.replace('xmlns="http://www.idpf.org/2007/opf"', 'xmlns="urn:foreign"')
    source = write_epub(
        tmp_path / "foreign-root.epub",
        opf=opf,
        container=container,
        members={"OEBPS/c.xhtml": xhtml_document("<p>Must not parse.</p>")},
    )

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


def test_epub_without_navigation_uses_heading_then_deterministic_fallback(tmp_path: Path) -> None:
    opf = opf_document(
        manifest="""
          <item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>
          <item id="b" href="b.xhtml" media-type="application/xhtml+xml"/>
        """,
        spine='<itemref idref="a"/><itemref idref="b"/>',
    )
    source = write_epub(
        tmp_path / "fallback.epub",
        opf=opf,
        members={
            "OEBPS/a.xhtml": xhtml_document("<h2>Document Heading</h2><p>One.</p>"),
            "OEBPS/b.xhtml": xhtml_document("<p>Two.</p>"),
        },
    )

    extracted = EpubExtractor().extract(source, FINGERPRINT)

    assert [chapter.title for chapter in extracted.chapters] == [
        "Document Heading",
        "Chapter 2",
    ]


def test_epub_two_ncx_labels_are_used_for_epub2_titles(tmp_path: Path) -> None:
    opf = opf_document(
        version="2.0",
        manifest="""
          <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
          <item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>
        """,
        spine='<itemref idref="c"/>',
        spine_attributes='toc="ncx"',
    )
    ncx = """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint id="n1">
  <navLabel><text>NCX Chapter</text></navLabel><content src="c.xhtml#part"/>
</navPoint></navMap></ncx>"""
    source = write_epub(
        tmp_path / "epub2.epub",
        opf=opf,
        members={
            "OEBPS/toc.ncx": ncx,
            "OEBPS/c.xhtml": xhtml_document("<h1>Heading</h1>"),
        },
    )

    extracted = EpubExtractor().extract(source, FINGERPRINT)

    assert extracted.chapters[0].title == "NCX Chapter"


def test_epub3_navigation_ignores_foreign_local_name_impersonators(tmp_path: Path) -> None:
    opf = opf_document(
        manifest="""
          <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
          <item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>
        """,
        spine='<itemref idref="c"/>',
    )
    nav = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops" xmlns:foreign="urn:foreign">
  <body>
    <foreign:nav epub:type="toc"><foreign:a href="c.xhtml">Poisoned nav</foreign:a></foreign:nav>
    <nav epub:type="toc"><ol><li><a href="c.xhtml">Legal nav</a></li></ol></nav>
  </body>
</html>"""
    source = write_epub(
        tmp_path / "foreign-nav.epub",
        opf=opf,
        members={
            "OEBPS/nav.xhtml": nav,
            "OEBPS/c.xhtml": xhtml_document("<h1>Heading</h1>"),
        },
    )

    extracted = EpubExtractor().extract(source, FINGERPRINT)

    assert extracted.chapters[0].title == "Legal nav"


def test_epub2_ncx_ignores_foreign_local_name_impersonators(tmp_path: Path) -> None:
    opf = opf_document(
        version="2.0",
        manifest="""
          <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
          <item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>
        """,
        spine='<itemref idref="c"/>',
        spine_attributes='toc="ncx"',
    )
    ncx = """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" xmlns:foreign="urn:foreign">
  <navMap>
    <foreign:navPoint><foreign:navLabel><foreign:text>Poisoned NCX</foreign:text></foreign:navLabel>
      <foreign:content src="c.xhtml"/></foreign:navPoint>
    <navPoint id="legal"><navLabel><text>Legal NCX</text></navLabel><content src="c.xhtml"/></navPoint>
  </navMap>
</ncx>"""
    source = write_epub(
        tmp_path / "foreign-ncx.epub",
        opf=opf,
        members={
            "OEBPS/toc.ncx": ncx,
            "OEBPS/c.xhtml": xhtml_document("<h1>Heading</h1>"),
        },
    )

    extracted = EpubExtractor().extract(source, FINGERPRINT)

    assert extracted.chapters[0].title == "Legal NCX"


def test_epub_skips_non_linear_spine_documents(tmp_path: Path) -> None:
    opf = opf_document(
        manifest="""
          <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
          <item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>
        """,
        spine='<itemref idref="nav" linear="no"/><itemref idref="c"/>',
    )
    source = write_epub(
        tmp_path / "linear.epub",
        opf=opf,
        members={
            "OEBPS/nav.xhtml": xhtml_document('<nav epub:type="toc"><a href="c.xhtml">C</a></nav>'),
            "OEBPS/c.xhtml": xhtml_document("<p>Chapter.</p>"),
        },
    )

    extracted = EpubExtractor().extract(source, FINGERPRINT)

    assert len(extracted.chapters) == 1
    assert extracted.chapters[0].content == "Chapter."


def test_xhtml_conversion_keeps_readable_structures_and_local_footnotes(tmp_path: Path) -> None:
    body = """
      <script>steal()</script><style>.hidden {}</style><form>secret</form>
      <h1>Structures</h1><p>First <strong>paragraph</strong>.</p>
      <ul><li>Alpha</li><li>Beta</li></ul>
      <ol><li>First</li><li>Second</li></ol>
      <pre><code>value = 1\nprint(value)</code></pre>
      <table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>1</td></tr></table>
      <blockquote><p>Quoted thought.</p></blockquote>
      <p>Claim<a epub:type="noteref" href="#fn1">1</a>.</p>
      <aside id="fn1" epub:type="footnote"><p>Footnote detail.</p></aside>
    """
    source = basic_epub(tmp_path / "structures.epub", chapter_body=body)

    content = EpubExtractor().extract(source, FINGERPRINT).chapters[0].content

    assert "# Structures" in content
    assert "First paragraph." in content
    assert "- Alpha" in content and "- Beta" in content
    assert "1. First" in content and "2. Second" in content
    assert "```\nvalue = 1\nprint(value)\n```" in content
    assert "| Name | Value |" in content and "| A | 1 |" in content
    assert "> Quoted thought." in content
    assert "Claim[1]." in content and "[1] Footnote detail." in content
    assert "steal" not in content and "hidden" not in content and "secret" not in content


def test_epub_fails_when_no_readable_spine_content_remains(tmp_path: Path) -> None:
    source = basic_epub(
        tmp_path / "empty.epub",
        chapter_body="<script>only()</script><style>nothing</style>",
    )

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


def test_epub_maps_excessively_deep_xhtml_to_safe_failure(tmp_path: Path) -> None:
    depth = 1_500
    body = f"{'<div>' * depth}<p>private deep content</p>{'</div>' * depth}"
    source = basic_epub(tmp_path / "deep.epub", chapter_body=body)

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED
    assert "private deep content" not in str(exc_info.value)


@pytest.mark.parametrize("unsafe_name", ["/absolute.xhtml", "../parent.xhtml", "a/../../escape"])
def test_epub_rejects_absolute_and_parent_archive_names(tmp_path: Path, unsafe_name: str) -> None:
    source = basic_epub(tmp_path / "unsafe-path.epub")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(unsafe_name, "hostile")

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED
    assert unsafe_name not in str(exc_info.value)


def test_epub_rejects_backslash_archive_names(tmp_path: Path) -> None:
    source = basic_epub(tmp_path / "backslash.epub")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("OEBPS\\ambiguous.xhtml", "hostile")

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


@pytest.mark.parametrize(
    ("location", "reference"),
    [
        ("container", "/OEBPS/content.opf"),
        ("container", "%2fOEBPS/content.opf"),
        ("manifest", "../../outside.xhtml"),
        ("manifest", "..%2f..%2foutside.xhtml"),
        ("nav", "..\\chapter.xhtml"),
        ("nav", "https://attacker.invalid/chapter.xhtml"),
        ("nav", "//attacker.invalid/chapter.xhtml"),
        ("nav", "%68%74%74%70%3a//attacker.invalid/chapter.xhtml"),
        ("ncx", "..%5cchapter.xhtml"),
        ("ncx", "../../outside.xhtml"),
    ],
)
def test_epub_rejects_unsafe_archive_references_without_leaking_them(
    tmp_path: Path, location: str, reference: str
) -> None:
    source = _unsafe_reference_epub(tmp_path, location=location, reference=reference)

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED
    assert reference not in str(exc_info.value)


def test_epub_rejects_encrypted_members_with_specific_safe_code(tmp_path: Path) -> None:
    source = basic_epub(tmp_path / "encrypted.epub")
    patch_all_entries_encrypted(source)

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.ENCRYPTED_DOCUMENT
    assert str(source) not in str(exc_info.value)


def test_epub_rejects_unix_symlink_entries(tmp_path: Path) -> None:
    link = zipfile.ZipInfo("OEBPS/link.xhtml")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    opf = opf_document(
        manifest='<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>',
        spine='<itemref idref="c"/>',
    )
    source = write_epub(
        tmp_path / "symlink.epub",
        opf=opf,
        members={"OEBPS/c.xhtml": xhtml_document("<p>Readable.</p>")},
        extra_infos=((link, b"c.xhtml"),),
    )

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


@pytest.mark.parametrize("nested_name", ["OEBPS/payload.zip", "OEBPS/second.epub"])
def test_epub_rejects_nested_archives(tmp_path: Path, nested_name: str) -> None:
    source = basic_epub(tmp_path / "nested-archive.epub")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(nested_name, b"PK\x03\x04payload")

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


@pytest.mark.parametrize(
    ("nested_name", "payload"),
    [
        ("OEBPS/disguised.apk", b"package-like member"),
        ("OEBPS/disguised.bin", _nested_zip_payload()),
        ("OEBPS/disguised.data", b"\x1f\x8b\x08\x00bounded-prefix"),
    ],
)
def test_epub_rejects_nested_packages_by_extended_suffix_or_bounded_magic(
    tmp_path: Path, nested_name: str, payload: bytes
) -> None:
    source = basic_epub(tmp_path / "disguised-nested-package.epub")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(nested_name, payload)

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED
    assert nested_name not in str(exc_info.value)


def test_epub_rejects_member_count_before_reading_content(tmp_path: Path) -> None:
    source = basic_epub(tmp_path / "count.epub")

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor(limits=ExtractionLimits(max_zip_members=3)).extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


def test_epub_rejects_oversized_individual_member(tmp_path: Path) -> None:
    source = basic_epub(tmp_path / "member-size.epub", chapter_body="<p>1234567890</p>")

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor(limits=ExtractionLimits(max_zip_member_bytes=10)).extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


def test_epub_rejects_oversized_total_expansion(tmp_path: Path) -> None:
    source = basic_epub(tmp_path / "total-size.epub")

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor(limits=ExtractionLimits(max_expanded_zip_bytes=100)).extract(
            source, FINGERPRINT
        )

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


def test_epub_rejects_excessive_compression_ratio(tmp_path: Path) -> None:
    source = basic_epub(tmp_path / "ratio.epub", chapter_body=f"<p>{'A' * 10_000}</p>")

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor(limits=ExtractionLimits(max_compression_ratio=2)).extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


@pytest.mark.parametrize(
    ("container", "opf"),
    [
        ("<container>", "unused"),
        (None, "<package>"),
    ],
)
def test_epub_maps_malformed_container_and_opf_to_safe_failure(
    tmp_path: Path, container: str | None, opf: str
) -> None:
    kwargs: dict[str, object] = {
        "opf": opf,
        "members": {},
    }
    if container is not None:
        kwargs["container"] = container
    source = write_epub(tmp_path / "malformed.epub", **kwargs)  # type: ignore[arg-type]

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED
    assert "<package>" not in str(exc_info.value)


def test_epub_fails_safely_when_a_spine_document_is_missing(tmp_path: Path) -> None:
    opf = opf_document(
        manifest='<item id="missing" href="private-name.xhtml" media-type="application/xhtml+xml"/>',
        spine='<itemref idref="missing"/>',
    )
    source = write_epub(tmp_path / "missing.epub", opf=opf, members={})

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED
    assert "private-name.xhtml" not in str(exc_info.value)


def test_zip_preflight_finishes_before_any_xhtml_member_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = basic_epub(tmp_path / "preflight.epub")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("../late-hostile-entry", "hostile")
    original_read = zipfile.ZipFile.read

    def reject_xhtml_read(
        archive: zipfile.ZipFile, name: str | zipfile.ZipInfo, pwd: bytes | None = None
    ) -> bytes:
        member_name = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if member_name.endswith(".xhtml"):
            raise AssertionError("XHTML was read before archive preflight completed")
        return original_read(archive, name, pwd)

    monkeypatch.setattr(zipfile.ZipFile, "read", reject_xhtml_read)

    with pytest.raises(ForgeException) as exc_info:
        EpubExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


def test_default_registry_exposes_the_epub_extractor(tmp_path: Path) -> None:
    source = basic_epub(tmp_path / "registered.epub")

    extracted = BookExtractorRegistry().extract(source)

    assert extracted.format is BookFormat.EPUB
    assert extracted.source_fingerprint != FINGERPRINT
