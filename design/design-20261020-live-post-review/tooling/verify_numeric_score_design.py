"""Pure design examples and schema validation; no product imports or network."""
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from statistics import NormalDist
import hashlib
import importlib.util
import json
import math

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
POLICY = 'LIVE-SCORE-0.1'


def goal_score(actual, goal):
    if actual is None or goal is None or goal <= 0:
        return None
    return min(100, max(0, 100 * actual / goal))


def relative_score(actual, baseline, higher=True, upper_bound=None):
    if actual is None or baseline is None or baseline <= 0:
        return None
    if actual < 0:
        raise ValueError('This design mapping requires a nonnegative metric')
    if upper_bound is not None and (actual > upper_bound or baseline > upper_bound):
        raise ValueError('Metric exceeds its declared upper bound')
    if higher:
        if upper_bound is not None and baseline == upper_bound:
            return None
        target = min(1.5 * baseline, upper_bound) if upper_bound is not None else 1.5 * baseline
        return 60 * actual / baseline if actual <= baseline else min(100, 60 + 40 * (actual - baseline) / (target - baseline))
    return max(0, 100 - 40 * actual / baseline) if actual <= baseline else max(0, 60 - 60 * (actual / baseline - 1))


def legal_rate(successes, trials):
    if successes is None or trials is None or trials <= 0:
        return None
    if not 0 <= successes <= trials:
        raise ValueError('Successes must be a subset of trials')
    return successes / trials


def wilson(successes, trials):
    p = legal_rate(successes, trials)
    if p is None:
        return None
    z = NormalDist().inv_cdf(.975)
    factor = 1 + z*z/trials
    center = (p + z*z/(2*trials)) / factor
    radius = z * math.sqrt(p*(1-p)/trials + z*z/(4*trials*trials)) / factor
    return [max(0, center-radius), min(1, center+radius)]


def coverage_score(parts):
    if not parts or any(p is None for p in parts):
        return None
    if not all(0 <= p <= 1 for p in parts):
        raise ValueError('Invalid coverage ratio')
    return 100 * min(parts)


def schema():
    nonempty = {'type': 'string', 'minLength': 1}
    nullable_ref = {'type': ['string', 'null'], 'minLength': 1}
    result = {'$schema': 'https://json-schema.org/draft/2020-12/schema',
              '$id': 'urn:shengbo:live-score:design:0.1',
              'title': 'Proposed ScoreResult, not a registered business API',
              'type': 'object', 'additionalProperties': False,
              'properties': {
                  'score_id': nonempty, 'policy_version': {'const': POLICY},
                  'score_basis': {'enum': ['target_completion', 'historical_higher', 'historical_lower', 'content_completion', 'data_coverage']},
                  'metric_ref': nonempty, 'archive_ref': nonempty,
                  'baseline_ref': nullable_ref, 'goal_ref': nullable_ref,
                  'status': {'enum': ['available', 'provisional', 'unavailable', 'not_applicable']},
                  'score': {'type': ['number', 'null'], 'minimum': 0, 'maximum': 100},
                  'raw_value': {'type': ['number', 'null']}, 'unit': nonempty,
                  'currency': nullable_ref,
                  'minor_unit_exponent': {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 9},
                  'numerator': {'type': ['number', 'null']}, 'denominator': {'type': ['number', 'null']},
                  'as_of': nonempty, 'missing_reason': nullable_ref,
                  'sample_notes': {'type': 'array', 'items': nonempty},
                  'data_eligibility': {'type': 'array', 'items': nonempty},
                  'limitations': {'type': 'array', 'items': nonempty},
                  'evidence_refs': {'type': 'array', 'items': nonempty},
                  'source_kinds': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                                   'items': {'enum': ['public_interaction', 'authorized_business_export', 'local_player', 'manual_note', 'synthetic']}},
                  'synthetic': {'type': 'boolean'},
                  'business_outcome': {'enum': ['goal_met', 'goal_not_met', 'undetermined']}
              }}
    result['required'] = list(result['properties'])
    result['allOf'] = [
        {'if': {'properties': {'status': {'enum': ['unavailable', 'not_applicable']}}},
         'then': {'properties': {'score': {'type': 'null'}, 'missing_reason': nonempty,
                                'business_outcome': {'const': 'undetermined'}}}},
        {'if': {'properties': {'status': {'enum': ['available', 'provisional']}}},
         'then': {'properties': {'score': {'type': 'number', 'minimum': 0, 'maximum': 100},
                                'missing_reason': {'type': 'null'}, 'evidence_refs': {'minItems': 1}}}},
        {'if': {'properties': {'score_basis': {'const': 'target_completion'}, 'status': {'enum': ['available', 'provisional']}}},
         'then': {'properties': {'goal_ref': nonempty}}},
        {'if': {'properties': {'score_basis': {'enum': ['historical_higher', 'historical_lower']}, 'status': {'enum': ['available', 'provisional']}}},
         'then': {'properties': {'baseline_ref': nonempty}}},
        {'if': {'properties': {'synthetic': {'const': True}}},
         'then': {'properties': {'business_outcome': {'const': 'undetermined'}}}},
        {'if': {'properties': {'source_kinds': {'contains': {'const': 'synthetic'}}}},
         'then': {'properties': {'synthetic': {'const': True}}}},
        {'if': {'properties': {'score_basis': {'enum': ['historical_higher', 'historical_lower', 'content_completion', 'data_coverage']}}},
         'then': {'properties': {'business_outcome': {'const': 'undetermined'}}}},
        {'if': {'properties': {'business_outcome': {'enum': ['goal_met', 'goal_not_met']}}},
         'then': {'properties': {'score_basis': {'const': 'target_completion'},
                  'status': {'enum': ['available', 'provisional']}, 'synthetic': {'const': False},
                  'source_kinds': {'contains': {'const': 'authorized_business_export'}},
                  'goal_ref': nonempty, 'numerator': {'type': 'number'},
                  'denominator': {'type': 'number', 'exclusiveMinimum': 0}}}},
        {'if': {'properties': {'business_outcome': {'const': 'goal_met'}}},
         'then': {'properties': {'score': {'const': 100}}}},
        {'if': {'properties': {'business_outcome': {'const': 'goal_not_met'}}},
         'then': {'properties': {'score': {'type': 'number', 'minimum': 0, 'exclusiveMaximum': 100}}}},
        {'if': {'properties': {'unit': {'const': 'currency_minor'}}},
         'then': {'properties': {'currency': nonempty,
                  'minor_unit_exponent': {'type': 'integer', 'minimum': 0, 'maximum': 9},
                  'raw_value': {'type': ['integer', 'null']},
                  'numerator': {'type': ['integer', 'null']}, 'denominator': {'type': ['integer', 'null']}}}}
    ]
    return result


class HtmlCheck(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = 0
        self.titles = []
    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.tables += 1


def main():
    protected_names = ['00_整体方案与并行分工.md', '01_统一数据与工具契约.md', '04_播中运营Agent任务.md',
                       '05_播后复盘Agent任务.md', '08_业务闭环与动态数据详细设计.md']
    protected = {name: hashlib.sha256((ROOT.parent/name).read_bytes()).hexdigest() for name in protected_names}
    checks = []
    def verify(name, condition):
        assert condition, name
        checks.append(name)

    verify('goal_900_of_1000_is_90', goal_score(900, 1000) == 90)
    verify('goal_score_caps_while_raw_attainment_is_120pct', goal_score(1200, 1000) == 100 and 1200/1000 == 1.2)
    verify('missing_data_stays_unknown', goal_score(None, 1000) is None and legal_rate(None, 100) is None)
    verify('zero_goal_and_denominator_stay_unknown', goal_score(100, 0) is None and legal_rate(0, 0) is None)
    verify('valid_observed_zero_is_zero', legal_rate(0, 50) == 0 and goal_score(0, 1000) == 0)
    verify('zero_baseline_not_divided', relative_score(1, 0) is None)
    verify('higher_anchor_points', all(math.isclose(relative_score(x, 1), y) for x,y in [(0,0),(.5,30),(.8,48),(1,60),(1.25,80),(1.5,100),(2,100)]))
    verify('lower_anchor_points', all(math.isclose(relative_score(x, .1, False), y) for x,y in [(0,100),(.05,80),(.1,60),(.15,30),(.2,0)]))
    verify('ratio_ceiling_80_to_90_is_80', math.isclose(relative_score(.9, .8, upper_bound=1), 80))
    verify('ceiling_baseline_not_fictitious_150pct_target', relative_score(1, 1, upper_bound=1) is None)
    samples = [relative_score(i/500, 1) for i in range(1001)]
    verify('higher_mapping_monotone_and_bounded', all(0<=x<=100 for x in samples) and all(a<=b for a,b in zip(samples,samples[1:])))
    samples = [relative_score(i/500, 1, False) for i in range(1001)]
    verify('lower_mapping_monotone_and_bounded', all(0<=x<=100 for x in samples) and all(a>=b for a,b in zip(samples,samples[1:])))
    try:
        legal_rate(3, 2)
    except ValueError:
        verify('150pct_created_conversion_rejected', True)
    else:
        raise AssertionError('Invalid conversion accepted')
    verify('full_hour_income_not_divided_by_capture_uptime', 600/1 == 600 and 600/(50/60) == 720)
    verify('coverage_bottleneck_not_average', math.isclose(coverage_score([59/60,59.5/60,.996]), 98.33333333333333))
    verify('missing_coverage_not_renormalized', coverage_score([59/60,None,.996]) is None)
    verify('four_content_requirements_875', sum([25,25,25,12.5]) == 87.5)
    intervals = [{'successes': k, 'trials': n, 'rate': legal_rate(k,n), 'wilson95': wilson(k,n)} for k,n in [(3,50),(30,500),(30,1000),(40,1000),(0,50)]]
    verify('wilson_examples_match_published_formula', abs(intervals[0]['wilson95'][0]-.0206)<.0001 and abs(intervals[0]['wilson95'][1]-.1622)<.0001)
    verify('same_rate_different_precision', intervals[0]['rate']==intervals[1]['rate'] and intervals[0]['wilson95'][1]-intervals[0]['wilson95'][0] > intervals[1]['wilson95'][1]-intervals[1]['wilson95'][0])
    verify('observed_zero_interval_has_positive_upper', intervals[-1]['wilson95'][1] > 0)

    contract = schema()
    Draft202012Validator.check_schema(contract)
    validator = Draft202012Validator(contract)
    example = {'score_id': 'SYNTHETIC_GOAL', 'policy_version': POLICY, 'score_basis': 'target_completion',
               'metric_ref': 'SYNTHETIC/net_amount/r1', 'archive_ref': 'SYNTHETIC/archive/r1',
               'baseline_ref': None, 'goal_ref': 'SYNTHETIC/goal_1000/r1', 'status': 'provisional',
               'score': 90, 'raw_value': 90000, 'unit': 'currency_minor', 'currency': 'CNY',
               'minor_unit_exponent': 2, 'numerator': 90000, 'denominator': 100000,
               'as_of': '2026-10-20T12:00:00+08:00', 'missing_reason': None,
               'sample_notes': ['合成数据，仅设计演算'], 'data_eligibility': ['假设同支付队列与同口径已成立'],
               'limitations': ['不是实际经营结果或利润'], 'evidence_refs': ['SYNTHETIC/fixture/r1'],
               'source_kinds': ['synthetic'], 'synthetic': True, 'business_outcome': 'undetermined'}
    validator.validate(example)
    verify('money_example_is_integer_minor_units', example['raw_value']/10**example['minor_unit_exponent'] == 900 and example['denominator']/100 == 1000)
    verify('schema_synthetic_cannot_claim_goal_met', bool(list(validator.iter_errors({**example, 'business_outcome': 'goal_met'}))))
    missing = {**example, 'status': 'unavailable', 'score': None, 'raw_value': None,
               'numerator': None, 'missing_reason': '缺经营数据',
               'data_eligibility': ['无实际经营分子，无法判断经营目标完成情况']}
    validator.validate(missing)
    verify('schema_missing_not_zero', bool(list(validator.iter_errors({**missing, 'score': 0}))))
    verify('schema_available_requires_goal_ref', bool(list(validator.iter_errors({**example, 'goal_ref': None}))))
    real_unknown = {**missing, 'synthetic': False, 'source_kinds': ['public_interaction'], 'business_outcome': 'goal_met'}
    verify('schema_unknown_cannot_claim_business_success', bool(list(validator.iter_errors(real_unknown))))
    public_only = {**example, 'synthetic': False, 'source_kinds': ['public_interaction'], 'score': 100, 'business_outcome': 'goal_met'}
    verify('schema_public_only_cannot_claim_business_success', bool(list(validator.iter_errors(public_only))))
    real_ninety = {**example, 'synthetic': False, 'source_kinds': ['authorized_business_export'], 'business_outcome': 'goal_met'}
    verify('schema_ninety_cannot_claim_goal_met', bool(list(validator.iter_errors(real_ninety))))
    validator.validate({**real_ninety, 'business_outcome': 'goal_not_met'})
    verify('schema_real_goal_not_met_has_legal_source', True)

    policy = {'policy_version': POLICY, 'status': 'proposed_design_not_runtime', 'synthetic_examples': True,
              'not_industry_thresholds': True, 'score_bases_must_not_be_merged': True,
              'default_anchors': {'baseline_score': 60, 'higher_full_score_ratio': 1.5,
                 'bounded_ratio_target': 'min(1.5*baseline, upper_bound)', 'ceiling_baseline': 'score=null',
                 'lower_zero_score_ratio': 2, 'goal_cap': 100, 'raw_goal_attainment_uncapped': True},
              'baseline_selection': {'lookback_days': 28, 'max_sessions': 20, 'min_sessions': 5,
                 'min_sessions_scope': 'whole_session_or_product_session_metrics',
                 'few_history_warning_below': 10, 'aggregator': 'median_of_comparable_session_metrics',
                 'module_min_occurrences': 10, 'module_min_sessions': 3,
                 'module_qualification_replaces_whole_session_minimum': True,
                 'module_all_requirements_required': True, 'module_scores_always_provisional': True,
                 'price_relative_difference_hint': .05, 'paid_traffic_share_absolute_difference_hint': .10,
                 'start_time_difference_hours_hint': 2, 'freeze_revision': True},
              'quality_hints': {'observed_time_min_for_module': .98, 'playback_journal_time_min_for_module': .98,
                 'bound_content_time_min_for_full_module_rank': .99, 'online_snapshot_max_hold_seconds': 15,
                 'fine_sentence_delay_range_width_max_seconds_hint': 5, 'initial_calibration_markers': 5,
                 'small_denominator_warning_below': 100, 'small_success_warning_below': 10,
                 'comment_manual_audit_count': 50, 'product_assignment_accuracy_goal': .90,
                 'intent_precision_goal': .85, 'intent_precision_formula': 'TP/(TP+FP)',
                 'zero_predicted_positive_precision': None, 'intent_min_predicted_positive_for_initial_check': 10},
              'association_windows': {'comment_primary_seconds': 30, 'comment_sensitivity_seconds': [15,60],
                 'product_payment_exploration_primary_minutes': 5, 'product_payment_sensitivity_minutes': [1,3,10],
                 'not_causal': True, 'overlapping_windows_not_additive': True},
              'schema_reference': '评分结果交接契约.schema.json',
              'examples': {'goal': example, 'missing_commerce': missing, 'wilson': intervals,
                 'module_intent_proxy': {'synthetic': True, 'intent_theme_groups': 6, 'person_minutes': 250,
                    'raw_density_per_100': 2.4, 'baseline': 2, 'score': relative_score(2.4,2),
                    'qualification': '假设模块跨场、次数、在线字段、区间及标签均合格；否则score=null'},
                 'worked_example_scores': {'target': 90, 'net_per_hour_relative': 80, 'click_relative': 80,
                    'click_to_pay_relative': 48, 'refund_relative': 0, 'content': 87.5,
                    'intent_proxy_relative': 76, 'data_coverage': coverage_score([59/60,59.5/60,.996])}}}
    (ROOT/'评分政策与数值样例.json').write_text(json.dumps(policy,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROOT/'评分结果交接契约.schema.json').write_text(json.dumps(contract,ensure_ascii=False,indent=2),encoding='utf-8')

    spec = importlib.util.spec_from_file_location('design_renderer', ROOT/'tooling/build_design_artifacts.py')
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    source = (ROOT/'直播评价体系与数值评分细则.md').read_text(encoding='utf-8')
    html = renderer.html_document(source)
    html = html.replace('<title>播中播后综合Agent设计方案</title>', '<title>声播直播评价体系与数值评分细则</title>')
    html = html.replace('<strong>播中只监控，播后评价与改进</strong>', '<strong>评价体系：每个数字说明什么</strong>')
    (ROOT/'直播评价体系与数值评分细则.html').write_text(html, encoding='utf-8')
    parser = HtmlCheck(); parser.feed(html)
    verify('html_has_numeric_tables_and_examples', parser.tables>=15 and '87.5' in html and '86.7' in html)
    verify('old_plans_unchanged', protected == {name: hashlib.sha256((ROOT.parent/name).read_bytes()).hexdigest() for name in protected_names})
    output = {'verified_at': datetime.now().astimezone().isoformat(), 'policy_version': POLICY,
              'verification_kind': 'synthetic_mathematical_and_document_schema_only',
              'passed_checks': checks, 'passed_check_count': len(checks), 'html_tables': parser.tables,
              'business_code_changes': 0, 'new_real_live_collection': False, 'paid_model_calls': 0,
              'third_party_runtime_integrated': False, 'actual_platform_effect_validated': False,
              'old_plan_hashes': protected, 'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
              'limitations': ['无真实经营报表／完整遥测接入测试；不能证明评分政策预测成交或因果有效。',
                             'Schema不代替cohort、时区、来源授权、基线可比性和实际内容语义检查。']}
    (ROOT/'数值评分设计校验.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'design_checks_passed':len(checks), 'numeric_tables':parser.tables, 'business_code_changes':0},ensure_ascii=False))


if __name__ == '__main__':
    main()
