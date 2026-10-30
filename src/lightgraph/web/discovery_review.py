"""Project-scoped investigation requests; these never accept graph facts."""
from lightgraph.errors import LightGraphError
from lightgraph.runtime.candidates import CandidateQueue
from lightgraph.runtime.locking import run_lock
from lightgraph.store import GraphStore
from pathlib import Path


def discovery_review(workspace,pid,*,offset=0,decision=None):
    config=workspace.resolved_config(pid)
    path=Path(config.output_dir)/'graph.sqlite'
    if path.is_symlink():raise LightGraphError('workspace_path_invalid')
    if not path.exists():
        if decision:raise LightGraphError('discovery_candidate_not_found')
        return {'candidates':[],'total_candidates':0,'next_offset':None,'counts':{}}
    # Serialize decisions with CLI and worker publication, not only web requests.
    with run_lock(config.output_dir), GraphStore(path) as store:
        queue=CandidateQueue(store,config.goal,config.scopes,config.isolation_domain,config.sources)
        if decision:
            return queue.decide(decision.candidate_id,decision.action,decision.revision)
        return queue.listing(offset)
