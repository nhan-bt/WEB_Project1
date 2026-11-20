"""
test_data_generator.py - Test Data Generator (phần lõi, mức GUARD) cho mô hình EFSM.

Với mỗi guard, module phân tích biến và ràng buộc, rồi sinh dữ liệu hợp lệ / không hợp lệ bằng
Equivalence Partitioning (EP) và Boundary Value Analysis (BVA), sau đó **kiểm tra lại từng bộ dữ liệu
bằng GuardEvaluator** (module này không tự quyết định một bộ dữ liệu là VERIFIED).

Phạm vi: dữ liệu cho MỘT guard / một transition. Chưa sinh dữ liệu xuyên suốt test path, không gọi API,
không truy cập database, không dùng eval/exec, không sửa EFSM / GuardEvaluator / TestPathGenerator.

Nguyên tắc
----------
* Giá trị kỳ vọng (``expected_guard``) được SUY RA ĐỘC LẬP bằng một bộ biểu diễn trung gian (IR) nhỏ của
  chính module này; GuardEvaluator là bên kiểm tra. Hai bên không khớp -> ``UNVERIFIED``.
* Biên của BVA được TÍNH từ toán tử và giá trị biên thực tế (kể cả khi biên phụ thuộc biến khác đã cố định
  bằng ngữ cảnh, ví dụ ``current_qty + quantity <= stock``). Không có giá trị nào bị hardcode.
* Miền giá trị lấy từ EFSM: ``type``, ``allowed_values``. EFSM hiện KHÔNG khai báo min/max cho biến số, nên
  miền số được ghi là ``UNDETERMINED_IN_EFSM`` và module không tự đặt ràng buộc nghiệp vụ.
* Kết quả giải: ``SAT`` / ``UNSAT`` / ``UNKNOWN`` / ``UNSUPPORTED``. ``UNSAT`` chỉ khi CHỨNG MINH được
  (suy luận khoảng trên số nguyên, hoặc duyệt hết miền hữu hạn đã khai báo như boolean/enum). Hết ngân sách
  tìm kiếm luôn là ``UNKNOWN``.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from math import ceil, floor
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from guard_evaluator import GuardEvaluator, GuardEvaluatorError, GuardStatus

__all__ = [
    "TestDataGenerator", "GuardDataSet", "DataSample", "SolveResult", "GuardAnalysis",
    "VerificationStatus", "SolveStatus", "DataGeneratorError", "ModelFormatError", "UnsupportedGuard",
    "OFF_DOMAIN_VALUE", "main",
    "ApiMapping", "ShadowState", "PathDataGenerator", "Readiness", "generate_test_cases", "build_coverage",
    "request_signature",
]

SCHEMA_VERSION = "1.0"
GENERATOR_NAME = "TestDataGenerator"
OFF_DOMAIN_VALUE = "__OUT_OF_DOMAIN__"  # giá trị chuỗi nằm ngoài miền khai báo (chỉ dùng cho EP không hợp lệ)
_NUMERIC_TYPES = ("integer", "decimal")
_DEFAULT_INT_POOL = (0, 1, -1, 2, 5, 10, 100)  # ứng viên tìm kiếm, KHÔNG phải miền nghiệp vụ
_NEGATE = {"==": "!=", "!=": "==", ">": "<=", ">=": "<", "<": ">=", "<=": ">", "in": "not_in", "not_in": "in"}
_FLIP = {">": "<", ">=": "<=", "<": ">", "<=": ">=", "==": "==", "!=": "!="}
_COMPARE_OPS = set(_NEGATE)


class DataGeneratorError(Exception):
    """Lớp cơ sở cho lỗi của module."""


class ModelFormatError(DataGeneratorError):
    """Không nạp được mô hình / file đầu vào."""


class UnsupportedGuard(DataGeneratorError):
    """Guard dùng cấu trúc mà bộ sinh dữ liệu chưa hỗ trợ."""


class _NotEvaluable(Exception):
    """Biểu thức IR không tính được với dữ liệu hiện có (thiếu biến, sai kiểu, chia 0)."""


class _DnfLimit(Exception):
    pass


class VerificationStatus:
    VERIFIED_VALID = "VERIFIED_VALID"
    VERIFIED_INVALID = "VERIFIED_INVALID"
    UNVERIFIED = "UNVERIFIED"
    UNSUPPORTED = "UNSUPPORTED"


class SolveStatus:
    SAT = "SAT"
    UNSAT = "UNSAT"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


# --------------------------------------------------------------------------
# Biểu diễn trung gian (IR) của guard - chỉ để phân tích và dự đoán kết quả
# --------------------------------------------------------------------------
Term = tuple  # ("var", name) | ("const", v) | ("list", (v, ...)) | ("neg", t) | ("bin", op, l, r)


@dataclass(frozen=True)
class Atom:
    op: str
    left: Term
    right: Term
    text: str


@dataclass(frozen=True)
class And:
    children: tuple


@dataclass(frozen=True)
class Or:
    children: tuple


@dataclass(frozen=True)
class Not:
    child: Any


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal, Fraction)) and not isinstance(value, bool)


def _frac(value: Any) -> Fraction:
    return Fraction(repr(value)) if isinstance(value, float) else Fraction(value)


def term_vars(term: Term) -> list[str]:
    kind = term[0]
    if kind == "var":
        return [term[1]]
    if kind == "neg":
        return term_vars(term[1])
    if kind == "bin":
        return term_vars(term[2]) + term_vars(term[3])
    return []


def iter_atoms(node: Any) -> Iterable[Atom]:
    if isinstance(node, Atom):
        yield node
    elif isinstance(node, (And, Or)):
        for child in node.children:
            yield from iter_atoms(child)
    elif isinstance(node, Not):
        yield from iter_atoms(node.child)


def node_vars(node: Any) -> list[str]:
    """Biến theo thứ tự xuất hiện đầu tiên."""
    seen: dict[str, None] = {}
    for atom in iter_atoms(node):
        for name in term_vars(atom.left) + term_vars(atom.right):
            seen.setdefault(name, None)
    return list(seen)


def describe(node: Any) -> str:
    if isinstance(node, Atom):
        return node.text
    if isinstance(node, And):
        return "(" + " AND ".join(describe(c) for c in node.children) + ")"
    if isinstance(node, Or):
        return "(" + " OR ".join(describe(c) for c in node.children) + ")"
    return f"NOT {describe(node.child)}"


def eval_term(term: Term, a: Mapping[str, Any]) -> Any:
    kind = term[0]
    if kind == "var":
        if term[1] not in a:
            raise _NotEvaluable(f"missing {term[1]}")
        return a[term[1]]
    if kind == "const":
        return term[1]
    if kind == "list":
        return list(term[1])
    if kind == "neg":
        value = eval_term(term[1], a)
        if not _is_num(value):
            raise _NotEvaluable("unary minus on non-number")
        return -value
    left, right = eval_term(term[2], a), eval_term(term[3], a)
    if not (_is_num(left) and _is_num(right)):
        raise _NotEvaluable("arithmetic on non-numbers")
    op = term[1]
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    if right == 0:
        raise _NotEvaluable("division by zero")
    return left / right


def eval_atom(atom: Atom, a: Mapping[str, Any]) -> bool:
    left, right = eval_term(atom.left, a), eval_term(atom.right, a)
    op = atom.op
    if op == "==":
        return bool(left == right)
    if op == "!=":
        return bool(left != right)
    if op in ("in", "not_in"):
        if not isinstance(right, list):
            raise _NotEvaluable("in/not_in needs a list")
        return (left in right) == (op == "in")
    if not (_is_num(left) and _is_num(right)):
        raise _NotEvaluable("ordering on non-numbers")
    return {">": left > right, ">=": left >= right, "<": left < right, "<=": left <= right}[op]


def eval_node(node: Any, a: Mapping[str, Any]) -> bool:
    """Tính IR, không short-circuit (giống GuardEvaluator: thiếu dữ liệu ở đâu cũng là lỗi)."""
    if isinstance(node, Atom):
        return eval_atom(node, a)
    if isinstance(node, And):
        return all([eval_node(c, a) for c in node.children])
    if isinstance(node, Or):
        return any([eval_node(c, a) for c in node.children])
    return not eval_node(node.child, a)


def to_dnf(node: Any, negate: bool = False, cap: int = 256) -> list[tuple[Atom, ...]]:
    """Dạng tuyển chuẩn (đẩy NOT xuống nguyên tử). Vượt ``cap`` hạng tử -> _DnfLimit."""
    if isinstance(node, Atom):
        if negate:
            return [(Atom(_NEGATE[node.op], node.left, node.right, f"NOT({node.text})"),)]
        return [(node,)]
    if isinstance(node, Not):
        return to_dnf(node.child, not negate, cap)
    conjunctive = isinstance(node, And) != negate  # And thường, hoặc Or bị phủ định
    parts = [to_dnf(c, negate, cap) for c in node.children]
    if conjunctive:
        terms: list[tuple[Atom, ...]] = [()]
        for part in parts:
            terms = [t + p for t in terms for p in part]
            if len(terms) > cap:
                raise _DnfLimit
        return terms
    terms = [t for part in parts for t in part]
    if len(terms) > cap:
        raise _DnfLimit
    return terms


# --- tuyến tính hóa: dùng để rút biên từ nguyên tử khi các biến khác đã cố định -------------
def linearize(term: Term, fixed: Mapping[str, Any]) -> tuple[dict[str, Fraction], Fraction] | None:
    kind = term[0]
    if kind == "const":
        return ({}, _frac(term[1])) if _is_num(term[1]) else None
    if kind == "var":
        name = term[1]
        if name in fixed:
            return ({}, _frac(fixed[name])) if _is_num(fixed[name]) else None
        return ({name: Fraction(1)}, Fraction(0))
    if kind == "neg":
        inner = linearize(term[1], fixed)
        return None if inner is None else ({k: -v for k, v in inner[0].items()}, -inner[1])
    if kind != "bin":
        return None
    op = term[1]
    left, right = linearize(term[2], fixed), linearize(term[3], fixed)
    if left is None or right is None:
        return None
    if op in ("+", "-"):
        sign = 1 if op == "+" else -1
        coeffs = dict(left[0])
        for name, value in right[0].items():
            coeffs[name] = coeffs.get(name, Fraction(0)) + sign * value
        return coeffs, left[1] + sign * right[1]
    if op == "*":
        if not left[0]:
            return {k: left[1] * v for k, v in right[0].items()}, left[1] * right[1]
        if not right[0]:
            return {k: right[1] * v for k, v in left[0].items()}, left[1] * right[1]
    return None  # phép chia và tích hai biến: phi tuyến


def atom_bound(atom: Atom, var: str, fixed: Mapping[str, Any]):
    """Rút ràng buộc một biến từ nguyên tử khi mọi biến khác đã cố định.

    Trả về ``(op, k)`` nghĩa là ``var op k``; ``("in"/"not_in", [giá trị])``; ``("const", bool)`` nếu nguyên tử không
    còn phụ thuộc ``var``; hoặc None nếu không rút được (phi tuyến / còn biến tự do khác)."""
    if atom.op in ("in", "not_in"):
        if atom.left == ("var", var) and atom.right[0] == "list":
            return atom.op, list(atom.right[1])
        if var not in term_vars(atom.left):
            try:
                return "const", eval_atom(atom, fixed)
            except _NotEvaluable:
                return None
        return None
    lhs, rhs = linearize(atom.left, fixed), linearize(atom.right, fixed)
    if lhs is None or rhs is None:
        return None
    coeffs = {k: lhs[0].get(k, Fraction(0)) - rhs[0].get(k, Fraction(0)) for k in set(lhs[0]) | set(rhs[0])}
    coeffs = {k: v for k, v in coeffs.items() if v != 0}
    const = lhs[1] - rhs[1]
    if not coeffs:
        truth = {"==": const == 0, "!=": const != 0, ">": const > 0, ">=": const >= 0, "<": const < 0, "<=": const <= 0}[atom.op]
        return "const", truth
    if set(coeffs) != {var}:
        return None
    a = coeffs[var]
    return (atom.op if a > 0 else _FLIP[atom.op]), -const / a


def int_interval(bounds: Iterable[tuple]) -> dict[str, Any]:
    """Giao các ràng buộc trên số nguyên: ``lo``, ``hi`` (None = vô hạn), ``excluded``, ``allowed``, ``feasible``."""
    lo: int | None = None
    hi: int | None = None
    excluded: set[int] = set()
    allowed: set[int] | None = None
    impossible = False
    for op, k in bounds:
        if op == "const":
            impossible |= not k
        elif op in ("in", "not_in"):
            ints = {int(e) for e in k if _is_num(e) and _frac(e).denominator == 1}
            if op == "in":
                allowed = ints if allowed is None else allowed & ints
            else:
                excluded |= ints
        elif op == ">=":
            lo = ceil(k) if lo is None else max(lo, ceil(k))
        elif op == ">":
            lo = floor(k) + 1 if lo is None else max(lo, floor(k) + 1)
        elif op == "<=":
            hi = floor(k) if hi is None else min(hi, floor(k))
        elif op == "<":
            hi = ceil(k) - 1 if hi is None else min(hi, ceil(k) - 1)
        elif op == "==":
            if k.denominator == 1:
                lo = int(k) if lo is None else max(lo, int(k))
                hi = int(k) if hi is None else min(hi, int(k))
            else:
                impossible = True
        elif op == "!=" and k.denominator == 1:
            excluded.add(int(k))
    feasible = not impossible
    if feasible and lo is not None and hi is not None and lo > hi:
        feasible = False
    if feasible and allowed is not None:
        feasible = any((lo is None or e >= lo) and (hi is None or e <= hi) and e not in excluded for e in allowed)
    elif feasible and lo is not None and hi is not None:
        feasible = any(v not in excluded for v in range(lo, min(hi, lo + len(excluded) + 1) + 1))
    return {"lo": lo, "hi": hi, "excluded": excluded, "allowed": allowed, "feasible": feasible}


# --------------------------------------------------------------------------
# Dựng IR từ guard JSON (DSL của GuardEvaluator)
# --------------------------------------------------------------------------
class _IRBuilder:
    def __init__(self, variables: set[str], enum_literals: set[str]) -> None:
        self.variables = variables
        self.enums = enum_literals

    def build(self, cond: Any) -> Any:
        if isinstance(cond, str):
            cond = {"expr": cond}
        if not isinstance(cond, dict):
            raise UnsupportedGuard(f"condition must be an object or string, got {type(cond).__name__}")
        if "all" in cond:
            return And(tuple(self.build(c) for c in self._items(cond, "all")))
        if "any" in cond:
            return Or(tuple(self.build(c) for c in self._items(cond, "any")))
        if "not" in cond:
            return Not(self.build(cond["not"]))
        if "expr" in cond:
            return self._from_expr(cond["expr"])
        if "var" in cond:
            return self._leaf(cond)
        raise UnsupportedGuard(f"unrecognised condition {sorted(cond)}")

    @staticmethod
    def _items(cond: dict, key: str) -> list:
        items = cond[key]
        if not isinstance(items, list) or not items:
            raise UnsupportedGuard(f"'{key}' must be a non-empty list")
        return items

    def split_conjuncts(self, guard: Any) -> list[tuple[Any, dict]]:
        """Các điều kiện cấp cao của guard (làm phẳng ``all`` lồng nhau), kèm JSON để GuardEvaluator đánh giá riêng."""
        if isinstance(guard, str):
            guard = {"expr": guard}
        if isinstance(guard, dict) and "all" in guard and len(guard) == 1:
            out: list[tuple[Any, dict]] = []
            for child in self._items(guard, "all"):
                out.extend(self.split_conjuncts(child))
            return out
        if isinstance(guard, dict) and "expr" in guard and len(guard) == 1:
            tree = self._parse(guard["expr"])
            if isinstance(tree.body, ast.BoolOp) and isinstance(tree.body.op, ast.And):
                return [(self._cond(v), {"expr": ast.unparse(v)}) for v in tree.body.values]
        return [(self.build(guard), guard if isinstance(guard, dict) else {"expr": guard})]

    # --- lá của DSL ---
    def _leaf(self, cond: dict) -> Atom:
        var, op = cond["var"], cond.get("op")
        if var not in self.variables:
            raise UnsupportedGuard(f"variable {var!r} is not declared in the model")
        if op not in _COMPARE_OPS:
            raise UnsupportedGuard(f"operator {op!r} is not supported by the data generator")
        if "value_var" in cond:
            if cond["value_var"] not in self.variables:
                raise UnsupportedGuard(f"variable {cond['value_var']!r} is not declared in the model")
            right: Term = ("var", cond["value_var"])
            right_text = cond["value_var"]
        elif op in ("in", "not_in"):
            values = cond.get("value")
            if not isinstance(values, (list, tuple)):
                raise UnsupportedGuard(f"{op!r} needs a list value")
            right, right_text = ("list", tuple(values)), json.dumps(list(values), ensure_ascii=False)
        else:
            right, right_text = ("const", cond.get("value")), json.dumps(cond.get("value"), ensure_ascii=False)
        return Atom(op, ("var", var), right, f"{var} {op} {right_text}")

    # --- biểu thức expr (ast whitelist) ---
    @staticmethod
    def _parse(text: Any) -> ast.Expression:
        try:
            return ast.parse(str(text).strip(), mode="eval")
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise UnsupportedGuard(f"cannot parse expression {text!r}") from exc

    def _from_expr(self, text: Any) -> Any:
        return self._cond(self._parse(text).body)

    def _cond(self, node: ast.AST) -> Any:
        if isinstance(node, ast.BoolOp):
            children = tuple(self._cond(v) for v in node.values)
            return And(children) if isinstance(node.op, ast.And) else Or(children)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return Not(self._cond(node.operand))
        if isinstance(node, ast.Compare):
            ops = {ast.Eq: "==", ast.NotEq: "!=", ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<=",
                   ast.In: "in", ast.NotIn: "not_in"}
            atoms, left = [], node.left
            for op_node, right in zip(node.ops, node.comparators):
                if type(op_node) not in ops:
                    raise UnsupportedGuard(f"comparison {type(op_node).__name__} is not supported")
                piece = ast.Compare(left=left, ops=[op_node], comparators=[right])
                atoms.append(Atom(ops[type(op_node)], self._term(left), self._term(right), ast.unparse(piece)))
                left = right
            return atoms[0] if len(atoms) == 1 else And(tuple(atoms))
        raise UnsupportedGuard(f"unsupported construct {type(node).__name__} in a condition")

    def _term(self, node: ast.AST) -> Term:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (bool, int, float, str)):
                return ("const", node.value)
            raise UnsupportedGuard(f"unsupported constant {node.value!r}")
        if isinstance(node, ast.Name):
            if node.id in self.variables:
                return ("var", node.id)
            if node.id in self.enums:
                return ("const", node.id)
            raise UnsupportedGuard(f"unknown identifier {node.id!r}")
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = []
            for item in node.elts:
                term = self._term(item)
                if term[0] != "const":
                    raise UnsupportedGuard("list elements must be constants")
                values.append(term[1])
            return ("list", tuple(values))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            inner = self._term(node.operand)
            return ("neg", inner) if isinstance(node.op, ast.USub) else inner
        if isinstance(node, ast.BinOp) and type(node.op) in (ast.Add, ast.Sub, ast.Mult, ast.Div):
            op = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}[type(node.op)]
            return ("bin", op, self._term(node.left), self._term(node.right))
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else type(node.func).__name__
            raise UnsupportedGuard(f"function {name!r} is not supported by the data generator")
        raise UnsupportedGuard(f"unsupported construct {type(node).__name__}")


# --------------------------------------------------------------------------
# Kết quả
# --------------------------------------------------------------------------
@dataclass
class SolveResult:
    """Kết quả giải ràng buộc hữu hạn."""

    status: str
    assignment: dict[str, Any] | None = None
    reason: str = ""
    nodes: int = 0
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "assignment": self.assignment, "reason": self.reason,
                "search_nodes": self.nodes, "assumptions": self.assumptions}


@dataclass
class GuardAnalysis:
    """Kết quả phân tích guard: biến, miền, ràng buộc."""

    supported: bool
    expression: str
    reason: str | None = None
    variables: list[dict[str, Any]] = field(default_factory=list)
    conjuncts: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"supported": self.supported, "reason": self.reason, "expression": self.expression,
                "variables": self.variables, "conjuncts": self.conjuncts, "notes": self.notes}


@dataclass
class DataSample:
    """Một bộ dữ liệu cho guard."""

    sample_id: str
    data: dict[str, Any]
    expected_guard: bool | None
    technique: str
    techniques: list[str]
    expected_status: str | None = None  # chỉ dùng cho mẫu sai định dạng (MISSING_VARIABLE / TYPE_ERROR)
    partition: dict[str, Any] = field(default_factory=dict)
    target_variable: str | None = None
    changed_variables: list[str] = field(default_factory=list)
    violated_conditions: dict[str, Any] = field(default_factory=dict)
    verification_status: str = VerificationStatus.UNVERIFIED
    evaluator_verdict: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id, "data": self.data, "expected_guard": self.expected_guard,
            "expected_status": self.expected_status, "technique": self.technique, "techniques": self.techniques,
            "partition": self.partition, "target_variable": self.target_variable,
            "changed_variables": self.changed_variables, "violated_conditions": self.violated_conditions,
            "verification_status": self.verification_status, "evaluator_verdict": self.evaluator_verdict,
            "notes": self.notes,
        }


@dataclass
class GuardDataSet:
    """Dữ liệu sinh cho một guard / transition."""

    label: str
    guard: Any
    status: str  # OK | NO_GUARD | UNSUPPORTED | UNSAT | UNKNOWN
    reason: str | None
    analysis: GuardAnalysis
    transition_id: str | None = None
    source_state: str | None = None
    event: str | None = None
    rule_ids: list[str] = field(default_factory=list)
    base: dict[str, Any] = field(default_factory=dict)
    conjunct_notes: list[dict[str, Any]] = field(default_factory=list)
    samples: list[DataSample] = field(default_factory=list)

    def valid_samples(self) -> list[DataSample]:
        return [s for s in self.samples if s.expected_guard is True]

    def invalid_samples(self) -> list[DataSample]:
        return [s for s in self.samples if s.expected_guard is False or s.expected_status]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION, "transition_id": self.transition_id, "label": self.label,
            "source_state": self.source_state, "event": self.event, "rule_ids": self.rule_ids,
            "guard": self.guard, "status": self.status, "reason": self.reason,
            "analysis": self.analysis.to_dict(), "base": self.base, "conjunct_notes": self.conjunct_notes,
            "samples": [s.to_dict() for s in self.samples],
        }


# --------------------------------------------------------------------------
# Bộ sinh
# --------------------------------------------------------------------------
class TestDataGenerator:
    """Sinh và xác minh dữ liệu kiểm thử ở mức guard."""

    __test__ = False  # không để pytest coi đây là lớp test

    def __init__(
        self,
        model_path: str | Path,
        rules_path: str | Path | None = None,
        *,
        evaluator: GuardEvaluator | None = None,
        max_nodes: int = 20000,
        max_dnf_terms: int = 256,
        int_pool: Sequence[int] = _DEFAULT_INT_POOL,
    ) -> None:
        """``max_nodes``: ngân sách tìm kiếm mỗi thành phần ràng buộc. ``int_pool``: ứng viên số nguyên bổ sung cho tìm
        kiếm (chỉ là ứng viên, không phải miền nghiệp vụ)."""
        self.model_path = Path(model_path)
        try:
            self.evaluator = evaluator or GuardEvaluator(self.model_path)
        except GuardEvaluatorError as exc:
            raise ModelFormatError(f"cannot load model: {exc}") from exc
        self.model: dict[str, Any] = self.evaluator.model
        self.model_sha256 = hashlib.sha256(self.model_path.read_bytes()).hexdigest() if self.model_path.exists() else None
        self.max_nodes, self.max_dnf_terms, self.int_pool = max_nodes, max_dnf_terms, tuple(int_pool)
        self.rules: dict[str, dict[str, Any]] = {}
        if rules_path is not None:
            try:
                data = json.loads(Path(rules_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ModelFormatError(f"cannot read security rules: {exc}") from exc
            self.rules = {r["rule_id"]: r for r in data.get("rules", []) if "rule_id" in r}
        self._vars = {v["name"]: v for v in self.model["variables"]}
        enums = {s["id"] for s in self.model["states"]}
        for spec in self._vars.values():
            enums.update(a for a in spec.get("allowed_values", []) if isinstance(a, str))
        self._builder = _IRBuilder(set(self._vars), enums)
        self._transitions = {t["id"]: t for t in self.model["transitions"]}
        self._allow_off_domain = False  # chỉ bật tạm trong solve(allow_off_domain=True)

    # ------------------------------------------------------------------
    # Miền giá trị (chỉ từ EFSM)
    # ------------------------------------------------------------------
    def _origin(self, name: str) -> str | None:
        return self._vars.get(name, {}).get("origin")

    def _is_input(self, name: str) -> bool:
        """Biến do client cung cấp (hoặc chưa rõ nguồn: coi là có thể sinh)."""
        return self._origin(name) in ("request", None)

    def domain_of(self, name: str, constants: Iterable[Any] = ()) -> dict[str, Any]:
        spec = self._vars.get(name)
        if spec is None:
            return {"kind": "undeclared", "values": None, "complete": False, "range": "UNDETERMINED"}
        kind = spec.get("type")
        if kind == "boolean":
            return {"kind": "boolean", "values": [True, False], "complete": True, "range": "DECLARED_TYPE"}
        if kind == "string":
            declared = [a for a in spec.get("allowed_values", [])]
            if declared:
                return {"kind": "enum", "values": declared, "complete": True, "range": "DECLARED_ALLOWED_VALUES"}
            consts = [c for c in constants if isinstance(c, str)]
            return {"kind": "string", "values": list(dict.fromkeys(consts)), "complete": False,
                    "range": "UNDETERMINED_IN_EFSM"}
        if kind in _NUMERIC_TYPES:
            return {"kind": kind, "values": None, "complete": False, "range": "UNDETERMINED_IN_EFSM"}
        return {"kind": kind or "unknown", "values": None, "complete": False, "range": "UNSUPPORTED_TYPE"}

    # ------------------------------------------------------------------
    # Phân tích guard
    # ------------------------------------------------------------------
    def analyze_guard(self, guard: Any) -> GuardAnalysis:
        """Thu thập biến, kiểu, miền, vai trò và ràng buộc của guard; ghi rõ phần chưa hỗ trợ."""
        text = self._guard_text(guard)
        if guard is None:
            return GuardAnalysis(True, "(no guard)", notes=["Transition has no guard: always enabled."])
        try:
            parts = self._builder.split_conjuncts(guard)
            root = parts[0][0] if len(parts) == 1 else And(tuple(p[0] for p in parts))
            self._check_supported_types(root)
        except UnsupportedGuard as exc:
            return GuardAnalysis(False, text, reason=str(exc))
        names = node_vars(root)
        atoms = list(iter_atoms(root))
        variables = []
        for name in names:
            spec = self._vars[name]
            consts = [c for a in atoms if name in term_vars(a.left) + term_vars(a.right)
                      for side in (a.left, a.right) for c in (side[1] if side[0] == "list" else
                                                              [side[1]] if side[0] == "const" else [])]
            constraints = []
            for atom in atoms:
                involved = list(dict.fromkeys(term_vars(atom.left) + term_vars(atom.right)))
                if name not in involved:
                    continue
                others = [v for v in involved if v != name]
                const_bound = None
                if not others:
                    const_bound = atom.right[1] if atom.left == ("var", name) and atom.right[0] in ("const", "list") else None
                constraints.append({
                    "condition": atom.text, "operator": atom.op, "constant_bound": const_bound,
                    "depends_on_variables": others, "linear": self._is_linear(atom),
                })
            variables.append({
                "name": name, "type": spec.get("type"), "origin": spec.get("origin"),
                "role": "input" if self._is_input(name) else "context",
                "domain": self.domain_of(name, consts), "constraints": constraints,
            })
        conjuncts = [{"index": i, "condition": describe(n), "variables": node_vars(n)} for i, (n, _) in enumerate(parts)]
        notes = []
        if any(v["domain"]["range"] == "UNDETERMINED_IN_EFSM" for v in variables):
            notes.append("Numeric/string value ranges are not declared in the EFSM (UNDETERMINED_IN_EFSM); no business "
                         "range was assumed.")
        if any(not c["linear"] for v in variables for c in v["constraints"]):
            notes.append("Some constraints are non-linear; boundary values for them come from search candidates only.")
        return GuardAnalysis(True, text, None, variables, conjuncts, notes)

    def _check_supported_types(self, root: Any) -> None:
        for name in node_vars(root):
            if self._vars[name].get("type") == "list":
                raise UnsupportedGuard(f"variable {name!r} has type list, which the data generator cannot generate")

    @staticmethod
    def _is_linear(atom: Atom) -> bool:
        return atom.op in ("in", "not_in") or (linearize(atom.left, {}) is not None and linearize(atom.right, {}) is not None)

    @staticmethod
    def _guard_text(guard: Any) -> str:
        return "(no guard)" if guard is None else json.dumps(guard, ensure_ascii=False, sort_keys=True)

    # ------------------------------------------------------------------
    # Giải ràng buộc hữu hạn
    # ------------------------------------------------------------------
    def solve(
        self,
        guard: Any,
        *,
        fixed: Mapping[str, Any] | None = None,
        hints: Mapping[str, Any] | None = None,
        mutable: Sequence[str] | None = None,
        want_violation: bool = False,
        allow_off_domain: bool = False,
    ) -> SolveResult:
        """Tìm bộ giá trị thỏa guard (hoặc làm sai guard nếu ``want_violation``) bằng tìm kiếm hữu hạn.

        * ``fixed``: biến giữ nguyên giá trị (ngữ cảnh); ``mutable``: biến được tìm (mặc định mọi biến còn lại).
        * ``allow_off_domain``: cho phép ứng viên chuỗi NGOÀI miền khai báo (``OFF_DOMAIN_VALUE``) để vi phạm enum;
          khi bật, miền enum không còn được coi là đầy đủ nên không bao giờ kết luận UNSAT do duyệt hết miền.
        * ``UNSAT`` chỉ khi chứng minh được (khoảng số nguyên / duyệt hết miền hữu hạn đã khai báo) và luôn kèm
          ``assumptions`` về các biến cố định. Hết ngân sách -> ``UNKNOWN``.
        """
        try:
            root = self._builder.build(guard)
            self._check_supported_types(root)
        except UnsupportedGuard as exc:
            return SolveResult(SolveStatus.UNSUPPORTED, reason=str(exc))
        self._allow_off_domain = bool(allow_off_domain)
        try:
            return self._solve_node(Not(root) if want_violation else root, dict(fixed or {}), dict(hints or {}), mutable)
        finally:
            self._allow_off_domain = False

    def _solve_node(self, node: Any, fixed: dict[str, Any], hints: dict[str, Any], mutable: Sequence[str] | None) -> SolveResult:
        try:
            terms = to_dnf(node, cap=self.max_dnf_terms)
        except _DnfLimit:
            return SolveResult(SolveStatus.UNKNOWN, reason=f"DNF exceeds {self.max_dnf_terms} terms")
        names = node_vars(node)
        mutable_set = [v for v in names if v not in fixed] if mutable is None else [v for v in mutable if v in names]
        nodes, proven = 0, 0
        reasons: list[str] = []
        proofs: list[str] = []
        for term in terms:
            result = self._solve_term(term, fixed, mutable_set, hints)
            nodes += result.nodes
            if result.status == SolveStatus.SAT:
                return SolveResult(SolveStatus.SAT, result.assignment, "found", nodes)
            if result.status == SolveStatus.UNSAT:
                proven += 1
                proofs.append(result.reason)
            else:
                reasons.append(result.reason)
        assumptions = [f"{k} fixed to {v!r}" for k, v in fixed.items() if k in names]
        if proven == len(terms):
            why = "; ".join(dict.fromkeys(proofs))
            return SolveResult(SolveStatus.UNSAT, None, f"no solution exists in the defined domain ({why})", nodes, assumptions)
        return SolveResult(SolveStatus.UNKNOWN, None, "; ".join(dict.fromkeys(reasons)) or "not found", nodes, assumptions)

    def _solve_term(self, atoms: tuple[Atom, ...], fixed: dict, mutable: list[str], hints: dict) -> SolveResult:
        muts = [v for v in dict.fromkeys(n for a in atoms for n in term_vars(a.left) + term_vars(a.right)) if v in mutable]
        free_atoms = []
        for atom in atoms:
            atom_mut = [v for v in term_vars(atom.left) + term_vars(atom.right) if v in muts]
            if not atom_mut:
                try:
                    if not eval_atom(atom, fixed):
                        return SolveResult(SolveStatus.UNSAT, reason=f"{atom.text} is false for the fixed values")
                except _NotEvaluable:
                    return SolveResult(SolveStatus.UNKNOWN, reason=f"{atom.text} cannot be evaluated with the fixed values")
            else:
                free_atoms.append(atom)
        # chứng minh UNSAT bằng suy luận khoảng trên số nguyên (điều kiện cần, an toàn)
        for v in muts:
            if self._vars[v].get("type") != "integer":
                continue
            bounds = []
            for atom in free_atoms:
                involved = set(term_vars(atom.left) + term_vars(atom.right)) & set(muts)
                if involved == {v}:
                    bound = atom_bound(atom, v, {k: x for k, x in fixed.items() if k != v})
                    if bound is not None:
                        bounds.append(bound)
            if bounds and not int_interval(bounds)["feasible"]:
                return SolveResult(SolveStatus.UNSAT, reason=f"integer constraints on {v} are contradictory")
        # tách thành phần liên thông theo biến chung
        parent = {v: v for v in muts}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for atom in free_atoms:
            vs = [v for v in term_vars(atom.left) + term_vars(atom.right) if v in parent]
            for other in vs[1:]:
                parent[find(other)] = find(vs[0])
        comps: dict[str, list[str]] = {}
        for v in muts:
            comps.setdefault(find(v), []).append(v)
        assignment: dict[str, Any] = {}
        nodes = 0
        for comp in comps.values():
            comp_atoms = [a for a in free_atoms if set(term_vars(a.left) + term_vars(a.right)) & set(comp)]
            result = self._search(comp, comp_atoms, fixed, hints)
            nodes += result.nodes
            if result.status != SolveStatus.SAT:
                result.nodes = nodes
                return result
            assignment.update(result.assignment or {})
        return SolveResult(SolveStatus.SAT, assignment, "found", nodes)

    def _search(self, comp: list[str], atoms: list[Atom], fixed: dict, hints: dict) -> SolveResult:
        pools = {v: self._pool(v, atoms, fixed, hints) for v in comp}
        order = sorted(comp, key=lambda v: (len(pools[v]), comp.index(v)))
        nodes = 0
        exhausted = True

        def consistent(assign: dict) -> bool:
            known = {**fixed, **assign}
            for atom in atoms:
                if all(n in known for n in term_vars(atom.left) + term_vars(atom.right)):
                    try:
                        if not eval_atom(atom, known):
                            return False
                    except _NotEvaluable:
                        return False
            return True

        def dfs(i: int, assign: dict) -> dict | None:
            nonlocal nodes, exhausted
            if i == len(order):
                return dict(assign)
            for value in pools[order[i]]:
                nodes += 1
                if nodes > self.max_nodes:
                    exhausted = False
                    return None
                assign[order[i]] = value
                if consistent(assign):
                    found = dfs(i + 1, assign)
                    if found is not None:
                        return found
                    if not exhausted:
                        return None
                del assign[order[i]]
            return None

        found = dfs(0, {})
        if found is not None:
            return SolveResult(SolveStatus.SAT, found, "found", nodes)
        if not exhausted:
            return SolveResult(SolveStatus.UNKNOWN, reason=f"search budget ({self.max_nodes} nodes) exhausted", nodes=nodes)
        if not self._allow_off_domain and all(
                self.domain_of(v)["complete"] and self.domain_of(v)["kind"] in ("boolean", "enum") for v in comp):
            return SolveResult(SolveStatus.UNSAT, reason="exhausted the complete declared finite domain", nodes=nodes)
        return SolveResult(SolveStatus.UNKNOWN, reason="candidate pool exhausted; the domain is not fully defined", nodes=nodes)

    def _pool(self, v: str, atoms: Sequence[Atom], fixed: Mapping[str, Any], hints: Mapping[str, Any]) -> list[Any]:
        """Giá trị ứng viên của một biến, theo thứ tự ưu tiên (gợi ý ngữ cảnh trước)."""
        consts = [c for a in atoms for side in (a.left, a.right)
                  for c in (side[1] if side[0] == "list" else [side[1]] if side[0] == "const" else [])]
        dom = self.domain_of(v, consts)
        candidates: list[Any] = [hints[v]] if v in hints else []
        if dom["kind"] == "boolean":
            candidates += [True, False]
        elif dom["kind"] in ("enum", "string"):
            candidates += list(dom["values"] or []) + [c for c in consts if isinstance(c, str)]
            if self._allow_off_domain:
                candidates.append(OFF_DOMAIN_VALUE)
        else:
            bounds = []
            others = {k: x for k, x in fixed.items() if k != v}
            for atom in atoms:
                involved = set(term_vars(atom.left) + term_vars(atom.right))
                if involved <= ({v} | set(others)):
                    bound = atom_bound(atom, v, others)
                    if bound is not None:
                        bounds.append(bound)
            iv = int_interval(bounds)
            lo, hi = iv["lo"], iv["hi"]
            if lo is not None and hi is not None:
                candidates += [lo + (hi - lo) // 2, lo, hi, lo + 1, hi - 1]
            elif lo is not None:
                candidates += [lo, lo + 1]
            elif hi is not None:
                candidates += [hi, hi - 1]
            for op, k in bounds:
                if op in ("in", "not_in"):
                    candidates += [e for e in k if _is_num(e)]
                elif op != "const":
                    base = floor(k)
                    candidates += [base, base + 1, base - 1]
            candidates += [c for c in consts if _is_num(c)]
            for c in consts:  # ràng buộc nhân (a * b == c): thử các ước của hằng số
                if _is_num(c) and _frac(c).denominator == 1 and 0 < abs(int(c)) <= 100000:
                    candidates += [d for d in range(1, abs(int(c)) + 1) if int(c) % d == 0]
            for x in others.values():
                if _is_num(x):
                    candidates += [int(x), int(x) + 1, int(x) - 1]
            candidates += list(self.int_pool)
            candidates = [int(c) if isinstance(c, Fraction) and c.denominator == 1 else c for c in candidates]
            candidates = [c for c in candidates if not isinstance(c, Fraction)]
        pool = list(dict.fromkeys(candidates))
        return pool[:200]

    # ------------------------------------------------------------------
    # Sinh dữ liệu cho một guard
    # ------------------------------------------------------------------
    def generate_for_transition(self, transition_id: str, context: Mapping[str, Any] | None = None, **options: Any) -> GuardDataSet:
        """Sinh dữ liệu cho guard của một transition trong mô hình."""
        transition = self._transitions.get(transition_id)
        if transition is None:
            raise KeyError(f"Transition {transition_id!r} does not exist")
        dataset = self.generate_for_guard(
            transition.get("guard"), context, label=transition_id, transition_id=transition_id, **options)
        dataset.source_state = transition["source"]
        dataset.event = transition.get("event")
        dataset.rule_ids = list(transition.get("rule_refs", []))
        return dataset

    def generate_for_guard(
        self,
        guard: Any,
        context: Mapping[str, Any] | None = None,
        *,
        label: str = "guard",
        transition_id: str | None = None,
        include_negative: bool = True,
        include_malformed: bool = True,
    ) -> GuardDataSet:
        """Sinh mẫu EP/BVA hợp lệ + không hợp lệ cho ``guard`` rồi xác minh bằng GuardEvaluator.

        ``context``: giá trị ưu tiên cho biến ngữ cảnh (không phải biến client), ví dụ ``{"stock": 10}``.
        Biến ngữ cảnh được giữ cố định khi biến thể hóa; nếu ngữ cảnh làm guard vô nghiệm, bộ sinh thả lỏng nó và ghi
        lại biến nào đã đổi (``base.context_adjusted``).
        """
        context = copy.deepcopy(dict(context or {}))
        analysis = self.analyze_guard(guard)
        dataset = GuardDataSet(label, copy.deepcopy(guard), "OK", None, analysis, transition_id)
        if guard is None:
            dataset.status = "NO_GUARD"
            dataset.samples = [self._verify(dataset, DataSample("", {}, True, "NO_GUARD", ["NO_GUARD"]), [])]
            return dataset
        if not analysis.supported:
            dataset.status, dataset.reason = "UNSUPPORTED", analysis.reason
            return dataset

        parts = self._builder.split_conjuncts(guard)
        root = parts[0][0] if len(parts) == 1 else And(tuple(p[0] for p in parts))
        names = node_vars(root)
        base_info = self._find_base(root, names, context)
        dataset.base = base_info["report"]
        if base_info["assignment"] is None:
            dataset.status, dataset.reason = base_info["status"], base_info["reason"]
            return dataset
        base = base_info["assignment"]

        conj_vars = [node_vars(n) for n, _ in parts]
        mutable_per_conj = []
        for vs in conj_vars:
            inputs = [v for v in vs if self._is_input(v)]
            mutable_per_conj.append(inputs or vs)
        varied = list(dict.fromkeys(v for vs in mutable_per_conj for v in vs))
        dataset.base["varied_variables"] = varied

        raw: dict[str, DataSample] = {}

        def add(data: dict, technique: str, partition: dict, changed: list[str], target: str | None,
                expected_status: str | None = None, note: str | None = None) -> None:
            key = json.dumps(data, sort_keys=True, default=str)
            if key in raw:
                if technique not in raw[key].techniques:
                    raw[key].techniques.append(technique)
                return
            expected = None
            if expected_status is None:
                try:
                    expected = eval_node(root, data)
                except _NotEvaluable:
                    expected = None
            raw[key] = DataSample("", data, expected, technique, [technique], expected_status, partition, target,
                                  changed, notes=[note] if note else [])

        add(dict(base), "BASE", {"description": "nominal valid assignment found by search"}, [], None)
        if include_negative:
            for v in varied:
                for value, technique, partition in self._values_for(v, root, base):
                    data = {**base, v: value}
                    add(data, technique, {"variable": v, **partition}, [v] if value != base.get(v) else [], v)
        if include_malformed:
            for v in varied:
                if not self._is_input(v):
                    continue
                for kind, value, status in self._malformed_values(v):
                    data = dict(base)
                    if kind == "missing":
                        del data[v]
                    else:
                        data[v] = value
                    add(data, f"EP_{kind.upper()}", {"variable": v, "partition": kind}, [v], v, expected_status=status)

        samples = list(raw.values())
        # mỗi điều kiện cấp cao cần có ít nhất một mẫu CHỈ vi phạm điều kiện đó; nếu chưa có, tìm bằng ràng buộc
        if include_negative:
            for j, (conj_node, _) in enumerate(parts):
                hit = any(s.expected_guard is False and self._predicted_violations(parts, s.data) == [j] for s in samples)
                if hit:
                    continue
                others = [n for k, (n, _) in enumerate(parts) if k != j]
                target = And((Not(conj_node), *others)) if others else Not(conj_node)
                fixed = {k: x for k, x in base.items() if k not in mutable_per_conj[j]}
                result = self._solve_node(target, fixed, {k: base[k] for k in mutable_per_conj[j]}, mutable_per_conj[j])
                if result.status == SolveStatus.SAT:
                    data = {**fixed, **(result.assignment or {})}
                    before = len(raw)
                    add(data, "CONSTRAINT_SEARCH", {"description": f"violate only: {describe(conj_node)}"},
                        [k for k in data if data[k] != base.get(k)], None)
                    if len(raw) > before:
                        samples = list(raw.values())
                else:
                    dataset.conjunct_notes.append({
                        "condition": describe(conj_node), "single_violation_sample": result.status,
                        "reason": result.reason})
        order = {v: i for i, v in enumerate(varied)}

        def sort_key(sample: DataSample) -> tuple:
            value = sample.data.get(sample.target_variable) if sample.target_variable else None
            value_key = (0, value, "") if _is_num(value) else (1, 0, str(value))
            return (sample.technique != "BASE", sample.expected_status is not None,
                    order.get(sample.target_variable or "", -1), value_key,
                    json.dumps(sample.data, sort_keys=True, default=str))

        samples.sort(key=sort_key)
        dataset.samples = [self._verify(dataset, s, parts) for s in samples]
        for number, s in enumerate(dataset.samples, start=1):
            s.sample_id = f"GD{number:03d}"
        return dataset

    # --- tìm bộ dữ liệu nền hợp lệ ---
    def _find_base(self, root: Any, names: list[str], context: dict[str, Any]) -> dict[str, Any]:
        non_input_ctx = {k: v for k, v in context.items() if k in names and not self._is_input(k)}
        hints = {k: v for k, v in context.items() if k in names}
        attempts = [("context_fixed", non_input_ctx)] if non_input_ctx else []
        attempts.append(("context_as_hint", {}))
        last = None
        for mode, fixed in attempts:
            last = self._solve_node(root, dict(fixed), hints, None)
            if last.status == SolveStatus.SAT:
                assignment = {**fixed, **(last.assignment or {})}
                assignment = {k: assignment[k] for k in names}
                adjusted = [k for k in non_input_ctx if assignment.get(k) != non_input_ctx[k]]
                return {"assignment": assignment, "status": "OK", "reason": None, "report": {
                    "status": SolveStatus.SAT, "mode": mode, "assignment": assignment, "context_given": hints,
                    "context_adjusted": adjusted, "search_nodes": last.nodes}}
        assert last is not None
        status = {SolveStatus.UNSAT: "UNSAT", SolveStatus.UNSUPPORTED: "UNSUPPORTED"}.get(last.status, "UNKNOWN")
        return {"assignment": None, "status": status, "reason": last.reason, "report": last.to_dict()}

    # --- giá trị EP/BVA cho một biến ---
    def _values_for(self, v: str, root: Any, base: Mapping[str, Any]) -> list[tuple[Any, str, dict]]:
        atoms = [a for a in iter_atoms(root) if v in term_vars(a.left) + term_vars(a.right)]
        consts = [c for a in atoms for side in (a.left, a.right)
                  for c in (side[1] if side[0] == "list" else [side[1]] if side[0] == "const" else [])]
        dom = self.domain_of(v, consts)
        out: list[tuple[Any, str, dict]] = []
        if dom["kind"] == "boolean":
            return [(x, "EP", {"partition": f"value {x}"}) for x in dom["values"]]
        if dom["kind"] in ("enum", "string"):
            members = list(dict.fromkeys(list(dom["values"] or []) + [c for c in consts if isinstance(c, str)]))
            out = [(x, "EP", {"partition": "IN_DOMAIN" if dom["kind"] == "enum" else "CONSTANT_IN_GUARD"}) for x in members]
            out.append((OFF_DOMAIN_VALUE, "EP", {"partition": "OUT_OF_DECLARED_DOMAIN" if dom["kind"] == "enum" else "OTHER_VALUE"}))
            return out
        if dom["kind"] not in _NUMERIC_TYPES:
            return []
        others = {k: x for k, x in base.items() if k != v}
        cuts: set[int] = set()
        for atom in atoms:
            bound = atom_bound(atom, v, others)
            if bound is None or bound[0] == "const":
                continue
            op, k = bound
            if op in ("in", "not_in"):
                for e in k:
                    if _is_num(e) and _frac(e).denominator == 1:
                        cuts.update({int(e), int(e) + 1})
            elif op == ">=":
                cuts.add(ceil(k))
            elif op == ">":
                cuts.add(floor(k) + 1)
            elif op == "<=":
                cuts.add(floor(k) + 1)
            elif op == "<":
                cuts.add(ceil(k))
            elif k.denominator == 1:  # == hoặc !=
                cuts.update({int(k), int(k) + 1})
            else:
                cuts.add(ceil(k))
        ordered = sorted(cuts)
        segments: list[tuple[int | None, int | None]] = []
        previous: int | None = None
        for c in ordered:
            segments.append((previous, c - 1))
            previous = c
        segments.append((previous, None))
        for lo, hi in segments:
            label = f"{v} in [{'-inf' if lo is None else lo}, {'+inf' if hi is None else hi}]"
            bva: list[int] = []
            if lo is not None:
                bva += [lo] + ([lo + 1] if hi is None or lo + 1 <= hi else [])
            if hi is not None:
                bva += [hi] + ([hi - 1] if lo is None or hi - 1 >= lo else [])
            if lo is not None and hi is not None:
                rep = lo + (hi - lo) // 2
            elif lo is not None:
                rep = lo + 1
            elif hi is not None:
                rep = hi - 1
            else:
                rep = int(base[v]) if _is_num(base.get(v)) else 0
            for value in bva:
                out.append((value, "BVA", {"partition": label}))
            out.append((rep, "EP", {"partition": label}))
        out.sort(key=lambda t: t[0])
        return out

    def _malformed_values(self, v: str) -> list[tuple[str, Any, str]]:
        kind = self._vars[v].get("type")
        wrong = {"integer": "not_a_number", "decimal": "not_a_number", "string": 12345, "boolean": "not_a_boolean"}.get(kind)
        values = [("missing", None, "MISSING_VARIABLE"), ("null", None, "TYPE_ERROR")]
        if wrong is not None:
            values.append(("wrong_type", wrong, "TYPE_ERROR"))
        return values

    # --- kiểm tra ---
    def _predicted_violations(self, parts: Sequence[tuple[Any, dict]], data: Mapping[str, Any]) -> list[int] | None:
        out = []
        try:
            for j, (node, _) in enumerate(parts):
                if not eval_node(node, data):
                    out.append(j)
        except _NotEvaluable:
            return None
        return out

    def _verify(self, dataset: GuardDataSet, sample: DataSample, parts: Sequence[tuple[Any, dict]]) -> DataSample:
        """Đối chiếu kỳ vọng với GuardEvaluator; chỉ khớp mới được VERIFIED."""
        if dataset.transition_id:
            check = self.evaluator.check_transition(dataset.transition_id, self._transitions[dataset.transition_id]["source"], sample.data)
            status, result = check.status, check.guard_result
        else:
            ev = self.evaluator.evaluate_guard(dataset.guard, sample.data)
            status, result = ev.status, ev.result
        sample.evaluator_verdict = {"status": status.value, "guard_result": result}
        if status in (GuardStatus.UNSUPPORTED_EXPRESSION, GuardStatus.INVALID_GUARD):
            sample.verification_status = VerificationStatus.UNSUPPORTED
        elif sample.expected_status is not None:
            ok = status.value == sample.expected_status
            sample.verification_status = VerificationStatus.VERIFIED_INVALID if ok else VerificationStatus.UNVERIFIED
        elif sample.expected_guard is True:
            ok = status is GuardStatus.GUARD_SATISFIED
            sample.verification_status = VerificationStatus.VERIFIED_VALID if ok else VerificationStatus.UNVERIFIED
        elif sample.expected_guard is False:
            ok = status is GuardStatus.GUARD_NOT_SATISFIED
            sample.verification_status = VerificationStatus.VERIFIED_INVALID if ok else VerificationStatus.UNVERIFIED
        else:
            sample.verification_status = VerificationStatus.UNVERIFIED
            sample.notes.append("no independent expectation could be derived")
        if sample.expected_guard is False and parts:
            predicted = self._predicted_violations(parts, sample.data)
            verified: list[int] | None = []
            for j, (_, child) in enumerate(parts):
                verdict = self.evaluator.evaluate_guard(child, sample.data)
                if verdict.status is GuardStatus.GUARD_NOT_SATISFIED:
                    verified.append(j)
                elif verdict.status is not GuardStatus.GUARD_SATISFIED:
                    verified = None
                    break
            texts = [describe(n) for n, _ in parts]
            sample.violated_conditions = {
                "predicted": [texts[j] for j in predicted] if predicted is not None else None,
                "verified": [texts[j] for j in verified] if verified is not None else None,
                "single_condition": (len(verified) == 1) if verified is not None else None,
                "prediction_matches_evaluator": (predicted == verified) if predicted is not None and verified is not None else None,
            }
            if verified is not None and predicted is not None and predicted != verified:
                sample.verification_status = VerificationStatus.UNVERIFIED
        return sample

    # ------------------------------------------------------------------
    # Toàn bộ mô hình, phân tích invariant, xuất JSON
    # ------------------------------------------------------------------
    def analyze_invariants(self) -> list[dict[str, Any]]:
        """Biến runtime mà từng invariant cần, và mức hỗ trợ của bộ sinh (chỉ phân tích, không sinh dữ liệu)."""
        out = []
        for inv in self.model.get("invariants", []):
            entry: dict[str, Any] = {"invariant_id": inv["id"], "scope": inv.get("scope"), "kind": inv.get("kind")}
            if inv.get("kind") == "model_structure":
                entry.update(variables=[], generator_support="NOT_APPLICABLE", note="checks the model structure, not runtime data")
            elif "check" not in inv:
                entry.update(variables=[], generator_support="UNSUPPORTED", note=inv.get("unsupported_reason"))
            else:
                try:
                    needs = {**self.evaluator.get_guard_variables(inv.get("applies_when")), **self.evaluator.get_guard_variables(inv["check"])}
                    entry["variables"] = sorted(needs)
                    entry["runtime_origins"] = {n: self._origin(n) for n in sorted(needs)}
                    analysis = self.analyze_guard(inv["check"])
                    entry["generator_support"] = "SUPPORTED" if analysis.supported else "UNSUPPORTED"
                    entry["note"] = analysis.reason
                except GuardEvaluatorError as exc:
                    entry.update(variables=[], generator_support="UNSUPPORTED", note=str(exc))
            out.append(entry)
        return out

    def generate_all(
        self,
        context: Mapping[str, Any] | None = None,
        per_transition_context: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        transition_ids: Sequence[str] | None = None,
        include_negative: bool = True,
        include_malformed: bool = True,
    ) -> list[GuardDataSet]:
        """Sinh dữ liệu cho mọi (hoặc các) transition; sample_id được đánh số liên tục, ổn định."""
        datasets = []
        for tid in transition_ids or list(self._transitions):
            merged = {**(context or {}), **(per_transition_context or {}).get(tid, {})}
            datasets.append(self.generate_for_transition(tid, merged, include_negative=include_negative,
                                                         include_malformed=include_malformed))
        counter = 0
        for dataset in datasets:
            for sample in dataset.samples:
                counter += 1
                sample.sample_id = f"GD{counter:03d}"
        return datasets

    def summarize(self, datasets: Sequence[GuardDataSet]) -> dict[str, Any]:
        samples = [s for d in datasets for s in d.samples]
        count = lambda pred: sum(1 for s in samples if pred(s))  # noqa: E731
        guarded = [d for d in datasets if d.status != "NO_GUARD"]
        return {
            "transitions": len(datasets),
            "guards_analyzed": len(guarded),
            "guards_supported": sum(d.analysis.supported for d in guarded),
            "guards_unsupported": sum(not d.analysis.supported for d in guarded),
            "transitions_without_guard": len(datasets) - len(guarded),
            "datasets_by_status": {s: sum(d.status == s for d in datasets) for s in sorted({d.status for d in datasets})},
            "samples_total": len(samples),
            "valid_samples": count(lambda s: s.expected_guard is True),
            "invalid_samples": count(lambda s: s.expected_guard is False),
            "malformed_samples": count(lambda s: s.expected_status is not None),
            "verification": {k: count(lambda s, k=k: s.verification_status == k) for k in (
                VerificationStatus.VERIFIED_VALID, VerificationStatus.VERIFIED_INVALID,
                VerificationStatus.UNVERIFIED, VerificationStatus.UNSUPPORTED)},
            "verified_by_guard_evaluator": count(lambda s: s.verification_status in (
                VerificationStatus.VERIFIED_VALID, VerificationStatus.VERIFIED_INVALID)),
            "single_condition_violations_verified": count(lambda s: s.violated_conditions.get("single_condition") is True),
            "unsolved": [{"label": d.label, "status": d.status, "reason": d.reason} for d in datasets
                         if d.status in ("UNSAT", "UNKNOWN", "UNSUPPORTED")],
        }

    def to_output_dict(self, datasets: Sequence[GuardDataSet], configuration: Mapping[str, Any] | None = None) -> dict[str, Any]:
        used = sorted({r for d in datasets for r in d.rule_ids})
        return {
            "generator": GENERATOR_NAME,
            "schema_version": SCHEMA_VERSION,
            "scope": "guard-level data for one transition at a time; not path-level",
            "model": {"name": self.model.get("model_name"), "schema_version": self.model.get("schema_version"),
                      "sha256": self.model_sha256},
            "configuration": {"max_nodes": self.max_nodes, "max_dnf_terms": self.max_dnf_terms,
                              "int_pool": list(self.int_pool), **(configuration or {})},
            "summary": self.summarize(datasets),
            "rules_index": {r: {k: self.rules[r].get(k) for k in ("name", "category", "severity")} for r in used if r in self.rules},
            "invariant_analysis": self.analyze_invariants(),
            "datasets": [d.to_dict() for d in datasets],
        }

    @staticmethod
    def export_json(data: Mapping[str, Any], output_path: str | Path) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        return path


# ===========================================================================
# PHẦN 2: SINH DỮ LIỆU THEO TEST PATH (path-aware)
# ===========================================================================
# Đọc test_paths.json (do TestPathGenerator sinh, KHÔNG sửa) và biến mỗi path thành một test case có dữ liệu cụ thể.
# Không gửi HTTP, không chạm database. "Trạng thái dữ liệu" được mô phỏng bằng ShadowState; mỗi bước đều được
# đối chiếu với GuardEvaluator (guard + action ``assign`` + invariant) để phát hiện mô phỏng sai.
TEST_CASE_SCHEMA_VERSION = "1.0"
PATH_GENERATOR_NAME = "PathAwareTestDataGenerator"
_KNOWN_OPS = {"cart_add", "cart_set", "cart_remove", "cart_clear", "order_create", "order_status", "order_cancel",
              "payment_record"}
_SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, None: 4}
_RUNTIME_UNVERIFIED = "NOT_VERIFIED_UNTIL_EXECUTION"


class Readiness:
    READY = "READY"
    PARTIALLY_READY = "PARTIALLY_READY"
    BLOCKED = "BLOCKED"
    UNSUPPORTED = "UNSUPPORTED"


class _Unresolved(Exception):
    """Mô phỏng không thể xác định/đối chiếu được ngữ cảnh (UNRESOLVED_CONTEXT)."""


class _Budget(Exception):
    """Hết ngân sách tìm kiếm của một path."""


class ApiMapping:
    """Nạp ``api_test_mapping.json``: cách gọi API cho từng transition (chỉ mô tả, không gọi)."""

    def __init__(self, data: Mapping[str, Any], source: str = "<memory>") -> None:
        for key in ("catalog", "transitions", "input_hints"):
            if key not in data:
                raise ModelFormatError(f"api mapping thiếu khóa '{key}'")
        self.data = copy.deepcopy(dict(data))
        self.source = source
        self.products = {int(p["id"]): dict(p) for p in self.data["catalog"]["products"]}
        self.hints: dict[str, Any] = dict(self.data["input_hints"])

    @classmethod
    def load(cls, path: str | Path) -> "ApiMapping":
        try:
            raw = Path(path).read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ModelFormatError(f"cannot read api mapping: {exc}") from exc
        mapping = cls(data, str(path))
        mapping.sha256 = hashlib.sha256(raw).hexdigest()
        return mapping

    sha256: str | None = None

    def transition(self, tid: str) -> dict[str, Any] | None:
        return self.data["transitions"].get(tid)

    @property
    def primary_product(self) -> int:
        return int(self.data["catalog"].get("primary_product", min(self.products)))

    def product_order(self) -> list[int]:
        return [self.primary_product] + [p for p in sorted(self.products) if p != self.primary_product]

    def validate(self, model: Mapping[str, Any]) -> list[str]:
        """Đối chiếu mapping với EFSM; trả về danh sách vấn đề (rỗng nếu khớp)."""
        issues: list[str] = []
        names = {v["name"] for v in model["variables"]}
        captured = {c for spec in self.data["transitions"].values() for c in (spec.get("capture") or {})}
        for t in model["transitions"]:
            spec = self.transition(t["id"])
            if spec is None:
                issues.append(f"{t['id']}: không có trong api_test_mapping")
                continue
            if spec.get("implemented_by") != "API":
                continue
            api = t.get("api") or {}
            if set(re.findall(r"\{(\w+)\}", api.get("path", ""))) != set(spec["request"]["path_params"]):
                issues.append(f"{t['id']}: path params của mapping không khớp EFSM api.path")
            if set(api.get("request_body") or {}) != set(spec["request"]["body"]):
                issues.append(f"{t['id']}: body của mapping không khớp EFSM api.request_body")
            for op in spec.get("ops", []):
                if op.split(":")[0] not in _KNOWN_OPS:
                    issues.append(f"{t['id']}: op không hỗ trợ '{op}'")
            for where, group in (("path_params", spec["request"]["path_params"]), ("body", spec["request"]["body"])):
                for field_name, src in group.items():
                    if src.get("source") in ("input", "string_of_length") and src.get("var") not in names:
                        issues.append(f"{t['id']}.{where}.{field_name}: biến '{src.get('var')}' không có trong EFSM")
                    if src.get("source") == "ref" and src.get("capture") not in captured:
                        issues.append(f"{t['id']}.{where}.{field_name}: capture '{src.get('capture')}' không được transition nào tạo ra")
        for tid in self.data.get("setup_transitions", []):
            t = next((x for x in model["transitions"] if x["id"] == tid), None)
            if t is None or t["source"] != model["initial_state"] or t["target"] != model["initial_state"]:
                issues.append(f"setup transition {tid} phải là self-loop tại trạng thái đầu")
        return issues


class ShadowState:
    """Mô phỏng dữ liệu nghiệp vụ (giỏ, tồn kho, đơn, thanh toán) theo các quy tắc cập nhật có cấu trúc."""

    def __init__(self, products: Mapping[int, Mapping[str, Any]]) -> None:
        self.products = {int(k): dict(v) for k, v in products.items()}
        self.cart: dict[int, int] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.payments: list[dict[str, Any]] = []
        self.active: str | None = None

    def clone(self) -> "ShadowState":
        return copy.deepcopy(self)

    # --- quy tắc cập nhật -------------------------------------------------
    def cart_add(self, pid: int, qty: int) -> None:
        self.cart[pid] = self.cart.get(pid, 0) + qty

    def cart_set(self, pid: int, qty: int) -> None:
        self.cart[pid] = qty

    def cart_remove(self, pid: int) -> None:
        self.cart.pop(pid, None)

    def cart_clear(self) -> None:
        self.cart.clear()

    def order_create(self, ref: str) -> None:
        items, total = [], 0
        for pid in sorted(self.cart):
            product, qty = self.products[pid], self.cart[pid]
            items.append({"product_id": pid, "quantity": qty, "unit_price": product["price"], "stock_before": product["stock"]})
            product["stock"] -= qty
            total += qty * product["price"]
        self.orders[ref] = {"status": "PENDING_CONFIRMATION", "items": items, "total": total, "cancel_count": 0,
                            "restoration_count": 0, "payment_count": 0}
        self.cart.clear()
        self.active = ref

    def order_status(self, status: str) -> None:
        self.orders[self.active]["status"] = status  # type: ignore[index]

    def order_cancel(self) -> None:
        order = self.orders[self.active]  # type: ignore[index]
        for item in order["items"]:
            self.products[item["product_id"]]["stock"] += item["quantity"]
        order["status"] = "CANCELLED"
        order["cancel_count"] += 1
        order["restoration_count"] += 1

    def payment_record(self, result: str) -> None:
        order = self.orders[self.active]  # type: ignore[index]
        self.payments.append({"order_ref": self.active, "amount": order["total"], "result": result})
        if result == "SUCCESS":
            order["payment_count"] += 1

    def set_stock(self, pid: int, value: int) -> None:
        self.products[pid]["stock"] = value

    # --- chiếu sang biến EFSM ---------------------------------------------
    def project(self, pid: int | None, view: str) -> dict[str, Any]:
        """Giá trị các biến EFSM suy ra từ dữ liệu mô phỏng (``view``: ``cart`` hoặc ``order``)."""
        v: dict[str, Any] = {"cart_items_count": len(self.cart), "cart_item_exists": False, "current_qty": 0}
        if pid is not None:
            if pid in self.products:
                product = self.products[pid]
                v.update(product_exists=True, stock=product["stock"], unit_price=product["price"],
                         current_qty=self.cart.get(pid, 0), cart_item_exists=pid in self.cart)
            else:
                v["product_exists"] = False
        if view == "cart" and self.cart:
            lines = [(p, self.cart[p]) for p in sorted(self.cart)]
            v["order_items"] = [{"quantity": q, "unit_price": self.products[p]["price"]} for p, q in lines]
            first = pid if pid in self.cart else lines[0][0]
            v["ordered_quantity"] = self.cart[first]
        if view == "order" and self.active is not None:
            order = self.orders[self.active]
            first = order["items"][0]
            v.update(order_status=order["status"], order_total=order["total"], cancel_count=order["cancel_count"],
                     payment_count=order["payment_count"], stock_restoration_count=order["restoration_count"],
                     order_items=[{"quantity": i["quantity"], "unit_price": i["unit_price"]} for i in order["items"]],
                     ordered_quantity=first["quantity"], stock_before=first["stock_before"],
                     stock_after=self.products[first["product_id"]]["stock"])
            if pid is None or pid == first["product_id"]:
                v.update(stock=self.products[first["product_id"]]["stock"])
            mine = [p for p in self.payments if p["order_ref"] == self.active]
            if mine:
                v["payment_amount"] = mine[-1]["amount"]
        return v

    def summary(self) -> dict[str, Any]:
        return {
            "cart_items": [{"product_id": p, "quantity": self.cart[p]} for p in sorted(self.cart)],
            "product_stock": {str(p): self.products[p]["stock"] for p in sorted(self.products)},
            "orders": [{"order_ref": ref, "status": o["status"], "total_amount": o["total"],
                        "items": [{k: i[k] for k in ("product_id", "quantity", "unit_price")} for i in o["items"]]}
                       for ref, o in self.orders.items()],
            "payments": [dict(p) for p in self.payments],
        }


@dataclass
class PlanState:
    shadow: ShadowState
    state: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    fixtures: int = 0

    def clone(self) -> "PlanState":
        return PlanState(self.shadow.clone(), self.state, list(self.steps), dict(self.refs), self.fixtures)


class PathDataGenerator:
    """Biến từng path của ``test_paths.json`` thành test case có dữ liệu cụ thể (chưa thực thi)."""

    __test__ = False

    def __init__(self, generator: TestDataGenerator, mapping: ApiMapping, *, max_setup_steps: int = 3,
                 max_nodes: int = 3000) -> None:
        self.g, self.map = generator, mapping
        self.ev = generator.evaluator
        self.model = generator.model
        self.tr = {t["id"]: t for t in self.model["transitions"]}
        self.neg = {n["id"]: n for n in self.model.get("negative_transitions", [])}
        self.vars = generator._vars
        self.states = {s["id"]: s for s in self.model["states"]}
        self.max_setup_steps, self.max_nodes = max_setup_steps, max_nodes
        self.mapping_issues = mapping.validate(self.model)
        self._failures: list[dict[str, Any]] = []
        self._nodes = 0

    # ------------------------------------------------------------------
    # Tiện ích
    # ------------------------------------------------------------------
    def _initial(self) -> PlanState:
        return PlanState(ShadowState(self.map.products), self.model["initial_state"])

    def _view(self, state: str) -> str:
        return "cart" if self.states[state].get("lifecycle") == "cart" else "order"

    def _fail(self, plan: PlanState, tid: str, code: str, detail: str, pid: int | None = None) -> None:
        self._failures.append({"depth": sum(1 for s in plan.steps if s["role"] == "path"), "transition": tid, "product_id": pid, "code": code, "detail": detail})

    def _vars_in(self, text: str) -> set[str]:
        return {w for w in re.findall(r"[A-Za-z_]\w*", text) if w in self.vars}

    def _request_vars(self, tid: str) -> list[str]:
        spec = self.map.transition(tid) or {"request": {"body": {}, "path_params": {}}}
        found: list[str] = []
        for group in (spec["request"]["path_params"], spec["request"]["body"]):
            for src in group.values():
                if src.get("source") in ("input", "string_of_length") and src["var"] not in found:
                    found.append(src["var"])
        return found

    def _ref_pid(self, plan: PlanState) -> int | None:
        if self._view(plan.state) == "cart":
            return min(plan.shadow.cart) if plan.shadow.cart else None
        active = plan.shadow.active
        return plan.shadow.orders[active]["items"][0]["product_id"] if active else None

    def _products_for(self, plan: PlanState, mode: str) -> list[int]:
        if mode == "catalog":
            return self.map.product_order()
        if mode == "cart":
            return sorted(plan.shadow.cart)
        return [self._ref_pid(plan)]  # type: ignore[list-item]

    def _setup_variants(self) -> list[tuple[int, ...]]:
        order = self.map.product_order()
        if not self.map.data.get("setup_transitions"):
            return [()]
        return [tuple(order[:k]) for k in range(0, min(self.max_setup_steps, len(order)) + 1)]

    def _valid_inputs(self, tid: str, ctx: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """Giá trị biến request thỏa guard của ``tid`` với ngữ cảnh ``ctx`` (giải bằng solver Phần 1)."""
        names = self._request_vars(tid)
        hints = {n: self.map.hints[n] for n in names if n in self.map.hints}
        guard = self.tr[tid].get("guard")
        if guard is None or not names:
            return dict(hints), "hint"
        gvars = set(self.ev.get_guard_variables(guard))
        fixed = {k: v for k, v in ctx.items() if k in gvars and k not in names}
        result = self.g.solve(guard, fixed=fixed, hints=hints, mutable=[n for n in names if n in gvars])
        if result.status != SolveStatus.SAT:
            return None, f"{result.status}: {result.reason}"
        assignment = result.assignment or {}
        return {n: assignment.get(n, hints.get(n)) for n in names if n in assignment or n in hints}, "solver"

    # ------------------------------------------------------------------
    # Một bước positive
    # ------------------------------------------------------------------
    def _candidates(self, plan: PlanState, tid: str, role: str, pids: Sequence[int | None] | None = None) -> Iterator[PlanState]:
        tr, spec = self.tr[tid], self.map.transition(tid)
        if spec is None or spec.get("implemented_by") != "API":
            self._fail(plan, tid, "UNSUPPORTED_TRANSITION", "transition không có cách gọi API trong mapping")
            return
        if plan.state != tr["source"]:
            self._fail(plan, tid, "STATE_MISMATCH", f"đang ở {plan.state}, cần {tr['source']}")
            return
        view = self._view(plan.state)
        for pid in (list(pids) if pids is not None else self._products_for(plan, spec["select_product"])):
            if spec["select_product"] == "cart" and pid is None:
                self._fail(plan, tid, "GUARD_NOT_SATISFIED", "giỏ hàng không có dòng nào để thao tác", pid)
                continue
            ctx = plan.shadow.project(pid, view)
            inputs, how = self._valid_inputs(tid, ctx)
            if inputs is None:
                code = "UNSUPPORTED_GUARD" if how.startswith(SolveStatus.UNSUPPORTED) else "GUARD_NOT_SATISFIED"
                self._fail(plan, tid, code, how, pid)
                continue
            variables = {**ctx, **inputs}
            check = self.ev.check_transition(tid, plan.state, variables)
            if check.status is not GuardStatus.GUARD_SATISFIED:
                conds = [c.get("condition") for c in check.failed_conditions]
                self._fail(plan, tid, check.status.value, "; ".join(map(str, conds)) or check.reason, pid)
                continue
            if spec.get("each_line") and not self._lines_ok(plan, tid, variables):
                self._fail(plan, tid, "GUARD_NOT_SATISFIED", "một dòng giỏ hàng vi phạm guard", pid)
                continue
            try:
                yield self._advance(plan, tid, role, spec, pid, inputs, variables)
            except _Unresolved as exc:
                self._fail(plan, tid, "UNRESOLVED_CONTEXT", str(exc), pid)

    def _lines_ok(self, plan: PlanState, tid: str, variables: Mapping[str, Any]) -> bool:
        for pid, qty in sorted(plan.shadow.cart.items()):
            line = {**variables, "ordered_quantity": qty, "stock": plan.shadow.products[pid]["stock"]}
            if self.ev.check_transition(tid, plan.state, line).status is not GuardStatus.GUARD_SATISFIED:
                return False
        return True

    def _advance(self, plan: PlanState, tid: str, role: str, spec: Mapping[str, Any], pid: int | None,
                 inputs: Mapping[str, Any], variables: Mapping[str, Any]) -> PlanState:
        tr = self.tr[tid]
        out = self.ev.apply_transition(tid, plan.state, variables, verify_invariants=True)
        if not out.applied:
            raise _Unresolved(f"GuardEvaluator không áp dụng được action: {out.error}")
        step_no = len(plan.steps) + 1
        nxt = plan.clone()
        nxt.state = out.new_state
        order_ref = f"$ref:steps.{step_no}.{spec['capture']['order_id']}" if "order_id" in spec.get("capture", {}) else None
        self._apply_ops(nxt.shadow, spec, pid, inputs, order_ref)
        post_pid = self._ref_pid(nxt) if spec["select_product"] == "none" else pid
        shadow_vars = nxt.shadow.project(post_pid, self._view(nxt.state))
        assigned = [a["var"] for a in tr.get("actions", []) if isinstance(a, dict) and a.get("type") == "assign"]
        mismatches = []
        for name in assigned:
            if name not in shadow_vars:
                raise _Unresolved(f"biến '{name}' do action gán không có trong dữ liệu mô phỏng")
            if shadow_vars[name] != out.new_variables.get(name):
                mismatches.append(f"{name}: EFSM={out.new_variables.get(name)!r} mô phỏng={shadow_vars[name]!r}")
        if mismatches:
            raise _Unresolved("mô phỏng dữ liệu không khớp action của EFSM: " + "; ".join(mismatches))
        for cap, expr in spec.get("capture", {}).items():
            nxt.refs[cap] = {"step": step_no, "path": expr}
        nxt.steps.append(self._step_record(plan, nxt, tid, role, spec, pid, inputs, variables, out, step_no))
        return nxt

    def _apply_ops(self, sh: ShadowState, spec: Mapping[str, Any], pid: int | None, inputs: Mapping[str, Any],
                   order_ref: str | None) -> None:
        for op in spec["ops"]:
            name, _, arg = op.partition(":")
            if name in ("cart_add", "cart_set"):
                if pid is None or "quantity" not in inputs:
                    raise _Unresolved(f"{name} cần product_id và quantity")
                getattr(sh, name)(pid, inputs["quantity"])
            elif name == "cart_remove":
                sh.cart_remove(pid)  # type: ignore[arg-type]
            elif name == "cart_clear":
                sh.cart_clear()
            elif name == "order_create":
                if order_ref is None:
                    raise _Unresolved("order_create cần capture order_id")
                sh.order_create(order_ref)
            elif name == "order_status":
                sh.order_status(arg)
            elif name == "order_cancel":
                sh.order_cancel()
            elif name == "payment_record":
                if "payment_result" not in inputs:
                    raise _Unresolved("payment_record cần payment_result")
                sh.payment_record(inputs["payment_result"])

    # ------------------------------------------------------------------
    # Dựng bản ghi bước
    # ------------------------------------------------------------------
    def _request(self, plan: PlanState, tid: str, spec: Mapping[str, Any], pid: int | None, inputs: Mapping[str, Any]) -> tuple[dict, list]:
        api, params, body, refs = self.tr[tid]["api"], {}, {}, []
        for name, src in spec["request"]["path_params"].items():
            if src["source"] == "product_id":
                params[name] = pid
            elif src["source"] == "ref":
                ref = plan.refs.get(src["capture"])
                if ref is None:
                    raise _Unresolved(f"chưa có giá trị capture '{src['capture']}' cho path param '{name}'")
                params[name] = f"$ref:steps.{ref['step']}.{ref['path']}"
                refs.append({"name": name, "used_in": f"path_params.{name}", "ref": params[name],
                             "captured_from_step": ref["step"]})
        literal = self.map.data.get("off_domain_literal", "INVALID_VALUE")
        for name, src in spec["request"]["body"].items():
            kind = src["source"]
            if kind == "product_id":
                body[name] = pid
            elif kind == "input":
                value = inputs[src["var"]]
                body[name] = literal if value == OFF_DOMAIN_VALUE else value
            elif kind == "string_of_length":
                length = inputs[src["var"]]
                body[name] = src.get("char", "x") * max(0, int(length))
            else:
                raise _Unresolved(f"nguồn dữ liệu body không hỗ trợ: {kind}")
        return {"method": api["method"], "path": api["path"], "path_params": params, "body": body}, refs

    def _response_checks(self, spec: Mapping[str, Any], tid: str, plan_after: PlanState, inputs: Mapping[str, Any],
                         status: int) -> list[dict[str, Any]]:
        sim = "shadow_state_simulation"
        checks: list[dict[str, Any]] = [{"target": "response.status_code", "op": "==", "value": status,
                                         "source": f"efsm_model.transitions[{tid}].expected_http_status"}]
        sh, ops = plan_after.shadow, [o.split(":")[0] for o in spec["ops"]]
        order = sh.orders.get(sh.active) if sh.active else None
        if "order_create" in ops and order:
            checks += [{"target": "response.order.status", "op": "==", "value": order["status"], "source": sim},
                       {"target": "response.order.total_amount", "op": "==", "value": order["total"], "source": sim},
                       {"target": "response.order.items", "op": "==", "source": sim,
                        "value": [{k: i[k] for k in ("product_id", "quantity", "unit_price")} for i in order["items"]]}]
        elif order and any(o in ops for o in ("order_status", "order_cancel", "payment_record")):
            checks.append({"target": "response.order.status", "op": "==", "value": order["status"], "source": sim})
        if "payment_record" in ops and sh.payments:
            pay = sh.payments[-1]
            checks += [{"target": "response.payment.result", "op": "==", "value": pay["result"], "source": sim},
                       {"target": "response.payment.amount", "op": "==", "value": pay["amount"], "source": sim},
                       {"target": "response.payment.order_id", "op": "==", "value": pay["order_ref"], "source": "symbolic_ref"}]
        return checks

    def _invariants(self, vars_after: Mapping[str, Any], state: str) -> dict[str, Any]:
        report = self.ev.check_invariants(vars_after, current_state=state)
        simulated = {r.invariant_id: r.status.value for r in report.results}
        return {"simulated_status": simulated, "simulated_violations": sorted(k for k, s in simulated.items() if s == "VIOLATED"),
                "runtime_status": _RUNTIME_UNVERIFIED,
                "note": "SATISFIED chỉ có nghĩa là đúng trên dữ liệu mô phỏng của EFSM (hoặc, với invariant kind=model_structure, đúng trên cấu trúc mô hình); chưa được kiểm chứng trên backend."}

    def _step_record(self, before: PlanState, after: PlanState, tid: str, role: str, spec: Mapping[str, Any],
                     pid: int | None, inputs: Mapping[str, Any], variables: Mapping[str, Any], out: Any, step_no: int) -> dict[str, Any]:
        tr = self.tr[tid]
        request, refs = self._request(before, tid, spec, pid, inputs)
        required = list(tr.get("variables", {}).get("required_inputs", []))
        context_before = {k: variables[k] for k in required if k in variables}
        context_after = {k: out.new_variables[k] for k in tr.get("variables", {}).get("action_writes", []) if k in out.new_variables}
        input_names = sorted(set(self._request_vars(tid)))
        record = {
            "step_no": step_no, "role": role, "kind": "transition", "transition_id": tid, "event": tr["event"],
            "state_before": before.state, "state_after": after.state,
            "request": request, "request_data": dict(inputs), "symbolic_refs": refs,
            "data_classification": {
                "input": sorted(set(input_names) | ({"product_id"} if pid is not None and spec["select_product"] != "none" else set())),
                "derived": sorted(k for k in context_before if k not in input_names),
                "backend_generated": [{"name": n, "ref": f"$ref:steps.{step_no}.{e}"} for n, e in spec.get("capture", {}).items()],
            },
            "context_before": context_before, "context_after": context_after,
            "guard_check": {"verified_by": "GuardEvaluator.check_transition", "status": "GUARD_SATISFIED"},
            "expected": {"outcome": "ACCEPT", "http_status": tr["expected_http_status"],
                         "http_status_source": f"efsm_model.transitions[{tid}].expected_http_status",
                         "state_after": after.state,
                         "response_checks": self._response_checks(spec, tid, after, inputs, tr["expected_http_status"])},
            "captures": {n: f"$ref:steps.{step_no}.{e}" for n, e in spec.get("capture", {}).items()},
            "unsimulated_backend_effects": list(out.backend_effects),
            "invariant_checkpoint": self._invariants(out.new_variables, out.new_state),
            "data_after": after.shadow.summary(),
            "rule_ids": list(tr.get("rule_refs", [])),
        }
        return record

    # ------------------------------------------------------------------
    # Tìm kiếm kế hoạch
    # ------------------------------------------------------------------
    def _dfs(self, plan: PlanState, tids: Sequence[str], final: Callable[[PlanState], bool]) -> bool:
        if not tids:
            return final(plan)
        for nxt in self._candidates(plan, tids[0], "path"):
            self._nodes += 1
            if self._nodes > self.max_nodes:
                raise _Budget()
            if self._dfs(nxt, tids[1:], final):
                return True
        return False

    def _with_setup(self, tids: Sequence[str], final: Callable[[PlanState, tuple[int, ...]], bool]) -> str:
        """Thử lần lượt các biến thể setup; trả về ``FOUND`` / ``EXHAUSTED`` / ``BUDGET``."""
        status = "EXHAUSTED"
        for variant in self._setup_variants():
            plan = self._initial()
            setup_tid = self.map.data["setup_transitions"][0] if variant else None
            ok = True
            for pid in variant:
                nxt = next(self._candidates(plan, setup_tid, "setup", pids=[pid]), None)  # type: ignore[arg-type]
                if nxt is None:
                    ok = False
                    break
                plan = nxt
            if not ok:
                continue
            try:
                if self._dfs(plan, tids, lambda p, v=variant: final(p, v)):
                    return "FOUND"
            except _Budget:
                status = "BUDGET"
                break
        return status

    def plan_positive(self, tids: Sequence[str]) -> tuple[PlanState | None, dict[str, Any]]:
        self._failures, self._nodes = [], 0
        found: list[PlanState] = []

        def final(plan: PlanState, variant: tuple[int, ...]) -> bool:
            found.append(plan)
            return True

        status = self._with_setup(tids, final)
        return (found[0] if found else None), {"search": status, "nodes": self._nodes}

    # ------------------------------------------------------------------
    # Negative: tiền tố positive + một bước vi phạm có chủ đích
    # ------------------------------------------------------------------
    _ISO_RANK = {"ISOLATED": 0, "UNEVALUATED": 1, "NOT_ISOLATED": 2}

    def _isolation(self, neg: Mapping[str, Any], tid: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        guard = self.tr[tid].get("guard")
        if guard is None:
            return {"status": "UNEVALUATED", "reason": "transition không có guard để đối chiếu", "failed_conditions": []}
        evaluation = self.ev.evaluate_guard(guard, variables)
        if evaluation.status is GuardStatus.GUARD_NOT_SATISFIED:
            violated = self._vars_in(neg["invalid_condition"])
            failed = [str(c.get("condition")) for c in evaluation.failed_conditions]
            extra = [c for c in failed if not (self._vars_in(c) & violated)]
            return {"status": "NOT_ISOLATED" if extra else "ISOLATED", "failed_conditions": failed,
                    "extra_failed_conditions": extra,
                    "reason": ("guard còn bị vi phạm ở điều kiện khác" if extra else "chỉ các điều kiện liên quan đến invalid_condition bị vi phạm")}
        if evaluation.status is GuardStatus.GUARD_SATISFIED:
            return {"status": "NOT_ISOLATED", "failed_conditions": [], "extra_failed_conditions": [],
                    "reason": "guard của base transition vẫn thỏa: invalid_condition không được guard biểu diễn"}
        return {"status": "UNEVALUATED", "failed_conditions": [], "extra_failed_conditions": [],
                "reason": f"không đánh giá được guard ({evaluation.status.value}: {evaluation.reason})"}

    def _negative_candidate(self, plan: PlanState, neg: Mapping[str, Any], pid: int | None, view: str) -> dict[str, Any] | None:
        base, hints = neg["base_transition"], self.map.hints
        ctx = plan.shadow.project(pid, view)
        valid, _ = self._valid_inputs(base, ctx)
        inputs = dict(valid) if valid is not None else {n: hints[n] for n in self._request_vars(base) if n in hints}
        status = self.ev.check_negative_transition(neg["id"], plan.state, {**ctx, **inputs}).status
        technique, changed = "STATE_OR_CONTEXT_VIOLATION", []
        if status is not GuardStatus.NEGATIVE_APPLICABLE:
            cond_vars = [v for v in self._request_vars(base) if v in self._vars_in(neg["invalid_condition"])]
            if not cond_vars:
                return None
            fixed = {k: v for k, v in {**ctx, **inputs}.items() if k not in cond_vars}
            result = self.g.solve({"expr": neg["invalid_condition"]}, fixed=fixed, hints=inputs, mutable=cond_vars,
                                  allow_off_domain=True)
            if result.status != SolveStatus.SAT or not result.assignment:
                return None
            literal = self.map.data.get("off_domain_literal", "INVALID_VALUE")
            new = dict(inputs)
            new.update({k: (literal if result.assignment[k] == OFF_DOMAIN_VALUE else result.assignment[k])
                        for k in cond_vars if k in result.assignment})
            status = self.ev.check_negative_transition(neg["id"], plan.state, {**ctx, **new}).status
            if status is not GuardStatus.NEGATIVE_APPLICABLE:
                return None
            changed = [k for k in cond_vars if new.get(k) != inputs.get(k)]
            inputs, technique = new, "EP_BVA_INPUT_VIOLATION"
        variables = {**ctx, **inputs}
        isolation = self._isolation(neg, base, variables)
        return {"pid": pid, "inputs": inputs, "variables": variables, "technique": technique, "changed": changed,
                "isolation": isolation, "rank": self._ISO_RANK[isolation["status"]]}

    def _negative_step(self, plan: PlanState, neg: Mapping[str, Any], fixture: dict[str, Any] | None = None) -> PlanState | None:
        base = neg["base_transition"]
        spec = self.map.transition(base)
        if spec is None or spec.get("implemented_by") != "API":
            self._fail(plan, base, "UNSUPPORTED_TRANSITION", "base transition không có cách gọi API")
            return None
        if plan.state != neg["source"]:
            self._fail(plan, base, "STATE_MISMATCH", f"đang ở {plan.state}, negative cần {neg['source']}")
            return None
        view, mode = self._view(plan.state), spec["select_product"]
        nonexistent = self.map.data["catalog"].get("nonexistent_product_id")
        if mode == "catalog":
            pids: list[int | None] = list(self.map.product_order()) + [nonexistent]
        elif mode == "cart":
            pids = sorted(plan.shadow.cart) + [p for p in self.map.product_order() if p not in plan.shadow.cart] + [nonexistent]
        else:
            pids = [self._ref_pid(plan)]
        best = None
        for pid in pids:
            cand = self._negative_candidate(plan, neg, pid, view)
            if cand is None:
                self._fail(plan, neg["id"], "NEGATIVE_NOT_APPLICABLE", f"không tạo được dữ liệu vi phạm '{neg['invalid_condition']}'", pid)
                continue
            if best is None or cand["rank"] < best["rank"]:
                best = cand
            if cand["rank"] == 0:
                break
        if best is None:
            return None
        return self._append_negative(plan, neg, spec, best, fixture)

    def _append_negative(self, plan: PlanState, neg: Mapping[str, Any], spec: Mapping[str, Any], cand: Mapping[str, Any],
                         fixture: dict[str, Any] | None) -> PlanState:
        nxt = plan.clone()
        step_no = len(plan.steps) + 1
        base, pid, inputs = neg["base_transition"], cand["pid"], cand["inputs"]
        request, refs = self._request(plan, base, spec, pid, inputs)
        expected = neg.get("expected", {})
        before = plan.shadow.summary()
        relevant = (set(self.tr[base].get("variables", {}).get("required_inputs", [])) | self._vars_in(neg["invalid_condition"])
                    | set(inputs))
        context = {k: v for k, v in cand["variables"].items() if k in relevant}
        record = {
            "step_no": step_no, "role": "negative", "kind": "negative_transition", "negative_transition_id": neg["id"],
            "base_transition_id": base, "event": neg["event"], "state_before": plan.state, "state_after": plan.state,
            "request": request, "request_data": dict(inputs), "symbolic_refs": refs,
            "data_classification": {
                "input": sorted(set(self._request_vars(base)) | ({"product_id"} if pid is not None and spec["select_product"] != "none" else set())),
                "derived": sorted(k for k in context if k not in inputs),
                "backend_generated": []},
            "context_before": context,
            "violation": {
                "invalid_condition": neg["invalid_condition"], "technique": cand["technique"],
                "changed_variables": list(cand["changed"]),
                "evaluator_status": "NEGATIVE_APPLICABLE", "verified_by": "GuardEvaluator.check_negative_transition",
                "prefix_verified_by": "GuardEvaluator.check_transition / apply_transition (từng bước tiền tố)",
                "isolation": cand["isolation"]},
            "expected": {"outcome": "REJECT", "accepted": bool(expected.get("accepted", False)),
                         "http_status": expected.get("http_status"),
                         "http_status_source": f"efsm_model.negative_transitions[{neg['id']}].expected.http_status",
                         "state_after": plan.state, "state_unchanged": bool(expected.get("state_unchanged", True)),
                         "data_unchanged": bool(expected.get("data_unchanged", True))},
            "captures": {},
            "data_before": before, "data_after": plan.shadow.summary(),
            "rule_ids": list(neg.get("rule_refs", [])), "mutant_refs": list(neg.get("mutant_refs", [])),
            "api_reachable": neg.get("api_reachable", True),
        }
        nxt.steps.append(record)
        return nxt

    def _fixture_negative(self, plan: PlanState, neg: Mapping[str, Any]) -> PlanState | None:
        """NT không đạt được chỉ qua API (api_reachable=false): thêm bước FIXTURE đổi dữ liệu database."""
        base = neg["base_transition"]
        spec = self.map.transition(base)
        fixtures = self.map.data.get("fixtures", {})
        cond_vars = [v for v in self._vars_in(neg["invalid_condition"]) if self.vars[v].get("origin") == "database"]
        if spec is None or not cond_vars or any(v not in fixtures for v in cond_vars):
            self._fail(plan, neg["id"], "UNSUPPORTED_FIXTURE", "chưa có mô tả fixture cho biến database của điều kiện vi phạm")
            return None
        if plan.state != neg["source"]:
            self._fail(plan, base, "STATE_MISMATCH", f"đang ở {plan.state}, negative cần {neg['source']}")
            return None
        view = self._view(plan.state)
        pid = self._ref_pid(plan)
        ctx = plan.shadow.project(pid, view)
        valid, _ = self._valid_inputs(base, ctx)
        inputs = dict(valid) if valid is not None else {n: self.map.hints[n] for n in self._request_vars(base) if n in self.map.hints}
        if "stock" not in ctx and "ordered_quantity" not in ctx:
            self._fail(plan, neg["id"], "NO_CONTEXT", "giỏ hàng trống: không có dòng để tạo xung đột tồn kho", pid)
            return None
        fixed = {k: v for k, v in {**ctx, **inputs}.items() if k not in cond_vars}
        result = self.g.solve({"expr": neg["invalid_condition"]}, fixed=fixed, hints=ctx, mutable=cond_vars)
        if result.status != SolveStatus.SAT or not result.assignment:
            self._fail(plan, neg["id"], "NO_FIXTURE_VALUE", f"{result.status}: {result.reason}", pid)
            return None
        nxt = plan.clone()
        for var in cond_vars:
            fx, value = fixtures[var], result.assignment[var]
            if var != "stock" or pid is None:
                raise _Unresolved(f"fixture cho biến '{var}' chưa được ShadowState hỗ trợ")
            nxt.shadow.set_stock(pid, value)
            nxt.steps.append({
                "step_no": len(nxt.steps) + 1, "role": "fixture", "kind": "FIXTURE", "requires_fixture": True, "via_api": False,
                "action": "SET_DB_VALUE", "target": {"table": fx["table"], "column": fx["column"], "where": {fx["key"]: pid}},
                "value": value, "variable": var,
                "description": f"Đặt {fx['table']}.{fx['column']} = {value} cho {fx['key']}={pid} bằng fixture/seed DB (không có API) để điều kiện '{neg['invalid_condition']}' xảy ra.",
                "status": "REQUIRES_FIXTURE", "data_after": nxt.shadow.summary()})
            nxt.fixtures += 1
        ctx2 = nxt.shadow.project(pid, view)
        status = self.ev.check_negative_transition(neg["id"], nxt.state, {**ctx2, **inputs}).status
        if status is not GuardStatus.NEGATIVE_APPLICABLE:
            self._fail(plan, neg["id"], "NEGATIVE_NOT_APPLICABLE", f"sau fixture: {status.value}", pid)
            return None
        variables = {**ctx2, **inputs}
        cand = {"pid": pid, "inputs": inputs, "variables": variables, "technique": "FIXTURE_DATA_VIOLATION",
                "changed": list(cond_vars), "isolation": self._isolation(neg, base, variables)}
        return self._append_negative(nxt, neg, spec, cand, None)

    def plan_negative(self, prefix: Sequence[str], negative_id: str) -> tuple[PlanState | None, dict[str, Any]]:
        self._failures, self._nodes = [], 0
        neg = self.neg[negative_id]
        best: list[tuple[int, PlanState]] = []
        reachable = neg.get("api_reachable", True)

        def final(plan: PlanState, variant: tuple[int, ...]) -> bool:
            try:
                result = self._negative_step(plan, neg) if reachable else self._fixture_negative(plan, neg)
            except _Unresolved as exc:
                self._fail(plan, negative_id, "UNRESOLVED_CONTEXT", str(exc))
                return False
            if result is None:
                return False
            last = result.steps[-1]
            rank = self._ISO_RANK[last["violation"]["isolation"]["status"]]
            if not best or rank < best[0][0]:
                best[:] = [(rank, result)]
            return rank == 0

        status = self._with_setup(prefix, final)
        if best:
            status = "FOUND"
        return (best[0][1] if best else None), {"search": status, "nodes": self._nodes}

    # ------------------------------------------------------------------
    # Test case
    # ------------------------------------------------------------------
    def _assertions(self, plan: PlanState, negative: bool) -> list[dict[str, Any]]:
        final = plan.steps[-1]["data_after"] if plan.steps else plan.shadow.summary()
        if negative:
            final = plan.steps[-1]["data_before"]
        sim = "shadow_state_simulation"
        out = [
            {"id": "A1", "probe": dict(self.map.data["probes"]["cart"]), "expect": {"items": final["cart_items"]},
             "source": sim, "status": "NOT_EXECUTED"},
            {"id": "A2", "probe": dict(self.map.data["probes"]["products"]), "expect": {"stock_by_product_id": final["product_stock"]},
             "source": sim, "status": "NOT_EXECUTED"},
            {"id": "A3", "probe": dict(self.map.data["probes"]["payments"]),
             "expect": {"payments": [{k: v for k, v in p.items()} for p in final["payments"]]}, "source": sim,
             "status": "NOT_EXECUTED"},
        ]
        for i, order in enumerate(final["orders"], start=1):
            out.append({"id": f"A{3 + i}", "probe": dict(self.map.data["probes"]["order"]),
                        "path_params": {"order_id": order["order_ref"]},
                        "expect": {"status": order["status"], "total_amount": order["total_amount"], "items": order["items"]},
                        "source": sim, "status": "NOT_EXECUTED"})
        if negative:
            for item in self.map.data.get("negative_postconditions", []):
                out.append({"id": f"A{len(out) + 1}", "probe_name": item["probe"],
                            "probe": dict(self.map.data["probes"][item["probe"]]), "check": item["check"],
                            "source": "api_test_mapping.negative_postconditions", "status": "NOT_EXECUTED"})
        return out

    def _invariant_specs(self, plan: PlanState) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        checkpoints, seen = [], {}
        for step in plan.steps:
            cp = step.get("invariant_checkpoint")
            if cp:
                checkpoints.append({"after_step": step["step_no"], "state": step["state_after"], **cp})
                for inv, status in cp["simulated_status"].items():
                    if status in ("SATISFIED", "VIOLATED"):
                        seen[inv] = step["step_no"]
        texts = self.map.data.get("invariant_assertions", {})
        specs = [{"invariant_id": inv, "last_checkpoint_step": n, "post_execution_check": texts.get(inv),
                  "status": "NOT_EXECUTED"} for inv, n in sorted(seen.items())]
        return checkpoints, specs

    def convert_path(self, path: Mapping[str, Any]) -> dict[str, Any]:
        """Chuyển một path của test_paths.json; trả về ``{"readiness", "test_case"?, "reason_code", ...}``."""
        pid_, ptype = path["path_id"], path["path_type"]
        base = {"source_path_id": pid_, "path_key": path["path_key"], "type": ptype,
                "source_feasibility": path.get("feasibility")}
        unknown = [t for t in path.get("transition_ids", []) if t not in self.tr]
        if ptype == "negative" and path.get("negative_transition_id") not in self.neg:
            unknown.append(str(path.get("negative_transition_id")))
        if unknown:
            return {**base, "readiness": Readiness.UNSUPPORTED, "reason_code": "UNKNOWN_TRANSITION",
                    "reason": f"EFSM hiện tại không có: {', '.join(unknown)}"}
        unmapped = [t for t in path.get("transition_ids", []) if (self.map.transition(t) or {}).get("implemented_by") != "API"]
        if unmapped:
            return {**base, "readiness": Readiness.UNSUPPORTED, "reason_code": "NO_API_MAPPING",
                    "reason": f"transition chưa có cách gọi API: {', '.join(unmapped)}"}
        try:
            if ptype == "negative":
                plan, meta = self.plan_negative(path["transition_ids"], path["negative_transition_id"])
            else:
                plan, meta = self.plan_positive(path["transition_ids"])
        except _Unresolved as exc:
            return {**base, "readiness": Readiness.BLOCKED, "reason_code": "UNRESOLVED_CONTEXT", "reason": str(exc)}
        if plan is None:
            return {**base, **self._blocked(meta)}
        return {**base, "readiness": None, "plan": plan, "meta": meta}

    def _blocked(self, meta: Mapping[str, Any]) -> dict[str, Any]:
        failures = self._failures
        codes = {f["code"] for f in failures}
        deepest = max(failures, key=lambda f: f["depth"]) if failures else None
        where = (f"bước {deepest['depth'] + 1} của path ({deepest['transition']}): {deepest['code']} - {deepest['detail']}" if deepest else "không có bước nào khả thi")
        if meta["search"] == "BUDGET":
            return {"readiness": Readiness.BLOCKED, "reason_code": "SEARCH_BUDGET_EXHAUSTED", "evidence": "SEARCH_BUDGET_EXHAUSTED",
                    "reason": f"hết ngân sách tìm kiếm ({self.max_nodes} nút); {where}", "search_nodes": meta["nodes"]}
        if "UNRESOLVED_CONTEXT" in codes:
            return {"readiness": Readiness.BLOCKED, "reason_code": "UNRESOLVED_CONTEXT", "evidence": "SIMULATION_MISMATCH",
                    "reason": next(f["detail"] for f in failures if f["code"] == "UNRESOLVED_CONTEXT")}
        if codes and codes <= {"UNSUPPORTED_GUARD", "UNSUPPORTED_TRANSITION", "UNSUPPORTED_FIXTURE"}:
            return {"readiness": Readiness.UNSUPPORTED, "reason_code": sorted(codes)[0], "reason": where}
        return {"readiness": Readiness.BLOCKED, "reason_code": "NO_FEASIBLE_DATA_FOUND", "evidence": "SEARCH_EXHAUSTED",
                "proof_scope": "bounded search: product choices over the seed catalog and up to "
                               f"{self.max_setup_steps} API setup steps; not a proof that no data exists",
                "reason": where, "search_nodes": meta["nodes"]}

    def finalize_case(self, converted: Mapping[str, Any], path: Mapping[str, Any], case_id: str) -> dict[str, Any]:
        """Dựng bản ghi test case đầy đủ từ kế hoạch đã tìm được."""
        plan: PlanState = converted["plan"]  # type: ignore[assignment]
        negative = converted["type"] == "negative"
        steps = plan.steps
        reasons = []
        if plan.fixtures:
            reasons.append("REQUIRES_FIXTURE")
        if negative:
            iso = steps[-1]["violation"]["isolation"]["status"]
            if iso != "ISOLATED":
                reasons.append(f"NEGATIVE_ISOLATION_{iso}")
        readiness = Readiness.PARTIALLY_READY if reasons else Readiness.READY
        checkpoints, inv_specs = self._invariant_specs(plan)
        setup = [s for s in steps if s["role"] in ("setup", "fixture")]
        last = steps[-1]
        rules = sorted(set(path.get("security_rule_ids", [])) | {r for s in steps for r in s.get("rule_ids", [])})
        case = {
            "test_case_id": case_id, "source_path_id": converted["source_path_id"], "path_key": converted["path_key"],
            "type": converted["type"], "readiness": readiness, "readiness_reasons": reasons,
            "negative_transition_id": path.get("negative_transition_id") if negative else None,
            "negative_category": path.get("negative_category") if negative else None,
            "severity": path.get("severity"), "security_rule_ids": rules,
            "source_feasibility": converted.get("source_feasibility"),
            "replanned": converted.get("source_feasibility") == "INFEASIBLE",
            "setup": [{"step_no": s["step_no"], "kind": "API" if s["kind"] == "transition" else "FIXTURE",
                       "description": (f"{s['transition_id']} {s['request']['method']} {s['request']['path']} (thêm dòng giỏ hàng để tiền đề của path khả thi)"
                                       if s["kind"] == "transition" else s["description"])} for s in setup],
            "initial_data": self._initial().shadow.summary(),
            "steps": steps,
            "expected_outcome": "REJECT_FINAL_STEP" if negative else "ACCEPT_ALL_STEPS",
            "expected_final_state": last["state_after"],
            "expected_final_data": (last["data_before"] if negative else last["data_after"]),
            "assertions": self._assertions(plan, negative),
            "invariant_checkpoints": checkpoints, "invariant_assertions": inv_specs,
            "cleanup": [dict(self.map.data["cleanup"])],
            "symbolic_refs": [r for s in steps for r in s.get("symbolic_refs", [])],
            "execution": {"status": "NOT_EXECUTED"},
        }
        return case


def request_signature(case: Mapping[str, Any]) -> str:
    """Chữ ký các lời gọi của một test case (dùng để loại trùng)."""
    parts = []
    for s in case["steps"]:
        if s["kind"] == "FIXTURE":
            parts.append(["FIXTURE", s["target"], s["value"]])
        else:
            r = s["request"]
            parts.append([s["role"], r["method"], r["path"], r["path_params"], r["body"], s.get("negative_transition_id")])
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _select(cases: list[dict[str, Any]], max_cases: int | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Chọn test case: loại trùng; nếu có giới hạn thì ưu tiên negative, transition mới, rule severity cao."""
    kept, dropped, seen = [], [], {}
    for case in cases:
        sig = request_signature(case)
        if sig in seen:
            dropped.append({"source_path_id": case["source_path_id"], "path_key": case["path_key"], "reason_code": "DUPLICATE",
                            "reason": f"cùng chuỗi lời gọi với {seen[sig]}"})
        else:
            seen[sig] = case["source_path_id"]
            kept.append(case)
    if max_cases is None or len(kept) <= max_cases:
        return kept, dropped
    chosen, remaining = [], list(range(len(kept)))
    covered_t: set[str] = set()
    covered_r: set[str] = set()

    def key(i: int) -> tuple:
        c = kept[i]
        trans = {s.get("transition_id") or s.get("negative_transition_id") for s in c["steps"] if s["kind"] != "FIXTURE"}
        new_t = len(trans - covered_t)
        weight = sum(4 - _SEVERITY_RANK.get(c["severity"], 4) for r in set(c["security_rule_ids"]) - covered_r)
        return (0 if c["type"] == "negative" else 1, -new_t, -weight, _SEVERITY_RANK.get(c["severity"], 4), i)

    while remaining and len(chosen) < max_cases:
        i = min(remaining, key=key)
        remaining.remove(i)
        c = kept[i]
        covered_t |= {s.get("transition_id") or s.get("negative_transition_id") for s in c["steps"] if s["kind"] != "FIXTURE"}
        covered_r |= set(c["security_rule_ids"])
        chosen.append(i)
    for i in remaining:
        dropped.append({"source_path_id": kept[i]["source_path_id"], "path_key": kept[i]["path_key"],
                        "reason_code": "NOT_SELECTED_MAX_CASES", "reason": f"vượt giới hạn max_cases={max_cases}"})
    return [kept[i] for i in sorted(chosen)], dropped


def generate_test_cases(generator: TestDataGenerator, mapping: ApiMapping, paths_doc: Mapping[str, Any], *,
                        include_negative: bool = True, max_cases: int | None = None, max_setup_steps: int = 3,
                        max_nodes: int = 3000, paths_file: str | None = None, paths_sha256: str | None = None
                        ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Chuyển toàn bộ ``test_paths.json`` thành ``test_cases.json`` và ``test_data_coverage.json`` (dạng dict)."""
    planner = PathDataGenerator(generator, mapping, max_setup_steps=max_setup_steps, max_nodes=max_nodes)
    paths = [p for p in paths_doc["paths"] if include_negative or p["path_type"] != "negative"]
    unconverted: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    for path in paths:
        converted = planner.convert_path(path)
        if converted.get("readiness") in (Readiness.BLOCKED, Readiness.UNSUPPORTED):
            unconverted.append({k: v for k, v in converted.items() if k != "plan"})
            continue
        cases.append(planner.finalize_case(converted, path, "TMP"))
    selected, dropped = _select(cases, max_cases)
    for index, case in enumerate(selected, start=1):
        case["test_case_id"] = f"TC{index:03d}"
        case["selection_rank"] = index
    unconverted += [{**d, "type": next((c["type"] for c in cases if c["source_path_id"] == d["source_path_id"]), None),
                     "readiness": "NOT_SELECTED"} for d in dropped]
    rules = {r for c in selected for r in c["security_rule_ids"]}
    index_rules = paths_doc.get("rules_index", {})
    by_readiness = {k: sum(c["readiness"] == k for c in selected) for k in (Readiness.READY, Readiness.PARTIALLY_READY)}
    for k in (Readiness.BLOCKED, Readiness.UNSUPPORTED):
        by_readiness[k] = sum(u.get("readiness") == k for u in unconverted)
    summary = {
        "input_paths": len(paths),
        "input_positive_paths": sum(p["path_type"] == "positive" for p in paths),
        "input_negative_paths": sum(p["path_type"] == "negative" for p in paths),
        "test_cases": len(selected),
        "positive_cases": sum(c["type"] == "positive" for c in selected),
        "negative_cases": sum(c["type"] == "negative" for c in selected),
        "readiness": by_readiness,
        "unconverted_paths": len(unconverted),
        "unconverted_by_reason": {k: sum(u.get("reason_code") == k for u in unconverted)
                                  for k in sorted({u.get("reason_code") for u in unconverted})},
        "paths_infeasible_in_test_paths_now_planned": sum(c["replanned"] for c in selected),
        "cases_with_api_setup_steps": sum(any(s["role"] == "setup" for s in c["steps"]) for c in selected),
        "fixture_steps": sum(s["kind"] == "FIXTURE" for c in selected for s in c["steps"]),
        "rules_linked": len(rules),
        "rules_in_security_rules_index": len(index_rules),
    }
    output = {
        "generator": PATH_GENERATOR_NAME, "schema_version": TEST_CASE_SCHEMA_VERSION,
        "scope": "dữ liệu cho từng test path; KHÔNG gửi HTTP, KHÔNG thực thi; mọi test case ở trạng thái NOT_EXECUTED",
        "model": {"name": generator.model.get("model_name"), "schema_version": generator.model.get("schema_version"),
                  "sha256": generator.model_sha256},
        "sources": {"test_paths": {"file": paths_file, "sha256": paths_sha256, "generator": paths_doc.get("generator"),
                                   "schema_version": paths_doc.get("schema_version"), "total_paths": len(paths_doc["paths"])},
                    "api_mapping": {"file": mapping.source, "sha256": mapping.sha256}},
        "configuration": {"include_negative": include_negative, "max_cases": max_cases, "max_setup_steps": max_setup_steps,
                          "max_nodes": max_nodes, "mapping_issues": planner.mapping_issues},
        "conventions": {
            "data_classes": {"input": "giá trị do test chọn và gửi lên (body / path param từ catalog seed)",
                             "derived": "biến EFSM suy ra từ dữ liệu mô phỏng (cart_items_count, stock, order_total...)",
                             "backend_generated": "giá trị do backend tạo (order.id, payment.id): CHỈ là tham chiếu $ref:steps.N.<đường dẫn response>"},
            "symbolic_ref": "$ref:steps.<step_no>.<response path>; API Executor thay bằng giá trị thật lấy từ response của bước đó",
            "expected_values": "expected_source cho biết giá trị kỳ vọng lấy từ EFSM hay từ mô phỏng dữ liệu (shadow_state_simulation)",
            "invariants": "invariant_checkpoints.simulated_status chỉ là kết quả trên dữ liệu mô phỏng; runtime_status luôn NOT_VERIFIED_UNTIL_EXECUTION"},
        "summary": summary,
        "rules_index": {r: index_rules[r] for r in sorted(rules) if r in index_rules},
        "test_cases": selected,
        "unconverted_paths": unconverted,
    }
    return output, build_coverage(generator, selected, unconverted, paths, summary)


def build_coverage(generator: TestDataGenerator, cases: Sequence[Mapping[str, Any]], unconverted: Sequence[Mapping[str, Any]],
                   paths: Sequence[Mapping[str, Any]], summary: Mapping[str, Any]) -> dict[str, Any]:
    model = generator.model
    all_t = [t["id"] for t in model["transitions"]]
    all_n = [n["id"] for n in model.get("negative_transitions", [])]
    t_path = {s["transition_id"] for c in cases for s in c["steps"] if s["kind"] == "transition" and s["role"] == "path"}
    t_any = {s["transition_id"] for c in cases for s in c["steps"] if s["kind"] == "transition"}
    n_cov = {s["negative_transition_id"] for c in cases for s in c["steps"] if s["kind"] == "negative_transition"}
    states = {s["state_after"] for c in cases for s in c["steps"] if s["kind"] != "FIXTURE"} | {model["initial_state"]}
    inv_ids = [i["id"] for i in model["invariants"]]
    inv: dict[str, Any] = {}
    descriptions = {i["id"]: i.get("description") for i in model["invariants"]}
    for iid in inv_ids:
        counts = {"SATISFIED": 0, "VIOLATED": 0, "SKIPPED": 0, "UNSUPPORTED": 0, "ERROR": 0}
        for c in cases:
            for cp in c["invariant_checkpoints"]:
                counts[cp["simulated_status"].get(iid, "SKIPPED")] += 1
        spec = next(i for i in model["invariants"] if i["id"] == iid)
        inv[iid] = {"description": descriptions[iid], "kind": spec.get("kind"), "covered_by": list(spec.get("covered_by", [])),
                    "simulated_basis": "MODEL_STRUCTURE" if spec.get("kind") == "model_structure" else "EFSM_SIMULATED_DATA",
                    "simulated_checkpoints": counts, "runtime_status": _RUNTIME_UNVERIFIED}
    iso = {k: sum(1 for c in cases if c["type"] == "negative" and c["steps"][-1]["violation"]["isolation"]["status"] == k)
           for k in ("ISOLATED", "NOT_ISOLATED", "UNEVALUATED")}
    classes = {"input": 0, "derived": 0, "backend_generated": 0}
    for c in cases:
        for s in c["steps"]:
            for k, v in s.get("data_classification", {}).items():
                classes[k] += len(v)
    path_rules = {r for p in paths for r in p.get("security_rule_ids", [])}
    case_rules = {r for c in cases for r in c["security_rule_ids"]}
    return {
        "generator": PATH_GENERATOR_NAME, "schema_version": TEST_CASE_SCHEMA_VERSION,
        "summary": dict(summary),
        "transition_coverage": {"total": len(all_t), "covered_as_path_step": sorted(t_path),
                                "covered_including_setup": sorted(t_any), "not_covered": sorted(set(all_t) - t_any),
                                "ratio_path_step": round(len(t_path) / len(all_t), 4) if all_t else None},
        "negative_transition_coverage": {"total": len(all_n), "covered": sorted(n_cov), "not_covered": sorted(set(all_n) - n_cov)},
        "state_coverage": {"covered": sorted(states), "total": len(model["states"])},
        "rule_coverage": {"rules_in_selected_paths": len(path_rules), "rules_linked_to_test_cases": len(case_rules),
                          "not_linked": sorted(path_rules - case_rules)},
        "negative_isolation": iso,
        "data_classification_totals": classes,
        "fixture_steps": [{"test_case_id": c["test_case_id"], "step_no": s["step_no"], "description": s["description"]}
                          for c in cases for s in c["steps"] if s["kind"] == "FIXTURE"],
        "invariants": {"note": "Mọi invariant đều chưa thể kiểm chứng trước khi thực thi; số liệu bên dưới chỉ là đánh giá trên dữ liệu mô phỏng.",
                       "per_invariant": inv,
                       "never_evaluated_as_satisfied_or_violated": sorted(
                           i for i, v in inv.items() if v["simulated_checkpoints"]["SATISFIED"] + v["simulated_checkpoints"]["VIOLATED"] == 0)},
        "unconverted_paths": [{k: u.get(k) for k in ("source_path_id", "path_key", "type", "readiness", "reason_code", "reason",
                                                     "evidence", "proof_scope", "source_feasibility")} for u in unconverted],
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _load_context(path: str | None) -> tuple[dict, dict]:
    if not path:
        return {}, {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelFormatError(f"cannot read context file: {exc}") from exc
    unknown = set(data) - {"defaults", "transitions", "description"}
    if unknown or not isinstance(data.get("defaults", {}), dict) or not isinstance(data.get("transitions", {}), dict):
        raise ModelFormatError("context file must be {\"defaults\": {...}, \"transitions\": {id: {...}}}")
    return data.get("defaults", {}), data.get("transitions", {})


def _run_path_mode(args: argparse.Namespace, model: Path, rules: Path) -> int:
    try:
        generator = TestDataGenerator(model, rules if rules.exists() else None, max_nodes=args.max_nodes)
        mapping_path = Path(args.api_mapping) if args.api_mapping else model.with_name("api_test_mapping.json")
        mapping = ApiMapping.load(mapping_path)
        raw = Path(args.test_paths).read_bytes()
        paths_doc = json.loads(raw.decode("utf-8"))
        if not isinstance(paths_doc, dict) or not isinstance(paths_doc.get("paths"), list):
            raise ModelFormatError("test_paths file must be an object with a 'paths' list")
        if paths_doc.get("model", {}).get("sha256") not in (None, generator.model_sha256):
            print("WARNING: test_paths.json was generated from a different efsm_model.json (sha256 differs)", file=sys.stderr)
        output, coverage = generate_test_cases(
            generator, mapping, paths_doc, include_negative=not args.no_negative_paths, max_cases=args.max_cases,
            max_setup_steps=args.max_setup_steps, max_nodes=args.max_path_nodes, paths_file=Path(args.test_paths).name,
            paths_sha256=hashlib.sha256(raw).hexdigest())
        out = generator.export_json(output, args.test_cases_output)
        cov = generator.export_json(coverage, args.coverage_output)
    except (DataGeneratorError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    s = output["summary"]
    print(f"paths={s['input_paths']} test_cases={s['test_cases']} (positive={s['positive_cases']} negative={s['negative_cases']}) "
          f"readiness={s['readiness']} unconverted={s['unconverted_paths']}")
    print(f"wrote {out} and {cov}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sinh dữ liệu kiểm thử mức guard (EP/BVA) và xác minh bằng GuardEvaluator.")
    parser.add_argument("--model", default="efsm_model.json")
    parser.add_argument("--rules", default=None, help="security_rules.json (mặc định: cùng thư mục model nếu có)")
    parser.add_argument("--transition", action="append", help="chỉ sinh cho transition này (lặp lại được)")
    parser.add_argument("--context", default=None, help='JSON {"defaults": {...}, "transitions": {"T01": {...}}}')
    parser.add_argument("--no-negative", action="store_true")
    parser.add_argument("--no-malformed", action="store_true")
    parser.add_argument("--max-nodes", type=int, default=20000)
    parser.add_argument("--output", default="generated_guard_data.json")
    parser.add_argument("--test-paths", default=None, help="test_paths.json: bật chế độ sinh dữ liệu theo path (phần 2)")
    parser.add_argument("--api-mapping", default=None, help="api_test_mapping.json (mặc định: cùng thư mục model)")
    parser.add_argument("--test-cases-output", default="test_cases.json")
    parser.add_argument("--coverage-output", default="test_data_coverage.json")
    parser.add_argument("--max-cases", type=int, default=None, help="giới hạn số test case (ưu tiên negative, transition mới, rule nặng)")
    parser.add_argument("--max-setup-steps", type=int, default=3)
    parser.add_argument("--max-path-nodes", type=int, default=3000, help="ngân sách tìm kiếm kế hoạch dữ liệu cho mỗi path")
    parser.add_argument("--no-negative-paths", action="store_true")
    args = parser.parse_args(argv)
    model = Path(args.model)
    rules = Path(args.rules) if args.rules else model.with_name("security_rules.json")
    if args.test_paths:
        return _run_path_mode(args, model, rules)
    try:
        generator = TestDataGenerator(model, rules if rules.exists() else None, max_nodes=args.max_nodes)
        defaults, per_transition = _load_context(args.context)
        datasets = generator.generate_all(defaults, per_transition, transition_ids=args.transition,
                                          include_negative=not args.no_negative, include_malformed=not args.no_malformed)
        out = generator.export_json(generator.to_output_dict(datasets, {
            "context_file": Path(args.context).name if args.context else None,
            "include_negative": not args.no_negative, "include_malformed": not args.no_malformed}), args.output)
    except (DataGeneratorError, KeyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    summary = generator.summarize(datasets)
    print(f"guards={summary['guards_analyzed']} supported={summary['guards_supported']} samples={summary['samples_total']} "
          f"verified={summary['verified_by_guard_evaluator']} unverified={summary['verification']['UNVERIFIED']}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
