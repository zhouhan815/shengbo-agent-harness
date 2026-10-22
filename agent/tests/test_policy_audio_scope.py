"""Audio scope classification regressions; no model, service or TTS calls."""
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.models import IntentAssessment
from shengbo_prelive.policy import excludes_audio, validate_intent


@pytest.mark.parametrize("text,needs_decision", [
    ("新建全新商品并为直播间完成情人节播前准备。需要资料、话术和语音。", False),
    ("请完成这个商品的播前准备。", False),
    ("请只完成播前准备中的商品资料分析。", True),
    ("请做好播前准备的一部分，暂时仅整理资料。", True),
])
def test_complete_prelive_request_does_not_need_redundant_scope_confirmation(text, needs_decision):
    proposal = IntentAssessment(objective="prepare_session", script_delivery_requirement="new_generation", summary="本次准备",
        authorization_quote=text)
    result = validate_intent({"input": {"text": text}}, proposal)
    assert result["requires_decision"] is needs_decision


LIVE_VALENTINE_REQUEST = (
    '请根据上传的两份资料，把全新的绯笺柔雾唇膏导入商品库，为直播间 room_cd697a001ef44226892ffac21bbeb5b6 准备整场情人节直播。'
    '这不是更新已有商品，资料中未提供的属性标注未知，不编造。'
    '请完成商品事实整理、顾客与使用场景分析、本场活动配置三类分析，适合时并行，主Agent验收并汇合正式研究。'
    '本场信息与长期商品资料分开保存，沿用材料中的2027年2月14日日期、129元日常价、99元本场单价与满180减20的门槛条件。'
    '为同一商品生成两版不同角度的完整模块话术：通勤自用、约会赠礼；整理所需演示素材清单，编排第1版再第2版循环。'
    '只使用本机免费普通话Huihui，完成话术后请展示实际整包文本、音色、顺序让我确认，确认前不要生成音频。'
    '确认后生成本场实际所需语音并准备未采用候选，绝不开播。'
    '整个过程中请保留各子任务的正式来源与报告引用，并如实说明哪些顾客结论只是待验证假设。'
)
AUDIO_EFFECTS = {'prepare_required_audio', 'publish_preparation'}


def assess(text, effects=()):
    proposal = IntentAssessment(objective='prepare_session', script_delivery_requirement='new_generation', summary='按用户请求准备本场',
        authorization_quote=text, proposed_effects=list(effects))
    return validate_intent({'input': {'text': text}}, proposal)


def test_exact_live_valentine_request_keeps_audio_scope_and_original_checks():
    assert not excludes_audio(LIVE_VALENTINE_REQUEST)
    result = assess(LIVE_VALENTINE_REQUEST, AUDIO_EFFECTS)
    assert not result['requires_decision']
    assert AUDIO_EFFECTS <= set(result['allowed_effects'])
    with pytest.raises(ContractError, match='INTENT_EVIDENCE_INVALID'):
        validate_intent({'input': {'text': LIVE_VALENTINE_REQUEST}}, IntentAssessment(
            objective='prepare_session', script_delivery_requirement='new_generation', summary='测试', authorization_quote='模型改写的授权'))


@pytest.mark.parametrize('instruction', [
    '不生成语音。', '先不生成声音', '暂时不要生成音频', '不要合成语音',
    '不要生成任何音频', '本场不需要语音合成', '音频先不要生成', '语音不需要',
    '暂不合成', '本场不要TTS', '取消 tts', '停止生成音频',
    '只要文案', '仅文案', '只输出文本', '只生成一版话术', '只是先要文案',
    '只生成文案；确认后只生成文案', '确认后也不要生成语音',
    '确认前不要生成音频，确认后仍不生成音频',
    '先不生成音频；下次确认后生成语音', '不生成语音：只要文案',
    '只要文案！不要音频？', '不生成音频，确认后生成文案',
])
def test_real_audio_exclusions_still_remove_effects_and_reject_conflicting_plan(instruction):
    text = '准备整场直播；' + instruction
    assert excludes_audio(text)
    result = assess(text)
    assert not AUDIO_EFFECTS & set(result['allowed_effects'])
    with pytest.raises(ContractError, match='GOAL_SCOPE_CONFLICT'):
        assess(text, AUDIO_EFFECTS)


@pytest.mark.parametrize('instruction', [
    '不编造商品信息，确认后生成语音',
    '资料不编造；事实不推断。确认后生成语音。',
    '不要编造生成语音的效果，确认后生成音频',
    '只使用本机普通话音色，生成文案后合成音频',
    '只生成文案和音频', '只要文案与语音',
    '不需要语音识别，确认后合成音频',
    '确认前不要生成音频。确认后生成本场实际所需语音',
    '未经我确认不要生成语音', '未获人工批准前不能合成音频',
    '确认前不要生成，确认后生成',
    '先不生成语音，确认后生成音频',
    '先只要文案，批准后生成语音',
    '暂不合成，先整理文案，等我确认后合成语音',
    '不要直接生成语音，先给文案，等我确认再生成。',
    '不是不生成音频，而是确认后再生成。',
    '不能不生成语音',
    '不用我确认，直接生成语音',
    '资料：不得编造。完成文案后生成音频。',
])
def test_unrelated_negations_and_approval_gates_do_not_cancel_audio_task(instruction):
    text = '准备整场直播；' + instruction
    assert not excludes_audio(text)
    result = assess(text, AUDIO_EFFECTS)
    assert AUDIO_EFFECTS <= set(result['allowed_effects'])

