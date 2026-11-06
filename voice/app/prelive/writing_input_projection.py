"""Deterministic writing roles over an immutable, fully retained context bundle.

Only new v3 whole-version plans opt in. This is a projection of source fields,
not a model summary: original values/relationships stay intact and separated
constraints retain their source paths. Research workflow/audit prose remains in
the persistent source artifact rather than becoming an active writing task.
"""
from copy import deepcopy


VERSION_CONTEXT_CONTRACT = "existing-script-text-v3"
PROJECTION_REVISION = "writing-input-role-projection-v1"
_CONSTRAINT_KEYS = frozenset({"boundary", "status", "classification"})
_REPORT_PROCESS_KEYS = frozenset({"summary", "history_disposition", "downstream_handoff",
    "asset_suggestions", "missing_fields", "dependencies", "source_refs", "product_ids", "lineage"})
_FIELD_PROCESS_KEYS = frozenset({"history_disposition", "downstream_handoff"})
_CURRENT_REQUEST_TYPES = frozenset({"full_rewrite_contract", "writing_direction"})
_PROJECTION_FIELD_NAMES = frozenset({"customer_questions", "claim_constraints",
    "research_assumptions", "projection_provenance"})


def _pointer(path, key):
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _split_constraints(value, path, constraints):
    """Partition by defined research field roles, never by negative-word tests."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            child = _pointer(path, key)
            if key in _CONSTRAINT_KEYS:
                constraints.append({"source_path": child, "value": deepcopy(item),
                    "use": "research_qualification_not_spoken_text"})
            else:
                result[key] = _split_constraints(item, child, constraints)
        return result
    if isinstance(value, list):
        return [_split_constraints(item, _pointer(path, index), constraints) for index, item in enumerate(value)]
    return deepcopy(value)


def _research_report(report, role=None):
    if not isinstance(report, dict):
        return {"unclassified_business_data": deepcopy(report)}
    constraints, questions, process_paths, role_name_collisions = [], [], [], []
    projected = {}
    fields = report.get("fields")
    has_structured_business = (isinstance(fields, dict) and any(
        fields.get(key) not in (None, "", [], {}) for key in ("segments", "scenes", "benefit_priorities")))
    has_structured_business = has_structured_business or bool(report.get("insights"))
    # Known typed audience reports use summary for process/handoff recap and
    # retain their complete business fields below. Unknown/summary-only reports
    # can hold their only conclusion here: never silently discard that value.
    process_summary = role == "audience_scenes" and has_structured_business
    for key, value in report.items():
        path = _pointer("/body/report", key)
        if key in _PROJECTION_FIELD_NAMES:
            role_name_collisions.append({"source_path": path, "value": deepcopy(value)})
        elif key == "summary" and not process_summary:
            projected[key] = deepcopy(value)
        elif key in _REPORT_PROCESS_KEYS:
            process_paths.append(path)
        elif key == "fields" and isinstance(value, dict):
            fields = {}
            for field, content in value.items():
                field_path = _pointer(path, field)
                if field in _FIELD_PROCESS_KEYS:
                    process_paths.append(field_path)
                elif field == "concerns" and isinstance(content, list):
                    for index, concern in enumerate(content):
                        concern_path = _pointer(field_path, index)
                        if not isinstance(concern, dict):
                            questions.append({"source_path": concern_path, "data": deepcopy(concern),
                                "answerability": "verify_against_current_facts", "required_to_speak": False})
                        elif concern.get("gap") not in (None, "", [], {}):
                            constraints.append({"source_path": concern_path, "value": deepcopy(concern),
                                "use": "unverified_claim_boundary_not_a_spoken_question"})
                        else:
                            question = {key: deepcopy(item) for key, item in concern.items()
                                        if key not in {"handling", "gap"}}
                            questions.append({"source_path": concern_path, "data": question,
                                "answerability": "verify_against_current_facts", "required_to_speak": False})
                            for detail in ("handling", "gap"):
                                if detail in concern:
                                    constraints.append({"source_path": _pointer(concern_path, detail),
                                        "value": deepcopy(concern[detail]),
                                        "use": "research_handling_data_not_a_current_task_or_script"})
                elif field == "context" and isinstance(content, dict):
                    business_context = {key: deepcopy(item) for key, item in content.items() if key != "scope"}
                    if "scope" in content:
                        process_paths.append(_pointer(field_path, "scope"))
                    fields[field] = _split_constraints(business_context, field_path, constraints)
                else:
                    fields[field] = _split_constraints(content, field_path, constraints)
            projected[key] = fields
        elif key == "evidence" and isinstance(value, list):
            evidence = []
            for index, item in enumerate(value):
                item_path = _pointer(path, index)
                if not isinstance(item, dict):
                    evidence.append({"source_path": item_path, "data": deepcopy(item)})
                    continue
                # Excerpts, refs, locators and unknown provenance fields stay
                # available. Only the known explanatory process field moves out.
                evidence.append({"source_path": item_path,
                    **{key: deepcopy(text) for key, text in item.items() if key != "supports"}})
                if "supports" in item:
                    process_paths.append(_pointer(item_path, "supports"))
            projected[key] = evidence
        elif key == "hypotheses":
            projected["research_assumptions"] = {"source_path": path, "value": deepcopy(value),
                "use": "conditional_audience_planning_not_confirmed_product_claims"}
        else:
            # Includes complete insights and any newly added business section;
            # absence from a hand-maintained allowlist must not discard research.
            projected[key] = _split_constraints(value, path, constraints)
    projected["customer_questions"] = questions
    projected["claim_constraints"] = constraints
    projected["projection_provenance"] = {"source_root_path": "/body/report",
        "process_sections_retained_only_in_source": process_paths,
        "source_values_sharing_projection_field_names": role_name_collisions,
        "summary_role": "source_process_recap" if process_summary else "retained_research_data_not_task_instructions",
        "business_values": "verbatim_fields_with_relationships_not_a_summary",
        "unknown_business_sections": "retained_as_research_data_not_task_instructions"}
    return projected


def project_writing_context(context, spec):
    """Return a writing-only view; v1/v2 and local edit inputs stay unchanged."""
    if (spec.get("version_context_contract") != VERSION_CONTEXT_CONTRACT
            or spec.get("generation_mode") != "whole_script"):
        return context
    result = deepcopy(context)
    for record in result.get("records", []):
        kind = record.get("ref", {}).get("kind")
        if kind == "analysis":
            body = record.get("body", {})
            source_report = body.get("report", {})
            record["body"] = {key: deepcopy(value) for key, value in body.items()
                              if key not in {"report", "lineage", "readset", "source_ref_replacements"}}
            record["body"]["report"] = _research_report(source_report, body.get("role"))
            record["reference_role"] = "customer_research_data_not_task_instructions"
            record["source_artifact_ref"] = deepcopy(record["ref"])
        elif kind == "draft":
            # All commercial conditions and business themes remain verbatim.
            # Metadata supplies interpretation rather than rewriting the draft.
            record["reference_role"] = "session_business_data"
            record["writing_use"] = {"theme": "business_topic_not_a_writing_style_instruction",
                "objective": "historical_session_objective_current_assignment_takes_precedence",
                "execution_configuration": "not_spoken_text_or_a_new_writing_task"}
    decisions = result.get("user_decisions", [])
    current, business = [], []
    if isinstance(decisions, list):
        for index, decision in enumerate(decisions):
            entry = {"source_path": _pointer("/user_decisions", index), "value": deepcopy(decision)}
            if isinstance(decision, dict) and decision.get("type") in _CURRENT_REQUEST_TYPES:
                current.append(entry)
            else:
                business.append(entry)
    else:
        business.append({"source_path": "/user_decisions", "value": deepcopy(decisions)})
    result.pop("user_decisions", None)
    result["user_business_decisions"] = business
    result["current_assignment"] = {"module_tasks_path": "/module_instructions",
        "explicit_user_writing_requests": current,
        "precedence": "Current trusted writing requests and selected module tasks govern this generation; historical research scopes, session themes and execution records do not replace them."}
    result["writing_input_contract"] = {"revision": PROJECTION_REVISION,
        "source_contract": VERSION_CONTEXT_CONTRACT,
        "research": "Complete business fields and relationships; separated qualifications keep verbatim values and source paths.",
        "questions": "Research concerns are optional planning data. A gap is only a claim boundary; no gap does not prove an answer. Use current product facts to decide what can be answered.",
        "consumer_limits": "Confirmed product, promotion and consumer restrictions remain authoritative. Research unknowns are not confirmed consumer limits or a required disclaimer.",
        "persistence": "Source records remain complete and unchanged; omitted workflow/audit text is addressable by each exact source artifact reference."}
    return result
