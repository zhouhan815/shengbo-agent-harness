"""Validate planning artifacts only; this does not test the Agent implementation."""
from pathlib import Path
import hashlib
import json
import re
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "engineering-v2"
FILES = [
    "00_后端工程设计.md", "01_类型与持久化.md", "02_运行时与中间件.md",
    "03_声播业务服务与事务.md", "04_业务执行与上下文.md",
    "05_实施顺序与验收.md", "06_前端对接合同.md",
    "08_架构图与中间件Skill挂载说明.md",
    "09_复盘输入与话术闭环设计.md", "10_人工确认与语音门槛设计.md",
    "11_原始运行记录与调试台设计.md",
    "12_用户编排与重排设计.md", "13_任务识别与历史上下文设计.md",
    "14_任务验收与素材准备设计.md", "15_可复用工具流与Agent协调边界.md",
    "16_最终复查修订与待讨论事项.md", "17_ChatGPT授权模型来源与后续接入记录.md",
]


def table_cells(line):
    line = re.sub(r"`+[^`]*`+", "CODE", line)
    line = line.replace(r"\|", "PIPE")
    return len(line.strip().strip("|").split("|"))


def main():
    errors, metrics = [], {}
    texts = {}
    for name in FILES:
        path = DOC / name
        if not path.is_file():
            errors.append(f"missing:{name}")
            continue
        text = path.read_text(encoding="utf-8")
        texts[name] = text
        lines = text.splitlines()
        in_code, current_table, table_count = False, None, 0
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("```"):
                in_code = not in_code
                current_table = None
                continue
            if in_code:
                continue
            if line.startswith("|"):
                count = table_cells(line)
                if current_table is None:
                    current_table = count
                    table_count += 1
                elif count != current_table:
                    errors.append(f"table-columns:{name}:{i}:{count}!={current_table}")
            else:
                current_table = None
        if in_code:
            errors.append(f"unclosed-fence:{name}")
        if "\ufffd" in text:
            errors.append(f"encoding-replacement:{name}")
        if re.search(r"\bsk-[A-Za-z0-9_-]{18,}", text):
            errors.append(f"credential-like-pattern:{name}")
        checked_links = 0
        link_text = re.sub(r"```[\s\S]*?```", "", text)
        for match in re.finditer(r"\[[^\]\n]+\]\((<[^>]+>|[^)]+)\)", link_text):
            target = unquote(match.group(1).strip("<>"))
            if target.startswith(("https://", "http://", "#", "codex://")):
                continue
            target = target.split("#", 1)[0]
            target = re.sub(r":\d+$", "", target)
            dest = Path(target) if re.match(r"^[A-Za-z]:[/\\]", target) else path.parent / target
            checked_links += 1
            if not dest.exists():
                errors.append(f"broken-link:{name}:{target}")
        metrics[name] = {"lines": len(lines), "bytes": path.stat().st_size,
                         "tables": table_count, "local_links": checked_links}

    runtime = texts.get(FILES[2], "")
    required_runtime = ["RunFenceMiddleware", "ChildFenceMiddleware", "ContextProjectionMiddleware",
                        "ToolPolicyMiddleware", "ActionBoundaryMiddleware", "CompletionGuardMiddleware",
                        "BranchCompletionGuardMiddleware", "ModelCallLimitMiddleware", "BudgetMiddleware",
                        "AuditMiddleware", "RemoteOutcomeUnknown", "PREPARED", "COMPLETED"]
    for name in required_runtime:
        if name not in runtime:
            errors.append(f"runtime-contract-missing:{name}")
    combined = "\n".join(texts.values())
    for name in ["revocation tombstone", "replace_with_expanded_scope", "compiled_entry_id",
                 "save_context_bundle", "save_script_plan", "accept_analysis_reports",
                 "adopt_snapshot", "start_prepared_room", "read_completion_observation"]:
        if name not in combined:
            errors.append(f"engineering-contract-missing:{name}")
    for old_name in ["DeterministicContextWindowMiddleware", "src/prelive_agent/"]:
        if old_name in combined:
            errors.append(f"deprecated-architecture-name:{old_name}")

    service_doc = texts.get(FILES[3], "")
    registry_block = re.search(r"COMMAND_HANDLERS = \{([\s\S]*?)\n\}", service_doc)
    command_keys = re.findall(r"^\s*'([^']+)'\s*:", registry_block.group(1), re.M) if registry_block else []
    query_section = service_doc.split("### 20.3", 1)[-1].split("\n## ", 1)[0]
    query_keys = re.findall(r"^\| ([a-z][a-z_]+) \|", query_section, re.M)
    if len(command_keys) != 30 or len(set(command_keys)) != 30:
        errors.append(f"command-registry-size:{len(command_keys)}/{len(set(command_keys))}")
    if len(query_keys) != 27 or len(set(query_keys)) != 27:
        errors.append(f"query-registry-size:{len(query_keys)}/{len(set(query_keys))}")
    index = json.loads((DOC / "设计索引.json").read_text(encoding="utf-8"))
    main_tools = index["main_business_tools"]
    if len(main_tools) != len(set(main_tools)):
        errors.append("duplicate-main-tool")
    if len(main_tools) != 39:
        errors.append(f"main-tool-count:{len(main_tools)}")
    for tool in main_tools:
        if tool not in texts.get(FILES[4], ""):
            errors.append(f"tool-not-in-business-contract:{tool}")
    for called in re.findall(r"(?:client|shengbo)\.query\(\s*['\"]([^'\"]+)['\"]", combined):
        if called not in query_keys:
            errors.append(f"unknown-client-query:{called}")
    for called in re.findall(r"(?:client|shengbo)\.command\(\s*['\"]([^'\"]+)['\"]", combined):
        if called not in command_keys:
            errors.append(f"unknown-client-command:{called}")

    fixture = json.loads((DOC / 'fixtures/模拟上场复盘.json').read_text(encoding='utf-8'))
    raw_path = DOC / 'fixtures' / fixture['original_document']['relative_path']
    raw_bytes = raw_path.read_bytes()
    raw_text = raw_bytes.decode('utf-8')
    if hashlib.sha256(raw_bytes).hexdigest() != fixture['original_document']['sha256']:
        errors.append('recap-raw-hash-mismatch')
    if not (fixture['synthetic'] and fixture['recap_draft']['origin']['synthetic']):
        errors.append('recap-not-marked-synthetic')
    if fixture['recap_draft']['origin']['previous_live_session_id'] is not None:
        errors.append('recap-fabricated-real-session')
    evidence = fixture['recap_draft']['evidence']
    evidence_ids = {x['evidence_id'] for x in evidence}
    for item in evidence:
        if item['quote'] not in raw_text:
            errors.append(f"recap-quote-not-in-source:{item['evidence_id']}")
        if hashlib.sha256(item['quote'].encode('utf-8')).hexdigest() != item['content_hash']:
            errors.append(f"recap-quote-hash-mismatch:{item['evidence_id']}")
    findings = fixture['recap_draft']['findings']
    finding_ids = [x['finding_id'] for x in findings]
    dispositions = fixture['expected_dispositions_under_submitted_test_goal']
    disposition_ids = [x['finding_id'] for x in dispositions]
    if sorted(finding_ids) != sorted(disposition_ids) or len(set(disposition_ids)) != len(disposition_ids):
        errors.append('recap-disposition-coverage')
    for finding in findings:
        if not set(finding['evidence_ids']).issubset(evidence_ids):
            errors.append(f"recap-evidence-ref:{finding['finding_id']}")
    for item in dispositions:
        if item['action'] not in {'apply','partial','reject','defer','already_covered'} or not item['reason']:
            errors.append(f"recap-invalid-disposition:{item['finding_id']}")
    inv = fixture['expected_invariants']
    if (inv['regular_price_stays'],inv['session_price_stays'],inv['currency_stays']) != ('178','107','TWD'):
        errors.append('recap-price-fixture-drift')
    rf8 = next(x for x in findings if x['finding_id']=='RF08')
    rf8_disposition = next(x for x in dispositions if x['finding_id']=='RF08')
    if (rf8['scope_patch']['product_refs'] != [{'fixture_ref':'fixture://product/second-item@1'}]
            or rf8['scope_patch']['variant_refs'] != [] or rf8_disposition['action'] != 'defer'):
        errors.append('rf08-product-number-not-variant-number')
    positive = fixture['positive_ordering_scenario']
    if (sorted(positive['selected_order']) != sorted(positive['expected_order'])
            or positive['expected_order'][0] != 'fixture://script/second-product-v1@1'):
        errors.append('rf08-positive-reorder-changes-selection')
    for finding in findings:
        if finding['finding_id'] != 'RF08' and finding['scope_patch']['product_refs'] != [{'fixture_ref':'fixture://product/slipper@1'}]:
            errors.append(f"recap-finding-product-scope:{finding['finding_id']}")
    fixture_metrics = {'kind': 'synthetic_testpack_structure_not_runtime_test',
                       'findings':len(findings),'dispositions':len(dispositions),
                       'source_quotes':len(evidence),'source_hash_verified':True}
    # Prevent the newly reviewed circular dependency from returning to the blueprint.
    submit_sig = re.search(r'def submit_script_change\(([\s\S]*?)\) -> JobReceipt', service_doc)
    if submit_sig and 'approval_ref' in submit_sig.group(1):
        errors.append('text-generation-incorrectly-requires-audio-approval')
    if 'class ChildScope:' in runtime:
        child = runtime.split('class ChildScope:',1)[1].split('```',1)[0]
        if 'run_id: str' not in child or 'invocation_id: str' not in child:
            errors.append('child-callback-scope-missing-identities')
    if 'AudioRequirements`与TTS job增加' in combined:
        errors.append('approval-circular-requirements-binding')
    for marker in ['ASSESSING_INTENT', 'IntentAssessmentProposal', 'freeze_goal',
                   'save_task_assessment', 'GoalAcceptanceSpec', 'GoalAcceptanceResult',
                   'ResearchResolution', 'AssetChecklist', 'preview_playlist_reorder']:
        if marker not in combined:
            errors.append(f'v22-marker-missing:{marker}')
    if 'save_task_assessment(proposal:IntentAssessmentProposal|TaskAssessmentProposal)' not in texts[FILES[4]]:
        errors.append('intent-assessment-tool-union-missing')
    if 'intent阶段跳过read_completion_observation' not in runtime:
        errors.append('intent-null-goal-observation-branch-missing')
    for marker in ['GraphExecutionRegistry', 'InstanceLock', 'RunFinalizer', 'FINALIZING',
                   'QuestionResumeEnvelope', 'system_resolution_ref', 'outcome_unknown']:
        if marker not in runtime:
            errors.append(f'v23-runtime-contract-missing:{marker}')
    for marker in ['record_product_revision_choice', 'RecordProductRevisionChoiceCommand',
                   'evaluate_revision_currentness', 'needs_revision_choice', '_apply_revision_choice']:
        if marker not in service_doc:
            errors.append(f'v23-business-contract-missing:{marker}')
    if 'record_product_revision_choice' in main_tools:
        errors.append('human-revision-choice-exposed-as-agent-tool')
    assembly_doc = texts.get(FILES[4], '')
    for marker in ['allowed_write_paths', 'selling_point_action', 'preserve']:
        if marker not in assembly_doc:
            errors.append(f'v23-product-preserve-contract-missing:{marker}')
    for name in index['documents']:
        if 'PRELIVE-2.3' not in texts.get(name, ''):
            errors.append(f'current-contract-version-missing:{name}')
    if index['registry_counts'] != {'commands':30, 'queries':27, 'main_business_tools':39}:
        errors.append('v23-index-registry-counts')
    task_doc = texts.get('13_任务识别与历史上下文设计.md','')
    task_block = re.search(r'class TaskAssessment\(BaseModel\):([\s\S]*?)\nclass ',task_doc)
    if task_block and 'acceptance_spec_ref:' in task_block.group(1):
        errors.append('cyclic-assessment-acceptance-reference')
    probe_path = DOC/'fixtures/编排算法设计探针结果.json'
    probe=json.loads(probe_path.read_text(encoding='utf-8'))
    if not (probe['status']=='passed' and probe['distribution_cases']==2681 and not probe['business_application_imported']):
        errors.append('playlist-design-probe-status')
    if hashlib.sha256((ROOT/'tools/probe_playlist_design.py').read_bytes()).hexdigest()!=probe['probe_source_sha256']:
        errors.append('playlist-design-probe-source-hash')

    result = {"kind": "engineering_design_static_validation", "status": "pass" if not errors else "fail",
              "scope": "document structure, local links, selected contract markers only",
              "not_executed": ["business implementation", "SDK integration", "provider calls",
                               "HTTP/database recovery", "OBS playback"],
              "documents": metrics,
              "registry": {"commands": command_keys, "queries": query_keys,
                           "main_business_tools": main_tools}, "fixture":fixture_metrics,"errors": errors}
    result['playlist_design_probe']={'distributions':probe['distribution_cases'],'status':probe['status'],
                                    'scope':'independent finite design model; no application execution'}
    out = DOC / "设计静态校验结果.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "files": len(metrics), "errors": errors,
                      "report": str(out)}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
