"""
feasibility_path_generator.py - Bộ sinh đường đi EFSM có kiểm tra tính khả thi TRONG lúc duyệt.

Mở rộng ``test_path_generator.TestPathGenerator`` (file gốc được khóa sha256 trong ``tests/baseline_hashes.json``
nên không bị sửa) bằng kế thừa, tái sử dụng toàn bộ BFS/DFS/negative/coverage hiện có:

1. **Không cho dữ liệu đầu vào ghi đè biến trạng thái** (lỗi D1 của bản gốc): ở mỗi bước, giá trị của context
   chỉ được áp dụng cho biến ``origin == "request"`` hoặc biến CHƯA có trong context hiện tại. Biến đã được action
   trước đó xác định thì giữ nguyên; ý định ghi đè được ghi vào ``rejected_input_overrides`` của bước.
2. **Chứng minh vô nghiệm** bằng ``efsm_feasibility.EfsmPathProver`` (diễn giải trừu tượng trên guard + action,
   đúng với mọi dữ liệu / setup / lựa chọn sản phẩm). Ba chế độ ``dfs_feasibility``:

   * ``off``      : như bản gốc.
   * ``annotate`` : (mặc định) sinh đúng tập path như bản gốc; mỗi path có thêm ``feasibility_proof``
                    (``PROVEN_INFEASIBLE`` kèm chứng cứ, hoặc ``NOT_PROVEN_INFEASIBLE``).
   * ``prune``    : DFS cắt nhánh ngay khi phần mở rộng bị chứng minh vô nghiệm (phát hiện sớm); các phần mở rộng
                    bị cắt được liệt kê ở ``pruned_infeasible_extensions`` để vẫn truy vết được.

   Chỉ positive path bị cắt. Negative path không bao giờ bị lọc bởi chứng minh này (bước tấn công được phép vi
   phạm guard); ``feasibility_proof`` của negative path chỉ áp dụng cho tiền tố positive.
3. Không chèn transition vào path, không sửa guard/action/luật bảo mật.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from efsm_feasibility import EfsmPathProver, ProofResult
from test_path_generator import (
    Edge,
    GenerationResult,
    PathContext,
    PathGeneratorError,
    TestPathGenerator,
    _build_parser,
)

__all__ = ["FeasibilityAwarePathGenerator", "DFS_FEASIBILITY_MODES", "main"]

DFS_FEASIBILITY_MODES = ("off", "annotate", "prune")


def _load_line_variables() -> frozenset[str]:
    from test_data_generator import ShadowState  # cùng định nghĩa với bộ sinh dữ liệu (ShadowState.project)

    return ShadowState.LINE_VARIABLES


class FeasibilityAwarePathGenerator(TestPathGenerator):
    """``TestPathGenerator`` + chứng minh vô nghiệm + chặn ghi đè biến trạng thái."""

    __test__ = False

    def __init__(self, model_path: str | Path, rules_path: str | Path | None = None, *,
                 api_mapping: Mapping[str, Any] | str | Path | None = None, dfs_feasibility: str = "annotate",
                 **kwargs: Any) -> None:
        if dfs_feasibility not in DFS_FEASIBILITY_MODES:
            raise ValueError(f"dfs_feasibility must be one of {DFS_FEASIBILITY_MODES}")
        super().__init__(model_path, rules_path, **kwargs)
        self.api_mapping_file: str | None = None
        if isinstance(api_mapping, (str, Path)):
            self.api_mapping_file = Path(api_mapping).name
            api_mapping = json.loads(Path(api_mapping).read_text(encoding="utf-8"))
        self.prover: EfsmPathProver | None = (
            EfsmPathProver.from_api_mapping(self.evaluator, api_mapping, _load_line_variables()) if api_mapping else None
        )
        if self.prover is None and dfs_feasibility != "off":
            raise ValueError("dfs_feasibility='annotate'/'prune' needs an api mapping (product-selection semantics)")
        self.dfs_feasibility = dfs_feasibility
        self._proof_cache: dict[tuple[str, ...], ProofResult] = {}
        self.pruned_extensions: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # D1: dữ liệu đầu vào không được ghi đè biến trạng thái
    # ------------------------------------------------------------------
    def _simulate_step(self, state: str, carry: Mapping[str, Any], tid: str, inputs: Mapping[str, Any], index: int):
        allowed: dict[str, Any] = {}
        rejected: list[dict[str, Any]] = []
        for name, value in inputs.items():
            if self._origin(name) == "request" or name not in carry or carry[name] == value:
                allowed[name] = value
            else:
                rejected.append({"variable": name, "input_value": value, "state_value": carry[name],
                                 "reason": "variable already determined by the path; inputs may only set request "
                                           "variables or fill variables that are still unknown"})
        record, verdict, new_state, new_carry = super()._simulate_step(state, carry, tid, allowed, index)
        if rejected:
            record["rejected_input_overrides"] = rejected
        return record, verdict, new_state, new_carry

    # ------------------------------------------------------------------
    # Chứng minh
    # ------------------------------------------------------------------
    def prove(self, tids: Sequence[str]) -> ProofResult:
        key = tuple(tids)
        if key not in self._proof_cache:
            assert self.prover is not None
            self._proof_cache[key] = self.prover.prove(key)
        return self._proof_cache[key]

    # ------------------------------------------------------------------
    # DFS có cắt nhánh vô nghiệm (chỉ ở chế độ prune)
    # ------------------------------------------------------------------
    def generate_dfs_paths(self, context: PathContext | None = None, *, max_depth: int = 8, max_paths: int = 200,
                           max_visits_per_state: int = 3, max_transition_repeats: int = 2):
        if self.dfs_feasibility != "prune":
            return super().generate_dfs_paths(context, max_depth=max_depth, max_paths=max_paths,
                                              max_visits_per_state=max_visits_per_state,
                                              max_transition_repeats=max_transition_repeats)
        for name, value in (("max_depth", max_depth), ("max_paths", max_paths),
                            ("max_visits_per_state", max_visits_per_state),
                            ("max_transition_repeats", max_transition_repeats)):
            if value < 1:
                raise ValueError(f"{name} must be >= 1")
        found: list[tuple[list[str], str]] = []
        initial = self.graph.initial_state
        path: list[Edge] = []
        visits = {initial: 1}
        repeats: dict[str, int] = {}
        truncated = False
        pruned_keys: dict[str, dict[str, Any]] = {}

        def structural(edge: Edge) -> bool:
            return (len(path) < max_depth
                    and visits.get(edge.target, 0) < max_visits_per_state
                    and repeats.get(edge.transition_id, 0) < max_transition_repeats)

        def feasible(edge: Edge) -> bool:
            tids = [e.transition_id for e in path] + [edge.transition_id]
            proof = self.prove(tids)
            if proof.proven_infeasible:
                pruned_keys.setdefault(self._key(tids), proof.to_dict())
                return False
            return True

        def allowed(edge: Edge) -> bool:
            return structural(edge) and feasible(edge)

        stack: list[tuple[str, list[Edge], int]] = [(initial, self.graph.out_edges(initial), 0)]
        while stack:
            state, edges, cursor = stack[-1]
            if cursor == 0 and (not edges or not any(allowed(e) for e in edges)) and path:
                if not edges:
                    reason = "TERMINAL_STATE"
                elif any(structural(e) for e in edges):
                    reason = "PRUNED_INFEASIBLE"  # mọi phần mở rộng hợp lệ về cấu trúc đều bị chứng minh vô nghiệm
                else:
                    reason = "LIMIT"
                found.append(([e.transition_id for e in path], reason))
                if len(found) > max_paths:
                    found.pop()
                    truncated = True
                    break
            if cursor >= len(edges):
                stack.pop()
                if path and stack:
                    last = path.pop()
                    visits[last.target] -= 1
                    repeats[last.transition_id] -= 1
                continue
            stack[-1] = (state, edges, cursor + 1)
            edge = edges[cursor]
            if not allowed(edge):
                continue
            path.append(edge)
            visits[edge.target] = visits.get(edge.target, 0) + 1
            repeats[edge.transition_id] = repeats.get(edge.transition_id, 0) + 1
            stack.append((edge.target, self.graph.out_edges(edge.target), 0))

        self.pruned_extensions = [{"path_key": k, "feasibility_proof": v} for k, v in pruned_keys.items()]
        paths = [self._make_positive(tids, "DFS", context, [], {"dfs_termination": reason}) for tids, reason in found]
        self.last_dfs_info = {"truncated": truncated, "pruned_extensions": len(self.pruned_extensions)}
        return self._assign_ids(paths, "P")

    # ------------------------------------------------------------------
    # Điều phối + chú thích
    # ------------------------------------------------------------------
    def generate(self, **kwargs: Any) -> GenerationResult:
        self.pruned_extensions = []
        result = super().generate(**kwargs)
        result.configuration["dfs_feasibility"] = self.dfs_feasibility
        result.configuration["api_mapping_file"] = self.api_mapping_file
        if self.dfs_feasibility == "off":
            return result
        counts = {"positive": {"PROVEN_INFEASIBLE": 0, "NOT_PROVEN_INFEASIBLE": 0},
                  "negative_prefix": {"PROVEN_INFEASIBLE": 0, "NOT_PROVEN_INFEASIBLE": 0}}
        for path in result.paths:
            proof = self.prove(path.transition_ids).to_dict()
            proof["applies_to"] = "positive_prefix" if path.is_negative else "whole_path"
            path.extra["feasibility_proof"] = proof
            counts["negative_prefix" if path.is_negative else "positive"][proof["status"]] += 1
        result.summary["paths_by_proof"] = counts
        if self.dfs_feasibility == "prune":
            result.summary["pruned_infeasible_extensions"] = len(self.pruned_extensions)
        return result

    def to_output_dict(self, result: GenerationResult) -> dict[str, Any]:
        data = super().to_output_dict(result)
        if self.dfs_feasibility != "off":
            data["feasibility_assumptions"] = list(data["feasibility_assumptions"]) + [
                "feasibility_proof=PROVEN_INFEASIBLE holds for every initial data, setup prefix, product choice and "
                "request input (efsm_feasibility abstract interpretation); NOT_PROVEN_INFEASIBLE is not a proof of feasibility.",
                "Context inputs never override variables already determined by earlier actions (rejected_input_overrides).",
            ]
        if self.dfs_feasibility == "prune":
            data["pruned_infeasible_extensions"] = self.pruned_extensions
        return data


# --------------------------------------------------------------------------
# CLI (cùng tham số với test_path_generator.py, thêm --api-mapping / --dfs-feasibility)
# --------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    parser: argparse.ArgumentParser = _build_parser()
    parser.description = ("Sinh đường đi EFSM có chứng minh vô nghiệm (annotate/prune). "
                          "Không gọi API, không dùng DB.")
    parser.add_argument("--api-mapping", default=None,
                        help="api_test_mapping.json (mặc định: cùng thư mục với model) - ngữ nghĩa chọn sản phẩm")
    parser.add_argument("--dfs-feasibility", choices=DFS_FEASIBILITY_MODES, default="annotate")
    args = parser.parse_args(argv)
    model = Path(args.model)
    rules = Path(args.rules) if args.rules else model.with_name("security_rules.json")
    mapping = Path(args.api_mapping) if args.api_mapping else model.with_name("api_test_mapping.json")
    try:
        generator = FeasibilityAwarePathGenerator(
            model, rules if rules.exists() else None, effects_policy=args.effects_policy,
            api_mapping=mapping if args.dfs_feasibility != "off" else None, dfs_feasibility=args.dfs_feasibility)
        context = PathContext.from_file(args.context) if args.context else None
        result = generator.generate(
            strategy=args.strategy, include_negative=args.include_negative, context=context,
            prefix_mode=args.prefix_mode, max_depth=args.max_depth, max_paths=args.max_paths,
            max_visits_per_state=args.max_visits_per_state, max_transition_repeats=args.max_transition_repeats,
            max_nodes=args.max_nodes,
        )
        out = TestPathGenerator.export_json(generator.to_output_dict(result), args.output)
        coverage_path = Path(args.coverage_output) if args.coverage_output else out.with_name("test_path_coverage.json")
        TestPathGenerator.export_json(generator.calculate_coverage(result.paths), coverage_path)
    except (PathGeneratorError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    summary = result.summary
    print(f"paths={summary['total_paths']} by_type={summary['paths_by_type']} feasibility={summary['paths_by_feasibility']}")
    if "paths_by_proof" in summary:
        print(f"proof={summary['paths_by_proof']}"
              + (f" pruned_extensions={summary['pruned_infeasible_extensions']}" if "pruned_infeasible_extensions" in summary else ""))
    for warning in result.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(f"wrote {out} and {coverage_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
