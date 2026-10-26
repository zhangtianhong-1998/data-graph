"""Fixed output writers; source objects and exception bodies are never accepted."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
import tempfile
from ..contracts import Coverage, RunReport, RuleSpec
from ..errors import LightGraphError


def atomic_text(path, text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=".published-",dir=path.parent)
    try:
        os.chmod(tmp,0o600)
        with os.fdopen(fd,"w",encoding="utf-8") as out:out.write(text)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def write_report(path, report:RunReport):
    if not isinstance(report,RunReport):raise LightGraphError("retention_policy_violation")
    atomic_text(path,report.model_dump_json(indent=2))


def write_rules(path, rules:list[RuleSpec]):
    import yaml
    if any(not isinstance(r,RuleSpec) or r.status not in ("validated","published","canary_checked") for r in rules):
        raise LightGraphError("unvalidated_rule_persistence")
    from ..store.policy import PersistencePolicy
    for rule in rules:PersistencePolicy().check_record(rule)
    exported=[]
    for rule in rules:
        # Historical rule records remain immutable, but their source-derived
        # partition tokens must not be copied into a newly written artifact.
        # The writer cannot infer a fresh full validation from an old rule cache.
        if any(re.fullmatch(r"partition_[0-9a-f]{64}", token) for token in rule.coverage.partitions):
            rule=rule.model_copy(deep=True)
            rule.coverage=Coverage(scope="partition", partitions=[], consistent=False)
        exported.append(rule.model_dump(mode="json",by_alias=True))
    atomic_text(path,yaml.safe_dump(exported,allow_unicode=True,sort_keys=False))


def retain_check(roots, markers):
    """Inspect application artifacts only. Deployment and provider retention require separate attestation."""
    hits=[];checked=0
    for root in roots:
        root=Path(root)
        for p in ([root] if root.is_file() else root.rglob('*')):
            if not p.is_file() or p.is_symlink():continue
            checked+=1
            try:
                with p.open('rb') as f:
                    overlap=b''
                    while block:=f.read(1024*1024):
                        data=overlap+block
                        if any(m.encode() in data for m in markers):
                            hits.append(str(p));break
                        overlap=data[-max([len(m.encode()) for m in markers]+[1]):]
            except OSError:raise LightGraphError("retention_scan_incomplete") from None
    return {"files_checked":checked,"violations":len(hits),"paths":hits,"deployment_verified":False}
