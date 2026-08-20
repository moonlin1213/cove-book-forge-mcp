"""Public synchronous Obsidian output service."""

from cove_book_forge.config import ObsidianOutputConfig
from cove_book_forge.contracts import AnalyzedChapter, ChapterSnapshot, ObsidianPublishResult
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs.obsidian_render import ObsidianRenderer
from cove_book_forge.outputs.publisher import GuardedPublisher


class ObsidianOutput:
    """Publish one already analyzed chapter without any Provider interaction."""

    def __init__(self, config: ObsidianOutputConfig) -> None:
        self._renderer = ObsidianRenderer(config)
        self._publisher = GuardedPublisher(config)

    def publish(
        self,
        snapshot: ChapterSnapshot,
        analyzed: AnalyzedChapter,
    ) -> ObsidianPublishResult:
        try:
            receipt = self._publisher.publish(
                lambda previous: self._renderer.render(snapshot, analyzed, previous)
            )
            rendered = receipt.rendered
            return ObsidianPublishResult(
                book_key=rendered.manifest.book_key,
                chapter_path=rendered.chapter_path,
                moc_path=rendered.moc_path,
                card_paths=rendered.card_paths,
                input_fingerprint=analyzed.input_fingerprint,
                changed_paths=receipt.changed_paths,
                unchanged=receipt.unchanged,
            )
        except ForgeException as exc:
            if exc.code in {
                ForgeErrorCode.OUTPUT_NOT_CONFIGURED,
                ForgeErrorCode.PATH_NOT_ALLOWED,
                ForgeErrorCode.OUTPUT_PERMISSION_DENIED,
                ForgeErrorCode.EXTERNAL_MODIFICATION,
            }:
                raise
            raise ForgeException(
                ForgeErrorCode.EXTERNAL_MODIFICATION,
                "output changed outside this application",
            ) from None
        except Exception:
            raise ForgeException(
                ForgeErrorCode.EXTERNAL_MODIFICATION,
                "output changed outside this application",
            ) from None
