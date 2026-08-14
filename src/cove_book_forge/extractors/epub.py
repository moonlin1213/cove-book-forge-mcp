from __future__ import annotations

import posixpath
import stat
import zipfile
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
_NESTED_ARCHIVE_SUFFIXES = frozenset({".cbz", ".epub", ".jar", ".zip"})
_XHTML_MEDIA_TYPE = "application/xhtml+xml"
_NCX_MEDIA_TYPE = "application/x-dtbncx+xml"


@dataclass(frozen=True, slots=True)
class _ManifestItem:
    member: str
    media_type: str
    properties: frozenset[str]


def _failure(code: ForgeErrorCode = ForgeErrorCode.EXTRACTION_FAILED) -> ForgeException:
    return ForgeException(code, "EPUB extraction failed.")


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


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
    return members


def _resolve_reference(base_member: str, reference: str) -> str:
    split = urlsplit(reference)
    if split.scheme or split.netloc:
        raise _failure()
    decoded = unquote(split.path)
    if not decoded or "\\" in decoded or decoded.startswith("/"):
        raise _failure()
    base_directory = posixpath.dirname(base_member) if base_member else ""
    candidate = posixpath.normpath(posixpath.join(base_directory, decoded))
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


def _first_descendant_text(root: Element, name: str) -> str:
    for element in root.iter():
        if _local_name(element.tag) == name:
            value = sanitize_text(" ".join("".join(element.itertext()).split()))
            if value:
                return value
    return ""


def _parse_container(payload: bytes) -> str:
    root = _parse_xml(payload)
    for element in root.iter():
        if _local_name(element.tag) == "rootfile":
            full_path = element.get("full-path", "")
            return _resolve_reference("", full_path)
    raise _failure()


def _parse_package(
    payload: bytes, opf_member: str
) -> tuple[BookMetadata, dict[str, _ManifestItem], list[tuple[str, bool]], str]:
    root = _parse_xml(payload)
    if _local_name(root.tag) != "package":
        raise _failure()
    title = _first_descendant_text(root, "title")
    if not title:
        raise _failure()
    metadata = BookMetadata(
        title=title,
        author=_first_descendant_text(root, "creator"),
        language=_first_descendant_text(root, "language"),
    )
    manifest: dict[str, _ManifestItem] = {}
    spine: list[tuple[str, bool]] = []
    toc_id = ""
    for element in root.iter():
        name = _local_name(element.tag)
        if name == "item":
            item_id = element.get("id", "")
            href = element.get("href", "")
            if not item_id or not href or item_id in manifest:
                raise _failure()
            manifest[item_id] = _ManifestItem(
                member=_resolve_reference(opf_member, href),
                media_type=element.get("media-type", ""),
                properties=frozenset(element.get("properties", "").split()),
            )
        elif name == "spine":
            toc_id = element.get("toc", "")
        elif name == "itemref":
            idref = element.get("idref", "")
            if not idref:
                raise _failure()
            spine.append((idref, element.get("linear", "yes").lower() != "no"))
    if not manifest or not spine:
        raise _failure()
    return metadata, manifest, spine, toc_id


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


def _attribute_by_local_name(element: Element, name: str) -> str:
    for attribute, value in element.attrib.items():
        if _local_name(attribute) == name:
            return value
    return ""


def _epub3_navigation_labels(root: Element, nav_member: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    toc_nodes = [
        element
        for element in root.iter()
        if _local_name(element.tag) == "nav"
        and "toc" in _attribute_by_local_name(element, "type").split()
    ]
    for toc in toc_nodes:
        for anchor in toc.iter():
            if _local_name(anchor.tag) != "a":
                continue
            href = anchor.get("href", "")
            label = sanitize_text(" ".join("".join(anchor.itertext()).split()))
            if href and label:
                labels.setdefault(_resolve_reference(nav_member, href), label)
    return labels


def _ncx_navigation_labels(root: Element, ncx_member: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for point in root.iter():
        if _local_name(point.tag) != "navPoint":
            continue
        label = ""
        source = ""
        for child in point.iter():
            if _local_name(child.tag) == "text" and not label:
                label = sanitize_text(" ".join("".join(child.itertext()).split()))
            elif _local_name(child.tag) == "content" and not source:
                source = child.get("src", "")
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
        except (OSError, ParseError, ValueError, zipfile.BadZipFile) as exc:
            raise _failure() from exc
