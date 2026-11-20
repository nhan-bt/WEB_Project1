"""
test_path_generator.py - Sinh đường đi kiểm thử (positive / negative) từ mô hình EFSM.

Độc lập với website: không truy cập database, không gửi HTTP, không dùng eval/exec.
Chỉ phụ thuộc ``guard_evaluator.GuardEvaluator`` (đúng interface hiện có) và thư viện chuẩn.

Khái niệm chính
---------------
* **Đường đi** được định danh bằng CHUỖI transition ID (``path_key`` = ``"T01>T04>!NT05"``),
  không phải chuỗi state, vì hai transition khác nhau có thể cùng source/target.
* ``structure`` : ``STRUCTURALLY_VALID`` - các transition nối tiếp đúng trên đồ thị EFSM.
* ``feasibility``: ``FEASIBLE`` / ``INFEASIBLE`` / ``UNKNOWN`` - chỉ kết luận khi có
  ``PathContext`` và đã mô phỏng guard + biến qua TỪNG bước (mọi guard thỏa mãn riêng lẻ
  là chưa đủ). ``INFEASIBLE`` luôn kèm ``proof_scope = GIVEN_CONTEXT``: chỉ chứng minh cho
  context đã cung cấp, không phải mọi dữ liệu có thể có.
* Mô phỏng chỉ cập nhật BIẾN EFSM từ action ``assign``; action ``effect`` (tác động lên
  backend/DB) KHÔNG được thực thi và được liệt kê trong ``unsimulated_effects``.
* Negative path = positive prefix ngắn nhất + đúng một negative transition ở cuối.
  Negative transition KHÔNG bao giờ là cạnh của đồ thị dùng để tìm prefix.
* Executed coverage luôn là ``NOT_MEASURED``: module này không thực thi API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from guard_evaluator import GuardEvaluator, GuardEvaluatorError, GuardStatus

__all__ = [
    "TestPathGenerator",
    "EfsmGraph",
    "Edge",
    "PathContext",
    "GeneratedPath",
    "FeasibilityResult",
    "GenerationResult",
    "Feasibility",
    "NegativeCategory",
    "PathGeneratorError",
    "ModelFormatError",
    "PathStructureError",
    "ContextError",
    "main",
]

SCHEMA_VERSION = "1.0"
GENERATOR_NAME = "TestPathGenerator"
STRUCTURALLY_VALID = "STRUCTURALLY_VALID"
NOT_MEASURED = "NOT_MEASURED"
_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


# --------------------------------------------------------------------------
# Lỗi và kiểu liệt kê
# --------------------------------------------------------------------------
class PathGeneratorError(Exception):
    """Lớp cơ sở cho lỗi của module."""


class ModelFormatError(PathGeneratorError):
    """Mô hình EFSM thiếu trường cần thiết hoặc không nhất quán."""


class PathStructureError(PathGeneratorError):
    """Chuỗi transition không phải đường đi hợp lệ trên đồ thị."""


class ContextError(PathGeneratorError):
    """File/đối tượng runtime context sai định dạng."""


class Feasibility(StrEnum):
    """Khả năng thực thi của đường đi (xem docstring module)."""

    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"


class NegativeCategory(StrEnum):
    """Nhãn phân loại negative path (một path có thể có nhiều nhãn)."""

    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    GUARD_VIOLATION = "GUARD_VIOLATION"
    BUSINESS_CONSTRAINT_VIOLATION = "BUSINESS_CONSTRAINT_VIOLATION"
    INPUT_VALIDATION_VIOLATION = "INPUT_VALIDATION_VIOLATION"


_CATEGORY_ORDER = [
    NegativeCategory.INVALID_STATE_TRANSITION,
    NegativeCategory.BUSINESS_CONSTRAINT_VIOLATION,
    NegativeCategory.INPUT_VALIDATION_VIOLATION,
    NegativeCategory.GUARD_VIOLATION,
]


# --------------------------------------------------------------------------
# Đồ thị EFSM
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Edge:
    """Một cạnh có hướng = một positive transition (multigraph, cho phép self-loop)."""

    transition_id: str
    source: str
    target: str
    event: str | None
    order: int  # thứ tự trong mô hình, dùng để tie-break ổn định


class EfsmGraph:
    """Đa đồ thị có hướng: state là đỉnh, positive transition là cạnh.

    Giữ nguyên self-loop, cạnh song song và chu trình. Thứ tự duyệt luôn theo thứ tự
    khai báo trong mô hình nên kết quả có thể tái lập.
    """

    def __init__(self, states: Sequence[str], initial_state: str, edges: Sequence[Edge]) -> None:
        if initial_state not in states:
            raise ModelFormatError(f"initial state {initial_state!r} is not a declared state")
        self.states: list[str] = list(states)
        self.initial_state = initial_state
        self.edges: list[Edge] = list(edges)
        self._by_id = {e.transition_id: e for e in self.edges}
        self._out: dict[str, list[Edge]] = {s: [] for s in self.states}
        self._in: dict[str, list[Edge]] = {s: [] for s in self.states}
        for edge in self.edges:
            self._out[edge.source].append(edge)
            self._in[edge.target].append(edge)

    def edge(self, transition_id: str) -> Edge:
        try:
            return self._by_id[transition_id]
        except KeyError:
            raise PathStructureError(f"unknown transition id {transition_id!r}") from None

    def out_edges(self, state: str) -> list[Edge]:
        return list(self._out[state])

    def in_edges(self, state: str) -> list[Edge]:
        return list(self._in[state])

    def reachable_states(self) -> list[str]:
        """State đạt được từ initial state (theo thứ tự khai báo)."""
        seen = {self.initial_state}
        queue = deque([self.initial_state])
        while queue:
            for edge in self._out[queue.popleft()]:
                if edge.target not in seen:
                    seen.add(edge.target)
                    queue.append(edge.target)
        return [s for s in self.states if s in seen]

    def unreachable_states(self) -> list[str]:
        reachable = set(self.reachable_states())
        return [s for s in self.states if s not in reachable]

    def reachable_edges(self) -> list[Edge]:
        reachable = set(self.reachable_states())
        return [e for e in self.edges if e.source in reachable]

    def dead_ends(self) -> list[str]:
        """State không có cạnh đi ra."""
        return [s for s in self.states if not self._out[s]]

    def self_loops(self) -> list[Edge]:
        return [e for e in self.edges if e.source == e.target]

    def parallel_edges(self) -> dict[tuple[str, str], list[str]]:
        """Các cặp (source, target) có từ 2 transition trở lên."""
        groups: dict[tuple[str, str], list[str]] = {}
        for edge in self.edges:
            groups.setdefault((edge.source, edge.target), []).append(edge.transition_id)
        return {k: v for k, v in groups.items() if len(v) > 1}

    def has_cycle(self) -> bool:
        """True nếu có chu trình (kể cả self-loop). Duyệt lặp, không đệ quy."""
        color: dict[str, int] = {}
        for root in self.states:
            if color.get(root):
                continue
            color[root] = 1
            stack = [(root, iter(self._out[root]))]
            while stack:
                state, edges = stack[-1]
                advanced = False
                for edge in edges:
                    state_color = color.get(edge.target, 0)
                    if state_color == 1:
                        return True
                    if state_color == 0:
                        color[edge.target] = 1
                        stack.append((edge.target, iter(self._out[edge.target])))
                        advanced = True
                        break
                if not advanced:
                    color[state] = 2
                    stack.pop()
        return False

    def shortest_path(self, target_state: str) -> list[Edge] | None:
        """BFS: đường đi ngắn nhất (theo số transition) từ initial tới ``target_state``."""
        if target_state not in self._out:
            raise PathStructureError(f"unknown state {target_state!r}")
        parent: dict[str, Edge | None] = {self.initial_state: None}
        queue = deque([self.initial_state])
        while queue:
            state = queue.popleft()
            if state == target_state:
                break
            for edge in self._out[state]:
                if edge.target not in parent:
                    parent[edge.target] = edge
                    queue.append(edge.target)
        if target_state not in parent:
            return None
        path: list[Edge] = []
        cursor = target_state
        while parent[cursor] is not None:
            edge = parent[cursor]
            assert edge is not None
            path.append(edge)
            cursor = edge.source
        path.reverse()
        return path

    def shortest_path_to_edge(self, transition_id: str) -> list[Edge] | None:
        """Đường đi ngắn nhất KẾT THÚC bằng transition ``transition_id``."""
        edge = self.edge(transition_id)
        prefix = self.shortest_path(edge.source)
        return None if prefix is None else prefix + [edge]


# --------------------------------------------------------------------------
# Runtime context
# --------------------------------------------------------------------------
@dataclass
class PathContext:
    """Dữ liệu runtime dùng để đánh giá tính khả thi.

    * ``initial_variables``: biến trạng thái ở state khởi đầu (ví dụ giỏ rỗng).
    * ``default_inputs``   : đầu vào áp dụng cho mọi bước.
    * ``step_inputs``      : đầu vào theo transition ID, hoặc theo số thứ tự bước ("2").
    * ``negative_inputs``  : đầu vào của negative transition (theo NT ID).

    Biến có ``origin == "request"`` là đầu vào từng bước và KHÔNG được mang sang bước sau.
    """

    initial_variables: dict[str, Any] = field(default_factory=dict)
    default_inputs: dict[str, Any] = field(default_factory=dict)
    step_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    negative_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)

    _KEYS = ("initial_variables", "default_inputs", "step_inputs", "negative_inputs")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PathContext:
        if not isinstance(data, Mapping):
            raise ContextError("context must be a JSON object")
        unknown = set(data) - set(cls._KEYS) - {"description", "$comment"}
        if unknown:
            raise ContextError(f"unknown context key(s): {sorted(unknown)}")
        for key in ("initial_variables", "default_inputs"):
            if not isinstance(data.get(key, {}), Mapping):
                raise ContextError(f"'{key}' must be an object")
        for key in ("step_inputs", "negative_inputs"):
            value = data.get(key, {})
            if not isinstance(value, Mapping) or not all(isinstance(v, Mapping) for v in value.values()):
                raise ContextError(f"'{key}' must map ids to objects")
        return cls(
            initial_variables=dict(data.get("initial_variables", {})),
            default_inputs=dict(data.get("default_inputs", {})),
            step_inputs={k: dict(v) for k, v in data.get("step_inputs", {}).items()},
            negative_inputs={k: dict(v) for k, v in data.get("negative_inputs", {}).items()},
        )

    @classmethod
    def from_file(cls, path: str | Path) -> PathContext:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ContextError(f"context file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ContextError(f"context file is not valid JSON: {exc}") from exc
        return cls.from_dict(data)

    def inputs_for(self, step_index: int, transition_id: str) -> dict[str, Any]:
        """Đầu vào của một bước; khóa theo số bước ưu tiên hơn khóa theo transition ID."""
        merged = dict(self.default_inputs)
        merged.update(self.step_inputs.get(transition_id, {}))
        merged.update(self.step_inputs.get(str(step_index), {}))
        return merged

    def fingerprint(self) -> str:
        payload = {k: getattr(self, k) for k in self._KEYS}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Kết quả
# --------------------------------------------------------------------------
@dataclass
class FeasibilityResult:
    """Kết quả đánh giá tính khả thi của một đường đi."""

    status: Feasibility
    reason_code: str
    reason: str
    proof_scope: str | None = None
    failed_step: int | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    final_state: str | None = None
    final_variables: dict[str, Any] | None = None
    negative_check: dict[str, Any] | None = None
    assumptions: list[str] = field(default_factory=list)
    witness: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "proof_scope": self.proof_scope,
            "failed_step": self.failed_step,
            "assumptions": self.assumptions,
            "steps": self.steps,
            "final_state": self.final_state,
            "final_variables": self.final_variables,
            "negative_check": self.negative_check,
            "witness": self.witness,
        }


@dataclass
class GeneratedPath:
    """Một đường đi kiểm thử (positive hoặc negative)."""

    path_id: str
    path_type: str  # "positive" | "negative"
    strategy: str  # "BFS" | "DFS" | "NEGATIVE"
    path_key: str
    transition_ids: list[str]
    states: list[str]
    steps: list[dict[str, Any]]
    feasibility: FeasibilityResult
    security_rule_ids: list[str]
    required_context: dict[str, Any]
    expected_outcome: str
    strategies: list[str] = field(default_factory=list)
    goals: list[dict[str, str]] = field(default_factory=list)
    severity: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_negative(self) -> bool:
        return self.path_type == "negative"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "path_id": self.path_id,
            "path_type": self.path_type,
            "strategy": self.strategy,
            "strategies": self.strategies or [self.strategy],
            "path_key": self.path_key,
            "goals": self.goals,
            "transition_ids": self.transition_ids,
            "states": self.states,
            "structure": STRUCTURALLY_VALID,
            "feasibility": self.feasibility.status.value,
            "feasibility_detail": {k: v for k, v in self.feasibility.to_dict().items() if k != "assumptions"},
            "security_rule_ids": self.security_rule_ids,
            "severity": self.severity,
            "required_context": self.required_context,
            "expected_outcome": self.expected_outcome,
            "steps": self.steps,
            "execution": {"status": "NOT_EXECUTED"},
        }
        data.update(self.extra)
        return data


@dataclass
class GenerationResult:
    """Kết quả ``TestPathGenerator.generate``."""

    paths: list[GeneratedPath]
    summary: dict[str, Any]
    configuration: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Bộ sinh
# --------------------------------------------------------------------------
class TestPathGenerator:
    """Sinh đường đi kiểm thử từ ``efsm_model.json`` (hoặc mô hình cùng schema)."""

    __test__ = False  # không để pytest coi đây là lớp test

    def __init__(
        self,
        model_path: str | Path,
        rules_path: str | Path | None = None,
        *,
        evaluator: GuardEvaluator | None = None,
        effects_policy: str = "model_contract",
        input_validation_statuses: Iterable[int] = (422,),
    ) -> None:
        """
        ``effects_policy``:
          * ``model_contract`` (mặc định): action ``effect`` được coi là phần lưu trữ của các
            ``assign`` cùng transition (hợp đồng của mô hình); FEASIBLE nghĩa là khả thi trên
            mô phỏng biến EFSM.
          * ``strict``: nếu đường đi có bất kỳ ``effect`` nào chưa mô phỏng thì tối đa UNKNOWN.
        ``input_validation_statuses``: HTTP status coi là lỗi validate đầu vào (FastAPI: 422).
        """
        if effects_policy not in ("model_contract", "strict"):
            raise ValueError("effects_policy must be 'model_contract' or 'strict'")
        self.model_path = Path(model_path)
        self.effects_policy = effects_policy
        self.input_validation_statuses = frozenset(input_validation_statuses)
        try:
            self.evaluator = evaluator or GuardEvaluator(self.model_path)
        except GuardEvaluatorError as exc:
            raise ModelFormatError(f"cannot load model: {exc}") from exc
        self.model: dict[str, Any] = self.evaluator.model
        self.model_sha256 = hashlib.sha256(self.model_path.read_bytes()).hexdigest() if self.model_path.exists() else None
        self.rules: dict[str, dict[str, Any]] = {}
        self.excluded_rules: dict[str, dict[str, Any]] = {}
        self.rules_path = Path(rules_path) if rules_path else None
        if self.rules_path is not None:
            self._load_rules(self.rules_path)
        self._transitions: dict[str, dict[str, Any]] = {t["id"]: t for t in self.model["transitions"]}
        self._negatives: dict[str, dict[str, Any]] = {
            n["id"]: n for n in self.model.get("negative_transitions", [])
        }
        self.graph = self.build_graph()
        self._var_cache: dict[str, dict[str, list[str]]] = {}
        self._explore_cache: dict[tuple, dict[str, Any]] = {}
        self.last_bfs_info: dict[str, Any] = {}
        self.last_dfs_info: dict[str, Any] = {}
        self.last_negative_skipped: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # Nạp dữ liệu
    # ------------------------------------------------------------------
    def _load_rules(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ModelFormatError(f"security rules file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ModelFormatError(f"security rules file is not valid JSON: {exc}") from exc
        self.rules = {r["rule_id"]: r for r in data.get("rules", []) if "rule_id" in r}
        self.excluded_rules = {r["rule_id"]: r for r in data.get("excluded_rules", []) if "rule_id" in r}

    def build_graph(self) -> EfsmGraph:
        """Dựng đồ thị từ ``states`` và ``transitions`` (negative transition bị loại trừ)."""
        states_raw = self.model.get("states")
        if not isinstance(states_raw, list) or not states_raw:
            raise ModelFormatError("model needs a non-empty 'states' list")
        states = [s["id"] for s in states_raw]
        initial = self.model.get("initial_state")
        if initial is None:
            initials = [s["id"] for s in states_raw if s.get("type") == "initial"]
            if len(initials) != 1:
                raise ModelFormatError("model needs 'initial_state' or exactly one state with type 'initial'")
            initial = initials[0]
        edges = []
        for order, tr in enumerate(self.model["transitions"]):
            for key in ("id", "source", "target"):
                if key not in tr:
                    raise ModelFormatError(f"transition #{order} is missing '{key}'")
            edges.append(Edge(tr["id"], tr["source"], tr["target"], tr.get("event"), order))
        return EfsmGraph(states, initial, edges)

    # ------------------------------------------------------------------
    # Tiện ích mô hình
    # ------------------------------------------------------------------
    def _origin(self, variable: str) -> str | None:
        spec = self.evaluator.get_variable_spec(variable)
        return spec.get("origin") if spec else None

    def _transition_vars(self, tid: str) -> dict[str, list[str]]:
        if tid not in self._var_cache:
            self._var_cache[tid] = self.evaluator.get_transition_variables(tid)
        return self._var_cache[tid]

    def _effects(self, tid: str) -> list[str]:
        out = []
        for action in self._transitions[tid].get("actions", []):
            if isinstance(action, dict) and action.get("type") == "effect":
                out.append(str(action.get("description")))
            elif isinstance(action, str) and ":=" not in action:
                out.append(action)
        return out

    def _unconditional_writes(self, tid: str) -> set[str]:
        written = set()
        for action in self._transitions[tid].get("actions", []):
            if isinstance(action, dict) and action.get("type") == "assign" and action.get("when") is None:
                written.add(action["var"])
        return written

    def _drop_request_vars(self, variables: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in variables.items() if self._origin(k) != "request"}

    def _rule_ids_for(self, tids: Iterable[str]) -> list[str]:
        seen: dict[str, None] = {}
        for tid in tids:
            for rule in self._transitions[tid].get("rule_refs", []):
                seen.setdefault(rule, None)
        return list(seen)

    def _severity(self, rule_ids: Iterable[str]) -> str | None:
        ranks = [(_SEVERITY_RANK.get(str(self.rules[r].get("severity", "")).upper(), 0), self.rules[r]["severity"])
                 for r in rule_ids if r in self.rules and self.rules[r].get("severity")]
        return max(ranks)[1] if ranks else None

    @staticmethod
    def _key(tids: Sequence[str], negative: str | None = None) -> str:
        key = ">".join(tids)
        if not negative:
            return key
        return f"{key}>!{negative}" if key else f"!{negative}"

    def _edges_for(self, tids: Sequence[str]) -> list[Edge]:
        """Kiểm tra chuỗi transition là đường đi hợp lệ từ initial state."""
        state = self.graph.initial_state
        edges = []
        for index, tid in enumerate(tids, start=1):
            edge = self.graph.edge(tid)
            if edge.source != state:
                raise PathStructureError(
                    f"step {index}: transition {tid} starts at {edge.source}, but the path is at {state}"
                )
            edges.append(edge)
            state = edge.target
        return edges

    def _states_of(self, edges: Sequence[Edge]) -> list[str]:
        return [self.graph.initial_state] + [e.target for e in edges]

    # ------------------------------------------------------------------
    # Context cần thiết (phân tích tĩnh)
    # ------------------------------------------------------------------
    def _required_context(self, tids: Sequence[str], negative: str | None = None) -> dict[str, Any]:
        """Biến cần có để đánh giá đường đi, trừ những biến đã được gán vô điều kiện ở bước trước."""
        written: set[str] = set()
        initial: list[str] = []
        per_step: dict[str, list[str]] = {}
        for index, tid in enumerate(tids, start=1):
            for name in self._transition_vars(tid)["required_inputs"]:
                if name in written:
                    continue
                if self._origin(name) == "request":
                    per_step.setdefault(str(index), [])
                    if name not in per_step[str(index)]:
                        per_step[str(index)].append(name)
                elif name not in initial:
                    initial.append(name)
            written |= self._unconditional_writes(tid)
        negative_inputs: list[str] = []
        if negative:
            negative_inputs = self._negative_variables(negative)
            for name in negative_inputs:
                if self._origin(name) != "request" and name not in written and name not in initial:
                    initial.append(name)
        return {
            "initial_variables": sorted(initial),
            "step_inputs": {k: sorted(v) for k, v in per_step.items()},
            "negative_inputs": sorted(n for n in negative_inputs if self._origin(n) == "request"),
        }

    def _negative_variables(self, negative_id: str) -> list[str]:
        cond = self._negatives[negative_id]["invalid_condition"]
        return sorted(self.evaluator.get_guard_variables({"expr": cond}))

    # ------------------------------------------------------------------
    # Mô phỏng một bước
    # ------------------------------------------------------------------
    def _simulate_step(
        self, state: str, carry: Mapping[str, Any], tid: str, inputs: Mapping[str, Any], index: int
    ) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
        """Mô phỏng một transition. Trả về (bản ghi, verdict, state mới, biến mang sang bước sau).

        verdict: ``OK`` | ``INFEASIBLE`` | ``UNKNOWN`` | ``INVALID_STRUCTURE``.
        """
        variables = {**carry, **inputs}
        outcome = self.evaluator.apply_transition(tid, state, variables, verify_invariants=True)
        check = outcome.check
        record: dict[str, Any] = {
            "step_index": index,
            "transition_id": tid,
            "state_before": state,
            "guard_status": check.status.value,
            "applied": outcome.applied,
            "inputs": dict(inputs),
            "failed_conditions": check.failed_conditions,
            "missing_variables": outcome.missing_variables or check.missing_variables,
            "unsimulated_effects": outcome.backend_effects,
        }
        if not outcome.applied:
            status = outcome.error_status or check.status
            if check.enabled:  # guard đúng nhưng action không mô phỏng được
                record.update(verdict="UNKNOWN", code=f"ACTION_{status.value}", reason=outcome.error)
                return record, "UNKNOWN", state, dict(carry)
            if status is GuardStatus.GUARD_NOT_SATISFIED:
                record.update(verdict="INFEASIBLE", code="GUARD_NOT_SATISFIED", reason=check.reason)
                return record, "INFEASIBLE", state, dict(carry)
            if status is GuardStatus.STATE_MISMATCH:
                record.update(verdict="INVALID_STRUCTURE", code="STATE_MISMATCH", reason=check.reason)
                return record, "INVALID_STRUCTURE", state, dict(carry)
            record.update(verdict="UNKNOWN", code=status.value, reason=outcome.error or check.reason)
            return record, "UNKNOWN", state, dict(carry)

        report = outcome.invariant_report
        record["state_after"] = outcome.new_state
        record["invariants"] = report.summary() if report else None
        if report and report.violations:
            record.update(
                verdict="INFEASIBLE", code="INVARIANT_VIOLATED",
                reason="; ".join(f"{r.invariant_id}: {r.reason}" for r in report.violations),
                invariant_violations=[r.invariant_id for r in report.violations],
            )  # fmt: skip
            return record, "INFEASIBLE", outcome.new_state, self._drop_request_vars(outcome.new_variables)
        if report and report.errors:
            record.update(
                verdict="UNKNOWN", code="INVARIANT_ERROR",
                reason="; ".join(f"{r.invariant_id}: {r.reason}" for r in report.errors),
            )  # fmt: skip
            return record, "UNKNOWN", outcome.new_state, self._drop_request_vars(outcome.new_variables)
        record["verdict"] = "OK"
        return record, "OK", outcome.new_state, self._drop_request_vars(outcome.new_variables)

    # ------------------------------------------------------------------
    # Tính khả thi
    # ------------------------------------------------------------------
    def evaluate_path_feasibility(
        self,
        transition_ids: Sequence[str],
        context: PathContext | None = None,
        *,
        negative_transition_id: str | None = None,
    ) -> FeasibilityResult:
        """Đánh giá đường đi bằng cách mô phỏng từng bước với GuardEvaluator.

        * Không có context -> ``UNKNOWN`` (``NO_RUNTIME_CONTEXT``).
        * ``FEASIBLE`` chỉ khi MỌI bước: guard thỏa mãn với biến đã cập nhật từ các bước trước,
          action gán mô phỏng được, và không invariant nào bị vi phạm (và, với negative path,
          negative transition thực sự kích hoạt được).
        * Thiếu biến / sai kiểu / action không mô phỏng được -> ``UNKNOWN``, không bao giờ FEASIBLE.
        * Guard sai hoặc invariant bị vi phạm -> ``INFEASIBLE`` với ``proof_scope=GIVEN_CONTEXT``.
        """
        tids = list(transition_ids)
        self._edges_for(tids)  # ném PathStructureError nếu sai cấu trúc
        if negative_transition_id is not None and negative_transition_id not in self._negatives:
            raise PathStructureError(f"unknown negative transition {negative_transition_id!r}")
        assumptions = [
            "Only EFSM variables are simulated (from 'assign' actions); the backend and database are NOT touched.",
            "Backend 'effect' actions are listed in unsimulated_effects and were NOT executed.",
        ]
        if context is None:
            return FeasibilityResult(
                Feasibility.UNKNOWN, "NO_RUNTIME_CONTEXT",
                "No runtime context was supplied; guards cannot be evaluated.", assumptions=assumptions,
            )  # fmt: skip
        assumptions.append("Verdicts hold for the supplied context only (proof_scope=GIVEN_CONTEXT).")
        if self.effects_policy == "model_contract":
            assumptions.append(
                "effects_policy=model_contract: every EFSM variable changed by a backend effect is also "
                "assigned by an 'assign' action of the same transition."
            )
        else:
            assumptions.append("effects_policy=strict: any unsimulated effect limits the verdict to UNKNOWN.")

        state = self.graph.initial_state
        carry = dict(context.initial_variables)
        steps: list[dict[str, Any]] = []
        effects_before = 0
        witness_steps: list[dict[str, Any]] = []

        def finish(status: Feasibility, code: str, reason: str, failed: int | None = None) -> FeasibilityResult:
            scope = "GIVEN_CONTEXT" if status is not Feasibility.UNKNOWN else None
            return FeasibilityResult(
                status, code, reason, scope, failed, steps, state, dict(carry), None, assumptions,
                {"initial_variables": dict(context.initial_variables), "step_inputs": witness_steps}
                if status is Feasibility.FEASIBLE else None,
            )  # fmt: skip

        for index, tid in enumerate(tids, start=1):
            inputs = context.inputs_for(index, tid)
            record, verdict, new_state, new_carry = self._simulate_step(state, carry, tid, inputs, index)
            steps.append(record)
            if verdict == "INVALID_STRUCTURE":
                raise PathStructureError(record["reason"])
            if verdict != "OK":
                strict_blocked = self.effects_policy == "strict" and effects_before > 0
                if verdict == "INFEASIBLE" and not strict_blocked:
                    return finish(Feasibility.INFEASIBLE, record["code"], f"step {index} ({tid}): {record['reason']}", index)
                code = "UNCONCLUSIVE_AFTER_UNSIMULATED_EFFECTS" if verdict == "INFEASIBLE" else record["code"]
                return finish(Feasibility.UNKNOWN, code, f"step {index} ({tid}): {record['reason']}", index)
            effects_before += len(record["unsimulated_effects"])
            witness_steps.append({"step_index": index, "transition_id": tid, "inputs": dict(inputs)})
            state, carry = new_state, new_carry

        negative_check = None
        if negative_transition_id is not None:
            negative_check = self._check_negative_step(negative_transition_id, state, carry, context)
            if negative_check["status"] == "NEGATIVE_APPLICABLE":
                pass
            elif negative_check["status"] == "NEGATIVE_NOT_APPLICABLE":
                result = finish(Feasibility.INFEASIBLE, "NEGATIVE_CONDITION_NOT_TRIGGERED",
                                f"{negative_transition_id}: {negative_check['reason']}", len(tids) + 1)
                result.negative_check = negative_check
                return result
            else:
                code = negative_check["reason_code"]
                result = finish(Feasibility.UNKNOWN, code, f"{negative_transition_id}: {negative_check['reason']}", len(tids) + 1)
                result.negative_check = negative_check
                return result
            witness_steps.append({"step_index": len(tids) + 1, "negative_transition_id": negative_transition_id,
                                  "inputs": dict(negative_check["inputs"])})

        if self.effects_policy == "strict" and effects_before > 0:
            result = finish(Feasibility.UNKNOWN, "EFFECTS_NOT_SIMULATED",
                            f"{effects_before} backend effect(s) on the path are not simulated (strict policy).")
        elif negative_check is not None:
            result = finish(Feasibility.FEASIBLE, "PREFIX_FEASIBLE_NEGATIVE_APPLICABLE",
                            f"The positive prefix is feasible and the invalid condition of {negative_transition_id} holds.")
        else:
            result = finish(Feasibility.FEASIBLE, "ALL_STEPS_SIMULATED",
                            "Every guard holds with the simulated variables and no invariant is violated.")
        result.negative_check = negative_check
        return result

    def _check_negative_step(
        self, negative_id: str, state: str, carry: Mapping[str, Any], context: PathContext
    ) -> dict[str, Any]:
        """Kiểm tra negative transition ở cuối đường đi và guard nào bị vi phạm."""
        negative = self._negatives[negative_id]
        inputs = dict(context.negative_inputs.get(negative_id, {}))
        variables = {**carry, **context.default_inputs, **inputs}
        check = self.evaluator.check_negative_transition(negative_id, state, variables)
        info: dict[str, Any] = {
            "status": check.status.value,
            "reason_code": check.status.value,
            "reason": check.reason,
            "inputs": inputs,
            "missing_variables": check.missing_variables,
            "violated_transition_guards": [],
        }
        if negative_id not in context.negative_inputs and check.status is GuardStatus.MISSING_VARIABLE:
            info["reason_code"] = "NEGATIVE_INPUT_NOT_PROVIDED"
        for tid in self._positives_for(negative["event"], negative["source"]):
            info["violated_transition_guards"].append(self._guard_violation_report(tid, state, variables))
        return info

    def _guard_violation_report(self, tid: str, state: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        """Điều kiện nào của guard transition ``tid`` bị vi phạm với ``variables``.

        Guard dạng ``all`` được đánh giá từng điều kiện con: chỉ cần một điều kiện con sai là cả
        guard sai, kể cả khi các điều kiện khác chưa đánh giá được (thiếu biến); số điều kiện chưa
        đánh giá được ghi ở ``unevaluated_conditions``.
        """
        check = self.evaluator.check_transition(tid, state, variables)
        guard = self._transitions[tid].get("guard")
        children = guard["all"] if isinstance(guard, dict) and set(guard) == {"all"} else [guard]
        failed: list[dict[str, Any]] = []
        unevaluated = 0
        for child in children:
            result = self.evaluator.evaluate_guard(child, variables)
            if result.status is GuardStatus.GUARD_NOT_SATISFIED:
                failed.extend(result.failed_conditions)
            elif result.status is not GuardStatus.GUARD_SATISFIED:
                unevaluated += 1
        status = GuardStatus.GUARD_NOT_SATISFIED.value if failed else check.status.value
        return {
            "transition_id": tid,
            "status": status,
            "failed_conditions": failed,
            "unevaluated_conditions": unevaluated,
        }

    def _positives_for(self, event: str | None, source: str | None = None) -> list[str]:
        return [
            t["id"] for t in self.model["transitions"]
            if t.get("event") == event and (source is None or t["source"] == source)
        ]

    # ------------------------------------------------------------------
    # Tìm prefix khả thi (BFS trên cấu hình = state + biến)
    # ------------------------------------------------------------------
    def explore_feasible(
        self, context: PathContext, *, max_depth: int = 12, max_nodes: int = 5000
    ) -> dict[str, Any]:
        """BFS trên (state, biến) bằng mô phỏng; chỉ mở rộng bước ``OK`` đã chứng minh.

        Trả về đường đi KHẢ THI ngắn nhất tới từng state (``state_paths``) và tới từng
        transition (``edge_paths``), cùng thống kê. Mỗi bước dùng ``context.inputs_for``.
        """
        key = (context.fingerprint(), max_depth, max_nodes, self.effects_policy)
        if key in self._explore_cache:
            return self._explore_cache[key]
        initial = (self.graph.initial_state, dict(context.initial_variables))
        state_paths: dict[str, list[str]] = {self.graph.initial_state: []}
        edge_paths: dict[str, list[str]] = {}
        visited = {self._config_key(*initial)}
        queue: deque[tuple[str, dict[str, Any], list[str]]] = deque([(initial[0], initial[1], [])])
        nodes, truncated = 1, False
        while queue:
            state, carry, path = queue.popleft()
            if len(path) >= max_depth:
                continue
            for edge in self.graph.out_edges(state):
                index = len(path) + 1
                _, verdict, new_state, new_carry = self._simulate_step(
                    state, carry, edge.transition_id, context.inputs_for(index, edge.transition_id), index
                )
                if verdict != "OK":
                    continue
                new_path = path + [edge.transition_id]
                edge_paths.setdefault(edge.transition_id, new_path)
                state_paths.setdefault(new_state, new_path)
                config = self._config_key(new_state, new_carry)
                if config in visited:
                    continue
                if nodes >= max_nodes:
                    truncated = True
                    continue
                visited.add(config)
                nodes += 1
                queue.append((new_state, new_carry, new_path))
        result = {"state_paths": state_paths, "edge_paths": edge_paths, "nodes": nodes, "truncated": truncated}
        self._explore_cache[key] = result
        return result

    @staticmethod
    def _config_key(state: str, carry: Mapping[str, Any]) -> tuple[str, str]:
        return state, json.dumps(carry, sort_keys=True, default=str)

    # ------------------------------------------------------------------
    # Dựng bản ghi đường đi
    # ------------------------------------------------------------------
    def _positive_step(self, index: int, edge: Edge) -> dict[str, Any]:
        tr = self._transitions[edge.transition_id]
        info = self._transition_vars(edge.transition_id)
        step: dict[str, Any] = {
            "step_index": index,
            "kind": "transition",
            "transition_id": edge.transition_id,
            "event": edge.event,
            "from_state": edge.source,
            "to_state": edge.target,
            "expected_outcome": "ACCEPT",
            "expected_http_status": tr.get("expected_http_status"),
            "required_inputs": info["required_inputs"],
            "rule_ids": list(tr.get("rule_refs", [])),
        }
        # guard, guard_reads, action_writes, unsimulated_effects, api: tra cứu theo transition_id
        # trong ``transition_catalog`` của file xuất (không lặp lại ở từng bước).
        return step

    def _make_positive(
        self, tids: Sequence[str], strategy: str, context: PathContext | None, goals: list[dict[str, str]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> GeneratedPath:
        edges = self._edges_for(tids)
        rule_ids = self._rule_ids_for(tids)
        return GeneratedPath(
            path_id="",
            path_type="positive",
            strategy=strategy,
            path_key=self._key(tids),
            transition_ids=list(tids),
            states=self._states_of(edges),
            steps=[self._positive_step(i, e) for i, e in enumerate(edges, start=1)],
            feasibility=self.evaluate_path_feasibility(tids, context),
            security_rule_ids=rule_ids,
            required_context=self._required_context(tids),
            expected_outcome="ACCEPT_ALL_STEPS",
            strategies=[strategy],
            goals=goals or [],
            severity=self._severity(rule_ids),
            extra=extra or {},
        )

    @staticmethod
    def _assign_ids(paths: list[GeneratedPath], prefix: str) -> list[GeneratedPath]:
        for number, path in enumerate(paths, start=1):
            path.path_id = f"{prefix}{number:03d}"
        return paths

    # ------------------------------------------------------------------
    # BFS
    # ------------------------------------------------------------------
    def _resolve_prefix_mode(self, prefix_mode: str, context: PathContext | None) -> str:
        if prefix_mode not in ("auto", "structural", "feasible"):
            raise ValueError("prefix_mode must be 'auto', 'structural' or 'feasible'")
        if prefix_mode == "feasible" and context is None:
            raise ValueError("prefix_mode='feasible' needs a PathContext")
        if prefix_mode == "auto":
            return "feasible" if context is not None else "structural"
        return prefix_mode

    def generate_bfs_paths(
        self,
        context: PathContext | None = None,
        *,
        prefix_mode: str = "auto",
        max_paths: int | None = None,
        max_depth: int = 12,
        max_nodes: int = 5000,
        prune_prefixes: bool = True,
    ) -> list[GeneratedPath]:
        """Đường đi NGẮN NHẤT tới từng state và từng transition (theo số transition).

        ``prefix_mode``: ``structural`` = BFS trên đồ thị; ``feasible`` = BFS trên (state, biến) bằng
        mô phỏng guard (cần context); ``auto`` = feasible nếu có context. Đích không có đường khả thi
        trong giới hạn sẽ quay về đường cấu trúc ngắn nhất (nhãn feasibility cho biết kết quả).
        ``max_depth``: độ dài tối đa (số transition); đích cần đường dài hơn bị bỏ và đếm ở
        ``last_bfs_info["targets_over_max_depth"]``.
        ``prune_prefixes``: bỏ đường đi là tiền tố thực sự của đường khác (độ bao phủ không đổi).
        """
        mode = self._resolve_prefix_mode(prefix_mode, context)
        explored = self.explore_feasible(context, max_depth=max_depth, max_nodes=max_nodes) if mode == "feasible" else None
        targets: list[tuple[dict[str, str], list[str] | None]] = []
        for state in self.graph.reachable_states():
            if state == self.graph.initial_state:
                continue
            path = explored["state_paths"].get(state) if explored else None
            if path is None:
                structural = self.graph.shortest_path(state)
                path = [e.transition_id for e in structural] if structural is not None else None
            targets.append(({"type": "state", "id": state}, path))
        for edge in self.graph.reachable_edges():
            path = explored["edge_paths"].get(edge.transition_id) if explored else None
            if path is None:
                structural = self.graph.shortest_path_to_edge(edge.transition_id)
                path = [e.transition_id for e in structural] if structural is not None else None
            targets.append(({"type": "transition", "id": edge.transition_id}, path))

        unique: dict[str, dict[str, Any]] = {}
        over_depth = 0
        for goal, path in targets:
            if path is None:
                continue
            if len(path) > max_depth:
                over_depth += 1
                continue
            entry = unique.setdefault(self._key(path), {"tids": path, "goals": [], "order": len(unique)})
            entry["goals"].append(goal)
        entries = list(unique.values())
        pruned = 0
        if prune_prefixes:
            kept = []
            for entry in entries:
                tids = entry["tids"]
                if any(len(o["tids"]) > len(tids) and o["tids"][: len(tids)] == tids for o in entries):
                    pruned += 1
                    continue
                kept.append(entry)
            entries = kept
        entries.sort(key=lambda e: (len(e["tids"]), e["order"]))
        truncated = max_paths is not None and len(entries) > max_paths
        if truncated:
            entries = entries[:max_paths]
        paths = [
            self._make_positive(e["tids"], "BFS", context, e["goals"], {"prefix_mode": mode}) for e in entries
        ]
        self.last_bfs_info = {"pruned_prefix_paths": pruned, "truncated": truncated, "prefix_mode": mode,
                              "targets_over_max_depth": over_depth,
                              "explore_nodes": explored["nodes"] if explored else None,
                              "explore_truncated": explored["truncated"] if explored else False}
        return self._assign_ids(paths, "P")

    # ------------------------------------------------------------------
    # DFS có giới hạn
    # ------------------------------------------------------------------
    def generate_dfs_paths(
        self,
        context: PathContext | None = None,
        *,
        max_depth: int = 8,
        max_paths: int = 200,
        max_visits_per_state: int = 3,
        max_transition_repeats: int = 2,
    ) -> list[GeneratedPath]:
        """Liệt kê các đường đi TỐI ĐẠI (không mở rộng thêm được) từ initial state.

        Giới hạn: độ sâu, số path, số lần một state xuất hiện trong path (tính cả đỉnh xuất phát),
        số lần một transition lặp lại. Duyệt iterative theo thứ tự mô hình nên kết quả ổn định và
        luôn dừng. Path dừng vì không còn cạnh đi ra có ``dfs_termination = TERMINAL_STATE``;
        dừng vì chạm giới hạn có ``LIMIT``.
        """
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

        def allowed(edge: Edge) -> bool:
            return (len(path) < max_depth
                    and visits.get(edge.target, 0) < max_visits_per_state
                    and repeats.get(edge.transition_id, 0) < max_transition_repeats)

        stack: list[tuple[str, list[Edge], int]] = [(initial, self.graph.out_edges(initial), 0)]
        while stack:
            state, edges, cursor = stack[-1]
            if cursor == 0 and (not edges or not any(allowed(e) for e in edges)) and path:
                found.append(([e.transition_id for e in path], "TERMINAL_STATE" if not edges else "LIMIT"))
                if len(found) > max_paths:  # còn đường đi vượt giới hạn -> báo cắt cụt
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

        paths = [
            self._make_positive(tids, "DFS", context, [], {"dfs_termination": reason}) for tids, reason in found
        ]
        self.last_dfs_info = {"truncated": truncated}
        return self._assign_ids(paths, "P")

    # ------------------------------------------------------------------
    # Negative paths
    # ------------------------------------------------------------------
    def classify_negative(self, negative_id: str) -> dict[str, Any]:
        """Phân loại negative transition dựa trên mô hình (có thể nhiều nhãn) kèm bằng chứng."""
        negative = self._negatives[negative_id]
        same_source = self._positives_for(negative["event"], negative["source"])
        same_event = self._positives_for(negative["event"])
        variables = self._negative_variables(negative_id)
        origins = {v: self._origin(v) for v in variables}
        status = negative.get("expected", {}).get("http_status")
        labels: list[NegativeCategory] = []
        if not same_source:
            labels.append(NegativeCategory.INVALID_STATE_TRANSITION)
        else:
            labels.append(NegativeCategory.GUARD_VIOLATION)
            if any(o in ("database", "derived") for o in origins.values()):
                labels.append(NegativeCategory.BUSINESS_CONSTRAINT_VIOLATION)
        request_only = bool(origins) and all(o == "request" for o in origins.values())
        if request_only or status in self.input_validation_statuses:
            labels.append(NegativeCategory.INPUT_VALIDATION_VIOLATION)
        labels = [c for c in _CATEGORY_ORDER if c in labels]
        return {
            "categories": [c.value for c in labels],
            "primary": labels[0].value if labels else None,
            "evidence": {
                "positive_transitions_same_event_and_source": same_source,
                "positive_transitions_same_event": same_event,
                "allowed_source_states_for_event": sorted({self._transitions[t]["source"] for t in same_event}),
                "condition_variables": {v: origins[v] for v in variables},
                "http_status": status,
            },
        }

    def _negative_prefix(
        self, source: str, mode: str, explored: dict[str, Any] | None
    ) -> list[str] | None:
        if mode == "feasible" and explored is not None and source in explored["state_paths"]:
            return explored["state_paths"][source]
        structural = self.graph.shortest_path(source)
        return None if structural is None else [e.transition_id for e in structural]

    def generate_negative_paths(
        self,
        context: PathContext | None = None,
        *,
        prefix_mode: str = "auto",
        max_depth: int = 12,
        max_nodes: int = 5000,
    ) -> list[GeneratedPath]:
        """Positive prefix ngắn nhất tới state nguồn + MỘT negative transition ở cuối.

        Negative transition không nằm trong đồ thị và không bao giờ xuất hiện trong prefix.
        Negative có state nguồn không đạt tới được, hoặc prefix dài hơn ``max_depth``, bị bỏ qua
        và ghi vào ``last_negative_skipped``.
        """
        mode = self._resolve_prefix_mode(prefix_mode, context)
        explored = self.explore_feasible(context, max_depth=max_depth, max_nodes=max_nodes) if mode == "feasible" and context else None
        paths: list[GeneratedPath] = []
        skipped: list[dict[str, str]] = []
        for nid, negative in self._negatives.items():
            prefix = self._negative_prefix(negative["source"], mode, explored)
            if prefix is None:
                skipped.append({"negative_transition_id": nid, "reason": "SOURCE_STATE_UNREACHABLE"})
                continue
            if len(prefix) > max_depth:
                skipped.append({"negative_transition_id": nid, "reason": "PREFIX_EXCEEDS_MAX_DEPTH"})
                continue
            paths.append(self._make_negative(nid, prefix, context, mode))
        self.last_negative_skipped = skipped
        return self._assign_ids(paths, "N")

    def _make_negative(self, nid: str, prefix: Sequence[str], context: PathContext | None, mode: str) -> GeneratedPath:
        negative = self._negatives[nid]
        edges = self._edges_for(prefix)
        feasibility = self.evaluate_path_feasibility(prefix, context, negative_transition_id=nid)
        classification = self.classify_negative(nid)
        source = negative["source"]
        base = negative.get("base_transition")
        expected = negative.get("expected", {})
        status = expected.get("http_status")
        steps = [self._positive_step(i, e) for i, e in enumerate(edges, start=1)]
        rule_ids = list(negative.get("rule_refs", []))
        neg_vars = self._negative_variables(nid)
        steps.append({
            "step_index": len(steps) + 1,
            "kind": "negative_transition",
            "negative_transition_id": nid,
            "base_transition_id": base,
            "event": negative["event"],
            "from_state": source,
            "to_state": source,
            "invalid_condition": negative["invalid_condition"],
            "expected_outcome": "REJECT",
            "expected_http_status": status,
            "required_inputs": neg_vars,
            "rule_ids": rule_ids,
            "mutant_refs": list(negative.get("mutant_refs", [])),
            "api_reachable": negative.get("api_reachable", True),
        })
        forbidden = self._effects(base) if base in self._transitions else []
        check = feasibility.negative_check or {}
        extra = {
            "negative_transition_id": nid,
            "negative_category": classification["primary"],
            "negative_categories": classification["categories"],
            "category_evidence": classification["evidence"],
            "prefix_mode": mode,
            "base_transition_id": base,
            "mutant_refs": list(negative.get("mutant_refs", [])),
            "api_reachable": negative.get("api_reachable", True),
            "expected_rejection": {
                "rejected": True,
                "http_status": status,
                "http_status_source": f"efsm_model.negative_transitions[{nid}].expected.http_status" if status is not None else None,
                "reason": f"'{negative['event']}' from {source} must be rejected: {negative['invalid_condition']}",
                "violated_condition": negative["invalid_condition"],
                "violated_transition_guards": check.get("violated_transition_guards", []),
                "forbidden_backend_effects": forbidden,
            },
            "expected_state_unchanged": expected.get("state_unchanged"),
            "expected_data_unchanged": expected.get("data_unchanged"),
        }
        if "note" in negative:
            extra["model_note"] = negative["note"]
        return GeneratedPath(
            path_id="",
            path_type="negative",
            strategy="NEGATIVE",
            path_key=self._key(prefix, nid),
            transition_ids=list(prefix),
            states=self._states_of(edges),
            steps=steps,
            feasibility=feasibility,
            security_rule_ids=rule_ids,
            required_context=self._required_context(prefix, nid),
            expected_outcome="REJECT_FINAL_STEP",
            strategies=["NEGATIVE"],
            goals=[{"type": "negative_transition", "id": nid}],
            severity=self._severity(rule_ids),
            extra=extra,
        )

    # ------------------------------------------------------------------
    # Điều phối
    # ------------------------------------------------------------------
    def generate(
        self,
        *,
        strategy: str = "bfs",
        include_negative: bool = True,
        context: PathContext | None = None,
        prefix_mode: str = "auto",
        max_depth: int = 8,
        max_paths: int = 200,
        max_visits_per_state: int = 3,
        max_transition_repeats: int = 2,
        max_nodes: int = 5000,
        rules_path: str | Path | None = None,
    ) -> GenerationResult:
        """Chạy các chiến lược được chọn, loại trùng theo ``path_key`` và đánh lại path_id."""
        strategy = strategy.lower()
        if strategy not in ("bfs", "dfs", "both"):
            raise ValueError("strategy must be 'bfs', 'dfs' or 'both'")
        if rules_path is not None and Path(rules_path) != self.rules_path:
            self.rules_path = Path(rules_path)
            self._load_rules(self.rules_path)
        warnings: list[str] = []
        limits_hit: dict[str, bool] = {}
        positives: list[GeneratedPath] = []
        mode = self._resolve_prefix_mode(prefix_mode, context)
        if strategy in ("bfs", "both"):
            positives += self.generate_bfs_paths(context, prefix_mode=prefix_mode, max_paths=max_paths,
                                                 max_depth=max_depth, max_nodes=max_nodes)
            limits_hit["bfs_max_paths"] = self.last_bfs_info["truncated"]
            limits_hit["feasible_search_max_nodes"] = self.last_bfs_info["explore_truncated"]
        if strategy in ("dfs", "both"):
            positives += self.generate_dfs_paths(context, max_depth=max_depth, max_paths=max_paths,
                                                 max_visits_per_state=max_visits_per_state,
                                                 max_transition_repeats=max_transition_repeats)
            limits_hit["dfs_max_paths"] = self.last_dfs_info["truncated"]

        merged: dict[str, GeneratedPath] = {}
        for path in positives:
            existing = merged.get(path.path_key)
            if existing is None:
                merged[path.path_key] = path
            else:
                if path.strategy not in existing.strategies:
                    existing.strategies.append(path.strategy)
                existing.goals += [g for g in path.goals if g not in existing.goals]
                existing.extra.update({k: v for k, v in path.extra.items() if k not in existing.extra})
        paths = self._assign_ids(list(merged.values()), "P")

        if include_negative:
            negatives = self.generate_negative_paths(context, prefix_mode=prefix_mode,
                                                     max_depth=max_depth, max_nodes=max_nodes)
            paths += negatives
            for skip in self.last_negative_skipped:
                warnings.append(f"{skip['negative_transition_id']} skipped: {skip['reason']}")

        unreachable = self.graph.unreachable_states()
        if unreachable:
            warnings.append(f"states not reachable from {self.graph.initial_state}: {unreachable}")
        for tid, tr in self._transitions.items():
            for rule in tr.get("rule_refs", []):
                if self.rules and rule not in self.rules:
                    warnings.append(f"transition {tid} references unknown rule {rule}")
        for nid, neg in self._negatives.items():
            for rule in neg.get("rule_refs", []):
                if self.rules and rule not in self.rules:
                    warnings.append(f"negative transition {nid} references unknown rule {rule}")
        warnings = list(dict.fromkeys(warnings))

        configuration = {
            "strategy": strategy,
            "include_negative": include_negative,
            "prefix_mode": mode,
            "effects_policy": self.effects_policy,
            "max_depth": max_depth,
            "max_paths": max_paths,
            "max_visits_per_state": max_visits_per_state,
            "max_transition_repeats": max_transition_repeats,
            "max_nodes": max_nodes,
            "input_validation_statuses": sorted(self.input_validation_statuses),
            "context_fingerprint": context.fingerprint() if context else None,
            "security_rules_file": self.rules_path.name if self.rules_path else None,
        }
        summary = self._summarize(paths, limits_hit)
        return GenerationResult(paths, summary, configuration, warnings)

    @staticmethod
    def _summarize(paths: Sequence[GeneratedPath], limits_hit: dict[str, bool]) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        by_strategy: dict[str, int] = {}
        by_feasibility: dict[str, dict[str, int]] = {}
        for path in paths:
            by_type[path.path_type] = by_type.get(path.path_type, 0) + 1
            for strategy in path.strategies:
                by_strategy[strategy] = by_strategy.get(strategy, 0) + 1
            bucket = by_feasibility.setdefault(path.path_type, {})
            bucket[path.feasibility.status.value] = bucket.get(path.feasibility.status.value, 0) + 1
        return {
            "total_paths": len(paths),
            "paths_by_type": by_type,
            "paths_by_strategy": by_strategy,
            "paths_by_feasibility": by_feasibility,
            "limits_hit": limits_hit,
        }

    # ------------------------------------------------------------------
    # Coverage
    # ------------------------------------------------------------------
    def calculate_coverage(self, paths: Sequence[GeneratedPath]) -> dict[str, Any]:
        """Độ bao phủ cấu trúc / khả thi. Executed coverage luôn là NOT_MEASURED.

        * Structural: state/transition xuất hiện trong ít nhất một path (negative path đóng góp prefix).
        * Feasible : chỉ tính các path có kết luận FEASIBLE (cách đếm thận trọng).
        """
        graph = self.graph
        reachable = graph.reachable_states()
        reachable_edges = graph.reachable_edges()
        positive_edge_ids = [e.transition_id for e in graph.edges]

        def collect(selected: Iterable[GeneratedPath]) -> tuple[set[str], set[str], set[str], set[tuple[str, str]]]:
            states, transitions, negatives, pairs = set(), set(), set(), set()
            for path in selected:
                states.update(path.states)
                transitions.update(path.transition_ids)
                pairs.update(zip(path.transition_ids, path.transition_ids[1:]))
                if path.is_negative:
                    negatives.add(path.extra["negative_transition_id"])
            return states, transitions, negatives, pairs

        s_all, t_all, n_all, p_all = collect(paths)
        feasible_paths = [p for p in paths if p.feasibility.status is Feasibility.FEASIBLE]
        s_ok, t_ok, n_ok, p_ok = collect(feasible_paths)

        possible_pairs = {
            (i.transition_id, o.transition_id)
            for state in reachable for i in graph.in_edges(state) if i.source in reachable
            for o in graph.out_edges(state)
        }
        negative_ids = list(self._negatives)
        skipped_negatives = [n for n in negative_ids if n not in n_all]
        rule_cov = self._rule_coverage(paths)

        def ratio(part: int, whole: int) -> float | None:
            return round(part / whole, 4) if whole else None

        feasible_counts: dict[str, dict[str, int]] = {}
        for path in paths:
            bucket = feasible_counts.setdefault(path.path_type, {})
            bucket[path.feasibility.status.value] = bucket.get(path.feasibility.status.value, 0) + 1
        by_type: dict[str, int] = {}
        for path in paths:
            by_type[path.path_type] = by_type.get(path.path_type, 0) + 1

        return {
            "generator": GENERATOR_NAME,
            "schema_version": SCHEMA_VERSION,
            "model": {"name": self.model.get("model_name"), "sha256": self.model_sha256},
            "states": {
                "total": len(graph.states),
                "reachable": len(reachable),
                "unreachable": graph.unreachable_states(),
                "covered_structural": len(s_all & set(reachable)),
                "covered_feasible": len(s_ok & set(reachable)),
                "structural_ratio": ratio(len(s_all & set(reachable)), len(reachable)),
                "feasible_ratio": ratio(len(s_ok & set(reachable)), len(reachable)),
                "uncovered_structural": [s for s in reachable if s not in s_all],
                "uncovered_feasible": [s for s in reachable if s not in s_ok],
            },
            "positive_transitions": {
                "total": len(positive_edge_ids),
                "reachable": len(reachable_edges),
                "covered_structural": len(t_all & set(positive_edge_ids)),
                "covered_feasible": len(t_ok & set(positive_edge_ids)),
                "structural_ratio": ratio(len(t_all), len(reachable_edges)),
                "feasible_ratio": ratio(len(t_ok), len(reachable_edges)),
                "uncovered_structural": [t for t in positive_edge_ids if t not in t_all],
                "uncovered_feasible": [t for t in positive_edge_ids if t not in t_ok],
            },
            "transition_pairs": {
                "possible": len(possible_pairs),
                "covered_structural": len(p_all & possible_pairs),
                "covered_feasible": len(p_ok & possible_pairs),
                "structural_ratio": ratio(len(p_all & possible_pairs), len(possible_pairs)),
                "uncovered_structural": sorted(f"{a}>{b}" for a, b in possible_pairs - p_all),
            },
            "negative_transitions": {
                "total": len(negative_ids),
                "with_generated_path": len(n_all),
                "with_feasible_path": len(n_ok),
                "structural_ratio": ratio(len(n_all), len(negative_ids)),
                "feasible_ratio": ratio(len(n_ok), len(negative_ids)),
                "without_path": skipped_negatives,
                "not_feasible": [n for n in negative_ids if n in n_all and n not in n_ok],
                "not_api_reachable": [n for n, v in self._negatives.items() if v.get("api_reachable") is False],
            },
            "security_rules": rule_cov,
            "paths": {"total": len(paths), "by_type": by_type, "by_feasibility": feasible_counts},
            "executed_coverage": {
                "status": NOT_MEASURED,
                "note": "No API call was made by this module; nothing is counted as executed or verified.",
            },
        }

    def _rule_coverage(self, paths: Sequence[GeneratedPath]) -> dict[str, Any]:
        implemented = [r for r, v in self.rules.items() if v.get("status") == "IMPLEMENTED"]
        linked: dict[str, None] = {}
        with_witness: dict[str, None] = {}
        for path in paths:
            for rule in path.security_rule_ids:
                linked.setdefault(rule, None)
                if path.feasibility.status is Feasibility.FEASIBLE and path.feasibility.witness:
                    with_witness.setdefault(rule, None)
        return {
            "rules_file_loaded": bool(self.rules),
            "implemented_total": len(implemented),
            "linked_to_paths": sorted(r for r in linked if r in self.rules),
            "linked_count": len([r for r in implemented if r in linked]),
            "not_linked": [r for r in implemented if r not in linked],
            "unknown_rule_ids_in_paths": sorted(r for r in linked if self.rules and r not in self.rules),
            "excluded_not_applicable": sorted(self.excluded_rules),
            "with_feasibility_witness": [r for r in implemented if r in with_witness],
            "with_generated_test_data": {"status": NOT_MEASURED, "note": "test_data_generator.py has not run."},
            "executed_on_backend": {"status": NOT_MEASURED},
            "verified_by_oracle": {"status": NOT_MEASURED},
            "level": "PATH_COVERAGE_ONLY",
        }

    # ------------------------------------------------------------------
    # Xuất JSON
    # ------------------------------------------------------------------
    def feasibility_assumptions(self) -> list[str]:
        """Giả định áp dụng cho mọi kết luận feasibility (ghi một lần ở cấp file)."""
        items = [
            "Only EFSM variables are simulated (from 'assign' actions); the backend and database are NOT touched.",
            "Backend 'effect' actions are listed in the catalog (unsimulated_effects) and were NOT executed.",
            "Verdicts hold for the supplied context only (proof_scope=GIVEN_CONTEXT); INFEASIBLE is not a universal proof.",
        ]
        if self.effects_policy == "model_contract":
            items.append(
                "effects_policy=model_contract: every EFSM variable changed by a backend effect is also "
                "assigned by an 'assign' action of the same transition."
            )
        else:
            items.append("effects_policy=strict: any unsimulated effect limits the verdict to UNKNOWN.")
        return items

    def _catalogs(self, paths: Sequence[GeneratedPath]) -> dict[str, Any]:
        tids = [t for t in self._transitions if any(t in p.transition_ids for p in paths)]
        nids = [n for n in self._negatives if any(p.extra.get("negative_transition_id") == n for p in paths)]
        transition_catalog = {}
        for tid in tids:
            tr = self._transitions[tid]
            info = self._transition_vars(tid)
            transition_catalog[tid] = {
                "event": tr.get("event"), "source": tr["source"], "target": tr["target"],
                "guard": tr.get("guard"), "guard_reads": info["guard_reads"],
                "required_inputs": info["required_inputs"],
                "conditional_action_reads": info.get("conditional_action_reads", []),
                "action_writes": info["action_writes"],
                "unsimulated_effects": self._effects(tid),
                "expected_http_status": tr.get("expected_http_status"),
                "api": {"method": tr.get("api", {}).get("method"), "path": tr.get("api", {}).get("path")} if tr.get("api") else None,
                "rule_ids": list(tr.get("rule_refs", [])),
            }
        negative_catalog = {
            nid: {
                "event": self._negatives[nid]["event"], "source": self._negatives[nid]["source"],
                "base_transition_id": self._negatives[nid].get("base_transition"),
                "invalid_condition": self._negatives[nid]["invalid_condition"],
                "condition_variables": self._negative_variables(nid),
                "expected": self._negatives[nid].get("expected"),
                "rule_ids": list(self._negatives[nid].get("rule_refs", [])),
            }
            for nid in nids
        }
        used_rules = sorted({r for p in paths for r in p.security_rule_ids})
        rules_index = {
            r: {k: self.rules[r].get(k) for k in ("name", "category", "severity", "status")}
            for r in used_rules if r in self.rules
        }
        return {"transition_catalog": transition_catalog, "negative_catalog": negative_catalog, "rules_index": rules_index}

    def to_output_dict(self, result: GenerationResult) -> dict[str, Any]:
        """Cấu trúc ``test_paths.json`` (không có timestamp để kết quả tái lập được)."""
        return {
            "generator": GENERATOR_NAME,
            "schema_version": SCHEMA_VERSION,
            "model": {
                "name": self.model.get("model_name"),
                "schema_version": self.model.get("schema_version"),
                "sha256": self.model_sha256,
                "initial_state": self.graph.initial_state,
            },
            "configuration": result.configuration,
            "feasibility_assumptions": self.feasibility_assumptions(),
            "summary": result.summary,
            "warnings": result.warnings,
            **self._catalogs(result.paths),
            "paths": [p.to_dict() for p in result.paths],
        }

    @staticmethod
    def export_json(data: Mapping[str, Any], output_path: str | Path) -> Path:
        """Ghi JSON (UTF-8, thụt lề 2, có xuống dòng cuối)."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sinh đường đi kiểm thử (positive/negative) từ mô hình EFSM. Không gọi API, không dùng DB.")
    parser.add_argument("--model", default="efsm_model.json", help="đường dẫn efsm_model.json")
    parser.add_argument("--rules", default=None, help="security_rules.json (mặc định: cùng thư mục với model nếu có)")
    parser.add_argument("--strategy", choices=["bfs", "dfs", "both"], default="bfs")
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--max-paths", type=int, default=200)
    parser.add_argument("--max-visits-per-state", type=int, default=3)
    parser.add_argument("--max-transition-repeats", type=int, default=2)
    parser.add_argument("--max-nodes", type=int, default=5000, help="giới hạn tìm kiếm prefix khả thi")
    parser.add_argument("--include-negative", action="store_true", help="sinh negative paths")
    parser.add_argument("--context", default=None, help="file JSON PathContext để đánh giá tính khả thi")
    parser.add_argument("--prefix-mode", choices=["auto", "structural", "feasible"], default="auto")
    parser.add_argument("--effects-policy", choices=["model_contract", "strict"], default="model_contract")
    parser.add_argument("--output", default="test_paths.json")
    parser.add_argument("--coverage-output", default=None,
                        help="file coverage (mặc định: test_path_coverage.json cạnh --output)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Điểm vào CLI. Trả về 0 nếu thành công, 2 nếu lỗi đầu vào."""
    args = _build_parser().parse_args(argv)
    model = Path(args.model)
    rules = Path(args.rules) if args.rules else model.with_name("security_rules.json")
    try:
        generator = TestPathGenerator(model, rules if rules.exists() else None, effects_policy=args.effects_policy)
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
    except (PathGeneratorError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    summary = result.summary
    print(f"paths={summary['total_paths']} by_type={summary['paths_by_type']} feasibility={summary['paths_by_feasibility']}")
    for warning in result.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(f"wrote {out} and {coverage_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
