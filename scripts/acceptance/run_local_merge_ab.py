"""Compare two committed merges using identical synthetic extract checkpoints and a local model.

Only the workers' draft choices are scripted. The reviewer uses the actual loopback
OpenAI-compatible endpoint. --reuse-experiment reruns only the merge against the earlier synthetic checkpoints.
"""
import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from openpyxl import Workbook
from pytest import MonkeyPatch

from lightgraph.construction.checkpoints import load_checkpoint
from lightgraph.discovery import sdk_transport
from lightgraph.tests.construction_worker_fixture import regional_proposal, replace_worker_launch
from lightgraph.tests.test_web_api import create, upload
from lightgraph.tests.web_client import RevisionClient
from lightgraph.web.app import create_app


def ordinary_projection(tools, table):
    """Two business views map the same worksheet key to different ordinary neighbors."""
    draft = regional_proposal(tools, table)
    kind = draft['entities'][0]['type']
    peer, key, label, role = (
        ('Product', 'product_code', 'product_name', 'stocks_product') if kind == 'SalesRegion'
        else ('SupportTeam', 'team_code', 'team_name', 'served_by_team'))
    peer_def = draft['entities'][1]
    peer_def.update(type=peer, key_names=[key])
    peer_def['mappings'][0].update(key_columns=[key], label_column=label, applicability={'status': 'global'})
    relation = draft['relations'][0]
    relation.update(type=role, target_type=peer)
    relation['mappings'][0].update(target_key_columns=[key], applicability={'status': 'global'})
    return draft


def independent_catalog_projection(tools, table):
    draft = regional_proposal(tools, table)
    region = draft['entities'][0]
    if region['type'] == 'ReportingArea':
        region['identity_domain'] = 'test.independent_reporting_catalog'
    return draft


def declared_relation_projection(tools, table):
    draft = regional_proposal(tools, table)
    draft['entities'][1]['mappings'][0]['applicability'] = {'status': 'global'}
    relation = draft['relations'][0]
    relation['description'] = 'The reporting country to which this region belongs.'
    relation['mappings'][0]['applicability'] = {'status': 'global'}
    return draft


def example(catalog='common'):
    columns = ['region_code', 'region_name', 'country_code', 'country_name', 'short_name', 'postal_prefix', 'zone',
               'product_code', 'product_name', 'team_code', 'team_name']
    rows = [
        ['D01', 'Central', 'Z', 'Country Z', 'C', 'P10', 'Z1', 'PR1', 'Product 1', 'T2', 'Team 2'],
        ['D02', 'Coast', 'Z', 'Country Z', 'K', 'P20', 'Z2', 'PR3', 'Product 3', 'T4', 'Team 4'],
        ['D03', 'Central', 'Z', 'Country Z', 'C', 'P30', 'Z3', 'PR5', 'Product 5', 'T6', 'Team 6'],
    ]
    book = Workbook()
    book.active.append(columns + ['catalog'])
    for row in rows:
        book.active.append(row + [catalog])
    buf = BytesIO()
    book.save(buf)
    return buf.getvalue(), rows


def summary(graph, rows, scenario, snapshots):
    districts = [n for n in graph['nodes'] if n['identity'].get('code') in {r[0] for r in rows}]
    nodes = {n['id']: n for n in graph['nodes']}
    occurrences = {r[0]: [n['id'] for n in districts if n['identity']['code'] == r[0]] for r in rows}
    expected_edges = {(r[0], role, value) for r in rows for role, value in (
        [('located_in', r[2])] if scenario != 'complementary-roles' else [('stocks_product', r[7]), ('served_by_team', r[9])])}
    three_branches = scenario in {'same-role', 'declared-relation'}
    participants = {'sales', 'reporting', 'service'} if three_branches else {'sales', 'reporting'}
    expected_units = {'located_in': participants, 'stocks_product': {'sales'},
                      'served_by_team': {'reporting'}}
    source_rows = {r[0]: i for i, r in enumerate(rows, start=2)}
    expected_observations = {(*fact, unit, source_rows[fact[0]]) for fact in expected_edges
                             for unit in expected_units[fact[1]]}
    required_columns = {'short_name': 4, 'postal_prefix': 5}
    if three_branches:
        required_columns['zone'] = 6
    expected_attributes = {(r[0], field, r[i]) for r in rows for field, i in required_columns.items()}
    seen_attributes = {(n['identity']['code'], field, value) for n in districts
                       for field, values in n['properties'].items() if field in required_columns for value in values}
    observed = set()
    found = set()
    for e in graph['edges']:
        head, tail = nodes[e['source']], nodes[e['target']]
        if head in districts:
            fact = head['identity']['code'], e['type'].split('__def_')[0], next(iter(tail['identity'].values()))
            found.add(fact)
            for support in e['supports']:
                locator = support.get('source_row_witness') or {}
                snapshot = snapshots.get(support.get('contributor_id'))
                if snapshot and locator.get('snapshot') == snapshot and locator.get('kind') == 'excel_rows_v1':
                    for first, last in locator['rows']:
                        observed.update((*fact, support.get('contributor_id'), n) for n in range(first, last + 1))
    return {
        'total_nodes': len(nodes), 'raw_edges': len(graph['edges']),
        'output_ids_by_district_code': occurrences,
        'distinct_district_nodes': len(districts),
        'namesake_different_codes_stay_separate': bool(occurrences['D01'] and occurrences['D03']
            and set(occurrences['D01']).isdisjoint(occurrences['D03'])),
        'source_observations_matched': len(observed & expected_observations),
        'source_observations_expected': len(expected_observations),
        'unexpected_source_observations': sorted(observed - expected_observations),
        'expected_directed_facts': len(expected_edges), 'matched_directed_facts': len(expected_edges & found),
        'unexpected_directed_facts': sorted(found - expected_edges),
        'missing_directed_facts': sorted(expected_edges - found),
        'missing_attributes': sorted(expected_attributes - seen_attributes),
        'unexpected_attributes': sorted(seen_attributes - expected_attributes),
        'verified_attributes': len(expected_attributes & seen_attributes),
        'regions_with_multiple_contributors': sum(
            len({s['contributor_id'] for s in n['supports'] if s.get('contributor_id')}) > 1 for n in districts),
    }


def experiment(args):
    out = args.output.resolve()
    out.mkdir(exist_ok=False, parents=True)
    original = args.reuse_experiment.resolve() if args.reuse_experiment else out
    prior = json.loads((original / 'results.json').read_text()) if args.reuse_experiment else None
    if prior and (prior.get('synthetic_data') is not True or prior.get('scenario') != args.scenario
                  or prior.get('model') != args.model or prior.get('extract_drafts') != 'scripted'):
        raise ValueError('Select a prior synthetic run with the same model and scenario.')
    app = create_app(original / 'workspace', frontend_dir=out / 'no-dist')
    names = {'sales': 'SalesRegion', 'reporting': 'ReportingArea'}
    if args.scenario in {'same-role', 'declared-relation'}:
        names['service'] = 'ServiceTerritory'
    script_path = Path(__file__).resolve()
    factory = {'same-role': 'regional_proposal', 'complementary-roles': 'ordinary_projection',
               'independent-catalogs': 'independent_catalog_projection',
               'declared-relation': 'declared_relation_projection'}[args.scenario]
    expression = (f"import sys; from lightgraph.tests.construction_worker_fixture import main; "
                  f"sys.path.insert(0,{str(script_path.parent)!r}); "
                  f"from run_local_merge_ab import "
                  f"{factory} as draft; "
                  "sys.exit(main(draft))")
    report = {'model': args.model, 'endpoint_host': urlsplit(args.url).hostname, 'scenario': args.scenario,
              'synthetic_data': True, 'extract_drafts': 'scripted', 'merge_reviewer': 'real_loopback_http'}
    with RevisionClient(app) as client, MonkeyPatch.context() as patch:
        patch.setenv('LOCAL_SYNTHETIC_MODEL_KEY', 'local-only')
        def worker(original, argv, *a, **kw):
            return original([sys.executable, '-c', expression, argv[-1]], *a, **kw)
        replace_worker_launch(patch, worker)
        raw_exchanges = []
        raw_requests = []
        original_request = sdk_transport.request_completion

        def record_response(config, budget, transport, key, payload):
            raw_requests.append(payload)
            (out / 'model-requests.json').write_text(json.dumps(raw_requests, indent=2, ensure_ascii=False))
            response = original_request(config, budget, transport, key, payload)
            raw_exchanges.append(response)
            (out / 'model-responses.json').write_text(json.dumps(raw_exchanges, indent=2, ensure_ascii=False))
            return response
        patch.setattr(sdk_transport, 'request_completion', record_response)
        base = '/api/projects/alpha'
        book, rows = example()
        if prior:
            rid = prior['deterministic']['run_id']
            baseline = app.state.manager.get('alpha', rid)
            original_cp = baseline['workflow_state']['artifacts']['join']
            assert original_cp['artifact_id'] == prior['deterministic']['artifact_id']
            project = client.app.state.workspace.project_path('alpha')
            graph = load_checkpoint(project / baseline['output_path'], original_cp)['graph']
            snapshots = prior.get('source_xlsx_sha256_by_unit') or {uid: prior['source_xlsx_sha256'] for uid in names}
            report['deterministic'] = {**prior['deterministic'], **summary(graph, rows, args.scenario, snapshots)}
            cfg = client.get(base + '/config').json()
        else:
            create(client)
            sources = {}
            snapshots = {}
            if args.scenario == 'independent-catalogs':
                for uid in names:
                    content, _ = example(catalog=uid)
                    upload(client, uid + '.xlsx', content)
                    configured = client.get(base + '/config').json()['sources']
                    [sources[uid]] = [s['id'] for s in configured if s['id'] not in sources.values()]
                    snapshots[uid] = hashlib.sha256(content).hexdigest()
            else:
                upload(client, 'districts.xlsx', book)
                [source] = client.get(base + '/config').json()['sources']
                sources = dict.fromkeys(names, source['id'])
                snapshots = dict.fromkeys(names, hashlib.sha256(book).hexdigest())
            cfg = client.get(base + '/config').json()
            cfg['goal']['purpose'] = 'Align the regional projections and preserve their observed relationships.'
            cfg['model'].update(mode='in_environment', model=args.model, base_url=args.url,
                                response_format='json_object', max_output_tokens=4096, stream=False,
                                api_key_env='LOCAL_SYNTHETIC_MODEL_KEY')
            cfg['workflow'] = {'enabled': True, 'units': [
                {'unit_id': uid, 'goal_markdown': kind, 'source_ids': [sources[uid]]}
                for uid, kind in names.items()], 'merge_nodes': [{'node_id': 'join', 'merge_mode': 'deterministic'}],
                'edges': [{'edge_id': uid, 'source': uid, 'target': 'join'} for uid in names]}
            rid = None
        report['source_xlsx_sha256_by_unit'] = snapshots
        for mode in (['overlap_review'] if prior else ['deterministic', 'overlap_review']):
            cfg['workflow']['merge_nodes'][0]['merge_mode'] = mode
            put = client.put(base + '/config', json=cfg)
            put.raise_for_status()
            if rid is None:
                accepted = client.post(base + '/runs')
            else:
                accepted = client.post(f'{base}/runs/{rid}/resume',
                    headers={'If-Match': client.get(base + '/config').headers['etag']},
                    json={'action': 'rerun_node', 'from_node_id': 'join'})
            accepted.raise_for_status()
            rid = accepted.json()['run_id']
            until = time.monotonic() + 360
            while time.monotonic() < until:
                run = app.state.manager.get('alpha', rid)
                if run.get('finished_at'):
                    break
                time.sleep(.25)
            state = run['workflow_state']
            if run['status'] not in {'completed', 'completed_with_gaps'}:
                raise RuntimeError((run['status'], run.get('error_code'), state['merge_nodes']['join']))
            output = app.state.workspace.project_path('alpha') / run['output_path']
            artifact = state['artifacts']['join']
            checkpoint = load_checkpoint(output, artifact)
            result = summary(checkpoint['graph'], rows, args.scenario, snapshots)
            result.update(run_id=rid, artifact_id=artifact['artifact_id'], graph_digest=artifact['graph_digest'],
                          reused_nodes=state.get('reused_nodes', []),
                          upstream={uid: {k: state['artifacts'][uid][k] for k in ('artifact_id', 'graph_digest')} for uid in names},
                          model_calls=state['merge_nodes']['join']['model_calls'])
            review = checkpoint.get('overlap_review')
            if review:
                result['review_summary'] = review['summary']
                result['decisions'] = review['decisions']
                result['proposal_checks'] = review['proposal_checks']
                result['relation_proposal_checks'] = review['relation_proposal_checks']
                edges = {e['id']: e for e in checkpoint['graph']['edges']}
                fact_edges = [set(g['edge_ids']) for check in review['relation_proposal_checks']
                              for g in check['groups']]
                result['verified_fact_edge_groups'] = len(fact_edges)
                result['all_raw_edges_grouped_once'] = (set().union(*fact_edges) == edges.keys()
                    and sum(map(len, fact_edges)) == len(edges))
            report[mode] = result
            (out / 'results.json').write_text(json.dumps(report, indent=2, ensure_ascii=False))
            print(mode, result['total_nodes'], result['raw_edges'], result['model_calls'], flush=True)
            cfg = client.get(base + '/config').json()
        a, b = report['deterministic'], report['overlap_review']
        assert a['upstream'] == b['upstream'] and set(b['reused_nodes']) == set(names)
        report['delta_district_nodes'] = a['distinct_district_nodes'] - b['distinct_district_nodes']
        report['delta_raw_edges'] = a['raw_edges'] - b['raw_edges']
        if args.scenario == 'independent-catalogs':
            expected_fusion = (a['distinct_district_nodes'] == b['distinct_district_nodes'] == len(rows) * len(names)
                               and all(len(ids) == len(names) for ids in b['output_ids_by_district_code'].values())
                               and b['regions_with_multiple_contributors'] == 0)
        else:
            expected_fusion = (b['model_calls'] > 0 and b['distinct_district_nodes'] == len(rows)
                               and report['delta_district_nodes'] == len(rows) * (len(names) - 1))
        report['entity_and_source_expectations_met'] = (expected_fusion
            and all(x['namesake_different_codes_stay_separate'] and not x['missing_directed_facts']
                    and not x['missing_attributes'] and not x['unexpected_attributes']
                    and not x['unexpected_directed_facts'] and not x['unexpected_source_observations']
                    and x['source_observations_matched'] == x['source_observations_expected'] for x in [a, b]))
        report['verified_result'] = report['entity_and_source_expectations_met']
        if args.scenario == 'declared-relation':
            # One edge may already carry every source observation. A separate
            # proof over multiple remaining edges applies only to the other representation.
            relation_checks = {
                'one_stored_edge_per_fact': b['raw_edges'] == b['matched_directed_facts'] == b['expected_directed_facts'],
                'all_original_edges_in_verified_facts': b['verified_fact_edge_groups'] == len(rows)
                    and b['all_raw_edges_grouped_once'],
            }
            report['relation_representation'] = relation_checks
            report['verified_result'] = report['verified_result'] and any(relation_checks.values())
        (out / 'results.json').write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print('verified_result', report['verified_result'], flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--reuse-experiment', type=Path, help='Existing synthetic workspace with committed baseline')
    parser.add_argument('--url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--scenario', choices=['same-role', 'complementary-roles', 'independent-catalogs', 'declared-relation'],
                        required=True)
    args = parser.parse_args()
    if urlsplit(args.url).hostname not in {'localhost', '127.0.0.1', '::1'}:
        parser.error('This offline experiment requires a loopback model URL.')
    sys.exit(0 if experiment(args)['verified_result'] else 1)
