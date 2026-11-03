"""Frozen manifests use portable paths without relaxing integrity checks."""
import sqlite3

import pytest

from lightgraph.errors import LightGraphError
from lightgraph.evaluation import frozen
from lightgraph.tests.test_evaluation import inputs
from lightgraph.tests.test_frozen_resume import parent_run


def windows_keys(bundle, manifest):
    converted={**manifest,'files':{name.replace('/','\\'):value
        for name,value in manifest['files'].items()}}
    frozen.write(bundle/'bundle.json',converted)
    return converted


def test_new_manifest_is_portable_and_old_windows_keys_still_execute(tmp_path):
    _,gold,config=inputs(tmp_path);bundle=tmp_path/'bundle'
    manifest=frozen.prepare(config,bundle,gold_path=gold)
    assert all('\\' not in key for key in manifest['files'])
    assert all('\\' not in key for key in manifest['configured_input_files'])
    assert any(key.startswith('code/lightgraph/') for key in manifest['files'])
    converted=windows_keys(bundle,manifest)
    assert frozen.verify(bundle)==converted
    assert frozen.launch(bundle) in (0,2)
    assert frozen.load(bundle/'execution.json')['frozen_files_unchanged']


@pytest.mark.parametrize('change,code',[
    ('changed_file','evaluation_frozen_file_changed'),
    ('added_module','evaluation_frozen_code_changed'),
    ('duplicate_key','evaluation_bundle_invalid'),
    ('escape','evaluation_frozen_file_changed'),
])
def test_windows_keys_keep_hash_membership_and_boundary_checks(tmp_path,change,code):
    _,gold,config=inputs(tmp_path);bundle=tmp_path/'bundle'
    manifest=windows_keys(bundle,frozen.prepare(config,bundle,gold_path=gold))
    if change=='changed_file':
        (bundle/'code/lightgraph/__init__.py').write_text('changed',encoding='utf-8')
    elif change=='added_module':
        (bundle/'code/lightgraph/extra.py').write_text('pass',encoding='utf-8')
    else:
        if change=='duplicate_key':
            key=next(key for key in manifest['files'] if '\\' in key)
            manifest['files'][key.replace('\\','/')]=manifest['files'][key]
        else:
            outside=tmp_path/'outside.txt';outside.write_text('outside',encoding='utf-8')
            manifest['files']['..\\outside.txt']=frozen.fingerprint(outside)
        frozen.write(bundle/'bundle.json',manifest)
    with pytest.raises(LightGraphError,match=code):frozen.verify(bundle)
