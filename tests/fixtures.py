from __future__ import annotations

import struct
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def xhtml_document(body: str, *, title: str = "") -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>{title}</title></head>
  <body>{body}</body>
</html>
"""


def opf_document(
    *,
    title: str = "Synthetic Book",
    author: str = "Fixture Author",
    language: str = "en",
    manifest: str,
    spine: str,
    version: str = "3.0",
    spine_attributes: str = "",
) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="{version}"
         unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">fixture-book</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>{language}</dc:language>
  </metadata>
  <manifest>{manifest}</manifest>
  <spine {spine_attributes}>{spine}</spine>
</package>
"""


def write_epub(
    path: Path,
    *,
    opf: str | bytes,
    members: Mapping[str, str | bytes],
    opf_path: str = "OEBPS/content.opf",
    container: str | bytes | None = None,
    extra_infos: tuple[tuple[zipfile.ZipInfo, bytes], ...] = (),
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    if container is None:
        container = f"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="{opf_path}"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr(opf_path, opf)
        for name, content in members.items():
            archive.writestr(name, content)
        for info, content in extra_infos:
            archive.writestr(info, content)
    return path


def patch_all_entries_encrypted(path: Path) -> None:
    """Set the encryption bit in local and central headers without encrypting fixture bytes."""
    payload = bytearray(path.read_bytes())
    cursor = 0
    while (cursor := payload.find(b"PK\x03\x04", cursor)) != -1:
        flags = struct.unpack_from("<H", payload, cursor + 6)[0]
        struct.pack_into("<H", payload, cursor + 6, flags | 1)
        cursor += 4
    cursor = 0
    while (cursor := payload.find(b"PK\x01\x02", cursor)) != -1:
        flags = struct.unpack_from("<H", payload, cursor + 8)[0]
        struct.pack_into("<H", payload, cursor + 8, flags | 1)
        cursor += 4
    path.write_bytes(payload)


def basic_epub(path: Path, *, chapter_body: str = "<p>Readable.</p>") -> Path:
    opf = opf_document(
        manifest=('<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'),
        spine='<itemref idref="chapter"/>',
    )
    return write_epub(
        path,
        opf=opf,
        members={"OEBPS/chapter.xhtml": xhtml_document(chapter_body)},
    )


PdfLine = tuple[float, str]


def _pdf_literal(text: str) -> bytes:
    encoded = text.encode("latin-1")
    return encoded.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def write_pdf(
    path: Path,
    *,
    pages: Sequence[Sequence[PdfLine]],
    metadata: Mapping[str, str] | None = None,
    outline: Sequence[tuple[str, int | None]] = (),
    password: str | None = None,
) -> Path:
    """Write an original text-layer PDF fixture entirely at test runtime."""
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)  # noqa: SLF001 - pypdf fixture construction
    for lines in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        operators = []
        for y_position, line in lines:
            operators.append(
                b"BT /F1 12 Tf 72 "
                + str(y_position).encode("ascii")
                + b" Td ("
                + _pdf_literal(line)
                + b") Tj ET"
            )
        stream = DecodedStreamObject()
        stream.set_data(b"\n".join(operators))
        page[NameObject("/Contents")] = writer._add_object(stream)  # noqa: SLF001
    if metadata:
        writer.add_metadata({f"/{key}": value for key, value in metadata.items()})
    for title, page_number in outline:
        writer.add_outline_item(title, page_number)
    if password is not None:
        writer.encrypt(password)
    with path.open("wb") as stream:
        writer.write(stream)
    return path
