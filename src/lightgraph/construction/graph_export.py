"""Interchange formats for a published, permission-filtered property graph."""

import base64
import json
import re
from xml.etree import ElementTree as ET

from lightgraph.errors import LightGraphError


FORMATS = {
    'semantica-json': ('application/json', 'json'),
    'graphml': ('application/graphml+xml', 'graphml'),
}
_GRAPHML = 'http://graphml.graphdrawing.org/xmlns'
_INVALID_XML = re.compile(r'[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]')


def _json(value, *, ascii_only=False):
    return json.dumps(value, ensure_ascii=ascii_only, separators=(',', ':'), allow_nan=False)


def _record(item, *, relationship=False):
    fields = ('scopes', 'supports', 'identity', 'identity_types', 'aliases', 'label_variants',
              'english_names', 'descriptions')
    metadata = {key: item[key] for key in fields if key in item}
    # Semantica accepts these fields directly in KGBuilder. Supports stay
    # separate so each project/period-specific value keeps its own witness.
    rec = {'id': item['id'], 'type': item['type'], 'properties': item.get('properties', {}),
           'metadata': metadata}
    if relationship:
        rec.update(source=item['source'], target=item['target'])
    else:
        rec['name'] = item.get('display_label') or item.get('label') or item['id']
    return rec


def _identifier(original):
    """Use ordinary IDs unchanged; encode values outside our XML-safe subset."""
    if re.fullmatch(r'[a-zA-Z0-9_.:-]+', original) and not original.startswith('encoded_'):
        return original
    return 'encoded_' + base64.urlsafe_b64encode(original.encode('utf-8')).decode().rstrip('=')


def _graphml(data):
    root = ET.Element('graphml', xmlns=_GRAPHML)
    for name, domain in [('name', 'node'), ('type', 'all'), ('properties', 'all'),
                         ('metadata', 'all'), ('original_id', 'all'), ('original_text', 'all')]:
        ET.SubElement(root, 'key', {'id': name, 'for': domain, 'attr.name': name, 'attr.type': 'string'})
    graph = ET.SubElement(root, 'graph', id='G', edgedefault='directed')
    ET.SubElement(graph, 'data', key='metadata').text = _json(data['metadata'], ascii_only=True)

    def write_record(record, tag, attributes):
        xml_id = _identifier(record['id'])
        element = ET.SubElement(graph, tag, id=xml_id, **attributes)
        if xml_id != record['id']:
            ET.SubElement(element, 'data', key='original_id').text = _json(record['id'], ascii_only=True)
        originals = {}
        for key in ('name', 'type', 'properties', 'metadata'):
            if key not in record:
                continue
            if key in ('properties', 'metadata'):
                text = _json(record[key], ascii_only=True)
            else:
                text = record[key]
                if _INVALID_XML.search(text) or '\r' in text:
                    originals[key] = text
                    text = _INVALID_XML.sub('\ufffd', text).replace('\r', '\n')
            ET.SubElement(element, 'data', key=key).text = text
        if originals:
            ET.SubElement(element, 'data', key='original_text').text = json.dumps(originals, ensure_ascii=True)

    for entity in data['entities']:
        write_record(entity, 'node', {})
    for edge in data['relationships']:
        write_record(edge, 'edge', {'source': _identifier(edge['source']), 'target': _identifier(edge['target'])})
    return ET.tostring(root, encoding='utf-8', xml_declaration=True).decode('utf-8')


def serialize_graph(graph, format_name):
    if format_name == 'yaml':
        from .workspace import dump_yaml
        return dump_yaml(graph)
    if format_name not in FORMATS:
        raise LightGraphError('graph_export_format_invalid')
    data = {
        'entities': [_record(node) for node in graph['nodes']],
        'relationships': [_record(edge, relationship=True) for edge in graph['edges']],
        'metadata': {'format_version': 'lightgraph.interchange/1', 'version_id': graph.get('version_id'),
                     'business_context': graph.get('business_context')},
    }
    return _json(data) if format_name == 'semantica-json' else _graphml(data)
