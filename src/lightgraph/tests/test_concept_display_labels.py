"""Context captions use accepted assignments, not parsing of generated names."""
from copy import deepcopy

import pytest

from lightgraph.store import GraphStore
from lightgraph.store.display import display_rules, project_display_labels
from lightgraph.store.paging import graph_page
from lightgraph.tests.test_concept_mapping import coded_row, coded_rule, execute


def contexts(graph):
    return [node for node in graph['nodes'] if node['kind']=='ConceptBinding']


def test_context_caption_is_readable_without_changing_identity_or_name_facts():
    with GraphStore() as store:
        rows=[coded_row(metric='占用 | 特殊名称'),coded_row(id='2',metric='占用 | 特殊名称'),
              coded_row(subject='B',metric='占用 | 特殊名称')]
        assert execute(store,rows,coded_rule()).published
        graph=store.view({'A','B'})
        nodes=contexts(graph)
        assert len(nodes)==3 and len({node['id'] for node in nodes})==3
        assert {node['display_label'] for node in nodes}=={'联合适用：占用 | 特殊名称 · 梨 · 批发'}
        assert len({node['properties']['business_code'] for node in nodes})==3
        assert len({node['label'] for node in nodes})==3
        assert all('metric=' in node['label'] and '〔' in node['label'] for node in nodes)
        assert all(node['display_label_basis']['method']=='accepted_concept_components' for node in nodes)
        assert all('display_label' not in node for node in graph['nodes'] if node['kind']!='ConceptBinding')
        original=deepcopy(graph['facts'])
        # Projecting a second time is stable and never writes a new label fact.
        assert project_display_labels(graph,display_rules(store,graph))==3
        assert graph['facts']==original
        assert store.view({'A','B'})['nodes']==graph['nodes']
        assert not contexts(store.view({'public'}))


@pytest.mark.parametrize('damage',['candidate_edge','missing_edge','wrong_publication','wrong_binding',
                                   'private_label','conflicting_label','wrong_rule_kind'])
def test_incomplete_or_unrelated_component_evidence_does_not_generate_caption(damage):
    with GraphStore() as store:
        assert execute(store,[coded_row()],coded_rule()).published
        graph=store.view({'A'});rules=display_rules(store,graph)
    for node in graph['nodes']:
        node.pop('display_label',None);node.pop('display_label_basis',None)
    edge=next(fact for fact in graph['facts'] if fact['fact_kind']=='Edge'
              and fact['payload']['relation']=='binds_product')
    edge_support=next(s for s in graph['supports'] if s['fact_id']==edge['id'])
    if damage=='candidate_edge':edge['status']='candidate'
    elif damage=='missing_edge':graph['facts'].remove(edge)
    elif damage=='wrong_publication':edge_support['publication_id']='unrelated_publication'
    elif damage=='wrong_binding':edge_support['source_binding']='other_context'
    elif damage=='wrong_rule_kind':
        next(iter(rules.values()))['kind']='column_group_entity'
    else:
        label=next(fact for fact in graph['facts'] if fact['fact_kind']=='PropertyValue'
                   and fact['payload'].get('entity_id')==edge['payload']['target']
                   and fact['payload']['name']=='label')
        support=next(s for s in graph['supports'] if s['fact_id']==label['id'])
        if damage=='private_label':support['scope_ref']='private'
        else:
            conflicting=deepcopy(label);conflicting['id']='conflicting_name'
            conflicting['payload']['value']='另一名称';graph['facts'].append(conflicting)
            graph['supports'].append({**support,'id':'other_support','fact_id':conflicting['id']})
    project_display_labels(graph,rules)
    assert all('display_label' not in node for node in contexts(graph))
    assert all('metric=' in node['label'] for node in contexts(graph))


def test_paged_concept_caption_uses_only_authorized_off_page_components(monkeypatch):
    with GraphStore() as store:
        assert execute(store,[coded_row(),coded_row(subject='B')],coded_rule()).published
        expected={node['id']:node['display_label'] for node in contexts(store.view({'A'}))}
        monkeypatch.setattr(store,'view',lambda *a,**k: (_ for _ in ()).throw(AssertionError('full view')))
        page=graph_page(store,{'A'},'default',kind='ConceptBinding',limit=1)
        assert len(page['nodes'])==1 and not page['edges']
        node=page['nodes'][0]
        assert node['display_label']==expected[node['id']]
        assert all(fact['payload'].get('entity_id')==node['id'] for fact in page['facts'])
        assert all(support['scope_ref']=='A' for support in page['supports'])
        hidden=graph_page(store,{'public'},'default',kind='ConceptBinding',limit=1)
        assert not hidden['nodes']
        assert '占用' not in str(hidden)
