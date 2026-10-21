"""Command-line entry points; failures expose codes, never source or provider bodies."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from .contracts import RuleSpec, RunReport, CapabilityManifest
from .errors import LightGraphError
from .runtime.config import AgentConfig, RunConfig, load_config, open_sources
from .runtime.retention import atomic_text, retain_check


def emit(value):
    if hasattr(value, 'model_dump'): value=value.model_dump(mode='json',by_alias=True)
    print(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2))


def parser():
    p=argparse.ArgumentParser(prog='lightgraph',description='只读数据探索与轻量知识图谱构建')
    p.add_argument('--version',action='version',version='lightgraph 1.0.0')
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('run','discover','connect-check'):
        c=sub.add_parser(name);c.add_argument('config')
        if name=='connect-check':
            c.add_argument('--verify-runtime',action='store_true',help='执行有界超时和取消探针；请在隔离测试环境使用')
            c.add_argument('--output',help='保存类型化能力清单，不含连接信息')
    c=sub.add_parser('sample');c.add_argument('directory');c.add_argument('--rows',type=int,default=5000)
    c=sub.add_parser('demo');c.add_argument('--output',default='runs/demo');c.add_argument('--rows',type=int,default=5000)
    c=sub.add_parser('verify');c.add_argument('--output',default='runs/acceptance');c.add_argument('--rows',type=int,default=5000)
    for name in ('inspect','search','export'):
        c=sub.add_parser(name);c.add_argument('store');c.add_argument('--scope',action='append',default=[])
        c.add_argument('--domain',default='default');c.add_argument('--include-candidates',action='store_true')
        if name=='search':c.add_argument('query')
        if name=='export':c.add_argument('output')
    c=sub.add_parser('hierarchy-view',help='从指定实体展开层级视图，不改写实体或关系')
    c.add_argument('store');c.add_argument('--root',required=True);c.add_argument('--taxonomy',required=True)
    c.add_argument('--scope',action='append',default=[]);c.add_argument('--domain',default='default')
    c.add_argument('--max-depth',type=int,default=32);c.add_argument('--publication')
    c.add_argument('--context',action='append',default=[],metavar='KEY=VALUE',
                   help='层级上下文，如 period=2026；支持 JSON 标量或普通文本')
    c=sub.add_parser('report');c.add_argument('path')
    c=sub.add_parser('import-graph');c.add_argument('snapshot');c.add_argument('store')
    c=sub.add_parser('validate');c.add_argument('rules',nargs='+')
    c=sub.add_parser('schema');c.add_argument('output')
    c=sub.add_parser('retain-check');c.add_argument('paths',nargs='+');c.add_argument('--marker',action='append',required=True)
    c=sub.add_parser('rollback');c.add_argument('config');c.add_argument('rule_id');c.add_argument('revision',type=int)
    return p


def run(args):
    from .pipeline import run_pipeline,load_rules,make_model
    from .documents import load_documents
    if args.command=='sample':
        from .sample import write_sample
        if not 100 <= args.rows <= 100000:raise LightGraphError('sample_size_out_of_range')
        write_sample(args.directory,args.rows)
        emit({'status':'created','directory':str(Path(args.directory).resolve()),'synthetic':True});return 0
    if args.command=='verify':
        from .acceptance import verify
        if not 100 <= args.rows <= 100000:raise LightGraphError('sample_size_out_of_range')
        result=verify(args.output,args.rows);emit(result);return 1 if result['status']=='FAIL' else 0
    if args.command=='demo':
        from .sample import build_sample,sample_rules,DESCRIPTION,ANALYSIS
        from .sources import MemorySource
        from .documents import load_document
        if not 100 <= args.rows <= 100000:raise LightGraphError('sample_size_out_of_range')
        tables,rows=build_sample(args.rows)
        source=MemorySource(tables,rows)
        config=RunConfig(sources=[],output_dir=str(Path(args.output).resolve()),scopes=['public','A','B'])
        documents=[load_document('description.md',text=DESCRIPTION),load_document('analysis.md',text=ANALYSIS)]
        try:report=run_pipeline(config,[source],rules=sample_rules(),documents=documents)
        finally:source.close()
        emit(report);return 2 if report.status=='PARTIAL' else (1 if report.status=='FAILED' else 0)
    if args.command in ('run','discover','connect-check','rollback'):
        config=load_config(args.config)
        sources=open_sources(config,metadata_only=args.command=='connect-check')
        try:
            if args.command=='connect-check':
                manifests={}
                for source in sources:
                    # Source construction already performed catalog/readonly
                    # preflight. The explicit opt-in adds runtime probes only.
                    if args.verify_runtime and hasattr(source,'verify_runtime_controls'):
                        manifest=source.verify_runtime_controls()
                    else:manifest=source.capabilities()
                    tables=source.describe();sid=tables[0].source_id if tables else 'empty'
                    manifests[sid]=manifest.model_dump(mode='json')
                if args.output:atomic_text(args.output,json.dumps(manifests,ensure_ascii=False,indent=2))
                emit(manifests);return 0
            rules=load_rules(config.rules) if config.rules else None
            documents=load_documents(config.documents)
            if args.command=='rollback':
                from .store import GraphStore
                from .rules import RuleEngine
                from .contracts import Dependencies
                with GraphStore(Path(config.output_dir)/'graph.sqlite') as store:
                    history=store.get_rules(include_stale=True)
                    valid=store.get_rules()
                    old=next((r for r in history if r.rule_id==args.rule_id and r.revision==args.revision),None)
                    if old is None:raise LightGraphError('rollback_revision_missing')
                    table=next((t for source in sources for t in source.describe()
                        if (t.source_id,t.schema_name,t.name)==(old.source.source_id,old.source.schema_name,old.source.table)),None)
                    if table is None:raise LightGraphError('rollback_source_unavailable')
                    document_versions={d.document_id+'@'+d.revision for d in documents}
                    dependencies=Dependencies().model_dump(mode='json',by_alias=True)
                    dependencies['schema']=RuleEngine.schema_fingerprint(table)
                    dependencies['documents']=[d for d in old.dependencies.documents if d in document_versions]
                    old=store.prepare_rule_rollback(args.rule_id,args.revision,dependencies)

                def latest(items):
                    selected={}
                    for item in items:
                        if item.rule_id not in selected or item.revision>selected[item.rule_id].revision:
                            selected[item.rule_id]=item
                    return selected
                historical=latest(history);current=latest(valid);configured=latest(rules or [])
                candidates={**historical,**current,**configured}
                related={args.rule_id}
                # The current and old definition can point to different parents.
                # Revalidate both dependency sides, including referencing rules
                # whose old supports would otherwise keep stale identity edges.
                changed=True
                while changed:
                    changed=False
                    for candidate in list(candidates.values())+[old]:
                        if not candidate.relation:continue
                        endpoints={candidate.relation.source_rule_id,candidate.relation.target_rule_id}
                        if candidate.rule_id in related or endpoints&related:
                            addition={candidate.rule_id}|endpoints
                            if not addition<=related:
                                related.update(addition);changed=True
                if related&set(config.removed_rule_ids):
                    raise LightGraphError('rollback_dependency_explicitly_removed')
                selected=dict(configured)
                valid_revisions={(r.rule_id,r.revision) for r in valid}
                historical_revisions={(r.rule_id,r.revision) for r in history}
                newest=latest(history+list(configured.values()))
                for rid in sorted(related):
                    candidate=old if rid==args.rule_id else candidates.get(rid)
                    if candidate is None:raise LightGraphError('rollback_dependency_missing')
                    stale=(candidate.rule_id,candidate.revision) in historical_revisions and (candidate.rule_id,candidate.revision) not in valid_revisions
                    if rid==args.rule_id or stale:
                        candidate=candidate.model_copy(deep=True)
                        candidate.revision=newest[rid].revision+1
                        candidate.status='draft';candidate.validation_ref=None
                    selected[rid]=candidate
                rules=list(selected.values())
            report=run_pipeline(config,sources,rules=rules,documents=documents,
                model=make_model(config),discover_only=args.command=='discover')
            emit(report);return 2 if report.status=='PARTIAL' else (1 if report.status=='FAILED' else 0)
        finally:
            for source in sources:source.close()
    if args.command=='validate':
        from .rules.compiler import validate_plan
        rules=load_rules(args.rules)
        for rule in rules:validate_plan(rule)
        emit({'status':'schema_valid','rules':len(rules),'source_validation':'not_run'});return 0
    if args.command=='schema':
        out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
        from .catalog import DeclaredCatalog,SemanticCatalog
        from .rules.views import HierarchyView
        from .discovery.models import TaskPacket,ProposalBatch
        for cls in (RuleSpec,RunConfig,AgentConfig,RunReport,CapabilityManifest,DeclaredCatalog,
                    SemanticCatalog,HierarchyView,TaskPacket,ProposalBatch):
            atomic_text(out/(cls.__name__+'.schema.json'),json.dumps(cls.model_json_schema(),ensure_ascii=False,indent=2))
        from .presentation.schema import snapshot_schema
        atomic_text(out/'GraphSnapshot.schema.json',json.dumps(snapshot_schema(),ensure_ascii=False,indent=2))
        emit({'status':'created','output':str(out.resolve())});return 0
    if args.command=='import-graph':
        from .presentation.importer import import_graph
        emit(import_graph(args.snapshot,args.store));return 0
    if args.command=='report':
        path=Path(args.path);path=path/'report.json' if path.is_dir() else path
        emit(RunReport.model_validate_json(path.read_text()));return 0
    if args.command=='retain-check':
        result=retain_check(args.paths,args.marker);emit(result)
        return 1 if result.get('violations') else 0
    from .store import GraphStore
    with GraphStore(args.store) as store:
        scopes=set(args.scope or ['public'])
        if args.command=='hierarchy-view':
            from .rules.views import HierarchyView
            qualifiers={}
            for item in args.context:
                key,separator,value=item.partition('=')
                if not separator or key in qualifiers:raise LightGraphError('invalid_hierarchy_view_context')
                try:qualifiers[key]=json.loads(value)
                except ValueError:qualifiers[key]=value
            definition=HierarchyView(taxonomy=args.taxonomy,root_entity=args.root,
                max_depth=args.max_depth,context_qualifiers=qualifiers)
            emit(store.hierarchy_view(scopes,definition,domain=args.domain,publication_id=args.publication))
        elif args.command=='export':
            from .presentation import export_graph
            graph=export_graph(store,args.output,scopes=scopes,domain=args.domain,include_candidates=args.include_candidates)
            emit({'nodes':len(graph['nodes']),'edges':len(graph['edges']),'output':str(Path(args.output).resolve())})
        else:
            graph=store.view(scopes,domain=args.domain,include_candidates=args.include_candidates)
            if args.command=='search':
                needle=args.query.casefold()
                emit({'nodes':[n for n in graph['nodes'] if needle in json.dumps(n,ensure_ascii=False).casefold()]})
            else:emit(graph)
    return 0


def main(argv=None):
    args=parser().parse_args(argv)
    try:return run(args)
    except KeyboardInterrupt:
        emit({'status':'PARTIAL','code':'cancelled'});return 130
    except LightGraphError as exc:
        emit({'status':'FAILED','code':exc.code});return 1
    except (ValueError,TypeError,KeyError):
        emit({'status':'FAILED','code':'invalid_input'});return 1
    except Exception:
        # Provider/driver/path exceptions may include credentials, SQL or rows.
        emit({'status':'FAILED','code':'internal_error'});return 1

if __name__=='__main__':sys.exit(main())
