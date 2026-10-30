"""Export only an authorized, publication-consistent graph projection."""
import csv
import io
import json
from pathlib import Path
from importlib.resources import files
import yaml
from ..runtime.retention import atomic_text


def export_graph(store,output,*,scopes,domain='default',include_candidates=True):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    graph=store.view(scopes,domain=domain,include_candidates=include_candidates)
    graph.update(format_version='1.0',isolation_domain=domain,scopes=sorted(scopes))
    from .schema import snapshot_schema
    atomic_text(output/'graph.schema.json',json.dumps(snapshot_schema(graph.get('semantic_schema')),
                                                     ensure_ascii=False,sort_keys=True,indent=2))
    atomic_text(output/'graph.json',json.dumps(graph,ensure_ascii=False,sort_keys=True,indent=2))
    atomic_text(output/'graph.yaml',yaml.safe_dump(graph,allow_unicode=True,sort_keys=True))
    for key in ('nodes','edges'):
        rows=graph.get(key,[])
        cols=sorted({k for r in rows for k in r})
        buf=io.StringIO();writer=csv.DictWriter(buf,fieldnames=cols);writer.writeheader()
        for row in rows:
            values={k:json.dumps(v,ensure_ascii=False,sort_keys=True) if isinstance(v,(dict,list)) else v for k,v in row.items()}
            writer.writerow({k:("'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v) for k,v in values.items()})
        atomic_text(output/(key+'.csv'),buf.getvalue())
    preview={k:v for k,v in graph.items() if k not in ('facts','supports')}
    preview['provenance_location']='graph.json'
    payload=json.dumps(preview,ensure_ascii=False).replace('<','\\u003c').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    template=files('lightgraph').joinpath('presentation/viewer.html').read_text()
    atomic_text(output/'viewer.html',template.replace('/*GRAPH_DATA*/{}',payload))
    return graph
