from pydantic import ConfigDict, Field

from cove_book_forge.contracts.base import ContractModel


class ObsidianPublishResult(ContractModel):
    """Public result returned after a managed Obsidian publication."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    book_key: str = Field(min_length=16, max_length=16, pattern=r"^[0-9a-f]{16}$")
    chapter_path: str = Field(min_length=1, max_length=500)
    moc_path: str = Field(min_length=1, max_length=500)
    card_paths: tuple[str, ...] = ()
    input_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    changed_paths: tuple[str, ...] = ()
    unchanged: bool = False
