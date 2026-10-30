"""Offline, declared-rule example; this does not measure model discovery quality.

Run from the repository: .venv/bin/python examples/concept_mapping_demo.py
"""
from pathlib import Path
import argparse
import json

from lightgraph.contracts import ColumnSpec, RuleSpec, TableSpec
from lightgraph.documents import load_document
from lightgraph.pipeline import run_pipeline
from lightgraph.runtime.config import RunConfig
from lightgraph.sources import MemorySource


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='runs/concept-mapping-demo')
    args=parser.parse_args()
    doc=load_document('concept_mapping.md',text='''---
purpose: data_description
---
# 独立概念与联合上下文
本示例中 metric、product、channel 分别表示指标定义、产品类别、销售渠道。
每列的相同值在本示例定义内表示同一个概念。不同定义不按同名合并。
id 只标识一条完整的联合条件，不属于任何独立概念的身份。
''')
    version=doc.document_id+'@'+doc.revision
    # The document explicitly defines the identity of each role. In automated
    # discovery, each reference must be issued and independently reviewed.
    components=[dict(role=role,entity_kind=kind,column=role,
        definition_ref=version+':'+doc.sections[0].section_id,relation='binds_'+role)
        for role,kind in [('metric','Metric'),('product','Product'),('channel','Channel')]]
    rule=RuleSpec(rule_id='declared_concepts',kind='concept_mapping',
        source={'source_id':'demo','table':'items'},
        identity={'key_columns':['id'],'entity_kind':'ConceptBinding'},
        concept_mapping={'evidence_ref':version,'components':components},
        dependencies={'documents':[version]},
        semantic_schema={
            'entity_types':[{'name':kind,'description':description} for kind,description in [
                ('ConceptBinding','一条来源记录中同时成立的概念组合'),
                ('Product','产品类别'),('Channel','销售渠道')]],
            'relations':[{'name':c['relation'],'description':'联合条件中的 '+c['role'],
                'source_kinds':['ConceptBinding'],'target_kinds':[c['entity_kind']]} for c in components]})
    table=TableSpec(source_id='demo',name='items',columns=[ColumnSpec(name=name)
        for name in ['id','metric','product','channel']])
    rows=[('1','应收账款占用','柑橘','批发渠道'),('2','应收账款占用','苹果','零售渠道')]
    with MemorySource([table],{'public.items':rows}) as source:
        report=run_pipeline(RunConfig(sources=[],output_dir=args.output,max_identity_reviews=0),
                            [source],rules=[rule],documents=[doc])
    print(json.dumps({'status':report.status,'business_nodes':report.counts.get('business_nodes'),
        'business_edges':report.counts.get('business_edges'),'rules_published':report.rules_published,
        'report':str((Path(args.output)/'report.json').resolve()),'model_calls':0},ensure_ascii=False))
    if not report.rules_published:
        raise SystemExit(1)


if __name__=='__main__':
    main()
