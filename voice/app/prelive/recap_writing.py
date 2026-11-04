"""Bind authorized recap findings to the exact writing targets in a plan."""
from shengbo_contracts.common import same_artifact_ref

from .storage import BusinessError


def bind_recap_writing(context, items, dispositions):
    findings = [(record['ref'], finding) for record in context.get('records', [])
                if record['ref']['kind'] == 'recap'
                for finding in record['body']['document'].get('findings', [])]
    if not findings and not dispositions:
        return None

    seen, guidance = set(), []
    changed = {item['variant_id']: (item['product_ref']['id'],
        {module['key'] for module in item['modules'] if module['action'] in {'generate', 'patch'}})
        for item in items}
    for decision in dispositions:
        finding_id = decision['finding_id']
        reference = decision.get('recap_ref')
        matches = [index for index, (ref, finding) in enumerate(findings)
                   if (finding.get('finding_id') or finding.get('id')) == finding_id
                   and (reference is None or same_artifact_ref(reference, ref))]
        if len(matches) != 1 or matches[0] in seen:
            raise BusinessError('RECAP_DISPOSITION_INVALID', '每条复盘意见须有唯一的处置，请核对复盘引用和意见编号。',
                status=422, details={'commit_state': 'not_committed', 'finding_id': finding_id})
        index = matches[0]
        seen.add(index)
        if decision['action'] not in {'apply', 'partial'}:
            continue

        modules = set(decision['target_modules'])
        products = set(decision['target_product_ids'])
        scoped = {variant: (product, modules & active) for variant, (product, active) in changed.items()
                  if not products or product in products}
        covered_modules = set().union(*(active for _, active in scoped.values()))
        missing_product_modules = any(set().union(*(active for owner, active in scoped.values() if owner == product)) != modules
                                      for product in products)
        if not modules or modules != covered_modules or missing_product_modules:
            raise BusinessError('RECAP_DISPOSITION_TARGET_INVALID', '已采用的复盘意见须指向本次确实改动的商品和话术模块。',
                status=422, details={'commit_state': 'not_committed', 'finding_id': finding_id})
        ref, finding = findings[index]
        for variant, (_, active) in scoped.items():
            if not active:
                continue
            row = {'recap_ref': ref, 'finding_id': finding_id, 'action': decision['action'],
                   'variant_id': variant, 'target_modules': sorted(active), 'reason': decision['reason']}
            if decision['action'] == 'apply' and isinstance(finding.get('suggestion'), str):
                row['suggestion'] = finding['suggestion']
            guidance.append(row)
    missing = [(ref, finding.get('finding_id') or finding.get('id')) for index, (ref, finding) in enumerate(findings)
               if index not in seen]
    if missing:
        raise BusinessError('RECAP_DISPOSITION_MISSING', '请为绑定复盘里的每条意见记录是否采用。', status=422,
            details={'commit_state': 'not_committed', 'missing': [{'recap_ref': ref, 'finding_id': key}
                for ref, key in missing]})
    return guidance


def project_recap_writing(context, variant_id, module_keys):
    if 'recap_writing_guidance' not in context:
        return context
    guidance = []
    for row in context['recap_writing_guidance']:
        active = set(row['target_modules']) & module_keys
        if row['variant_id'] == variant_id and active:
            guidance.append({**{key: value for key, value in row.items() if key != 'variant_id'},
                             'target_modules': sorted(active)})
    return {**context, 'records': [record for record in context['records'] if record['ref']['kind'] != 'recap'],
            'recap_writing_guidance': guidance,
            'recap_writing_use': '只按target_modules落实已采用的建议。部分采用以reason里的范围为准。复盘只指导话术表达，商品、价格、活动及履约条件仍以本场已确认事实为准。'}
