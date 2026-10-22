"""Create design fixtures only. No Agent, network, speech or business calls."""
from pathlib import Path
from datetime import datetime, timedelta
import copy
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'fixtures'
OUT.mkdir(parents=True, exist_ok=True)
BASE = datetime.fromisoformat('2026-10-07T10:00:00+08:00')
VERSION = 'shengbo.ops.v2'

def ts(seconds=0):
    return (BASE + timedelta(seconds=seconds)).isoformat()

def meta(kind, identity, seconds=0):
    return {'schema_version': VERSION, 'kind': kind, 'id': identity,
            'revision': 1, 'created_at': ts(seconds), 'data_origin': 'simulated'}

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()

def write(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def product(identity, name, price, selling, knowledge, facts):
    raw = {'name': name, 'price': price, 'promo_price': None, 'currency': 'TWD',
           'selling_points': selling, 'promotion': '历史活动价未核实有效期，本轮演示不启用优惠承诺。',
           'knowledge': knowledge}
    return {'product_id': identity, 'product_revision': 'fixture-sha256:' + digest(raw),
            'fields': raw, 'facts': [{'id': k, 'text': v} for k, v in facts.items()],
            'source': 'design_fixture_not_current_offer', 'data_origin': 'simulated',
            'verified_at': None, 'valid_until': None,
            'missing_fields': ['inventory', 'promotion_valid_until', 'current_delivery_date']}

shoe = product('product_slippers', '足弓支撑静音防滑厚底凉拖鞋', '178',
               'EVA材质，足弓支撑，厚底；地面有水时仍须小心行走。',
               '尺码36-37至44-45；脚背宽或脚型偏肉可斟酌大一码。\n历史样例配送9月21日至9月30日，当前日期不可直接沿用。',
               {'NAME': '足弓支撑静音防滑厚底凉拖鞋', 'PRICE': '标价178 TWD。',
                'SELLING': 'EVA材质、足弓支撑和厚底设计。',
                'SIZE': '尺码36-37、38-39、40-41、42-43、44-45；脚背宽可斟酌大一码。',
                'CARE': '浴室地面遇水仍请小心行走。',
                'PROMOTION': '历史样例活动价107 TWD，当前有效期未核实，不作现行优惠承诺。',
                'DELIVERY': '历史资料含9月21日至9月30日，不能当成当前配送承诺。'})
bag = product('product_bag', '演示收纳袋', '59', '仅用于测试的聚酯材质收纳袋。',
              '本商品完全为构造样例，不表示真实在售商品。',
              {'NAME': '演示收纳袋', 'PRICE': '构造标价59 TWD。', 'MATERIAL': '构造资料标明聚酯材质。'})
products = [shoe, bag]

def pref(p):
    return {'product_id': p['product_id'], 'product_revision': p['product_revision']}

def fref(p, fid):
    return {**pref(p), 'fact_id': fid}

catalog = {**meta('CatalogFixture', 'catalog_demo'), 'products': products,
           'notice': '全部为设计测试数据；拖鞋部分参考用户历史样例并做演示处理，不能导入实际销售承诺。'}
write('catalog.json', catalog)
request = {**meta('PreparationRequest', 'request_demo'), 'room_id': None,
           'target_start_at': ts(), 'objective': {'goal': '验证资料诊断和商品规格答疑闭环',
               'priority_product_ids': ['product_slippers', 'product_bag'],
               'constraints': ['简体中文普通话', '不猜库存', '不承诺历史物流日期', '不启用未核实活动']},
           'catalog_snapshots': products, 'existing_scripts': [], 'adopted_improvements': [],
           'policy': {'mode': 'observe', 'version_count_per_product': 2}, 'budget': {'max_model_calls_per_product': 10,
                    'max_tool_calls_per_product': 20, 'max_repair_rounds': 2}}
write('preparation-request.json', request)

def sentence(identity, p, stage, text, fact_ids):
    return {'sentence_id': identity, 'stage': stage, 'display_text': text, 'spoken_text': text,
            'kind': 'product_claim', 'fact_refs': [fref(p, fid) for fid in fact_ids]}

s1 = sentence('sentence_price', shoe, 'introduction', '这款凉拖鞋标价一百七十八新台币。', ['NAME', 'PRICE'])
s2 = sentence('sentence_benefit', shoe, 'benefit', '它采用EVA材质，带有足弓支撑和厚底设计。', ['SELLING'])
s3 = sentence('sentence_size', shoe, 'specification', '尺码从三十六码到四十五码，脚背宽可以斟酌大一码。', ['SIZE'])
s4 = sentence('sentence_material', bag, 'specification', '这款演示收纳袋的构造资料标明为聚酯材质。', ['MATERIAL'])
answer = '这款尺码从三十六码到四十五码，脚背较宽可以斟酌大一码。'
scripts = [
    {'script_id': 'script_shoe', 'script_revision': 1, 'product_ref': pref(shoe),
     'status': 'approved', 'sentences': [s1, s2, s3], 'approval_source': 'fixture_operator'},
    {'script_id': 'script_bag', 'script_revision': 1, 'product_ref': pref(bag),
     'status': 'approved', 'sentences': [s4], 'approval_source': 'fixture_operator'}]
faq = {'faq_id': 'faq_size', 'product_ref': pref(shoe), 'question': '有哪些尺码，脚背宽怎么选？',
       'answer_text': answer, 'fact_refs': [fref(shoe, 'SIZE')],
       'unknown_policy': 'ask_operator', 'review_status': 'approved', 'content_hash': digest(answer)}
bundle = {**meta('PreparationBundle', 'plan_demo'), 'plan_id': 'plan_demo',
          'room_id': 'room_demo', 'source_request_id': 'request_demo', 'status': 'published',
          'objective': request['objective'], 'catalog_refs': products,
          'issues': [{'issue_id': 'issue_delivery', 'severity': 'warning', 'field_ref': 'DELIVERY',
                      'reason': '历史配送日期不能沿用', 'required_action': '不作配送日期承诺；实际销售需确认新日期',
                      'blocking_scope': 'delivery_claim_only'}],
          'script_candidates': [{'draft_id': 'draft_'+s['script_id'], **s, 'angle': '规格和材料说明',
                                 'blocks': [], 'review_status': 'approved'} for s in scripts],
          'faq': [faq], 'lineup': [
              {'product_ref': pref(shoe), 'script_refs': [{'script_id': 'script_shoe', 'revision': 1}],
               'order': 1, 'must_cover_fact_refs': [fref(shoe, 'PRICE'), fref(shoe, 'SIZE')],
               'reason': '先介绍主要样例，再覆盖规格', 'estimated_duration': None, 'duration_basis': 'not_measured'},
              {'product_ref': pref(bag), 'script_refs': [{'script_id': 'script_bag', 'revision': 1}],
               'order': 2, 'must_cover_fact_refs': [fref(bag, 'MATERIAL')],
               'reason': '验证第二商品隔离', 'estimated_duration': None, 'duration_basis': 'not_measured'}],
          'live_policy': {'mode': 'manual_confirm', 'allowed_tools': ['insert_after_sentence'],
                          'cooldown_seconds': 30, 'proposal_ttl_seconds': 15, 'revision': 1},
          'preflight': [{'scope': 'fixture_environment', 'status': 'pass', 'evidence_refs': ['fixture:scenario_normal']}],
          'approvals': [{'actor': 'fixture_operator', 'object_id': 'faq_size', 'content_hash': digest(answer),
                         'product_ref': pref(shoe), 'at': ts(-10), 'result': 'approved', 'data_origin': 'simulated'}],
          'notice': 'published和approved只模拟批准后的输入形态，不能代表真实人工批准，也不能直接发布到现用声播。'}
write('preparation-bundle.json', bundle)

pending = [{'queue_id': 'queue_benefit', 'script_id': 'script_shoe', 'sentence_id': s2['sentence_id'],
            'product_ref': pref(shoe)},
           {'queue_id': 'queue_size', 'script_id': 'script_shoe', 'sentence_id': s3['sentence_id'],
            'product_ref': pref(shoe)},
           {'queue_id': 'queue_material', 'script_id': 'script_bag', 'sentence_id': s4['sentence_id'],
            'product_ref': pref(bag)}]
snapshot = {**meta('RoomSnapshot', 'snapshot_demo', 11), 'room_id': 'room_demo',
            'session_id': 'session_demo', 'boot_id': 'boot_demo',
            'plan_ref': {'plan_id': 'plan_demo', 'revision': 1}, 'status': 'playing',
            'queue_revision': 1, 'cursor_revision': 1, 'control_revision': 0, 'epoch': 1,
            'current': {'product_ref': pref(shoe), 'script_id': 'script_shoe', 'script_revision': 1,
                        'sentence_id': s1['sentence_id'], 'queue_id': 'queue_price', 'attempt_id': 'attempt_price'},
            'pending_queue': pending, 'insertion': None,
            'player': {'connected': True, 'source': 'fake_player'},
            'capabilities': {'insert_after_sentence': True, 'switch_platform_cart': False}}
write('live-state.json', snapshot)

def event(identity, seq, seconds, etype, payload, p=None):
    return {**meta('NormalizedEvent', identity, seconds), 'event_id': identity,
            'room_id': 'room_demo', 'session_id': 'session_demo', 'seq': seq,
            'occurred_at': ts(seconds), 'received_at': ts(seconds), 'event_type': etype,
            'source': 'fixture', 'product_id': p, 'payload': {'source_event_id': identity, **payload}}

q1 = event('question_size_1', 2, 10, 'question', {'text': '脚背宽选多大？', 'topic': 'size'}, shoe['product_id'])
q2 = event('question_size_2', 3, 11, 'question', {'text': '有多少码，脚背宽怎么选？', 'topic': 'size'}, shoe['product_id'])
q3 = event('question_stock', 9, 25, 'question', {'text': '现在还剩多少库存？', 'topic': 'inventory'}, shoe['product_id'])
(OUT/'questions.jsonl').write_text('\n'.join(json.dumps(q, ensure_ascii=False) for q in [q1,q2,q3])+'\n', encoding='utf-8')

proposal = {**meta('Proposal', 'proposal_size', 11), 'proposal_id': 'proposal_size',
            'room_id': 'room_demo', 'session_id': 'session_demo', 'plan_ref': {'plan_id': 'plan_demo', 'revision': 1},
            'based_on': {'boot_id': 'boot_demo', 'queue_revision': 1, 'control_revision': 0, 'product_ref': pref(shoe)},
            'event_refs': [q1['event_id'], q2['event_id']], 'fact_refs': [fref(shoe,'SIZE')],
            'decision': 'propose_action', 'reason': '合并同商品尺码问题，使用已审核FAQ',
            'tool': 'insert_after_sentence', 'tool_args': {'text': answer, 'faq_id': 'faq_size', 'content_hash': digest(answer)},
            'expires_at': ts(26), 'approval_state': 'approved', 'approval_source': 'fixture_operator', 'policy_revision': 1}
stock_proposal = {**meta('Proposal', 'proposal_stock', 26), 'proposal_id': 'proposal_stock',
                 'room_id': 'room_demo', 'session_id': 'session_demo', 'plan_ref': proposal['plan_ref'],
                 'based_on': {**proposal['based_on'], 'control_revision': 1}, 'event_refs': [q3['event_id']], 'fact_refs': [],
                 'decision': 'ask_operator', 'reason': '无库存来源，不编造数量。', 'tool': None, 'tool_args': None,
                 'expires_at': ts(41), 'approval_state': 'not_required', 'policy_revision': 1}
action = {**meta('ActionRecord', 'action_size', 11), 'action_id': 'action_size', 'proposal_id': 'proposal_size',
          'room_id': 'room_demo', 'session_id': 'session_demo', 'boot_id': 'boot_demo', 'actor': 'agent',
          'tool': proposal['tool'], 'tool_args': proposal['tool_args'], 'payload_hash': None,
          'preconditions': proposal['based_on'], 'status': 'completed', 'submitted_at': ts(11),
          'accepted_at': ts(11), 'started_at': ts(20), 'completed_at': ts(24),
          'attempt_refs': ['attempt_answer'], 'result_evidence_refs': ['playback_answer_end'], 'error': None}
action['payload_hash'] = digest({k:action[k] for k in ['room_id','session_id','boot_id','tool','tool_args','preconditions']})

def attempt(identity, p, script, sent, queue, start, end, facts, action_id=None):
    return {'attempt_id': identity, 'product_ref': pref(p), 'script_id': script, 'script_revision': 1,
            'sentence_id': sent, 'queue_id': queue, 'action_id': action_id, 'started_at': ts(start),
            'ended_at': ts(end), 'status': 'completed', 'source': 'fake_player',
            'data_origin': 'simulated', 'fact_refs': [fref(p, fid) for fid in facts]}

attempts = [attempt('attempt_price',shoe,'script_shoe',s1['sentence_id'],'queue_price',0,12,['PRICE']),
            attempt('attempt_answer',shoe,'script_shoe','sentence_answer','queue_answer',20,24,['SIZE'],'action_size'),
            attempt('attempt_benefit',shoe,'script_shoe',s2['sentence_id'],'queue_benefit',24,44,['SELLING']),
            attempt('attempt_size',shoe,'script_shoe',s3['sentence_id'],'queue_size',44,52,['SIZE']),
            attempt('attempt_bag',bag,'script_bag',s4['sentence_id'],'queue_material',52,60,['MATERIAL'])]
events = [event('playback_price_start',1,0,'playback_started',{'attempt_id':'attempt_price'},shoe['product_id']), q1,q2,
          event('action_size_accepted',4,11,'action_accepted',{'action_id':'action_size'},shoe['product_id']),
          event('playback_price_end',5,12,'playback_ended',{'attempt_id':'attempt_price'},shoe['product_id']),
          event('playback_answer_start',6,20,'playback_started',{'attempt_id':'attempt_answer','action_id':'action_size'},shoe['product_id']),
          event('playback_answer_end',7,24,'playback_ended',{'attempt_id':'attempt_answer','action_id':'action_size'},shoe['product_id']),
          event('playback_benefit_start',8,24,'playback_started',{'attempt_id':'attempt_benefit'},shoe['product_id']),q3,
          event('playback_benefit_end',10,44,'playback_ended',{'attempt_id':'attempt_benefit'},shoe['product_id']),
          event('playback_size_start',11,44,'playback_started',{'attempt_id':'attempt_size'},shoe['product_id']),
          event('playback_size_end',12,52,'playback_ended',{'attempt_id':'attempt_size'},shoe['product_id']),
          event('playback_bag_start',13,52,'playback_started',{'attempt_id':'attempt_bag'},bag['product_id']),
          event('playback_bag_end',14,60,'playback_ended',{'attempt_id':'attempt_bag'},bag['product_id'])]
index = {e['event_id']: {'collection':'events','index':i} for i,e in enumerate(events)}
index.update({a['attempt_id']:{'collection':'playback_attempts','index':i} for i,a in enumerate(attempts)})
index['action_size'] = {'collection':'actions','index':0}
archive = {**meta('SessionArchive','archive_demo',65), 'archive_id':'archive_demo', 'room_id':'room_demo',
           'session_id':'session_demo', 'started_at':ts(), 'ended_at':ts(60), 'sealed_at':ts(65),
           'end_reason':'fixture_queue_completed', 'preparation_ref':{'plan_id':'plan_demo','revision':1},
           'catalog_snapshots':products,'script_snapshots':scripts,'events':events,
           'proposals':[proposal,stock_proposal], 'actions':[action], 'playback_attempts':attempts,
           'business_metrics':None,
           'completeness':{k:{'status':'complete' if k!='business' else 'missing',
                               'coverage':{'start':ts(),'end':ts(60)},'gaps':[] if k!='business' else ['no_business_source']}
                           for k in ['events','actions','playback','catalog','business']},
           'evidence_index':index}
write('archive-complete.json',archive)
partial = copy.deepcopy(archive)
partial.update(id='archive_partial',archive_id='archive_partial',end_reason='legacy_snapshot_only')
partial['actions'][0].update(status='accepted',started_at=None,completed_at=None,attempt_refs=[],result_evidence_refs=[])
partial['playback_attempts'] = [partial['playback_attempts'][0]]
partial['playback_attempts'][0]['started_at'] = None
partial['events'] = [copy.deepcopy(q1),copy.deepcopy(q2),copy.deepcopy(q3)]
partial['proposals'] = []
partial['evidence_index'] = {e['event_id']:{'collection':'events','index':i} for i,e in enumerate(partial['events'])}
partial['evidence_index'].update({'attempt_price':{'collection':'playback_attempts','index':0},
                                'action_size':{'collection':'actions','index':0}})
for k in ['events','actions','playback']:
    partial['completeness'][k] = {'status':'partial','coverage':None,
                                 'gaps':['older_events_missing','action_playback_confirmation_missing']}
write('archive-partial.json',partial)

gold = {**meta('EvaluatorOnlyExpectations','golden_demo'),
        'notice':'供测试判定器使用，不得作为Agent提示输入；不是已经运行Agent的结果。',
        'complete_archive': {'raw_questions':3,'deduplicated_topics':2,'answered_topics':1,
                             'coverage':0.5,'answer_wait_seconds_per_source_question':[10,9],
                             'mean_answer_wait_seconds':9.5,'completed_actions':1,'total_actions':1,
                             'must_cover_facts':3,'covered_facts':3,'business_metrics':None},
        'partial_archive': {'must_not_claim_answer_completed':True,
                            'mean_answer_wait_seconds':None,'business_metrics':None}}
write('golden-expectations.json',gold)
manifest = {**meta('ScenarioManifest','scenarios_demo'),
            'normal': {'catalog':'catalog.json','preparation':'preparation-bundle.json',
                       'snapshot':'live-state.json','questions':'questions.jsonl',
                       'archive':'archive-complete.json','expected':'golden-expectations.json'},
            'variants_to_implement': [
                {'case':'T12','change':'先增加control_revision并切到product_bag，再提交旧proposal_size','expected':'rejected_no_audio'},
                {'case':'T17','change':'同一action_size重复请求；另试同ID不同文本','expected':'reuse_or_payload_conflict'},
                {'case':'T18','change':'dispatching后回执丢失且无法确认','expected':'unknown_no_replay'},
                {'case':'T21','input':'archive-partial.json','expected':'no_claim_answer_completed'},
                {'case':'T28','change':'模型输出坏JSON或超时','expected':'bounded_repair_or_partial'}],
            'notice':'变体是待实施的场景定义，不表示故障测试已执行。question文件仅为全事件流的问题子集。'}
write('scenario-manifest.json',manifest)
print(json.dumps({'fixture_files':len(list(OUT.glob('*.json')))+len(list(OUT.glob('*.jsonl'))),
                  'output':str(OUT)},ensure_ascii=False))
