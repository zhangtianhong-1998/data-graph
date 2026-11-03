"""Frozen continuation keeps old output immutable and revalidates copied state."""
from contextlib import closing
import json
import sqlite3

import pytest

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError
from lightgraph.evaluation.core import fingerprint
from lightgraph.evaluation.frozen import prepare, launch, load, verify, write
from lightgraph.runtime.config import load_config
from lightgraph.tests.test_evaluation import inputs


def parent_run(tmp_path):
    source, gold, config = inputs(tmp_path)
    rules=tmp_path/'rules.json'
    write(rules,[{'rule_id':'items','kind':'column_group_entity',
        'source':{'source_id':'s','table':'source'},
        'identity':{'key_columns':['code'],'entity_kind':'Metric'},
        'attributes':{'single_valued':{'label':'name'}}}])
    body=load(config);body['rules']=[str(rules)];write(config,body)
    parent = tmp_path/'parent'
    prepare(config,parent,gold_path=gold)
    assert launch(parent) in (0,2)
    return source,gold,config,parent
