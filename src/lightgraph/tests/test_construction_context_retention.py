"""Native surface/replay tests; no business data or external model is used."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_native_context_retention_replays_and_respects_complete_tool_groups(tmp_path):
    repo = Path(__file__).parents[3]
    vendor = repo / 'vendor/deepseek-harness'
    loader = vendor / 'node_modules/tsx/dist/esm/index.mjs'
    if not shutil.which('node') or not loader.exists():
        pytest.skip('vendored Harness bootstrap is required')
    script = tmp_path / 'retention.mjs'
    script.write_text(r'''
import assert from 'node:assert/strict'
import { Session } from SESSION
import { estimateMessage } from ESTIMATE
import { retainContext, readEventPage } from RETENTION
const meter = { estimateMessage }
const block = text => ({type:'text',text})
let id = 0
const user = text => ({id:`u${id++}`,role:'user',source:{kind:'user'},content:[block(text)]})
function assistant(s, calls) {
  return s.append('assistant/message', {turn:1,step:1,stream:[],message:{
    id:`a${id++}`,role:'assistant',source:{kind:'model',provider:'fixture',model:'fixture'},
    content:calls.map(call => ({type:'tool-call',id:call,name:'mcp__graph__save_graph_draft',
      arguments:JSON.stringify({yaml:'中😀'.repeat(30000)})}))}}, {surfaceOp:'append'})
}
function result(s, call, text, isError=false) {
  return s.append('tool/result',{turn:1,step:1,
    ...(isError?{error:{name:'ToolError',code:'KEEP_ERROR'}}:{}),message:{
      id:`r${id++}`,role:'tool',toolCallId:call,source:{kind:'tool',callId:call},isError,content:[block(text)]}},
    {surfaceOp:'append'})
}
const s = Session.create('retention')
s.append('user/message',user('ORIGINAL_GOAL'),{surfaceOp:'append'})
const original = assistant(s,['one','two'])
result(s,'one',JSON.stringify({status:'ok',draft_id:'keep-draft',result_ref:'result:keep'}))
retainContext(s,meter,1024)
assert(s.deriveMessages().some(m=>m.role==='assistant')) // second result not settled
s.append('user/message',user('REAL_USER_BOUNDARY'),{surfaceOp:'append'})
result(s,'two','failure',true)
retainContext(s,meter,1024)
assert(s.deriveMessages().some(m=>m.role==='assistant')) // never span the user boundary

const ready = Session.create('settled')
ready.append('user/message',user('ORIGINAL_GOAL'),{surfaceOp:'append'})
const original2 = assistant(ready,['three','four'])
result(ready,'three',JSON.stringify({status:'ok',draft_id:'keep-draft',result_ref:'result:keep'}))
result(ready,'four',JSON.stringify({status:'error',code:'KEEP_DETAIL'}),true)
retainContext(ready,meter,1024)
const derived = ready.deriveMessages()
assert.equal(derived.length,2)
const text = JSON.stringify(derived)
assert(!text.includes('中😀'.repeat(200)))
for(const marker of ['keep-draft','result:keep','KEEP_ERROR','KEEP_DETAIL','not a new user instruction']) assert(text.includes(marker))
assert.equal(ready.eventAt(original2.seq).data.message.content[0].arguments, original2.data.message.content[0].arguments)
const replay = Session.create('settled',ready.snapshotEvents())
assert.deepEqual(replay.deriveMessages(), derived)
const before = ready.snapshotEvents().length
retainContext(ready,meter,1024)
assert.equal(ready.snapshotEvents().length,before) // idempotent

const large = Session.create('large')
assistant(large,['large-output'])
const huge = result(large,'large-output','BEGIN'+'中😀'.repeat(50000)+'END',true)
retainContext(large,meter,1024)
assert(large.snapshotEvents().some(e=>e.type==='compaction/prune'))
const group = JSON.parse(large.deriveMessages()[0].content[0].text)
assert.equal(group.outcomes[0].is_error,true)
assert.equal(group.outcome_index.tool,'session_event_read_page')
assert.equal(group.summary_only,true)
assert(estimateMessage(large.deriveMessages()[0])<=1024)
assert(large.eventAt(huge.seq).data.message.content[0].text.includes('中😀'.repeat(50000)))

for(const limit of [512,1024,4096]) {
  const batch=Session.create('batch')
  const ids=Array.from({length:20},(_,index)=>`batch-${index}`)
  const batchAssistant=assistant(batch,ids)
  const batchResults=ids.map((call,index)=>result(batch,call,JSON.stringify({
    status:index===19?'error':'ok',code:index===19?'DOMAIN_FAILURE':'OK',
    result_ref:`result:batch-${index}`,coverage:{complete:false},material:'k'.repeat(limit*3)}),index===18))
  const originalCount=batch.snapshotEvents().length
  retainContext(batch,meter,limit)
  assert.equal(batch.deriveMessages().length,1)
  assert(estimateMessage(batch.deriveMessages()[0])<=limit)
  const summary=JSON.parse(batch.deriveMessages()[0].content[0].text)
  assert.equal(summary.call_count,20);assert.equal(summary.outcome_count,20)
  assert.equal(summary.tool_error_count,1);assert.equal(summary.reported_error_count,1)
  assert(summary.summary_only && summary.presentation_truncated)
  assert.equal(summary.outcomes_omitted+summary.outcomes.length,20)
  if(limit<4096)assert(summary.outcomes_omitted>0)
  assert(summary.outcomes.every(outcome=>outcome.details_omitted))
  assert.equal(summary.outcomes[0].is_error,true) // native error first
  assert(summary.outcomes.some(outcome=>outcome.result_ref==='result:batch-18'))
  const sourceIndex=batch.eventAt(summary.outcome_index.seq)
  assert.equal(sourceIndex.type,'compaction/prune')
  assert.deepEqual(sourceIndex.data.shadowedSeqs,[batchAssistant.seq,...batchResults.map(row=>row.seq)])
  for(const row of [batchAssistant,...batchResults]) assert.deepEqual(batch.eventAt(row.seq),row)
  assert.equal(batch.snapshotEvents().length,originalCount+2) // index + record only, no tool replay
  assert.deepEqual(Session.create('batch',batch.snapshotEvents()).deriveMessages(),batch.deriveMessages())
}

// Actual domain coverage is nested. Even omitted entries must contribute to
// exact incomplete/truncated counts, and retained entries keep their version.
for(const count of [12,80]) {
  const coverageBatch=Session.create('coverage-batch')
  const ids=Array.from({length:count},(_,index)=>`coverage-${index}`)
  assistant(coverageBatch,ids)
  const originals=ids.map((call,index)=>result(coverageBatch,call,JSON.stringify({
    status:'ok',result_ref:`result:coverage-${index}`,revision:index,
    material_revision:`immutable-revision-${index}`,
    coverage:{result_complete:false,truncated:true},
    result_presentation:{source_complete:false}})))
  retainContext(coverageBatch,meter,1024)
  const message=coverageBatch.deriveMessages()[0]
  assert(estimateMessage(message)<=1024)
  const summary=JSON.parse(message.content[0].text)
  assert(summary.summary_only && summary.presentation_truncated && summary.outcomes_omitted>0)
  assert.equal(summary.incomplete_count,count)
  assert.equal(summary.truncated_count,count)
  assert.equal(summary.completeness_unknown_count,0)
  assert(summary.outcomes.length>0)
  for(const outcome of summary.outcomes){
    assert.equal(outcome.result_complete,false)
    assert.equal(outcome.source_complete,false)
    assert.equal(outcome.truncated,true)
    assert.equal(outcome.material_revision,`immutable-revision-${outcome.call_index}`)
  }
  for(const row of originals)assert.deepEqual(coverageBatch.eventAt(row.seq),row)
}
const mixed=Session.create('mixed-completeness')
const mixedIds=Array.from({length:8},(_,index)=>`mixed-${index}`)
assistant(mixed,mixedIds)
const controls=[{complete:true},{},{result_presentation:{source_complete:false}},
  {coverage:{result_complete:false,truncated:true}},{coverage:{result_complete:true,truncated:false}},
  {status:'error'},{source_complete:false,coverage:{result_complete:true}},
  {result_presentation:{presentation_truncated:true}}]
mixedIds.forEach((call,index)=>result(mixed,call,JSON.stringify({status:'ok',material:'q'.repeat(300),...controls[index]})))
retainContext(mixed,meter,1024)
const mixedRecord=JSON.parse(mixed.deriveMessages()[0].content[0].text)
assert.equal(mixedRecord.incomplete_count,3)
assert.equal(mixedRecord.truncated_count,1) // display-only clipping is not source truncation
assert.equal(mixedRecord.completeness_unknown_count,3)
assert.equal(mixedRecord.outcomes[0].call_index,5) // failure before coverage gaps
assert.equal(mixedRecord.outcomes[0].completeness_unknown,true)
assert(mixedRecord.outcomes.slice(1,4).every(row=>[2,3,6].includes(row.call_index)))
assert(estimateMessage(mixed.deriveMessages()[0])<=1024)

const tooSmall=Session.create('small-metadata')
assistant(tooSmall,['small'])
result(tooSmall,'small','ok')
const tooSmallCount=tooSmall.snapshotEvents().length
assert.throws(()=>retainContext(tooSmall,meter,32),{code:'construction_context_metadata_limit_exceeded'})
assert.equal(tooSmall.snapshotEvents().length,tooSmallCount)
const hugeMetadata=Session.create('long-session-'.repeat(1000))
assistant(hugeMetadata,['metadata'])
result(hugeMetadata,'metadata','ok')
const metadataCount=hugeMetadata.snapshotEvents().length
assert.throws(()=>retainContext(hugeMetadata,meter,512),{code:'construction_context_metadata_limit_exceeded'})
assert.equal(hugeMetadata.snapshotEvents().length,metadataCount)

const bounded = Session.create('estimator-disagreement')
assistant(bounded,['bounded'])
result(bounded,'bounded',JSON.stringify({status:'error',code:'KEEP_DOMAIN_ERROR',
  result_ref:'result:original-result',coverage:{complete:false,result_complete:false,truncated:true},
  material_revision:'immutable-material-version',
  result_presentation:{stored_complete_tool_result:true,source_complete:false},material:'z'.repeat(100000)}))
retainContext(bounded,meter,1024)
const boundedGroup=JSON.parse(bounded.deriveMessages()[0].content[0].text)
const boundedOutput=JSON.parse(boundedGroup.outcomes[0].content[0].text)
assert.equal(boundedOutput.status,'error')
assert.equal(boundedOutput.code,'KEEP_DOMAIN_ERROR')
assert.equal(boundedOutput.result_ref,'result:original-result')
assert.deepEqual(boundedOutput.coverage,{complete:false,result_complete:false,truncated:true})
assert.equal(boundedOutput.result_complete,false)
assert.equal(boundedOutput.source_complete,false)
assert.equal(boundedOutput.truncated,true)
assert.equal(boundedOutput.material_revision,'immutable-material-version')
assert(estimateMessage({role:'tool',content:boundedGroup.outcomes[0].content})<=1024)

// Unicode offsets reconstruct the exact original JSON; delegated authorization
// errors and cancellation must never turn into successful partial reads.
const event = JSON.stringify({text:'α中😀'.repeat(10000)},null,2)
let denied=false,calls=0
const ctx={tokenMeter:meter,tools:{get:()=>({execute:async(args,exec)=>{
  calls++;assert.deepEqual(args,{seq:5});assert(exec.agent)
  if(denied)throw new Error('SESSION_QUERY_TOOL_UNAUTHORIZED')
  return 'Session changed title\nTarget event seq 5:\n```json\n'+event+'\n```'
}})}}
const exec={agent:{session:{id:'page-session'}},signal:new AbortController().signal}
let offset=0,joined='',pages=0
while(offset!==null){
  const raw=await readEventPage(ctx,{seq:5,offset},exec,512)
  assert(estimateMessage({role:'user',content:[block(raw)]})<=512)
  const page=JSON.parse(raw);joined+=page.content;offset=page.next_offset;pages++
}
assert.equal(joined,event);assert(pages>10)
denied=true
await assert.rejects(readEventPage(ctx,{seq:5},exec,512),/UNAUTHORIZED/)
await assert.rejects(readEventPage(ctx,{seq:5,offset:-1},exec,512),/nonnegative/)
const cancelled=new AbortController();cancelled.abort()
const callsBeforeCancel=calls
await assert.rejects(readEventPage(ctx,{seq:5},{...exec,signal:cancelled.signal},512))
assert.equal(calls,callsBeforeCancel)
console.log(JSON.stringify({replay:true,pages,calls}))
'''.replace('SESSION', json.dumps((vendor / 'packages/core/session/src/index.ts').as_uri()))
        .replace('ESTIMATE', json.dumps((vendor / 'packages/llm/token-meter/src/estimate.ts').as_uri()))
        .replace('RETENTION', json.dumps((repo / 'src/lightgraph/construction/runtime/context-retention.mjs').as_uri())))
    result = subprocess.run(['node', '--import', str(loader), str(script)],
                            cwd=vendor, capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(result.stdout)['replay'] is True
