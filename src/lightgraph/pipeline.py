"""End-to-end orchestration with safe, outcome-oriented reports."""
from __future__ import annotations
import json
from pathlib import Path
import yaml
from .contracts import RuleSpec, GraphFact, SupportSpec, PartitionCompletion, digest, canonical
from .errors import LightGraphError

_TIME_DEFERRALS=frozenset({
    'discovery_time_budget_exhausted','wall_time_budget_exhausted','run_time_budget_exhausted'})


def relation_attempt_consumed(result):
    """A clock stop before any relation rule is not a used attempt."""
    if getattr(result,'rules',None):
        return True
    return not any(issue.get('code') in _TIME_DEFERRALS for issue in (getattr(result,'issues',None) or []))


DISCOVERY_WORKFLOW_VERSION='goal_driven_iterations/25'
# Share of the discovery window held back from object discovery so relation
# discovery and its follow-up still run after entities exist.
RELATION_WINDOW_FRACTION=0.35


def _relation_context_rules(endpoints, rules, failed):
    """Retain verified relation vocabulary when resuming from entity endpoints."""
    return endpoints+[rule for rule in rules if rule.kind=='reference' and rule.rule_id not in failed]

# These failures mean the Agent never completed an interpretation. They are not
# permission to retry within a turn; leave the table pending for a later run.
_AGENT_EXECUTION_FAILURES = frozenset({
    'agent_source_tracing_forbidden', 'agent_source_persistence_forbidden',
    'agent_tracing_forbidden', 'agent_nontext_tool_response',
    'agent_sync_entrypoint_inside_event_loop', 'invalid_tool_schema',
    'model_structured_output_rejected', 'model_output_schema_missing',
    'model_response_format_invalid',
})


def _agent_execution_failure(code):
    return code in _AGENT_EXECUTION_FAILURES or code.startswith('model_stream_')


def _discovery_outcome(issues):
    """Historical diagnostics do not reopen completed or explicitly rejected work."""
    active=[issue for issue in issues if issue.get('status') not in {'diagnostic','resolved','rejected'}]
    if not active:
        return 'COMPLETED'
    partial_tokens=('budget','exhausted','timeout','truncated','unavailable','permission',
        'incomplete','unverified','formula_cache_missing','execution_failed',
        'dependency_rule_failed','too_large','model_http','model_response','model_transport')
    for issue in active:
        code=str(issue.get('code',''))
        if (issue.get('status') in {'deferred','partial'} or _agent_execution_failure(code)
                or any(token in code for token in partial_tokens)):
            return 'PARTIAL'
    return 'COMPLETED_WITH_GAPS'


def _deferred_tables(result, selected=()):
    """Operational failures are retryable work, not a completed interpretation."""
    tokens=('budget','limit','exhausted','timeout','too_large','incomplete','unavailable',
            'permission','unauthorized','execution_failed','model_http','model_response',
            'model_transport','model_request','model_json','model_schema','connection',
            'model_api_key','llm_schema','llm_json')
    failed=set()
    for issue in result.issues:
        if issue.get('status') in {'diagnostic','resolved','rejected'}:
            continue
        if (issue.get('status') in ('deferred','partial') or
            _agent_execution_failure(str(issue.get('code',''))) or
            any(token in str(issue.get('code','')) for token in tokens)):
            failed.update([issue['table']] if issue.get('table') else selected)
    return failed


def _relation_work_version(table, context_version, endpoints, role_context):
    """Remember a completed investigation against its schema and endpoint contract.

    No row values enter this key. A schema, document, endpoint revision or
    explicit retry can reopen investigation; an unchanged uncertain decision
    does not itself justify another model call.
    """
    return 'relations_'+digest('terminal_decisions/1',context_version,
        table.model_dump(mode='json',by_alias=True),
        sorted((rule.rule_id,rule.revision) for rule in endpoints),role_context)


def _relation_work_issue(report, table, state):
    """Keep a finished evidence gap distinct from an interrupted investigation."""
    codes={'relation_discovery_pending','relation_discovery_unresolved'}
    report.issues[:]=[i for i in report.issues if not (
        i.get('code') in codes and i.get('table')==table)]
    if state in ('deferred','unresolved'):
        report.issues.append({'code':'relation_discovery_pending' if state=='deferred'
            else 'relation_discovery_unresolved','table':table,'status':state})


def _graph_outcome(report, store, config, *, discover_only):
    """Report visible business output separately from metadata and process status."""
    if discover_only:
        report.validation['business_graph'] = 'not_requested'
        return
    graph = store.view(set(config.scopes), config.isolation_domain, include_candidates=True)
    business = {n['id']: n for n in graph['nodes'] if n['layer'] == 'business'}
    edges = [e for e in graph['edges'] if e['source'] in business and e['target'] in business]
    report.counts.update(
        business_nodes=sum(n['status'] == 'accepted' for n in business.values()),
        business_edges=sum(e['status'] == 'accepted' for e in edges),
        candidate_business_nodes=sum(n['status'] != 'accepted' for n in business.values()),
        candidate_business_edges=sum(e['status'] != 'accepted' for e in edges),
        display_captions_projected=sum(bool(n.get('display_label')) for n in business.values()),
    )
    report.validation['business_graph'] = (
        'entities_and_relations' if report.counts['business_edges'] else
        'entities_only' if report.counts['business_nodes'] else
        'candidates_only' if business else 'empty')
    if not business and store._has_scope_filtered_business_support(
            report.rules_published, set(config.scopes), config.isolation_domain):
        report.validation['business_graph_reason'] = 'scope_filtered'
        report.issues.append({'code':'business_graph_filtered_by_scopes',
            'message':'Published business support is outside the requested result scopes. '
                      'Review the configured authorized result scopes; permissions were not changed.'})
        if report.status == 'COMPLETED':
            report.status = 'COMPLETED_WITH_GAPS'


def load_rules(paths):
    result=[]
    try:
        for path in paths:
            value=yaml.safe_load(Path(path).read_text())
            records=value.get('rules',[]) if isinstance(value,dict) else value
            result.extend(RuleSpec.model_validate(v) for v in records)
    except Exception:raise LightGraphError("rules_invalid") from None
    keys=[(r.rule_id,r.revision) for r in result]
    if len(set(keys))!=len(keys):raise LightGraphError("duplicate_rule_revision")
    return result


def make_model(config):
    """Resolve the sole supported discovery runtime; no in-process LLM client."""
    from .construction.runner import runtime_options
    return runtime_options(config)


def publish_documents(store,documents,domain,model=None,scopes=("public",),document_scopes=("public",),check_time=None,goal=None):
    from .documents import extract_with_cache
    from .documents.cache import extractor_fingerprint
    from .context import CONTEXT_SELECTION_VERSION, goal_payload
    count=0;issues=[]
    snapshot=store.view(set(scopes)&set(document_scopes),domain=domain)
    known={}
    for n in snapshot.get('nodes',[]):known.setdefault(n.get('label',''),[]).append(n.get('id',n.get('entity_id')))
    for document in documents:
        if check_time:check_time()
        rid='document:'+document.document_id
        versions={'document':document.document_id+'@'+document.revision,'scopes':sorted(document_scopes),
                  'document_extractor':extractor_fingerprint(model),
                  'goal':digest(goal_payload(goal)), 'context_selection':CONTEXT_SELECTION_VERSION}
        if store.current_versions(rid)==versions:continue
        prior_cache=store.get_document_cache(rid)
        pub=store.begin_publication(rid,1,'all',versions)
        doc_publications=[pub]
        published=False
        try:
            docid='doc:'+digest(domain,document.document_id)[:24]
            def stage(kind,payload,ref,status='accepted'):
                for scope in document_scopes:
                    store.stage_fact(pub,GraphFact(isolation_domain=domain,fact_kind=kind,payload=payload),SupportSpec(
                        origin='document_claim',source_binding=ref,scope_ref=scope,partition_id='all',
                        rule_id=rid,rule_revision=1,document_ref=ref,status=status))
            stage('NodeExistence',{'entity_id':docid,'kind':'Document','layer':'provenance'},document.document_id+'@'+document.revision)
            stage('PropertyValue',{'entity_id':docid,'name':'label','value':document.title},document.document_id+'@'+document.revision)
            for section in document.sections:
                sid='section:'+digest(domain,section.section_id)[:24]
                ref=document.document_id+'@'+document.revision+'#'+section.locator
                stage('NodeExistence',{'entity_id':sid,'kind':'DocumentSection','layer':'provenance'},ref)
                stage('PropertyValue',{'entity_id':sid,'name':'label','value':section.heading},ref)
                stage('Edge',{'source':docid,'relation':'has_section','target':sid},ref)
            extracted=extract_with_cache(document,model=model,known_entities=known,cached_sections=prior_cache,goal=goal)
            claims=extracted.claims
            for claim in claims:
                conditions={str(i):v for i,v in enumerate(claim.conditions)}
                ref=document.document_id+'@'+document.revision+'#'+claim.document.locator
                stage('DocumentClaim',{'id':claim.claim_id,'subject':claim.subject,'predicate':claim.predicate,
                    'object':claim.object,'modality':claim.modality,'conditions':conditions},ref,claim.status)
                # An unbound analytical idea is visibly a candidate, never a duplicate accepted metric.
                method='analysis:'+digest(domain,claim.predicate,claim.object)[:24]
                stage('NodeExistence',{'entity_id':method,'kind':'AnalysisMethod','layer':'business'},ref,'candidate')
                stage('PropertyValue',{'entity_id':method,'name':'label','value':claim.object},ref,'candidate')
            if check_time:check_time()
            completions=[PartitionCompletion(publication_id=pub,complete=True,consistent=True,dependencies_valid=True)]
            if store.current_versions('document-alignment:'+document.document_id):
                align=store.begin_publication('document-alignment:'+document.document_id,1,'all',{'document_refresh':document.revision})
                doc_publications.append(align)
                completions.append(PartitionCompletion(publication_id=align,complete=True,consistent=True,dependencies_valid=True))
            store.publish_partitions(completions)
            published=True
            count+=len(claims)
            store.put_document_cache(pub,extracted.cache)
        except LightGraphError as exc:
            if not published:
                for publication in doc_publications:store.abort(publication)
            issues.append({'document':document.document_id,'code':exc.code})
    return count,issues



def _issues(report, items):
    from .discovery.repair import issue_classification
    for issue in items:
        if hasattr(issue,'model_dump'):issue=issue.model_dump()
        safe={k:v for k,v in issue.items() if k in ('code','table','rule_id','count') and isinstance(v,(str,int))}
        if issue.get('code') in {'discovery_draft_rejected','discovery_draft_field_diagnostic'}:
            for name in ('reason_code','field_path','constraint'):
                if isinstance(issue.get(name),str):safe[name]=issue[name]
        if issue.get('code') in {'unexplained_columns','coverage_shared_label_unresolved'}:
            raw=issue.get('column_names')
            if isinstance(raw,str) and len(raw)<=32768:
                try:names=json.loads(raw)
                except (ValueError,TypeError):names=None
                if isinstance(names,list) and len(names)<=32 and all(
                    isinstance(name,str) and 0<len(name)<=256 and
                    not any(ord(char)<32 or ord(char)==127 for char in name) for name in names):
                    safe['column_names']=canonical(names)
        if issue.get('status') in ('unresolved','deferred','partial','resolved','rejected','diagnostic'):
            safe['status']=issue['status']
        # Derive only known, value-free diagnostics; never trust model-provided
        # categories or instructions as durable user-facing advice.
        safe.update(issue_classification(safe.get('code')))
        if safe:report.issues.append(safe)


def _model_protocol_issues(usage):
    """Expose bounded parser diagnostics without retaining responses or values."""
    from .discovery.llm import MODEL_PROTOCOL_STAGES, MODEL_PROTOCOL_CODES, MODEL_PROTOCOL_FIELDS
    from .discovery.validation_feedback import _path
    histogram=usage.get('model_protocol_diagnostics',{})
    if not isinstance(histogram,dict):return []
    issues=[]
    for key,count in list(histogram.items())[:64]:
        if type(count) is not int or count<=0 or not isinstance(key,str) or len(key)>4096:continue
        try:diagnostic=json.loads(key)
        except (ValueError,TypeError):continue
        if not isinstance(diagnostic,dict):continue
        stage=diagnostic.get('stage');code=diagnostic.get('code');field=diagnostic.get('field')
        if not isinstance(stage,str) or stage not in MODEL_PROTOCOL_STAGES:continue
        if not isinstance(code,str) or code not in MODEL_PROTOCOL_CODES or not isinstance(field,list):continue
        issues.append({'code':'model_protocol_diagnostic','status':'diagnostic',
            'stage':stage,'field_path':canonical(_path(field,MODEL_PROTOCOL_FIELDS-{'<unknown_field>'})),
            'constraint':code,'count':count})
    return issues


def _goal_violation(rule,goal,projected):
    from .discovery.goal import source_in_goal, column_is_excluded, column_is_evidence_only
    if not source_in_goal(goal,rule.source):
        return 'outside_source_goal'
    if any(column_is_excluded(goal,rule.source,c) for c in projected):
        return 'excluded_column_projected'
    def predicate_columns(value):
        if not isinstance(value,dict):return []
        if 'not' in value:return predicate_columns(value['not'])
        if any(key in value for key in ('all','any','and')):
            return [c for key in ('all','any','and') for part in value.get(key,[]) for c in predicate_columns(part)]
        if 'column' in value:return [value['column']]
        return list(value)
    if any(column_is_excluded(goal,rule.source,c) for c in predicate_columns(rule.scope.predicate)):
        return 'excluded_column_queried'
    if any(column_is_evidence_only(goal,rule.source,c) for c in projected):
        return 'evidence_only_column_projected'
    if goal.entity_kinds and rule.identity.entity_kind not in goal.entity_kinds:return 'outside_extraction_goal'
    if goal.relation_kinds and rule.relation and rule.relation.relation not in goal.relation_kinds:return 'outside_relation_goal'
    if goal.relation_kinds and rule.kind in ('wide_hierarchy','long_hierarchy') and 'parent_of' not in goal.relation_kinds:return 'outside_relation_goal'
    if rule.concept_mapping:
        if goal.entity_kinds and any(c.entity_kind not in goal.entity_kinds for c in rule.concept_mapping.components):
            return 'outside_extraction_goal'
        if goal.relation_kinds and any(c.relation not in goal.relation_kinds for c in rule.concept_mapping.components):
            return 'outside_relation_goal'
    return None


def run_pipeline(config,adapters, *, rules=None,documents=(),model=None,discover_only=False):
    from .runtime.locking import run_lock
    from .runtime.trace_lifecycle import execution_trace
    from .runtime.trace import emit_trace
    with run_lock(config.output_dir), execution_trace(config):
        if rules is not None: rules=list(rules)
        emit_trace('pipeline_started', {'discover_only':discover_only,
            'rules':[r.model_dump(mode='json') for r in rules] if rules is not None else None})
        try:
            report=_run(config,adapters,rules=rules,documents=documents,model=model,discover_only=discover_only)
        except BaseException as exc:
            emit_trace('pipeline_error', {'error_type':type(exc).__name__,
                'code':exc.code if isinstance(exc,LightGraphError) else 'pipeline_interrupted'})
            raise
        emit_trace('pipeline_completed', {'report':report.model_dump(mode='json')})
        return report


def _run(config, adapters, *, rules, documents, model, discover_only):
    """Construction is owned by Harness; legacy proposal scheduling is removed."""
    from .construction.runner import run_construction
    from .construction.harness import HarnessOptions
    if rules or config.rules:
        raise LightGraphError("legacy_rule_input_removed")
    if documents:
        raise LightGraphError("legacy_document_input_removed")
    if model is not None and not isinstance(model, HarnessOptions):
        raise LightGraphError("legacy_model_runtime_removed")
    return run_construction(config, adapters, options=model, discover_only=discover_only)
