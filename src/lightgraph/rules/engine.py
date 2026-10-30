"""Deterministic projection and publication. Unfinished reads never retract prior support."""
from __future__ import annotations
import time
import json
import math
import uuid
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any
from ..contracts import (RuleSpec, TableSpec, GraphFact, SupportSpec, PartitionCompletion,
                         ValidationRecord, Coverage, canonical, digest)
from ..errors import LightGraphError
from ..semantic_schema import REFERENCE_SELF_LINK_CONTRACT_VERSION

@dataclass
class ExecutionResult:
    rule_id: str
    published: bool = False
    prepared: bool = False
    publication_id: str | None = None
    publication_ids: list[str] = field(default_factory=list)
    active_publication_ids: list[str] = field(default_factory=list)
    validation: ValidationRecord | None = None
    counts: dict[str, int] = field(default_factory=dict)
    codes: list[str] = field(default_factory=list)

class RuleEngine:
    """Validate a bounded, complete distinct projection before touching the current head."""
    def __init__(self, store, *, isolation_domain="default", max_working_bytes=32*1024*1024, max_partitions=256):
        self.store = store
        self.domain = isolation_domain
        self.max_working_bytes = max_working_bytes
        if not isinstance(max_partitions, int) or not 1 <= max_partitions <= 4096:
            raise LightGraphError("invalid_partition_limit")
        self.max_partitions = max_partitions
        self.bindings: dict[tuple[str, str], str] = {}

    def _table(self, adapter, rule):
        for t in adapter.describe():
            if t.source_id == rule.source.source_id and t.name == rule.source.table and t.schema_name == rule.source.schema_name:
                return t
        raise LightGraphError("source_permission_denied")

    @staticmethod
    def schema_fingerprint(table):
        return digest(table.model_dump(mode="json", by_alias=True))

    def columns(self, rule):
        cols = list(rule.scope.namespaces) + list(rule.scope.partition_by) + list(rule.identity.key_columns)
        if rule.concept_mapping:
            cols += [component.column for component in rule.concept_mapping.components]
            cols += [c.key_column for c in rule.concept_mapping.components if c.key_column]
            cols += [name for c in rule.concept_mapping.components for name in (c.namespace_columns or [])]
        cols += list(rule.identity.semantic_key_columns)
        cols += list(rule.attributes.single_valued.values()) + list(rule.attributes.multi_valued.values())
        if rule.hierarchy:
            for lv in rule.hierarchy.levels:
                cols += [lv.code] + ([lv.label] if lv.label else []) + lv.aliases
            if rule.hierarchy.parent_column:
                cols += [rule.hierarchy.parent_column]
        if rule.relation:
            cols += rule.relation.source_columns + rule.relation.target_columns
        return list(dict.fromkeys(cols))

    def _rows(self, adapter, table, columns, rule, *, predicate=None, limit=None):
        result=[]; size=0; start=time.monotonic()
        cap = rule.budget.max_rows_returned if limit is None else limit
        for batch in adapter.project(table, columns, predicate=predicate, distinct=True, limit=cap):
            try:
                for row in batch.rows():
                    item = dict(zip(batch.columns, row)) if not isinstance(row, dict) else dict(row)
                    size += len(canonical(item).encode())
                    if size > self.max_working_bytes:
                        raise LightGraphError("memory_budget_exhausted")
                    if len(result) >= cap:
                        raise LightGraphError("projection_truncated")
                    if time.monotonic()-start > rule.budget.max_seconds:
                        raise LightGraphError("query_timeout")
                    result.append(item)
            finally:
                if hasattr(batch, "release"): batch.release()
        return result

    def _partitions(self, adapter, table, rule):
        """Enumerate a complete, bounded partition set in the caller's snapshot.

        Values, predicates and ordinal batch references stay in this call's
        memory. All batches belong to one complete rule publication, never to
        durable partitions identified by a source value or its hash.
        """
        columns = rule.scope.partition_by
        if not columns:
            return [("all", rule.scope.predicate)], 0
        if not set(columns) <= set(rule.identity.key_columns):
            raise LightGraphError("partition_breaks_identity")
        if rule.kind in ("wide_hierarchy", "long_hierarchy") and not set(columns) <= set(rule.scope.namespaces):
            raise LightGraphError("partition_breaks_hierarchy")
        rows = self._rows(adapter, table, columns, rule, predicate=rule.scope.predicate, limit=self.max_partitions)
        try:
            from ..query.predicate import combine_scope_predicates
            partitions = []
            for index, row in enumerate(sorted(rows, key=canonical)):
                values = [row[c] for c in columns]
                filters = [{"column": c, "op": "is_null"} if value is None else
                           {"column": c, "op": "eq", "value": value}
                           for c, value in zip(columns, values)]
                predicate = combine_scope_predicates(rule.scope.predicate, {"all": filters})
                partitions.append((f"batch_{index}", predicate))
            # An empty full scope still needs a completed publication and rule
            # record. This empty read also validates the source snapshot before
            # withdrawing any formerly observed partitions.
            return partitions or [("all", rule.scope.predicate)], len(partitions)
        finally:
            rows.clear()

    def _validate(self, rule, rows):
        codes=[]
        evidence = rule.evidence
        requires_semantics = (rule.kind in {"reference", "wide_hierarchy", "long_hierarchy", "concept_mapping"}
                              or rule.identity.semantic_resolution != "source_scoped")
        if getattr(evidence, "origin", "user_rule") == "discovery" and requires_semantics:
            # An extractor's arbitrary nonempty string is not approval. Explicit
            # configured rules remain user decisions; discoveries need a scoped
            # affirmative review and must carry no unresolved counter-evidence.
            status = getattr(evidence, "semantic_status", "unresolved")
            if (status != "supported" or
                    getattr(evidence, "semantic_decision", "unreviewed") not in {"explicit_declaration", "model_review"} or
                    not getattr(evidence, "conditions_satisfied", False) or
                    getattr(evidence, "refuting_refs", [])):
                return ["semantic_evidence_refuted" if status == "refuted" else "semantic_evidence_unresolved"]
        if rule.kind == "reference":
            if not rule.evidence.semantic_basis:
                codes.append("semantic_evidence_missing")
            if self.store.policy.relation_allows_self_links(rule.relation.relation) and (
                evidence.semantic_status != 'supported' or evidence.semantic_decision != 'model_review'
                or not evidence.conditions_satisfied or evidence.refuting_refs
                or 'semantic_review:allow_self_links' not in evidence.semantic_basis
            ):
                codes.append('reference_self_link_semantics_unreviewed')
            return codes
        # FD checks happen over the complete joint projection, never over physical-row uniqueness.
        by_key=defaultdict(list)
        for row in rows:
            key=tuple(row.get(c) for c in rule.identity.key_columns)
            if any(v is None or v == "" for v in key):
                if rule.kind != "wide_hierarchy": codes.append("null_key")
                continue
            by_key[canonical(key)].append(row)
        for group in by_key.values():
            for col in rule.attributes.single_valued.values():
                vals={canonical(r[col]) for r in group if r.get(col) is not None}
                if len(vals)>1: codes.append("fd_violation")
        if rule.identity.semantic_resolution == "verified_definition_registry" and not rule.evidence.semantic_basis:
            codes.append("identity_evidence_missing")
        if rule.identity.semantic_resolution == "verified_identity_domain":
            if any(row.get(column) in (None, "") for row in rows for column in rule.scope.namespaces):
                codes.append("null_identity_namespace")
            if rule.kind == "wide_hierarchy" and rule.identity.level_policy != "relative_path":
                codes.append("relative_path_policy_required")
            if (rule.attributes.single_valued or rule.attributes.multi_valued
                    or (rule.hierarchy and any(level.label or level.aliases for level in rule.hierarchy.levels))):
                if not rule.identity.attribute_evidence:
                    codes.append("shared_attribute_evidence_missing")
        if rule.kind in ("wide_hierarchy", "long_hierarchy"):
            h=rule.hierarchy
            if not h.order_evidence: codes.append("level_group_ambiguous")
            if rule.kind == "wide_hierarchy" and len(h.levels)<2: codes.append("level_group_ambiguous")
            if rule.kind == "long_hierarchy" and not h.parent_column: codes.append("parent_column_missing")
            if rule.identity.level_policy == "relative_path":
                if not h.taxonomy: codes.append("hierarchy_taxonomy_missing")
                if not h.position_semantics or not h.position_evidence:
                    codes.append("hierarchy_position_semantics_missing")
        return sorted(set(codes))

    def execute(self, adapter, rule: RuleSpec, *, document_versions=None, defer_publish=False):
        # Persisted rule syntax never contains the ephemeral partition predicates.
        rule = RuleSpec.model_validate(rule.model_dump(mode="json", by_alias=True))
        outcome = ExecutionResult(rule.rule_id)
        prior_bindings = dict(self.bindings)
        prior_policy = self.store.policy
        self.bindings = {key: value for key, value in self.bindings.items() if key[0] != rule.rule_id}
        publications = []
        partitions = []
        rows = []
        validation = None
        validation_persisted = False
        started = time.monotonic()
        try:
            table = self._table(adapter, rule)
            cols = self.columns(rule)
            if not cols or set(cols) - set(table.column_names):
                raise LightGraphError("unknown_column")
            if set(cols) & {c.column for c in rule.attributes.excluded}:
                raise LightGraphError("excluded_column_projected")
            if rule.dependencies.schema_ref and rule.dependencies.schema_ref != self.schema_fingerprint(table):
                raise LightGraphError("schema_drift")
            if rule.dependencies.documents and (not document_versions or any(d not in document_versions for d in rule.dependencies.documents)):
                raise LightGraphError("document_dependency_stale")
            from .compiler import validate_plan
            validate_plan(rule)
            self.store.policy = self.store.preview_rule_semantics(rule)
            if any(adapter.capabilities().capabilities.get(c) != "verified" for c in rule.required_capabilities):
                raise LightGraphError("unsupported_dialect")
            rule = rule.model_copy(deep=True)
            rule.dependencies.schema_ref = self.schema_fingerprint(table)
            existing = next((r for r in self.store.get_rules() if r.rule_id == rule.rule_id and r.revision == rule.revision), None)
            if existing:
                excluded = {"status", "validation_ref", "coverage"}
                if canonical(existing.model_dump(exclude=excluded)) != canonical(rule.model_dump(exclude=excluded)):
                    raise LightGraphError("rule_revision_conflict")
            versions = {"schema": rule.dependencies.schema_ref, "operator": rule.dependencies.operator_version,
                        "documents": rule.dependencies.documents,
                        "identity": digest(rule.identity.model_dump()),
                        "hierarchy": digest(rule.hierarchy.model_dump() if rule.hierarchy else None)}
            if rule.kind == 'reference':
                versions['reference_self_links'] = REFERENCE_SELF_LINK_CONTRACT_VERSION
            validation = ValidationRecord(validation_id="v_" + uuid.uuid4().hex,
                rule_id=rule.rule_id, checks={"distinct_tuples": 0, "conflicts": 0},
                coverage=Coverage(scope="full", consistent=False), status="validated", codes=[])
            outcome.validation = validation
            rule.status = "validated"
            rule.validation_ref = validation.validation_id
            with adapter.snapshot() if hasattr(adapter, "snapshot") else nullcontext():
                if not adapter.capabilities().consistent_snapshot:
                    raise LightGraphError("snapshot_unavailable")
                partitions, enumerated = self._partitions(adapter, table, rule)
                validation.coverage = Coverage(scope="full", partitions=["all"], consistent=False)
                rule.coverage = validation.coverage
                outcome.counts["partitions_enumerated"] = enumerated
                publication = self.store.begin_publication(rule.rule_id, rule.revision, "all", versions,
                                                           replace_rule=True)
                publications.append(publication)
                outcome.active_publication_ids.append(publication)
                all_codes = set()
                for _, predicate in partitions:
                    if time.monotonic() - started > rule.budget.max_seconds:
                        raise LightGraphError("query_timeout")
                    rows = self._rows(adapter, table, cols, rule, predicate=predicate)
                    validation.checks["distinct_tuples"] += len(rows)
                    codes = self._validate(rule, rows)
                    if codes:
                        outcome.codes = codes
                        validation.status = "rejected"
                        raise LightGraphError("rule_partition_validation_failed")
                    if rule.kind == "reference":
                        counts, codes = self._references(publication, rule, rows)
                    elif rule.kind == "wide_hierarchy":
                        counts, codes = self._wide(publication, rule, rows)
                    elif rule.kind == "long_hierarchy":
                        counts, codes = self._long(publication, rule, rows)
                    elif rule.kind == "concept_mapping":
                        from .concepts import emit_concepts
                        counts, codes = emit_concepts(self, publication, rule, rows)
                    else:
                        counts, codes = self._entities(publication, rule, rows)
                    for name, count in counts.items():
                        outcome.counts[name] = outcome.counts.get(name, 0) + count
                    if rule.kind == "reference":
                        for name in ("null_endpoints_skipped", "empty_arrays_skipped"):
                            validation.checks[name] = validation.checks.get(name, 0) + counts.get(name, 0)
                    all_codes.update(codes)
                    if set(codes) - {"gap_in_path"}:
                        outcome.codes = sorted(all_codes)
                        validation.status = "unresolved"
                        raise LightGraphError("rule_partition_validation_failed")
                    rows.clear()
                    # Bindings are derived graph identities, but they still consume
                    # the same bounded working memory as subsequent partitions.
                    if sum(len(canonical([key, value]).encode()) for key, value in self.bindings.items()) > self.max_working_bytes:
                        raise LightGraphError("memory_budget_exhausted")
                    if time.monotonic() - started > rule.budget.max_seconds:
                        raise LightGraphError("query_timeout")
                # Source batches are not persistent identities. Withdrawals are
                # reconciled over the complete replacement's support set.
                outcome.counts["rule_generations_prepared"] = 1
                outcome.codes = sorted(all_codes)
            # An outer snapshot exit failure invalidates the entire enumeration,
            # including partitions read and staged successfully before the failure.
            validation.coverage.consistent = True
            validation.codes = outcome.codes
            self.store.register_rule_semantics(rule)
            self.store.put_validation(validation)
            validation_persisted = True
            if not existing:
                self.store.put_rule(rule)
            outcome.publication_ids = list(publications)
            outcome.publication_id = publications[0] if publications else None
            if defer_publish:
                outcome.prepared = True
            else:
                self.store.publish_partitions([PartitionCompletion(publication_id=p, complete=True,
                    consistent=True, dependencies_valid=True, truncated=False) for p in publications])
                outcome.published = True
            return outcome
        except Exception as exc:
            for publication in publications:
                try:
                    self.store.abort(publication)
                except Exception:
                    pass
            outcome.published = False
            outcome.prepared = False
            outcome.publication_ids = []
            outcome.active_publication_ids = []
            outcome.publication_id = None
            failure_code = exc.code if isinstance(exc, LightGraphError) else "execution_failed"
            if failure_code != "rule_partition_validation_failed":
                outcome.codes = sorted(set(outcome.codes + [failure_code]))
            elif not outcome.codes:
                outcome.codes = [failure_code]
            if validation:
                if validation_persisted:
                    # Structural validation and publication are separate events.
                    # Never rewrite a successful immutable validation merely
                    # because the later visibility/CAS step was rejected.
                    validation = validation.model_copy(deep=True)
                    validation.validation_id = "v_" + uuid.uuid4().hex
                    outcome.validation = validation
                if validation.status == "validated":
                    validation.status = "unresolved"
                validation.codes = outcome.codes
                validation.checks["conflicts"] = len(outcome.codes)
                self.store.put_validation(validation)
            return outcome
        finally:
            if not outcome.published and not outcome.prepared:
                self.bindings = prior_bindings
                self.store.policy = prior_policy
            rows.clear()
            partitions.clear()

    def _scope(self, rule, row):
        vals=[row.get(c) for c in rule.scope.namespaces]
        if not vals:return "public"
        if len(vals)==1:return str(vals[0])
        return canonical(dict(zip(rule.scope.namespaces,vals)))

    def _support(self, rule, binding, scope, partition_id="all"):
        resolved = self.store.identity.lookup(binding)
        return SupportSpec(source_binding=binding,scope_ref=scope,partition_id=partition_id,rule_id=rule.rule_id,
            rule_revision=rule.revision,validation_ref=rule.validation_ref,
            binding_revision=resolved.revision if resolved else 1)

    def _stage(self, pub, rule, binding, scope, kind, payload):
        partition_id = self.store._publication(pub)["partition_id"]
        self.store.stage_fact(pub,GraphFact(isolation_domain=self.domain,fact_kind=kind,payload=payload),
                              self._support(rule,binding,scope,partition_id))

    def _identity(self, rule, row, key, *, extra_definition=None, binding_columns=None, binding_context=None):
        from ..identity.registry import DEFINITION_FIELDS
        definition={k:row.get(c) for k,c in rule.attributes.single_valued.items()
                    if k in DEFINITION_FIELDS and k not in ("label","business_code") and row.get(c) is not None}
        if extra_definition:definition.update(extra_definition)
        if rule.identity.semantic_key_columns:
            definition["definition"] = canonical([row.get(c) for c in rule.identity.semantic_key_columns])
        if rule.hierarchy and rule.hierarchy.taxonomy and rule.identity.semantic_resolution != "verified_identity_domain":
            definition["taxonomy"] = rule.hierarchy.taxonomy
        # Local code spaces belong to declared lookup columns, not just their
        # values. Two distinct column groups may both contain code "01". Keep
        # the declared key signature stable across rules and across every level
        # emitted by one hierarchy; verified definitions may still unify them.
        lookup_namespace = {
            "namespace_columns": rule.scope.namespaces,
            "namespace_values": [row.get(c) for c in rule.scope.namespaces],
            "key_columns": binding_columns or rule.identity.key_columns,
        }
        if binding_context is not None:
            # Projection contexts distinguish independent joint assignments for
            # one source key; they are supplied only by trusted rule operators.
            lookup_namespace['projection_context']=binding_context
        domain_args = {}
        if rule.identity.semantic_resolution == "verified_identity_domain":
            verified_scope = ([row.get(c) for c in rule.scope.namespaces]
                if rule.identity.identity_scope == "namespace_local" else [])
            domain_args = {"identity_domain": rule.identity.identity_domain,
                "verified_key": [verified_scope, key[-1] if rule.identity.level_policy == "relative_path"
                    else [row.get(c) for c in rule.identity.key_columns if c not in rule.scope.namespaces]],
                "identity_evidence": tuple(rule.identity.identity_evidence)}
        resolution=self.store.identity.resolve(kind=rule.identity.entity_kind,
            source_ref=f"{rule.source.source_id}.{rule.source.schema_name}.{rule.source.table}",
            namespace=canonical(lookup_namespace), business_key=canonical(key),
            definition=definition,isolation_domain=self.domain,definition_version="",
            shared_verified=rule.identity.semantic_resolution=="verified_definition_registry",
            semantic_namespace=rule.identity.semantic_namespace, **domain_args)
        return resolution

    def _emit_entity(self,pub,rule,row,key, *, label=None,aliases=None,extra_definition=None,binding_columns=None,binding_context=None):
        if rule.identity.semantic_resolution == "verified_identity_domain":
            extra_definition = dict(extra_definition or {})
            if label is not None:
                extra_definition["label"] = label
            elif rule.attributes.single_valued.get("label"):
                extra_definition["label"] = row.get(rule.attributes.single_valued["label"])
        r=self._identity(rule,row,key,extra_definition=extra_definition,binding_columns=binding_columns,
                         binding_context=binding_context)
        scope=self._scope(rule,row)
        self._stage(pub,rule,r.binding_id,scope,"NodeExistence",{"entity_id":r.entity_id,"kind":rule.identity.entity_kind,"layer":"business"})
        attrs={k:row.get(col) for k,col in rule.attributes.single_valued.items()}
        if label is not None:attrs["label"]=label
        attrs["business_code"]=key[-1] if key else None
        for name,value in attrs.items():
            if value is not None:
                self._stage(pub,rule,r.binding_id,scope,"PropertyValue",{"entity_id":r.entity_id,"name":name,"value":value})
        for name,col in rule.attributes.multi_valued.items():
            value=row.get(col)
            if value is not None:
                self._stage(pub,rule,r.binding_id,scope,"PropertyValue",{"entity_id":r.entity_id,"name":name,"value":value})
        for value in aliases or []:
            if value is not None:
                self._stage(pub,rule,r.binding_id,scope,"PropertyValue",{"entity_id":r.entity_id,"name":"alias","value":value})
        self.bindings[(rule.rule_id,canonical(key))]=r.entity_id
        return r.entity_id,r.binding_id,scope

    def _entities(self,pub,rule,rows):
        ids=set()
        for row in rows:
            key=tuple(row.get(c) for c in rule.identity.key_columns)
            eid,_,_=self._emit_entity(pub,rule,row,key)
            ids.add(eid)
        return {"entities":len(ids),"projected_tuples":len(rows)},[]

    @staticmethod
    def _cycles(edges):
        graph=defaultdict(set)
        for a,b in edges:graph[a].add(b)
        # Kahn's algorithm handles arbitrarily deep paths without Python recursion.
        indegree=defaultdict(int)
        for a,targets in graph.items():
            indegree.setdefault(a,0)
            for b in targets:indegree[b]+=1
        queue=[n for n,d in indegree.items() if d==0];seen=0
        while queue:
            n=queue.pop();seen+=1
            for child in graph.get(n,()):
                indegree[child]-=1
                if indegree[child]==0:queue.append(child)
        return seen!=len(indegree)

    def _emit_edges(self,pub,rule,records):
        pairs={(a,b) for a,b,_,_ in records}
        if any(a==b for a,b in pairs):return ["hierarchy_self_loop"]
        if self._cycles(pairs):return ["hierarchy_cycle"]
        parents=defaultdict(set)
        for a,b in pairs:parents[b].add(a)
        if not rule.hierarchy.allow_dag and any(len(v)>1 for v in parents.values()):return ["multiple_parent"]
        for a,b,binding,scope in records:
            qualifiers={"taxonomy":rule.hierarchy.taxonomy, **rule.hierarchy.context_qualifiers}
            self._stage(pub,rule,binding,scope,"Edge",{"source":a,"target":b,"relation":"parent_of",
                "qualifiers":qualifiers})
        return []

    def _wide(self,pub,rule,rows):
        ids=set();records=set();codes=[];labels=defaultdict(set)
        code_positions=defaultdict(set);fill_pairs=defaultdict(set)
        levels=sorted(rule.hierarchy.levels,key=lambda x:x.level)
        relative = rule.identity.level_policy == "relative_path"
        for row in rows:
            prev=None;gap=False;seen_positions={};previous_level=None;seen_entities=set()
            for index,lv in enumerate(levels):
                if (relative and rule.hierarchy.position_semantics == "ordinal_slots"
                        and previous_level is not None and lv.level != previous_level + 1):
                    if prev:gap=True
                    prev=None
                previous_level = lv.level
                code=row.get(lv.code)
                if code is None or code=="":
                    if prev:gap=True
                    prev=None;continue
                key=tuple(row.get(c) for c in rule.scope.namespaces)+(code,)
                if rule.identity.level_policy=="level_qualified":key=key[:-1]+(lv.level,code)
                code_key=canonical(key)
                if rule.identity.level_policy not in {"level_qualified", "relative_path"}:
                    code_positions[code_key].add(index)
                    if code_key in seen_positions:
                        last_position=seen_positions[code_key]
                        if last_position!=index-1:
                            codes.append("non_contiguous_level_code")
                        elif rule.hierarchy.fill_semantics_confirmed:
                            fill_pairs[code_key].add((last_position,index))
                    seen_positions[code_key]=index
                label=row.get(lv.label) if lv.label else str(code)
                labels[canonical(key)].add(canonical(label))
                if len(labels[canonical(key)])>1:codes.append("same_code_diff_label")
                eid,binding,scope=self._emit_entity(pub,rule,row,key,label=label,
                    aliases=[row.get(c) for c in lv.aliases],
                    binding_columns=rule.scope.namespaces+[lv.code] if relative else None)
                ids.add(eid)
                if relative:
                    membership={"taxonomy":rule.hierarchy.taxonomy,
                        "source_group":f"{rule.source.source_id}.{rule.source.schema_name}.{rule.source.table}:{lv.code}",
                        **rule.hierarchy.context_qualifiers}
                    if lv.level in rule.hierarchy.semantic_levels:
                        membership["semantic_level"] = rule.hierarchy.semantic_levels[lv.level]
                    self._stage(pub,rule,binding,scope,"PropertyValue",
                        {"entity_id":eid,"name":"source_position","value":lv.level,"qualifiers":membership})
                    if eid in seen_entities and (not prev or prev[0] != eid):
                        codes.append("non_contiguous_level_code")
                    seen_entities.add(eid)
                if gap:codes.append("gap_in_path");gap=False
                if prev:
                    if prev[0]==eid:
                        if not rule.hierarchy.fill_semantics_confirmed:codes.append("fill_mismatch")
                    else:records.add((prev[0],eid,binding,scope))
                prev=(eid,binding,scope)
        if rule.identity.level_policy not in {"level_qualified", "relative_path"}:
            for code_key,positions in code_positions.items():
                ordered=sorted(positions)
                if len(ordered)>1 and any((a,b) not in fill_pairs[code_key] or b!=a+1 for a,b in zip(ordered,ordered[1:])):
                    codes.append("cross_level_code_reuse")
        codes+=self._emit_edges(pub,rule,records)
        return {"entities":len(ids),"edges":len({(a,b) for a,b,_,_ in records}),"projected_tuples":len(rows)},codes

    def _long(self,pub,rule,rows):
        ids=set();lookup={};records=set();codes=[]
        for row in rows:
            key=tuple(row.get(c) for c in rule.identity.key_columns)
            eid,binding,scope=self._emit_entity(pub,rule,row,key)
            lookup[canonical(key)]=(eid,binding,scope);ids.add(eid)
        for row in rows:
            parent=row.get(rule.hierarchy.parent_column)
            if parent is None or parent=="":continue
            key=tuple(row.get(c) for c in rule.identity.key_columns)
            pk=key[:-1]+(parent,)
            if canonical(pk) not in lookup:codes.append("reference_missing");continue
            a=lookup[canonical(pk)][0];b,binding,scope=lookup[canonical(key)]
            records.add((a,b,binding,scope))
        codes+=self._emit_edges(pub,rule,records)
        return {"entities":len(ids),"edges":len(records),"projected_tuples":len(rows)},codes

    def _references(self,pub,rule,rows):
        from .endpoints import endpoint_columns, endpoint_identity_key
        endpoint_rules={r.rule_id:r for r in self.store.get_rules()}
        source_rule=endpoint_rules.get(rule.relation.source_rule_id)
        target_rule=endpoint_rules.get(rule.relation.target_rule_id)
        allowed=self.store.policy.relation_endpoint_kinds(rule.relation.relation)
        if not source_rule or not target_rule:
            return {"edges":0},["reference_rule_not_validated"]
        if not allowed or source_rule.identity.entity_kind not in allowed[0] or target_rule.identity.entity_kind not in allowed[1]:
            return {"edges":0},["relation_endpoint_kind_mismatch"]
        for endpoint, columns, level in (
            (source_rule, rule.relation.source_columns, rule.relation.source_level),
            (target_rule, rule.relation.target_columns, rule.relation.target_level),
        ):
            if len(columns) != len(endpoint_columns(endpoint, level)):
                raise LightGraphError('reference_arity_mismatch')
        count=0;expanded=0;codes=[];emitted=set();null_skipped=0;empty_skipped=0
        for row in rows:
            endpoints=[tuple(row.get(column) for column in columns) for columns in
                       (rule.relation.source_columns, rule.relation.target_columns)]
            null_flags=[(any(value is None for value in key),all(value is None for value in key)) for key in endpoints]
            if any(some and not all_null for some,all_null in null_flags):
                codes.append("reference_partial_null");continue
            if any(all_null for _,all_null in null_flags):
                if rule.relation.require_full_coverage:codes.append("reference_null_endpoint")
                else:null_skipped+=1
                continue
            sources=self._reference_keys(row,rule.relation.source_columns,rule.relation.source_json_array_columns,rule.relation.max_array_elements)
            targets=self._reference_keys(row,rule.relation.target_columns,rule.relation.target_json_array_columns,rule.relation.max_array_elements)
            if not sources or not targets:
                if rule.relation.require_full_coverage:codes.append("reference_null_endpoint")
                else:empty_skipped+=1
                continue
            # The contract permits an array on only one endpoint; this product
            # expands one existing reference list, never two independent domains.
            for source in sources:
                for target in targets:
                    expanded+=1
                    if expanded>rule.budget.max_rows_returned:
                        raise LightGraphError("reference_expansion_limit")
                    akey=canonical(endpoint_identity_key(source_rule,source,rule.relation.source_level))
                    bkey=canonical(endpoint_identity_key(target_rule,target,rule.relation.target_level))
                    a=self.bindings.get((rule.relation.source_rule_id,akey));b=self.bindings.get((rule.relation.target_rule_id,bkey))
                    if not a or not b:
                        codes.append("reference_missing");continue
                    # Check resolved identities, including identity reuse across
                    # columns and levels. One forbidden self link rejects this
                    # rule's complete publication, not just the offending row.
                    self.store.policy.check_relation_self_link(rule.relation.relation,a,b,reference=True)
                    # Connection-table aliases are physical column names. The
                    # edge inherits the source object's logical namespace,
                    # otherwise renamed composite keys create an invisible scope.
                    source_values=dict(zip(endpoint_columns(source_rule,rule.relation.source_level),source))
                    if not set(source_rule.scope.namespaces)<=source_values.keys():
                        raise LightGraphError('reference_namespace_not_in_identity')
                    scope=self._scope(source_rule,source_values)
                    if (a,b,scope) in emitted:continue
                    emitted.add((a,b,scope))
                    binding="rel_"+digest(rule.rule_id,a,b)[:24]
                    self._stage(pub,rule,binding,scope,"Edge",{"source":a,"target":b,"relation":rule.relation.relation,
                        "qualifiers":rule.relation.qualifiers})
                    count+=1
        return {"edges":count,"expanded_references":expanded,"projected_tuples":len(rows),
                "null_endpoints_skipped":null_skipped,"empty_arrays_skipped":empty_skipped},sorted(set(codes))

    @staticmethod
    def _reference_keys(row,columns,array_columns,max_elements):
        if not array_columns:return [tuple(row.get(c) for c in columns)]
        array_column=array_columns[0]
        raw=row.get(array_column)
        if not isinstance(raw,str):raise LightGraphError("json_reference_requires_text")
        try:
            values=json.loads(raw)
        except (ValueError,TypeError,RecursionError):
            raise LightGraphError("invalid_json_reference") from None
        if not isinstance(values,list):raise LightGraphError("invalid_json_reference_array")
        if len(values)>max_elements:raise LightGraphError("json_reference_element_limit")
        if any(type(value) not in (str,int,float,bool) or isinstance(value,float) and not math.isfinite(value)
               or isinstance(value,str) and len(value)>4096 for value in values):
            raise LightGraphError("non_scalar_json_reference")
        unique={canonical(value):value for value in values}
        return [tuple(value if column==array_column else row.get(column) for column in columns) for value in unique.values()]
