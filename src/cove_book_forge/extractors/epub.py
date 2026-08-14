from __future__ import annotations

import lzma
import posixpath
import re
import stat
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree  # type: ignore[import-untyped]

from cove_book_forge.contracts import BookFormat, BookMetadata, ChapterContent, ExtractedBook
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.extractors.sanitize import sanitize_text
from cove_book_forge.extractors.security import ExtractionLimits
from cove_book_forge.extractors.xhtml import extract_xhtml

_CONTAINER_MEMBER = "META-INF/container.xml"
_CONTAINER_NAMESPACE = "urn:oasis:names:tc:opendocument:xmlns:container"
_DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
_EPUB_NAMESPACE = "http://www.idpf.org/2007/ops"
_NCX_NAMESPACE = "http://www.daisy.org/z3986/2005/ncx/"
_OPF_NAMESPACE = "http://www.idpf.org/2007/opf"
_XHTML_NAMESPACE = "http://www.w3.org/1999/xhtml"
_ARCHIVE_MAGIC_BYTES = 512
_ARCHIVE_MAGIC_PREFIXES = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07",
    b"\x1f\x8b",
    b"BZh",
    b"\xfd7zXZ\x00",
    b"\x28\xb5\x2f\xfd",
    b"!<arch>\n",
    b"MSCF",
    b"xar!",
)
_NESTED_ARCHIVE_SUFFIXES = frozenset(
    {
        ".7z",
        ".apk",
        ".bz2",
        ".cab",
        ".cbz",
        ".deb",
        ".docx",
        ".epub",
        ".gz",
        ".ipa",
        ".jar",
        ".odp",
        ".ods",
        ".odt",
        ".pptx",
        ".rar",
        ".rpm",
        ".tar",
        ".tgz",
        ".txz",
        ".whl",
        ".xlsx",
        ".xpi",
        ".xz",
        ".zip",
        ".zst",
    }
)
_XHTML_MEDIA_TYPE = "application/xhtml+xml"
_NCX_MEDIA_TYPE = "application/x-dtbncx+xml"
_SCHEME_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True, slots=True)
class _ManifestItem:
    member: str
    media_type: str
    properties: frozenset[str]


def _failure(code: ForgeErrorCode = ForgeErrorCode.EXTRACTION_FAILED) -> ForgeException:
    return ForgeException(code, "EPUB extraction failed.")


def _qname(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _normalized_member_name(name: str) -> str:
    if not name or "\\" in name or name.startswith("/"):
        raise _failure()
    path = PurePosixPath(name)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise _failure()
    if path.parts and path.parts[0].endswith(":"):
        raise _failure()
    normalized = posixpath.normpath(name)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise _failure()
    return normalized.rstrip("/")


def _preflight(archive: zipfile.ZipFile, limits: ExtractionLimits) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > limits.max_zip_members:
        raise _failure()
    expanded_bytes = 0
    members: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        member = _normalized_member_name(info.filename)
        if info.flag_bits & 1:
            raise _failure(ForgeErrorCode.ENCRYPTED_DOCUMENT)
        unix_mode = info.external_attr >> 16
        if info.create_system == 3 and stat.S_ISLNK(unix_mode):
            raise _failure()
        if not info.is_dir() and PurePosixPath(member).suffix.lower() in _NESTED_ARCHIVE_SUFFIXES:
            raise _failure()
        if info.file_size > limits.max_zip_member_bytes:
            raise _failure()
        expanded_bytes += info.file_size
        if expanded_bytes > limits.max_expanded_zip_bytes:
            raise _failure()
        if info.file_size > info.compress_size * limits.max_compression_ratio:
            raise _failure()
        if member in members:
            raise _failure()
        members[member] = info
    _reject_nested_archive_magic(archive, members)
    return members


def _reject_nested_archive_magic(
    archive: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo]
) -> None:
    for info in members.values():
        if info.is_dir() or info.file_size == 0:
            continue
        try:
            with archive.open(info) as stream:
                prefix = stream.read(_ARCHIVE_MAGIC_BYTES)
        except (
            EOFError,
            OSError,
            RuntimeError,
            lzma.LZMAError,
            zipfile.BadZipFile,
            zlib.error,
        ) as exc:
            raise _failure() from exc
        if prefix.startswith(_ARCHIVE_MAGIC_PREFIXES) or (
            len(prefix) >= 262 and prefix[257:262] == b"ustar"
        ):
            raise _failure()


def _resolve_reference(base_member: str, reference: str) -> str:
    split = urlsplit(reference)
    if split.scheme or split.netloc:
        raise _failure()
    path = unquote(split.path)
    if (
        not path
        or "\\" in path
        or path.startswith("/")
        or path.startswith("//")
        or _SCHEME_PREFIX.match(path)
    ):
        raise _failure()
    base_directory = posixpath.dirname(base_member) if base_member else ""
    candidate = posixpath.normpath(posixpath.join(base_directory, path))
    if candidate in {"", ".", ".."} or candidate.startswith("../") or candidate.startswith("/"):
        raise _failure()
    return _normalized_member_name(candidate)


def _read_member(
    archive: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo], member: str
) -> bytes:
    info = members.get(member)
    if info is None or info.is_dir():
        raise _failure()
    try:
        return archive.read(info)
    except RuntimeError as exc:
        if info.flag_bits & 1:
            raise _failure(ForgeErrorCode.ENCRYPTED_DOCUMENT) from exc
        raise _failure() from exc


def _parse_xml(payload: bytes) -> Element:
    element: Element = ElementTree.fromstring(payload)
    return element


def _first_direct_text(parent: Element, tag: str) -> str:
    for element in parent:
        if element.tag != tag:
            continue
        value = sanitize_text(" ".join("".join(element.itertext()).split()))
        if value:
            return value
    return ""


def _required_direct_child(parent: Element, tag: str) -> Element:
    matches = [element for element in parent if element.tag == tag]
    if len(matches) != 1:
        raise _failure()
    return matches[0]


def _parse_container(payload: bytes) -> str:
    root = _parse_xml(payload)
    if root.tag != _qname(_CONTAINER_NAMESPACE, "container"):
        raise _failure()
    rootfiles = _required_direct_child(root, _qname(_CONTAINER_NAMESPACE, "rootfiles"))
    rootfile = next(
        (
            element
            for element in rootfiles
            if element.tag == _qname(_CONTAINER_NAMESPACE, "rootfile")
        ),
        None,
    )
    if rootfile is None:
        raise _failure()
    return _resolve_reference("", rootfile.get("full-path", ""))


def _parse_package(
    payload: bytes, opf_member: str
) -> tuple[BookMetadata, dict[str, _ManifestItem], list[tuple[str, bool]], str]:
    root = _parse_xml(payload)
    if root.tag != _qname(_OPF_NAMESPACE, "package"):
        raise _failure()
    metadata_node = _required_direct_child(root, _qname(_OPF_NAMESPACE, "metadata"))
    manifest_node = _required_direct_child(root, _qname(_OPF_NAMESPACE, "manifest"))
    spine_node = _required_direct_child(root, _qname(_OPF_NAMESPACE, "spine"))
    title = _first_direct_text(metadata_node, _qname(_DC_NAMESPACE, "title"))
    if not title:
        raise _failure()
    metadata = BookMetadata(
        title=title,
        author=_first_direct_text(metadata_node, _qname(_DC_NAMESPACE, "creator")),
        language=_first_direct_text(metadata_node, _qname(_DC_NAMESPACE, "language")),
    )
    manifest: dict[str, _ManifestItem] = {}
    spine: list[tuple[str, bool]] = []
    for element in manifest_node:
        if element.tag != _qname(_OPF_NAMESPACE, "item"):
            continue
        item_id = element.get("id", "")
        href = element.get("href", "")
        if not item_id or not href or item_id in manifest:
            raise _failure()
        manifest[item_id] = _ManifestItem(
            member=_resolve_reference(opf_member, href),
            media_type=element.get("media-type", ""),
            properties=frozenset(element.get("properties", "").split()),
        )
    for element in spine_node:
        if element.tag != _qname(_OPF_NAMESPACE, "itemref"):
            continue
        idref = element.get("idref", "")
        if not idref:
            raise _failure()
        spine.append((idref, element.get("linear", "yes").lower() != "no"))
    if not manifest or not spine:
        raise _failure()
    return metadata, manifest, spine, spine_node.get("toc", "")


def _navigation_labels(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    manifest: dict[str, _ManifestItem],
    toc_id: str,
) -> dict[str, str]:
    nav_item = next((item for item in manifest.values() if "nav" in item.properties), None)
    if nav_item is not None:
        return _epub3_navigation_labels(
            _parse_xml(_read_member(archive, members, nav_item.member)), nav_item.member
        )
    ncx_item = manifest.get(toc_id) if toc_id else None
    if ncx_item is None:
        ncx_item = next(
            (item for item in manifest.values() if item.media_type == _NCX_MEDIA_TYPE), None
        )
    if ncx_item is not None:
        return _ncx_navigation_labels(
            _parse_xml(_read_member(archive, members, ncx_item.member)), ncx_item.member
        )
    return {}


def _epub3_navigation_labels(root: Element, nav_member: str) -> dict[str, str]:
    if root.tag != _qname(_XHTML_NAMESPACE, "html"):
        raise _failure()
    labels: dict[str, str] = {}
    toc_nodes = [
        element
        for element in root.iter(_qname(_XHTML_NAMESPACE, "nav"))
        if "toc" in element.get(_qname(_EPUB_NAMESPACE, "type"), "").split()
    ]
    for toc in toc_nodes:
        for anchor in toc.iter(_qname(_XHTML_NAMESPACE, "a")):
            href = anchor.get("href", "")
            label = sanitize_text(" ".join("".join(anchor.itertext()).split()))
            if href and label:
                labels.setdefault(_resolve_reference(nav_member, href), label)
    return labels


def _ncx_navigation_labels(root: Element, ncx_member: str) -> dict[str, str]:
    if root.tag != _qname(_NCX_NAMESPACE, "ncx"):
        raise _failure()
    nav_map = _required_direct_child(root, _qname(_NCX_NAMESPACE, "navMap"))
    labels: dict[str, str] = {}
    for point in nav_map.iter(_qname(_NCX_NAMESPACE, "navPoint")):
        nav_label = next(
            (child for child in point if child.tag == _qname(_NCX_NAMESPACE, "navLabel")),
            None,
        )
        content = next(
            (child for child in point if child.tag == _qname(_NCX_NAMESPACE, "content")),
            None,
        )
        label_node = (
            next(
                (child for child in nav_label if child.tag == _qname(_NCX_NAMESPACE, "text")),
                None,
            )
            if nav_label is not None
            else None
        )
        label = (
            sanitize_text(" ".join("".join(label_node.itertext()).split()))
            if label_node is not None
            else ""
        )
        source = content.get("src", "") if content is not None else ""
        if source and label:
            labels.setdefault(_resolve_reference(ncx_member, source), label)
    return labels


class EpubExtractor:
    def __init__(self, *, limits: ExtractionLimits | None = None) -> None:
        self._limits = limits or ExtractionLimits()

    def extract(self, source: Path, fingerprint: str) -> ExtractedBook:
        try:
            with zipfile.ZipFile(source) as archive:
                members = _preflight(archive, self._limits)
                opf_member = _parse_container(_read_member(archive, members, _CONTAINER_MEMBER))
                metadata, manifest, spine, toc_id = _parse_package(
                    _read_member(archive, members, opf_member), opf_member
                )
                labels = _navigation_labels(archive, members, manifest, toc_id)
                chapters: list[ChapterContent] = []
                for item_id, linear in spine:
                    item = manifest.get(item_id)
                    if item is None:
                        raise _failure()
                    if not linear:
                        continue
                    if item.member not in members:
                        raise _failure()
                    if item.media_type != _XHTML_MEDIA_TYPE:
                        continue
                    parsed = extract_xhtml(_read_member(archive, members, item.member))
                    if not parsed.content:
                        continue
                    chapter_index = len(chapters)
                    chapters.append(
                        ChapterContent(
                            index=chapter_index,
                            title=labels.get(item.member)
                            or parsed.heading
                            or f"Chapter {chapter_index + 1}",
                            content=parsed.content,
                            source_locator=f"epub:{item.member}",
                        )
                    )
                if not chapters:
                    raise _failure()
                return ExtractedBook(
                    format=BookFormat.EPUB,
                    metadata=metadata.model_copy(update={"total_chapters": len(chapters)}),
                    chapters=tuple(chapters),
                    source_fingerprint=fingerprint,
                )
        except ForgeException:
            raise
        except (
            EOFError,
            OSError,
            ParseError,
            RecursionError,
            ValueError,
            lzma.LZMAError,
            zipfile.BadZipFile,
            zlib.error,
        ) as exc:
            raise _failure() from exc
