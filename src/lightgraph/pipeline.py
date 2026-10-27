"""End-to-end orchestration with safe, outcome-oriented reports."""
from __future__ import annotations
import time
import uuid
import json
from pathlib import Path
import yaml
from .contracts import RuleSpec, RunReport, GraphFact, SupportSpec, PartitionCompletion, digest, canonical
from .errors import LightGraphError
from .rules import RuleEngine
from .metadata import publish_metadata
from .runtime.retention import write_report,write_rules,atomic_text

DISCOVERY_WORKFLOW_VERSION='goal_driven_iterations/6'

# These failures mean the Agent never completed an interpretation. They are not
# permission to retry within a turn; leave the table pending for a later run.
_AGENT_EXECUTION_FAILURES = frozenset({
    'agent_source_tracing_forbidden', 'agent_source_persistence_forbidden',
    'agent_tracing_forbidden', 'agent_nontext_tool_response',
    'agent_sync_entrypoint_inside_event_loop', 'invalid_tool_schema',
})


def _agent_execution_failure(code):
    return code in _AGENT_EXECUTION_FAILURES or code.startswith('model_stream_')

def _context_versions(marker):
    """Source-version values stay flat references, never arbitrary dictionaries."""
    result={}
    for entry in marker.get('contexts',[]):
        try:
            name,version=json.loads(entry)
            if isinstance(name,str) and isinstance(version,str):result[name]=version
        except (ValueError,TypeError):
            continue
    return result


def _abort_staging(store, publications):
    # Later reporting failures must not attempt to undo a committed publication.
    for publication in publications:
        try:
            if store._publication(publication)['state'] in ('staging','ready'):store.abort(publication)
        except LightGraphError:
            pass


def _deferred_tables(result, selected=()):
    """Operational failures are retryable work, not a completed interpretation."""
    tokens=('budget','limit','exhausted','timeout','too_large','incomplete','unavailable',
            'permission','unauthorized','execution_failed','model_http','model_response',
            'model_transport','model_request','model_json','model_schema','connection',
            'model_api_key','llm_schema','llm_json')
    failed=set()
    for issue in result.issues:
        if (issue.get('status') in ('deferred','partial') or
            _agent_execution_failure(str(issue.get('code',''))) or
            any(token in str(issue.get('code','')) for token in tokens)):
            failed.update([issue['table']] if issue.get('table') else selected)
    return failed


def _discovery_counts(report, result):
    for key in ('compile_repair_rounds', 'recovered_proposals', 'deferred_proposals'):
        value = result.stats.get(key, 0)
        if type(value) is int:
            report.counts[key] = report.counts.get(key, 0) + value


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
    )
    report.validation['business_graph'] = (
        'entities_and_relations' if report.counts['business_edges'] else
        'entities_only' if report.counts['business_nodes'] else
        'candidates_only' if business else 'empty')


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
    settings = config.resolved_model
    if settings.mode == 'disabled':
        return None
    from .discovery.llm import StructuredModelClient, ModelConfig
    from .discovery.budget import BudgetAllocator, Cost
    client = StructuredModelClient(ModelConfig(
        base_url=settings.base_url, model=settings.model, agent_name=config.agent_name,
        api_key=settings.api_key, api_key_env=settings.api_key_env,
        stream=settings.stream, stream_options_include_usage=settings.stream_options_include_usage,
        allow_insecure_http=settings.allow_insecure_http, timeout_seconds=settings.timeout_seconds,
        max_output_tokens=settings.max_output_tokens, max_prompt_bytes=settings.max_prompt_bytes,
        max_response_bytes=settings.max_response_bytes, retries=settings.retries,
        max_discovery_steps=settings.max_discovery_steps, max_tool_calls=settings.max_tool_calls,
        allow_source_samples=settings.mode in ("in_environment", "approved_external")),
        BudgetAllocator(Cost(db_cu=config.max_db_queries, llm_calls=settings.max_calls,
            llm_tokens=settings.max_tokens, memory_bytes=config.max_working_bytes,
            seconds=config.max_run_seconds)))
    return client


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
    for issue in items:
        if hasattr(issue,'model_dump'):issue=issue.model_dump()
        safe={k:v for k,v in issue.items() if k in ('code','table','rule_id','count') and isinstance(v,(str,int))}
        if issue.get('status') in ('unresolved','deferred','partial','resolved','rejected'):
            safe['status']=issue['status']
        if safe:report.issues.append(safe)


def _latest(rules):
    latest={}
    for rule in rules:
        if rule.rule_id not in latest or rule.revision>latest[rule.rule_id].revision:latest[rule.rule_id]=rule
    return list(latest.values())


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
    return None


def run_pipeline(config,adapters, *, rules=None,documents=(),model=None,discover_only=False):
    from .runtime.locking import run_lock
    with run_lock(config.output_dir):
        return _run(config,adapters,rules=rules,documents=documents,model=model,discover_only=discover_only)


def _run(config,adapters,*,rules,documents,model,discover_only):
    from .store import GraphStore
    from .discovery import discover,discover_references
    from .presentation import export_graph
    output=Path(config.output_dir);output.mkdir(parents=True,exist_ok=True)
    report=RunReport(run_id='run_'+uuid.uuid4().hex[:20])
    started=time.monotonic()
    store=GraphStore(':memory:' if discover_only else output/'graph.sqlite')
    prepared=[]
    understandings=[]
    discovery_completed={}
    documents=[d for d in documents if d.document_id not in config.removed_document_ids]
    auto=rules is None
    discovery_mode='agent_tools' if callable(getattr(model,'explore',None)) else 'deterministic'
    discovery_model=model if discovery_mode=='agent_tools' else None
    allrules=list(rules or [])
    document_versions={d.document_id+'@'+d.revision for d in documents}
    try:
        report.counts['recovered_publications']=len(store.recover_staging())
        namespace=store.current_versions('workspace')
        if namespace and namespace!={'domain':config.isolation_domain}:raise LightGraphError('output_domain_mismatch')
        if not namespace:
            publication=store.begin_publication('workspace',1,'all',{'domain':config.isolation_domain})
            store.publish_partition(PartitionCompletion(publication_id=publication,complete=True,consistent=True,dependencies_valid=True))
        from .sources.budget import QueryBudget
        # A live source opened by config already shares a budget from connection time.
        budget=QueryBudget(max_queries=config.max_db_queries,max_run_seconds=config.max_run_seconds)
        for a in adapters:a.install_budget(budget)
        if model and hasattr(model,'budget'):
            model.budget.tighten_deadline(budget.deadline)
            from dataclasses import replace
            model.budget.limits=replace(model.budget.limits,
                memory_bytes=min(model.budget.limits.memory_bytes,config.max_working_bytes))
        budget.check_time()
        tables=[t for a in adapters for t in a.describe()]
        from .documents import DocumentCorpus
        from .context import context_dependency_fingerprint
        from .catalog import declared_catalog, semantic_catalog, write_catalog, read_semantic_catalog
        corpus=DocumentCorpus(documents)
        corpus.bind_catalog(tables)
        declared=declared_catalog(adapters)
        previous_semantic=read_semantic_catalog(output)
        write_catalog(output,declared,semantic_catalog(declared,config.goal,documents=documents,
                                                       previous=previous_semantic))
        from .sources.scoped import GoalScopedSource
        scoped_adapters=[GoalScopedSource(a,config.goal) for a in adapters]
        rules_by_source={t.source_id:a for a in scoped_adapters for t in a.describe()}
        # Invalidate stale dependent metadata edges before replacing a catalogue.
        table_map={(t.source_id,t.schema_name,t.name):t for t in tables}
        old_catalog={sid:store.current_versions('catalog:'+sid) for sid in rules_by_source}
        old_rules=store.get_rules()
        from .discovery.goal import source_in_goal
        contexts={sid:{t.qualified_name:context_dependency_fingerprint(corpus,[t],config.goal)
                      for t in tables if t.source_id==sid and source_in_goal(config.goal,t)}
                  for sid in rules_by_source}
        from .runtime.iteration import IterationLedger, REVIEWABLE_CODES, rule_shape
        from .contracts import SourceRef
        ledger=IterationLedger(store,config.goal,config.scopes)
        store.configure_goal(config.goal,ledger.goal_revision)
        report.counts['goal_revision']=ledger.goal_revision
        report.counts['feedback_rounds']=0
        changed_contexts={};invalidating_contexts={}
        for sid in rules_by_source:
            marker=store.current_versions('discovery:'+sid)
            if marker:
                old=_context_versions(marker)
                changed_contexts[sid]={name for name,value in contexts[sid].items() if old.get(name)!=value}
                # A missing completion entry can mean a data counterexample,
                # not changed documentation. Keep that interpretation available
                # for complete zero-model validation on the next run.
                invalidating_contexts[sid]={name for name,value in contexts[sid].items()
                    if name in old and old[name]!=value}
        affected_context_rules=set()
        def invalidate(rule_id, reason):
            affected_context_rules.update(store.invalidate_rule(rule_id,reason))
        if auto:
            for rule in old_rules:
                if rule.source.schema_name+'.'+rule.source.table in invalidating_contexts.get(rule.source.source_id,set()):
                    invalidate(rule.rule_id,'applicable_documents_changed')
        # A changed objective makes the old interpretation stale even if the
        # model now returns no replacement. Keep history, never expose old facts
        # as current results for the new goal or silently reuse them next run.
        if auto:
            changed_goals={sid for sid in rules_by_source
                if (marker:=store.current_versions('discovery:'+sid))
                and (marker.get('goal')!=digest(config.goal.model_dump())
                     or marker.get('workflow')!=DISCOVERY_WORKFLOW_VERSION
                     or marker.get('mode')!=discovery_mode)}
            for rule in old_rules:
                if rule.source.source_id in changed_goals:
                    invalidate(rule.rule_id,'discovery_goal_or_workflow_changed')
            if changed_goals:
                store.invalidate_rule('metadata-alignments','discovery_goal_or_workflow_changed')
                report.counts['goal_invalidated_rules']=sum(r.source.source_id in changed_goals for r in old_rules)
        goal_engine=RuleEngine(store,isolation_domain=config.isolation_domain)
        for r in old_rules:
            violation=_goal_violation(r,config.goal,goal_engine.columns(r))
            if violation:
                invalidate(r.rule_id,violation)
                continue
            table=table_map.get((r.source.source_id,r.source.schema_name,r.source.table))
            if not table and r.source.source_id in rules_by_source:
                invalidate(r.rule_id,'source_object_unavailable')
            elif table and r.dependencies.schema_ref!=RuleEngine.schema_fingerprint(table):
                invalidate(r.rule_id,'schema_changed')
            elif not set(r.dependencies.documents)<=document_versions:
                invalidate(r.rule_id,'document_changed')
        # Explicit withdrawals must also retire derived links before any new
        # metadata publication validates the currently visible graph.
        for rid in config.removed_rule_ids:invalidate(rid,'explicit_rule_removed')
        for did in config.removed_document_ids:
            store.invalidate_rule('document:'+did,'explicit_document_removed')
            store.invalidate_rule('document-alignment:'+did,'explicit_document_removed')
        for rule in store.get_rules():
            if any(d.split('@',1)[0] in config.removed_document_ids for d in rule.dependencies.documents):
                invalidate(rule.rule_id,'document_removed')
        # Derived document links cannot outlive endpoints invalidated above.
        # Rebuild these inexpensive alignments after the replacement publishes;
        # preserve original document claims and historical publications.
        if affected_context_rules:
            for head in store.heads():
                if head['valid'] and head['logical_rule_id'].startswith('document-alignment:'):
                    store.invalidate_rule(head['logical_rule_id'],'business_dependency_changed')
        if any(old_catalog[sid] and old_catalog[sid].get('schema')!=digest([t.model_dump(mode='json',by_alias=True) for t in rules_by_source[sid].describe()]) for sid in rules_by_source):
            store.invalidate_rule('metadata-alignments','catalog_changed')
        for adapter in adapters:
            if not adapter.describe():report.issues.append({'code':'source_catalog_empty_unverified'})
            report.counts['tables']=report.counts.get('tables',0)+publish_metadata(store,adapter,config.isolation_domain)
        cached=_latest(store.get_rules()) if auto and not config.rediscover else []
        need_discovery={sid for sid in rules_by_source if old_catalog[sid]!=store.current_versions('catalog:'+sid)}
        discovery_tables={sid:set(contexts[sid]) for sid in need_discovery}
        for sid,names in changed_contexts.items():
            if names:
                need_discovery.add(sid);discovery_tables.setdefault(sid,set()).update(names)
        for rule in old_rules:
            if rule.rule_id in affected_context_rules:
                sid=rule.source.source_id
                name=rule.source.schema_name+'.'+rule.source.table
                if sid in contexts and name in contexts[sid]:
                    need_discovery.add(sid)
                    discovery_tables.setdefault(sid,set()).add(name)
        if config.rediscover:need_discovery.update(rules_by_source)
        # A dependency removed above needs discovery even if other source rules remain valid.
        need_discovery.update(r.source.source_id for r in old_rules if not set(r.dependencies.documents)<=document_versions)
        for sid in rules_by_source:
            marker=store.current_versions('discovery:'+sid)
            if marker and (marker.get('goal')!=digest(config.goal.model_dump())
                           or marker.get('workflow')!=DISCOVERY_WORKFLOW_VERSION
                           or marker.get('mode')!=discovery_mode):
                need_discovery.add(sid);discovery_tables[sid]=set(contexts[sid])
        if auto:
            from .discovery.cache import validate_cached_patterns
            for sid,adapter in rules_by_source.items():
                review=validate_cached_patterns(adapter,[r for r in cached if r.source.source_id==sid],documents=list(documents))
                _issues(report,review.issues)
                if review.needs_review:
                    need_discovery.add(sid)
                    discovery_tables.setdefault(sid,set()).update(
                        r.source.schema_name+'.'+r.source.table for r in cached if r.rule_id in review.needs_review)
            by_table={(t.source_id,t.schema_name,t.name):t for t in tables}
            for rule in cached:
                table=by_table.get((rule.source.source_id,rule.source.schema_name,rule.source.table))
                valid=table and rule.dependencies.schema_ref==RuleEngine.schema_fingerprint(table)
                valid=valid and set(rule.dependencies.documents)<=document_versions
                if valid:allrules.append(rule)
                else:
                    store.invalidate_rule(rule.rule_id,'dependency_changed')
                    need_discovery.add(rule.source.source_id)
                    discovery_tables.setdefault(rule.source.source_id,set()).add(rule.source.schema_name+'.'+rule.source.table)
            for sid in rules_by_source:
                if not store.current_versions('discovery:'+sid) and (
                    store.current_versions('discovery-attempt:'+sid) or
                    not any(r.source.source_id==sid for r in allrules)):
                    need_discovery.add(sid)
            for sid in sorted(need_discovery):
                selected=set(contexts[sid]) if config.rediscover else discovery_tables.get(sid,set(contexts[sid]))
                if not config.rediscover:
                    selected=selected-ledger.unresolved_tables(sid,contexts[sid])
                discovery_tables[sid]=selected
                if not selected:
                    continue
                # This is intent, not completion. It distinguishes a crashed
                # automatic run from an existing manually supplied rule cache.
                attempt=store.begin_publication('discovery-attempt:'+sid,1,'all',{
                    'goal':digest(config.goal.model_dump()),'workflow':DISCOVERY_WORKFLOW_VERSION})
                prepared.append(attempt)
                store.publish_partition(PartitionCompletion(publication_id=attempt,
                    complete=True,consistent=True,dependencies_valid=True))
                work=[ledger.enqueue(SourceRef(source_id=sid,schema=t.schema_name,table=t.name),
                    task='metadata_discovery',context_version=contexts[sid][t.qualified_name],force=True)
                    for t in tables if t.source_id==sid and t.qualified_name in selected]
                for item in work:ledger.transition(item,'running')
                result=discover(rules_by_source[sid],config.goal,documents=corpus,model=discovery_model,
                                table_refs=selected,iteration_context=ledger.context(work,allrules,iteration=0))
                visited={u.source.schema_name+'.'+u.source.table for u in result.understandings}
                # An examined table without an executable interpretation is still
                # open work: new data must not require a forced rediscovery.
                modeled={r.source.schema_name+'.'+r.source.table for r in result.rules}
                discovery_completed[sid]=(visited & modeled & selected)-_deferred_tables(result,selected)
                for item in work:
                    name=item.source.schema_name+'.'+item.source.table
                    ledger.transition(item,'resolved' if name in discovery_completed[sid] else
                        'deferred' if name in _deferred_tables(result,selected) else 'unresolved')
                old={r.rule_id:r for r in _latest(store.get_rules(include_stale=True))}
                for rule in result.rules:
                    if rule.rule_id in old:rule.revision=old[rule.rule_id].revision+1
                allrules=[r for r in allrules if r.source.source_id!=sid
                          or r.source.schema_name+'.'+r.source.table not in selected]+result.rules
                _issues(report,result.issues)
                _discovery_counts(report,result)
                report.counts['discovered_rules']=report.counts.get('discovered_rules',0)+len(result.rules)
                _write_understanding(output,result.understandings)
                understandings.extend(result.understandings)
            report.counts['reused_rules']=sum(r.source.schema_name+'.'+r.source.table not in
                discovery_tables.get(r.source.source_id,set()) for r in allrules)
            report.validation['D']='model_run_unscored' if discovery_model and need_discovery else 'deterministic_or_reused_unscored'
        engine=RuleEngine(store,isolation_domain=config.isolation_domain,max_working_bytes=config.max_working_bytes,max_partitions=config.max_partitions)
        prepared_rules=[];failed=set();feedback={}
        allrules.sort(key=lambda r:r.kind=='reference')
        def execute(rule):
            code=None
            if time.monotonic()-started>config.max_run_seconds:code='wall_time_budget_exhausted'
            adapter=rules_by_source.get(rule.source.source_id)
            if not adapter:code='source_not_configured'
            if rule.relation and {rule.relation.source_rule_id,rule.relation.target_rule_id}&failed:code='dependency_rule_failed'
            code=_goal_violation(rule,config.goal,engine.columns(rule)) or code
            if code:
                failed.add(rule.rule_id);report.rules_skipped.append(rule.rule_id)
                report.issues.append({'rule_id':rule.rule_id,'code':code});return False
            previous=[r for r in prepared_rules if r.rule_id==rule.rule_id]
            if previous:
                _abort_staging(store,[p for p in prepared if store._publication(p)['logical_rule_id']==rule.rule_id])
                prepared_rules[:]=[r for r in prepared_rules if r.rule_id!=rule.rule_id]
            outcome=engine.execute(adapter,rule,document_versions=document_versions,defer_publish=True)
            if outcome.prepared:
                prepared.extend(outcome.publication_ids or [outcome.publication_id]);prepared_rules.append(rule)
                from .metadata import stage_rule_metadata
                for publication_id in outcome.active_publication_ids:
                    stage_rule_metadata(store,publication_id,rule,allrules+prepared_rules,config.isolation_domain)
            else:
                failed.add(rule.rule_id);report.rules_skipped.append(rule.rule_id)
                discovery_completed.get(rule.source.source_id,set()).discard(
                    rule.source.schema_name+'.'+rule.source.table)
                repair_codes=set(outcome.codes)&REVIEWABLE_CODES
                if repair_codes:
                    name=rule.source.schema_name+'.'+rule.source.table
                    sid=rule.source.source_id
                    need_discovery.add(sid)
                    discovery_tables.setdefault(sid,set()).add(name)
                    entry=feedback.setdefault((sid,name,rule.rule_id),{'rules':[],'codes':set(),'counts':{}})
                    entry['rules'].append(rule);entry['codes'].update(repair_codes)
                    if outcome.validation:
                        for key in ('distinct_tuples','conflicts'):
                            value=outcome.validation.checks.get(key,0)
                            if type(value) is int:entry['counts'][key]=entry['counts'].get(key,0)+value
                    # Keep the executable interpretation for zero-model rechecks,
                    # but stop presenting its last publication as current truth.
                    if auto:
                        store.mark_rule_for_review(rule.rule_id,'data_pattern_requires_review')
                        for head in store.heads():
                            if head['valid'] and head['logical_rule_id'].startswith('document-alignment:'):
                                store.invalidate_rule(head['logical_rule_id'],'business_requires_review')
            for code in outcome.codes:report.issues.append({'rule_id':rule.rule_id,'code':code})
            return outcome.prepared
        for rule in allrules:execute(rule)
        relation_role_context={}
        def review_role_occurrences():
            if not auto or not discovery_model:return
            from .identity.role_occurrences import detect_role_occurrences
            relation_role_context.clear()
            events,coverage=detect_role_occurrences(
                [r for r in prepared_rules if r.kind!='reference' and r.rule_id not in failed],
                engine.bindings,max_working_bytes=config.max_working_bytes)
            report.counts.update({'role_detection_'+k:v for k,v in coverage.items()})
            if any(coverage.get(k,0) for k in ('binding_limit_omissions','role_limit_omissions',
                        'event_limit_omissions','memory_limit_reached')):
                issue={'code':'role_detection_incomplete','status':'deferred'}
                if issue not in report.issues:report.issues.append(issue)
            for event in events:
                local=[r for r in prepared_rules if r.source==event.source and r.rule_id in event.rule_ids]
                if not local:continue
                sid=event.source.source_id;name=event.source.schema_name+'.'+event.source.table
                relation_role_context.setdefault((sid,name),[]).append({
                    'code':event.code,'column_refs':list(event.column_refs),'count':event.count})
                key=(sid,name,'roles:'+','.join(sorted(r.rule_id for r in local)))
                entry=feedback.setdefault(key,{'rules':local,'codes':set(),'counts':{},'column_refs':[]})
                entry['codes'].add(event.code)
                entry['counts']={'distinct_tuples':event.count,'conflicts':event.count}
                entry['column_refs']=sorted(set(entry.get('column_refs',[]))|set(event.column_refs))
                issue={'code':event.code,'table':name,'rule_id':local[0].rule_id,'count':event.count,'status':'unresolved'}
                if issue not in report.issues:report.issues.append(issue)
        def discover_pending_relations():
            """Track relation exploration separately from entity completeness.

            Unresolved T2 work gets one attempt on the next run. Changes to the
            validated endpoint set create a new task in this run after repair.
            No source values or value fingerprints enter this scheduler key.
            """
            if not auto:return
            endpoint_ids={r.rule_id for r in prepared_rules if r.kind!='reference' and r.rule_id not in failed}
            endpoints=[r for r in _latest(store.get_rules()) if r.rule_id in endpoint_ids]
            endpoint_count=sum(len(r.hierarchy.levels) if r.kind=='wide_hierarchy' else 1 for r in endpoints)
            if endpoint_count<2:return
            endpoint_versions=sorted((r.rule_id,r.revision) for r in endpoints)
            for sid in sorted(rules_by_source):
                if not discovery_model and sid not in need_discovery:continue
                for name,context_version in sorted(contexts[sid].items()):
                    if not discovery_model and name not in discovery_tables.get(sid,set(contexts[sid])):continue
                    source=next(t for t in tables if t.source_id==sid and t.qualified_name==name)
                    role_context=relation_role_context.get((sid,name),[])
                    role_columns=sorted({c for item in role_context for c in item['column_refs']})
                    role_codes=['unexplained_role_occurrence'] if role_context else []
                    version='relations_'+digest(context_version,endpoint_versions,role_context)
                    key=(sid,name,version)
                    if key in relation_attempts:continue
                    item=ledger.enqueue(SourceRef(source_id=sid,schema=source.schema_name,table=source.name),
                        task='relation_discovery',rules=endpoints,context_version=version,
                        codes=role_codes,column_refs=role_columns)
                    if item.state=='resolved' and not config.rediscover:continue
                    if item.state=='unresolved' or config.rediscover:
                        item=ledger.enqueue(item.source,task='relation_discovery',rules=endpoints,
                            context_version=version,force=True,codes=role_codes,column_refs=role_columns)
                    relation_attempts.add(key)
                    ledger.supersede_previous(item)
                    ledger.transition(item,'running')
                    result=discover_references(rules_by_source[sid],endpoints,documents=corpus,model=discovery_model,
                        goal=config.goal,source_adapters=rules_by_source,table_refs={name},
                        iteration_context={**ledger.context([item],allrules,iteration=rounds),
                                           'role_occurrences':role_context})
                    _issues(report,result.issues);_discovery_counts(report,result)
                    report.counts['relation_discovery_tasks']=report.counts.get('relation_discovery_tasks',0)+1
                    existing={r.rule_id:r for r in _latest(store.get_rules(include_stale=True))}
                    for rule in result.rules:
                        if rule.rule_id in existing:rule.revision=existing[rule.rule_id].revision+1
                        allrules[:]=[r for r in allrules if r.rule_id!=rule.rule_id]+[rule]
                        execute(rule)
                        if rule.rule_id not in failed:
                            discovery_completed.setdefault(sid,set()).add(name)
                            ledger.resolve_exploration(item.source,'metadata_discovery')
                    outcome=getattr(result,'relation_table_outcomes',{}).get(name)
                    if name in _deferred_tables(result,{name}) or outcome=='deferred':state='deferred'
                    elif any(r.rule_id in failed for r in result.rules):state='unresolved'
                    elif outcome:state=outcome
                    elif discovery_model:
                        state='resolved' if result.rules and not result.issues else 'unresolved'
                    else:state='resolved'
                    ledger.transition(item,state)
                    report.issues[:]=[i for i in report.issues if not (i.get('code')=='relation_discovery_pending' and i.get('table')==name)]
                    if state!='resolved':
                        report.issues.append({'code':'relation_discovery_pending','table':name,'status':state})
        relation_attempts=set();rounds=0
        review_role_occurrences()
        discover_pending_relations()
        if auto:
            from .discovery.coverage import discover_uncovered
            coverage_issues,coverage_stats,coverage_pending=discover_uncovered(
                tables=[t for adapter in scoped_adapters for t in adapter.describe()],
                contexts=contexts,adapters=rules_by_source,goal=config.goal,engine=engine,
                rules=allrules,understandings=understandings,ledger=ledger,corpus=corpus,
                model=discovery_model,max_rounds=config.max_discovery_rounds,execute=execute,validated_rules=prepared_rules)
            _issues(report,coverage_issues)
            for key,value in coverage_stats.items():report.counts[key]=report.counts.get(key,0)+value
            # Coverage work is persisted independently; do not repeat successful
            # T0 groups when a single remaining field still needs explanation.
            review_role_occurrences()
            discover_pending_relations()
        def review_generation_constraints():
            """Feed cross-rule conflicts back before a publication is attempted."""
            pubs=[store._publication(p) for p in prepared if store._publication(p)['state'] in ('staging','ready')]
            if not auto or not pubs:return
            try:
                store._validate_endpoints(store._prospective_publications(pubs))
            except LightGraphError as exc:
                if exc.code not in REVIEWABLE_CODES:return
                topology=exc.code in ('hierarchy_cycle','hierarchy_self_loop','multiple_parent')
                for rule in prepared_rules:
                    if topology and rule.kind not in ('wide_hierarchy','long_hierarchy'):continue
                    sid=rule.source.source_id;name=rule.source.schema_name+'.'+rule.source.table
                    entry=feedback.setdefault((sid,name,rule.rule_id),{'rules':[],'codes':set(),'counts':{}})
                    if rule.rule_id not in {r.rule_id for r in entry['rules']}:entry['rules'].append(rule)
                    entry['codes'].add(exc.code)
                    failed.add(rule.rule_id)
                    need_discovery.add(sid);discovery_tables.setdefault(sid,set()).add(name)
                    discovery_completed.get(sid,set()).discard(name)
                    store.mark_rule_for_review(rule.rule_id,'graph_constraint_requires_review')
                    report.issues.append({'rule_id':rule.rule_id,'code':exc.code})
        review_generation_constraints()
        review_role_occurrences()
        # A new Agent task consumes structured state, never a persisted source
        # transcript. Any revisit discards this attempt's staging and re-reads
        # complete units; earlier snapshots are not spliced into a new generation.
        while auto and feedback:
            work=[]
            for (sid,name,_),entry in sorted(feedback.items()):
                item=ledger.enqueue(entry['rules'][0].source,codes=entry['codes'],
                    rules=entry['rules'],context_version=contexts[sid][name],force=config.rediscover,
                    observation_counts=entry.get('counts'),column_refs=entry.get('column_refs',()))
                if item.state in ('pending','deferred'):
                    work.append((item,entry))
            if not work:
                break
            if rounds>=config.max_discovery_rounds-1:
                for item,_ in work:ledger.transition(item,'deferred')
                report.issues.append({'code':'discovery_round_budget_exhausted','count':len(work)})
                break
            rounds+=1;report.counts['feedback_rounds']=rounds
            _abort_staging(store,prepared)
            prepared_rules.clear()
            changed=False
            for item,entry in work:
                budget.check_time()
                ledger.transition(item,'running')
                sid=item.source.source_id;name=item.source.schema_name+'.'+item.source.table
                references_only=all(r.kind=='reference' for r in entry['rules'])
                affected_ids={r.rule_id for r in entry['rules']}
                def in_review(rule):
                    return rule.rule_id in affected_ids
                prior=[r for r in allrules if in_review(r)]
                context=ledger.context([item],allrules,iteration=rounds)
                if references_only:
                    endpoints=[r for r in _latest(store.get_rules()) if r.kind!='reference' and r.rule_id not in failed]
                    result=discover_references(rules_by_source[sid],endpoints,documents=corpus,model=discovery_model,
                        goal=config.goal,source_adapters=rules_by_source,table_refs={name},
                        iteration_context=context,revision_rules=entry['rules'])
                else:
                    result=discover(rules_by_source[sid],config.goal,documents=corpus,model=discovery_model,
                        table_refs={name},iteration_context=context,revision_rules=entry['rules'])
                _issues(report,result.issues)
                _discovery_counts(report,result)
                _write_understanding(output,result.understandings)
                understandings.extend(result.understandings)
                if _deferred_tables(result,{name}):
                    ledger.transition(item,'deferred');continue
                old_shapes={rule_shape(r) for r in prior}
                if not result.rules or {rule_shape(r) for r in result.rules}==old_shapes:
                    ledger.transition(item,'unresolved')
                    report.issues.append({'code':'pattern_review_no_progress','table':name})
                    continue
                if not affected_ids & {r.rule_id for r in result.rules}:
                    ledger.transition(item,'unresolved')
                    report.issues.append({'code':'revision_rule_mapping_required','table':name})
                    continue
                existing={r.rule_id:r for r in _latest(store.get_rules(include_stale=True))}
                for rule in result.rules:
                    if rule.rule_id in existing:rule.revision=existing[rule.rule_id].revision+1
                replacement_ids={r.rule_id for r in result.rules}
                allrules=[r for r in allrules if r.rule_id not in replacement_ids]+result.rules
                discovery_completed.setdefault(sid,set()).add(name)
                report.counts['revised_rules']=report.counts.get('revised_rules',0)+len(result.rules)
                changed=True
            # Even an abstention interrupted this generation. Re-execute all
            # dependency units before publishing any successful replacements.
            failed.clear();feedback.clear()
            report.rules_skipped=[]
            before=len(report.issues)
            for rule in sorted(allrules,key=lambda r:r.kind=='reference'):execute(rule)
            # A repaired/new entity can enable relations that had no endpoints
            # during the first T2 pass. Revisit against this validated generation.
            review_role_occurrences()
            discover_pending_relations()
            review_generation_constraints()
            failing_ids=set(failed)|{r.rule_id for entry in feedback.values() for r in entry['rules']}
            report.issues=[issue for index,issue in enumerate(report.issues)
                if not (index<before and issue.get('rule_id') not in failing_ids
                        and issue.get('code') in REVIEWABLE_CODES)]
            for item,_ in work:
                if not any(r.rule_id in item.rule_revisions for entry in feedback.values() for r in entry['rules']):
                    ledger.transition(item,'resolved')
                elif item.state=='running':ledger.transition(item,'unresolved')
            if not changed:
                break
        if auto:
            from .discovery.coverage import unexplained_columns
            for table in tables:
                if table.qualified_name not in contexts.get(table.source_id,{}):continue
                if not unexplained_columns(table,prepared_rules,understandings,config.goal,engine):
                    ledger.resolve_exploration(SourceRef(source_id=table.source_id,schema=table.schema_name,table=table.name),
                                               'coverage_discovery')
                    report.issues[:]=[i for i in report.issues if not (
                        i.get('table')==table.qualified_name and i.get('code')=='unexplained_columns')]
        failed_tables={(r.source.source_id,r.source.schema_name,r.source.table) for r in allrules if r.rule_id in failed}
        for rule in prepared_rules:
            if (rule.source.source_id,rule.source.schema_name,rule.source.table) not in failed_tables:
                ledger.resolve_table(rule.source)
        # One atomic visibility change for the current dependency set. If an old
        # dependent edge would dangle, the whole replacement remains unpublished.
        business_prepared=[p for p in prepared if store._publication(p)['state'] in ('staging','ready')]
        publish_ok=not business_prepared
        if business_prepared:
            try:
                # Retire derived alignments in the same transaction as replacing
                # their possible endpoints. Original document claims stay intact.
                for head in store.heads():
                    if head['valid'] and head['logical_rule_id'].startswith('document-alignment:'):
                        replacement=store.begin_publication(head['logical_rule_id'],1,'all',{'refresh':report.run_id})
                        prepared.append(replacement);business_prepared.append(replacement)
                budget.check_time()
                from .identity.reconciliation import GenerationView
                generation=GenerationView.capture(store,business_prepared,set(config.scopes),config.isolation_domain)
                store.publish_partitions([PartitionCompletion(publication_id=p,complete=True,consistent=True,dependencies_valid=True)
                    for p in business_prepared],expected_heads=generation.heads)
                report.rules_published=[r.rule_id for r in prepared_rules]
                publish_ok=True
            except LightGraphError as exc:
                _abort_staging(store,prepared)
                report.rules_skipped.extend(r.rule_id for r in prepared_rules)
                report.issues.append({'code':exc.code});report.status='PARTIAL'
        if auto:
            for sid in need_discovery:
                previous=store.current_versions('discovery:'+sid) or {}
                matching=(previous.get('goal')==digest(config.goal.model_dump()) and
                    previous.get('workflow')==DISCOVERY_WORKFLOW_VERSION and previous.get('mode')==discovery_mode)
                completed_versions=_context_versions(previous) if matching else {}
                # Do not advance failed or unvisited tables. Missing entries are
                # retried next run even when another table published successfully.
                for name in discovery_tables.get(sid,set(contexts[sid])):
                    completed_versions.pop(name,None)
                if publish_ok:
                    completed_versions.update({name:contexts[sid][name]
                        for name in discovery_completed.get(sid,set()) if name in contexts[sid]})
                marker=store.begin_publication('discovery:'+sid,1,'all',{'goal':digest(config.goal.model_dump()),
                    'workflow':DISCOVERY_WORKFLOW_VERSION,'mode':discovery_mode,
                    'contexts':[canonical([name,value]) for name,value in sorted(completed_versions.items())]})
                prepared.append(marker)
                store.publish_partition(PartitionCompletion(publication_id=marker,complete=True,consistent=True,dependencies_valid=True))
        covered_tables={r.source.schema_name+'.'+r.source.table for r in prepared_rules if r.rule_id in report.rules_published}
        report.issues=[i for i in report.issues if not (i.get('code')=='no_confirmable_entity_group' and i.get('table') in covered_tables)]
        if auto:
            # Rule-cache hits do not establish coverage for the remaining tables.
            # Keep unmodeled tables visible on every run without recalling a model.
            mapped={(r.source.source_id,r.source.schema_name,r.source.table) for r in _latest(store.get_rules())}
            for table in tables:
                from .discovery.goal import source_in_goal
                if not source_in_goal(config.goal,table):continue
                if (table.source_id,table.schema_name,table.name) not in mapped:
                    issue={'code':'no_confirmable_entity_group','table':table.qualified_name}
                    if not any(i.get('code')==issue['code'] and i.get('table')==issue['table'] for i in report.issues):
                        report.issues.append(issue)
        if discover_only:
            accepted=[r for r in _latest(store.get_rules()) if r.rule_id in report.rules_published]
            write_rules(output/'rules.yaml',accepted)
        else:
            count,issues=publish_documents(store,documents,config.isolation_domain,model,config.scopes,
                config.document_scopes,check_time=budget.check_time,goal=config.goal)
            report.counts['document_claims']=count;report.issues.extend(issues)
            budget.check_time()
            _publish_document_alignments(store,[d for d in documents if d.document_id not in config.removed_document_ids],config)
            _publish_alignments(store,tables,config,report)
            from .identity.reconciliation import record_candidates
            candidates=record_candidates(store,store.view(set(config.scopes),config.isolation_domain,
                include_candidates=True,apply_resolutions=False),set(config.scopes),config.isolation_domain,
                max_entities=config.max_reconciliation_entities,max_candidates=config.max_reconciliation_candidates)
            report.counts.update({'reconciliation_'+key:value for key,value in candidates.items()})
            from .identity.reconciliation import review_with_agent
            reviewed,review_issues=review_with_agent(store,model,config.goal,corpus,
                scopes=set(config.scopes),domain=config.isolation_domain,max_reviews=config.max_identity_reviews)
            report.counts.update({'identity_'+key:value for key,value in reviewed.items()})
            _issues(report,review_issues)
        write_catalog(output,declared,semantic_catalog(declared,config.goal,understandings,
            _latest(store.get_rules()),documents,previous=previous_semantic))
        report.validation['incremental_mode']='complete_projection_reconciliation'
        report.validation['iteration_mode']='bounded_pattern_feedback'
        if report.issues and report.status=='COMPLETED':
            partial_tokens=('budget','exhausted','timeout','truncated','unavailable','permission','incomplete','unverified','formula_cache_missing','execution_failed','dependency_rule_failed','too_large','model_http','model_response','model_transport')
            report.status='PARTIAL' if any(
                i.get('status') in ('deferred','partial') or _agent_execution_failure(str(i.get('code',''))) or
                any(t in str(i.get('code','')) for t in partial_tokens)
                for i in report.issues) else 'COMPLETED_WITH_GAPS'
    except (LightGraphError,KeyboardInterrupt) as exc:
        _abort_staging(store,prepared)
        code=exc.code if isinstance(exc,LightGraphError) else 'cancelled'
        report.status='PARTIAL';report.issues.append({'code':code})
    except Exception:
        _abort_staging(store,prepared)
        report.status='FAILED';report.issues.append({'code':'internal_error'})
    finally:
        # Post-publication document/reconciliation failures must not suppress
        # files for the already committed, authorized graph (including empty).
        if not discover_only and store.current_versions('workspace')=={'domain':config.isolation_domain}:
            try:
                export_graph(store,output,scopes=set(config.scopes),domain=config.isolation_domain)
                report.validation['graph_export']='written'
            except Exception:
                report.validation['graph_export']='failed'
                report.issues.append({'code':'graph_export_failed'})
                if report.status in ('COMPLETED','COMPLETED_WITH_GAPS'):report.status='PARTIAL'
        else:report.validation['graph_export']='not_requested'
        try:
            _graph_outcome(report,store,config,discover_only=discover_only)
        except Exception:
            # Do not replace the original extraction failure with a reporting
            # failure, or mistake unavailable graph state for an empty graph.
            report.validation['business_graph']='unavailable'
        if 'ledger' in locals():
            # Pending work remains resumable after budget failures and interrupts.
            for item in ledger.items():
                if item.state=='running':ledger.transition(item,'deferred')
            report.counts.update(ledger.counts())
            if report.counts.get('work_deferred',0) and report.status in ('COMPLETED','COMPLETED_WITH_GAPS'):
                report.status='PARTIAL'
        report.counts['elapsed_seconds']=round(time.monotonic()-started,3)
        report.counts['llm_calls']=model.usage['calls'] if model else 0
        report.counts['db_queries']=budget.snapshot()['queries'] if 'budget' in locals() else sum(a.stats.queries for a in adapters)
        report.counts['returned_rows']=budget.snapshot()['returned_rows'] if 'budget' in locals() else sum(a.stats.returned_rows for a in adapters)
        report.counts['returned_bytes']=budget.snapshot()['returned_bytes'] if 'budget' in locals() else sum(a.stats.returned_bytes for a in adapters)
        report.counts['scanned_rows_known']=all(a.stats.scanned_rows is not None for a in adapters)
        write_report(output/'report.json',report)
        store.close()
    return report


def _write_understanding(output,understandings):
    from .discovery.models import DataUnderstanding
    for u in understandings:
        if not isinstance(u,DataUnderstanding):raise LightGraphError('typed_understanding_required')
        lines=['---','purpose: data_description','derived: true','dependencies: '+str([u.dependency_hash]+u.rule_refs+u.document_refs),'---',
            '# 自动生成的数据说明',f'来源：{u.source.source_id}.{u.source.schema_name}.{u.source.table}',
            f'覆盖：{u.coverage}；样本不能证明全量语义。','',
            '| 列 | 注释质量 | 角色假设 | 冲突代码 |','|---|---|---|---|']
        for c in u.columns:
            values=[c.name,c.comment_quality,c.inferred_role,', '.join(c.conflicts)]
            lines.append('| '+' | '.join(v.replace('|',r'\|').replace('\n',' ') for v in values)+' |')
        path=output/'understanding'/('table_'+digest(u.source.model_dump())[:16]+'.md')
        atomic_text(path,'\n'.join(lines)+'\n')


def _publish_alignments(store,tables,config,report):
    from .metadata_alignment import discover_metadata_alignments
    rules=store.get_rules(scopes=set(config.scopes),domain=config.isolation_domain)
    result=discover_metadata_alignments(rules,tables,domain=config.isolation_domain)
    _issues(report,result.issues)
    report.counts['column_alignment_candidates']=len(result.candidates)
    report.counts['column_alignment_deferred']=result.deferred_count
    rid='metadata-alignments';version={'alignment':result.version}
    if not result.complete:
        if store.current_versions(rid)!=version:store.invalidate_rule(rid,'alignment_dependencies_changed')
        return
    if store.current_versions(rid)==version:return
    pub=store.begin_publication(rid,1,'all',version)
    try:
        for item in result.candidates:
            store.stage_fact(pub,item.fact(config.isolation_domain),item.support(rid))
        store.publish_partition(PartitionCompletion(publication_id=pub,complete=True,consistent=True,dependencies_valid=True))
    except BaseException:
        store.abort(pub);raise


def _publish_document_alignments(store,documents,config):
    """Align accepted visible labels to existing claims; never re-call the model."""
    graph=store.view(set(config.scopes)&set(config.document_scopes),domain=config.isolation_domain,include_candidates=True)
    known={}
    for node in graph['nodes']:
        if node['layer']=='business' and node['status']=='accepted':
            for label in [node['label']]+node['aliases']:known.setdefault(label,set()).add(node['id'])
    support_by_id={s['id']:s for s in graph['supports']}
    from types import SimpleNamespace
    current=[]
    for head in store.heads():
        if head['valid'] and head['logical_rule_id'].startswith('document:'):
            versions=store.current_versions(head['logical_rule_id'])
            if versions and '@' in versions.get('document',''):
                did,revision=versions['document'].rsplit('@',1)
                current.append(SimpleNamespace(document_id=did,revision=revision))
    for document in current:
        ref=document.document_id+'@'+document.revision
        claims=[c for c in graph['claims'] if any((support_by_id.get(s,{}).get('document_ref') or '').startswith(ref+'#') for s in c['support_ids'])]
        bindings=[]
        for claim in claims:
            targets=known.get(claim['subject'],set())
            if not targets:
                # A unique longest accepted label inside the subject provides a
                # reviewable alignment only. It never upgrades the claim's truth.
                matches=[label for label in known if len(label)>=2 and label in claim['subject']]
                if matches:
                    longest=max(map(len,matches))
                    targets=set().union(*(known[label] for label in matches if len(label)==longest))
            if len(targets)!=1:continue
            method='analysis:'+digest(config.isolation_domain,claim['predicate'],claim['object'])[:24]
            if not any(n['id']==method for n in graph['nodes']):continue
            bindings.append((method,next(iter(targets)),claim['modality']))
        rid='document-alignment:'+document.document_id
        version={'document':ref,'bindings':digest(sorted(bindings)),'scopes':sorted(config.document_scopes)}
        if store.current_versions(rid)==version:continue
        pub=store.begin_publication(rid,1,'all',version)
        try:
            for method,target,modality in bindings:
                for scope in config.document_scopes:
                    support=SupportSpec(origin='document_claim',source_binding=ref,scope_ref=scope,
                        partition_id='all',rule_id=rid,rule_revision=1,document_ref=ref,status='candidate')
                    store.stage_fact(pub,GraphFact(isolation_domain=config.isolation_domain,fact_kind='Edge',
                        payload={'source':method,'relation':'analyzes','target':target,'qualifiers':{'modality':modality}}),support)
            store.publish_partition(PartitionCompletion(publication_id=pub,complete=True,consistent=True,dependencies_valid=True))
        except BaseException:
            store.abort(pub);raise
