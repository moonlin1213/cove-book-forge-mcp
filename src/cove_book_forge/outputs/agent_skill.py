"""Public synchronous Agent Skill output service."""

from cove_book_forge.config import SkillOutputConfig
from cove_book_forge.contracts import AnalyzedChapter, ChapterSnapshot, SkillPublishResult
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs.skill_install import SkillInstaller
from cove_book_forge.outputs.skill_publisher import CanonicalSkillPublisher
from cove_book_forge.outputs.skill_render import AgentSkillRenderer


class AgentSkillOutput:
    """Render, canonically publish, then install one already analyzed chapter."""

    def __init__(self, config: SkillOutputConfig) -> None:
        self._renderer = AgentSkillRenderer(config)
        self._publisher = CanonicalSkillPublisher(config)
        self._installer = SkillInstaller(config)

    def publish(
        self,
        snapshot: ChapterSnapshot,
        analyzed: AnalyzedChapter,
    ) -> SkillPublishResult:
        try:
            rendered = self._renderer.render(snapshot, analyzed, None)
            receipt = self._publisher.publish(rendered)
            installations = self._installer.install(receipt)
            return SkillPublishResult(
                book_key=rendered.manifest.book_key,
                skill_slug=rendered.skill_slug,
                canonical_path=receipt.canonical_path,
                chapter_path=rendered.chapter_path,
                input_fingerprint=analyzed.input_fingerprint,
                changed_paths=receipt.changed_paths,
                installations=installations,
                unchanged=receipt.unchanged and all(item.unchanged for item in installations),
            )
        except ForgeException as exc:
            if exc.code in {
                ForgeErrorCode.OUTPUT_NOT_CONFIGURED,
                ForgeErrorCode.PATH_NOT_ALLOWED,
                ForgeErrorCode.OUTPUT_PERMISSION_DENIED,
                ForgeErrorCode.EXTERNAL_MODIFICATION,
                ForgeErrorCode.INSTALL_CONFLICT,
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
