"""Validated retrieval contract shared by the project HTTP API and CLI."""
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from lightgraph.contracts import canonical


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

    mode: Literal['search', 'drilldown', 'auto', 'dimensional', 'traverse', 'hierarchy'] = 'search'
    query: str = Field(default='', max_length=4000)
    node_id: str | None = Field(default=None, min_length=1, max_length=512)
    field: Literal['all', 'name', 'description'] = 'all'
    method: Literal['hybrid', 'keyword', 'vector'] = 'hybrid'
    business_context: dict | None = Field(default=None, validation_alias=AliasChoices('applicability', 'business_context'))
    top_n: int = Field(default=20, ge=1, le=100)
    top_p_seeds: int = Field(default=4, ge=1, le=20)
    hops: int | None = Field(default=None, ge=0, le=2)
    max_per_rel: int = Field(default=30, ge=1, le=200)
    hub_degree_limit: int = Field(default=150, ge=10, le=5000)
    max_nodes: int = Field(default=80, ge=1, le=500)
    max_edges: int = Field(default=150, ge=1, le=1000)
    seed_ids: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(default_factory=list, max_length=32)
    target_id: str | None = Field(default=None, min_length=1, max_length=512)
    hierarchy_direction: Literal['children', 'parents'] = 'children'
    target_kind: str | None = Field(default=None, min_length=1, max_length=256)
    direction: Literal['outgoing', 'incoming', 'both'] = 'outgoing'
    relation_types: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(default_factory=list, max_length=64)
    include_endpoint_variants: bool = Field(default=False,
        description='Include visible endpoint specializations of a requested original relation. Exact type filtering is the default.')
    max_depth: int = Field(default=12, ge=1, le=64)

    conditions: list[Condition] = Field(default_factory=list, max_length=12)
    drill_role: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode='before')
    @classmethod
    def natural_language_default(cls, values):
        # A smaller output budget must not bypass project/condition recognition.
        if isinstance(values, dict) and 'applicability' in values:
            values = dict(values)
            selector = values.pop('applicability')
            if 'business_context' in values and canonical(values['business_context']) != canonical(selector):
                raise ValueError('conflicting_context_selectors')
            values['business_context'] = selector
        if isinstance(values, dict) and values.get('mode') == 'traverse' and 'direction' not in values:
            values = {**values, 'direction': 'both'}
        query_options = {'query', 'business_context', 'max_nodes', 'max_edges', 'max_depth', 'include_endpoint_variants'}
        if isinstance(values, dict) and 'query' in values and set(values) <= query_options:
            return {**values, 'mode': 'auto'}
        return values

    @model_validator(mode='after')
    def validate_lookup(self):
        options = self.model_fields_set
        if self.mode == 'hierarchy':
            if (bool(self.query.strip()) == bool(self.node_id) or options - {
                    'mode', 'query', 'node_id', 'business_context', 'hierarchy_direction', 'max_nodes', 'max_edges'}):
                raise ValueError('hierarchy_requires_one_anchor')
            return self
        if 'hierarchy_direction' in options:
            raise ValueError('hierarchy_direction_requires_hierarchy_mode')
        if self.include_endpoint_variants and not self.relation_types:
            raise ValueError('endpoint_variants_require_relation_types')
        ranking = {'field', 'method', 'top_n'}
        if self.mode == 'traverse':
            if bool(self.query.strip()) == bool(self.seed_ids):
                raise ValueError('traverse_requires_query_or_anchors')
            if options & ({'node_id', 'conditions', 'drill_role', 'hops', 'top_p_seeds', 'max_per_rel', 'hub_degree_limit'} | ranking):
                raise ValueError('unsupported_traversal_options')
            return self
        if self.direction == 'both' or 'target_kind' in options:
            raise ValueError('general_path_options_require_traverse')
        if self.mode == 'auto':
            if not self.query.strip() or options - {'mode', 'query', 'business_context', 'max_depth', 'max_nodes', 'max_edges',
                                                     'include_endpoint_variants'}:
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
        if self.mode == 'hierarchy':
            from .direct_hierarchy import retrieve_hierarchy
            return retrieve_hierarchy(retriever, self, scopes, term=self.query.strip(), direction=self.hierarchy_direction)
        if self.mode == 'auto':
            from .direct_hierarchy import hierarchy_question, retrieve_hierarchy
            question = hierarchy_question(self.query)
            if question:
                cohorts = retriever.authorized_cohorts(scopes, self.business_context)
                names = retriever.index.exact_names([self.query], scopes=cohorts, limit_per_term=1, include_codes=True)
                if not any(names.values()):
                    term, direction, requested_kind = question
                    return retrieve_hierarchy(retriever, self, scopes, term=term, direction=direction, requested_kind=requested_kind)
        if self.mode in {'auto', 'dimensional'} or (self.mode == 'traverse' and not self.seed_ids):
            from .planned import retrieve_planned
            return retrieve_planned(retriever, self, scopes)
        relations = self.relation_types
        if self.include_endpoint_variants:
            cohorts = retriever.authorized_cohorts(scopes, self.business_context)
            relations = retriever.index.expand_endpoint_relation_types(relations, cohorts)
        if self.mode == 'traverse' and self.seed_ids:
            from .traversal import retrieve_traversal
            return retrieve_traversal(retriever, self.query, scopes, business_context=self.business_context,
                seed_ids=self.seed_ids, direction=self.direction, relation_types=relations,
                target_id=self.target_id, target_kind=self.target_kind, max_depth=self.max_depth,
                max_nodes=self.max_nodes, max_edges=self.max_edges)
        if self.mode == 'drilldown':
            from .drilldown import retrieve_drilldown
            options = self.model_dump(exclude={'mode', 'hierarchy_direction', 'target_kind', 'node_id', 'hops', 'top_p_seeds', 'max_per_rel', 'hub_degree_limit', 'conditions', 'drill_role', 'relation_types', 'include_endpoint_variants'})
            return retrieve_drilldown(retriever, scopes=scopes, relation_types=relations, **options)
        options = self.model_dump(exclude={'mode', 'hierarchy_direction', 'target_kind', 'seed_ids', 'target_id', 'direction', 'relation_types', 'max_depth', 'conditions', 'drill_role', 'include_endpoint_variants'})
        return retriever.retrieve(scopes=scopes, **options)
