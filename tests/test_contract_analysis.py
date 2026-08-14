import pytest
from pydantic import ValidationError

from cove_book_forge.contracts.analysis import ChapterAnalysis, Framework


def test_analysis_requires_actionable_core_content() -> None:
    analysis = ChapterAnalysis(
        core_idea="Choose the smallest reversible action.",
        frameworks=[
            Framework(
                name="Reversible step",
                when_to_use="When uncertainty is high.",
                how=("Reduce scope.", "Observe the result."),
                why="It limits downside.",
            )
        ],
        key_takeaways=("Prefer reversible commitments.",),
    )
    assert analysis.frameworks[0].how == ("Reduce scope.", "Observe the result.")


def test_analysis_rejects_an_empty_core_idea() -> None:
    with pytest.raises(ValidationError):
        ChapterAnalysis(core_idea="")
