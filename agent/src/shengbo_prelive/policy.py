"""The model proposes a plan; this module never treats a proposal as permission."""
from __future__ import annotations

import json
import re
from shengbo_contracts.business import MODULE_KEYS
from shengbo_contracts.common import ContractError, same_artifact_ref

READ_TOOLS = {"read_capabilities", "read_business_schema", "read_source", "search_catalog", "read_product",
              "read_preparation", "read_room", "read_voice_options", "read_context", "read_script", "read_script_usage",
              "read_job", "read_run_history", "read_analysis_batch", "read_recap", "read_text_review", "read_audio_readiness",
              "preview_playlist_reorder", "find_audience_research", "read_session_history", "inspect_change_impact",
              "read_asset_checklist", "read_script_audio_preview", "list_script_audio_previews"}
FORBIDDEN = {"task", "execute", "write_file", "edit_file", "delete_file", "execute_sql", "shell"}
INTENT_TOOLS = READ_TOOLS | {"save_task_assessment", "request_user_decision"}
EXECUTE_READ_TOOLS = {"read_generation_output"}
ROLE_TOOLS = {
    "product_facts": {"read_capabilities", "read_business_schema", "read_source", "search_catalog", "read_product", "read_context", "submit_product_facts_report"},
    "session_config": {"read_capabilities", "read_business_schema", "read_voice_options", "read_source", "read_product", "read_preparation", "read_room", "read_context", "read_session_history", "submit_session_config_report"},
    "audience_scenes": {"read_capabilities", "read_source", "read_product", "read_context", "find_audience_research", "web_search", "fetch_source", "submit_audience_scenes_report"},
}
for _role in ROLE_TOOLS:
    ROLE_TOOLS[_role].add("correct_analysis_report")
BASE_WRITES = {"import_blob", "accept_analysis_report", "save_asset_checklist"}
EFFECTS = {
    "catalog_update": BASE_WRITES | {"save_product"},
    "facts_analysis": BASE_WRITES,
    "audience_research": BASE_WRITES,
    "session_config": BASE_WRITES | {"save_session_draft"},
    "asset_preparation": {"save_asset_checklist"},
    "playlist_reorder": {"save_playlist", "compile_playback_plan"},
    "script_change": BASE_WRITES | {"save_context_bundle", "save_script_plan", "submit_script_change", "patch_script", "save_playlist", "compile_playback_plan"},
    "audio_prepare": {"compile_playback_plan", "prepare_required_audio", "publish_preparation"},
    "audio_preview": {"preview_script_audio"},
    "prepare_session": BASE_WRITES | {"save_product", "save_session_draft", "update_preparation_products", "refresh_product_reference", "save_context_bundle", "save_script_plan", "submit_script_change", "patch_script", "save_playlist", "compile_playback_plan", "prepare_required_audio", "publish_preparation", "import_recap", "link_recap_to_preparation"},
    "materials_to_script": BASE_WRITES | {"save_product", "save_session_draft", "update_preparation_products", "save_context_bundle", "save_script_plan", "submit_script_change", "patch_script"},
    "read_explain": set(),
}
for _objective in ("script_change", "audio_prepare", "prepare_session", "materials_to_script"):
    EFFECTS[_objective].add("retry_failed_items")
for _objective in ("script_change", "prepare_session", "materials_to_script"):
    EFFECTS[_objective].add("import_script")


_AUDIO = r"(?:语音|音频|声音|TTS)(?!识别|转文字|转写|描述|说明)"
_AUDIO_ACTION = r"(?:生成|合成|制作|输出|准备|产出|渲染|做)"
_AUDIO_MODIFIERS = r"(?:本场|本次|当前|实际|所需|全部|任何|新|的|本机|普通话)*"
_NEGATION = r"(?:不需要|不需|不用|不必|不要|不能|不得|不|别|禁止|取消|停止)"
_ACTION_MODIFIERS = r"(?:再|先|现在|立即|直接|自动|进行|给我|为我|继续|去|来|提前|擅自|马上|帮我)*"
_PRE_APPROVAL = re.compile(r"(?:确认|批准|审核|审阅)(?:通过)?(?:之?前|以前)|(?:未经|未获|未得到|还未|尚未|没有|没)(?:我|人工|用户)?(?:确认|批准|审核|审阅)")
_POST_APPROVAL = re.compile(r"(?:确认|批准|审核|审阅)(?:通过)?(?:之?后|完后|完成后|好了再|了再|再)")
_NEGATIVE_AUDIO = re.compile(
    _NEGATION + _ACTION_MODIFIERS + _AUDIO_ACTION + _AUDIO_MODIFIERS + _AUDIO
    + "|" + _AUDIO + r"(?:生成|合成)?(?:都|也|先|暂时|目前)*" + _NEGATION + _ACTION_MODIFIERS + _AUDIO_ACTION
    + "|" + _AUDIO + r"(?:都|也|先|暂时|目前)*" + _NEGATION + r"(?:了)?$"
    + "|" + _NEGATION + _ACTION_MODIFIERS + _AUDIO_MODIFIERS + _AUDIO + r"(?:生成|合成)?(?:了|即可)?$"
    + "|" + _NEGATION + _ACTION_MODIFIERS + r"合成(?:了)?$", re.IGNORECASE)
_TEXT_ONLY = re.compile(r"(?:仅仅|仅|只)(?:是)?(?:先|暂时)?(?:要|需要|需|做|生成|输出|提供|写|准备|保存|整理)?(?:本场|本次|完整|整包|一版|两版|一份|两份|的)*(?:文案|话术|脚本|文字稿|文字|文本)")
_DOUBLE_NEGATION = re.compile(r"(?:不是|并非|不能|不得|不可|不要|不需要|不需|不用|不必|别|不)$")
_RECAP_IMPORT_REQUEST = re.compile(
    r"^(?:请|帮我|先|再|然后|直接|将|把)*"
    r"(?:导入(?:这份|此份|上传的|附件里的|附件中的|本次的)?(?:直播|播后|历史)?复盘(?:文件|资料|报告)?"
    r"|(?:这份|此份|上传的|附件里的|附件中的|本次的)?(?:直播|播后|历史)?复盘(?:文件|资料|报告)?导入)"
    r"(?:(?:并关联|并绑定|到|进)本场)?$"
)


def excludes_audio(text):
    """Recognize direct task exclusions, separately from pre-approval gates.

    Never let a negation in an unrelated sentence reach a later audio request.
    A nearby explicit post-approval generation step describes the same staged
    workflow; it still passes through the independent human approval boundary.
    """
    clauses = [re.sub(r"\s+", "", part) for part in re.split(r"[，,。；;！？!?\r\n]+", text) if part.strip()]

    def direct_negative(clause):
        return any(not _DOUBLE_NEGATION.search(clause[:match.start()]) for match in _NEGATIVE_AUDIO.finditer(clause))

    def after_approval_generation(clause):
        if not _POST_APPROVAL.search(clause) or direct_negative(clause):
            return False
        if re.search(r"下次|下一场|另一个任务|另行|以后", clause):
            return False
        return bool(re.search(_AUDIO_ACTION + _AUDIO_MODIFIERS + _AUDIO, clause, re.IGNORECASE)
                    or re.search(_AUDIO_ACTION + r"(?:即可|吧|了)?$", clause))

    for index, clause in enumerate(clauses):
        negative = direct_negative(clause)
        text_only = bool(_TEXT_ONLY.search(clause))
        # "只要文案和音频" includes audio rather than excluding it.
        if text_only and re.search(r"(?:和|及|与|、|以及)" + _AUDIO_MODIFIERS + _AUDIO, clause, re.IGNORECASE):
            text_only = False
        if not negative and not text_only:
            continue
        if _PRE_APPROVAL.search(clause):
            continue
        if any(after_approval_generation(following) for following in clauses[index + 1:index + 3]):
            continue
        return True
    return False


_SCRIPT_TEXT_EFFECTS = {"save_context_bundle", "save_script_plan", "submit_script_change", "retry_failed_items"}
_SCRIPT_BUTTON_HEADER = "请为当前本场新增完整话术版本："
_SCRIPT_BUTTON_SCOPE = re.compile(r"\r?\n当前明确范围与已有稿引用：\r?\n")
_SCRIPT_FOLLOW_UP = re.compile(
    r"(?:\A|(?<=[。；\n]))(?:复用[^。；\r\n]{1,80}，)?(?:只|仅)为以下[一二三四五六七八九十两0-9]+款分别新增完整(?:直播)?话术(?:版本)?[：:]"
)
_SCRIPT_TARGET_LINE = re.compile(r"(?m)^- [^：:\r\n]+[：:]\s*新增 ([1-9][0-9]?) 版[，；。]")


def _script_button_manifest(text):
    if not text.startswith(_SCRIPT_BUTTON_HEADER):
        return None
    sections = _SCRIPT_BUTTON_SCOPE.split(text, maxsplit=1)
    if len(sections) != 2:
        return None
    try:
        binding = json.loads(sections[1])
    except ValueError:
        return None
    return binding if isinstance(binding, dict) else None


def _script_authorization_from_input(run, proposal):
    """Resolve only the explicit new-text workflow from the current submitted input."""
    value = run["input"]
    text = value["text"]
    scope = run.get("scope") or {}
    preparation_id = scope.get("preparation_id")
    typed = value.get("script_generation")
    if typed:
        expected = {row["product_ref"]["id"]: row["count"] for row in typed["product_counts"]}
        counts = {row.product_id: row.count for row in proposal.requested_script_variants}
        if (proposal.objective != "materials_to_script" or proposal.script_mode != "generate"
                or proposal.script_delivery_requirement != "new_generation" or proposal.preparation_id != preparation_id
                or typed["preparation_id"] != preparation_id or scope.get("kind") != "preparation"
                or proposal.room_id not in {None, run.get("room_id")}
                or counts != expected or set(proposal.target_product_ids) != expected.keys()
                or proposal.required_analyses or proposal.required_reuse_analysis_refs or proposal.requested_modules
                or proposal.requested_product_paths or proposal.needs_clarification
                or set(proposal.proposed_effects) - _SCRIPT_TEXT_EFFECTS):
            raise ContractError("INTENT_SCRIPT_GENERATION_SCOPE", "新增话术只使用当前本场明确选择的商品、版数和文字生成范围", status=422)
        return text.strip().splitlines()[0][:4000]
    if (proposal.objective not in {"materials_to_script", "script_change"} or proposal.script_mode != "generate"
            or scope.get("kind") != "preparation" or not preparation_id
            or proposal.preparation_id not in {None, preparation_id}
            or value.get("material_refs") or value.get("selection") or proposal.needs_clarification
            or proposal.required_analyses or not excludes_audio(text)):
        return None
    counts = {item.product_id: item.count for item in proposal.requested_script_variants}
    if not counts or None in counts or set(counts) != set(proposal.target_product_ids):
        return None
    if text.startswith(_SCRIPT_BUTTON_HEADER):
        binding = _script_button_manifest(text)
        try:
            declared = binding["product_counts"]
            bound_preparation = binding["preparation_id"]
            expected = {item["product_ref"]["id"]: item["count"] for item in declared if item["product_ref"]["kind"] == "product"}
        except (ValueError, TypeError, KeyError):
            return None
        if (bound_preparation != preparation_id or len(expected) != len(declared)
                or any(type(count) is not int for count in expected.values()) or expected != counts):
            return None
        return _SCRIPT_BUTTON_HEADER
    if value.get("intent_mode") != "follow_up" or not text.startswith(("请接着完成", "继续完成")):
        return None
    match = _SCRIPT_FOLLOW_UP.search(text)
    products = {ref["id"] for ref in value.get("observed_versions", []) if ref["kind"] == "product"}
    declared_counts = [int(count) for count in _SCRIPT_TARGET_LINE.findall(text)]
    # Only the uniform, explicitly listed retry shape is bound without a
    # typed per-product manifest. The observed refs narrow the existing scope.
    if (match and products == set(counts) and len(set(counts.values())) == 1
            and sorted(declared_counts) == sorted(counts.values())):
        return match.group()
    return None


def bind_intent_authorization(run, proposal):
    """Persist an exact server-selected quote for supported script requests."""
    quote = _script_authorization_from_input(run, proposal)
    return proposal.model_copy(update={"authorization_quote": quote}) if quote is not None else proposal


def validate_intent(run, proposal):
    from .product_material_actions import validate_product_material_intent
    from .selling_points import validate_selling_points_intent
    validate_product_material_intent(run, proposal)
    validate_selling_points_intent(run, proposal)
    text = run["input"]["text"]
    bound_quote = _script_authorization_from_input(run, proposal)
    quote = proposal.authorization_quote
    if not isinstance(quote, str) or not quote.strip() or quote not in text:
        raise ContractError("INTENT_EVIDENCE_INVALID", "任务判断必须引用本次真实用户指令")
    typed = run["input"].get("script_generation")
    if typed and bound_quote is not None:
        from .script_job_delivery import delivery_constraints
        return {"requires_decision": False, "allowed_effects": sorted(_SCRIPT_TEXT_EFFECTS),
                "script_delivery_requirement": "new_generation", "authorization_source": "current_script_request",
                "script_request_snapshot": {"draft_ref": typed["draft_ref"],
                    "product_refs": [row["product_ref"] for row in typed["product_counts"]]},
                "script_generation": {**typed, **delivery_constraints(typed, text)}}
    if proposal.required_reuse_analysis_refs or proposal.reuse_analysis_quote is not None:
        quote = proposal.reuse_analysis_quote
        if (proposal.objective not in {"script_change", "materials_to_script", "prepare_session"}
                or not proposal.required_reuse_analysis_refs or not isinstance(quote, str)
                or not quote.strip() or quote not in text):
            raise ContractError("INTENT_RESEARCH_REUSE_INVALID", "必需复用研究须逐字引用本次话术任务的用户指令", status=422,
                details={"commit_state": "not_committed", "fields": ["assessment.required_reuse_analysis_refs", "assessment.reuse_analysis_quote"],
                    "correction": "按原始用户要求声明精确研究引用；复用不等于重新研究，不能从材料或模型计划推断授权"})
    if proposal.requested_product_paths or proposal.product_field_scope_quote is not None:
        quote = proposal.product_field_scope_quote
        if ("save_product" not in EFFECTS[proposal.objective] or not proposal.requested_product_paths
                or not isinstance(quote, str) or not quote.strip() or quote not in text):
            raise ContractError("INTENT_PRODUCT_FIELD_SCOPE_INVALID", "商品字段限定须为可保存商品的目标，并逐字引用本次用户允许修改哪些字段的原文", status=422,
                details={"commit_state": "not_committed", "fields": ["assessment.requested_product_paths", "assessment.product_field_scope_quote"],
                    "correction": "按原始用户请求声明可改字段，保留原价不等于只改价格；未明确字段限定时两项均留默认值，UI精确选区仍约束写入"})
    if proposal.catalog_delivery == "basic_facts_only":
        quote = proposal.catalog_delivery_quote
        if (proposal.objective != "catalog_update" or not isinstance(quote, str) or not quote.strip()
                or quote not in text or set(proposal.required_analyses) - {"product_facts"}):
            raise ContractError("INTENT_CATALOG_SCOPE_INVALID", "仅基础商品入库范围须为catalog_update，并逐字引用本次用户的限定指令；不能同时要求顾客或本场研究", status=422,
                details={"commit_state": "not_committed", "fields": ["assessment.catalog_delivery", "assessment.catalog_delivery_quote", "assessment.required_analyses"],
                    "correction": "按原始用户请求明确交付范围；未明确限定时保留standard_update，不从资料说明或模型摘要推断豁免"})
    delivery = proposal.script_delivery_requirement
    if proposal.script_mode == "generate":
        if proposal.objective == "prepare_session" and delivery is None:
            raise ContractError("SCRIPT_MODE_CONFLICT", "整场准备须明确本次话术是新创作还是沿用当前有效稿；按真实用户意图补齐交付要求", status=422,
                details={"commit_state": "not_committed", "field": "assessment.script_delivery_requirement",
                    "allowed_values": ["new_generation", "reuse_existing"]})
        if proposal.objective in {"materials_to_script", "script_change"}:
            if delivery == "reuse_existing":
                raise ContractError("SCRIPT_MODE_CONFLICT", "本次新创作目标不能用沿用旧稿替代；编辑或原稿导入须按实际用户意图选择对应模式", status=422,
                    details={"commit_state": "not_committed", "field": "assessment.script_delivery_requirement"})
            delivery = "new_generation"
    elif delivery is not None:
        raise ContractError("SCRIPT_MODE_CONFLICT", "script_delivery_requirement仅用于generate模式；导入、扩版及编辑由script_mode本身限定交付", status=422,
            details={"commit_state": "not_committed", "field": "assessment.script_delivery_requirement"})
    if proposal.needs_clarification:
        return {"requires_decision": True, "reason": proposal.clarification or "需要明确本次任务范围", "script_delivery_requirement": delivery}
    broad = bool(re.search(r"准备.*直播|完整.*播前|整场.*准备|全部.*准备|从.*商品.*到.*话术|开播前.*准备", text))
    # A clear request to complete prelive preparation is already an explicit
    # scope grant. Do not ask a second generic scope question just because the
    # user says “完成情人节播前准备” rather than “完整播前准备”. A request for
    # only one constituent part still needs its narrower objective.
    if not broad:
        broad = any(re.search(r"(?:完成|做好|执行|开展)[^。；！？\n]{0,32}播前准备", clause)
            and not re.search(r"只|仅|其中|中的|一部分", clause)
            for clause in re.split(r"[。；！？\n]+", text))
    selected_target = (run["input"].get("selection") or {}).get("target") or {}
    selected_script = (selected_target.get("object_ref") or {}).get("kind") == "script_revision"
    if (proposal.objective == "script_change" and proposal.script_mode == "edit"
            and re.search(r"只|仅|其余.*不|其他.*不", text)
            and re.search(r"活动|开场|卖点|商品介绍|促单|campaign|opening_(?:long|short)|selling_points|product_intro|cta|模块", text)
            and not proposal.requested_modules and not selected_target.get("module_key")):
        raise ContractError("INTENT_MODULE_SCOPE_REQUIRED", "用户已限定局部模块，任务判断须明确requested_modules，不能留空默许整稿修改", status=422,
            details={"field": "assessment.requested_modules", "allowed_values": list(MODULE_KEYS), "commit_state": "not_committed"})
    action = bool(re.search(r"更新|修改|改成|改为|改一下|润色|缩短|精简|补充|新增|新建|建立|保存|整理.*商品|导入|生成|重写|调整|重排|排序|准备|分析|研究|调研|配置|帮我.*做", text))
    if not action and proposal.objective == "script_change" and proposal.script_mode == "expand_original":
        action = any(re.search(r"(?:扩成|扩写成|扩展成|扩展为|扩版)\s*(?:[一二两三四五六七八九十]+|\d+)\s*(?:个\s*)?(?:新\s*)?版", clause)
            and not re.search(r"不要|不用|无需|不必|不想|不需要|禁止|别|怎么|如何|为什么", clause)
            for clause in re.split(r"[，,。；;！？!?\r\n]+", text))
    if proposal.objective == "audio_preview" and re.search(r"试听|合成", text):
        action = True
    if proposal.objective == "asset_preparation":
        action = bool(re.search(r"素材|布景|装修|拍摄|准备.*视频|视频.*清单", text))
    if selected_script and proposal.objective == "script_change" and re.search(r"改|润色|口语|缩短|精简|重写|扩写|替换", text):
        action = True
    if proposal.objective != "read_explain" and not action:
        return {"requires_decision": True, "reason": "请确认是只分析资料，还是更新商品或准备本场直播", "script_delivery_requirement": delivery}
    if proposal.objective == "prepare_session" and not broad:
        return {"requires_decision": True, "reason": "是否准备整场直播？这会包含资料、话术和编排等动作", "script_delivery_requirement": delivery}
    if proposal.objective in {"script_change", "materials_to_script"} and not selected_script and not re.search(r"话术|文案|脚本|开场|促单|正文|口播", text):
        return {"requires_decision": True, "reason": "本次是否明确要求修改话术？仅更新商品默认不改稿", "script_delivery_requirement": delivery}
    text_action = (r"导入|原样|现成.*(?:保存|播放)" if proposal.script_mode == "import_original" else
                   r"扩版|扩展|多版|不同版本|生成|编写|改写" if proposal.script_mode == "expand_original" else
                   r"修改|改成|调整|更新|替换" if proposal.script_mode == "edit" else
                   r"生成|编写|撰写|写出|写一|从.*(?:资料|商品).*到.*话术")
    if proposal.objective == "materials_to_script" and not re.search(text_action, text):
        return {"requires_decision": True, "reason": "是否需要根据资料生成话术？资料分析本身不授权商品保存和创作", "script_delivery_requirement": delivery}
    if proposal.objective in {"audio_prepare", "audio_preview"} and not re.search(r"试听|语音|声音|音频|合成|TTS|tts", text):
        return {"requires_decision": True, "reason": "本次是否需要生成语音？", "script_delivery_requirement": delivery}
    if proposal.objective == "audio_preview" and not proposal.requested_modules:
        raise ContractError("PREVIEW_MODULE_SCOPE_REQUIRED", "局部试听须明确requested_modules，不能默认整稿合成", status=422)
    effects = set(EFFECTS[proposal.objective])
    if bound_quote is not None:
        effects &= _SCRIPT_TEXT_EFFECTS
    if (proposal.objective in {"script_change", "materials_to_script"} and proposal.script_mode != "import_original"
            and run["input"].get("material_refs") and any(_RECAP_IMPORT_REQUEST.match(clause.strip())
                for clause in re.split(r"[，,。；;！？!?\r\n]+", text))):
        effects.update({"import_blob", "import_recap", "link_recap_to_preparation"})
    if proposal.script_mode == "import_original":
        effects -= {"save_script_plan", "submit_script_change", "patch_script", "retry_failed_items"}
    if excludes_audio(text):
        effects -= {"prepare_required_audio", "publish_preparation", "preview_script_audio"}
    if proposal.objective in {"catalog_update", "session_config"}:
        effects -= {"submit_script_change", "patch_script", "prepare_required_audio"}
    if proposal.proposed_effects and not set(proposal.proposed_effects) <= effects:
        raise ContractError("GOAL_SCOPE_CONFLICT", "计划包含用户任务未授予的业务动作")
    result = {"requires_decision": False, "allowed_effects": sorted(effects), "script_delivery_requirement": delivery}
    if bound_quote is not None:
        result["authorization_source"] = "current_script_request"
        binding = _script_button_manifest(text)
        if binding is not None and "draft_ref" in binding:
            draft = binding["draft_ref"]
            products = [item["product_ref"] for item in binding["product_counts"]]
            if (not same_artifact_ref(draft, draft) or draft["kind"] != "draft"
                    or any(not same_artifact_ref(ref, ref) for ref in products)):
                raise ContractError("CONTEXT_INPUTS_CHANGED", "生成要求缺少有效的本场配置或商品引用，请刷新后重新提交",
                                    status=409, details={"commit_state": "not_committed"})
            result["script_request_snapshot"] = {"draft_ref": draft, "product_refs": products}
    return result


def check_tool_allowed(scope, run, name):
    if name in FORBIDDEN:
        raise ContractError("TOOL_FORBIDDEN", "该执行入口未授权", status=403)
    if scope.role != "main":
        allowed = ROLE_TOOLS[scope.role] | {"ls", "read_file", "glob", "grep"}
    elif scope.phase == "intent":
        allowed = INTENT_TOOLS | {"ls", "read_file", "glob", "grep"}
    else:
        allowed = READ_TOOLS | EXECUTE_READ_TOOLS | set(run.get("allowed_tools", [])) | {
            "save_task_assessment", "request_user_decision", "dispatch_analysis_batch", "accept_analysis_reports",
            "finish_turn", "ls", "read_file", "glob", "grep", "validate_product_assembly", "tool_catalog", "activate_tools"}
        # These are optional capabilities, not stage gates. A flow cannot grant
        # its internal write permission: business_command rechecks every step.
        from .workflows import WORKFLOW_EFFECTS
        effects = set(run.get("allowed_effects", []))
        allowed |= {flow for flow, required in WORKFLOW_EFFECTS.items() if effects & required}
        if (run.get("goal") or {}).get("objective") == "asset_preparation":
            allowed.discard("dispatch_analysis_batch")
    if name not in allowed:
        raise ContractError("TOOL_SCOPE_DENIED", "当前授权或角色不能调用该工具", status=403, details={"tool": name})

