"""Opt-in evidence for an exact identity value in an offline checkpoint replay."""
from collections import Counter

from lightgraph.contracts import canonical
from lightgraph.worksheet_witness import worksheet_row_witness


class HierarchyTrace:
    def __init__(self, code):
        self.code = code
        self.nodes = {}
        self.events = []
        self.evaluated = False

    def start(self, graph):
        definitions = {d['type']: d for d in graph['definitions']['entities']}
        catalog = graph.get('evidence_catalog') or {}
        for node in graph['nodes']:
            if list((node.get('identity') or {}).values()) != [self.code]:
                continue
            bindings = {}
            for support in node.get('supports', ()):
                p = support.get('provenance') or {}
                binding = {'source': p.get('source'), 'key_columns': p.get('key_columns'),
                           'label_column': p.get('label_column'), 'observed_label': support.get('observed_label') or support.get('label'),
                           'scope_ref': support.get('scope_ref'), 'required_scope_refs': support.get('required_scope_refs'),
                           'business_context': support.get('business_context'),
                           'row_locator': worksheet_row_witness(support.get('source_row_witness'))}
                for origin in support.get('execution_origins') or [support.get('validation_origin') or {}]:
                    record = {**binding, 'unit': origin.get('contributor_id') or support.get('contributor_id'),
                              'draft_id': origin.get('draft_id'), 'revision': origin.get('draft_revision'),
                              'validation_id': origin.get('validation_id'), 'definition': origin.get('definition', p.get('definition')),
                              'mapping': origin.get('mapping', p.get('mapping'))}
                    bindings[canonical(record)] = record
            definition = definitions.get(node['type'], {})
            evidence = [{'ref': ref, 'kind': catalog.get(ref, {}).get('kind'),
                         'source_ref': catalog.get(ref, {}).get('source_ref')}
                        for ref in sorted(definition.get('identity_evidence_refs') or [])]
            self.nodes[node['id']] = {'type': node['type'], 'label': node.get('label'), 'identity': node['identity'],
                                      'contract': {k: definition[k] for k in ('key_names', 'identity_domain', 'identity_scope',
                                                   'identity_context_keys', 'identity_origin', 'definition_variant') if k in definition},
                                      'source_bindings': [bindings[k] for k in sorted(bindings)],
                                      'identity_evidence': evidence,
                                      'incident_edge_counts': Counter(), 'incident_edges': [], 'output_node_id': node['id']}
        for edge in graph['edges']:
            for nid in self.nodes.keys() & {edge['source'], edge['target']}:
                key = 'self_loop' if edge['source'] == edge['target'] else 'nonself'
                self.nodes[nid]['incident_edge_counts'][key] += 1
                bindings = {}
                for support in edge.get('supports', ()):
                    p = support.get('provenance') or {}
                    entry = {k: p.get(k) for k in ('source', 'source_key_columns', 'target_key_columns')}
                    entry['row_locator'] = worksheet_row_witness(support.get('source_row_witness'))
                    bindings[canonical(entry)] = entry
                self.nodes[nid]['incident_edges'].append({
                    **{k: edge[k] for k in ('id', 'type', 'source', 'target')},
                    'source_bindings': [bindings[k] for k in sorted(bindings)]})

    def record(self, ids, result, **fields):
        selected = sorted(self.nodes.keys() & ids)
        if selected:
            self.events.append({'node_ids': selected, 'result': result, **fields})

    def report(self, graph):
        output_ids = {n['output_node_id'] for n in self.nodes.values()}
        candidates = []
        for group in graph.get('candidate_identity_merges', ()):
            selected = sorted(output_ids.intersection(group['node_ids']))
            if selected:
                candidates.append({'reason': group.get('reason'), 'match_basis': group.get('match_basis'),
                                   'matching_output_ids': selected, 'member_count': len(group['node_ids'])})
        return {'identity_value': self.code, 'hierarchy_stage_ran': self.evaluated,
                'nodes_before_hierarchy': self.nodes, 'events': sorted(self.events, key=canonical),
                'output_ids': sorted(output_ids), 'candidate_groups': sorted(candidates, key=canonical)}
