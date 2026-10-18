"""Repeatable small synthetic acceptance. Gold checks never enter discovery input."""
from pathlib import Path
from .sample import build_sample,sample_rules,DESCRIPTION,ANALYSIS
from .sources import MemorySource
from .documents import load_document
from .runtime.config import RunConfig
from .runtime.retention import atomic_text,retain_check
from .pipeline import run_pipeline
from .store import GraphStore
import json


def verify(output,rows=5000):
    output=Path(output)
    if output.exists() and any(output.iterdir()):
        from .errors import LightGraphError
        raise LightGraphError('acceptance_directory_not_empty')
    output.mkdir(parents=True,exist_ok=True)
    documents=[load_document('data_description.md',text=DESCRIPTION),load_document('analysis.md',text=ANALYSIS)]
    report={'synthetic':True,'source_rows':rows,'E':{},'D':{},'I':{'status':'NOT_RUN','reason':'real_gauss_and_real_model_not_configured'}}
    for gate in ['E','D']:
        tables,data=build_sample(rows)
        cfg=RunConfig(sources=[],output_dir=str(output/gate),scopes=['A','B','public'],document_scopes=['A','B'])
        with MemorySource(tables,data) as source:
            result=run_pipeline(cfg,[source],rules=sample_rules() if gate=='E' else None,documents=documents)
        with GraphStore(output/gate/'graph.sqlite') as store:
            graph=store.view({'A','B','public'},include_candidates=True)
        nodes=[n for n in graph['nodes'] if n['layer']=='business' and n['status']=='accepted']
        names={n['label']:n['id'] for n in nodes}
        pairs={(e['source'],e['target']) for e in graph['edges'] if e['relation']=='parent_of'}
        checks={
            'seven_rules_published':len(result.rules_published)==7,
            'nineteen_business_entities':len(nodes)==19,
            'shared_sales_expense':sum(n['label']=='销售费用' for n in nodes)==1,
            'no_self_loop':all(a!=b for a,b in pairs),
            'missing_level_not_bridged':(names.get('经营活动付现利润'),names.get('减值损失')) not in pairs,
            'same_level_siblings':all((names.get('退款负债'),names.get(x)) in pairs for x in ('收入拨备1','其中：退款负债')),
            'metric_measure_relation':sum(e['relation']=='uses_measure' for e in graph['edges'])==1,
            'analytical_claims_candidate':bool(graph['claims']) and all(c['status']=='candidate' for c in graph['claims']),
            'raw_canary_absent':retain_check([output/gate],['RETENTION_CANARY_9f2d17'])['violations']==0,
        }
        # A repeat uses stored rules and current source queries, with no model.
        with MemorySource(tables,data) as source:
            repeated=run_pipeline(cfg,[source],rules=sample_rules() if gate=='E' else None,documents=documents)
        with GraphStore(output/gate/'graph.sqlite') as store:
            second=store.view({'A','B','public'},include_candidates=True)
        checks['repeat_stable_facts']={f['id'] for f in graph['facts']}=={f['id'] for f in second['facts']}
        checks['repeat_zero_model_calls']=repeated.counts['llm_calls']==0
        report[gate]={'status':'SYNTHETIC_PASS' if all(checks.values()) else 'FAIL',
            'mode':'fixed_rule' if gate=='E' else 'automatic_deterministic_discovery',
            'checks':checks,'counts':result.counts,'issues':result.issues,
            'business_nodes':len(nodes),'relationships':len(graph['edges']),
            'limitations':['does_not_validate_real_model_quality','does_not_validate_customer_gauss']}
    report['status']='PASS_WITH_UNVERIFIED_INTEGRATION' if all(report[g]['status']=='SYNTHETIC_PASS' for g in ['E','D']) else 'FAIL'
    atomic_text(output/'acceptance.json',json.dumps(report,ensure_ascii=False,indent=2))
    return report
