"""Redundant source names need complete equality checks, not a name heuristic."""
import json
from types import SimpleNamespace

import pytest

from lightgraph.contracts import ColumnSpec, ExtractionGoal, RuleSpec, TableSpec
from lightgraph.discovery.coverage import discover_uncovered, unexplained_columns
from lightgraph.discovery.shared_labels import enrich_shared_labels
from lightgraph.rules import RuleEngine
from lightgraph.runtime.iteration import IterationLedger
from lightgraph.sources import MemorySource
from lightgraph.sources.budget import QueryBudget
from lightgraph.store import GraphStore


def fixture():
    tables = [TableSpec(source_id='s', name=name, columns=[ColumnSpec(name=c)
              for c in ('code', 'name')]) for name in ('occurrences', 'definitions')]
    rules = [RuleSpec(rule_id=name, kind='column_group_entity',
        source={'source_id':'s', 'table':name}, identity={
            'key_columns':['code'], 'entity_kind':'Metric',
            'semantic_resolution':'verified_identity_domain', 'identity_domain':'metric_codes',
            'identity_scope':'domain_global', 'identity_evidence':['decl@1:codes'],
            'global_scope_evidence':['decl@1:codes'], 'attribute_evidence':['decl@1:names']},
        attributes={'single_valued':{'label':'name'} if name=='definitions' else {}},
        dependencies={'documents':['decl@1']}) for name in ('occurrences', 'definitions')]
    rows = {'public.occurrences':[('A','Alpha'),('A','Alpha'),('B','Beta')],
            'public.definitions':[('A','Alpha'),('B','Beta')]}
    return tables, rules, rows


def run_helper(source, tables, rules, execute, goal=None):
    return enrich_shared_labels(table=tables[0], tables=tables, adapter=source,
        goal=goal or ExtractionGoal(), rules=list(rules), validated_rules=rules,
        remaining=['name'], execute=execute)


def test_exact_shared_names_add_support_without_new_nodes():
    tables, rules, rows = fixture()
    with MemorySource(tables, rows) as source, GraphStore() as store:
        engine = RuleEngine(store)
        for rule in rules:
            outcome = engine.execute(source, rule, document_versions={'decl@1'})
            assert outcome.published, outcome.codes
        before = store.view({'public'})
        def execute(rule, preserve_previous=False):
            assert preserve_previous
            outcome = engine.execute(source, rule, document_versions={'decl@1'})
            assert outcome.published, outcome.codes
            return outcome.published
        issues, stats, interrupted = run_helper(source, tables, rules, execute)
        assert not issues and not interrupted
        assert stats['coverage_shared_label_columns'] == 1
        assert rules[-1].rule_id == 'occurrences' and rules[-1].revision == 2
        assert unexplained_columns(tables[0],rules,[],ExtractionGoal(),engine) == []
        after = store.view({'public'})
        assert {n['id'] for n in before['nodes']} == {n['id'] for n in after['nodes']}
        labels = [f for f in after['facts'] if f['fact_kind']=='PropertyValue'
                  and f['payload']['name']=='label']
        supports = {s['id']:s for s in after['supports']}
        assert len(labels) == 2
        assert all({supports[s]['rule_id'] for s in fact['support_ids']} ==
                   {'occurrences','definitions'} for fact in labels)


@pytest.mark.parametrize('bad_rows', [
    [('A','Alpha'),('B','different')],  # conflict only outside a first-row sample
    [('A','Alpha'),('A','different')],
    [('A','Alpha'),('C','Gamma')],
    [('A','Alpha'),('B',None)],
    [('A','Alpha'),(None,'Beta')],
    [],
])
def test_incomplete_or_conflicting_names_do_not_count_as_covered(bad_rows):
    tables, rules, rows = fixture(); rows['public.occurrences'] = bad_rows
    with MemorySource(tables, rows) as source:
        issues, stats, interrupted = run_helper(source,tables,rules,
            lambda *a,**kw:pytest.fail('Unverified mapping must not execute'))
    assert not stats and not interrupted
    assert issues == [{'table':'public.occurrences','code':'coverage_shared_label_unresolved',
                       'column_names':'["name"]','status':'unresolved'}]
    assert rules[0].attributes.single_valued == {}


@pytest.mark.parametrize('mutation', ['domain','kind','evidence','scope','normalization','excluded','evidence_only'])
def test_matching_text_cannot_grant_identity_or_projection_authority(mutation):
    tables, rules, rows = fixture(); goal=ExtractionGoal()
    if mutation=='domain': rules[1].identity.identity_domain='other_codes'
    elif mutation=='kind': rules[1].identity.entity_kind='Product'
    elif mutation=='evidence': rules[0].identity.attribute_evidence=[]
    elif mutation=='scope': rules[0].identity.identity_scope='namespace_local'
    elif mutation=='normalization': rules[1]=rules[1].model_copy(update={'normalization':{'version':'other'}})
    elif mutation=='excluded': goal.excluded_columns=['occurrences.name']
    else: goal.evidence_only_columns=['occurrences.name']
    with MemorySource(tables, rows) as source:
        assert run_helper(source,tables,rules,lambda *a,**kw:pytest.fail('No authority'),goal) == ([],{},False)
        assert source.stats.queries == 0


def test_shared_name_queries_obey_run_budget_and_leave_no_false_coverage():
    tables, rules, rows = fixture()
    with MemorySource(tables, rows, query_budget=QueryBudget(max_queries=1)) as source:
        issues, stats, interrupted = run_helper(source,tables,rules,
            lambda *a,**kw:pytest.fail('Incomplete checks must not execute'))
    assert interrupted and not stats
    assert issues == [{'table':'public.occurrences','code':'query_budget_exhausted','status':'deferred'}]
    assert rules[0].attributes.single_valued == {}


def test_failed_execution_does_not_replace_old_mapping():
    tables, rules, rows = fixture()
    with MemorySource(tables, rows) as source:
        issues, stats, interrupted = run_helper(source,tables,rules,lambda *a,**kw:False)
    assert interrupted and not stats
    assert issues[0]['code']=='coverage_shared_label_execution_failed'
    assert rules[0].attributes.single_valued == {} and rules[0].revision==1


def test_finished_coverage_gap_is_not_reexplored_without_changed_context():
    tables,rules,rows=fixture();rules[1].identity.identity_domain='different'
    goal=ExtractionGoal(); calls=[]
    def explore(*a,**kw):
        from lightgraph.discovery.models import DiscoveryResult
        calls.append(1)
        return DiscoveryResult()
    with MemorySource(tables,rows) as source, GraphStore() as store:
        ledger=IterationLedger(store,goal,['public'])
        args=dict(tables=tables,contexts={'s':{'public.occurrences':'v1'}},adapters={'s':source},
            goal=goal,engine=RuleEngine(store),rules=rules,understandings=[],ledger=ledger,
            corpus=None,model=SimpleNamespace(explore=lambda:None),max_rounds=2,
            execute=lambda *a,**kw:pytest.fail('No rule'),validated_rules=rules,explore=explore)
        for _ in range(2):
            issues,stats,unfinished=discover_uncovered(**args)
            assert issues[-1]['status']=='unresolved'
            assert json.loads(issues[-1]['column_names'])==['name']
        assert len(calls)==1
        tables[0].columns[1].comment='Updated source definition requires another interpretation'
        discover_uncovered(**args)
        assert len(calls)==2  # schema evidence changed even though document version did not
        discover_uncovered(**args,rediscover=True)
        assert len(calls)==3


def test_verified_redundant_name_closes_prior_coverage_work_before_agent():
    tables,rules,rows=fixture();goal=ExtractionGoal()
    with MemorySource(tables,rows) as source, GraphStore() as store:
        ledger=IterationLedger(store,goal,['public']);engine=RuleEngine(store)
        item=ledger.enqueue(rules[0].source,task='coverage_discovery',rules=[rules[0]],context_version='old')
        ledger.transition(item,'deferred')
        def execute(rule,preserve_previous=False):
            assert preserve_previous
            outcome=engine.execute(source,rule,document_versions={'decl@1'})
            assert outcome.published,outcome.codes
            return outcome.published
        for rule in rules:
            assert engine.execute(source,rule,document_versions={'decl@1'}).published
        issues,stats,unfinished=discover_uncovered(tables=tables,
            contexts={'s':{'public.occurrences':'v1'}},adapters={'s':source},goal=goal,
            engine=engine,rules=list(rules),understandings=[],ledger=ledger,corpus=None,
            model=SimpleNamespace(explore=lambda:None),max_rounds=2,execute=execute,
            validated_rules=rules,explore=lambda *a,**kw:pytest.fail('Coverage is already verified'))
        assert not issues and not unfinished
        assert stats['coverage_shared_label_columns']==1 and stats['coverage_discovery_tasks']==0
        assert ledger.items()[0].state=='resolved'


def test_name_verification_respects_each_rule_predicate():
    tables,rules,rows=fixture()
    rules[0].scope.predicate={'code':'A'}
    rows['public.occurrences'].append(('B','different'))
    executed=[]
    with MemorySource(tables,rows) as source:
        issues,stats,interrupted=run_helper(source,tables,rules,
            lambda rule,**kw:executed.append(rule) or True)
    assert not issues and not interrupted and len(executed)==1
    assert executed[0].scope.predicate=={'code':'A'}


def test_unexplained_column_names_do_not_expose_sensitive_or_excluded_metadata():
    table=TableSpec(source_id='s',name='t',columns=[ColumnSpec(name=n)
        for n in ['name','password','not_in_goal','context_only']])
    goal=ExtractionGoal(excluded_columns=['not_in_goal'],evidence_only_columns=['context_only'])
    assert unexplained_columns(table,[],[],goal,RuleEngine(None)) == ['name']




@pytest.mark.parametrize('max_rounds,expected_state',[(1,'deferred'),(2,'unresolved')])
def test_coverage_turn_limit_distinguishes_finished_progress_from_unstarted_work(max_rounds,expected_state):
    from lightgraph.discovery.models import DiscoveryResult
    table=TableSpec(source_id='s',name='items',columns=[ColumnSpec(name=c)
        for c in ['code','name','unclassified']])
    original=RuleSpec(rule_id='metric',kind='column_group_entity',source={'source_id':'s','table':'items'},
        identity={'key_columns':['code'],'entity_kind':'Metric'})
    candidate=original.model_copy(deep=True)
    candidate.attributes.single_valued['label']='name'
    allrules=[original];validated=[original];goal=ExtractionGoal();calls=[]
    with MemorySource([table],{'public.items':[('A','Alpha','unmapped')]}) as source, GraphStore() as store:
        engine=RuleEngine(store);ledger=IterationLedger(store,goal,['public'])
        assert engine.execute(source,original).published
        def execute(rule,**kw):
            outcome=engine.execute(source,rule)
            assert outcome.published,outcome.codes
            validated[:]=[rule]
            return True
        def explore(*a,**kw):
            calls.append(1)
            return DiscoveryResult(rules=[candidate],stats={'unresolved_proposals':2,'rejected_proposals':3})
        issues,stats,_=discover_uncovered(tables=[table],contexts={'s':{'public.items':'v1'}},
            adapters={'s':source},goal=goal,engine=engine,rules=allrules,understandings=[],ledger=ledger,
            corpus=None,model=SimpleNamespace(explore=lambda:None),max_rounds=max_rounds,
            execute=execute,validated_rules=validated,explore=explore)
        assert len(calls)==max_rounds-1
        assert issues[-1]['status']==ledger.items()[0].state==expected_state
        assert json.loads(issues[-1]['column_names'])==(['name','unclassified'] if max_rounds==1 else ['unclassified'])
        if calls:
            assert stats['unresolved_proposals']==2 and stats['rejected_proposals']==3
            assert allrules[0].revision==2 and allrules[0].attributes.single_valued['label']=='name'
