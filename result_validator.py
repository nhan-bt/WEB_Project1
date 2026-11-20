"""BShop Result Validator (Phần 7).

Đối chiếu kết quả thực thi của API Test Executor (``test_execution_results.json``) với hành vi mong đợi
(``test_cases.json``, ``efsm_model.json``, ``security_rules.json``) và phân loại từng test case thành
PASS / FAIL / SKIPPED / INCONCLUSIVE kèm bằng chứng expected/actual.

Nguyên tắc:
* Chỉ ĐỌC các file JSON. Không gửi HTTP, không import executor hay backend, không đọc database.
* Mọi assertion đều dựa trên dữ liệu đã quan sát (response, observed_state, assertion_probes); thiếu dữ liệu thì
  INCONCLUSIVE, không bao giờ tự PASS. HTTP 4xx/2xx không tự quyết định kết quả.
* Thứ tự ưu tiên đặc tả: assertion trong test case > expected của negative transition (EFSM) > guard/action/invariant EFSM
  > security rules. Nguồn mâu thuẫn -> SPEC_CONFLICT (INCONCLUSIVE), không chọn bên có lợi cho backend.
* Nhãn category chỉ là phân loại sơ bộ để truy vết; không kết luận mức độ nghiêm trọng hay lỗ hổng (Phần 8).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

VALIDATOR_NAME = "BShop Result Validator"
SCHEMA_VERSION = "1.0"
SUPPORTED_INPUT_SCHEMA = "1.0"

PASS, FAIL, SKIPPED, INCONCLUSIVE = "PASS", "FAIL", "SKIPPED", "INCONCLUSIVE"

# Nhãn phân loại sơ bộ (không phải mức độ nghiêm trọng)
HTTP_STATUS_MISMATCH = "HTTP_STATUS_MISMATCH"
RESPONSE_BODY_MISMATCH = "RESPONSE_BODY_MISMATCH"
INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
GUARD_VIOLATION = "GUARD_VIOLATION"
INVARIANT_VIOLATION = "INVARIANT_VIOLATION"
UNEXPECTED_SIDE_EFFECT = "UNEXPECTED_SIDE_EFFECT"
STOCK_INTEGRITY_VIOLATION = "STOCK_INTEGRITY_VIOLATION"
PAYMENT_LIFECYCLE_VIOLATION = "PAYMENT_LIFECYCLE_VIOLATION"
RESOURCE_LIFECYCLE_VIOLATION = "RESOURCE_LIFECYCLE_VIOLATION"
# Nhãn cho trường hợp không kết luận được
SPEC_CONFLICT = "SPEC_CONFLICT"
MISSING_DATA = "MISSING_OBSERVED_DATA"
UNSUPPORTED_ASSERTION = "UNSUPPORTED_ASSERTION"
SPEC_OR_TESTDATA_ISSUE = "SPEC_OR_TESTDATA_ISSUE"
EXECUTION_ISSUE = "EXECUTION_ISSUE"

# Invariant kiểu cấu trúc mô hình -> negative transition kiểm chứng chúng (theo invariant_assertions của Phần 5)
STRUCTURAL_INVARIANT_VERIFIERS: dict[str, tuple[str, ...]] = {
    "INV-06": ("NT08",),
    "INV-07": ("NT05", "NT10", "NT20"),
    "INV-08": ("NT06", "NT07", "NT11"),
    "INV-10": ("NT09", "NT10", "NT11"),
}
INVARIANT_CATEGORY = {
    "INV-01": INVARIANT_VIOLATION, "INV-02": STOCK_INTEGRITY_VIOLATION, "INV-03": INVARIANT_VIOLATION,
    "INV-04": PAYMENT_LIFECYCLE_VIOLATION, "INV-05": STOCK_INTEGRITY_VIOLATION,
    "INV-09": PAYMENT_LIFECYCLE_VIOLATION, "INV-11": STOCK_INTEGRITY_VIOLATION,
    "INV-12": PAYMENT_LIFECYCLE_VIOLATION,
}
ORDER_STATUSES = ("PENDING_CONFIRMATION", "CONFIRMED", "PAID", "CANCELLED")
CART_BOUND_PREFIX = "/api/cart"

POSITIVE_DOMAIN_CATEGORY = {
    "cart": RESOURCE_LIFECYCLE_VIOLATION, "stock": STOCK_INTEGRITY_VIOLATION,
    "orders": RESOURCE_LIFECYCLE_VIOLATION, "payments": PAYMENT_LIFECYCLE_VIOLATION,
}
NEGATIVE_DOMAIN_CATEGORY = {
    "cart": UNEXPECTED_SIDE_EFFECT, "stock": STOCK_INTEGRITY_VIOLATION,
    "orders": RESOURCE_LIFECYCLE_VIOLATION, "payments": PAYMENT_LIFECYCLE_VIOLATION,
}


class ValidatorError(Exception):
    """Lỗi chung của validator."""


class InputError(ValidatorError):
    """File đầu vào không đọc được hoặc sai cấu trúc tổng thể."""


class Unresolved(ValidatorError):
    """Không giải được tham chiếu / thiếu dữ liệu quan sát."""


# ---------------------------------------------------------------------------
# Result Loader
# ---------------------------------------------------------------------------
def _read_json(path: Path, label: str) -> tuple[Any, str]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise InputError(f"không đọc được {label} ({path}): {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{label} không phải JSON hợp lệ: {exc}") from exc


class Inputs:
    """Bốn nguồn dữ liệu đã nạp và kiểm tra cấu trúc tổng thể."""

    def __init__(self, cases: Any, results: Any, model: Any, rules: Any, *,
                 cases_sha256: str | None = None, results_sha256: str | None = None,
                 model_sha256: str | None = None, rules_sha256: str | None = None) -> None:
        if not isinstance(cases, dict) or not isinstance(cases.get("test_cases"), list):
            raise InputError("test_cases.json: thiếu danh sách 'test_cases'")
        if cases.get("schema_version") != SUPPORTED_INPUT_SCHEMA:
            raise InputError(f"test_cases.json: schema_version {cases.get('schema_version')!r} không được hỗ trợ")
        if not isinstance(results, dict) or not isinstance(results.get("results"), list):
            raise InputError("test_execution_results.json: thiếu danh sách 'results'")
        if results.get("schema_version") != SUPPORTED_INPUT_SCHEMA:
            raise InputError(f"test_execution_results.json: schema_version {results.get('schema_version')!r} không được hỗ trợ")
        if not isinstance(model, dict) or not isinstance(model.get("transitions"), list):
            raise InputError("efsm_model.json: thiếu 'transitions'")
        if not isinstance(rules, dict) or not isinstance(rules.get("rules"), list):
            raise InputError("security_rules.json: thiếu 'rules'")
        self.cases, self.results, self.model, self.rules = cases, results, model, rules
        self.hashes = {"test_cases": cases_sha256, "test_execution_results": results_sha256,
                       "efsm_model": model_sha256, "security_rules": rules_sha256}
        self.transitions = {t["id"]: t for t in model["transitions"]}
        self.negatives = {n["id"]: n for n in model.get("negative_transitions", [])}
        self.invariants = {i["id"]: i for i in model.get("invariants", [])}
        self.rule_index = {r["rule_id"]: r for r in rules["rules"]}
        self.unconverted = {p.get("source_path_id") or p.get("path_id"): p for p in cases.get("unconverted_paths", [])}

    @classmethod
    def from_files(cls, test_cases: Path, execution_results: Path, model: Path, rules: Path) -> "Inputs":
        c, ch = _read_json(test_cases, "test_cases.json")
        r, rh = _read_json(execution_results, "test_execution_results.json")
        m, mh = _read_json(model, "efsm_model.json")
        s, sh = _read_json(rules, "security_rules.json")
        return cls(c, r, m, s, cases_sha256=ch, results_sha256=rh, model_sha256=mh, rules_sha256=sh)


# ---------------------------------------------------------------------------
# Chuẩn hóa trạng thái quan sát / trạng thái mong đợi
# ---------------------------------------------------------------------------
def norm_observed(obs: Any) -> dict[str, Any] | None:
    """Chuẩn hóa observed_state của executor; trả None nếu thiếu/ sai cấu trúc."""
    if not isinstance(obs, dict):
        return None
    try:
        cart = sorted((int(i["product_id"]), int(i["quantity"])) for i in obs["cart"]["items"])
        stock = {str(k): int(v) for k, v in obs["product_stock"].items()}
        orders = {}
        for o in obs["orders"]:
            orders[int(o["id"])] = {
                "status": o["status"], "total": o["total_amount"],
                "items": sorted((int(i["product_id"]), int(i["quantity"]), i["unit_price"]) for i in o["items"]),
            }
        payments = sorted((int(p["order_id"]), p["amount"], p["result"]) for p in obs["payments"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return {"cart": cart, "stock": stock, "orders": orders, "payments": payments}


def resolve_ref(ref: str, result: dict[str, Any]) -> Any:
    """Giải ``$ref:steps.N.response.a.b`` từ response đã ghi của executor."""
    if not (isinstance(ref, str) and ref.startswith("$ref:steps.")):
        raise Unresolved(f"tham chiếu không hỗ trợ: {ref!r}")
    parts = ref[len("$ref:"):].split(".")
    try:
        step_no = int(parts[1])
        if parts[2] != "response":
            raise ValueError
    except (IndexError, ValueError) as exc:
        raise Unresolved(f"tham chiếu sai cú pháp: {ref!r}") from exc
    step = next((s for s in result.get("steps", []) if s.get("step_no") == step_no), None)
    node: Any = ((step or {}).get("response") or {}).get("body")
    if node is None:
        raise Unresolved(f"{ref}: bước {step_no} không có response body")
    for key in parts[3:]:
        try:
            node = node[int(key)] if isinstance(node, list) else node[key]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise Unresolved(f"{ref}: không có '{key}' trong response") from exc
    if node is None:
        raise Unresolved(f"{ref}: giá trị null")
    return node


def norm_expected(data: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Chuẩn hóa data_after/expected_final_data của test case; ``order_ref`` được giải bằng ID thật từ executor."""
    if not isinstance(data, dict):
        raise Unresolved("test case thiếu dữ liệu mong đợi")
    try:
        cart = sorted((int(i["product_id"]), int(i["quantity"])) for i in data["cart_items"])
        stock = {str(k): int(v) for k, v in data["product_stock"].items()}
        orders: dict[int, dict[str, Any]] = {}
        for o in data["orders"]:
            oid = int(resolve_ref(o["order_ref"], result))
            orders[oid] = {"status": o["status"], "total": o["total_amount"],
                           "items": sorted((int(i["product_id"]), int(i["quantity"]), i["unit_price"]) for i in o["items"])}
        payments = sorted((int(resolve_ref(p["order_ref"], result)), p["amount"], p["result"]) for p in data["payments"])
    except (KeyError, TypeError, ValueError) as exc:
        raise Unresolved(f"dữ liệu mong đợi sai cấu trúc: {exc}") from exc
    return {"cart": cart, "stock": stock, "orders": orders, "payments": payments}


def _orders_without_status(orders: dict[int, dict[str, Any]]) -> dict[int, tuple[Any, Any]]:
    return {i: (o["total"], o["items"]) for i, o in orders.items()}


def _statuses(orders: dict[int, dict[str, Any]]) -> dict[int, str]:
    return {i: o["status"] for i, o in orders.items()}


def diff_states(expected: dict[str, Any], actual: dict[str, Any], *, partial_stock: bool = True) -> dict[str, tuple[Any, Any]]:
    """So sánh hai trạng thái đã chuẩn hóa. Trả về {miền: (expected, actual)} cho các miền lệch.

    Các miền: cart, stock, orders (không gồm status), status (trạng thái đơn), payments.
    """
    out: dict[str, tuple[Any, Any]] = {}
    if expected["cart"] != actual["cart"]:
        out["cart"] = (expected["cart"], actual["cart"])
    exp_stock = expected["stock"]
    act_stock = {k: v for k, v in actual["stock"].items() if k in exp_stock} if partial_stock else actual["stock"]
    if exp_stock != act_stock:
        out["stock"] = (exp_stock, act_stock)
    if _orders_without_status(expected["orders"]) != _orders_without_status(actual["orders"]):
        out["orders"] = (_orders_without_status(expected["orders"]), _orders_without_status(actual["orders"]))
    if _statuses(expected["orders"]) != _statuses(actual["orders"]):
        out["status"] = (_statuses(expected["orders"]), _statuses(actual["orders"]))
    if expected["payments"] != actual["payments"]:
        out["payments"] = (expected["payments"], actual["payments"])
    return out


# ---------------------------------------------------------------------------
# Trình dựng assertion
# ---------------------------------------------------------------------------
def _assertion(aid: str, *, status: str, category: str | None, check: str, transition_id: str | None, step_no: int | None,
               expected: Any, actual: Any, rule_refs: list[str] | None = None, evidence: dict[str, Any] | None = None,
               reason: str | None = None, expected_source: str | None = None) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "assertion_id": aid, "check": check, "transition_id": transition_id, "step_no": step_no,
        "category": category, "status": status, "expected": expected, "actual": actual,
        "expected_source": expected_source, "rule_refs": sorted(set(rule_refs or [])), "evidence": dict(evidence or {}),
    }
    if step_no is not None:
        rec["evidence"].setdefault("execution_step", step_no)
    if reason:
        rec["reason"] = reason
    return rec


def _json_key(value: Any) -> Any:
    """Khóa JSON an toàn cho dict có khóa int (để xuất file)."""
    if isinstance(value, dict):
        return {str(k): _json_key(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_key(v) for v in value]
    return value


def _path_get(body: Any, path: list[str]) -> Any:
    node = body
    for key in path:
        if isinstance(node, list):
            node = node[int(key)]
        else:
            node = node[key]
    return node


def _norm_items(value: Any) -> Any:
    if isinstance(value, list) and all(isinstance(v, dict) for v in value):
        return sorted((int(v["product_id"]), int(v["quantity"]), v["unit_price"]) for v in value)
    return value


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------
class ResultValidator:
    """Xác minh từng test case bằng dữ liệu đã có; không thực hiện HTTP."""

    def __init__(self, inputs: Inputs) -> None:
        self.inputs = inputs

    # ----- toàn bộ suite -----
    def validate_all(self) -> dict[str, Any]:
        inp = self.inputs
        results_by_id: dict[str, dict[str, Any]] = {}
        data_errors: list[dict[str, Any]] = []
        extra: list[dict[str, Any]] = []
        for res in inp.results["results"]:
            tcid = res.get("test_case_id") if isinstance(res, dict) else None
            if not tcid:
                if isinstance(res, dict) and res.get("execution_status") == "SKIPPED" and res.get("path_id") in inp.unconverted:
                    extra.append(res)
                else:
                    data_errors.append({"code": "MISSING_TEST_CASE_ID", "path_id": (res or {}).get("path_id") if isinstance(res, dict) else None,
                                        "message": "kết quả thực thi không có test_case_id và không thuộc path chưa chuyển đổi"})
                    extra.append(res if isinstance(res, dict) else {})
                continue
            if tcid in results_by_id:
                data_errors.append({"code": "DUPLICATE_RESULT", "test_case_id": tcid, "message": "nhiều kết quả cho cùng test_case_id"})
                continue
            results_by_id[tcid] = res
        case_ids = {c.get("test_case_id") for c in inp.cases["test_cases"]}
        for tcid in sorted(set(results_by_id) - case_ids):
            data_errors.append({"code": "UNKNOWN_TEST_CASE", "test_case_id": tcid, "message": "kết quả thực thi không có test case tương ứng"})
        src = (inp.results.get("source") or {}).get("sha256")
        hash_mismatch = bool(src and inp.hashes["test_cases"] and src != inp.hashes["test_cases"])
        if hash_mismatch:
            data_errors.append({"code": "SOURCE_HASH_MISMATCH",
                                "message": "test_execution_results.json được sinh từ một test_cases.json khác (sha256 không khớp)"})
        out: list[dict[str, Any]] = []
        for case in inp.cases["test_cases"]:
            res = results_by_id.get(case.get("test_case_id"))
            rec = self.validate_case(case, res, force_inconclusive=("SOURCE_HASH_MISMATCH",) if hash_mismatch else ())
            out.append(rec)
        for res in extra:
            out.append(self._unmatched_result(res, data_errors))
        return self._document(out, data_errors)

    def _unmatched_result(self, res: dict[str, Any], data_errors: list[dict[str, Any]]) -> dict[str, Any]:
        tcid = res.get("test_case_id")
        base = {"test_case_id": tcid, "path_id": res.get("path_id"), "path_key": res.get("path_key"),
                "test_type": res.get("test_type"), "negative_transition_id": res.get("negative_transition_id"),
                "execution_status": res.get("execution_status"), "assertions": [], "data_errors": []}
        if res.get("execution_status") == "SKIPPED" and res.get("path_id") in self.inputs.unconverted:
            skip = res.get("skip_reason") or {}
            return {**base, "validation_status": SKIPPED, "reasons": [{"code": skip.get("code", "SKIPPED"), "message": skip.get("message")}],
                    "assertion_counts": _count([])}
        err = {"code": "MISSING_TEST_CASE_ID", "message": "không liên kết được kết quả với test case gốc"}
        return {**base, "validation_status": INCONCLUSIVE, "reasons": [err], "data_errors": [err], "assertion_counts": _count([])}

    # ----- một test case -----
    def validate_case(self, case: dict[str, Any], result: dict[str, Any] | None, *, force_inconclusive: tuple[str, ...] = ()) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "test_case_id": case.get("test_case_id"), "path_id": case.get("source_path_id"), "path_key": case.get("path_key"),
            "test_type": str(case.get("type", "")).upper() or None, "negative_transition_id": case.get("negative_transition_id"),
            "readiness": case.get("readiness"), "execution_status": (result or {}).get("execution_status"),
            "validation_status": INCONCLUSIVE, "reasons": [], "data_errors": [], "assertions": [],
            "notes": [], "assertion_counts": _count([]),
        }
        if not case.get("test_case_id"):
            rec["data_errors"].append({"code": "MISSING_TEST_CASE_ID", "message": "test case không có test_case_id"})
            rec["reasons"].append(rec["data_errors"][-1])
            return rec
        if case.get("readiness_reasons"):
            rec["notes"].append({"code": "READINESS_REASONS", "message": ", ".join(map(str, case["readiness_reasons"]))})
        if result is None:
            rec["reasons"].append({"code": "NO_EXECUTION_RESULT", "message": "không có kết quả thực thi cho test case này"})
            return rec
        if force_inconclusive:
            rec["reasons"].append({"code": force_inconclusive[0], "message": "nguồn kết quả không khớp test_cases.json"})
            return rec
        problems = self._consistency(case, result)
        if problems:
            rec["data_errors"].extend(problems)
            rec["reasons"].extend(problems)
            return rec
        status = result.get("execution_status")
        if status == "SKIPPED":
            skip = result.get("skip_reason") or {}
            rec["validation_status"] = SKIPPED
            rec["reasons"].append({"code": skip.get("code", "SKIPPED"), "message": skip.get("message")})
            return rec
        gate = self._execution_gate(result)
        if gate:
            rec["reasons"].extend(gate)
            return rec
        try:
            assertions = self._evaluate(case, result)
        except Unresolved as exc:  # lỗi dữ liệu không lường trước: không đoán
            rec["reasons"].append({"code": "UNRESOLVED_DATA", "message": str(exc)})
            return rec
        rec["assertions"] = assertions
        rec["assertion_counts"] = _count(assertions)
        statuses = {a["status"] for a in assertions}
        if FAIL in statuses:
            rec["validation_status"] = FAIL
        elif INCONCLUSIVE in statuses:
            rec["validation_status"] = INCONCLUSIVE
            rec["reasons"].append({"code": "UNEVALUATED_ASSERTIONS", "message": "có assertion không thể đánh giá"})
        elif not assertions:
            rec["reasons"].append({"code": "NO_ASSERTIONS", "message": "không có assertion nào để kiểm tra"})
        else:
            rec["validation_status"] = PASS
        return rec

    # ----- kiểm tra nhất quán và cổng thực thi -----
    @staticmethod
    def _consistency(case: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
        errs: list[dict[str, Any]] = []

        def bad(msg: str) -> None:
            errs.append({"code": "INCONSISTENT_SOURCES", "message": msg})

        if result.get("test_case_id") != case.get("test_case_id"):
            bad("test_case_id không khớp")
        if result.get("path_id") != case.get("source_path_id"):
            bad(f"path_id {result.get('path_id')!r} != source_path_id {case.get('source_path_id')!r}")
        if str(result.get("test_type", "")).upper() != str(case.get("type", "")).upper():
            bad("test_type không khớp")
        if result.get("negative_transition_id") != case.get("negative_transition_id"):
            bad("negative_transition_id không khớp")
        if result.get("execution_status") in ("SKIPPED",):
            return errs
        want = [(s.get("step_no"), s.get("transition_id") or s.get("negative_transition_id")) for s in case.get("steps", []) if s.get("kind") != "FIXTURE"]
        got = [(s.get("step_no"), s.get("transition_id") or s.get("negative_transition_id")) for s in result.get("steps", []) if s.get("kind") != "FIXTURE"]
        if result.get("execution_status") == "COMPLETED" and want != got:
            bad(f"danh sách bước không khớp: test case {want} / kết quả {got}")
        return errs

    @staticmethod
    def _execution_gate(result: dict[str, Any]) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        if result.get("execution_status") != "COMPLETED":
            reasons.append({"code": "EXECUTION_NOT_COMPLETED", "message": f"execution_status = {result.get('execution_status')}",
                            "errors": [{"kind": e.get("kind"), "step_no": e.get("step_no")} for e in result.get("errors", [])]})
            return reasons
        for step in result.get("steps", []):
            if step.get("execution_status") not in ("SENT", "FIXTURE_APPLIED"):
                reasons.append({"code": "STEP_NOT_EXECUTED", "message": f"bước {step.get('step_no')} có execution_status {step.get('execution_status')}"})
        iso = result.get("isolation") or {}
        if iso.get("initial_state_matches_test_case") is False:
            reasons.append({"code": "ENVIRONMENT_MISMATCH", "message": "trạng thái ban đầu không khớp initial_data"})
        return reasons

    # ----- lõi đánh giá -----
    def _evaluate(self, case: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        res_steps = {s["step_no"]: s for s in result.get("steps", [])}
        ordered = result.get("steps", [])
        snapshots = self._snapshots(result)
        for idx, cstep in enumerate(case.get("steps", [])):
            if cstep.get("kind") == "FIXTURE":
                continue
            rstep = res_steps.get(cstep["step_no"])
            before = snapshots[idx]["before"]
            after = snapshots[idx]["after"]
            if cstep.get("kind") == "negative_transition":
                out.extend(self._negative_step(case, cstep, rstep, before, after))
            else:
                out.extend(self._positive_step(case, cstep, rstep, before, after, result))
        out.extend(self._case_assertions(case, result, snapshots))
        out.extend(self._invariants(case, result, snapshots))
        del ordered
        return out

    @staticmethod
    def _snapshots(result: dict[str, Any]) -> list[dict[str, Any]]:
        """Với mỗi bước (theo thứ tự thực thi): trạng thái quan sát trước/sau (đã chuẩn hóa; None nếu thiếu)."""
        snaps: list[dict[str, Any]] = []
        prev = norm_observed(result.get("initial_observed_state"))
        for step in result.get("steps", []):
            cur = norm_observed(step.get("observed_state")) if not step.get("observation_errors") else None
            snaps.append({"before": prev, "after": cur, "step_no": step.get("step_no"), "kind": step.get("kind")})
            prev = cur
        return snaps

    # ----- expected -----
    def resolve_expected(self, cstep: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Kết hợp expected của test case với EFSM/rules; trả về (expected, mâu thuẫn)."""
        exp = dict(cstep.get("expected") or {})
        conflicts: list[dict[str, Any]] = []
        inp = self.inputs
        if cstep.get("kind") == "negative_transition":
            nt = inp.negatives.get(cstep.get("negative_transition_id"))
            if nt is None:
                conflicts.append({"what": "UNKNOWN_NEGATIVE_TRANSITION", "id": cstep.get("negative_transition_id")})
            else:
                nexp = nt.get("expected", {})
                for key in ("http_status", "accepted", "state_unchanged", "data_unchanged"):
                    if key in nexp and key in exp and nexp[key] != exp[key]:
                        conflicts.append({"what": key, "test_case": exp[key], "efsm": nexp[key],
                                          "sources": [exp.get("http_status_source"), f"efsm_model.negative_transitions[{nt['id']}].expected.{key}"]})
                for rid in nt.get("rule_refs", []):
                    viol = (inp.rule_index.get(rid) or {}).get("violation_expected") or {}
                    if "http_status" in viol and viol["http_status"] != nexp.get("http_status"):
                        conflicts.append({"what": "http_status", "efsm": nexp.get("http_status"), "security_rule": viol["http_status"],
                                          "sources": [f"efsm_model.negative_transitions[{nt['id']}]", f"security_rules.rules[{rid}].violation_expected"]})
        else:
            tr = inp.transitions.get(cstep.get("transition_id"))
            if tr is None:
                conflicts.append({"what": "UNKNOWN_TRANSITION", "id": cstep.get("transition_id")})
            elif tr.get("expected_http_status") != exp.get("http_status"):
                conflicts.append({"what": "http_status", "test_case": exp.get("http_status"), "efsm": tr.get("expected_http_status"),
                                  "sources": [exp.get("http_status_source"), f"efsm_model.transitions[{tr['id']}].expected_http_status"]})
        return exp, conflicts

    def _rule_refs(self, cstep: dict[str, Any]) -> list[str]:
        refs = list(cstep.get("rule_ids") or [])
        nt = self.inputs.negatives.get(cstep.get("negative_transition_id")) if cstep.get("kind") == "negative_transition" else None
        if nt:
            refs += list(nt.get("rule_refs", []))
        return refs

    # ----- bước positive/setup -----
    def _positive_step(self, case, cstep, rstep, before, after, result) -> list[dict[str, Any]]:
        n, tid = cstep["step_no"], cstep.get("transition_id")
        pre = f"S{n}"
        refs = self._rule_refs(cstep)
        exp, conflicts = self.resolve_expected(cstep)
        resp = (rstep or {}).get("response") or {}
        evid = {"expected_ref": (rstep or {}).get("expected_ref"), "request": _req(rstep)}
        out: list[dict[str, Any]] = []
        guard = (cstep.get("guard_check") or {}).get("status")
        if guard is not None and guard != "GUARD_SATISFIED":
            out.append(_assertion(f"{pre}.PRECONDITION", status=INCONCLUSIVE, category=SPEC_OR_TESTDATA_ISSUE, check="guard precondition",
                                  transition_id=tid, step_no=n, expected="GUARD_SATISFIED", actual=guard, rule_refs=refs, evidence=evid,
                                  reason="test case yêu cầu thành công nhưng dữ liệu không thỏa guard: lỗi đặc tả hoặc dữ liệu kiểm thử"))
        out.append(self._http(pre, tid, n, exp, conflicts, resp, refs, evid))
        out.extend(self._body_checks(pre, tid, n, exp, resp, refs, evid, result))
        if before is None or after is None:
            out.append(_assertion(f"{pre}.STATE", status=INCONCLUSIVE, category=MISSING_DATA, check="order state / data after step", transition_id=tid,
                                  step_no=n, expected=None, actual=None, rule_refs=refs, evidence=evid,
                                  reason="thiếu observed_state của bước hoặc của bước liền trước"))
            return out
        try:
            expected = norm_expected(cstep.get("data_after"), result)
        except Unresolved as exc:
            out.append(_assertion(f"{pre}.STATE", status=INCONCLUSIVE, category=MISSING_DATA, check="order state / data after step", transition_id=tid,
                                  step_no=n, expected=None, actual=None, rule_refs=refs, evidence=evid, reason=str(exc)))
            return out
        diffs = diff_states(expected, after)
        state_after = exp.get("state_after")
        # trạng thái đơn
        exp_status = _statuses(expected["orders"])
        act_status = _statuses(after["orders"])
        target_note = None
        if state_after in ORDER_STATUSES and exp_status and state_after not in exp_status.values():
            target_note = "state_after của bước không có trong data_after của test case"
        out.append(_assertion(
            f"{pre}.STATE", status=FAIL if "status" in diffs else PASS, category=INVALID_STATE_TRANSITION, check="order status after step",
            transition_id=tid, step_no=n, expected={"state_after": state_after, "order_status": _json_key(exp_status)},
            actual={"order_status": _json_key(act_status)}, rule_refs=refs, expected_source=exp.get("http_status_source"),
            evidence={**evid, "observed_ref": f"steps[{n}].observed_state.orders"}, reason=target_note))
        for dom in ("cart", "stock", "orders", "payments"):
            bad = dom in diffs
            out.append(_assertion(
                f"{pre}.DATA.{dom}", status=FAIL if bad else PASS, category=POSITIVE_DOMAIN_CATEGORY[dom], check=f"{dom} after step",
                transition_id=tid, step_no=n, expected=_json_key(diffs[dom][0]) if bad else "khớp data_after",
                actual=_json_key(diffs[dom][1]) if bad else "khớp", rule_refs=refs, evidence={**evid, "observed_ref": f"steps[{n}].observed_state"}))
        return out

    def _http(self, pre, tid, n, exp, conflicts, resp, refs, evid) -> dict[str, Any]:
        want, got = exp.get("http_status"), resp.get("status_code")
        conf = [c for c in conflicts if c.get("what") in ("http_status", "UNKNOWN_TRANSITION", "UNKNOWN_NEGATIVE_TRANSITION")]
        if conf:
            return _assertion(f"{pre}.HTTP", status=INCONCLUSIVE, category=SPEC_CONFLICT, check="http status", transition_id=tid, step_no=n,
                              expected=want, actual=got, rule_refs=refs, evidence={**evid, "conflicts": conf},
                              reason="các nguồn đặc tả mâu thuẫn; không chọn bên nào")
        if want is None or got is None:
            return _assertion(f"{pre}.HTTP", status=INCONCLUSIVE, category=MISSING_DATA, check="http status", transition_id=tid, step_no=n,
                              expected=want, actual=got, rule_refs=refs, evidence=evid, reason="thiếu expected http_status hoặc response")
        return _assertion(f"{pre}.HTTP", status=PASS if want == got else FAIL, category=HTTP_STATUS_MISMATCH, check="http status", transition_id=tid,
                          step_no=n, expected=want, actual=got, rule_refs=refs, expected_source=exp.get("http_status_source"),
                          evidence={**evid, "http_status": got})

    def _body_checks(self, pre, tid, n, exp, resp, refs, evid, result=None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        checks = [c for c in exp.get("response_checks", []) if c.get("target") != "response.status_code"]
        for k, chk in enumerate(checks, 1):
            aid = f"{pre}.BODY.{chk.get('target', k)}"
            target, op = chk.get("target", ""), chk.get("op")
            if isinstance(chk.get("value"), str) and chk["value"].startswith("$ref:"):
                try:  # giá trị mong đợi là tham chiếu tới response bước trước (ID do backend sinh)
                    chk = {**chk, "value": resolve_ref(chk["value"], result or {})}
                except Unresolved as exc:
                    out.append(_assertion(aid, status=INCONCLUSIVE, category=MISSING_DATA, check="response body", transition_id=tid, step_no=n,
                                          expected=chk["value"], actual=None, rule_refs=refs, evidence=evid, reason=str(exc)))
                    continue
            if op != "==" or "value" not in chk or not str(target).startswith("response."):
                out.append(_assertion(aid, status=INCONCLUSIVE, category=UNSUPPORTED_ASSERTION, check="response body", transition_id=tid, step_no=n,
                                      expected=chk, actual=None, rule_refs=refs, evidence=evid, reason=f"assertion không được hỗ trợ (op={op!r}, target={target!r})"))
                continue
            body = resp.get("body")
            try:
                actual = _norm_items(_path_get(body, str(target).split(".")[1:]))
                want = _norm_items(chk["value"])
            except (KeyError, IndexError, TypeError, ValueError):
                actual, want = MISSING, _norm_items(chk["value"]) if isinstance(chk.get("value"), list) else chk.get("value")
            if actual is MISSING:
                out.append(_assertion(aid, status=FAIL if resp.get("status_code") is not None and 200 <= resp["status_code"] < 300 else INCONCLUSIVE,
                                      category=RESPONSE_BODY_MISMATCH, check="response body", transition_id=tid, step_no=n,
                                      expected=_json_key(want), actual="<thiếu trường trong response>", rule_refs=refs,
                                      expected_source=chk.get("source"), evidence={**evid, "http_status": resp.get("status_code")}))
                continue
            out.append(_assertion(aid, status=PASS if actual == want else FAIL, category=RESPONSE_BODY_MISMATCH, check="response body",
                                  transition_id=tid, step_no=n, expected=_json_key(want), actual=_json_key(actual), rule_refs=refs,
                                  expected_source=chk.get("source"), evidence={**evid, "http_status": resp.get("status_code")}))
        return out

    # ----- bước negative -----
    def _negative_step(self, case, cstep, rstep, before, after) -> list[dict[str, Any]]:
        n, ntid = cstep["step_no"], cstep.get("negative_transition_id")
        pre = f"S{n}"
        refs = self._rule_refs(cstep)
        exp, conflicts = self.resolve_expected(cstep)
        resp = (rstep or {}).get("response") or {}
        evid = {"expected_ref": (rstep or {}).get("expected_ref"), "request": _req(rstep)}
        out: list[dict[str, Any]] = []
        viol = (cstep.get("violation") or {}).get("evaluator_status")
        if viol is not None and viol != "NEGATIVE_APPLICABLE":
            out.append(_assertion(f"{pre}.PRECONDITION", status=INCONCLUSIVE, category=SPEC_OR_TESTDATA_ISSUE, check="negative applicability",
                                  transition_id=ntid, step_no=n, expected="NEGATIVE_APPLICABLE", actual=viol, rule_refs=refs, evidence=evid,
                                  reason="điều kiện vi phạm không áp dụng được trên dữ liệu kiểm thử"))
        code = resp.get("status_code")
        neg_cat = case.get("negative_category")
        reject_cat = INVALID_STATE_TRANSITION if neg_cat == "INVALID_STATE_TRANSITION" else GUARD_VIOLATION
        if code is None:
            out.append(_assertion(f"{pre}.REJECTED", status=INCONCLUSIVE, category=MISSING_DATA, check="request rejected", transition_id=ntid, step_no=n,
                                  expected="rejected", actual=None, rule_refs=refs, evidence=evid, reason="thiếu response"))
        else:
            rejected = not (200 <= code < 300)
            out.append(_assertion(f"{pre}.REJECTED", status=PASS if rejected else FAIL, category=reject_cat, check="request rejected",
                                  transition_id=ntid, step_no=n, expected={"accepted": False}, actual={"accepted": not rejected, "http_status": code},
                                  rule_refs=refs, expected_source=exp.get("http_status_source"), evidence={**evid, "http_status": code}))
        out.append(self._http(pre, ntid, n, exp, conflicts, resp, refs, evid))
        if before is None or after is None:
            out.append(_assertion(f"{pre}.STATE", status=INCONCLUSIVE, category=MISSING_DATA, check="state/data unchanged", transition_id=ntid,
                                  step_no=n, expected="không đổi", actual=None, rule_refs=refs, evidence=evid,
                                  reason="thiếu observed_state trước/sau bước negative"))
            return out
        diffs = diff_states(before, after, partial_stock=False)
        state_exp = {"state_unchanged": exp.get("state_unchanged"), "state_after": exp.get("state_after")}
        out.append(_assertion(f"{pre}.STATE", status=FAIL if "status" in diffs else PASS, category=INVALID_STATE_TRANSITION,
                              check="order status unchanged after rejection", transition_id=ntid, step_no=n,
                              expected={**state_exp, "order_status": _json_key(_statuses(before["orders"]))},
                              actual={"order_status": _json_key(_statuses(after["orders"]))}, rule_refs=refs,
                              expected_source=exp.get("http_status_source"), evidence={**evid, "observed_ref": f"steps[{n}].observed_state.orders"}))
        for dom in ("cart", "stock", "orders", "payments"):
            bad = dom in diffs
            out.append(_assertion(f"{pre}.DATA.{dom}", status=FAIL if bad else PASS, category=NEGATIVE_DOMAIN_CATEGORY[dom],
                                  check=f"{dom} unchanged after rejection", transition_id=ntid, step_no=n,
                                  expected=_json_key(diffs[dom][0]) if bad else "không đổi so với trước bước",
                                  actual=_json_key(diffs[dom][1]) if bad else "không đổi", rule_refs=refs,
                                  evidence={**evid, "observed_ref": f"steps[{n}].observed_state", "compared_with": "observed_state của bước liền trước"}))
        return out

    # ----- assertion cấp test case (probe) -----
    def _case_assertions(self, case, result, snapshots) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        probes = {}
        for p in result.get("assertion_probes", []):
            for aid in p.get("assertion_ids", []):
                probes[aid] = p
        final = norm_observed(result.get("final_observed_state"))
        neg_before = None
        if case.get("type") == "negative":
            last = snapshots[-1] if snapshots else None
            neg_before = last["before"] if last else None
        for a in case.get("assertions", []):
            aid = a.get("id", "A?")
            probe = probes.get(aid)
            body = ((probe or {}).get("response") or {}).get("body") if probe and probe.get("execution_status") == "SENT" else None
            refs = list(case.get("security_rule_ids", []))
            ev = {"probe": f"{a.get('probe', {}).get('method')} {a.get('probe', {}).get('path')}", "source": a.get("source"),
                  "probe_status": (probe or {}).get("execution_status")}
            if "expect" in a:
                out.append(self._expect_assertion(case, result, a, aid, body, refs, ev))
            elif "check" in a and a.get("probe_name") in ("cart", "product", "order", "payments"):
                out.append(self._negative_post(a, aid, body, final, neg_before, refs, ev))
            else:
                out.append(_assertion(aid, status=INCONCLUSIVE, category=UNSUPPORTED_ASSERTION, check="test case assertion", transition_id=None,
                                      step_no=None, expected=a.get("expect") or a.get("check"), actual=None, rule_refs=refs, evidence=ev,
                                      reason="loại assertion không được hỗ trợ"))
        return out

    def _expect_assertion(self, case, result, a, aid, body, refs, ev) -> dict[str, Any]:
        expect, path = a["expect"], a.get("probe", {}).get("path", "")
        supported = {"/api/cart": {"items"}, "/api/products": {"stock_by_product_id"}, "/api/payments": {"payments"},
                     "/api/orders/{order_id}": {"status", "total_amount", "items"}}
        if path not in supported or not isinstance(expect, dict) or not set(expect) <= supported[path]:
            return _assertion(aid, status=INCONCLUSIVE, category=UNSUPPORTED_ASSERTION, check="test case assertion", transition_id=None, step_no=None,
                              expected=_json_key(expect), actual=None, rule_refs=refs, evidence=ev, reason="assertion không được hỗ trợ")
        if body is None:
            return _assertion(aid, status=INCONCLUSIVE, category=MISSING_DATA, check=f"probe {path}", transition_id=None, step_no=None,
                              expected=_json_key(expect), actual=None, rule_refs=refs, evidence=ev, reason="probe không được gửi hoặc không có response")
        try:
            if path == "/api/cart":
                want, got = sorted((int(i["product_id"]), int(i["quantity"])) for i in expect["items"]), sorted((int(i["product_id"]), int(i["quantity"])) for i in body["items"])
                cat = RESOURCE_LIFECYCLE_VIOLATION
            elif path == "/api/products":
                want = {str(k): int(v) for k, v in expect["stock_by_product_id"].items()}
                got = {str(p["id"]): int(p["stock"]) for p in body if str(p["id"]) in want}
                cat = STOCK_INTEGRITY_VIOLATION
            elif path == "/api/payments":
                want = sorted((int(resolve_ref(p["order_ref"], result)), p["amount"], p["result"]) for p in expect["payments"])
                got = sorted((int(p["order_id"]), p["amount"], p["result"]) for p in body)
                cat = PAYMENT_LIFECYCLE_VIOLATION
            else:
                want = {"status": expect["status"], "total_amount": expect["total_amount"], "items": _norm_items(expect["items"])}
                got = {"status": body["status"], "total_amount": body["total_amount"], "items": _norm_items(body["items"])}
                cat = INVALID_STATE_TRANSITION if want["status"] != got["status"] else RESOURCE_LIFECYCLE_VIOLATION
        except (KeyError, TypeError, ValueError, Unresolved) as exc:
            return _assertion(aid, status=INCONCLUSIVE, category=MISSING_DATA, check=f"probe {path}", transition_id=None, step_no=None,
                              expected=_json_key(expect), actual=None, rule_refs=refs, evidence=ev, reason=f"không đọc được dữ liệu probe: {exc}")
        return _assertion(aid, status=PASS if want == got else FAIL, category=cat, check=f"probe {path} khớp trạng thái mong đợi",
                          transition_id=None, step_no=None, expected=_json_key(want), actual=_json_key(got), rule_refs=refs, evidence=ev,
                          expected_source=a.get("source"))

    def _negative_post(self, a, aid, body, final, before, refs, ev) -> dict[str, Any]:
        """Hậu điều kiện negative: so với ảnh chụp ngay trước bước negative. Probe thiếu path param -> dùng final_observed_state (cũng từ GET)."""
        name = a["probe_name"]
        if before is None:
            return _assertion(aid, status=INCONCLUSIVE, category=MISSING_DATA, check=a.get("check"), transition_id=None, step_no=None,
                              expected="không đổi", actual=None, rule_refs=refs, evidence=ev, reason="thiếu ảnh chụp trước bước negative")
        actual_from = "assertion_probe"
        try:
            if name == "cart" and body is not None:
                got = sorted((int(i["product_id"]), int(i["quantity"])) for i in body["items"])
            elif name == "payments" and body is not None:
                got = sorted((int(p["order_id"]), p["amount"], p["result"]) for p in body)
            else:
                if final is None:
                    raise KeyError("final_observed_state")
                actual_from = "final_observed_state (probe không gửi được)" if body is None else "final_observed_state"
                got = {"cart": final["cart"], "product": final["stock"], "order": final["orders"], "payments": final["payments"]}[name]
        except (KeyError, TypeError, ValueError):
            return _assertion(aid, status=INCONCLUSIVE, category=MISSING_DATA, check=a.get("check"), transition_id=None, step_no=None,
                              expected="không đổi", actual=None, rule_refs=refs, evidence=ev, reason="không có dữ liệu quan sát để so sánh")
        want = {"cart": before["cart"], "product": before["stock"], "order": before["orders"], "payments": before["payments"]}[name]
        cat = {"cart": UNEXPECTED_SIDE_EFFECT, "product": STOCK_INTEGRITY_VIOLATION, "order": RESOURCE_LIFECYCLE_VIOLATION,
               "payments": PAYMENT_LIFECYCLE_VIOLATION}[name]
        return _assertion(aid, status=PASS if want == got else FAIL, category=cat, check=a.get("check"), transition_id=None, step_no=None,
                          expected=_json_key(want), actual=_json_key(got), rule_refs=refs, expected_source=a.get("source"),
                          evidence={**ev, "actual_from": actual_from})

    # ----- invariant -----
    def _invariants(self, case, result, snapshots) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        nt = case.get("negative_transition_id")
        main_neg = next((a for a in self._neg_summary(case, result) if a), None)
        for ia in case.get("invariant_assertions", []):
            iid = ia.get("invariant_id")
            inv = self.inputs.invariants.get(iid)
            refs = list((inv or {}).get("rule_refs", []))
            if inv is None:
                out.append(_assertion(iid or "INV-?", status=INCONCLUSIVE, category=UNSUPPORTED_ASSERTION, check="invariant", transition_id=None,
                                      step_no=None, expected=None, actual=None, evidence={}, reason="invariant không có trong efsm_model.json"))
                continue
            if iid in STRUCTURAL_INVARIANT_VERIFIERS:
                if nt in STRUCTURAL_INVARIANT_VERIFIERS[iid] and main_neg is not None:
                    out.append(_assertion(iid, status=main_neg["status"], category=INVARIANT_VIOLATION, check=f"{inv['expression']} (kiểm chứng bằng {nt})",
                                          transition_id=nt, step_no=main_neg.get("step_no"), expected=inv["expression"],
                                          actual={"negative_transition_rejected_with_state_unchanged": main_neg["status"] == PASS},
                                          rule_refs=refs, evidence={"verified_by": nt, "derived_from": main_neg["assertion_id"]}))
                continue  # không áp dụng cho test case này: không tính là assertion
            evaluator = getattr(self, "_inv_" + iid.replace("-", "").lower(), None)
            if evaluator is None:
                out.append(_assertion(iid, status=INCONCLUSIVE, category=UNSUPPORTED_ASSERTION, check=inv.get("expression"), transition_id=None,
                                      step_no=None, expected=inv.get("expression"), actual=None, rule_refs=refs, evidence={},
                                      reason="chưa có bộ đánh giá runtime cho invariant này"))
                continue
            if any(s["after"] is None for s in snapshots if s["kind"] != "FIXTURE") or any(s["before"] is None for s in snapshots[:1]):
                out.append(_assertion(iid, status=INCONCLUSIVE, category=MISSING_DATA, check=inv["expression"], transition_id=None, step_no=None,
                                      expected=inv["expression"], actual=None, rule_refs=refs, evidence={}, reason="thiếu observed_state để đánh giá invariant"))
                continue
            violations = evaluator(result, snapshots)
            out.append(_assertion(iid, status=FAIL if violations else PASS, category=INVARIANT_CATEGORY.get(iid, INVARIANT_VIOLATION),
                                  check=inv["expression"], transition_id=None, step_no=violations[0]["step_no"] if violations else None,
                                  expected=inv["expression"], actual={"violations": violations} if violations else "thỏa trên mọi trạng thái quan sát",
                                  rule_refs=refs, evidence={"states_checked": len(snapshots) + 1, "invariant_description": inv.get("description")}))
        return out

    def _neg_summary(self, case, result) -> list[dict[str, Any] | None]:
        """Kết quả tổng hợp của bước negative (REJECTED+HTTP+STATE+DATA) để suy ra invariant cấu trúc."""
        last = next((s for s in reversed(case.get("steps", [])) if s.get("kind") == "negative_transition"), None)
        if last is None:
            return [None]
        snaps = self._snapshots(result)
        idx = next((i for i, s in enumerate(case["steps"]) if s is last), None)
        rstep = next((s for s in result.get("steps", []) if s.get("step_no") == last["step_no"]), None)
        parts = self._negative_step(case, last, rstep, snaps[idx]["before"], snaps[idx]["after"])
        # invariant cấu trúc nói về việc bị từ chối + trạng thái không đổi, không phụ thuộc mã HTTP cụ thể
        statuses = [p["status"] for p in parts if not p["assertion_id"].endswith(("PRECONDITION", ".HTTP"))]
        agg = FAIL if FAIL in statuses else INCONCLUSIVE if INCONCLUSIVE in statuses else PASS
        return [{"status": agg, "assertion_id": f"S{last['step_no']}.*", "step_no": last["step_no"]}]

    @staticmethod
    def _states(snapshots) -> list[tuple[int | None, dict[str, Any]]]:
        states = []
        if snapshots and snapshots[0]["before"] is not None:
            states.append((0, snapshots[0]["before"]))
        for s in snapshots:
            if s["after"] is not None:
                states.append((s["step_no"], s["after"]))
        return states

    def _inv_inv01(self, result, snapshots):
        out = []
        for step_no, st in self._states(snapshots):
            for pid, qty in st["cart"]:
                if qty <= 0:
                    out.append({"step_no": step_no, "product_id": pid, "quantity": qty})
        for s in result.get("steps", []):
            req, resp = s.get("request") or {}, s.get("response") or {}
            body = req.get("body") or {}
            if str(req.get("path_template", "")).startswith(CART_BOUND_PREFIX) and isinstance(body.get("quantity"), int) \
                    and 200 <= (resp.get("status_code") or 0) < 300 and body["quantity"] <= 0:
                out.append({"step_no": s["step_no"], "accepted_quantity": body["quantity"]})
        return out

    def _inv_inv02(self, result, snapshots):
        out = []
        for s in snapshots:
            if s["before"] is None or s["after"] is None:
                continue
            for oid, o in s["after"]["orders"].items():
                if oid in s["before"]["orders"]:
                    continue
                for pid, qty, _ in o["items"]:
                    stock_before = s["before"]["stock"].get(str(pid))
                    if stock_before is not None and qty > stock_before:
                        out.append({"step_no": s["step_no"], "order_id": oid, "product_id": pid, "quantity": qty, "stock_before": stock_before})
        return out

    def _inv_inv03(self, result, snapshots):
        out = []
        for step_no, st in self._states(snapshots):
            for oid, o in st["orders"].items():
                total = sum(q * p for _, q, p in o["items"])
                if o["total"] != total:
                    out.append({"step_no": step_no, "order_id": oid, "total_amount": o["total"], "sum_of_items": total})
        return out

    def _inv_inv04(self, result, snapshots):
        out = []
        for step_no, st in self._states(snapshots):
            for oid, o in st["orders"].items():
                if o["status"] == "PAID" and not any(p[0] == oid and p[2] == "SUCCESS" for p in st["payments"]):
                    out.append({"step_no": step_no, "order_id": oid, "problem": "đơn PAID không có payment SUCCESS"})
        return out

    def _inv_inv05(self, result, snapshots):
        out = []
        for s in snapshots:
            if s["kind"] == "FIXTURE" or s["before"] is None or s["after"] is None:
                continue
            b, a = s["before"], s["after"]
            delta: Counter[str] = Counter()
            involved = False
            for oid, o in a["orders"].items():
                prev = b["orders"].get(oid)
                if o["status"] == "CANCELLED" and (prev is None or prev["status"] != "CANCELLED"):
                    involved = True
                    for pid, qty, _ in o["items"]:
                        delta[str(pid)] += qty
                elif o["status"] == "CANCELLED":
                    involved = True
                elif prev is None:
                    for pid, qty, _ in o["items"]:
                        delta[str(pid)] -= qty
            if not involved:
                continue
            for key in set(a["stock"]) | set(b["stock"]):
                got = a["stock"].get(key, 0) - b["stock"].get(key, 0)
                if got != delta.get(key, 0):
                    out.append({"step_no": s["step_no"], "product_id": key, "expected_stock_delta": delta.get(key, 0), "actual_stock_delta": got})
        return out

    def _inv_inv09(self, result, snapshots):
        out = []
        for step_no, st in self._states(snapshots):
            counts = Counter(p[0] for p in st["payments"] if p[2] == "SUCCESS")
            out.extend({"step_no": step_no, "order_id": oid, "successful_payments": n} for oid, n in counts.items() if n > 1)
        return out

    def _inv_inv11(self, result, snapshots):
        return [{"step_no": step_no, "product_id": k, "stock": v} for step_no, st in self._states(snapshots) for k, v in st["stock"].items() if v < 0]

    def _inv_inv12(self, result, snapshots):
        out = []
        for step_no, st in self._states(snapshots):
            for oid, o in st["orders"].items():
                if o["status"] == "PAID":
                    ok = [p for p in st["payments"] if p[0] == oid and p[2] == "SUCCESS"]
                    if ok and not any(p[1] == o["total"] for p in ok):
                        out.append({"step_no": step_no, "order_id": oid, "total_amount": o["total"], "payment_amounts": [p[1] for p in ok]})
        return out

    # ----- tài liệu đầu ra -----
    def _document(self, results: list[dict[str, Any]], data_errors: list[dict[str, Any]]) -> dict[str, Any]:
        inp = self.inputs
        by_status = Counter(r["validation_status"] for r in results)
        all_a = [a for r in results for a in r["assertions"]]
        fails = Counter(a["category"] for a in all_a if a["status"] == FAIL)
        return {
            "schema_version": SCHEMA_VERSION, "validator": VALIDATOR_NAME,
            "scope": "đối chiếu kết quả thực thi với expected; không gửi lại HTTP; không kết luận lỗ hổng hay mức độ nghiêm trọng (Phần 8)",
            "sources": {"test_cases": inp.hashes["test_cases"], "test_execution_results": inp.hashes["test_execution_results"],
                        "efsm_model": inp.hashes["efsm_model"], "security_rules": inp.hashes["security_rules"]},
            "summary": {
                "total": len(results), "passed": by_status[PASS], "failed": by_status[FAIL], "skipped": by_status[SKIPPED],
                "inconclusive": by_status[INCONCLUSIVE],
                "by_test_type": {t: dict(Counter(r["validation_status"] for r in results if r["test_type"] == t)) for t in ("POSITIVE", "NEGATIVE")},
                "assertions": {"total": len(all_a), "evaluated": sum(1 for a in all_a if a["status"] in (PASS, FAIL)),
                               "passed": sum(1 for a in all_a if a["status"] == PASS), "failed": sum(1 for a in all_a if a["status"] == FAIL),
                               "not_evaluable": sum(1 for a in all_a if a["status"] == INCONCLUSIVE)},
                "failures_by_category": dict(sorted(fails.items())),
                "data_errors": len(data_errors),
                "note": "PASS chỉ khi mọi assertion bắt buộc được đánh giá và thỏa; COMPLETED ở Phần 6 không có nghĩa là PASS.",
            },
            "data_errors": data_errors, "results": results,
        }


MISSING = object()


def _count(assertions: list[dict[str, Any]]) -> dict[str, int]:
    c = Counter(a["status"] for a in assertions)
    return {"total": len(assertions), "passed": c[PASS], "failed": c[FAIL], "not_evaluable": c[INCONCLUSIVE]}


def _req(rstep: dict[str, Any] | None) -> str | None:
    req = (rstep or {}).get("request") or {}
    return f"{req.get('method')} {req.get('url') or req.get('path_template')}" if req else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=VALIDATOR_NAME)
    p.add_argument("--test-cases", default="test_cases.json")
    p.add_argument("--execution-results", default="test_execution_results.json")
    p.add_argument("--model", default="efsm_model.json")
    p.add_argument("--rules", default="security_rules.json")
    p.add_argument("--output", default="test_validation_results.json")
    p.add_argument("--fail-on-fail", action="store_true", help="trả mã thoát 1 khi có test case FAIL")
    args = p.parse_args(argv)
    try:
        inputs = Inputs.from_files(Path(args.test_cases), Path(args.execution_results), Path(args.model), Path(args.rules))
        doc = ResultValidator(inputs).validate_all()
    except ValidatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    Path(args.output).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    s = doc["summary"]
    print(f"total={s['total']} passed={s['passed']} failed={s['failed']} skipped={s['skipped']} inconclusive={s['inconclusive']} "
          f"assertions={s['assertions']}")
    print(f"wrote {args.output}")
    return 1 if args.fail_on_fail and s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
