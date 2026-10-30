"""Validate the fixture with explicit rules, never as an automatic-discovery score.

Gold stays in the scoring step. The execution rules below are a deliberately
manual positive control for the two implemented operators and their scope rules.
"""
import argparse
import json
from pathlib import Path
from lightgraph.contracts import RuleSpec, PartitionCompletion
from lightgraph.documents import load_documents
from lightgraph.evaluation import evaluate
from lightgraph.evaluation.concepts import evaluate_concepts
from lightgraph.runtime.config import load_config,open_sources
from lightgraph.rules import RuleEngine
from lightgraph.store import GraphStore


def validate(dataset,output):
    dataset=Path(dataset).resolve();output=Path(output).resolve()
    if output.exists():raise ValueError('validation_output_exists')
    output.mkdir(parents=True)
    summaries={}
    controls={}
    for mode in ('hierarchy','concepts'):
        config=load_config(dataset/f'config-{mode}.json')
        docs=load_documents(config.documents);versions={d.document_id+'@'+d.revision for d in docs}
        if mode=='hierarchy':
            spec=RuleSpec(rule_id='manual_hierarchy_control',kind='wide_hierarchy',
                source={'source_id':'fruit','table':'报表项层级'},scope={'namespaces':['经营主体']},
                identity={'key_columns':['经营主体','FRUIT_ITEM_L1_CODE'],'entity_kind':'ReportItem',
                    'semantic_resolution':'verified_identity_domain','level_policy':'relative_path',
                    'identity_domain':'manual_fixture_items','identity_evidence':['manual:fixture_definition'],
                    'attribute_evidence':['manual:fixture_definition'],'identity_scope':'namespace_local'},
                hierarchy={'levels':[{'level':i,'code':f'FRUIT_ITEM_L{i}_CODE','label':f'FRUIT_ITEM_L{i}_CN_NAME',
                    'aliases':[f'FRUIT_ITEM_L{i}_EN_NAME']} for i in range(1,6)],
                    'taxonomy':'manual_fixture_hierarchy','order_evidence':['manual:fixture_definition'],
                    'position_semantics':'ordinal_slots','position_evidence':['manual:fixture_definition'],
                    'allow_dag':True,'fill_semantics_confirmed':True},dependencies={'documents':sorted(versions)})
        else:
            # These names are evaluator bindings, never furnished to a live Agent.
            ref=next(iter(versions))+':manual_scope'
            role_kinds={'metric':'Metric','product':'Product','channel':'Channel'}
            relations={r:'binds_'+r for r in role_kinds}
            components=[{'role':role,'entity_kind':role_kinds[role],'column':prefix+'名称','key_column':prefix+'编码',
                'definition_ref':ref,'relation':relations[role],'namespace_columns':[],'scope_evidence_ref':ref}
                for role,prefix in [('metric','指标'),('product','产品'),('channel','渠道')]]
            spec=RuleSpec(rule_id='manual_concept_control',kind='concept_mapping',
                source={'source_id':'fruit','table':'指标产品渠道'},scope={'namespaces':['经营主体']},
                identity={'key_columns':['经营主体','指标编码','产品编码','渠道编码'],'entity_kind':'ConceptBinding'},
                concept_mapping={'evidence_ref':next(iter(versions)),'components':components},
                dependencies={'documents':sorted(versions)},semantic_schema={
                    'entity_types':[{'name':k,'description':k} for k in ['ConceptBinding','Product','Channel']],
                    'relations':[{'name':relations[r],'description':'Joint context '+r,'source_kinds':['ConceptBinding'],
                        'target_kinds':[k]} for r,k in role_kinds.items()]})
        folder=output/mode;folder.mkdir();sources=open_sources(config)
        controls[mode]=spec.model_copy(deep=True)
        try:
            with GraphStore(folder/'graph.sqlite') as store:
                outcome=RuleEngine(store,isolation_domain=config.isolation_domain).execute(sources[0],spec,document_versions=versions)
                if not outcome.published:raise AssertionError(outcome.codes)
                graph=store.view(set(config.scopes),config.isolation_domain)
                (folder/'graph.json').write_text(json.dumps(graph,ensure_ascii=False))
        finally:
            for source in sources:source.close()
        if mode=='hierarchy':
            result=evaluate(folder/'graph.json',dataset/'gold/hierarchy.json',store=folder/'graph.sqlite').model_dump(mode='json')
            assert all(result['accepted'][k]['exact'] for k in ('identities','labels','english_names','positions','edges','subject_verified_identities'))
        else:
            result=evaluate_concepts(folder/'graph.json',dataset/'gold/concepts.json',role_kinds=role_kinds,
                role_relations=relations,context_kind='ConceptBinding')
            assert all(result['accepted'][k]['exact'] for k in ('concepts','labels','joint_contexts'))
        (folder/'evaluation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        summaries[mode]={'nodes':len(graph['nodes']),'edges':len(graph['edges']),'all_declared_metrics_exact':True}
    # Exercise all five physical tables in one atomic generation. Domains below
    # are explicit evaluator choices justified by the input documentation, never
    # provided to the discovery Agent or inferred from the Gold answer set.
    config=load_config(dataset/'config-full.json')
    docs=load_documents(config.documents);versions={d.document_id+'@'+d.revision for d in docs}
    concept=controls['concepts']
    rules=[controls['hierarchy'],concept]
    domains={role:'manual_fruit_'+role for role in role_kinds}
    concept_doc=next(d for d in docs if d.document_id=='fruit_v2_contexts')
    concept_ref=concept_doc.document_id+'@'+concept_doc.revision+':'+concept_doc.sections[1].section_id
    concept.concept_mapping.components=[c.model_copy(update={'identity_domain':domains[c.role],
        'identity_evidence':[concept_ref]}) for c in concept.concept_mapping.components]
    for role,prefix,attribute in [('metric','指标','口径说明'),('product','产品','产品分类'),('channel','渠道','渠道说明')]:
        document=next(d for d in docs if d.document_id=='fruit_v2_'+role)
        version=document.document_id+'@'+document.revision
        ref=version+':'+document.sections[0].section_id
        rules.append(RuleSpec(rule_id='manual_definition_'+role,kind='column_group_entity',
            source={'source_id':'fruit','table':prefix+'定义'},
            identity={'key_columns':[prefix+'编码'],'entity_kind':role_kinds[role],
                'semantic_resolution':'verified_identity_domain','identity_domain':domains[role],
                'identity_evidence':[ref],'attribute_evidence':[ref]},
            attributes={'single_valued':{'label':prefix+'名称','description':attribute}},
            dependencies={'documents':[version]},semantic_schema=concept.semantic_schema))
    folder=output/'full';folder.mkdir();sources=open_sources(config)
    try:
        with GraphStore(folder/'graph.sqlite') as store:
            engine=RuleEngine(store,isolation_domain=config.isolation_domain)
            outcomes=[engine.execute(sources[0],rule,document_versions=versions,defer_publish=True) for rule in rules]
            assert all(o.prepared for o in outcomes),[o.codes for o in outcomes]
            store.publish_partitions([PartitionCompletion(publication_id=p,complete=True,consistent=True,dependencies_valid=True)
                for outcome in outcomes for p in outcome.publication_ids])
            graph=store.view(set(config.scopes),config.isolation_domain)
            (folder/'graph.json').write_text(json.dumps(graph,ensure_ascii=False))
    finally:
        for source in sources:source.close()
    # The same explicit type binding used by the manual rule selects the
    # hierarchy score. Gold identities/edges stay unchanged; no best-fit search.
    hgold=json.loads((dataset/'gold/hierarchy.json').read_text());hgold['entity_kinds']=['ReportItem']
    (folder/'hierarchy-gold-binding.json').write_text(json.dumps(hgold,ensure_ascii=False))
    hierarchy=evaluate(folder/'graph.json',folder/'hierarchy-gold-binding.json',store=folder/'graph.sqlite').model_dump(mode='json')
    concepts=evaluate_concepts(folder/'graph.json',dataset/'gold/concepts.json',role_kinds=role_kinds,
        role_relations=relations,context_kind='ConceptBinding')
    assert all(hierarchy['accepted'][k]['exact'] for k in ('identities','labels','english_names','positions','edges','subject_verified_identities'))
    assert all(concepts['accepted'][k]['exact'] for k in ('concepts','labels','joint_contexts'))
    # Each concept has both its definition-table and occurrence-table support.
    support_rules={}
    for support in graph['supports']:support_rules.setdefault(support['fact_id'],set()).add(support['rule_id'])
    shared=[f for f in graph['facts'] if f['fact_kind']=='NodeExistence' and f['payload']['kind'] in role_kinds.values()]
    assert shared and all('manual_concept_control' in support_rules[f['id']] and
        any(r.startswith('manual_definition_') for r in support_rules[f['id']]) for f in shared)
    (folder/'evaluation.json').write_text(json.dumps({'hierarchy':hierarchy,'concepts':concepts},ensure_ascii=False,indent=2)+'\n')
    summaries['full']={'nodes':len(graph['nodes']),'edges':len(graph['edges']),
        'cross_table_shared_concepts':len(shared),'all_declared_metrics_exact':True}
    result={'mode':'manual_rule_control','model_calls':0,'results':summaries,
        'acceptance_boundary':'Explicit manual rules validated; automatic discovery and customer semantics remain untested.'}
    (output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('dataset');parser.add_argument('--output',required=True)
    args=parser.parse_args();print(json.dumps(validate(args.dataset,args.output),ensure_ascii=False,indent=2))
