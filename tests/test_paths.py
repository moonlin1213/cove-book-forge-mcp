from pathlib import Path

import pytest

from cove_book_forge.config.paths import AuthorizedPathPolicy
from cove_book_forge.errors import ForgeErrorCode, ForgeException


def test_resolve_target_stays_inside_authorized_root(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    policy = AuthorizedPathPolicy((root,))
    assert policy.resolve_target(root, "Books", "Safe.md") == root / "Books" / "Safe.md"


def test_resolve_target_rejects_traversal(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    policy = AuthorizedPathPolicy((root,))
    with pytest.raises(ForgeException) as caught:
        policy.resolve_target(root, "..", "outside.md")
    assert caught.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    private_path = str(caught.value.details["path"])
    public_result = caught.value.as_result()
    public_error = public_result["error"]
    assert private_path
    assert private_path not in str(public_result)
    assert isinstance(public_error, dict)
    assert public_error["details"] == {}


def test_resolve_target_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    policy = AuthorizedPathPolicy((root,))
    with pytest.raises(ForgeException):
        policy.resolve_target(root, "escape", "file.md")


@pytest.mark.parametrize("broad_root", [Path(Path.cwd().anchor), Path.home()])
def test_policy_rejects_filesystem_and_home_roots(broad_root: Path) -> None:
    with pytest.raises(ForgeException) as caught:
        AuthorizedPathPolicy((broad_root,))
    assert caught.value.code is ForgeErrorCode.PATH_NOT_ALLOWED


def test_policy_rejects_root_with_embedded_nul() -> None:
    with pytest.raises(ForgeException) as caught:
        AuthorizedPathPolicy((Path("invalid\x00root"),))
    assert caught.value.code is ForgeErrorCode.PATH_NOT_ALLOWED


@pytest.mark.parametrize("part", ["C:", "C:books", "D:", "D:books"])
def test_resolve_target_rejects_windows_drive_qualified_component(
    tmp_path: Path, part: str
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    policy = AuthorizedPathPolicy((root,))
    with pytest.raises(ForgeException) as caught:
        policy.resolve_target(root, part)
    assert caught.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
