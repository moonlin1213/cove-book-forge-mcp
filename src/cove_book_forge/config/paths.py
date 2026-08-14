from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from cove_book_forge.errors import ForgeErrorCode, ForgeException


def _is_within(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def _path_error(message: str, path: Path) -> ForgeException:
    return ForgeException(
        ForgeErrorCode.PATH_NOT_ALLOWED,
        message,
        details={"path": str(path)},
    )


@dataclass(frozen=True)
class AuthorizedPathPolicy:
    roots: tuple[Path, ...]

    def __post_init__(self) -> None:
        normalized = tuple(self.validate_root(root) for root in self.roots)
        if not normalized:
            raise _path_error("At least one authorized root is required.", Path("."))
        object.__setattr__(self, "roots", normalized)

    @staticmethod
    def validate_root(path: Path) -> Path:
        if "\x00" in str(path):
            raise _path_error("Authorized root contains an invalid path component.", path)
        try:
            root = path.expanduser().resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise _path_error("Authorized root does not exist.", path) from exc
        if not root.is_dir():
            raise _path_error("Authorized root must be a directory.", root)
        if root == Path(root.anchor) or root == Path.home().resolve():
            raise _path_error("Authorized root is too broad.", root)
        return root

    def resolve_target(self, root: Path, *parts: str) -> Path:
        normalized_root = self.validate_root(root)
        if normalized_root not in self.roots:
            raise _path_error("Root was not explicitly authorized.", normalized_root)

        current = normalized_root
        for part in parts:
            if (
                not part
                or part in {".", ".."}
                or "\x00" in part
                or "/" in part
                or "\\" in part
                or Path(part).is_absolute()
                or PureWindowsPath(part).drive
                or PureWindowsPath(part).anchor
            ):
                raise _path_error("Target contains an invalid path component.", current / part)
            candidate = current / part
            if candidate.exists() or candidate.is_symlink():
                try:
                    candidate = candidate.resolve(strict=True)
                except OSError as exc:
                    raise _path_error("Target path cannot be resolved.", candidate) from exc
                if not _is_within(candidate, normalized_root):
                    raise _path_error("Target escapes its authorized root.", candidate)
            current = candidate

        if not _is_within(current, normalized_root):
            raise _path_error("Target escapes its authorized root.", current)
        return current
