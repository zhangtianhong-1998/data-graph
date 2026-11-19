"""Validated retrieval contract shared by the project HTTP API and CLI."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Condition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: str | None = Field(default=None, min_length=1, max_length=120)
    kind: str | None = Field(default=None, min_length=1, max_length=256)
    text: str | None = Field(default=None, min_length=1, max_length=512)
    node_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode='after')
    def require_one_identifier(self):
        if bool(self.text and self.text.strip()) == bool(self.node_id):
            raise ValueError('condition_requires_text_or_node_id')
        return self


class RetrieveSubgraphRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)

    mode: Literal['search', 'drilldown', 'auto', 'dimensional'] = 'search'
    query: str = Field(default='', max_length=4000)
    node_id: str | None = Field(default=None, min_length=1, max_length=512)
    field: Literal['all', 'name', 'description'] = 'all'
    method: Literal['hybrid', 'keyword', 'vector'] = 'hybrid'
    business_context: dict | None = None
    top_n: int = Field(default=20, ge=1, le=100)
    top_p_seeds: int = Field(default=4, ge=1, le=20)
    hops: int | None = Field(default=None, ge=0, le=2)
    max_per_rel: int = Field(default=30, ge=1, le=200)
    hub_degree_limit: int = Field(default=150, ge=10, le=5000)
    max_nodes: int = Field(default=80, ge=1, le=500)
    max_edges: int = Field(default=150, ge=1, le=1000)
    seed_ids: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(default_factory=list, max_length=32)
    target_id: str | None = Field(default=None, min_length=1, max_length=512)
    direction: Literal['outgoing', 'incoming'] = 'outgoing'
    relation_types: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(default_factory=list, max_length=64)
    max_depth: int = Field(default=12, ge=1, le=64)

    conditions: list[Condition] = Field(default_factory=list, max_length=12)
    drill_role: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode='before')
    @classmethod
    def natural_language_default(cls, values):
        # A smaller output budget must not bypass project/condition recognition.
        query_options = {'query', 'business_context', 'max_nodes', 'max_edges', 'max_depth'}
        if isinstance(values, dict) and 'query' in values and set(values) <= query_options:
            return {**values, 'mode': 'auto'}
        return values

    @model_validator(mode='after')
    def validate_lookup(self):
        options = self.model_fields_set
        ranking = {'field', 'method', 'top_n'}
        if self.mode == 'auto':
            if not self.query.strip() or options - {'mode', 'query', 'business_context', 'max_depth', 'max_nodes', 'max_edges'}:
                raise ValueError('auto_requires_natural_language_query')
            return self
        if self.mode == 'dimensional':
            if len(self.conditions) < 2 and not self.query.strip():
                raise ValueError('dimensional_requires_conditions_or_query')
            if options & ({'seed_ids','target_id','node_id','hops','top_p_seeds','max_per_rel','hub_degree_limit'} | ranking):
                raise ValueError('unsupported_dimensional_options')
            if self.relation_types and not self.drill_role:
                raise ValueError('drill_relations_require_dimension')
            return self
        if options & {'conditions','drill_role'}:
            raise ValueError('conditions_require_dimensional')
        if self.mode == 'drilldown':
            if self.node_id or not (self.query.strip() or self.seed_ids):
                raise ValueError('drilldown_requires_query_or_seed_ids')
            if options & {'hops', 'top_p_seeds', 'max_per_rel', 'hub_degree_limit'}:
                raise ValueError('neighborhood_options_require_search')
            if self.seed_ids and options & ranking:
                raise ValueError('ranking_requires_query_seeds')
        else:
            if bool(self.query.strip()) == bool(self.node_id):
                raise ValueError('supply_query_or_node_id')
            if options & {'seed_ids', 'target_id', 'direction', 'relation_types', 'max_depth'}:
                raise ValueError('path_options_require_drilldown')
            if self.node_id and options & (ranking | {'top_p_seeds'}):
                raise ValueError('ranking_requires_query_seeds')
        return self

    def run(self, retriever, scopes):
        if self.mode in {'auto', 'dimensional'}:
            from .planned import retrieve_planned
            return retrieve_planned(retriever, self, scopes)
        if self.mode == 'drilldown':
            from .drilldown import retrieve_drilldown
            options = self.model_dump(exclude={'mode', 'node_id', 'hops', 'top_p_seeds', 'max_per_rel', 'hub_degree_limit', 'conditions', 'drill_role'})
            return retrieve_drilldown(retriever, scopes=scopes, **options)
        options = self.model_dump(exclude={'mode', 'seed_ids', 'target_id', 'direction', 'relation_types', 'max_depth', 'conditions', 'drill_role'})
        return retriever.retrieve(scopes=scopes, **options)
