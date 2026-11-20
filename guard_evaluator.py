"""
guard_evaluator.py - Guard Evaluator cho mô hình EFSM của hệ thống BlueMoon.

Module đọc ``efsm_model.json`` và đánh giá guard / invariant của EFSM một cách
an toàn: KHÔNG dùng ``eval()``/``exec()``, không chạy mã Python đọc từ JSON.

Ba lớp việc được tách bạch (xem README của từng phương thức):

* **Guard evaluation**  : ``evaluate_guard`` / ``check_transition`` - chỉ đọc biến.
* **State simulation**  : ``apply_transition`` - tính state/biến mới trong bộ nhớ.
  KHÔNG chạm vào website hay database; các tác động lên backend được liệt kê
  trong ``backend_effects`` để tầng thực thi API xử lý.
* **Backend execution** : nằm ngoài module này.

Cú pháp guard (JSON DSL)::

    {"all": [...]}  {"any": [...]}  {"not": {...}}
    {"var": "quantity", "op": ">",  "value": 0}
    {"var": "quantity", "op": "<=", "value_var": "stock"}
    {"var": "payment_method", "op": "in", "value": ["MOCK", "COD"]}
    {"var": "x", "op": "exists"} / {"var": "x", "op": "not_empty"}
    {"expr": "current_qty + quantity <= stock"}      # biểu thức an toàn (ast whitelist)

Biểu thức ``expr`` hỗ trợ: so sánh (== != < <= > >= in not in), and/or/not,
``+ - * /``, hằng số, tên biến, tên enum (CANCELLED), danh sách/tập hợp và hàm
``SUM_PRODUCT(list_var, 'field_a', 'field_b')``. Mọi cấu trúc khác bị từ chối
với ``UNSUPPORTED_EXPRESSION``.

Action của transition (chỉ mô phỏng)::

    {"type": "assign", "var": "stock", "expr": "stock - ordered_quantity"}
    {"type": "assign", "var": "order_status", "value": "CONFIRMED", "when": <guard>}
    {"type": "effect", "description": "..."}     # tác động backend, không mô phỏng

Invariant: có ``scope``, ``applies_in_states``, ``applies_when`` và ``check`` (cùng
DSL với guard), hoặc ``kind = model_structure`` (kiểm tra cấu trúc mô hình), hoặc
``evaluable = false`` kèm ``unsupported_reason``. Invariant ngoài phạm vi áp dụng
được báo ``SKIPPED`` chứ không bị coi là đúng.

Quy ước quan trọng: mọi biến mà guard tham chiếu phải có mặt và đúng kiểu (theo
``variables`` của mô hình) TRƯỚC khi đánh giá; không có giá trị mặc định ngầm.
Ngoại lệ: biến chỉ dùng với toán tử ``exists``.
"""

from __future__ import annotations

import ast
import copy
import json
import operator
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "GuardEvaluator",
    "GuardStatus",
    "InvariantStatus",
    "GuardEvaluation",
    "GuardCheckResult",
    "InvariantResult",
    "InvariantReport",
    "TransitionOutcome",
    "GuardEvaluatorError",
    "ModelLoadError",
    "InvalidGuardError",
    "UnsupportedExpressionError",
    "MissingVariableError",
    "InvalidVariableTypeError",
    "OperandTypeError",
    "EvaluationError",
]


# --------------------------------------------------------------------------
# Ngoại lệ
# --------------------------------------------------------------------------
class GuardEvaluatorError(Exception):
    """Lớp cơ sở cho mọi lỗi của module."""


class ModelLoadError(GuardEvaluatorError):
    """File mô hình không đọc được hoặc sai cấu trúc (``issues`` liệt kê từng lỗi)."""

    def __init__(self, message: str, issues: Iterable[str] | None = None) -> None:
        super().__init__(message)
        self.issues: list[str] = list(issues) if issues is not None else [message]


class InvalidGuardError(GuardEvaluatorError):
    """Guard/biểu thức sai cấu trúc hoặc sai cú pháp (lỗi do người viết mô hình)."""


class UnsupportedExpressionError(GuardEvaluatorError):
    """Biểu thức hợp lệ về cú pháp nhưng dùng cấu trúc mà evaluator không hỗ trợ."""


class MissingVariableError(GuardEvaluatorError):
    """Thiếu biến runtime mà guard/action yêu cầu."""

    def __init__(self, missing: Iterable[str]) -> None:
        self.missing = sorted(missing)
        super().__init__(f"Missing required variable(s): {', '.join(self.missing)}")


class InvalidVariableTypeError(GuardEvaluatorError):
    """Biến runtime sai kiểu so với khai báo trong mô hình."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        detail = "; ".join(
            f"{e['variable']} expected {e['expected_type']}, got {e['actual_type']}"
            for e in errors
        )
        super().__init__(f"Invalid variable type(s): {detail}")


class OperandTypeError(GuardEvaluatorError):
    """Toán hạng không phù hợp với toán tử (ví dụ so sánh '>' giữa chuỗi và số)."""


class EvaluationError(GuardEvaluatorError):
    """Lỗi khi tính biểu thức (chia cho 0, đối số hàm sai...)."""


# --------------------------------------------------------------------------
# Kết quả trả về
# --------------------------------------------------------------------------
class GuardStatus(StrEnum):
    """Trạng thái kết quả khi kiểm tra transition / guard."""

    TRANSITION_NOT_FOUND = "TRANSITION_NOT_FOUND"
    STATE_MISMATCH = "STATE_MISMATCH"
    GUARD_SATISFIED = "GUARD_SATISFIED"
    GUARD_NOT_SATISFIED = "GUARD_NOT_SATISFIED"
    MISSING_VARIABLE = "MISSING_VARIABLE"
    TYPE_ERROR = "TYPE_ERROR"
    INVALID_VARIABLE_TYPE = "TYPE_ERROR"  # tên cũ - bí danh của TYPE_ERROR
    INVALID_GUARD = "INVALID_GUARD"
    UNSUPPORTED_EXPRESSION = "UNSUPPORTED_EXPRESSION"
    EVALUATION_ERROR = "EVALUATION_ERROR"
    # Chỉ dùng cho negative_transitions (invalid_condition có đang đúng hay không)
    NEGATIVE_APPLICABLE = "NEGATIVE_APPLICABLE"
    NEGATIVE_NOT_APPLICABLE = "NEGATIVE_NOT_APPLICABLE"


class InvariantStatus(StrEnum):
    """Kết quả kiểm tra một invariant. ``reason_code`` cho biết chi tiết."""

    SATISFIED = "SATISFIED"
    VIOLATED = "VIOLATED"
    SKIPPED = "SKIPPED"  # ngoài phạm vi áp dụng hoặc thiếu dữ liệu
    UNSUPPORTED = "UNSUPPORTED"
    ERROR = "ERROR"


def _to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


@dataclass
class GuardEvaluation:
    """Kết quả đánh giá riêng một guard (không xét trạng thái nguồn)."""

    status: GuardStatus
    result: bool | None  # None khi không thể quyết định (thiếu biến, guard lỗi...)
    reason: str
    failed_conditions: list[dict[str, Any]] = field(default_factory=list)
    missing_variables: list[str] = field(default_factory=list)
    type_errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "result": self.result,
            "reason": self.reason,
            "failed_conditions": self.failed_conditions,
            "missing_variables": self.missing_variables,
            "type_errors": self.type_errors,
        }


@dataclass
class GuardCheckResult:
    """Kết quả ``check_transition`` - dành cho Test Path Generator."""

    transition_id: str
    current_state: str
    source_state: str | None
    guard_result: bool | None
    status: GuardStatus
    reason: str
    failed_conditions: list[dict[str, Any]] = field(default_factory=list)
    event: str | None = None
    target_state: str | None = None
    rule_refs: list[str] = field(default_factory=list)
    missing_variables: list[str] = field(default_factory=list)
    type_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        """True nếu transition được phép thực hiện (đúng state và guard đúng)."""
        return self.status is GuardStatus.GUARD_SATISFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "current_state": self.current_state,
            "source_state": self.source_state,
            "guard_result": self.guard_result,
            "status": self.status.value,
            "reason": self.reason,
            "failed_conditions": self.failed_conditions,
            "event": self.event,
            "target_state": self.target_state,
            "rule_refs": self.rule_refs,
            "missing_variables": self.missing_variables,
            "type_errors": self.type_errors,
        }

    def to_json(self) -> str:
        return _to_json(self.to_dict())


@dataclass
class InvariantResult:
    """Kết quả kiểm tra một invariant (đủ dữ liệu để debug)."""

    invariant_id: str
    status: InvariantStatus
    reason: str
    reason_code: str
    scope: str | None = None
    expression: str | None = None
    missing_variables: list[str] = field(default_factory=list)
    failed_conditions: list[dict[str, Any]] = field(default_factory=list)
    variables_used: dict[str, Any] = field(default_factory=dict)
    covered_by: list[str] = field(default_factory=list)
    rule_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "invariant_id": self.invariant_id,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "scope": self.scope,
            "expression": self.expression,
            "missing_variables": self.missing_variables,
            "failed_conditions": self.failed_conditions,
            "variables_used": self.variables_used,
            "covered_by": self.covered_by,
            "rule_refs": self.rule_refs,
        }


@dataclass
class InvariantReport:
    """Tập kết quả của ``check_invariants``."""

    results: list[InvariantResult]

    def by_status(self, status: InvariantStatus) -> list[InvariantResult]:
        return [r for r in self.results if r.status is status]

    @property
    def violations(self) -> list[InvariantResult]:
        return self.by_status(InvariantStatus.VIOLATED)

    @property
    def errors(self) -> list[InvariantResult]:
        return self.by_status(InvariantStatus.ERROR)

    @property
    def skipped(self) -> list[InvariantResult]:
        return self.by_status(InvariantStatus.SKIPPED)

    @property
    def unsupported(self) -> list[InvariantResult]:
        return self.by_status(InvariantStatus.UNSUPPORTED)

    @property
    def ok(self) -> bool:
        """True nếu KHÔNG có invariant nào VIOLATED hoặc ERROR.

        Lưu ý: ``ok`` không có nghĩa mọi invariant đã được kiểm tra; hãy xem
        ``fully_evaluated`` và ``summary()``.
        """
        return not self.violations and not self.errors

    @property
    def fully_evaluated(self) -> bool:
        """True nếu mọi invariant đều ra SATISFIED hoặc VIOLATED."""
        return all(
            r.status in (InvariantStatus.SATISFIED, InvariantStatus.VIOLATED) for r in self.results
        )

    def summary(self) -> dict[str, int]:
        counts = {status.value: 0 for status in InvariantStatus}
        for result in self.results:
            counts[result.status.value] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "fully_evaluated": self.fully_evaluated,
            "summary": self.summary(),
            "results": [r.to_dict() for r in self.results],
        }

    def to_json(self) -> str:
        return _to_json(self.to_dict())


@dataclass
class TransitionOutcome:
    """Kết quả ``apply_transition`` - CHỈ là mô phỏng trong bộ nhớ."""

    applied: bool
    check: GuardCheckResult
    new_state: str
    new_variables: dict[str, Any]
    executed_actions: list[str] = field(default_factory=list)
    backend_effects: list[str] = field(default_factory=list)  # không được mô phỏng
    skipped_actions: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    error_status: GuardStatus | None = None
    missing_variables: list[str] = field(default_factory=list)
    invariant_report: InvariantReport | None = None
    simulated: bool = True  # luôn True: database/website không bị thay đổi

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "simulated": self.simulated,
            "new_state": self.new_state,
            "new_variables": self.new_variables,
            "executed_actions": self.executed_actions,
            "backend_effects": self.backend_effects,
            "skipped_actions": self.skipped_actions,
            "warnings": self.warnings,
            "error": self.error,
            "error_status": self.error_status.value if self.error_status else None,
            "missing_variables": self.missing_variables,
            "invariant_report": self.invariant_report.to_dict() if self.invariant_report else None,
            "check": self.check.to_dict(),
        }

    def to_json(self) -> str:
        return _to_json(self.to_dict())


# --------------------------------------------------------------------------
# Hằng số của DSL
# --------------------------------------------------------------------------
_ORDER_OPERATORS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}
_COMPARE_OPERATORS = {"==", "!=", "in", "not_in", *_ORDER_OPERATORS}
_UNARY_OPERATORS = {"exists", "not_empty"}
_ALL_OPERATORS = sorted(_COMPARE_OPERATORS | _UNARY_OPERATORS)
_KNOWN_TYPES = {"integer", "decimal", "string", "boolean", "list"}
_NUMERIC_TYPES = {"integer", "decimal"}
_REQUIREMENT_RANK = {"optional": 0, "present": 1, "typed": 2}

_AST_COMPARE = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.In: "in",
    ast.NotIn: "not_in",
}
_ARITHMETIC = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul}

# Whitelist các node AST được phép. Mọi thứ khác (Attribute, Subscript, Lambda,
# Pow, comprehension, ...) bị từ chối bằng UnsupportedExpressionError.
_ALLOWED_AST_NODES: tuple[type, ...] = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.USub, ast.UAdd, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Constant, ast.Name, ast.Load, ast.List,
    ast.Tuple, ast.Set, ast.Call,
)  # fmt: skip

# "cart_quantity := current_qty + quantity" hoặc "keep order_status := CONFIRMED"
_ASSIGNMENT_RE = re.compile(r"^\s*(?:keep\s+)?([A-Za-z_]\w*)\s*:=\s*(.+?)\s*$")


def _fmt(value: Any) -> str:
    """Định dạng giá trị để đưa vào thông điệp lý do."""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _sum_product(items: Any, field_a: Any, field_b: Any) -> Any:
    """Hàm whitelist ``SUM_PRODUCT(list, 'a', 'b')`` = tổng item[a] * item[b]."""
    if not isinstance(items, list):
        raise OperandTypeError(f"SUM_PRODUCT expects a list, got {type(items).__name__}")
    total: Any = 0
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise OperandTypeError(f"SUM_PRODUCT item #{index} must be an object")
        for name in (field_a, field_b):
            if name not in item:
                raise EvaluationError(f"SUM_PRODUCT item #{index} has no field {name!r}")
            value = item[name]
            if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
                raise OperandTypeError(f"SUM_PRODUCT item #{index}.{name} must be numeric")
        total = _arith(operator.add, total, _arith(operator.mul, item[field_a], item[field_b]))
    return total


# Hàm được phép gọi trong biểu thức: tên -> (số đối số, hàm thực thi)
_FUNCTIONS: dict[str, tuple[int, Callable[..., Any]]] = {"SUM_PRODUCT": (3, _sum_product)}


def _arith(op: Callable[[Any, Any], Any], left: Any, right: Any) -> Any:
    """Phép toán số học; Decimal kết hợp float được đổi sang Decimal."""
    if isinstance(left, Decimal) and isinstance(right, float):
        right = Decimal(repr(right))
    elif isinstance(right, Decimal) and isinstance(left, float):
        left = Decimal(repr(left))
    return op(left, right)


# --------------------------------------------------------------------------
# Lớp chính
# --------------------------------------------------------------------------
class GuardEvaluator:
    """Đánh giá guard, kiểm tra invariant và mô phỏng transition của EFSM."""

    def __init__(self, model_path: str | Path, *, strict: bool = True) -> None:
        """``strict=True``: mọi guard/action/invariant được kiểm tra tĩnh khi nạp mô
        hình; có lỗi thì ném ``ModelLoadError``. ``strict=False``: lỗi được lưu ở
        ``model_issues`` và chỉ bộc lộ khi phần tử đó được dùng."""
        self.model_path = Path(model_path)
        self.strict = strict
        self.model: dict[str, Any] = {}
        self.model_issues: list[str] = []
        self._states: dict[str, dict[str, Any]] = {}
        self._variables: dict[str, dict[str, Any]] = {}
        self._transitions: dict[str, dict[str, Any]] = {}
        self._invariants: list[dict[str, Any]] = []
        self._negative: dict[str, dict[str, Any]] = {}
        self._enum_literals: set[str] = set()
        self._expr_cache: dict[str, ast.Expression] = {}
        self.load_model()

    def __repr__(self) -> str:
        return (
            f"GuardEvaluator(model={self.model.get('model_name')!r}, "
            f"transitions={len(self._transitions)}, path={str(self.model_path)!r})"
        )

    # ------------------------------------------------------------------
    # Nạp mô hình
    # ------------------------------------------------------------------
    def load_model(self) -> None:
        """Đọc JSON, dựng chỉ mục và kiểm tra tĩnh. Không sửa file mô hình."""
        try:
            with self.model_path.open("r", encoding="utf-8") as handle:
                model = json.load(handle)
        except FileNotFoundError as exc:
            raise ModelLoadError(f"Model file not found: {self.model_path}") from exc
        except json.JSONDecodeError as exc:
            raise ModelLoadError(f"Model file is not valid JSON: {exc}") from exc
        except OSError as exc:
            raise ModelLoadError(f"Cannot read model file: {exc}") from exc
        self._build_indexes(model)
        self.model_issues = self._validate_model()
        if self.strict and self.model_issues:
            raise ModelLoadError(
                f"Model has {len(self.model_issues)} issue(s): " + " | ".join(self.model_issues),
                self.model_issues,
            )

    def _build_indexes(self, model: Any) -> None:
        if not isinstance(model, dict):
            raise ModelLoadError("Model root must be a JSON object")
        for key in ("states", "variables", "transitions"):
            if not isinstance(model.get(key), list) or not model[key]:
                raise ModelLoadError(f"Model must contain a non-empty list '{key}'")

        states = self._index_by(model["states"], "id", "state")
        variables = self._index_by(model["variables"], "name", "variable")
        transitions = self._index_by(model["transitions"], "id", "transition")
        negatives = self._index_by(model.get("negative_transitions", []), "id", "negative transition")

        for tid, tr in transitions.items():
            for end in ("source", "target"):
                if tr.get(end) not in states:
                    raise ModelLoadError(f"Transition '{tid}' has unknown {end} state: {tr.get(end)!r}")

        invariants = model.get("invariants", [])
        if not isinstance(invariants, list) or not all(isinstance(i, dict) for i in invariants):
            raise ModelLoadError("'invariants' must be a list of objects")
        seen: set[str] = set()
        for inv in invariants:
            inv_id = inv.get("id")
            if not isinstance(inv_id, str) or inv_id in seen:
                raise ModelLoadError(f"Invariant needs a unique string 'id', got {inv_id!r}")
            seen.add(inv_id)

        literals = set(states)
        for spec in variables.values():
            for allowed in spec.get("allowed_values", []):
                if isinstance(allowed, str):
                    literals.add(allowed)

        self.model = model
        self._states = states
        self._variables = variables
        self._transitions = transitions
        self._negative = negatives
        self._invariants = invariants
        self._enum_literals = literals
        self._expr_cache.clear()

    @staticmethod
    def _index_by(items: Any, key: str, label: str) -> dict[str, dict[str, Any]]:
        if not isinstance(items, list):
            raise ModelLoadError(f"{label} list must be a JSON array")
        index: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get(key), str):
                raise ModelLoadError(f"Each {label} must be an object with a string '{key}'")
            if item[key] in index:
                raise ModelLoadError(f"Duplicate {label} {key}: {item[key]!r}")
            index[item[key]] = item
        return index

    def _validate_model(self) -> list[str]:
        """Kiểm tra tĩnh toàn bộ guard / action / invariant / negative transition."""
        issues: list[str] = []

        def check(label: str, func: Callable[[], Any]) -> None:
            try:
                func()
            except GuardEvaluatorError as exc:
                issues.append(f"{label}: {exc}")

        for name, spec in self._variables.items():
            if spec.get("type") not in _KNOWN_TYPES:
                issues.append(f"variable {name}: unknown type {spec.get('type')!r}")

        for tid, tr in self._transitions.items():
            if tr.get("guard") is not None:
                check(f"transition {tid} guard", lambda g=tr["guard"]: self._analyze_condition(g, {}, "guard"))
            actions = tr.get("actions", [])
            if not isinstance(actions, list):
                issues.append(f"transition {tid}: 'actions' must be a list")
                continue
            for index, raw in enumerate(actions):
                check(
                    f"transition {tid} actions[{index}]",
                    lambda r=raw: self._analyze_action(self._normalize_action(r)),
                )

        for inv in self._invariants:
            check(f"invariant {inv['id']}", lambda i=inv: self._validate_invariant(i))

        for nid, neg in self._negative.items():
            if neg.get("source") not in self._states:
                issues.append(f"negative transition {nid}: unknown source state {neg.get('source')!r}")
            base = neg.get("base_transition")
            if base is not None and base not in self._transitions:
                issues.append(f"negative transition {nid}: unknown base_transition {base!r}")
            if "invalid_condition" in neg:
                check(
                    f"negative transition {nid} invalid_condition",
                    lambda c=neg["invalid_condition"]: self._analyze_condition({"expr": c}, {}, "invalid_condition"),
                )
        return issues

    def _validate_invariant(self, inv: dict[str, Any]) -> None:
        for state in inv.get("applies_in_states", []) or []:
            if state not in self._states:
                raise InvalidGuardError(f"applies_in_states: unknown state {state!r}")
        if inv.get("kind") == "model_structure":
            self._validate_structure(inv.get("structure"))
            return
        if inv.get("evaluable") is False:
            if not inv.get("unsupported_reason"):
                raise InvalidGuardError("evaluable=false requires 'unsupported_reason'")
            return
        if "check" not in inv:
            raise InvalidGuardError(
                "invariant has no machine-readable 'check'; add one or mark it "
                "evaluable=false with an 'unsupported_reason'"
            )
        self._analyze_condition(inv["check"], {}, "check")
        if inv.get("applies_when") is not None:
            self._analyze_condition(inv["applies_when"], {}, "applies_when")

    def _validate_structure(self, spec: Any) -> None:
        if not isinstance(spec, dict) or spec.get("type") not in ("no_event_from_state", "event_only_from_states"):
            raise InvalidGuardError("structure.type must be 'no_event_from_state' or 'event_only_from_states'")
        for state in ([spec["state"]] if "state" in spec else []) + list(spec.get("states", [])):
            if state not in self._states:
                raise InvalidGuardError(f"structure refers to unknown state {state!r}")
        if spec["type"] == "no_event_from_state" and "state" not in spec:
            raise InvalidGuardError("no_event_from_state requires 'state'")
        if spec["type"] == "event_only_from_states" and not (spec.get("event") and spec.get("states")):
            raise InvalidGuardError("event_only_from_states requires 'event' and 'states'")

    # ------------------------------------------------------------------
    # Truy vấn mô hình
    # ------------------------------------------------------------------
    @property
    def states(self) -> list[str]:
        return list(self._states)

    @property
    def variable_names(self) -> list[str]:
        return list(self._variables)

    @property
    def transition_ids(self) -> list[str]:
        return list(self._transitions)

    @property
    def negative_transition_ids(self) -> list[str]:
        return list(self._negative)

    @property
    def invariant_ids(self) -> list[str]:
        return [inv["id"] for inv in self._invariants]

    def get_transition(self, transition_id: str) -> dict[str, Any] | None:
        """Trả về bản sao của transition, hoặc None nếu không tồn tại."""
        transition = self._transitions.get(transition_id)
        return copy.deepcopy(transition) if transition is not None else None

    def get_negative_transition(self, negative_id: str) -> dict[str, Any] | None:
        negative = self._negative.get(negative_id)
        return copy.deepcopy(negative) if negative is not None else None

    def get_outgoing_transitions(self, state: str) -> list[str]:
        """ID các transition có trạng thái nguồn là ``state``."""
        return [tid for tid, tr in self._transitions.items() if tr["source"] == state]

    def get_variable_spec(self, name: str) -> dict[str, Any] | None:
        spec = self._variables.get(name)
        return copy.deepcopy(spec) if spec is not None else None

    def get_guard_variables(self, guard: Any) -> dict[str, str]:
        """Biến mà guard tham chiếu -> mức yêu cầu ('typed' | 'present' | 'optional')."""
        requirements: dict[str, str] = {}
        if guard is not None:
            self._analyze_condition(guard, requirements, "guard")
        return requirements

    def get_transition_variables(self, transition_id: str) -> dict[str, list[str]]:
        """Biến liên quan của transition (để sinh dữ liệu test).

        * ``guard_reads``  : biến guard tham chiếu.
        * ``action_reads`` : biến action đọc mà chưa được action trước đó gán.
        * ``conditional_action_reads``: biến chỉ cần khi action có ``when`` thỏa mãn.
        * ``action_writes``: biến action gán.
        * ``required_inputs``: biến bắt buộc phải có để check + apply thành công.
        """
        transition = self._transitions.get(transition_id)
        if transition is None:
            raise KeyError(f"Transition {transition_id!r} does not exist")
        guard_requirements = self.get_guard_variables(transition.get("guard"))
        written: set[str] = set()
        action_reads: set[str] = set()
        conditional_reads: set[str] = set()
        writes: list[str] = []
        for raw in transition.get("actions", []):
            action = self._normalize_action(raw)
            always, conditional, write = self._analyze_action(action)
            action_reads |= always - written
            conditional_reads |= conditional - written
            if write:
                if action.get("when") is None:  # gán có điều kiện chưa chắc đã chạy
                    written.add(write)
                if write not in writes:
                    writes.append(write)
        required = {n for n, mode in guard_requirements.items() if mode != "optional"} | action_reads
        return {
            "guard_reads": sorted(guard_requirements),
            "action_reads": sorted(action_reads),
            "conditional_action_reads": sorted(conditional_reads - action_reads),
            "action_writes": writes,
            "required_inputs": sorted(required),
        }

    # ------------------------------------------------------------------
    # Kiểm tra transition
    # ------------------------------------------------------------------
    def check_transition(
        self, transition_id: str, current_state: str, variables: Mapping[str, Any]
    ) -> GuardCheckResult:
        """Kiểm tra transition theo thứ tự: tồn tại -> state nguồn -> guard."""
        transition = self._transitions.get(transition_id)
        if transition is None:
            return GuardCheckResult(
                transition_id=transition_id,
                current_state=current_state,
                source_state=None,
                guard_result=None,
                status=GuardStatus.TRANSITION_NOT_FOUND,
                reason=f"Transition '{transition_id}' does not exist in the model",
            )

        source = transition["source"]
        info = {
            "transition_id": transition_id,
            "current_state": current_state,
            "source_state": source,
            "event": transition.get("event"),
            "target_state": transition.get("target"),
            "rule_refs": list(transition.get("rule_refs", [])),
        }
        if current_state != source:
            reason = f"Current state '{current_state}' does not match source state '{source}'"
            if current_state not in self._states:
                reason += " (and is not a state of the model)"
            return GuardCheckResult(
                **info, guard_result=None, status=GuardStatus.STATE_MISMATCH, reason=reason
            )

        evaluation = self.evaluate_guard(transition.get("guard"), variables)
        return GuardCheckResult(
            **info,
            guard_result=evaluation.result,
            status=evaluation.status,
            reason=evaluation.reason,
            failed_conditions=evaluation.failed_conditions,
            missing_variables=evaluation.missing_variables,
            type_errors=evaluation.type_errors,
        )

    def check_outgoing_transitions(
        self, current_state: str, variables: Mapping[str, Any]
    ) -> list[GuardCheckResult]:
        """Kiểm tra tất cả transition đi ra từ ``current_state``."""
        return [
            self.check_transition(tid, current_state, variables)
            for tid in self.get_outgoing_transitions(current_state)
        ]

    def get_enabled_transitions(
        self, current_state: str, variables: Mapping[str, Any]
    ) -> list[GuardCheckResult]:
        """Các transition đi ra mà guard đang thỏa mãn với ``variables``."""
        return [r for r in self.check_outgoing_transitions(current_state, variables) if r.enabled]

    def check_negative_transition(
        self, negative_id: str, current_state: str, variables: Mapping[str, Any]
    ) -> GuardCheckResult:
        """Kiểm tra ``invalid_condition`` của một negative transition.

        ``NEGATIVE_APPLICABLE``: điều kiện không hợp lệ đang đúng -> ca kiểm thử
        âm tính này sẽ được kích hoạt với bộ biến hiện tại.
        """
        negative = self._negative.get(negative_id)
        if negative is None:
            return GuardCheckResult(
                transition_id=negative_id,
                current_state=current_state,
                source_state=None,
                guard_result=None,
                status=GuardStatus.TRANSITION_NOT_FOUND,
                reason=f"Negative transition '{negative_id}' does not exist in the model",
            )
        source = negative.get("source")
        info = {
            "transition_id": negative_id,
            "current_state": current_state,
            "source_state": source,
            "event": negative.get("event"),
            "rule_refs": list(negative.get("rule_refs", [])),
        }
        if current_state != source:
            return GuardCheckResult(
                **info,
                guard_result=None,
                status=GuardStatus.STATE_MISMATCH,
                reason=f"Current state '{current_state}' does not match source state '{source}'",
            )
        condition = negative.get("invalid_condition")
        evaluation = self.evaluate_guard({"expr": condition}, variables)
        if evaluation.status is GuardStatus.GUARD_SATISFIED:
            status, reason = GuardStatus.NEGATIVE_APPLICABLE, f"Invalid condition holds: {condition}"
        elif evaluation.status is GuardStatus.GUARD_NOT_SATISFIED:
            status, reason = (
                GuardStatus.NEGATIVE_NOT_APPLICABLE,
                f"Invalid condition does not hold: {condition}",
            )
        else:
            status, reason = evaluation.status, evaluation.reason
        return GuardCheckResult(
            **info,
            guard_result=evaluation.result,
            status=status,
            reason=reason,
            missing_variables=evaluation.missing_variables,
            type_errors=evaluation.type_errors,
        )

    # ------------------------------------------------------------------
    # Đánh giá guard
    # ------------------------------------------------------------------
    def evaluate_guard(self, guard: Any, variables: Mapping[str, Any]) -> GuardEvaluation:
        """Đánh giá một guard với giá trị biến runtime.

        Thứ tự: kiểm tra cấu trúc guard -> biến thiếu -> sai kiểu -> đánh giá.
        Guard ``None`` nghĩa là transition không có điều kiện (luôn đúng).
        Không bao giờ trả ``GUARD_SATISFIED`` nếu chưa thực sự đánh giá xong.
        """
        if not isinstance(variables, Mapping):
            raise TypeError("variables must be a mapping (dict) of EFSM variables")
        if guard is None:
            return GuardEvaluation(
                GuardStatus.GUARD_SATISFIED, True, "Transition has no guard (always enabled)"
            )
        try:
            satisfied, failures = self._run_condition(guard, variables)
        except GuardEvaluatorError as exc:
            status, missing, type_errors = self._classify(exc)
            return GuardEvaluation(
                status, None, str(exc), missing_variables=missing, type_errors=type_errors
            )
        if satisfied:
            return GuardEvaluation(GuardStatus.GUARD_SATISFIED, True, "All guard conditions are satisfied")
        reason = "Guard not satisfied: " + "; ".join(self._format_failure(f) for f in failures)
        return GuardEvaluation(GuardStatus.GUARD_NOT_SATISFIED, False, reason, failures)

    @staticmethod
    def _classify(exc: GuardEvaluatorError) -> tuple[GuardStatus, list[str], list[dict[str, Any]]]:
        """Đổi ngoại lệ nội bộ thành (status, biến thiếu, lỗi kiểu)."""
        if isinstance(exc, MissingVariableError):
            return GuardStatus.MISSING_VARIABLE, exc.missing, []
        if isinstance(exc, InvalidVariableTypeError):
            return GuardStatus.TYPE_ERROR, [], exc.errors
        if isinstance(exc, OperandTypeError):
            return GuardStatus.TYPE_ERROR, [], []
        if isinstance(exc, UnsupportedExpressionError):
            return GuardStatus.UNSUPPORTED_EXPRESSION, [], []
        if isinstance(exc, EvaluationError):
            return GuardStatus.EVALUATION_ERROR, [], []
        return GuardStatus.INVALID_GUARD, [], []

    def _run_condition(
        self, cond: Any, variables: Mapping[str, Any]
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Phân tích -> kiểm tra biến -> đánh giá. Ném ngoại lệ khi không đánh giá được."""
        requirements: dict[str, str] = {}
        self._analyze_condition(cond, requirements, "guard")
        self._validate_variables(requirements, variables)
        return self._evaluate_condition(cond, variables)

    # --- phân tích cấu trúc (không cần giá trị biến) --------------------
    @staticmethod
    def _normalize_condition(cond: Any) -> Any:
        return {"expr": cond} if isinstance(cond, str) else cond

    @staticmethod
    def _require(requirements: dict[str, str], name: str, mode: str) -> None:
        """Ghi nhận mức yêu cầu của biến; mức cao hơn ghi đè mức thấp hơn."""
        if name not in requirements or _REQUIREMENT_RANK[mode] > _REQUIREMENT_RANK[requirements[name]]:
            requirements[name] = mode

    def _analyze_condition(self, cond: Any, requirements: dict[str, str], path: str) -> None:
        """Kiểm tra cấu trúc guard và thu thập danh sách biến cần có."""
        cond = self._normalize_condition(cond)
        if not isinstance(cond, dict):
            raise InvalidGuardError(f"{path}: condition must be an object or string, got {type(cond).__name__}")
        keys = set(cond)

        for logic in ("all", "any"):
            if logic in keys:
                if keys != {logic}:
                    raise InvalidGuardError(f"{path}: '{logic}' cannot be combined with other keys {sorted(keys - {logic})}")
                children = cond[logic]
                if not isinstance(children, list) or not children:
                    raise InvalidGuardError(f"{path}.{logic}: must be a non-empty list")
                for index, child in enumerate(children):
                    self._analyze_condition(child, requirements, f"{path}.{logic}[{index}]")
                return

        if "not" in keys:
            if keys != {"not"}:
                raise InvalidGuardError(f"{path}: 'not' cannot be combined with other keys")
            self._analyze_condition(cond["not"], requirements, f"{path}.not")
            return

        if "expr" in keys:
            if keys != {"expr"}:
                raise InvalidGuardError(f"{path}: 'expr' cannot be combined with other keys")
            tree = self._parse_expression(cond["expr"])
            for name in self._expression_variables(tree):
                self._require(requirements, name, "typed")
            return

        if "var" in keys:
            self._analyze_leaf(cond, requirements, path)
            return

        raise InvalidGuardError(
            f"{path}: unrecognised condition {sorted(keys)}; expected all/any/not/expr or var+op"
        )

    def _analyze_leaf(self, cond: dict[str, Any], requirements: dict[str, str], path: str) -> None:
        extra = set(cond) - {"var", "op", "value", "value_var"}
        if extra:
            raise InvalidGuardError(f"{path}: unexpected key(s) {sorted(extra)}")
        var, op = cond["var"], cond.get("op")
        if not isinstance(var, str) or var not in self._variables:
            raise InvalidGuardError(f"{path}: variable {var!r} is not declared in the model")

        if op in _UNARY_OPERATORS:
            if "value_var" in cond or ("value" in cond and not isinstance(cond["value"], bool)):
                raise InvalidGuardError(f"{path}: operator {op!r} accepts only an optional boolean 'value'")
            # exists: không bắt buộc có biến; not_empty: bắt buộc có mặt (giá trị có thể rỗng)
            self._require(requirements, var, "present" if op == "not_empty" else "optional")
            return

        if op not in _COMPARE_OPERATORS:
            raise InvalidGuardError(f"{path}: unsupported operator {op!r}; supported: {_ALL_OPERATORS}")
        has_value, has_var = "value" in cond, "value_var" in cond
        if has_value == has_var:
            raise InvalidGuardError(f"{path}: exactly one of 'value' or 'value_var' is required for {op!r}")
        if op in ("in", "not_in") and not (has_value and isinstance(cond["value"], (list, tuple, set, frozenset))):
            raise InvalidGuardError(f"{path}: operator {op!r} requires 'value' to be a list")

        if op in _ORDER_OPERATORS:
            if self._variables[var].get("type") not in _NUMERIC_TYPES:
                raise InvalidGuardError(f"{path}: operator {op!r} needs a numeric variable, {var!r} is {self._variables[var].get('type')}")
            if has_value and not self._is_number(cond["value"]):
                raise InvalidGuardError(f"{path}: operator {op!r} needs a numeric 'value', got {cond['value']!r}")
        if has_var:
            other = cond["value_var"]
            if not isinstance(other, str) or other not in self._variables:
                raise InvalidGuardError(f"{path}: value_var {other!r} is not declared in the model")
            if op in _ORDER_OPERATORS and self._variables[other].get("type") not in _NUMERIC_TYPES:
                raise InvalidGuardError(f"{path}: operator {op!r} needs numeric variables, {other!r} is not")
            self._require(requirements, other, "typed")
        self._require(requirements, var, "typed")

    # --- kiểm tra biến ---------------------------------------------------
    @staticmethod
    def _type_ok(declared: str | None, value: Any) -> bool:
        if declared == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if declared == "decimal":
            return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
        if declared == "string":
            return isinstance(value, str)
        if declared == "boolean":
            return isinstance(value, bool)
        if declared == "list":
            return isinstance(value, list)
        return True  # kiểu không xác định trong mô hình: không kiểm tra

    def _validate_variables(self, requirements: Mapping[str, str], variables: Mapping[str, Any]) -> None:
        """Ném MissingVariableError / InvalidVariableTypeError nếu biến không đạt."""
        needed = {name: mode for name, mode in requirements.items() if mode != "optional"}
        missing = [name for name in needed if name not in variables]
        if missing:
            raise MissingVariableError(missing)
        errors = []
        for name in sorted(needed):
            if needed[name] != "typed":
                continue
            declared = self._variables[name].get("type")
            value = variables[name]
            if not self._type_ok(declared, value):
                errors.append(
                    {
                        "variable": name,
                        "expected_type": declared,
                        "actual_type": type(value).__name__,
                        "actual": value,
                    }
                )
        if errors:
            raise InvalidVariableTypeError(errors)

    def _resolve_variable(self, name: str, variables: Mapping[str, Any]) -> Any:
        """Lấy giá trị biến; float của biến 'decimal' được đổi sang Decimal."""
        if name not in self._variables:
            raise InvalidGuardError(f"variable {name!r} is not declared in the model")
        if name not in variables:
            raise MissingVariableError([name])
        value = variables[name]
        if self._variables[name].get("type") == "decimal" and isinstance(value, float):
            return Decimal(repr(value))
        return value

    # --- đánh giá điều kiện ---------------------------------------------
    def _evaluate_condition(
        self, cond: Any, variables: Mapping[str, Any]
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Trả về (kết quả, danh sách điều kiện lá bị sai). Không short-circuit
        để báo đủ mọi điều kiện sai."""
        cond = self._normalize_condition(cond)

        if "all" in cond:
            results = [self._evaluate_condition(c, variables) for c in cond["all"]]
            if all(ok for ok, _ in results):
                return True, []
            return False, [f for ok, fails in results if not ok for f in fails]

        if "any" in cond:
            results = [self._evaluate_condition(c, variables) for c in cond["any"]]
            if any(ok for ok, _ in results):
                return True, []
            return False, [
                {
                    "condition": self._describe(cond),
                    "actual": None,
                    "expected": "at least one alternative must hold",
                    "alternatives": [f for _, fails in results for f in fails],
                }
            ]

        if "not" in cond:
            inner, _ = self._evaluate_condition(cond["not"], variables)
            if not inner:
                return True, []
            return False, [
                {
                    "condition": self._describe(cond),
                    "actual": "inner condition is true",
                    "expected": "inner condition must be false",
                }
            ]

        if "expr" in cond:
            text = cond["expr"]
            tree = self._parse_expression(text)
            value = self._eval_node(tree.body, variables)
            if not isinstance(value, bool):
                raise OperandTypeError(f"expression '{text}' does not evaluate to a boolean")
            if value:
                return True, []
            return False, [self._expression_failure(text, tree, variables)]

        return self._evaluate_leaf(cond, variables)

    def _evaluate_leaf(
        self, cond: dict[str, Any], variables: Mapping[str, Any]
    ) -> tuple[bool, list[dict[str, Any]]]:
        var, op = cond["var"], cond["op"]

        if op == "exists":
            present = var in variables and variables[var] is not None
            expected = cond.get("value", True)
            if present == expected:
                return True, []
            return False, [{"condition": self._describe(cond), "actual": present, "expected": expected}]

        if op == "not_empty":
            actual = self._is_non_empty(variables[var])
            expected = cond.get("value", True)
            if actual == expected:
                return True, []
            return False, [{"condition": self._describe(cond), "actual": actual, "expected": expected}]

        left = self._resolve_variable(var, variables)
        right = (
            self._resolve_variable(cond["value_var"], variables)
            if "value_var" in cond
            else cond["value"]
        )
        if self._compare_values(left, op, right):
            return True, []
        return False, [
            {
                "condition": self._describe(cond),
                "actual": left,
                "expected": right,
                "operator": op,
            }
        ]

    @staticmethod
    def _is_number(value: Any) -> bool:
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)

    @staticmethod
    def _is_non_empty(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (bytes, list, tuple, set, frozenset, dict)):
            return len(value) > 0
        return True

    def _compare_values(self, left: Any, op: str, right: Any) -> bool:
        """So sánh hai giá trị theo toán tử DSL (Decimal so với float được đổi về Decimal)."""
        if isinstance(left, Decimal) and isinstance(right, float):
            right = Decimal(repr(right))
        elif isinstance(right, Decimal) and isinstance(left, float):
            left = Decimal(repr(left))
        if op == "==":
            return bool(left == right)
        if op == "!=":
            return bool(left != right)
        if op in ("in", "not_in"):
            if not isinstance(right, (list, tuple, set, frozenset)):
                raise OperandTypeError(f"operator {op!r} requires a list on the right-hand side")
            return (left in right) == (op == "in")
        if op in _ORDER_OPERATORS:
            if not (self._is_number(left) and self._is_number(right)):
                raise OperandTypeError(
                    f"operator {op!r} requires numeric operands, got "
                    f"{type(left).__name__} and {type(right).__name__}"
                )
            return bool(_ORDER_OPERATORS[op](left, right))
        raise InvalidGuardError(f"unsupported operator {op!r}")

    # --- mô tả / lý do ---------------------------------------------------
    def _describe(self, cond: Any) -> str:
        """Biểu diễn điều kiện dưới dạng chuỗi dễ đọc."""
        cond = self._normalize_condition(cond)
        if "all" in cond:
            return "(" + " AND ".join(self._describe(c) for c in cond["all"]) + ")"
        if "any" in cond:
            return "(" + " OR ".join(self._describe(c) for c in cond["any"]) + ")"
        if "not" in cond:
            return f"NOT {self._describe(cond['not'])}"
        if "expr" in cond:
            return str(cond["expr"])
        var, op = cond["var"], cond["op"]
        negated = cond.get("value", True) is False
        if op == "exists":
            return f"{var} does not exist" if negated else f"{var} exists"
        if op == "not_empty":
            return f"{var} is empty" if negated else f"{var} is not empty"
        right = cond["value_var"] if "value_var" in cond else _fmt(cond["value"])
        return f"{var} {op} {right}"

    @staticmethod
    def _format_failure(failure: dict[str, Any]) -> str:
        if "operator" in failure:
            return (
                f"{failure['condition']} is false "
                f"({_fmt(failure['actual'])} {failure['operator']} {_fmt(failure['expected'])})"
            )
        return f"{failure['condition']} is false"

    def _expression_failure(
        self, text: str, tree: ast.Expression, variables: Mapping[str, Any]
    ) -> dict[str, Any]:
        body = tree.body
        if isinstance(body, ast.Compare) and len(body.ops) == 1:
            return {
                "condition": text,
                "actual": self._eval_node(body.left, variables),
                "expected": self._eval_node(body.comparators[0], variables),
                "operator": _AST_COMPARE[type(body.ops[0])],
            }
        return {"condition": text, "actual": False, "expected": True}

    # ------------------------------------------------------------------
    # Bộ phân tích biểu thức an toàn (ast whitelist, không eval)
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_names(tree: ast.AST) -> Iterable[ast.Name]:
        """Các node Name trong biểu thức, bỏ qua tên hàm của lời gọi hàm."""
        func_nodes = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        return [n for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in func_nodes]

    def _parse_expression(self, text: Any) -> ast.Expression:
        """Parse + kiểm tra whitelist. Kết quả được cache theo chuỗi.

        * Sai cú pháp / tên không xác định -> InvalidGuardError.
        * Cấu trúc hợp lệ nhưng không được phép -> UnsupportedExpressionError.
        """
        if not isinstance(text, str) or not text.strip():
            raise InvalidGuardError("expression must be a non-empty string")
        cached = self._expr_cache.get(text)
        if cached is not None:
            return cached
        try:
            tree = ast.parse(text.strip(), mode="eval")
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            detail = getattr(exc, "msg", None) or type(exc).__name__
            raise InvalidGuardError(f"cannot parse expression '{text}': {detail}") from exc

        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_AST_NODES):
                raise UnsupportedExpressionError(
                    f"unsupported construct '{type(node).__name__}' in expression '{text}'"
                )
            if isinstance(node, ast.Constant) and not isinstance(node.value, (bool, int, float, str)):
                raise UnsupportedExpressionError(f"unsupported constant {node.value!r} in expression '{text}'")
            if isinstance(node, ast.Call):
                self._validate_call(node, text)
        for name in self._iter_names(tree):
            if name.id not in self._variables and name.id not in self._enum_literals:
                raise InvalidGuardError(
                    f"unknown identifier '{name.id}' in expression '{text}' "
                    "(not a declared variable or enum value)"
                )
        self._expr_cache[text] = tree
        return tree

    def _validate_call(self, node: ast.Call, text: str) -> None:
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            name = node.func.id if isinstance(node.func, ast.Name) else type(node.func).__name__
            raise UnsupportedExpressionError(
                f"function {name!r} is not supported in expression '{text}' (allowed: {sorted(_FUNCTIONS)})"
            )
        if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
            raise UnsupportedExpressionError(f"keyword/star arguments are not supported in '{text}'")
        arity = _FUNCTIONS[node.func.id][0]
        if len(node.args) != arity:
            raise InvalidGuardError(f"{node.func.id} takes {arity} arguments, got {len(node.args)} in '{text}'")
        first = node.args[0]
        if not (isinstance(first, ast.Name) and self._variables.get(first.id, {}).get("type") == "list"):
            raise InvalidGuardError(f"first argument of {node.func.id} must be a declared list variable in '{text}'")
        for arg in node.args[1:]:
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                raise InvalidGuardError(f"field arguments of {node.func.id} must be string literals in '{text}'")

    def _expression_variables(self, tree: ast.Expression) -> set[str]:
        return {n.id for n in self._iter_names(tree) if n.id in self._variables}

    @staticmethod
    def _require_bool(value: Any, context: str) -> bool:
        if not isinstance(value, bool):
            raise OperandTypeError(f"'{context}' requires boolean operands, got {type(value).__name__}")
        return value

    def _eval_node(self, node: ast.AST, variables: Mapping[str, Any]) -> Any:
        """Đệ quy tính giá trị một node đã qua whitelist."""
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self._variables:
                return self._resolve_variable(node.id, variables)
            if node.id in self._enum_literals:
                return node.id  # tên enum "trần" như CANCELLED -> chuỗi 'CANCELLED'
            raise InvalidGuardError(f"unknown identifier '{node.id}'")
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return [self._eval_node(item, variables) for item in node.elts]
        if isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand, variables)
            if isinstance(node.op, ast.Not):
                return not self._require_bool(operand, "not")
            if not self._is_number(operand):
                raise OperandTypeError("unary '+'/'-' requires a number")
            return -operand if isinstance(node.op, ast.USub) else +operand
        if isinstance(node, ast.BoolOp):
            values = [self._require_bool(self._eval_node(v, variables), "and/or") for v in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.BinOp):
            return self._eval_binop(node, variables)
        if isinstance(node, ast.Compare):
            left = self._eval_node(node.left, variables)
            result = True
            for op_node, comparator in zip(node.ops, node.comparators):
                right = self._eval_node(comparator, variables)
                result = self._compare_values(left, _AST_COMPARE[type(op_node)], right) and result
                left = right
            return result
        if isinstance(node, ast.Call):
            _, func = _FUNCTIONS[node.func.id]  # type: ignore[attr-defined]
            return func(*[self._eval_node(arg, variables) for arg in node.args])
        raise UnsupportedExpressionError(f"unsupported construct '{type(node).__name__}'")

    def _eval_binop(self, node: ast.BinOp, variables: Mapping[str, Any]) -> Any:
        left = self._eval_node(node.left, variables)
        right = self._eval_node(node.right, variables)
        if not (self._is_number(left) and self._is_number(right)):
            raise OperandTypeError(
                f"arithmetic requires numeric operands, got {type(left).__name__} and {type(right).__name__}"
            )
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise EvaluationError("division by zero")
            return _arith(operator.truediv, left, right)
        return _arith(_ARITHMETIC[type(node.op)], left, right)

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------
    def check_invariants(
        self,
        variables: Mapping[str, Any],
        invariant_ids: Iterable[str] | None = None,
        *,
        current_state: str | None = None,
        scopes: Iterable[str] | None = None,
    ) -> InvariantReport:
        """Kiểm tra invariant của mô hình với ``variables``.

        Mỗi invariant ra đúng một trong các trạng thái:

        * ``SATISFIED`` / ``VIOLATED``: đã được đánh giá đầy đủ.
        * ``SKIPPED``: không áp dụng (``reason_code`` = NOT_APPLICABLE_STATE hoặc
          NOT_APPLICABLE_CONDITION) hoặc áp dụng nhưng thiếu dữ liệu
          (``MISSING_VARIABLE``). Không bao giờ bị coi là VIOLATED.
        * ``UNSUPPORTED``: mô hình khai báo không đánh giá được theo máy.
        * ``ERROR``: dữ liệu sai kiểu, biểu thức lỗi hoặc lỗi khi tính.

        ``current_state``: nếu có, các invariant có ``applies_in_states`` không chứa
        state này sẽ bị SKIPPED. ``scopes``: chỉ kiểm tra các scope được liệt kê.
        """
        if not isinstance(variables, Mapping):
            raise TypeError("variables must be a mapping (dict) of EFSM variables")
        wanted_ids = set(invariant_ids) if invariant_ids is not None else None
        wanted_scopes = set(scopes) if scopes is not None else None
        results = [
            self._check_one_invariant(inv, variables, current_state)
            for inv in self._invariants
            if (wanted_ids is None or inv["id"] in wanted_ids)
            and (wanted_scopes is None or inv.get("scope") in wanted_scopes)
        ]
        return InvariantReport(results)

    def _check_one_invariant(
        self, inv: dict[str, Any], variables: Mapping[str, Any], current_state: str | None
    ) -> InvariantResult:
        base: dict[str, Any] = {
            "invariant_id": inv["id"],
            "scope": inv.get("scope"),
            "expression": inv.get("expression"),
            "covered_by": list(inv.get("covered_by", [])),
            "rule_refs": list(inv.get("rule_refs", [])),
        }

        def result(status: InvariantStatus, code: str, reason: str, **extra: Any) -> InvariantResult:
            return InvariantResult(status=status, reason_code=code, reason=reason, **base, **extra)

        # 1. Invariant mô hình khai báo là không đánh giá được
        if inv.get("evaluable") is False or (
            inv.get("kind", "state_predicate") == "state_predicate" and "check" not in inv
        ):
            reason = inv.get("unsupported_reason") or "invariant has no machine-readable check"
            return result(InvariantStatus.UNSUPPORTED, "UNSUPPORTED", reason)

        # 2. Invariant cấu trúc: kiểm tra trên mô hình, không cần biến runtime
        if inv.get("kind") == "model_structure":
            try:
                holds, offenders = self._check_structure(inv["structure"])
            except GuardEvaluatorError as exc:
                return result(InvariantStatus.ERROR, "INVALID_EXPRESSION", str(exc))
            if holds:
                return result(InvariantStatus.SATISFIED, "SATISFIED", "Model structure satisfies the invariant")
            return result(
                InvariantStatus.VIOLATED,
                "VIOLATED",
                f"Model structure violates the invariant: {offenders}",
                failed_conditions=[{"condition": inv.get("expression"), "offending_transitions": offenders}],
            )

        # 3. Phạm vi áp dụng theo state
        states = inv.get("applies_in_states")
        if states and current_state is not None and current_state not in states:
            return result(
                InvariantStatus.SKIPPED,
                "NOT_APPLICABLE_STATE",
                f"Invariant applies only in states {states}; current state is '{current_state}'",
            )

        used = self._variables_used(inv, variables)

        # 4. Điều kiện áp dụng (applies_when)
        if inv.get("applies_when") is not None:
            try:
                applicable, _ = self._run_condition(inv["applies_when"], variables)
            except GuardEvaluatorError as exc:
                return self._invariant_error(exc, result, used, "applies_when")
            if not applicable:
                return result(
                    InvariantStatus.SKIPPED,
                    "NOT_APPLICABLE_CONDITION",
                    f"Precondition not met: {self._describe(inv['applies_when'])}",
                    variables_used=used,
                )

        # 5. Đánh giá check
        try:
            satisfied, failures = self._run_condition(inv["check"], variables)
        except GuardEvaluatorError as exc:
            return self._invariant_error(exc, result, used, "check")
        if satisfied:
            return result(InvariantStatus.SATISFIED, "SATISFIED", "Invariant holds", variables_used=used)
        return result(
            InvariantStatus.VIOLATED,
            "VIOLATED",
            "Invariant violated: " + "; ".join(self._format_failure(f) for f in failures),
            failed_conditions=failures,
            variables_used=used,
        )

    def _invariant_error(
        self, exc: GuardEvaluatorError, result: Callable[..., InvariantResult], used: dict[str, Any], part: str
    ) -> InvariantResult:
        """Ánh xạ lỗi khi đánh giá invariant. Thiếu biến KHÔNG bao giờ thành VIOLATED."""
        if isinstance(exc, MissingVariableError):
            return result(
                InvariantStatus.SKIPPED, "MISSING_VARIABLE", f"{part}: {exc}",
                missing_variables=exc.missing, variables_used=used,
            )  # fmt: skip
        if isinstance(exc, UnsupportedExpressionError):
            return result(InvariantStatus.UNSUPPORTED, "UNSUPPORTED_EXPRESSION", f"{part}: {exc}", variables_used=used)
        if isinstance(exc, (InvalidVariableTypeError, OperandTypeError)):
            return result(InvariantStatus.ERROR, "TYPE_ERROR", f"{part}: {exc}", variables_used=used)
        if isinstance(exc, EvaluationError):
            return result(InvariantStatus.ERROR, "EVALUATION_ERROR", f"{part}: {exc}", variables_used=used)
        return result(InvariantStatus.ERROR, "INVALID_EXPRESSION", f"{part}: {exc}", variables_used=used)

    def _variables_used(self, inv: dict[str, Any], variables: Mapping[str, Any]) -> dict[str, Any]:
        """Giá trị hiện có của các biến mà invariant tham chiếu (phục vụ debug)."""
        names: dict[str, str] = {}
        for key in ("applies_when", "check"):
            if inv.get(key) is not None:
                try:
                    self._analyze_condition(inv[key], names, key)
                except GuardEvaluatorError:
                    pass
        return {name: variables[name] for name in sorted(names) if name in variables}

    def _check_structure(self, spec: dict[str, Any]) -> tuple[bool, list[str]]:
        """Kiểm tra invariant cấu trúc trên chính mô hình EFSM."""
        self._validate_structure(spec)
        if spec["type"] == "no_event_from_state":
            events = spec.get("events")
            offenders = [
                tid
                for tid, tr in self._transitions.items()
                if tr["source"] == spec["state"] and (events is None or tr.get("event") in events)
            ]
        else:
            offenders = [
                tid
                for tid, tr in self._transitions.items()
                if tr.get("event") == spec["event"] and tr["source"] not in spec["states"]
            ]
        return not offenders, offenders

    # ------------------------------------------------------------------
    # Mô phỏng transition (KHÔNG chạm database / website)
    # ------------------------------------------------------------------
    def apply_transition(
        self,
        transition_id: str,
        current_state: str,
        variables: Mapping[str, Any],
        *,
        sync_order_status: bool = False,
        verify_invariants: bool = False,
    ) -> TransitionOutcome:
        """Mô phỏng transition trong bộ nhớ; ``variables`` gốc KHÔNG bị sửa.

        * Chỉ action ``assign`` được thực thi (tuần tự, atomic: một action lỗi thì
          không action nào được áp dụng). Action ``effect`` được liệt kê trong
          ``backend_effects``; kết quả luôn có ``simulated=True``.
        * Thiếu dữ liệu cho action -> ``applied=False`` kèm ``missing_variables``;
          evaluator không tự đoán giá trị.
        * ``verify_invariants=True``: kiểm tra invariant trên state/biến mới và
          gắn vào ``invariant_report`` (kết quả KHÔNG làm đảo ngược việc áp dụng).
        * ``sync_order_status=True``: nếu state đích là một giá trị của
          ``order_status`` mà các action chưa đặt, đặt ``order_status`` = state đích.
        """
        check = self.check_transition(transition_id, current_state, variables)
        original = copy.deepcopy(dict(variables))
        if not check.enabled:
            return TransitionOutcome(
                False, check, current_state, original, error=check.reason, error_status=check.status,
                missing_variables=list(check.missing_variables),
            )  # fmt: skip

        transition = self._transitions[transition_id]
        new_vars = copy.deepcopy(dict(variables))
        executed: list[str] = []
        effects: list[str] = []
        skipped: list[dict[str, str]] = []
        warnings: list[str] = []

        for raw in transition.get("actions", []):
            try:
                action = self._normalize_action(raw)
                label = self._describe_action(action)
                if action["type"] == "effect":
                    effects.append(label)
                    continue
                outcome = self._execute_assign(action, new_vars)
            except GuardEvaluatorError as exc:
                status, missing, _ = self._classify(exc)
                return TransitionOutcome(
                    False, check, current_state, original,
                    error=f"Action failed: {exc}", error_status=status, missing_variables=missing,
                )  # fmt: skip
            if outcome == "WHEN_FALSE":
                skipped.append({"action": label, "code": "WHEN_FALSE", "reason": "'when' condition is false"})
            else:
                executed.append(label)
                if outcome:
                    warnings.append(outcome)

        target = transition["target"]
        allowed = self._variables.get("order_status", {}).get("allowed_values", [])
        if target in allowed:
            if sync_order_status and new_vars.get("order_status") != target:
                new_vars["order_status"] = target
            elif not sync_order_status and new_vars.get("order_status") != target:
                warnings.append(
                    f"order_status was not set to '{target}' by the model's actions; "
                    "pass sync_order_status=True to align it with the target state"
                )

        report = self.check_invariants(new_vars, current_state=target) if verify_invariants else None
        return TransitionOutcome(
            True, check, target, new_vars, executed, effects, skipped, warnings, invariant_report=report
        )

    def _normalize_action(self, raw: Any) -> dict[str, Any]:
        """Đưa action về dạng dict. Chuỗi cũ 'x := expr' -> assign nếu parse được,
        ngược lại coi là ``effect`` mô tả."""
        if isinstance(raw, str):
            match = _ASSIGNMENT_RE.match(raw)
            if match:
                target, rhs = match.groups()
                try:
                    self._parse_expression(rhs)
                except GuardEvaluatorError:
                    pass
                else:
                    return {"type": "assign", "var": target, "expr": rhs, "legacy": True}
            return {"type": "effect", "description": raw, "legacy": True}
        if isinstance(raw, dict):
            if raw.get("type") not in ("assign", "effect"):
                raise InvalidGuardError(f"unknown action type {raw.get('type')!r}")
            return raw
        raise InvalidGuardError(f"action must be a string or object, got {type(raw).__name__}")

    def _analyze_action(self, action: dict[str, Any]) -> tuple[set[str], set[str], str | None]:
        """Kiểm tra tĩnh một action; trả về (biến luôn đọc, biến đọc có điều kiện, biến ghi)."""
        if action["type"] == "effect":
            if not isinstance(action.get("description"), str) or not action["description"].strip():
                raise InvalidGuardError("effect action needs a non-empty 'description'")
            return set(), set(), None

        target = action.get("var")
        if not isinstance(target, str) or (target not in self._variables and not action.get("legacy")):
            raise InvalidGuardError(f"assign target {target!r} is not declared in the model variables")
        if ("expr" in action) == ("value" in action):
            raise InvalidGuardError(f"assign to {target!r} needs exactly one of 'expr' or 'value'")
        expr_reads: set[str] = set()
        if "expr" in action:
            expr_reads = self._expression_variables(self._parse_expression(action["expr"]))
        elif not isinstance(action["value"], (bool, int, float, str)):
            raise InvalidGuardError(f"assign value for {target!r} must be a boolean, number or string")
        if action.get("when") is not None:
            requirements: dict[str, str] = {}
            self._analyze_condition(action["when"], requirements, "when")
            return {n for n, m in requirements.items() if m != "optional"}, expr_reads, target
        return expr_reads, set(), target

    def _describe_action(self, action: dict[str, Any]) -> str:
        if action["type"] == "effect":
            return str(action["description"])
        rhs = action["expr"] if "expr" in action else _fmt(action["value"])
        text = f"{action['var']} := {rhs}"
        if action.get("when") is not None:
            text += f"  [when {self._describe(action['when'])}]"
        return text

    def _execute_assign(self, action: dict[str, Any], variables: dict[str, Any]) -> str | None:
        """Thực thi một action gán. Trả về 'WHEN_FALSE', cảnh báo (str) hoặc None."""
        if action.get("when") is not None:
            holds, _ = self._run_condition(action["when"], variables)
            if not holds:
                return "WHEN_FALSE"
        target = action["var"]
        if "expr" in action:
            tree = self._parse_expression(action["expr"])
            self._validate_variables({n: "typed" for n in self._expression_variables(tree)}, variables)
            value = self._eval_node(tree.body, variables)
        else:
            value = action["value"]

        spec = self._variables.get(target)
        warning = None
        if spec is None:
            warning = f"'{target}' is not declared in the model variables (value stored anyway)"
        elif not self._type_ok(spec.get("type"), value):
            raise InvalidVariableTypeError(
                [
                    {
                        "variable": target,
                        "expected_type": spec.get("type"),
                        "actual_type": type(value).__name__,
                        "actual": value,
                    }
                ]
            )
        variables[target] = value
        return warning


# --------------------------------------------------------------------------
# Chạy thử: python guard_evaluator.py [duong_dan/efsm_model.json]
# --------------------------------------------------------------------------
def _demo(model_path: Path) -> None:
    evaluator = GuardEvaluator(model_path)
    print(evaluator)

    print("\n== T02: update_quantity, quantity=3, stock=10 ==")
    state = {"quantity": 3, "stock": 10, "product_exists": True, "cart_item_exists": True}
    print(evaluator.check_transition("T02", "CART", state).to_json())

    print("\n== T02: quantity=12, stock=10 ==")
    print(evaluator.check_transition("T02", "CART", {**state, "quantity": 12}).to_json())

    print("\n== T01 (guard dạng expr): current_qty=8, quantity=5, stock=10 ==")
    print(evaluator.check_transition("T01", "CART", {**state, "quantity": 5, "current_qty": 8}).to_json())

    print("\n== Invariants (state PAID, thiếu một số biến) ==")
    report = evaluator.check_invariants({"order_status": "PAID", "payment_count": 1}, current_state="PAID")
    for item in report.results:
        print(f"{item.invariant_id:7} {item.status.value:11} {item.reason_code:24} {item.reason}")


if __name__ == "__main__":
    default_path = Path(__file__).with_name("efsm_model.json")
    _demo(Path(sys.argv[1]) if len(sys.argv) > 1 else default_path)
