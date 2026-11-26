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
    c=sub.add_parser('export-graph',help='导出授权范围内已发布的图谱')
    c.add_argument('config');c.add_argument('output')
    c.add_argument('--format', choices=('yaml', 'semantica-json', 'graphml'), default='yaml')
    c.add_argument('--business-context', '--applicability', type=json.loads, metavar='JSON', help='限定资料的适用范围')
    c=sub.add_parser('export-structure',help='导出已发布的结构与映射 YAML，不连接数据源');c.add_argument('config');c.add_argument('output')
    c = sub.add_parser('analyze-graph', help='计算或读取已发布全图的社区与中心度')
    c.add_argument('config')
    c=sub.add_parser('merge-graphs',help='确定性合并多单元/继承图谱为标准化终态图谱');c.add_argument('config');c.add_argument('--unit',action='append',default=[])
    c = sub.add_parser('retrieve-subgraph', help='按文本或节点 ID 查询已发布图谱')
    c.add_argument('config')
    c.add_argument('query', nargs='?')
    c.add_argument('--sync', action='store_true', help='先更新当前图谱的检索索引')
    c.add_argument('--mode', choices=('search', 'drilldown', 'auto', 'dimensional', 'traverse', 'hierarchy'))
    c.add_argument('--field', choices=('all', 'name', 'description'))
    c.add_argument('--method', choices=('hybrid', 'keyword', 'vector'))
    c.add_argument('--node-id', help='精确查找节点，配合 --hops 可展开邻域')
    c.add_argument('--business-context', '--applicability', type=json.loads, metavar='JSON', help='限定资料的适用范围')
    c.add_argument('--condition', dest='conditions', action='append', type=json.loads,
                   help='联合检索的条件 JSON，可重复')
    c.add_argument('--drill-role', help='在已满足条件的事实中下钻的维度')
    c.add_argument('--seed-id', dest='seed_ids', action='append', help='路径节点，可重复')
    c.add_argument('--relation-type', dest='relation_types', action='append', help='路径允许经过的关系类型，可重复')
    c.add_argument('--include-endpoint-variants', action='store_true', default=None,
                   help='按原关系名查询时，包含因端点类型改变而生成的同一关系的分支')
    c.add_argument('--direction', choices=('incoming', 'outgoing', 'both'))
    c.add_argument('--hierarchy-direction', choices=('parents', 'children'), help='查看指定节点的直接上级或下级')
    c.add_argument('--target-id')
    c.add_argument('--target-kind', help='多层关联路径的目标节点类型')
    for option in ('hops', 'top-n', 'top-p-seeds', 'max-depth', 'max-nodes', 'max-edges',
                   'max-per-rel', 'hub-degree-limit'):
        c.add_argument('--' + option, type=int)
    c=sub.add_parser('embed-server',help='启动可替换的本地 OpenAI 兼容向量嵌入服务（支持 Qwen3-Embedding-0.6B）')
    c.add_argument('--model',default='Qwen/Qwen3-Embedding-0.6B',help='本地目录或 HuggingFace 模型名')
    c.add_argument('--served-model-name', help='API 对外使用的模型名，适用于从本地目录加载权重')
    c.add_argument('--host',default='127.0.0.1')
    c.add_argument('--port',type=int,default=8002)
    c.add_argument('--backend',choices=('auto','sentence_transformers','transformers'),default='auto')
    c.add_argument('--device',default=None)
    c.add_argument('--trust-remote-code',action='store_true')
    c=sub.add_parser('schema');c.add_argument('output')
    c=sub.add_parser('retain-check');c.add_argument('paths',nargs='+');c.add_argument('--marker',action='append',required=True)
    return p


def _read_published(config_path, read, *, built_config=None):
    """Apply the workbench's live grants and payload revision checks to CLI reads."""
    path = Path(config_path).resolve()
    if path.name != 'config.yaml' or not (path.parent / 'project.yaml').is_file():
        config = built_config if built_config is not None else load_config(config_path)
        if any(ref.source_project_id for ref in getattr(config, 'inherited_graphs', ())):
            raise LightGraphError('inherited_graph_workspace_required')
        return read(config, None)

    from .construction.analytics_store import current_version
    from .web import graph_history
    from .web.workspace import Workspace
    workspace = Workspace(path.parent.parent)

    def pinned(ws, pid, *, config):
        # A CLI build writes its configured output. The workbench may currently
        # be showing a different generation; authorize the version just built.
        target = config
        if built_config is not None:
            target = config.model_copy(update={'output_dir': built_config.output_dir})
        version = current_version(target)
        if version:
            entry = graph_history.resolve_version(ws, pid, config, version)
            target = graph_history._authorize_entry(ws, entry, config)
        return {'version_id': version, 'result': read(target, version or '')}

    return graph_history.read_consistently(workspace, path.parent.name, pinned)['result']


def _analyze_after_build(config_path, config):
    import logging
    from .construction.analytics_store import analyze_current
    try:
        _read_published(config_path, analyze_current, built_config=config)
    except Exception:
        logging.getLogger(__name__).warning('Graph published; offline graph analytics require a retry with analyze-graph')


def _run_workflow(config_path, config):
    """Use the same scheduler, checkpoints and publication contract as the workbench."""
    import time
    from .web.execution import RunManager, TERMINAL_STATUSES
    from .web.saved_connections import SavedConnections
    from .web.workspace import Workspace

    path = Path(config_path).resolve()
    managed = path.name == 'config.yaml' and (path.parent / 'project.yaml').is_file()
    if managed:
        pid = path.parent.name
        workspace = Workspace(path.parent.parent)
    else:
        from .standalone_workflow import StandaloneWorkflow
        workspace = StandaloneWorkflow(path, config)
        pid = workspace.project_id

    manager = RunManager(workspace, standalone=not managed)
    try:
        credentials = {}
        model_api_key = None
        if managed:
            config = workspace.resolved_config(pid)
            vault = SavedConnections(workspace.root)
            for source in config.sources:
                if source.credential_mode == 'session':
                    saved = vault.load(pid, source)
                    if saved:
                        credentials[source.id] = saved['dsn']
            model_api_key = vault.load_model(pid, config)
        record = manager.start(pid, source_credentials=credentials, model_api_key=model_api_key)
        while record['status'] not in TERMINAL_STATUSES:
            time.sleep(0.2)
            record = manager.get(pid, record['run_id'])
        if (record.get('workflow_state', {}).get('merge') or {}).get('publication_status') == 'published':
            _analyze_after_build(path, config)
        emit(record)
        return 0 if record['status'] in ('completed', 'completed_with_gaps') else 2 if record['status'] == 'partial' else 1
    finally:
        manager.shutdown()


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
        if args.command in ('run', 'build-graph') and config.workflow and config.workflow.enabled:
            return _run_workflow(args.config, config)
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
            if args.command != 'discover' and report.status in {'COMPLETED', 'COMPLETED_WITH_GAPS', 'PARTIAL'}:
                _analyze_after_build(args.config, config)
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
    if args.command == 'analyze-graph':
        from .construction.analytics_store import analyze_current
        emit(_read_published(args.config, analyze_current))
        return 0
    if args.command=='export-graph':
        from .web.construction_graph import load_graph, select_business_context
        from .construction.graph_export import serialize_graph
        from .construction.contexts import normalize_context_selection
        selection = normalize_context_selection(args.business_context) if args.business_context is not None else None
        def export(config, version):
            graph = load_graph(config)
            if version is not None and not version:
                raise LightGraphError('graph_version_not_found')
            if version is not None and graph.get('version_id') != version:
                raise LightGraphError('graph_read_conflict')
            graph = select_business_context(graph, config, selection)
            return serialize_graph(graph, args.format), {'nodes': len(graph['nodes']), 'edges': len(graph['edges'])}
        payload, counts = _read_published(args.config, export)
        atomic_text(args.output, payload)
        emit({'status':'exported', **counts, 'output':str(Path(args.output).resolve()),'format':args.format})
        return 0
    if args.command=='export-structure':
        from .construction.structure_export import load_published_structures
        from .construction.workspace import write_yaml
        structure=load_published_structures(load_config(args.config))
        write_yaml(Path(args.output), structure)
        emit({'status':'exported','target':'graph_structure','plans':len(structure['plans']),
              'data_validation':'not_run','instance_enumeration':'not_run',
              'output':str(Path(args.output).resolve()),'format':'yaml'});return 0
    if args.command=='merge-graphs':
        from .construction.workflow import merge_project_workflow, resolve_workflow_plan
        from .web.execution import verified_unit_loader
        config = load_config(args.config)
        plan = resolve_workflow_plan(config.workflow) if config.workflow else None
        if plan and plan.merge_order:
            raise LightGraphError('workflow_dag_requires_run')
        mode = {'direct_single': 'direct_output', 'direct_multi': 'direct_multi_output'}.get(plan.mode, 'merged') if plan else 'merged'
        units = args.unit or (plan.extract_ids if plan else None)
        path = Path(args.config).resolve()
        project = path.parent if path.name == 'config.yaml' and (path.parent / 'project.yaml').is_file() else None
        loader = verified_unit_loader(config, project) if plan else None
        summary = merge_project_workflow(config.output_dir, config, unit_ids=units, mode=mode, load_committed=loader)
        _analyze_after_build(args.config, config)
        emit({'status':'merged', **summary});return 0
    if args.command=='retrieve-subgraph':
        from .web.construction_graph import project_retriever
        from .retrieval.query import RetrieveSubgraphRequest
        params = {key: value for key, value in vars(args).items()
                  if key in RetrieveSubgraphRequest.model_fields and value is not None}
        request = RetrieveSubgraphRequest.model_validate(params)
        def query(config, version):
            options = {} if version is None else {'expected_version': version}
            retriever, _ = project_retriever(config, refresh=args.sync, **options)
            return request.run(retriever, config.scopes)
        emit(_read_published(args.config, query))
        return 0
    if args.command=='embed-server':
        from .retrieval.local_embed_server import load_local_backend, create_embedding_app
        import uvicorn
        backend = load_local_backend(args.model, backend=args.backend, device=args.device,
                                     trust_remote_code=args.trust_remote_code)
        if args.served_model_name:
            backend.model_name = args.served_model_name
        print(f'Embedding 服务已启动：http://{args.host}:{args.port}/v1  模型：{backend.model_name}（维度 {backend.dim}）')
        uvicorn.run(create_embedding_app(backend), host=args.host, port=args.port, access_log=False)
        return 0
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
