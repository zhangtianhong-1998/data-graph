"""Portable exact-match Gold and result contracts; no model input contract imports."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class GoldNode(Closed):
    code: str = Field(min_length=1)
    kind: str | None = None
    english_names: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    positions: list[int] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)


class GoldEdge(Closed):
    source: str
    target: str
    relation: str = 'parent_of'


class GoldGraph(Closed):
    version: Literal['lightgraph.gold/1'] = 'lightgraph.gold/1'
    nodes: list[GoldNode]
    edges: list[GoldEdge] = Field(default_factory=list)
    code_property: str = 'business_code'
    namespace_column: str = '经营主体'
    entity_kinds: list[str] = Field(default_factory=list)
    # Mapping is fixed before scoring; never infer direction by maximizing fit.
    parent_relations: list[str] = Field(default_factory=lambda: ['parent_of', 'contains'])
    child_relations: list[str] = Field(default_factory=lambda: ['child_of'])

    @model_validator(mode='after')
    def check_gold(self):
        codes = {n.code for n in self.nodes}
        if len(codes) != len(self.nodes):
            raise ValueError('duplicate_gold_code')
        if set(self.parent_relations) & set(self.child_relations):
            raise ValueError('ambiguous_gold_direction')
        pairs = {(e.source, e.target, e.relation) for e in self.edges}
        if len(pairs) != len(self.edges) or any(e.source not in codes or e.target not in codes for e in self.edges):
            raise ValueError('invalid_gold_edges')
        return self


class Score(Closed):
    true_positive: int
    false_positive: int
    false_negative: int
    predicted: int
    expected: int
    precision: float | None
    recall: float | None
    f1: float | None
    exact: bool


class ModeReport(Closed):
    statuses: list[str]
    identities: Score
    edges: Score
    english_names: Score
    labels: Score
    positions: Score
    subject_verified_identities: Score
    duplicates: dict[str, list[str]]
    duplicate_node_ids: list[str] = Field(default_factory=list)
    merged_node_ids: list[str]
    unmapped_node_ids: list[str]
    unknown_codes: list[str]
    invalid_endpoint_edges: int
    duplicate_edge_instances: int
    unverified_scope_node_ids: list[str]
    wrong_scope_node_ids: list[str]


class Costs(Closed):
    calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    elapsed_seconds: float | None = None


class EvaluationReport(Closed):
    version: Literal['lightgraph.evaluation/1'] = 'lightgraph.evaluation/1'
    status: Literal['SCORED', 'GRAPH_FILE_MISSING']
    method: str = 'Exact explicit codes; unique identity only; no pooling across split entities; candidate facts excluded from accepted metrics.'
    graph_sha256: str | None = None
    gold_sha256: str
    accepted: ModeReport
    accepted_plus_candidates: ModeReport
    costs: Costs = Field(default_factory=Costs)
    run_status: str | None = None
    issue_counts: dict[str, int] = Field(default_factory=dict)
    unresolved_counts: dict[str, int] = Field(default_factory=dict)
    namespace_evidence: str
    acceptance_boundary: str = 'Scores measure only the supplied Gold and declared relation directions; they do not independently validate Gold business meaning or concept decomposition.'
