"""Freeze local inputs and code, then execute only the verified package snapshot.

Gold is hashed for provenance and exclusion checks, never copied into the bundle
or opened by the execution worker. Live database inputs are intentionally rejected.
An explicit resume copies a completed result database after input/scope checks.
"""
from __future__ import annotations
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from .core import fingerprint, load
from lightgraph.errors import LightGraphError


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)+'\n', encoding='utf-8')


def environment():
    return {'python': sys.version.split()[0], 'packages': {
        name: importlib.metadata.version(name) for name in
        ('agentscope','openai','pydantic','httpx','PyYAML','sqlglot','openpyxl','jsonschema')}}


def files_under(directory):
    return {str(p.relative_to(directory)): fingerprint(p) for p in sorted(directory.rglob('*')) if p.is_file()}


def _resume_contract(config):
    """Paths may move; authorized inputs, semantics and permissions must not."""
    sources=[]
    for source in config.sources:
        item=source.model_dump(mode='json')
        path=item.pop('path', None)
        item.pop('capability_manifest', None)
        if not path or not Path(path).is_file():
            raise LightGraphError('evaluation_resume_input_missing')
        item['content_sha256']=fingerprint(path)
        if source.kind=='csv_file':
            item['table_name']=source.table_name or Path(path).stem
        sources.append(item)
    return {'sources':sources, 'documents':[fingerprint(p) for p in config.documents],
        'rules':[fingerprint(p) for p in config.rules], 'goal':config.goal.model_dump(mode='json'),
        'scopes':sorted(config.scopes), 'document_scopes':sorted(config.document_scopes),
        'domain':config.isolation_domain, 'removed_rule_ids':sorted(config.removed_rule_ids),
        'removed_document_ids':sorted(config.removed_document_ids), 'mode':config.resolved_model.mode}


def _prepare_resume(parent, target, config):
    """Back up the completed parent without changing its graph or frozen files."""
    from lightgraph.runtime.config import load_config
    from lightgraph.runtime.locking import run_lock
    from lightgraph.contracts import digest
    parent=Path(parent).resolve()
    verify(parent)
    terminal=load(parent/'execution.json') if (parent/'execution.json').is_file() else {}
    if terminal.get('status') not in {'PARTIAL','COMPLETED','COMPLETED_WITH_GAPS'}:
        raise LightGraphError('evaluation_resume_parent_not_completed')
    previous=load_config(parent/'config.json')
    if config.rediscover:
        raise LightGraphError('evaluation_resume_requires_incremental')
    if _resume_contract(previous)!=_resume_contract(config):
        raise LightGraphError('evaluation_resume_context_changed')
    database=parent/'output/graph.sqlite'
    if not database.is_file():
        raise LightGraphError('evaluation_resume_store_missing')
    seed=target/'resume_seed';seed.mkdir()
    # The original run may have WAL pages. A raw file copy is not a snapshot.
    with run_lock(parent/'output'):
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as source:
            with sqlite3.connect(seed/'graph.sqlite') as destination:
                source.backup(destination)
                if destination.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                    raise LightGraphError('evaluation_resume_store_invalid')
        for name in ('metadata','understanding'):
            if (parent/'output'/name).is_dir():
                shutil.copytree(parent/'output'/name,seed/name)
    frozen=load_config(target/'config.json')
    def scope_key(cfg):
        return digest(cfg.goal.model_dump(mode='json'),sorted(cfg.scopes),cfg.isolation_domain,
            [s.model_dump(mode='json',by_alias=True) for s in cfg.sources])
    old_key,new_key=scope_key(previous),scope_key(frozen)
    with sqlite3.connect(seed/'graph.sqlite') as connection:
        tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ('discovery_candidate','discovery_candidate_event','discovery_candidate_replacement'):
            if name in tables and old_key!=new_key:
                connection.execute(f'UPDATE {name} SET scope_key=? WHERE scope_key=?',(new_key,old_key))
    return {'parent_bundle':str(parent), 'parent_manifest_sha256':fingerprint(parent/'bundle.json'),
        'parent_report_sha256':fingerprint(parent/'output/report.json'),
        'seed_database_sha256':fingerprint(seed/'graph.sqlite'),
        'candidate_scope_rebound':old_key!=new_key,
        'input_contract_sha256':digest(_resume_contract(config)),
        'mode':'resume_and_full_revalidation', 'parent_status':terminal['status']}


def prepare(config_path, output, *, gold_path, resume_from=None):
    from lightgraph.runtime.config import load_config
    config = load_config(config_path)
    settings = config.resolved_model
    if settings.api_key is not None and settings.api_key.get_secret_value():
        raise LightGraphError('evaluation_requires_environment_credentials')
    # Calls and wall time bound a frozen experiment. Token totals may explicitly
    # be uncapped; keep null in the snapshot and continue recording actual use.
    if settings.mode != 'disabled' and settings.max_calls is None:
        raise LightGraphError('evaluation_requires_finite_call_budget')
    if any(s.kind not in ('csv_file','excel_workbook') for s in config.sources):
        raise LightGraphError('evaluation_requires_frozen_local_sources')
    # Hash-only access: the evaluator is the sole component that interprets Gold.
    gold_file = Path(gold_path).resolve()
    gold_hash = fingerprint(gold_file)
    sources = [(s.path, f'source_{index}') for index, s in enumerate(config.sources)]
    documents = [(p, f'document_{index}') for index, p in enumerate(config.documents)]
    rules = [(p, f'rule_{index}') for index, p in enumerate(config.rules)]
    assets = sources + documents + rules
    for path, _ in assets:
        if path is None or not Path(path).is_file(): raise LightGraphError('evaluation_input_missing')
        if Path(path).resolve() == gold_file or fingerprint(path) == gold_hash:
            raise LightGraphError('evaluation_gold_in_model_inputs')
    target = Path(output).resolve()
    if target.exists(): raise LightGraphError('evaluation_bundle_exists')
    if target == gold_file or target in gold_file.parents:
        raise LightGraphError('evaluation_gold_inside_bundle')
    package = Path(__file__).resolve().parents[1]
    target.mkdir(parents=True)
    try:
        shutil.copytree(package, target/'code/lightgraph',
                        ignore=shutil.ignore_patterns('__pycache__','tests','*.pyc'))
        (target/'inputs').mkdir()
        remap = {}
        for path, label in assets:
            destination = target/'inputs'/(label+Path(path).suffix)
            shutil.copyfile(path, destination)
            if fingerprint(path) != fingerprint(destination):
                raise LightGraphError('evaluation_input_changed_during_freeze')
            remap[str(Path(path).resolve())] = str(destination)
        data = config.model_dump(mode='json')
        for item in data['sources']:
            if item['kind']=='csv_file' and not item.get('table_name'):
                # Freezing changes the filename, not the authorized table name.
                item['table_name']=Path(item['path']).stem
            item['path'] = remap[str(Path(item['path']).resolve())]
            item['capability_manifest'] = None
        data['documents'] = [remap[str(Path(p).resolve())] for p in config.documents]
        data['rules'] = [remap[str(Path(p).resolve())] for p in config.rules]
        data['output_dir'] = str(target/'output')
        if config.agent_config:
            write(target/'agent.json', config._agent.model_dump(mode='json'))
            data['agent_config'] = str(target/'agent.json')
        write(target/'config.json', data)
        resume=_prepare_resume(resume_from,target,config) if resume_from is not None else None
        current_code = files_under(package)
        frozen_code = files_under(target/'code/lightgraph')
        if any(current_code.get(name) != digest for name, digest in frozen_code.items()):
            raise LightGraphError('evaluation_code_changed_during_freeze')
        protected = files_under(target)
        manifest = {'version':'lightgraph.frozen/1', 'created_at':datetime.now(timezone.utc).isoformat(),
            'bundle_root':str(target), 'files':protected, 'environment':environment(),
            'gold_sha256':gold_hash, 'gold_copied':False,
            'gold_isolation':'Configured input/document/rule paths and whole-file hashes exclude Gold; this does not prove documents lack semantically leaked answers.',
            'model':settings.model, 'mode':settings.mode,
            'budgets':{'calls':settings.max_calls,'tokens':settings.max_tokens,'seconds':config.max_run_seconds},
            'configured_input_files':sorted(str(Path(v).relative_to(target)) for v in remap.values())}
        if resume is not None:manifest['resume']=resume
        write(target/'bundle.json', manifest)
        return manifest
    except Exception:
        # Preserve a failed preparation for inspection; never claim it runnable.
        write(target/'preparation_failed.json', {'status':'FAILED'})
        raise


def verify(bundle):
    root = Path(bundle).resolve()
    manifest = load(root/'bundle.json')
    if manifest.get('version') != 'lightgraph.frozen/1' or manifest.get('bundle_root') != str(root):
        raise LightGraphError('evaluation_bundle_invalid')
    if (root/'preparation_failed.json').exists(): raise LightGraphError('evaluation_bundle_invalid')
    for relative, digest in manifest['files'].items():
        path = (root/relative).resolve()
        if root not in path.parents or not path.is_file() or fingerprint(path) != digest:
            raise LightGraphError('evaluation_frozen_file_changed')
    # An added module could change lazy import behavior even with every old hash intact.
    current_code = {str(p.relative_to(root)) for p in (root/'code').rglob('*')
                    if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    if current_code != {p for p in manifest['files'] if p.startswith('code/')}:
        raise LightGraphError('evaluation_frozen_code_changed')
    if manifest.get('resume'):
        current_seed={str(p.relative_to(root)) for p in (root/'resume_seed').rglob('*') if p.is_file()}
        if current_seed != {p for p in manifest['files'] if p.startswith('resume_seed/')}:
            raise LightGraphError('evaluation_frozen_seed_changed')
    if environment() != manifest['environment']:
        raise LightGraphError('evaluation_runtime_environment_changed')
    return manifest


def launch(bundle):
    root = Path(bundle).resolve()
    verify(root)
    if (root/'execution.json').exists(): raise LightGraphError('evaluation_run_already_started')
    env = dict(os.environ)
    env['PYTHONPATH'] = str(root/'code')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # No original harness path or Gold path is passed to the worker.
    return subprocess.call([sys.executable, '-P', '-m', 'lightgraph.evaluation.frozen', str(root)],
                           env=env, cwd=root)


def execute(bundle):
    root = Path(bundle).resolve()
    manifest=verify(root)
    import lightgraph
    if Path(lightgraph.__file__).resolve() != root/'code/lightgraph/__init__.py':
        raise LightGraphError('evaluation_wrong_loaded_package')
    with (root/'execution.json').open('x', encoding='utf-8') as handle:
        json.dump({'status':'RUNNING'}, handle)
    from lightgraph.runtime.config import load_config, open_sources
    from lightgraph.pipeline import make_model, run_pipeline, load_rules
    from lightgraph.documents import load_documents
    config = load_config(root/'config.json')
    sources, model, status, error = [], None, 'FAILED', None
    start = time.monotonic()
    try:
        if manifest.get('resume'):
            # Restore only the hash-protected seed, never read the parent again.
            if (root/'output').exists():raise LightGraphError('evaluation_resume_output_exists')
            shutil.copytree(root/'resume_seed',root/'output')
        model = make_model(config)
        sources = open_sources(config)
        report = run_pipeline(config, sources, model=model,
                              rules=load_rules(config.rules) if config.rules else None,
                              documents=load_documents(config.documents))
        status = report.status
        return 2 if status == 'PARTIAL' else (1 if status == 'FAILED' else 0)
    except LightGraphError as exc:
        error = exc.code
        return 1
    except Exception:
        error = 'evaluation_execution_failed'
        return 1
    finally:
        for source in sources: source.close()
        write(root/'model_usage.json', {'usage':model.usage if model else {},
                                      'budget':model.budget.snapshot() if model else {}})
        integrity = True
        try: verify(root)
        except LightGraphError: integrity = False
        write(root/'execution.json', {'status':status, 'error':error, 'elapsed_seconds':round(time.monotonic()-start,6),
            'finished_at':datetime.now(timezone.utc).isoformat(), 'frozen_files_unchanged':integrity,
            'loaded_package':str(Path(lightgraph.__file__).resolve()), 'gold_loaded_by_worker':False,
            'execution_mode':'resume_and_full_revalidation' if manifest.get('resume') else 'fresh'})


if __name__ == '__main__':
    try: sys.exit(execute(sys.argv[1]))
    except Exception:
        print(json.dumps({'status':'FAILED','code':'evaluation_worker_failed'}))
        sys.exit(1)
