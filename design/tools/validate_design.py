"""Validate design documents and fixture consistency; not business Agent tests."""
from pathlib import Path
from datetime import datetime
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[1]
F = ROOT / 'fixtures'
checks = []
issues = []

def check(name, condition, detail=''):
    checks.append({'name':name,'passed':bool(condition),'detail':detail})
    if not condition:
        issues.append(name)

def load(name):
    return json.loads((F/name).read_text(encoding='utf-8'))

docs = [ROOT/name for name in [
    '00_整体方案与并行分工.md','01_统一数据与工具契约.md','02_统筹与接入任务.md',
    '03_播前策划Agent任务.md','04_播中运营Agent任务.md','05_播后复盘Agent任务.md',
    '06_验收与回放测试计划.md','07_多窗口分发说明.md',
    '08_业务闭环与动态数据详细设计.md']]
check('nine_main_documents_exist',all(p.is_file() for p in docs))
for p in docs:
    text = p.read_text(encoding='utf-8')
    check('utf8_and_title:'+p.name, text.startswith('# ') and '\ufffd' not in text)
    for label,target in re.findall(r'\[([^\]]+)\]\(([^)]+)\)',text):
        if target.startswith(('https://','http://')):
            continue
        target = target.strip('<>').split('#')[0]
        path = Path(target) if re.match(r'^[A-Za-z]:[/\\]',target) else p.parent/target
        # Validation record is written by the parent after this script succeeds.
        if path.name == '设计校验记录.md':
            continue
        check('local_link:'+p.name+':'+label,path.exists(),str(path))

business_doc = docs[-1].read_text(encoding='utf-8')
metric_ids = re.findall(r'^\| (M\d\d) \|', business_doc, re.M)
rule_ids = re.findall(r'^\| (R\d\d) ', business_doc, re.M)
case_ids = re.findall(r'^\| (V\d\d) \|', business_doc, re.M)
check('business_metric_ids_unique_and_complete', metric_ids == [f'M{i:02}' for i in range(1,13)])
check('business_rule_ids_unique_and_complete', rule_ids == [f'R{i:02}' for i in range(1,11)])
check('business_case_ids_unique_and_complete', case_ids == [f'V{i:02}' for i in range(1,17)])
for p in docs[:-1]:
    check('business_revision_link:'+p.name, '08_业务闭环与动态数据详细设计.md' in p.read_text(encoding='utf-8'))
handoff = docs[-2].read_text(encoding='utf-8')
check('four_prompts_include_business_revision', handoff.count('先读 D:/Codex project/shengbo-ops-agent-plan/08_业务闭环与动态数据详细设计.md') == 4)

json_files = list(F.glob('*.json'))
jsonl = [json.loads(line) for line in (F/'questions.jsonl').read_text(encoding='utf-8').splitlines() if line]
for p in json_files:
    item = load(p.name)
    check('envelope:'+p.name, item.get('schema_version')=='shengbo.ops.v2'
          and isinstance(item.get('revision'),int) and item.get('kind') and item.get('id'))
    check('simulation_mark:'+p.name,item.get('data_origin')=='simulated')
    check('timezone:'+p.name,datetime.fromisoformat(item['created_at']).utcoffset() is not None)
check('question_lines_envelope',all(q['schema_version']=='shengbo.ops.v2' and q['data_origin']=='simulated' for q in jsonl))

catalog = load('catalog.json')
facts = {(p['product_id'],p['product_revision'],f['id']) for p in catalog['products'] for f in p['facts']}
def check_refs(value,context):
    if isinstance(value,dict):
        if 'fact_id' in value:
            check('fact_ref:'+context+':'+value['fact_id'],
                  (value.get('product_id'),value.get('product_revision'),value['fact_id']) in facts)
        for k,v in value.items():
            check_refs(v,context+'/'+k)
    elif isinstance(value,list):
        for i,v in enumerate(value):
            check_refs(v,context+'/'+str(i))
for p in json_files:
    check_refs(load(p.name),p.name)
check('amounts_are_decimal_strings',all(isinstance(p['fields']['price'],str) for p in catalog['products']))

archive = load('archive-complete.json')
check('unique_and_contiguous_complete_stream', [e['seq'] for e in archive['events']]==list(range(1,len(archive['events'])+1)))
event_ids = {e['event_id'] for e in archive['events']}
check('unique_complete_event_ids',len(event_ids)==len(archive['events']))
for key,locator in archive['evidence_index'].items():
    obj=archive[locator['collection']][locator['index']]
    check('evidence_locator:'+key,key in [obj.get('event_id'),obj.get('attempt_id'),obj.get('action_id')])
for a in archive['playback_attempts']:
    check('attempt_time:'+a['attempt_id'],datetime.fromisoformat(a['started_at'])<=datetime.fromisoformat(a['ended_at']))
    check('attempt_origin:'+a['attempt_id'],a['data_origin']=='simulated')
for p in archive['proposals']:
    check('proposal_event_refs:'+p['proposal_id'],set(p['event_refs'])<=event_ids)
    check('proposal_ttl:'+p['proposal_id'],datetime.fromisoformat(p['created_at'])<datetime.fromisoformat(p['expires_at']))

gold=load('golden-expectations.json')['complete_archive']
questions=[e for e in archive['events'] if e['event_type']=='question']
topics={(e['product_id'],e['payload']['topic']) for e in questions}
by_event={e['event_id']:e for e in questions}
by_prop={p['proposal_id']:p for p in archive['proposals']}
answered=set()
wait=[]
for a in archive['actions']:
    prop=by_prop[a['proposal_id']]
    matches=[p for p in archive['playback_attempts'] if p['attempt_id'] in a['attempt_refs']]
    check('action_attempt_link:'+a['action_id'],len(matches)==len(a['attempt_refs']) and all(p['action_id']==a['action_id'] for p in matches))
    check('action_ended_evidence:'+a['action_id'],a['status']=='completed' and bool(matches) and all(p['status']=='completed' for p in matches)
          and set(a['result_evidence_refs'])<=event_ids)
    frozen_payload={k:a[k] for k in ['room_id','session_id','boot_id','tool','tool_args','preconditions']}
    check('action_payload_hash:'+a['action_id'],a['payload_hash']==hashlib.sha256(json.dumps(frozen_payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest())
    for eid in prop['event_refs']:
        q=by_event[eid]
        answered.add((q['product_id'],q['payload']['topic']))
        wait.append((datetime.fromisoformat(a['started_at'])-datetime.fromisoformat(q['occurred_at'])).total_seconds())
check('gold_question_counts',(len(questions),len(topics),len(answered))==(gold['raw_questions'],gold['deduplicated_topics'],gold['answered_topics']))
check('gold_coverage',len(answered)/len(topics)==gold['coverage'])
check('gold_waits',wait==gold['answer_wait_seconds_per_source_question'] and sum(wait)/len(wait)==gold['mean_answer_wait_seconds'])
bundle=load('preparation-bundle.json')
must={(r['product_id'],r['product_revision'],r['fact_id']) for row in bundle['lineup'] for r in row['must_cover_fact_refs']}
completed={(r['product_id'],r['product_revision'],r['fact_id']) for a in archive['playback_attempts'] if a['status']=='completed' for r in a['fact_refs']}
check('gold_fact_coverage',len(must)==gold['must_cover_facts'] and len(must&completed)==gold['covered_facts'])
check('business_unknown_not_zero',archive['business_metrics'] is None and archive['completeness']['business']['status']=='missing')
partial=load('archive-partial.json')
check('partial_has_no_answer_completion',partial['actions'][0]['status']=='accepted' and partial['actions'][0]['completed_at'] is None
      and all(a['action_id']!='action_size' for a in partial['playback_attempts']))
check('partial_unknown_start_preserved',partial['playback_attempts'][0]['started_at'] is None)

all_text='\n'.join(p.read_text(encoding='utf-8') for p in ROOT.rglob('*') if p.is_file() and p.suffix in {'.md','.json','.jsonl'})
check('no_api_key_pattern',not re.search(r'sk-[A-Za-z0-9_-]{16,}',all_text))
source=Path('D:/Codex project/ecommerce-voice-agent')
source_files=['app/server.py','web/audio-player.js','tests/test_rooms.py','tests/test_playback.py','tests/test_audio_player.cjs']
source_hashes={name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in source_files}
report={'date':'2026-10-07','scope':'Document references and constructed-fixture consistency only; no business Agent or speech executed',
        'main_documents':len(docs),'fixture_files':len(json_files)+1,'checks':checks,'issues':issues,
        'source_files_read_only_sha256':source_hashes,
        'source_evidence':'Static source inspection, not runtime capability validation'}
(ROOT/'设计校验结果.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'documents':len(docs),'fixtures':len(json_files)+1,'checks':len(checks),'issues':issues},ensure_ascii=False))
if issues:
    raise SystemExit(1)
