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
    for name in ('run','discover','build-graph','connect-check'):
        c=sub.add_parser(name);c.add_argument('config')
        if name=='connect-check':
            c.add_argument('--verify-runtime',action='store_true',help='执行有界超时和取消探针；请在隔离测试环境使用')
            c.add_argument('--output',help='保存类型化能力清单，不含连接信息')
    c=sub.add_parser('serve',help='启动本地 Vue 工作台与项目 API')
    c.add_argument('--port',type=int,default=8000)
    c.add_argument('--workspace',help='项目文件根目录；默认仓库根目录 workspace')
    c.add_argument('--frontend-dir',help='已构建的 Vue dist 目录')
    c=sub.add_parser('sample');c.add_argument('directory');c.add_argument('--rows',type=int,default=5000)
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
    c=sub.add_parser('evaluate',help='离线评分；Gold 仅供评测，不调用模型')
    c.add_argument('graph');c.add_argument('--gold',required=True);c.add_argument('--output',required=True)
    c.add_argument('--store');c.add_argument('--run-report');c.add_argument('--usage');c.add_argument('--manifest')
    c=sub.add_parser('evaluate-concepts',help='按明确类型和关系映射评分独立概念及联合组合，不调用模型')
    c.add_argument('graph');c.add_argument('--gold',required=True);c.add_argument('--output',required=True)
    c.add_argument('--mapping',required=True,help='JSON with role_kinds, role_relations and context_kind; mapping is not inferred.')
    c=sub.add_parser('report');c.add_argument('path')
    c=sub.add_parser('import-graph');c.add_argument('snapshot');c.add_argument('store')
    c=sub.add_parser('validate',help='校验新图谱 YAML 草稿格式，不查询数据');c.add_argument('drafts',nargs='+')
    c=sub.add_parser('export-graph',help='导出当前授权范围内已发布的图谱 YAML');c.add_argument('config');c.add_argument('output')
    c=sub.add_parser('export-structure',help='导出已发布的结构与映射 YAML，不连接数据源');c.add_argument('config');c.add_argument('output')
    c=sub.add_parser('schema');c.add_argument('output')
    c=sub.add_parser('retain-check');c.add_argument('paths',nargs='+');c.add_argument('--marker',action='append',required=True)
    return p


def run(args):
    if args.command=='serve':
        if not 1<=args.port<=65535:raise LightGraphError('invalid_port')
        import uvicorn
        from .web.app import create_app
        app=create_app(args.workspace,frontend_dir=args.frontend_dir)
        uvicorn.run(app,host='127.0.0.1',port=args.port,access_log=False)
        return 0
    if args.command=='evaluate':
        from .evaluation import evaluate
        result=evaluate(args.graph,args.gold,store=args.store,run_report=args.run_report,
                        usage=args.usage,manifest=args.manifest)
        atomic_text(args.output,result.model_dump_json(indent=2))
        emit(result);return 1 if result.status=='GRAPH_FILE_MISSING' else 0
    if args.command=='evaluate-concepts':
        from .evaluation.concepts import evaluate_concepts
        mapping=json.loads(Path(args.mapping).read_text())
        result=evaluate_concepts(args.graph,args.gold,**mapping)
        atomic_text(args.output,json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        emit(result);return 0
    from .pipeline import run_pipeline
    if args.command=='sample':
        from .sample import write_sample
        if not 100 <= args.rows <= 100000:raise LightGraphError('sample_size_out_of_range')
        write_sample(args.directory,args.rows)
        emit({'status':'created','directory':str(Path(args.directory).resolve()),'synthetic':True});return 0
    if args.command in ('run','discover','build-graph','connect-check'):
        config=load_config(args.config)
        if args.command != 'connect-check':
            from .construction.access import ensure_supported_access
            ensure_supported_access(config)
            if config.rules:
                raise LightGraphError('legacy_rule_input_removed')
        sources=open_sources(config,metadata_only=args.command=='connect-check' or
                             config.resolved_model.mode in {"disabled", "metadata_only"})
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
            report=run_pipeline(config,sources,discover_only=args.command=='discover')
            emit(report);return 2 if report.status=='PARTIAL' else (1 if report.status=='FAILED' else 0)
        finally:
            for source in sources:source.close()
    if args.command=='validate':
        from .construction.workspace import parse_graph_draft
        from pydantic import ValidationError
        drafts=[]
        try:
            for name in args.drafts:
                drafts.append(parse_graph_draft(Path(name).read_text(encoding='utf-8')))
        except ValidationError:
            raise LightGraphError('invalid_graph_draft') from None
        emit({'status':'schema_valid','drafts':[d.draft_id for d in drafts],
              'data_validation':'not_run','semantic_assessment':'not_run'});return 0
    if args.command=='export-graph':
        from .web.construction_graph import load_graph
        from .construction.workspace import write_yaml
        config=load_config(args.config)
        graph=load_graph(config)
        write_yaml(Path(args.output), graph)
        emit({'status':'exported','nodes':len(graph['nodes']),'edges':len(graph['edges']),
              'output':str(Path(args.output).resolve()),'format':'yaml'});return 0
    if args.command=='export-structure':
        from .construction.structure_export import load_published_structures
        from .construction.workspace import write_yaml
        structure=load_published_structures(load_config(args.config))
        write_yaml(Path(args.output), structure)
        emit({'status':'exported','target':'graph_structure','plans':len(structure['plans']),
              'data_validation':'not_run','instance_enumeration':'not_run',
              'output':str(Path(args.output).resolve()),'format':'yaml'});return 0
    if args.command=='schema':
        out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
        from .catalog import DeclaredCatalog,SemanticCatalog
        from .rules.views import HierarchyView
        from .discovery.models import TaskPacket,ProposalBatch
        for cls in (RuleSpec,RunConfig,AgentConfig,RunReport,CapabilityManifest,DeclaredCatalog,
                    SemanticCatalog,HierarchyView,TaskPacket,ProposalBatch):
            atomic_text(out/(cls.__name__+'.schema.json'),json.dumps(cls.model_json_schema(),ensure_ascii=False,indent=2))
        from .construction.models import GraphDraft, DataValidation, EvidenceRecord
        for cls in (GraphDraft,DataValidation,EvidenceRecord):
            atomic_text(out/(cls.__name__+'.schema.json'),json.dumps(cls.model_json_schema(),ensure_ascii=False,indent=2))
        from .presentation.schema import snapshot_schema
        atomic_text(out/'GraphSnapshot.schema.json',json.dumps(snapshot_schema(),ensure_ascii=False,indent=2))
        emit({'status':'created','output':str(out.resolve())});return 0
    if args.command=='import-graph':
        from .presentation.importer import import_graph
        emit(import_graph(args.snapshot,args.store));return 0
    if args.command=='report':
        from .construction.workspace import safe_load_yaml
        path=Path(args.path)
        if path.is_dir():path=path/('report.yaml' if (path/'report.yaml').exists() else 'report.json')
        value=safe_load_yaml(path.read_text(encoding='utf-8')) if path.suffix in {'.yaml','.yml'} else json.loads(path.read_text(encoding='utf-8'))
        emit(RunReport.model_validate(value));return 0
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
