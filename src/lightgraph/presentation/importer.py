"""Import a published graph snapshot into an empty result store, never a source DB."""
import json
from pathlib import Path
from ..contracts import GraphFact,SupportSpec,PartitionCompletion,digest
from ..errors import LightGraphError
from ..store import GraphStore


def import_graph(path,store_path,*,max_bytes=64*1024*1024):
    path=Path(path)
    if path.stat().st_size>max_bytes:raise LightGraphError('import_too_large')
    try:
        data=json.loads(path.read_text())
        if data.get('format_version')!='1.0':raise ValueError()
        domain=data['isolation_domain'];records=data['facts'];supports=data['supports']
        if not isinstance(records,list) or not isinstance(supports,list):raise ValueError()
        facts={r['id']:GraphFact(isolation_domain=domain,fact_kind=r['fact_kind'],payload=r['payload']) for r in records}
        if len(facts)!=len(records) or any(k!=v.fact_id for k,v in facts.items()):raise ValueError()
    except Exception:raise LightGraphError('invalid_graph_snapshot') from None
    with GraphStore(store_path) as store:
        if store.heads():raise LightGraphError('import_requires_empty_store')
        publication=None
        try:
            rid='import:'+digest(sorted(facts))[:24]
            publication=store.begin_publication(rid,1,'all',{'snapshot':digest(data),'domain':domain})
            covered=set()
            for item in supports:
                fid=item['fact_id'];fact=facts.get(fid)
                if fact is None:raise LightGraphError('import_unknown_fact')
                # Imported support is an explicit user-supplied result snapshot.
                # It is not an executable database rule or newly reverified source.
                support=SupportSpec(origin='user_input',source_binding='snapshot:'+item['id'],
                    scope_ref=item['scope_ref'],partition_id='all',rule_id=rid,rule_revision=1,
                    validation_ref=item.get('validation_ref'),document_ref=item.get('document_ref'),status=item['status'])
                store.stage_fact(publication,fact,support);covered.add(fid)
            if covered!=set(facts):raise LightGraphError('import_missing_support')
            store.publish_partition(PartitionCompletion(publication_id=publication,complete=True,consistent=True,dependencies_valid=True))
            graph=store.view(set(data['scopes']),domain=domain,include_candidates=True)
            return {'nodes':len(graph['nodes']),'edges':len(graph['edges']),'claims':len(graph['claims']),
                'source_revalidation':'not_performed','execution_rules_restored':False}
        except BaseException:
            if publication:
                try:store.abort(publication)
                except Exception:pass
            raise
