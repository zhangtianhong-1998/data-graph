"""Exercise generated files through actual config, source and document readers."""
import importlib.util
import json
from pathlib import Path
import pytest
from lightgraph.runtime.config import load_config,open_sources
from lightgraph.documents import load_documents,DocumentCorpus
from lightgraph.evaluation.core import fingerprint
from lightgraph.contracts import SourceRef


def generator():
    spec=importlib.util.spec_from_file_location('fixture_generator',Path(__file__).parents[3]/'examples/generate_fruit_workbook.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_generated_configs_load_only_inputs_and_documents_bind_to_correct_tables(tmp_path):
    module=generator();root=tmp_path/'fixture';stats=module.generate(root)
    assert stats['hierarchy_cross_depth_entities']>0 and stats['hierarchy_internal_fill_rows']>0
    assert stats['hierarchy_rows']>stats['hierarchy_distinct_rows']
    assert stats['concept_contexts']<stats['concept_cartesian_contexts']
    for mode,count in [('hierarchy',1),('concepts',1),('full',5)]:
        config=load_config(root/f'config-{mode}.json');sources=open_sources(config)
        try:
            assert config.max_run_seconds==7200
            assert config.max_run_seconds*(1-config.discovery_time_fraction)==1800
            assert config.max_db_queries==3000
            assert config.max_db_queries*(1-config.discovery_query_fraction)==750
            assert config.resolved_model.max_calls==240
            assert config.resolved_model.max_tokens is None
            raw=json.loads((root/f'config-{mode}.json').read_text())
            assert 'max_tokens' in raw['model'] and raw['model']['max_tokens'] is None
            assert config.resolved_model.max_discovery_steps==20
            assert config.resolved_model.max_tool_calls==48
            assert config.resolved_model.timeout_seconds==300
            tables=[t for source in sources for t in source.describe()]
            assert len(tables)==count and not config.rules
            corpus=DocumentCorpus(load_documents(config.documents));corpus.bind_catalog(tables)
            for table in tables:
                docs=corpus.context_for(SourceRef(source_id=table.source_id,schema=table.schema_name,table=table.name))
                assert docs and all(d['doc_id'].startswith('fruit_v2_') for d in docs)
                assert all(Path(p).parent.name=='inputs' for p in config.documents)
        finally:
            for source in sources:source.close()
    manifest=json.loads((root/'manifest.json').read_text())
    assert all(fingerprint(root/name)==digest for name,digest in manifest['files'].items())
    with pytest.raises(ValueError,match='dataset_output_exists'):module.generate(root)


def test_longer_experiment_keeps_provider_output_settings_and_excludes_secrets(tmp_path):
    source=tmp_path/'source.json'
    source.write_text(json.dumps({'sources':[],'model':{'max_output_tokens':16384,
        'max_response_bytes':1048576,'api_key':'fixture-only-not-a-real-secret',
        'api_key_env':'FIXTURE_API_KEY'}}))
    root=tmp_path/'fixture';generator().generate(root,source)
    for mode in ('hierarchy','concepts','full'):
        path=root/f'config-{mode}.json';text=path.read_text();model=load_config(path).resolved_model
        assert model.max_output_tokens==16384 and model.max_response_bytes==1048576
        assert model.api_key is None and model.api_key_env=='FIXTURE_API_KEY'
        assert 'fixture-only-not-a-real-secret' not in text


def test_full_fixture_validator_detects_unregistered_code_and_wrong_parent():
    module=generator()
    tables={'报表项层级':(module.HIERARCHY_HEADER,module.hierarchy_rows()),
            '指标产品渠道':(module.CONCEPT_HEADER,module.concept_rows())}
    gold=module.hierarchy_gold(tables['报表项层级'][1]);concepts=module.concepts_gold()
    tables['指标产品渠道'][1][0][3]='UNREGISTERED'
    with pytest.raises(AssertionError):module.validate_fixture(tables,gold,concepts)
    tables['指标产品渠道']=(module.CONCEPT_HEADER,module.concept_rows())
    tables['报表项层级'][1][0][1]='unrelated_root'
    with pytest.raises(AssertionError):module.validate_fixture(tables,gold,concepts)
