"""Publish one already analyzed chapter as a local Agent Skill without a Provider call."""

from pathlib import Path
from tempfile import TemporaryDirectory

from cove_book_forge.config import SkillOutputConfig
from cove_book_forge.contracts import AnalyzedChapter, ChapterSnapshot
from cove_book_forge.outputs import AgentSkillOutput


def main() -> None:
    """Build a complete local fixture; applications pass their cached values instead."""
    snapshot = ChapterSnapshot.model_validate(
        {
            "source_system": "example-reader",
            "external_book_id": "durable-decisions",
            "book": {"title": "Durable Decisions", "total_chapters": 1},
            "chapter": {
                "index": 0,
                "title": "Reversible moves",
                "content": "Choose reversible decisions when evidence is incomplete.",
                "source_locator": "example:chapter:0",
            },
        }
    )
    analyzed = AnalyzedChapter.model_validate(
        {
            "analysis": {
                "core_idea": "Prefer reversible choices while uncertainty is high.",
                "concepts": [
                    {
                        "term": "Reversibility",
                        "definition": "A decision that can be changed at low cost.",
                    }
                ],
                "topic_tags": ["decision-making"],
            },
            "input_fingerprint": "0" * 64,
            "cache_hit": True,
        }
    )
    with TemporaryDirectory(prefix=".cove-book-forge-skill-", dir=Path.cwd()) as directory:
        canonical_root = Path(directory) / "generated-skills"
        canonical_root.mkdir()
        result = AgentSkillOutput(
            SkillOutputConfig(enabled=True, canonical_path=canonical_root)
        ).publish(snapshot, analyzed)
        print(f"Published {result.skill_slug} at {result.canonical_path}")


if __name__ == "__main__":
    main()
