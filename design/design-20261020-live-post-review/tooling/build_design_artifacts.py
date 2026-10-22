"""Render and validate design documents only. No runtime, network or model calls."""
from datetime import datetime
from html import escape
from pathlib import Path
import hashlib
import json
import re

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / '播中播后综合Agent设计方案.md'
VERSION = 'LIVE-REVIEW-1.0'


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


def enum(*values):
    return {'enum': list(values)}


def ref(name):
    return {'$ref': '#/$defs/' + name}


def arr(item):
    return {'type': 'array', 'items': item}


TEXT = {'type': 'string', 'minLength': 1}
NULL_TEXT = {'type': ['string', 'null']}
NUMBER = {'type': ['number', 'null']}
NONNEG = {'type': 'number', 'minimum': 0}
TIME = {'type': 'string', 'format': 'date-time'}
NULL_TIME = {'type': ['string', 'null'], 'format': 'date-time'}
DIMENSIONS = {'type': 'object', 'additionalProperties': {'type': ['string', 'integer', 'null']}}


def contract():
    defs = {}
    defs['ArtifactRef'] = obj({'kind': TEXT, 'id': TEXT, 'revision': {'type': 'integer', 'minimum': 1},
        'content_hash': {'type': 'string', 'pattern': '^sha256:[a-f0-9]{64}$'}, 'schema_version': TEXT},
        ('kind', 'id', 'revision', 'content_hash'))
    defs['SourceKind'] = enum('public_platform', 'local_player', 'official_merchant_export', 'manual_note', 'synthetic')
    defs['Clock'] = obj({'occurred_at': NULL_TIME, 'received_at': TIME, 'source_precision': enum('millisecond', 'second', 'minute', 'unknown'),
        'clock_domain': TEXT, 'source_instance': TEXT, 'boot_id': TEXT, 'monotonic_ns': {'type': ['integer', 'null'], 'minimum': 0},
        'estimated_offset_ms': NUMBER, 'uncertainty_ms': {'type': ['number', 'null'], 'minimum': 0}},
        ('occurred_at', 'received_at', 'source_precision', 'clock_domain', 'source_instance', 'boot_id', 'uncertainty_ms'))
    defs['ObservationEvent'] = obj({'schema_version': {'const': VERSION}, 'event_id': TEXT, 'monitor_session_id': TEXT,
        'merchant_id': TEXT, 'source_kind': ref('SourceKind'), 'source_message_id': NULL_TEXT,
        'source_cursor': NULL_TEXT, 'local_seq': {'type': 'integer', 'minimum': 1}, 'event_type': TEXT,
        'clock': ref('Clock'), 'definition_status': enum('verified', 'unverified', 'not_applicable'),
        'raw_ref': ref('ArtifactRef'), 'payload': {'type': 'object'}, 'parser_revision': TEXT},
        ('schema_version', 'event_id', 'monitor_session_id', 'merchant_id', 'source_kind', 'source_message_id',
         'local_seq', 'event_type', 'clock', 'definition_status', 'raw_ref', 'payload', 'parser_revision'))
    defs['PlaybackOccurrence'] = obj({'monitor_session_id': TEXT, 'item_occurrence_id': TEXT,
        'module_occurrence_id': TEXT, 'playback_attempt_id': TEXT, 'compiled_entry_id': TEXT,
        'source_sentence_id': TEXT, 'playback_sentence_id': TEXT, 'variant_id': TEXT, 'module_key': TEXT,
        'original_position': {'type': 'integer', 'minimum': 1}, 'cycle_index': {'type': 'integer', 'minimum': 0},
        'segment_index': {'type': 'integer', 'minimum': 0}, 'owner_id': TEXT, 'epoch': {'type': 'integer', 'minimum': 0},
        'product_ref': ref('ArtifactRef'), 'script_ref': ref('ArtifactRef'), 'module_ref': ref('ArtifactRef'),
        'publication_ref': ref('ArtifactRef'), 'approval_ref': ref('ArtifactRef'),
        'clock': ref('Clock'), 'status': enum('created', 'playing', 'paused', 'buffering', 'ended', 'interrupted', 'failed', 'unknown'),
        'evidence_level': enum('server_scheduled', 'player_acknowledged', 'local_audio_observed', 'platform_recording_verified'),
        'opening_mode': enum('long', 'short', 'merchant_original', 'not_applicable'), 'raw_event_ref': ref('ArtifactRef')},
        ('monitor_session_id', 'item_occurrence_id', 'module_occurrence_id', 'playback_attempt_id', 'compiled_entry_id',
         'source_sentence_id', 'playback_sentence_id', 'variant_id', 'module_key', 'original_position', 'cycle_index',
         'segment_index', 'owner_id', 'epoch', 'product_ref', 'script_ref', 'module_ref', 'publication_ref', 'approval_ref',
         'clock', 'status', 'evidence_level', 'opening_mode', 'raw_event_ref'))
    defs['MetricResult'] = obj({'metric_id': TEXT, 'definition_revision': TEXT, 'archive_ref': ref('ArtifactRef'),
        'dimensions': DIMENSIONS, 'status': enum('available', 'partial', 'unavailable', 'unverified'),
        'unit': TEXT, 'value': NUMBER, 'numerator': NUMBER, 'denominator': NUMBER, 'missing_reason': NULL_TEXT,
        'as_of': TIME, 'cohort_ref': {'oneOf': [ref('ArtifactRef'), {'type': 'null'}]},
        'coverage_status': enum('full', 'partial', 'unknown'), 'sample_size': {'type': ['integer', 'null'], 'minimum': 0},
        'denominator_basis': enum('official_metric', 'verified_cohort', 'observed_participants', 'time_integral_proxy', 'matching_time_window', 'not_applicable', 'unknown'),
        'amount_basis': NULL_TEXT, 'currency': NULL_TEXT, 'formula_text': TEXT, 'source_refs': arr(ref('ArtifactRef')),
        'limitations': arr(TEXT)},
        ('metric_id', 'definition_revision', 'archive_ref', 'dimensions', 'status', 'unit', 'value', 'numerator',
         'denominator', 'missing_reason', 'as_of', 'cohort_ref', 'coverage_status', 'sample_size', 'denominator_basis',
         'formula_text', 'source_refs', 'limitations'))
    defs['MetricResult']['allOf'] = [
        {'if': {'properties': {'status': {'enum': ['unavailable', 'unverified']}}},
         'then': {'properties': {'value': {'type': 'null'}, 'missing_reason': TEXT}}},
        {'if': {'properties': {'unit': {'const': 'ratio'}, 'status': {'enum': ['available', 'partial']}}},
         'then': {'properties': {'denominator': {'type': 'number', 'exclusiveMinimum': 0},
                               'numerator': NONNEG, 'value': {'type': 'number', 'minimum': 0, 'maximum': 1}}}}
    ]
    defs['Finding'] = obj({'finding_id': TEXT, 'observation': TEXT, 'hypothesis': NULL_TEXT,
        'claim_strength': enum('observed', 'time_association', 'hypothesis', 'insufficient_evidence'),
        'metric_refs': arr(ref('ArtifactRef')), 'evidence_refs': arr(ref('ArtifactRef')),
        'alternative_explanations': arr(TEXT), 'limitations': arr(TEXT)},
        ('finding_id', 'observation', 'hypothesis', 'claim_strength', 'metric_refs', 'evidence_refs', 'alternative_explanations', 'limitations'))
    defs['Recommendation'] = obj({'recommendation_id': TEXT, 'finding_ids': arr(TEXT),
        'type': enum('script_revision', 'faq_add', 'material_request', 'display_material_request',
                     'lineup_suggestion', 'verification_request', 'technical_fix', 'experiment'),
        'target_ref': {'oneOf': [ref('ArtifactRef'), {'type': 'null'}]}, 'target_module_keys': arr(TEXT),
        'old_issue': TEXT, 'proposed_change': TEXT, 'evidence_refs': arr(ref('ArtifactRef')),
        'expected_metric_ids': arr(TEXT), 'applicability': arr(TEXT), 'validation_plan': TEXT,
        'priority': enum('high', 'medium', 'low'), 'adoption_state': {'const': 'pending'}},
        ('recommendation_id', 'finding_ids', 'type', 'target_ref', 'target_module_keys', 'old_issue',
         'proposed_change', 'evidence_refs', 'expected_metric_ids', 'applicability', 'validation_plan', 'priority', 'adoption_state'))
    defs['ReviewBundle'] = obj({'schema_version': {'const': VERSION}, 'review_id': TEXT, 'archive_ref': ref('ArtifactRef'),
        'metric_panel_ref': ref('ArtifactRef'), 'monitor_session_id': TEXT, 'merchant_id': TEXT,
        'data_mode': enum('interaction_only', 'business_aggregate', 'time_association', 'mature_business'),
        'data_origins': {**arr(ref('SourceKind')), 'description': '参与本报告统计或结论的来源；被排除的模拟来源只列excluded_source_refs。'},
        'excluded_source_refs': arr(ref('ArtifactRef')),
        'review_status': enum('complete', 'partial', 'needs_data'),
        'business_outcome': enum('goal_met', 'goal_not_met', 'undetermined'),
        'maturity': enum('initial', 'as_of', 'matured'), 'as_of': TIME, 'goal_definition': NULL_TEXT,
        'findings': arr(ref('Finding')), 'recommendations': arr(ref('Recommendation')),
        'limitations': arr(TEXT), 'trace_ref': ref('ArtifactRef')},
        ('schema_version', 'review_id', 'archive_ref', 'metric_panel_ref', 'monitor_session_id', 'merchant_id',
         'data_mode', 'data_origins', 'excluded_source_refs', 'review_status', 'business_outcome', 'maturity',
         'as_of', 'goal_definition', 'findings', 'recommendations', 'limitations', 'trace_ref'))
    defs['ReviewBundle']['allOf'] = [
        {'if': {'properties': {'data_mode': {'const': 'interaction_only'}}},
         'then': {'properties': {'business_outcome': {'const': 'undetermined'}}}},
        {'if': {'properties': {'data_origins': {'contains': {'const': 'synthetic'}}}},
         'then': {'properties': {'business_outcome': {'const': 'undetermined'}}}}
    ]
    defs['NextSessionFeedback'] = obj({'schema_version': {'const': VERSION}, 'report_ref': ref('ArtifactRef'),
        'archive_ref': ref('ArtifactRef'), 'recommendations': arr(ref('Recommendation')),
        'state': {'const': 'candidate'}, 'requires_user_selection': {'const': True},
        'business_side_effects_authorized': {'const': False}},
        ('schema_version', 'report_ref', 'archive_ref', 'recommendations', 'state',
         'requires_user_selection', 'business_side_effects_authorized'))
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$id': 'urn:shengbo:live-review:design:v1', 'title': 'LIVE-REVIEW-1.0 proposed contracts',
        'description': '设计契约，未注册为现有API。结构校验不代替来源、cohort、归属、授权或数值复算。',
        '$defs': defs, 'oneOf': [ref(name) for name in
            ('ObservationEvent', 'PlaybackOccurrence', 'MetricResult', 'ReviewBundle', 'NextSessionFeedback')]}


def metrics():
    rows = [
        ('observed_time_ratio', '监控覆盖时间比例', 'ratio', '有可靠健康证据的观测秒数/本场目标观测秒数', '本场目标区间已知', '不等于所有事件召回率'),
        ('confirmed_play_seconds', '确认播放区间时长', 'seconds', '同一时钟域下连续playing区间的并集秒数', '结构化attempt/segment回执', '网页回执不证明平台观众听到'),
        ('observed_member_messages', '观测成员／进场消息', 'messages', '去重后该方法消息数', '本场归属及消息去重', '不等于全量进入UV'),
        ('like_messages', '观测点赞消息', 'messages', '去重后点赞消息数', '本场归属', '18条消息不等于18个赞'),
        ('verified_like_increments', '已核口径点赞增量', 'count', '按已核增量／累计分段规则汇总', '字段语义、重置和窗口核验', '未核口径时null'),
        ('online_time_weighted_mean', '观测在线时间加权均值', 'persons', '可信快照值按有效观测区间积分/有效区间秒数', 'online口径已核、缺口不外推', '当前来源verified=false时不可作确定人数'),
        ('observed_online_person_minutes', '观测在线人分钟代理', 'person_minutes', '有效可信在线快照积分/60', '口径已核与采样区间限制', '不是商品真实曝光或完整留存'),
        ('official_average_watch_seconds', '官方人均观看时长', 'seconds', '保留同场官方原报表值及其定义', '授权报表与场次口径', '不能从公开消息自行还原'),
        ('relevant_question_count', '相关非垃圾问题数', 'messages', '按版本化范围／去重规则对合格问题计数', '可回读的分类标签及排除映射', '数字依分类判断，需显示歧义/排除量'),
        ('intent_observed_users', '表达购买意向的观测用户', 'persons', '有购买意向标签的可识别会话化用户去重数', '可识别用户键/标签，未知用户另列', '自述购买不等于支付'),
        ('questions_per_100_observed_person_minutes', '每百观测在线人分钟问题数', 'per_100_person_minutes', '相关非垃圾问题数/匹配观测人分钟*100', '同区间口径和完整分母', '过程代理，不是商品转化率或满意度'),
        ('product_click_uv_rate', '商品曝光到点击率', 'ratio', '同场同商品同口径点击UV/曝光UV，或平台原生率', '授权商品报表有同口径分子分母', '整场汇总不能拆到短模块'),
        ('viewer_to_buyer_rate', '看播到支付率', 'ratio', '同一看播队列内支付买家/看播队列，或同定义官方率', '可靠用户队列或官方定义', '不能把独立聚合UV或成员消息强行求交'),
        ('clicker_to_buyer_rate', '点击到支付率', 'ratio', '同一点击队列内支付买家/点击队列，或同定义官方率', '同商品同cohort及截止时点', '缺少跨域匹配则只列各自计数'),
        ('created_cohort_payment_rate', '同创建队列支付完成率', 'ratio', 'C内截至as_of已支付订单/C；C为固定创建区间的同粒度本场订单', '创建、支付、粒度、归属、截止齐全', '本分钟3单支付/2单创建不能算150%'),
        ('paid_order_count', '支付订单数量', 'orders', '本场已归属支付队列按order_grain指定父或SKU子单去重', '稳定业务键/订单粒度/支付状态映射', '父子订单不相加'),
        ('paid_amount_minor', '支付金额', 'currency_minor', '固定队列每订单同amount_basis的实际支付额之和', '币种、分摊、优惠、运费口径明确', 'GMV不等于利润或到账'),
        ('successful_refund_minor', '原支付队列成功退款额', 'currency_minor', 'P内订单截至as_of已成功退款的同口径金额', '退款ID、原订单关联、状态、币种及as_of', '旧场订单今日退款不混入P'),
        ('refund_amount_rate', '原支付队列退款金额率', 'ratio', 'P内成功退款额/P原实付额', '同P、同amount_basis及非零分母', '退款申请不计成功退款'),
        ('net_paid_asof_minor', '截止日分析净成交', 'currency_minor', 'P原实付-截至as_of属于P的成功退款', '同P同口径，退款成熟度说明', '不是平台结算、到账或利润'),
        ('paid_amount_per_live_hour', '每直播小时支付金额', 'currency_minor_per_hour', '与分子完全相同场次／窗口的支付额/直播小时', '匹配收入和直播时间覆盖', '整场600元/60分钟不因漏采10分钟变成720元每小时'),
        ('net_amount_per_live_hour', '每直播小时净成交', 'currency_minor_per_hour', '同队列截止净额/对应直播小时', '同场范围及退款截止，窗口覆盖一致', '不能用采集在线时长替代整场直播时长'),
        ('paid_per_1000_viewers', '千次看播UV支付金额', 'currency_minor_per_1000persons', '同场归属支付额/同定义看播UV*1000', '真实看播UV及同场支付', '不用公开成员消息数充UV'),
    ]
    return {'schema_version': VERSION, 'status': 'proposed_metric_catalog_not_implemented',
        'formula_owner': 'deterministic metric service', 'platform_native_values_separate': True,
        'metrics': [{'metric_id': x[0], 'label': x[1], 'unit': x[2], 'formula': x[3],
                     'required_capability': x[4], 'interpretation_limit': x[5], 'definition_revision': '1'} for x in rows]}


def inline(value):
    text = escape(value)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    return text


def diagram():
    return '''<section class="diagram" aria-label="播中采集与播后复盘结构">
<div class="lane"><b>播中 · 普通采集程序</b><div class="nodes"><span>抖音公开事件</span><span>声播实际播放回执</span><span>连接健康与人工场记</span></div></div>
<div class="arrow">↓ 按本场绑定、原件和时间线归档</div>
<div class="lane"><b>关播后 · 数据基础</b><div class="nodes"><span>授权经营报表导入</span><span>固定归档修订</span><span>程序计算指标与数据资格</span></div></div>
<div class="arrow">↓ 主Agent按问题选择工具和证据</div>
<div class="lane"><b>播后 · Deep Agents 主复盘Agent</b><div class="nodes"><span>自主取证与诊断</span><span>可选内容专题</span><span>可选经营专题</span></div></div>
<div class="arrow">↓ 可读报告 ＋ 下一场建议候选</div>
<div class="lane"><b>用户选中建议 → 显式关联下一场 → 播前核版本后处理</b><p>没有任何自动插播、改稿、重排、改价或发券通路。</p></div></section>'''


def html_document(text):
    lines, out, toc = text.splitlines(), [], []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        heading = re.match(r'^(#{1,4}) (.+)$', line)
        if heading:
            level, label = len(heading[1]), heading[2]
            anchor = 's' + str(len(toc))
            toc.append((level, label, anchor))
            out.append(f'<h{level} id="{anchor}">{inline(label)}</h{level}>')
            index += 1
        elif line.startswith('```'):
            lang, fence = line[3:], []
            index += 1
            while index < len(lines) and not lines[index].startswith('```'):
                fence.append(lines[index]); index += 1
            index += 1
            out.append(diagram() if lang == 'mermaid' else '<pre><code>' + escape('\n'.join(fence)) + '</code></pre>')
        elif line.startswith('|') and index + 1 < len(lines) and re.match(r'^\|[\s:|-]+\|$', lines[index+1]):
            rows = []
            while index < len(lines) and lines[index].startswith('|'):
                if not re.match(r'^\|[\s:|-]+\|$', lines[index]):
                    rows.append([c.strip() for c in lines[index].strip().strip('|').split('|')])
                index += 1
            assert len({len(r) for r in rows}) == 1, rows[0]
            out.append('<div class="table-wrap"><table><thead><tr>' + ''.join('<th>'+inline(c)+'</th>' for c in rows[0]) + '</tr></thead><tbody>')
            out.extend('<tr>' + ''.join('<td>'+inline(c)+'</td>' for c in row) + '</tr>' for row in rows[1:])
            out.append('</tbody></table></div>')
        elif re.match(r'^(- |\d+\. )', line):
            ordered = bool(re.match(r'^\d+\. ', line)); tag = 'ol' if ordered else 'ul'
            out.append('<'+tag+'>')
            while index < len(lines) and re.match(r'^(\d+\. )' if ordered else r'^- ', lines[index]):
                out.append('<li>'+inline(re.sub(r'^(\d+\. |- )', '', lines[index]))+'</li>'); index += 1
            out.append('</'+tag+'>')
        else:
            paragraph = [line]; index += 1
            while index < len(lines) and lines[index].strip() and not re.match(r'^(#|\||```|- |\d+\. )', lines[index]):
                paragraph.append(lines[index]); index += 1
            out.append('<p>'+inline(' '.join(paragraph))+'</p>')
    nav = ''.join('<a href="#'+a+'">'+escape(label)+'</a>' for level, label, a in toc if level == 2)
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>播中播后综合Agent设计方案</title>
<style>html{scroll-behavior:smooth}body{margin:0;color:#233047;background:#f4f6fa;font:15px/1.75 "Microsoft YaHei",sans-serif}header{background:#e9eff8;padding:30px max(24px,calc((100vw - 1500px)/2));border-bottom:1px solid #d9e2f0}header strong{font-size:25px}header p{margin:8px 0 0}main{max-width:1500px;margin:auto;padding:24px 28px 60px}h1{font-size:29px}h2{margin-top:42px;font-size:23px}h3{margin-top:28px;font-size:19px}h1,h2,h3{scroll-margin-top:15px}p,li{max-width:1180px}nav{display:flex;flex-wrap:wrap;gap:8px 18px;padding:18px;background:white;border:1px solid #d9e2f0}a{color:#285fa5;text-decoration:none}a:hover{text-decoration:underline}.table-wrap{overflow:auto;margin:18px 0 25px;border:1px solid #d9e2f0;border-radius:8px;background:white}table{border-collapse:collapse;width:100%;min-width:880px;font-size:14px}td,th{padding:12px 14px;text-align:left;vertical-align:top;border-bottom:1px solid #e1e6ee;border-right:1px solid #e1e6ee;min-width:130px;max-width:480px;overflow-wrap:anywhere}th{background:#e8edf6}tr:last-child td{border-bottom:0}tr:nth-child(even){background:#f9fbfe}td:first-child{font-weight:600;min-width:125px}code{font:12px/1.65 Consolas,monospace;overflow-wrap:anywhere;background:#edf1f6;border-radius:3px;padding:2px 4px}pre{white-space:pre-wrap;background:white;padding:20px;border:1px solid #d9e2f0}.diagram{padding:20px;background:white;border:1px solid #ced9e9;border-radius:12px}.lane{padding:16px;background:#eef4fb;border:1px solid #cbd9ed;border-radius:8px;text-align:center}.nodes{display:flex;gap:15px;justify-content:center;flex-wrap:wrap;margin:14px 0 0}.nodes span{background:white;border:1px solid #bccfe5;padding:10px 15px;border-radius:6px}.arrow{text-align:center;color:#4d607a;margin:13px}.lane p{margin:auto;font-size:13px;color:#5c6b7f}footer{padding-top:25px;color:#69778b}@media print{body{background:white}nav{display:none}main{padding:0}.table-wrap{overflow:visible}table{min-width:0;font-size:10px}td,th{min-width:0;padding:6px;max-width:none}h2{break-after:avoid}tr{break-inside:avoid}}</style>
<header><strong>播中只监控，播后评价与改进</strong><p>标准路径：实时采集 ＋ 用户手动导出经营报表 ＋ 有证据的复盘建议。设计阶段，未启动新采集或修改业务代码。</p></header><main><nav>''' + nav + '</nav>' + ''.join(out) + '<footer>旧方案保留。公式和接口为本项目拟定设计，真实可得性和效果需未来实施验证。</footer></main></html>'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    protected = [ROOT.parent / name for name in ('00_整体方案与并行分工.md', '01_统一数据与工具契约.md',
                 '04_播中运营Agent任务.md', '05_播后复盘Agent任务.md', '08_业务闭环与动态数据详细设计.md')]
    before = {str(p): digest(p) for p in protected}
    text = MAIN.read_text(encoding='utf-8')
    schema = contract(); Draft202012Validator.check_schema(schema)
    unavailable = {'metric_id': 'viewer_to_buyer_rate', 'definition_revision': '1',
        'archive_ref': {'kind': 'session_archive', 'id': 'DESIGN_FIXTURE', 'revision': 1, 'content_hash': 'sha256:'+'0'*64},
        'dimensions': {'monitor_session_id': 'DESIGN_FIXTURE'}, 'status': 'unavailable', 'unit': 'ratio',
        'value': None, 'numerator': None, 'denominator': None, 'missing_reason': '设计反例：缺少同一观众队列与支付资料',
        'as_of': '2026-10-20T00:00:00Z', 'cohort_ref': None, 'coverage_status': 'unknown', 'sample_size': None,
        'denominator_basis': 'unknown', 'formula_text': '同一看播队列内的支付买家/该看播队列', 'source_refs': [], 'limitations': ['设计验证样例，不是真实报告']}
    validator = Draft202012Validator({**schema, '$ref': '#/$defs/MetricResult'}, format_checker=FormatChecker())
    validator.validate(unavailable)
    bad = {**unavailable, 'value': 0}
    assert list(validator.iter_errors(bad)), 'Missing commerce must not silently become zero'
    good = {**unavailable, 'status': 'available', 'value': .5, 'numerator': 1, 'denominator': 2,
            'missing_reason': None, 'coverage_status': 'full', 'denominator_basis': 'verified_cohort'}
    validator.validate(good)
    assert list(validator.iter_errors({**good, 'value': 1.5})), 'A conversion ratio cannot be 150%'
    for name, value in [('数据交接契约.schema.json', schema), ('指标口径与数据资格.json', metrics())]:
        (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    html = html_document(text)
    (ROOT / '播中播后综合Agent设计方案.html').write_text(html, encoding='utf-8')
    assert before == {str(p): digest(p) for p in protected}
    checks = ['用户已确认', 'after=0', '200条', '复盘', '退款', 'cohort', '人工',
              'Deep Agents', 'opening_long', 'transition', 'import_recap', 'segment_index', '订阅', '6.2.1']
    assert all(value in text for value in checks)
    flow_section = text.split('## 4 从开监控到交付报告的九步', 1)[1].split('## 5 要采集什么数据', 1)[0]
    assert len(re.findall(r'^\| [1-9] ', flow_section, flags=re.M)) == 9
    result = {'schema_version': 'live-review-design-verification.v1', 'verified_at': datetime.now().astimezone().isoformat(),
        'design_static_validation_passed': True, 'implementation_status': 'design_only',
        'live_collection_executed': False, 'business_code_modified': False, 'runtime_tests_executed': False,
        'new_model_calls_to_project_adapter': 0, 'new_tts_calls': 0,
        'schema_meta_validation': 'Draft202012Validator.check_schema passed',
        'contract_examples': {'unknown_ratio_keeps_null': True, 'unknown_as_zero_rejected': True,
                              'same_cohort_half_valid': True, 'ratio_over_one_rejected': True},
        'metric_definitions': len(metrics()['metrics']), 'workflow_steps': 9,
        'html_tables': html.count('<table>'), 'old_plan_hashes': before,
        'old_plans_unchanged_during_render': True, 'main_markdown_sha256': digest(MAIN),
        'limitations': ['这是文档与契约设计校验，不证明采集、指标服务或Agent已实现或通过实播。',
                       'cohort归属、金额复算、证据有效性和语义不能仅靠JSON Schema判定。']}
    (ROOT / '设计校验记录.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'design_artifacts_built', 'steps': 9, 'metrics': len(metrics()['metrics']),
                      'tables': html.count('<table>'), 'old_plans_unchanged': True, 'business_changes': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
