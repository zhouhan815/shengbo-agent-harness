"""Decode model-produced tool arguments without ambiguous normalization."""
import json
import math


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def parse_tool_arguments(value):
    if not isinstance(value, str):
        raise TypeError("tool arguments must be JSON text")
    parsed = json.loads(value, object_pairs_hook=_unique_object,
                        parse_constant=_reject_constant, parse_float=_finite_float)
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed
