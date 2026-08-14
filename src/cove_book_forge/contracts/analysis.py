from pydantic import Field

from cove_book_forge.contracts.base import ContractModel


class EvidenceRef(ContractModel):
    locator: str = Field(min_length=1, max_length=500)
    note: str = Field(default="", max_length=1000)


class Framework(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    when_to_use: str = Field(default="", max_length=2000)
    how: tuple[str, ...] = ()
    why: str = Field(default="", max_length=2000)
    limitations: tuple[str, ...] = ()


class Concept(ContractModel):
    term: str = Field(min_length=1, max_length=200)
    definition: str = Field(min_length=1, max_length=4000)
    evidence_refs: tuple[EvidenceRef, ...] = ()


class MentalModel(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=4000)
    when_to_use: str = Field(default="", max_length=2000)


class Method(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    steps: tuple[str, ...] = ()
    when_to_use: str = Field(default="", max_length=2000)
    limitations: tuple[str, ...] = ()


class AntiPattern(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    why: str = Field(min_length=1, max_length=2000)
    alternative: str = Field(default="", max_length=2000)


class DecisionRule(ContractModel):
    rule: str = Field(min_length=1, max_length=2000)
    conditions: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()


class WorkedExample(ContractModel):
    title: str = Field(min_length=1, max_length=200)
    situation: str = Field(default="", max_length=3000)
    application: str = Field(default="", max_length=3000)
    result: str = Field(default="", max_length=3000)


class QualityWarning(ContractModel):
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1200)


class ChapterAnalysis(ContractModel):
    core_idea: str = Field(min_length=1, max_length=4000)
    frameworks: tuple[Framework, ...] = ()
    concepts: tuple[Concept, ...] = ()
    mental_models: tuple[MentalModel, ...] = ()
    methods: tuple[Method, ...] = ()
    anti_patterns: tuple[AntiPattern, ...] = ()
    decision_rules: tuple[DecisionRule, ...] = ()
    worked_examples: tuple[WorkedExample, ...] = ()
    key_takeaways: tuple[str, ...] = ()
    highlight_insights: tuple[str, ...] = ()
    annotation_insights: tuple[str, ...] = ()
    topic_tags: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    quality_warnings: tuple[QualityWarning, ...] = ()
