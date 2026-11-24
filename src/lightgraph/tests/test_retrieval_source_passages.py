"""Evidence-only text search across permitted node observations."""
from contextlib import closing
import sqlite3

from lightgraph.retrieval import SubgraphRetriever, sync_graph_to_retrieval
from lightgraph.retrieval.query import RetrieveSubgraphRequest
from lightgraph.tests.test_retrieval_fields import FixtureEmbedder


def support(key, value, *, scope='public', project='M', **kwargs):
    return {'scope_ref': scope, 'observation_id': key, 'label': '检修单',
            'business_context': {'version': 1, 'status': 'scoped', 'dimensions': {'project': project}},
            'properties': {'现场记录': [value]},
            'provenance': {'source': {'source_id':'plant','schema':'main','table':key},
                           'evidence_refs':['e_' + key]}, **kwargs}


def build(tmp_path, supports, embedder=None):
    graph = {'support_visibility_version': 1, 'definitions': {'entities': [], 'relations': []},
             'nodes': [{'id':'job','type':'记录','identity':{'code':'A1'},'supports':supports}], 'edges':[]}
    sync_graph_to_retrieval(graph, tmp_path, embedder=embedder)
    return SubgraphRetriever(tmp_path, embedder=embedder)


def test_query_can_return_source_property_evidence_without_entity_match(tmp_path):
    indexed_texts = []

    class RecordingEmbedder(FixtureEmbedder):
        def embed(self, texts):
            indexed_texts.extend(texts)
            return super().embed(texts)

    r = build(tmp_path, [support('report', {'observed': ['外壳出现晶罅']}),
                         support('other', '晶罅伴随形变', project='K'),
                         support('restricted', '秘密的晶罅位置', scope='private'),
                         support('unrelated', '正常')], RecordingEmbedder())
    assert all('晶罅' not in text for text in indexed_texts)
    result = RetrieveSubgraphRequest.model_validate({'query':'M 项目中，晶罅'}).run(r,['public'])
    assert not result['nodes'] and not result['edges']
    assert result['available_context']['status'] == 'source_text_only'
    assert result['available_context']['passages'] == [
        {'text':'晶罅','node_id':'job','display_name':'检修单','excerpt':'外壳出现晶罅','field':'property',
         'property':'现场记录','observation_id':'report','evidence_refs':['e_report'],'source_tables':['plant.main.report']}]
    cohorts = r.authorized_cohorts(['public'], {'project':'M'})
    assert '外壳出现晶罅' not in r.index.authorized_terms(cohorts)
    assert not r.index.exact_names(['外壳出现晶罅'], scopes=cohorts, limit_per_term=4)['外壳出现晶罅']
    assert not r.index.search_lexical('晶罅', scopes=cohorts)


def test_raw_property_name_and_excerpt_survive_normalization(tmp_path):
    raw = 'early' + ' ' * 500 + 'ＤＥＬＴＡ日记'
    obs = support('entry', 'normalized', property_observations=[
        {'property':'现场记录','value':'normalized','original_property':'原始记录','raw_value':raw}])
    r = build(tmp_path, [obs])
    scopes = r.authorized_cohorts(['public'], None)
    p, = r.index.source_passages(['delta'], scopes=scopes, limit=1)
    assert p['property'] == '原始记录' and p['excerpt'] in raw and 'ＤＥＬＴＡ' in p['excerpt']
    assert not r.index.source_passages(['normalized'], scopes=scopes, limit=1)


def test_description_remains_readable_in_pre_property_index(tmp_path):
    r = build(tmp_path, [support('manual', '仅属性中的词', description='手册上提到误报情况')])
    # Reproduce an older frozen build: its postings only included descriptions, names and codes.
    with closing(sqlite3.connect(r.index.path)) as conn:
        conn.execute("DELETE FROM kg_node_terms WHERE role = 'source_property'")
        conn.commit()
    old = SubgraphRetriever(tmp_path)
    scopes = old.authorized_cohorts(['public'], None)
    p, = old.index.source_passages(['误报'], scopes=scopes, limit=1)
    assert p['field'] == 'description' and p['evidence_refs'] == ['e_manual']
    assert not old.index.source_passages(['仅属性'], scopes=scopes, limit=1)
