"""End-to-end orchestration with safe, outcome-oriented reports."""
from __future__ import annotations
import time
import uuid
from pathlib import Path
import yaml
from .contracts import RuleSpec, RunReport, GraphFact, SupportSpec, PartitionCompletion, digest
from .errors import LightGraphError
from .rules import RuleEngine
from .metadata import publish_metadata
from .runtime.retention import write_report,write_rules,atomic_text

DISCOVERY_WORKFLOW_VERSION='goal_tools/1'


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
    if config.model.mode=='disabled':return None
    from .discovery.llm import StructuredModelClient,ModelConfig
    from .discovery.budget import BudgetAllocator,Cost
    client=StructuredModelClient(ModelConfig(base_url=config.model.base_url,model=config.model.model,
        api_key_env=config.model.api_key_env,timeout_seconds=config.model.timeout_seconds,
        max_discovery_steps=config.model.max_discovery_steps,max_tool_calls=config.model.max_tool_calls,
        allow_source_samples=config.model.mode in ("in_environment","approved_external")),
        BudgetAllocator(Cost(db_cu=config.max_db_queries,llm_calls=config.model.max_calls,
            llm_tokens=config.model.max_tokens,memory_bytes=config.max_working_bytes,seconds=config.max_run_seconds)))
    return client


def publish_documents(store,documents,domain,model=None,scopes=("public",),document_scopes=("public",),check_time=None):
    from .documents import extract_with_cache
    from .documents.cache import extractor_fingerprint
    count=0;issues=[]
    snapshot=store.view(set(scopes)&set(document_scopes),domain=domain)
    known={}
    for n in snapshot.get('nodes',[]):known.setdefault(n.get('label',''),[]).append(n.get('id',n.get('entity_id')))
    for document in documents:
        if check_time:check_time()
        rid='document:'+document.document_id
        versions={'document':document.document_id+'@'+document.revision,'scopes':sorted(document_scopes),
                  'document_extractor':extractor_fingerprint(model)}
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
            extracted=extract_with_cache(document,model=model,known_entities=known,cached_sections=prior_cache)
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
    if rule.identity.entity_kind not in goal.entity_kinds:return 'outside_extraction_goal'
    if rule.relation and rule.relation.relation not in goal.relation_kinds:return 'outside_relation_goal'
    if rule.kind in ('wide_hierarchy','long_hierarchy') and 'parent_of' not in goal.relation_kinds:return 'outside_relation_goal'
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
    auto=rules is None
    discovery_mode='agent_tools' if callable(getattr(model,'explore',None)) else 'deterministic'
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
        budget.check_time()
        tables=[t for a in adapters for t in a.describe()]
        from .sources.scoped import GoalScopedSource
        scoped_adapters=[GoalScopedSource(a,config.goal) for a in adapters]
        rules_by_source={t.source_id:a for a in scoped_adapters for t in a.describe()}
        # Invalidate stale dependent metadata edges before replacing a catalogue.
        table_map={(t.source_id,t.schema_name,t.name):t for t in tables}
        old_catalog={sid:store.current_versions('catalog:'+sid) for sid in rules_by_source}
        old_rules=store.get_rules()
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
                    store.invalidate_rule(rule.rule_id,'discovery_goal_or_workflow_changed')
            if changed_goals:
                store.invalidate_rule('metadata-alignments','discovery_goal_or_workflow_changed')
                report.counts['goal_invalidated_rules']=sum(r.source.source_id in changed_goals for r in old_rules)
        goal_engine=RuleEngine(store,isolation_domain=config.isolation_domain)
        for r in old_rules:
            violation=_goal_violation(r,config.goal,goal_engine.columns(r))
            if violation:
                store.invalidate_rule(r.rule_id,violation)
                continue
            table=table_map.get((r.source.source_id,r.source.schema_name,r.source.table))
            if not table and r.source.source_id in rules_by_source:
                store.invalidate_rule(r.rule_id,'source_object_unavailable')
            elif table and r.dependencies.schema_ref!=RuleEngine.schema_fingerprint(table):
                store.invalidate_rule(r.rule_id,'schema_changed')
            elif not set(r.dependencies.documents)<=document_versions:
                store.invalidate_rule(r.rule_id,'document_changed')
        if any(old_catalog[sid] and old_catalog[sid].get('schema')!=digest([t.model_dump(mode='json',by_alias=True) for t in rules_by_source[sid].describe()]) for sid in rules_by_source):
            store.invalidate_rule('metadata-alignments','catalog_changed')
        for adapter in adapters:
            if not adapter.describe():report.issues.append({'code':'source_catalog_empty_unverified'})
            report.counts['tables']=report.counts.get('tables',0)+publish_metadata(store,adapter,config.isolation_domain)
        # Removal is explicit: an omitted input path may be a temporary outage.
        for rid in config.removed_rule_ids:store.invalidate_rule(rid,'explicit_rule_removed')
        for did in config.removed_document_ids:
            store.invalidate_rule('document:'+did,'explicit_document_removed')
            store.invalidate_rule('document-alignment:'+did,'explicit_document_removed')
        for rule in store.get_rules():
            if any(d.split('@',1)[0] in config.removed_document_ids for d in rule.dependencies.documents):
                store.invalidate_rule(rule.rule_id,'document_removed')
        cached=_latest(store.get_rules()) if auto and not config.rediscover else []
        need_discovery={sid for sid in rules_by_source if old_catalog[sid]!=store.current_versions('catalog:'+sid)}
        if config.rediscover:need_discovery.update(rules_by_source)
        # A dependency removed above needs discovery even if other source rules remain valid.
        need_discovery.update(r.source.source_id for r in old_rules if not set(r.dependencies.documents)<=document_versions)
        for sid in rules_by_source:
            marker=store.current_versions('discovery:'+sid)
            if marker and (marker.get('goal')!=digest(config.goal.model_dump())
                           or marker.get('workflow')!=DISCOVERY_WORKFLOW_VERSION
                           or marker.get('mode')!=discovery_mode):need_discovery.add(sid)
        if auto:
            from .discovery.cache import validate_cached_patterns
            for sid,adapter in rules_by_source.items():
                review=validate_cached_patterns(adapter,[r for r in cached if r.source.source_id==sid],documents=list(documents))
                _issues(report,review.issues)
                if review.needs_review:need_discovery.add(sid)
            by_table={(t.source_id,t.schema_name,t.name):t for t in tables}
            for rule in cached:
                table=by_table.get((rule.source.source_id,rule.source.schema_name,rule.source.table))
                valid=table and rule.dependencies.schema_ref==RuleEngine.schema_fingerprint(table)
                valid=valid and set(rule.dependencies.documents)<=document_versions
                if valid:allrules.append(rule)
                else:
                    store.invalidate_rule(rule.rule_id,'dependency_changed')
                    need_discovery.add(rule.source.source_id)
            for sid in rules_by_source:
                if not any(r.source.source_id==sid for r in allrules):need_discovery.add(sid)
            for sid in need_discovery:
                result=discover(rules_by_source[sid],config.goal,documents=list(documents),model=model)
                old={r.rule_id:r for r in _latest(store.get_rules(include_stale=True))}
                for rule in result.rules:
                    if rule.rule_id in old:rule.revision=old[rule.rule_id].revision+1
                allrules=[r for r in allrules if r.source.source_id!=sid]+result.rules
                _issues(report,result.issues)
                report.counts['discovered_rules']=report.counts.get('discovered_rules',0)+len(result.rules)
                _write_understanding(output,result.understandings)
            report.counts['reused_rules']=len([r for r in allrules if r.source.source_id not in need_discovery])
            report.validation['D']='model_run_unscored' if model and need_discovery else 'deterministic_or_reused_unscored'
        engine=RuleEngine(store,isolation_domain=config.isolation_domain,max_working_bytes=config.max_working_bytes,max_partitions=config.max_partitions)
        prepared_rules=[];failed=set()
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
                report.issues.append({'rule_id':rule.rule_id,'code':code});return
            outcome=engine.execute(adapter,rule,document_versions=document_versions,defer_publish=True)
            if outcome.prepared:
                prepared.extend(outcome.publication_ids or [outcome.publication_id]);prepared_rules.append(rule)
                from .metadata import stage_rule_metadata
                for publication_id in outcome.active_publication_ids:
                    stage_rule_metadata(store,publication_id,rule,allrules+prepared_rules,config.isolation_domain)
            else:
                failed.add(rule.rule_id);report.rules_skipped.append(rule.rule_id)
            for code in outcome.codes:report.issues.append({'rule_id':rule.rule_id,'code':code})
        for rule in allrules:execute(rule)
        if auto and need_discovery:
            for sid in need_discovery:
                endpoint_ids={r.rule_id for r in prepared_rules if r.kind!='reference' and r.source.source_id==sid}
                endpoints=[r for r in _latest(store.get_rules()) if r.rule_id in endpoint_ids]
                result=discover_references(rules_by_source[sid],endpoints,documents=list(documents),model=model,goal=config.goal)
                _issues(report,result.issues)
                existing={r.rule_id:r for r in _latest(store.get_rules(include_stale=True))}
                for rule in result.rules:
                    if rule.rule_id in existing:rule.revision=existing[rule.rule_id].revision+1
                    execute(rule)
        # One atomic visibility change for the current dependency set. If an old
        # dependent edge would dangle, the whole replacement remains unpublished.
        if prepared:
            try:
                # Retire derived alignments in the same transaction as replacing
                # their possible endpoints. Original document claims stay intact.
                for head in store.heads():
                    if head['valid'] and head['logical_rule_id'].startswith('document-alignment:'):
                        prepared.append(store.begin_publication(head['logical_rule_id'],1,'all',{'refresh':report.run_id}))
                budget.check_time()
                store.publish_partitions([PartitionCompletion(publication_id=p,complete=True,consistent=True,dependencies_valid=True) for p in prepared])
                report.rules_published=[r.rule_id for r in prepared_rules]
                if auto:
                    for sid in need_discovery:
                        marker=store.begin_publication('discovery:'+sid,1,'all',{'goal':digest(config.goal.model_dump()),
                            'workflow':DISCOVERY_WORKFLOW_VERSION,'mode':discovery_mode})
                        store.publish_partition(PartitionCompletion(publication_id=marker,complete=True,consistent=True,dependencies_valid=True))
            except LightGraphError as exc:
                for pub in prepared:store.abort(pub)
                report.rules_skipped.extend(r.rule_id for r in prepared_rules)
                report.issues.append({'code':exc.code});report.status='PARTIAL'
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
            count,issues=publish_documents(store,[d for d in documents if d.document_id not in config.removed_document_ids],config.isolation_domain,model,config.scopes,config.document_scopes,check_time=budget.check_time)
            report.counts['document_claims']=count;report.issues.extend(issues)
            budget.check_time()
            _publish_document_alignments(store,[d for d in documents if d.document_id not in config.removed_document_ids],config)
            _publish_alignments(store,tables,config,report)
            export_graph(store,output,scopes=set(config.scopes),domain=config.isolation_domain)
        report.validation['incremental_mode']='complete_projection_reconciliation'
        if report.issues and report.status=='COMPLETED':
            partial_tokens=('budget','exhausted','timeout','truncated','unavailable','permission','incomplete','unverified','formula_cache_missing','execution_failed','dependency_rule_failed')
            report.status='PARTIAL' if any(any(t in str(i.get('code','')) for t in partial_tokens) for i in report.issues) else 'COMPLETED_WITH_GAPS'
    except (LightGraphError,KeyboardInterrupt) as exc:
        for pub in prepared:
            try:store.abort(pub)
            except Exception:pass
        code=exc.code if isinstance(exc,LightGraphError) else 'cancelled'
        report.status='PARTIAL';report.issues.append({'code':code})
    except Exception:
        for pub in prepared:
            try:store.abort(pub)
            except Exception:pass
        report.status='FAILED';report.issues.append({'code':'internal_error'})
    finally:
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
