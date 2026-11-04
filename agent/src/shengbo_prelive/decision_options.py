"""Canonical option values for new questions and compatible historical reads."""
from collections import Counter
import re
from typing import Annotated

from pydantic import BeforeValidator, Field

from shengbo_contracts.common import StrictModel


_LEGACY_VALUE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,99}\Z")
_VALUE_KEYS = {"value", "id", "choice_id"}
_LABEL_KEYS = {"label", "title"}
_RESERVED_KEYS = _VALUE_KEYS | _LABEL_KEYS | {"human_action", "requires_human_action", "text", "description", "detail", "option"}
_OPTION_NUMBER = r"(?:\d+|[A-Z]|[一二三四五六七八九十]+)"
_PLACEHOLDER = re.compile(rf"(?:{_OPTION_NUMBER}|(?:选项|option)(?:\s*{_OPTION_NUMBER})?)[。.:：、)）]?", re.I)


class DecisionOption(StrictModel):
    value: str = Field(min_length=1, max_length=6000, description="稳定机器值。每个选项必须不同，不在界面上显示。")
    label: str = Field(min_length=1, max_length=6000, description="用户能看懂的选项文字。说明实际处理动作。")


def read_option(raw):
    if not isinstance(raw, dict):
        raise ValueError("确认选项必须是包含 value 与 label 的对象")
    keys = set(raw)
    value_keys, label_keys = keys & _VALUE_KEYS, keys & _LABEL_KEYS
    if len(keys) == 2 and len(value_keys) == len(label_keys) == 1:
        value, label = raw[next(iter(value_keys))], raw[next(iter(label_keys))]
    elif len(raw) == 1:
        value, label = next(iter(raw.items()))
        if not isinstance(value, str) or value in _RESERVED_KEYS or not _LEGACY_VALUE.fullmatch(value):
            raise ValueError("确认选项缺少明确的 value 与 label")
    else:
        raise ValueError("确认选项仅接受明确的 value 与 label")
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > 6000
            or not isinstance(label, str) or not label.strip() or len(label) > 6000):
        raise ValueError("确认选项的 value 与 label 必须是非空文字")
    label = label.strip()
    if _PLACEHOLDER.fullmatch(label):
        raise ValueError("确认选项的 label 要说明实际动作，不能只写编号")
    return {"value": value, "label": label}


def read_stored_option(raw):
    if not isinstance(raw, dict):
        raise ValueError("确认选项必须是对象")
    explicit = {key: raw[key] for key in (_VALUE_KEYS | _LABEL_KEYS) & raw.keys()}
    return read_option(explicit or raw)


def unambiguous_options(options):
    values = Counter(option["value"] for option in options)
    labels = Counter(option["label"].casefold() for option in options)
    return [option for option in options if values[option["value"]] == 1 and labels[option["label"].casefold()] == 1]


def normalize_option_list(raw):
    if not isinstance(raw, list) or len(raw) > 12:
        raise ValueError("确认选项应为最多 12 项的列表")
    options = [read_option(option) for option in raw]
    if len(unambiguous_options(options)) != len(options):
        raise ValueError("确认选项的 value 与 label 均不能重复")
    return options


DecisionOptions = Annotated[list[dict[str, str]],
    BeforeValidator(normalize_option_list, json_schema_input_type=list[DecisionOption])]
