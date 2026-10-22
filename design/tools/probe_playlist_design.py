"""Reproducible design probe; intentionally imports no voice/Agent business code.

Models engineering-v2/12_用户编排与重排设计.md reorder-v1. This is evidence for
the proposed algorithm, not an implementation/API/UI/transaction integration test.
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
from hashlib import sha256
from itertools import product
import json
from pathlib import Path


Item = tuple[int, int]  # product ID and unique occurrence ID within product


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def model_interleave(items: list[Item], group_order: list[int]) -> list[Item]:
    queues: dict[int, deque[Item]] = {p: deque() for p in group_order}
    for item in items:
        queues[item[0]].append(item)
    output: list[Item] = []
    previous: int | None = None
    group_rank = {p: index for index, p in enumerate(group_order)}
    while any(queues.values()):
        candidates = [p for p in group_order if queues[p] and p != previous]
        if not candidates:
            candidates = [p for p in group_order if queues[p]]
        chosen = min(candidates, key=lambda p: (-len(queues[p]), group_rank[p]))
        output.append(queues[chosen].popleft())
        previous = chosen
    return output


def model_grouped(items: list[Item], group_order: list[int]) -> list[Item]:
    return [item for p in group_order for item in items if item[0] == p]


def model_shuffle(items: list[Item], seed: str, domain: str) -> list[Item]:
    def occurrence_id(item: Item) -> str:
        return f"product-{item[0]}-occurrence-{item[1]}"

    def rank(item: Item) -> tuple[bytes, str]:
        item_id = occurrence_id(item)
        payload = f"reorder-v1|{domain}|{seed}|{item_id}".encode("utf-8")
        return sha256(payload).digest(), item_id

    return sorted(items, key=rank)


def run_probe() -> dict[str, object]:
    cases: list[dict[str, object]] = []
    counts_by_groups: dict[str, int] = {}
    for group_count in range(1, 6):
        for counts in product(range(1, 7), repeat=group_count):
            if sum(counts) > 14:
                continue
            group_order = list(range(group_count))
            items = [(p, occurrence) for p, count in enumerate(counts)
                     for occurrence in range(count)]
            output = model_interleave(items, group_order)
            adjacent = sum(a[0] == b[0] for a, b in zip(output, output[1:]))
            lower_bound = max(0, 2 * max(counts) - sum(counts) - 1)
            assert Counter(items) == Counter(output), ("permutation", counts)
            assert adjacent == lower_bound, ("linear_minimum", counts, output)
            for p, count in enumerate(counts):
                assert [i for product_id, i in output if product_id == p] == list(range(count)), (
                    "within_group_order", counts, p
                )
            assert output == model_interleave(items, group_order), ("determinism", counts)
            cases.append({"counts": counts, "output": output,
                          "adjacent": adjacent, "lower_bound": lower_bound})
            key = str(group_count)
            counts_by_groups[key] = counts_by_groups.get(key, 0) + 1

    assert len(cases) == 2681, len(cases)

    sample = [(0, 0), (1, 2), (0, 1), (2, 4), (1, 0)]
    expected_grouped = [(0, 0), (0, 1), (1, 2), (1, 0), (2, 4)]
    assert model_grouped(sample, [0, 1, 2]) == expected_grouped
    seed = "00000000000000000000000000000001"
    shuffle = model_shuffle(sample, seed, "full_shuffle")
    assert Counter(sample) == Counter(shuffle)
    assert shuffle == model_shuffle(sample, seed, "full_shuffle")

    duplicate_script_items = [(0, 0), (0, 1), (1, 0)]
    # 0,0 and 0,1 represent two occurrences even if they point to one script ref.
    duplicate_output = model_shuffle(duplicate_script_items, seed, "full_shuffle")
    assert Counter(duplicate_script_items) == Counter(duplicate_output)
    assert len(duplicate_output) == 3
    assert model_interleave([], []) == []
    assert model_interleave([(0, 0)], [0]) == [(0, 0)]
    loop_example = model_interleave([(0, 0), (0, 1), (1, 0)], [0, 1])
    assert all(a[0] != b[0] for a, b in zip(loop_example, loop_example[1:]))
    assert loop_example[0][0] == loop_example[-1][0]

    return {
        "schema_version": "playlist-design-probe-v1",
        "status": "passed",
        "validation_kind": "independent_design_algorithm_model",
        "business_application_imported": False,
        "algorithm_version": "reorder-v1",
        "design_document": "12_用户编排与重排设计.md",
        "distribution_cases": len(cases),
        "case_counts_by_product_groups": counts_by_groups,
        "input_bounds": {"product_groups_min": 1, "product_groups_max": 5,
                         "occurrences_per_group_min": 1,
                         "occurrences_per_group_max": 6,
                         "total_occurrences_max": 14},
        "distribution_checks": ["exact_occurrence_permutation",
                                "within_product_relative_order",
                                "minimum_linear_same_product_adjacency",
                                "determinism"],
        "ancillary_checks": ["grouped_preserves_requested_product_order",
                             "full_shuffle_permutation_and_seed_repeatability",
                             "duplicate_script_occurrences_not_deduplicated",
                             "empty_list", "single_item",
                             "linear_optimum_may_repeat_at_loop_boundary"],
        "case_results_sha256": sha256(canonical(cases)).hexdigest(),
        "probe_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "limitations": ["Finite enumerated inputs; not a proof for arbitrary input size.",
                        "Models a proposed algorithm, not implemented business functions.",
                        "No API, UI, CAS, approval invalidation, TTS or OBS integration test.",
                        "Linear adjacency checked; circular optimum is not claimed."],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    default_output = Path(__file__).resolve().parents[1] / "engineering-v2" / "fixtures" / "编排算法设计探针结果.json"
    parser.add_argument("--output", type=Path, default=default_output)
    args = parser.parse_args()
    result = run_probe()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
