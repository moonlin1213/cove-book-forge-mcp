from cove_book_forge import __version__


def test_package_version_matches_first_development_release() -> None:
    assert __version__ == "0.1.0.dev0"
