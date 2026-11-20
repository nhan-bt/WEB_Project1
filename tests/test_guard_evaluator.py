"""Unit test cho GuardEvaluator và efsm_model.json (KHÔNG gọi API, KHÔNG dùng database).

Nhóm A: nạp mô hình      Nhóm B: guard        Nhóm C: transition
Nhóm D: invariant        Nhóm E: kịch bản nghiệp vụ BlueMoon (chỉ đánh giá/mô phỏng)
Nhóm F: mô phỏng (apply) Nhóm G: truy vết với security_rules.json và cấu trúc mô hình

Kiểm chứng hành vi backend thật nằm ở tests/test_efsm_backend_conformance.py.
"""
import ast
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

import guard_evaluator as ge
from guard_evaluator import GuardEvaluator, GuardStatus, InvariantStatus, ModelLoadError

ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "efsm_model.json"
RULES_PATH = ROOT / "security_rules.json"


# --------------------------------------------------------------------------
# Fixtures và dữ liệu mẫu
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ev() -> GuardEvaluator:
    return GuardEvaluator(MODEL_PATH)


@pytest.fixture(scope="module")
def real_model() -> dict:
    return json.loads(MODEL_PATH.read_text(encoding="utf-8"))


def write_model(tmp_path: Path, model: dict, name: str = "model.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(model), encoding="utf-8")
    return path


def mini_model() -> dict:
    """Mô hình tối thiểu dùng cho các test cấu trúc."""
    return {
        "states": [{"id": "A"}, {"id": "B"}],
        "variables": [
            {"name": "x", "type": "integer"},
            {"name": "s", "type": "string", "allowed_values": ["P", "Q"]},
        ],
        "transitions": [
            {
                "id": "T1", "source": "A", "target": "B", "event": "go",
                "guard": {"var": "x", "op": ">", "value": 0},
                "actions": [{"type": "assign", "var": "x", "expr": "x - 1"}],
            }
        ],
        "invariants": [],
        "negative_transitions": [],
    }


def make(tmp_path: Path, model: dict, strict: bool = True) -> GuardEvaluator:
    return GuardEvaluator(write_model(tmp_path, model), strict=strict)


def t01(current_qty, quantity, stock, **extra):
    return {"product_exists": True, "current_qty": current_qty, "quantity": quantity, "stock": stock, **extra}


def t02(quantity, stock, **extra):
    return {"product_exists": True, "cart_item_exists": True, "quantity": quantity, "stock": stock, **extra}


def order_inputs(**overrides):
    base = {
        "cart_items_count": 1, "ordered_quantity": 3, "stock": 10, "payment_method": "MOCK",
        "customer_name_length": 10, "phone_length": 10, "address_length": 12,
        "order_items": [{"quantity": 3, "unit_price": 150000}],
    }
    base.update(overrides)
    return base


# ==========================================================================
# Nhóm A - Model loading
# ==========================================================================
class TestModelLoading:
    def test_real_model_loads_in_strict_mode(self, ev):
        assert ev.model_issues == []
        assert ev.states == ["CART", "PENDING_CONFIRMATION", "CONFIRMED", "PAID", "CANCELLED"]
        assert {"T01", "T02", "T03", "T04", "T05", "T06", "T07", "T08", "T09"} <= set(ev.transition_ids)
        assert len(ev.invariant_ids) == 12

    def test_original_ids_are_preserved(self, ev):
        assert {f"INV-{i:02d}" for i in range(1, 11)} <= set(ev.invariant_ids)
        assert {f"NT{i:02d}" for i in range(1, 13)} <= set(ev.negative_transition_ids)

    def test_missing_file(self, tmp_path):
        with pytest.raises(ModelLoadError, match="not found"):
            GuardEvaluator(tmp_path / "nope.json")

    def test_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ModelLoadError, match="not valid JSON"):
            GuardEvaluator(path)

    @pytest.mark.parametrize("missing_key", ["states", "variables", "transitions"])
    def test_missing_required_section(self, tmp_path, missing_key):
        model = mini_model()
        del model[missing_key]
        with pytest.raises(ModelLoadError, match=missing_key):
            make(tmp_path, model)

    def test_duplicate_transition_id(self, tmp_path):
        model = mini_model()
        model["transitions"].append(copy.deepcopy(model["transitions"][0]))
        with pytest.raises(ModelLoadError, match="Duplicate transition id"):
            make(tmp_path, model)

    def test_duplicate_invariant_id(self, tmp_path):
        model = mini_model()
        inv = {"id": "I1", "scope": "GLOBAL", "check": {"var": "x", "op": ">=", "value": 0}}
        model["invariants"] = [inv, dict(inv)]
        with pytest.raises(ModelLoadError, match="unique"):
            make(tmp_path, model)

    @pytest.mark.parametrize("field", ["source", "target"])
    def test_unknown_state_reference(self, tmp_path, field):
        model = mini_model()
        model["transitions"][0][field] = "NOWHERE"
        with pytest.raises(ModelLoadError, match="unknown"):
            make(tmp_path, model)

    def test_malformed_guard_rejected_in_strict_mode(self, tmp_path):
        model = mini_model()
        model["transitions"][0]["guard"] = {"var": "x", "op": "~~", "value": 1}
        with pytest.raises(ModelLoadError) as exc:
            make(tmp_path, model)
        assert any("T1" in issue and "unsupported operator" in issue for issue in exc.value.issues)

    def test_malformed_guard_in_lenient_mode_is_reported_not_hidden(self, tmp_path):
        model = mini_model()
        model["transitions"][0]["guard"] = {"all": []}
        lenient = make(tmp_path, model, strict=False)
        assert lenient.model_issues
        result = lenient.check_transition("T1", "A", {"x": 1})
        assert result.status is GuardStatus.INVALID_GUARD
        assert result.guard_result is None

    def test_guard_referencing_undeclared_variable(self, tmp_path):
        model = mini_model()
        model["transitions"][0]["guard"] = {"var": "ghost", "op": "==", "value": 1}
        with pytest.raises(ModelLoadError, match="ghost"):
            make(tmp_path, model)

    def test_assign_to_undeclared_variable(self, tmp_path):
        model = mini_model()
        model["transitions"][0]["actions"] = [{"type": "assign", "var": "ghost", "value": 1}]
        with pytest.raises(ModelLoadError, match="not declared"):
            make(tmp_path, model)

    def test_unknown_variable_type(self, tmp_path):
        model = mini_model()
        model["variables"][0]["type"] = "quantum"
        with pytest.raises(ModelLoadError, match="unknown type"):
            make(tmp_path, model)

    def test_invariant_must_be_checkable_or_explicitly_unsupported(self, tmp_path):
        model = mini_model()
        model["invariants"] = [{"id": "I1", "scope": "GLOBAL", "expression": "free text"}]
        with pytest.raises(ModelLoadError, match="machine-readable"):
            make(tmp_path, model)

    def test_unsupported_invariant_requires_reason(self, tmp_path):
        model = mini_model()
        model["invariants"] = [{"id": "I1", "scope": "GLOBAL", "evaluable": False}]
        with pytest.raises(ModelLoadError, match="unsupported_reason"):
            make(tmp_path, model)

    def test_negative_transition_with_unknown_base(self, tmp_path):
        model = mini_model()
        model["negative_transitions"] = [
            {"id": "N1", "source": "A", "event": "go", "base_transition": "T404", "invalid_condition": "x <= 0"}
        ]
        with pytest.raises(ModelLoadError, match="base_transition"):
            make(tmp_path, model)

    def test_model_file_is_never_modified(self, ev):
        before = MODEL_PATH.read_bytes()
        GuardEvaluator(MODEL_PATH).check_invariants({})
        assert MODEL_PATH.read_bytes() == before


# ==========================================================================
# Nhóm B - Guard
# ==========================================================================
class TestGuardEvaluation:
    def test_valid_guard_satisfied(self, ev):
        result = ev.evaluate_guard(ev.get_transition("T02")["guard"], t02(3, 10))
        assert result.status is GuardStatus.GUARD_SATISFIED and result.result is True
        assert result.failed_conditions == []

    def test_guard_not_satisfied_reports_values(self, ev):
        result = ev.evaluate_guard(ev.get_transition("T02")["guard"], t02(12, 10))
        assert result.status is GuardStatus.GUARD_NOT_SATISFIED and result.result is False
        failed = result.failed_conditions[0]
        assert (failed["condition"], failed["actual"], failed["expected"]) == ("quantity <= stock", 12, 10)
        assert "12 <= 10" in result.reason

    @pytest.mark.parametrize(
        "quantity, stock, expected",
        [
            (0, 10, GuardStatus.GUARD_NOT_SATISFIED),    # quantity = 0
            (-1, 10, GuardStatus.GUARD_NOT_SATISFIED),   # quantity < 0
            (1, 10, GuardStatus.GUARD_SATISFIED),
            (10, 10, GuardStatus.GUARD_SATISFIED),       # quantity = stock
            (11, 10, GuardStatus.GUARD_NOT_SATISFIED),   # quantity > stock
            (1, 0, GuardStatus.GUARD_NOT_SATISFIED),     # stock = 0
            (0, 0, GuardStatus.GUARD_NOT_SATISFIED),
        ],
    )
    def test_boundary_values_update_quantity(self, ev, quantity, stock, expected):
        assert ev.check_transition("T02", "CART", t02(quantity, stock)).status is expected

    @pytest.mark.parametrize(
        "current, quantity, stock, expected",
        [
            (0, 10, 10, GuardStatus.GUARD_SATISFIED),
            (5, 5, 10, GuardStatus.GUARD_SATISFIED),    # tổng = stock
            (5, 6, 10, GuardStatus.GUARD_NOT_SATISFIED),
            (0, 1, 0, GuardStatus.GUARD_NOT_SATISFIED),
            (3, 0, 10, GuardStatus.GUARD_NOT_SATISFIED),
        ],
    )
    def test_boundary_values_add_item_expression_guard(self, ev, current, quantity, stock, expected):
        assert ev.check_transition("T01", "CART", t01(current, quantity, stock)).status is expected

    def test_expr_failure_reports_computed_value(self, ev):
        result = ev.check_transition("T01", "CART", t01(8, 5, 10))
        failed = result.failed_conditions[0]
        assert failed["condition"] == "current_qty + quantity <= stock"
        assert (failed["actual"], failed["expected"]) == (13, 10)

    def test_and_reports_every_failed_condition(self, ev):
        variables = order_inputs(ordered_quantity=11, payment_method="PAYPAL", phone_length=3)
        result = ev.check_transition("T04", "CART", variables)
        assert result.status is GuardStatus.GUARD_NOT_SATISFIED
        failed = {f["condition"] for f in result.failed_conditions}
        assert failed == {"ordered_quantity <= stock", 'payment_method in ["MOCK", "COD"]', "phone_length >= 9"}

    def test_or_needs_one_true_condition(self, ev):
        guard = {"any": [{"var": "payment_method", "op": "==", "value": "MOCK"},
                         {"var": "payment_method", "op": "==", "value": "COD"}]}
        assert ev.evaluate_guard(guard, {"payment_method": "COD"}).status is GuardStatus.GUARD_SATISFIED
        bad = ev.evaluate_guard(guard, {"payment_method": "BANK"})
        assert bad.status is GuardStatus.GUARD_NOT_SATISFIED
        assert len(bad.failed_conditions[0]["alternatives"]) == 2

    def test_not_negates(self, ev):
        guard = {"not": {"var": "order_status", "op": "==", "value": "CANCELLED"}}
        assert ev.evaluate_guard(guard, {"order_status": "PAID"}).result is True
        assert ev.evaluate_guard(guard, {"order_status": "CANCELLED"}).result is False

    def test_not_inside_expression(self, ev):
        guard = {"expr": "not (quantity > 5) and quantity != 0"}
        assert ev.evaluate_guard(guard, {"quantity": 3}).result is True
        assert ev.evaluate_guard(guard, {"quantity": 9}).result is False
        assert ev.evaluate_guard(guard, {"quantity": 0}).result is False

    def test_nested_logic(self, ev):
        guard = {"all": [
            {"any": [{"var": "order_status", "op": "==", "value": "PAID"},
                     {"not": {"var": "payment_count", "op": "==", "value": 0}}]},
            {"expr": "payment_count <= 1"},
        ]}
        assert ev.evaluate_guard(guard, {"order_status": "CONFIRMED", "payment_count": 1}).result is True
        assert ev.evaluate_guard(guard, {"order_status": "CONFIRMED", "payment_count": 0}).result is False

    def test_value_var_comparison(self, ev):
        guard = {"var": "quantity", "op": "<=", "value_var": "stock"}
        assert ev.evaluate_guard(guard, {"quantity": 3, "stock": 3}).result is True
        assert ev.evaluate_guard(guard, {"quantity": 4, "stock": 3}).result is False

    def test_in_and_not_in(self, ev):
        in_guard = {"var": "payment_method", "op": "in", "value": ["MOCK", "COD"]}
        assert ev.evaluate_guard(in_guard, {"payment_method": "MOCK"}).result is True
        assert ev.evaluate_guard(in_guard, {"payment_method": "BANK"}).result is False
        not_in = {"var": "payment_method", "op": "not_in", "value": ["BANK"]}
        assert ev.evaluate_guard(not_in, {"payment_method": "MOCK"}).result is True

    def test_string_comparison_is_case_sensitive(self, ev):
        guard = {"var": "payment_result", "op": "==", "value": "SUCCESS"}
        assert ev.evaluate_guard(guard, {"payment_result": "success"}).result is False

    def test_exists_does_not_require_the_variable(self, ev):
        exists = {"var": "payment_method", "op": "exists"}
        assert ev.evaluate_guard(exists, {}).status is GuardStatus.GUARD_NOT_SATISFIED
        assert ev.evaluate_guard(exists, {"payment_method": None}).status is GuardStatus.GUARD_NOT_SATISFIED
        assert ev.evaluate_guard(exists, {"payment_method": "MOCK"}).result is True
        absent = {"var": "payment_method", "op": "exists", "value": False}
        assert ev.evaluate_guard(absent, {}).result is True

    def test_not_empty(self, ev):
        guard = {"var": "payment_method", "op": "not_empty"}
        assert ev.evaluate_guard(guard, {"payment_method": "MOCK"}).result is True
        assert ev.evaluate_guard(guard, {"payment_method": "  "}).result is False
        assert ev.evaluate_guard(guard, {}).status is GuardStatus.MISSING_VARIABLE

    def test_arithmetic_and_decimal(self, ev):
        guard = {"expr": "unit_price * quantity >= 100.5"}
        assert ev.evaluate_guard(guard, {"unit_price": Decimal("33.5"), "quantity": 3}).result is True
        assert ev.evaluate_guard(guard, {"unit_price": 33.4, "quantity": 3}).result is False  # 100.2
        assert ev.evaluate_guard({"expr": "quantity / 2 == 1.5"}, {"quantity": 3}).result is True

    def test_guard_none_means_always_enabled(self, ev):
        assert ev.evaluate_guard(None, {}).status is GuardStatus.GUARD_SATISFIED

    def test_string_guard_is_treated_as_expression(self, ev):
        assert ev.evaluate_guard("quantity > 0", {"quantity": 1}).result is True

    # --- thiếu biến / sai kiểu ------------------------------------------
    def test_missing_variable_is_not_defaulted(self, ev):
        variables = {"product_exists": True, "cart_item_exists": True, "quantity": 3}  # thiếu stock
        result = ev.check_transition("T02", "CART", variables)
        assert result.status is GuardStatus.MISSING_VARIABLE
        assert result.missing_variables == ["stock"]
        assert result.guard_result is None

    def test_missing_variable_wins_even_if_another_condition_is_false(self, ev):
        result = ev.check_transition("T02", "CART", {"product_exists": True, "cart_item_exists": True, "quantity": 0})
        assert result.status is GuardStatus.MISSING_VARIABLE  # không kết luận sớm là NOT_SATISFIED

    @pytest.mark.parametrize("bad", ["3", 3.5, None, True, [3]])
    def test_wrong_type_integer_variable(self, ev, bad):
        result = ev.check_transition("T02", "CART", t02(bad, 10))
        assert result.status is GuardStatus.TYPE_ERROR
        assert result.type_errors[0]["variable"] == "quantity"
        assert result.guard_result is None

    def test_old_status_name_is_an_alias(self):
        assert GuardStatus.INVALID_VARIABLE_TYPE is GuardStatus.TYPE_ERROR

    def test_wrong_type_string_and_boolean_variables(self, ev):
        assert ev.evaluate_guard({"var": "order_status", "op": "==", "value": "PAID"}, {"order_status": 5}).status is GuardStatus.TYPE_ERROR
        assert ev.evaluate_guard({"var": "cart_item_exists", "op": "==", "value": True}, {"cart_item_exists": 1}).status is GuardStatus.TYPE_ERROR

    def test_wrong_type_list_variable(self, ev):
        result = ev.evaluate_guard({"expr": "order_total == SUM_PRODUCT(order_items, 'quantity', 'unit_price')"},
                                   {"order_total": 1, "order_items": "nope"})
        assert result.status is GuardStatus.TYPE_ERROR

    def test_operand_type_mismatch_in_expression(self, ev):
        result = ev.evaluate_guard({"expr": "quantity > 'a'"}, {"quantity": 3})
        assert result.status is GuardStatus.TYPE_ERROR

    # --- biểu thức lỗi / không hỗ trợ -----------------------------------
    @pytest.mark.parametrize(
        "expr",
        ["__import__('os').system('echo hacked')", "open('/tmp/x', 'w')", "quantity ** 2 > 1",
         "stock.real > 1", "[1, 2][0] == 1", "(lambda: 1)() == 1", "SUM(order_items) > 0",
         "quantity > 0 if stock else False", "{k: 1 for k in [1]}"],
    )
    def test_unsupported_expressions_are_rejected(self, ev, expr):
        result = ev.evaluate_guard({"expr": expr}, {"quantity": 1, "stock": 1, "order_items": []})
        assert result.status is GuardStatus.UNSUPPORTED_EXPRESSION
        assert result.result is None

    @pytest.mark.parametrize(
        "guard",
        [
            {"expr": "quantity >"},                                       # sai cú pháp
            {"expr": "unknown_name > 1"},                                 # tên không xác định
            {"expr": ""},
            {"var": "quantity", "op": "~~", "value": 1},                  # toán tử không có
            {"var": "ghost", "op": "==", "value": 1},                     # biến chưa khai báo
            {"var": "quantity", "op": ">"},                               # thiếu value
            {"var": "quantity", "op": ">", "value": 1, "value_var": "stock"},
            {"var": "quantity", "op": ">", "value": "5"},                 # so sánh thứ tự với chuỗi
            {"var": "order_status", "op": ">", "value": 1},               # biến chuỗi dùng '>'
            {"var": "quantity", "op": "in", "value": 5},                  # 'in' cần danh sách
            {"all": []},
            {"all": "oops"},
            {"all": [{"var": "quantity", "op": ">", "value": 0}], "any": []},
            {"foo": "bar"},
            {},
            42,
            {"expr": "quantity > 0", "var": "quantity"},
            {"expr": "SUM_PRODUCT(quantity, 'a', 'b') > 0"},              # đối số đầu không phải list
        ],
    )
    def test_invalid_guards(self, ev, guard):
        result = ev.evaluate_guard(guard, {"quantity": 1, "stock": 1})
        assert result.status is GuardStatus.INVALID_GUARD, result.reason
        assert result.result is None

    def test_division_by_zero(self, ev):
        result = ev.evaluate_guard({"expr": "quantity / stock > 1"}, {"quantity": 3, "stock": 0})
        assert result.status is GuardStatus.EVALUATION_ERROR
        assert "division by zero" in result.reason

    def test_evaluate_guard_rejects_non_mapping_variables(self, ev):
        with pytest.raises(TypeError):
            ev.evaluate_guard({"var": "quantity", "op": ">", "value": 0}, [("quantity", 1)])

    def test_never_satisfied_without_real_evaluation(self, ev):
        """Mọi guard lỗi/thiếu dữ liệu đều không được ra GUARD_SATISFIED."""
        for guard, variables in [({"expr": "foo("}, {}), ({"var": "quantity", "op": ">", "value": 0}, {}),
                                 ({"all": []}, {"quantity": 1}), ({"expr": "x.y"}, {})]:
            assert ev.evaluate_guard(guard, variables).status is not GuardStatus.GUARD_SATISFIED

    def test_module_never_uses_eval_or_exec(self):
        tree = ast.parse((ROOT / "guard_evaluator.py").read_text(encoding="utf-8"))
        called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not called & {"eval", "exec", "compile", "__import__"}

    def test_variables_are_not_mutated(self, ev):
        variables = t02(3, 10)
        snapshot = copy.deepcopy(variables)
        ev.check_transition("T02", "CART", variables)
        assert variables == snapshot


# ==========================================================================
# Nhóm C - Transition
# ==========================================================================
class TestTransitionEvaluation:
    def test_correct_state_and_guard(self, ev):
        result = ev.check_transition("T05", "PENDING_CONFIRMATION", {"order_status": "PENDING_CONFIRMATION"})
        assert result.status is GuardStatus.GUARD_SATISFIED and result.enabled
        assert (result.source_state, result.target_state, result.event) == ("PENDING_CONFIRMATION", "CONFIRMED", "confirm_order")
        assert result.rule_refs == ["BSR-ORDER-05"]

    def test_wrong_state(self, ev):
        result = ev.check_transition("T02", "CONFIRMED", t02(3, 10))
        assert result.status is GuardStatus.STATE_MISMATCH
        assert result.guard_result is None and not result.enabled
        assert result.source_state == "CART"

    def test_unknown_state_is_mismatch_with_explanation(self, ev):
        result = ev.check_transition("T02", "SHIPPED", t02(3, 10))
        assert result.status is GuardStatus.STATE_MISMATCH
        assert "not a state of the model" in result.reason

    def test_transition_not_found(self, ev):
        result = ev.check_transition("T99", "CART", {})
        assert result.status is GuardStatus.TRANSITION_NOT_FOUND
        assert result.source_state is None and result.guard_result is None

    def test_guard_true_but_wrong_state_is_still_a_mismatch(self, ev):
        variables = {"order_status": "CONFIRMED", "payment_result": "SUCCESS"}
        assert ev.evaluate_guard(ev.get_transition("T06")["guard"], variables).result is True
        assert ev.check_transition("T06", "PENDING_CONFIRMATION", variables).status is GuardStatus.STATE_MISMATCH

    def test_right_state_but_guard_false(self, ev):
        result = ev.check_transition("T06", "CONFIRMED", {"order_status": "CONFIRMED", "payment_result": "FAILED"})
        assert result.status is GuardStatus.GUARD_NOT_SATISFIED
        assert result.failed_conditions[0]["condition"] == 'payment_result == "SUCCESS"'

    def test_state_is_checked_before_variables(self, ev):
        # Sai state: không cần (và không được) báo thiếu biến
        assert ev.check_transition("T06", "CART", {}).status is GuardStatus.STATE_MISMATCH

    def test_t06_and_t07_split_on_payment_result(self, ev):
        base = {"order_status": "CONFIRMED"}
        ok = ev.get_enabled_transitions("CONFIRMED", {**base, "payment_result": "SUCCESS"})
        assert {r.transition_id for r in ok} == {"T06", "T09"}
        failed = ev.get_enabled_transitions("CONFIRMED", {**base, "payment_result": "FAILED"})
        assert {r.transition_id for r in failed} == {"T07", "T09"}

    def test_outgoing_transitions(self, ev):
        assert ev.get_outgoing_transitions("CART") == ["T01", "T02", "T03", "T10", "T04"]
        assert ev.get_outgoing_transitions("PAID") == []
        assert ev.get_outgoing_transitions("CANCELLED") == []

    def test_check_outgoing_reports_missing_data_per_transition(self, ev):
        results = {r.transition_id: r for r in ev.check_outgoing_transitions("CONFIRMED", {"order_status": "CONFIRMED"})}
        assert results["T09"].status is GuardStatus.GUARD_SATISFIED
        assert results["T06"].status is GuardStatus.MISSING_VARIABLE

    def test_result_dict_and_json_are_serialisable(self, ev):
        result = ev.check_transition("T02", "CART", t02(12, 10))
        data = json.loads(result.to_json())
        assert data["status"] == "GUARD_NOT_SATISFIED" and data["guard_result"] is False
        assert set(data) >= {"transition_id", "current_state", "source_state", "guard_result", "status",
                             "reason", "failed_conditions"}

    def test_get_transition_returns_copy(self, ev):
        copy_of = ev.get_transition("T01")
        copy_of["guard"] = None
        assert ev.get_transition("T01")["guard"] is not None
        assert ev.get_transition("T404") is None

    def test_transition_without_guard_is_enabled(self, ev):
        assert ev.check_transition("T10", "CART", {}).status is GuardStatus.GUARD_SATISFIED

    def test_negative_transitions(self, ev):
        applicable = ev.check_negative_transition("NT12", "CONFIRMED", {"payment_result": "PENDING"})
        assert applicable.status is GuardStatus.NEGATIVE_APPLICABLE
        assert ev.check_negative_transition("NT12", "CONFIRMED", {"payment_result": "SUCCESS"}).status is GuardStatus.NEGATIVE_NOT_APPLICABLE
        assert ev.check_negative_transition("NT12", "CART", {"payment_result": "X"}).status is GuardStatus.STATE_MISMATCH
        assert ev.check_negative_transition("NT99", "CART", {}).status is GuardStatus.TRANSITION_NOT_FOUND
        assert ev.check_negative_transition("NT12", "CONFIRMED", {}).status is GuardStatus.MISSING_VARIABLE


# ==========================================================================
# Nhóm D - Invariant
# ==========================================================================
def by_id(report, inv_id):
    return next(r for r in report.results if r.invariant_id == inv_id)


class TestInvariants:
    def test_invariant_satisfied(self, ev):
        report = ev.check_invariants({"quantity": 3}, ["INV-01"], current_state="CART")
        result = by_id(report, "INV-01")
        assert result.status is InvariantStatus.SATISFIED and report.ok
        assert result.variables_used == {"quantity": 3}

    def test_invariant_violated_has_debug_data(self, ev):
        report = ev.check_invariants({"quantity": 0}, ["INV-01"], current_state="CART")
        result = by_id(report, "INV-01")
        assert result.status is InvariantStatus.VIOLATED and not report.ok
        assert result.failed_conditions[0]["actual"] == 0
        assert report.violations == [result]

    def test_invariant_out_of_scope_state_is_skipped(self, ev):
        result = by_id(ev.check_invariants({"quantity": 0}, ["INV-01"], current_state="PAID"), "INV-01")
        assert result.status is InvariantStatus.SKIPPED
        assert result.reason_code == "NOT_APPLICABLE_STATE"

    def test_invariant_precondition_not_met_is_skipped(self, ev):
        variables = {"order_status": "CONFIRMED", "payment_count": 0}
        result = by_id(ev.check_invariants(variables, ["INV-04"]), "INV-04")  # chỉ áp dụng khi PAID
        assert (result.status, result.reason_code) == (InvariantStatus.SKIPPED, "NOT_APPLICABLE_CONDITION")

    def test_paid_order_without_successful_payment_violates_inv04(self, ev):
        variables = {"order_status": "PAID", "payment_count": 0}
        result = by_id(ev.check_invariants(variables, ["INV-04"], current_state="PAID"), "INV-04")
        assert result.status is InvariantStatus.VIOLATED

    def test_missing_data_is_skipped_never_violated(self, ev):
        report = ev.check_invariants({"ordered_quantity": 5}, ["INV-02"], current_state="CART")  # thiếu stock_before
        result = by_id(report, "INV-02")
        assert result.status is InvariantStatus.SKIPPED and result.reason_code == "MISSING_VARIABLE"
        assert result.missing_variables == ["stock_before"]
        assert report.ok and not report.fully_evaluated

    def test_missing_variable_in_precondition_is_skipped(self, ev):
        result = by_id(ev.check_invariants({}, ["INV-05"]), "INV-05")
        assert (result.status, result.reason_code) == (InvariantStatus.SKIPPED, "MISSING_VARIABLE")
        assert result.missing_variables == ["order_status"]

    def test_wrong_type_is_error_not_violation(self, ev):
        result = by_id(ev.check_invariants({"quantity": "many"}, ["INV-01"], current_state="CART"), "INV-01")
        assert (result.status, result.reason_code) == (InvariantStatus.ERROR, "TYPE_ERROR")

    def test_derived_variable_total(self, ev):
        items = [{"quantity": 2, "unit_price": 150000}, {"quantity": 1, "unit_price": 350000}]
        good = by_id(ev.check_invariants({"order_total": 650000, "order_items": items}, ["INV-03"], current_state="CONFIRMED"), "INV-03")
        bad = by_id(ev.check_invariants({"order_total": 100, "order_items": items}, ["INV-03"], current_state="CONFIRMED"), "INV-03")
        assert good.status is InvariantStatus.SATISFIED
        assert bad.status is InvariantStatus.VIOLATED
        assert bad.failed_conditions[0]["actual"] == 100 and bad.failed_conditions[0]["expected"] == 650000

    def test_derived_variable_missing_items(self, ev):
        result = by_id(ev.check_invariants({"order_total": 10}, ["INV-03"], current_state="PAID"), "INV-03")
        assert result.status is InvariantStatus.SKIPPED and result.missing_variables == ["order_items"]

    def test_malformed_order_item_is_error(self, ev):
        variables = {"order_total": 0, "order_items": [{"quantity": 1}]}
        result = by_id(ev.check_invariants(variables, ["INV-03"], current_state="PAID"), "INV-03")
        assert result.status is InvariantStatus.ERROR

    def test_cancelled_order_must_restore_stock_exactly_once(self, ev):
        for count, expected in [(1, InvariantStatus.SATISFIED), (0, InvariantStatus.VIOLATED), (2, InvariantStatus.VIOLATED)]:
            variables = {"order_status": "CANCELLED", "stock_restoration_count": count}
            assert by_id(ev.check_invariants(variables, ["INV-05"], current_state="CANCELLED"), "INV-05").status is expected

    def test_payment_amount_must_equal_order_total(self, ev):
        base = {"order_status": "PAID", "order_total": 450000}
        assert by_id(ev.check_invariants({**base, "payment_amount": 450000}, ["INV-12"]), "INV-12").status is InvariantStatus.SATISFIED
        assert by_id(ev.check_invariants({**base, "payment_amount": 1}, ["INV-12"]), "INV-12").status is InvariantStatus.VIOLATED

    def test_non_negative_stock_and_single_payment(self, ev):
        assert by_id(ev.check_invariants({"stock_after": -1}, ["INV-11"]), "INV-11").status is InvariantStatus.VIOLATED
        assert by_id(ev.check_invariants({"payment_count": 2}, ["INV-09"]), "INV-09").status is InvariantStatus.VIOLATED
        assert by_id(ev.check_invariants({"payment_count": 1}, ["INV-09"]), "INV-09").status is InvariantStatus.SATISFIED

    def test_structural_invariants_hold_on_real_model(self, ev):
        report = ev.check_invariants({}, ["INV-06", "INV-07", "INV-08", "INV-10"])
        assert [r.status for r in report.results] == [InvariantStatus.SATISFIED] * 4

    def test_structural_invariant_detects_a_model_that_breaks_it(self, tmp_path, real_model):
        broken = copy.deepcopy(real_model)
        broken["transitions"].append({"id": "T90", "source": "PAID", "event": "cancel_order", "target": "CANCELLED", "guard": None})
        broken["transitions"].append({"id": "T91", "source": "CONFIRMED", "event": "confirm_order", "target": "CONFIRMED", "guard": None})
        report = make(tmp_path, broken).check_invariants({})
        assert by_id(report, "INV-06").status is InvariantStatus.VIOLATED
        assert by_id(report, "INV-06").failed_conditions[0]["offending_transitions"] == ["T90"]
        assert by_id(report, "INV-07").status is InvariantStatus.VIOLATED
        assert by_id(report, "INV-10").status is InvariantStatus.SATISFIED

    def test_unsupported_invariant_is_never_reported_satisfied(self, tmp_path):
        model = mini_model()
        model["invariants"] = [{"id": "U1", "scope": "ORDER", "expression": "needs a database join",
                                "evaluable": False, "unsupported_reason": "requires a DB query", "covered_by": ["T1"]}]
        report = make(tmp_path, model).check_invariants({"x": 1})
        assert report.results[0].status is InvariantStatus.UNSUPPORTED
        assert report.results[0].reason == "requires a DB query"
        assert report.ok and not report.fully_evaluated
        assert report.unsupported == report.results

    def test_legacy_free_text_invariant_is_unsupported_in_lenient_mode(self, tmp_path):
        model = mini_model()
        model["invariants"] = [{"id": "L1", "scope": "GLOBAL", "expression": "order_status == PAID => exists payment(...)"}]
        evaluator = make(tmp_path, model, strict=False)
        assert evaluator.model_issues
        assert evaluator.check_invariants({}).results[0].status is InvariantStatus.UNSUPPORTED

    def test_unsupported_construct_inside_check_is_unsupported(self, tmp_path):
        model = mini_model()
        model["invariants"] = [{"id": "U2", "scope": "GLOBAL", "check": {"expr": "x > 0"}}]
        evaluator = make(tmp_path, model)
        evaluator._invariants[0]["check"] = {"expr": "len(x) > 0"}  # mô phỏng mô hình viết sai sau khi nạp
        assert evaluator.check_invariants({"x": 1}).results[0].status is InvariantStatus.UNSUPPORTED

    def test_evaluation_error_is_error(self, tmp_path):
        model = mini_model()
        model["invariants"] = [{"id": "E1", "scope": "GLOBAL", "check": {"expr": "10 / x > 1"}}]
        result = make(tmp_path, model).check_invariants({"x": 0}).results[0]
        assert (result.status, result.reason_code) == (InvariantStatus.ERROR, "EVALUATION_ERROR")

    def test_filters_and_summary(self, ev):
        variables = {"quantity": 1}
        scoped = ev.check_invariants(variables, scopes=["CART"])
        assert [r.invariant_id for r in scoped.results] == ["INV-01"]
        full = ev.check_invariants(variables, current_state="CART")
        assert sum(full.summary().values()) == len(ev.invariant_ids)
        assert full.to_dict()["summary"]["SATISFIED"] >= 1
        assert json.loads(full.to_json())["results"]

    def test_empty_snapshot_does_not_pass_silently(self, ev):
        report = ev.check_invariants({})
        assert not report.fully_evaluated
        assert by_id(report, "INV-01").status is InvariantStatus.SKIPPED
        assert not any(r.status is InvariantStatus.VIOLATED for r in report.results)

    def test_consistent_paid_snapshot(self, ev):
        snapshot = {
            "order_status": "PAID", "order_total": 450000, "payment_amount": 450000, "payment_count": 1,
            "order_items": [{"quantity": 3, "unit_price": 150000}], "ordered_quantity": 3,
            "stock_before": 10, "stock_after": 7, "stock_restoration_count": 0,
        }
        report = ev.check_invariants(snapshot, current_state="PAID")
        assert report.ok and not report.violations and not report.errors
        assert by_id(report, "INV-04").status is InvariantStatus.SATISFIED


# ==========================================================================
# Nhóm E - Kịch bản nghiệp vụ BlueMoon (chỉ evaluator, không API)
# ==========================================================================
class TestBlueMoonBusinessScenarios:
    def test_add_valid_item(self, ev):
        assert ev.check_transition("T01", "CART", t01(0, 3, 10)).enabled

    def test_add_item_exceeding_stock(self, ev):
        result = ev.check_transition("T01", "CART", t01(8, 5, 10))
        assert result.status is GuardStatus.GUARD_NOT_SATISFIED
        assert ev.check_negative_transition("NT02", "CART", t01(8, 5, 10)).status is GuardStatus.NEGATIVE_APPLICABLE

    def test_add_item_non_positive_quantity(self, ev):
        assert ev.check_negative_transition("NT01", "CART", {"quantity": 0}).status is GuardStatus.NEGATIVE_APPLICABLE
        assert ev.check_negative_transition("NT01", "CART", {"quantity": -2}).status is GuardStatus.NEGATIVE_APPLICABLE
        assert ev.check_negative_transition("NT01", "CART", {"quantity": 1}).status is GuardStatus.NEGATIVE_NOT_APPLICABLE

    def test_add_item_for_unknown_product(self, ev):
        variables = t01(0, 1, 10, product_exists=False)
        assert ev.check_transition("T01", "CART", variables).status is GuardStatus.GUARD_NOT_SATISFIED
        assert ev.check_negative_transition("NT19", "CART", variables).status is GuardStatus.NEGATIVE_APPLICABLE

    def test_update_cart(self, ev):
        assert ev.check_transition("T02", "CART", t02(4, 10)).enabled
        missing_item = t02(4, 10, cart_item_exists=False)
        assert ev.check_transition("T02", "CART", missing_item).status is GuardStatus.GUARD_NOT_SATISFIED
        assert ev.check_negative_transition("NT15", "CART", missing_item).status is GuardStatus.NEGATIVE_APPLICABLE
        assert ev.check_negative_transition("NT16", "CART", t02(11, 10)).status is GuardStatus.NEGATIVE_APPLICABLE

    def test_remove_item(self, ev):
        assert ev.check_transition("T03", "CART", {"cart_item_exists": True}).enabled
        assert ev.check_negative_transition("NT18", "CART", {"cart_item_exists": False}).status is GuardStatus.NEGATIVE_APPLICABLE

    def test_create_order_with_empty_cart(self, ev):
        variables = order_inputs(cart_items_count=0)
        result = ev.check_transition("T04", "CART", variables)
        assert result.status is GuardStatus.GUARD_NOT_SATISFIED
        assert [f["condition"] for f in result.failed_conditions] == ["cart_items_count > 0"]
        assert ev.check_negative_transition("NT03", "CART", variables).status is GuardStatus.NEGATIVE_APPLICABLE

    def test_create_order_validates_payment_method_and_customer_fields(self, ev):
        assert ev.check_transition("T04", "CART", order_inputs()).enabled
        assert ev.check_negative_transition("NT13", "CART", order_inputs(payment_method="PAYPAL")).status is GuardStatus.NEGATIVE_APPLICABLE
        too_short = order_inputs(customer_name_length=1)
        assert ev.check_transition("T04", "CART", too_short).status is GuardStatus.GUARD_NOT_SATISFIED
        assert ev.check_negative_transition("NT14", "CART", too_short).status is GuardStatus.NEGATIVE_APPLICABLE
        assert ev.check_negative_transition("NT14", "CART", order_inputs()).status is GuardStatus.NEGATIVE_NOT_APPLICABLE

    def test_create_order_over_stock(self, ev):
        variables = order_inputs(ordered_quantity=11)
        assert ev.check_transition("T04", "CART", variables).status is GuardStatus.GUARD_NOT_SATISFIED
        assert ev.check_negative_transition("NT04", "CART", variables).status is GuardStatus.NEGATIVE_APPLICABLE

    def test_confirm_valid_order(self, ev):
        assert ev.check_transition("T05", "PENDING_CONFIRMATION", {"order_status": "PENDING_CONFIRMATION"}).enabled

    def test_confirm_twice_and_confirm_paid_are_rejected(self, ev):
        assert ev.check_transition("T05", "CONFIRMED", {"order_status": "CONFIRMED"}).status is GuardStatus.STATE_MISMATCH
        assert ev.check_negative_transition("NT05", "CONFIRMED", {"order_status": "CONFIRMED"}).status is GuardStatus.NEGATIVE_APPLICABLE
        assert ev.check_negative_transition("NT20", "PAID", {"order_status": "PAID"}).status is GuardStatus.NEGATIVE_APPLICABLE

    def test_pay_before_confirm(self, ev):
        variables = {"order_status": "PENDING_CONFIRMATION", "payment_result": "SUCCESS"}
        assert ev.check_transition("T06", "PENDING_CONFIRMATION", variables).status is GuardStatus.STATE_MISMATCH
        assert ev.check_negative_transition("NT06", "PENDING_CONFIRMATION", variables).status is GuardStatus.NEGATIVE_APPLICABLE

    def test_pay_twice(self, ev):
        variables = {"order_status": "PAID", "payment_result": "SUCCESS"}
        assert ev.check_transition("T06", "PAID", variables).status is GuardStatus.STATE_MISMATCH
        assert ev.check_negative_transition("NT07", "PAID", variables).status is GuardStatus.NEGATIVE_APPLICABLE

    def test_cancel_paid_order_is_impossible(self, ev):
        for tid in ("T08", "T09"):
            assert ev.check_transition(tid, "PAID", {"order_status": "PAID"}).status is GuardStatus.STATE_MISMATCH
        # Ngay cả khi ép state, guard vẫn từ chối vì order_status == PAID
        guard = ev.get_transition("T09")["guard"]
        assert ev.evaluate_guard(guard, {"order_status": "PAID"}).status is GuardStatus.GUARD_NOT_SATISFIED
        assert ev.check_negative_transition("NT08", "PAID", {"order_status": "PAID"}).status is GuardStatus.NEGATIVE_APPLICABLE

    def test_pay_or_cancel_a_cancelled_order(self, ev):
        variables = {"order_status": "CANCELLED", "payment_result": "SUCCESS"}
        assert ev.check_transition("T06", "CANCELLED", variables).status is GuardStatus.STATE_MISMATCH
        assert ev.check_negative_transition("NT11", "CANCELLED", variables).status is GuardStatus.NEGATIVE_APPLICABLE
        assert ev.check_negative_transition("NT09", "CANCELLED", variables).status is GuardStatus.NEGATIVE_APPLICABLE
        assert ev.check_negative_transition("NT10", "CANCELLED", variables).status is GuardStatus.NEGATIVE_APPLICABLE

    def test_invalid_payment_result_in_confirmed_order(self, ev):
        variables = {"order_status": "CONFIRMED", "payment_result": "MAYBE"}
        assert ev.check_negative_transition("NT12", "CONFIRMED", variables).status is GuardStatus.NEGATIVE_APPLICABLE
        assert not ev.get_enabled_transitions("CONFIRMED", variables) or all(
            r.transition_id == "T09" for r in ev.get_enabled_transitions("CONFIRMED", variables))

    def test_stock_after_cancel_is_restored(self, ev):
        created = ev.apply_transition("T04", "CART", order_inputs(stock=10, ordered_quantity=3))
        assert created.new_variables["stock"] == 7
        cancelled = ev.apply_transition(
            "T08", "PENDING_CONFIRMATION", created.new_variables, verify_invariants=True)
        assert cancelled.applied and cancelled.new_state == "CANCELLED"
        assert cancelled.new_variables["stock"] == 10
        assert cancelled.new_variables["stock_restoration_count"] == 1
        assert cancelled.new_variables["cancel_count"] == 1
        report = cancelled.invariant_report
        assert by_id(report, "INV-05").status is InvariantStatus.SATISFIED
        assert by_id(report, "INV-11").status is InvariantStatus.SATISFIED
        assert report.ok

    def test_order_total_from_database_prices(self, ev):
        items = [{"quantity": 2, "unit_price": 150000}, {"quantity": 1, "unit_price": 350000}]
        created = ev.apply_transition("T04", "CART", order_inputs(order_items=items, ordered_quantity=2))
        assert created.new_variables["order_total"] == 650000
        assert by_id(ev.check_invariants(created.new_variables, ["INV-03"], current_state="PENDING_CONFIRMATION"), "INV-03").status is InvariantStatus.SATISFIED

    def test_full_happy_path_keeps_every_invariant_valid(self, ev):
        state, variables = "CART", {**t01(0, 3, 10), "cart_items_count": 0, "cart_item_exists": False}
        step = ev.apply_transition("T01", state, variables, verify_invariants=True)
        assert step.applied and step.new_variables["current_qty"] == 3 and step.new_variables["cart_items_count"] == 1

        variables = {**step.new_variables, "ordered_quantity": 3, "payment_method": "COD", "customer_name_length": 8,
                     "phone_length": 10, "address_length": 20, "order_items": [{"quantity": 3, "unit_price": 150000}]}
        for tid, extra in (("T04", {}), ("T05", {}), ("T06", {"payment_result": "SUCCESS"})):
            step = ev.apply_transition(tid, state, {**variables, **extra}, verify_invariants=True)
            assert step.applied, step.error
            assert step.invariant_report.ok, step.invariant_report.violations
            state, variables = step.new_state, step.new_variables
        assert state == "PAID"
        assert variables["order_status"] == "PAID" and variables["payment_count"] == 1
        assert variables["payment_amount"] == variables["order_total"] == 450000

    def test_payment_failure_then_retry(self, ev):
        variables = {"order_status": "CONFIRMED", "payment_result": "FAILED", "order_total": 100, "payment_count": 0}
        failed = ev.apply_transition("T07", "CONFIRMED", variables)
        assert failed.applied and failed.new_state == "CONFIRMED"
        assert failed.new_variables["order_status"] == "CONFIRMED" and failed.new_variables["payment_count"] == 0
        assert failed.backend_effects
        retry = ev.apply_transition("T06", "CONFIRMED", {**failed.new_variables, "payment_result": "SUCCESS"})
        assert retry.applied and retry.new_state == "PAID"


# ==========================================================================
# Nhóm F - Mô phỏng (apply_transition)
# ==========================================================================
class TestApplyTransition:
    def test_input_variables_are_not_mutated(self, ev):
        variables = order_inputs()
        snapshot = copy.deepcopy(variables)
        outcome = ev.apply_transition("T04", "CART", variables)
        assert outcome.applied and variables == snapshot
        assert outcome.new_variables is not variables

    def test_simulation_is_flagged_and_effects_are_not_executed(self, ev):
        outcome = ev.apply_transition("T04", "CART", order_inputs())
        assert outcome.simulated is True
        assert len(outcome.backend_effects) == 3
        assert all("cart_items" in e or "orders" in e or "products" in e for e in outcome.backend_effects)
        assert json.loads(outcome.to_json())["simulated"] is True

    def test_rejected_transition_changes_nothing(self, ev):
        variables = t02(12, 10)
        outcome = ev.apply_transition("T02", "CART", variables)
        assert not outcome.applied and outcome.new_state == "CART" and outcome.new_variables == variables
        assert outcome.error_status is GuardStatus.GUARD_NOT_SATISFIED

    def test_wrong_state_changes_nothing(self, ev):
        outcome = ev.apply_transition("T05", "CART", {"order_status": "PENDING_CONFIRMATION"})
        assert not outcome.applied and outcome.error_status is GuardStatus.STATE_MISMATCH

    def test_missing_action_input_is_atomic_failure(self, ev):
        variables = {"order_status": "CONFIRMED", "payment_result": "SUCCESS", "payment_count": 0}  # thiếu order_total
        outcome = ev.apply_transition("T06", "CONFIRMED", variables)
        assert not outcome.applied
        assert outcome.error_status is GuardStatus.MISSING_VARIABLE
        assert outcome.missing_variables == ["order_total"]
        assert outcome.new_variables == variables  # order_status vẫn CONFIRMED, không áp dụng nửa chừng

    def test_conditional_assignment_only_runs_when_condition_holds(self, ev):
        new_line = ev.apply_transition("T01", "CART", t01(0, 2, 10, cart_items_count=0))
        assert new_line.new_variables["cart_items_count"] == 1
        existing = ev.apply_transition("T01", "CART", t01(3, 2, 10))  # không cần cart_items_count
        assert existing.applied and "cart_items_count" not in existing.new_variables
        assert existing.skipped_actions[0]["code"] == "WHEN_FALSE"
        assert existing.new_variables["current_qty"] == 5

    def test_remove_and_clear_cart(self, ev):
        removed = ev.apply_transition("T03", "CART", {"cart_item_exists": True, "cart_items_count": 2, "current_qty": 4})
        assert removed.new_variables == {"cart_item_exists": False, "cart_items_count": 1, "current_qty": 0}
        cleared = ev.apply_transition("T10", "CART", {})
        assert cleared.applied and cleared.new_variables["cart_items_count"] == 0

    def test_sync_order_status_option(self, tmp_path):
        model = mini_model()
        model["variables"].append({"name": "order_status", "type": "string", "allowed_values": ["B"]})
        model["transitions"][0]["actions"] = []
        evaluator = make(tmp_path, model)
        plain = evaluator.apply_transition("T1", "A", {"x": 1})
        assert any("order_status was not set" in w for w in plain.warnings) and "order_status" not in plain.new_variables
        synced = evaluator.apply_transition("T1", "A", {"x": 1}, sync_order_status=True)
        assert synced.new_variables["order_status"] == "B"

    def test_legacy_string_actions_are_supported(self, tmp_path):
        model = mini_model()
        model["transitions"][0]["actions"] = ["x := x + 10", "delete the cart", "x := SUM(unknown)"]
        outcome = make(tmp_path, model).apply_transition("T1", "A", {"x": 1})
        assert outcome.new_variables["x"] == 11
        assert outcome.executed_actions == ["x := x + 10"]
        assert outcome.backend_effects == ["delete the cart", "x := SUM(unknown)"]

    def test_assignment_type_check(self, tmp_path):
        model = mini_model()
        model["transitions"][0]["actions"] = [{"type": "assign", "var": "x", "value": "text"}]
        outcome = make(tmp_path, model).apply_transition("T1", "A", {"x": 1})
        assert not outcome.applied and outcome.error_status is GuardStatus.TYPE_ERROR

    def test_division_by_zero_in_action(self, tmp_path):
        model = mini_model()
        model["transitions"][0]["actions"] = [{"type": "assign", "var": "x", "expr": "10 / (x - 1)"}]
        outcome = make(tmp_path, model).apply_transition("T1", "A", {"x": 1})
        assert not outcome.applied and outcome.error_status is GuardStatus.EVALUATION_ERROR

    def test_verify_invariants_reports_violation_without_hiding_it(self, tmp_path):
        model = mini_model()
        model["invariants"] = [{"id": "I1", "scope": "GLOBAL", "check": {"var": "x", "op": ">=", "value": 0}}]
        outcome = make(tmp_path, model).apply_transition("T1", "A", {"x": 1}, verify_invariants=True)
        assert outcome.applied and outcome.new_variables["x"] == 0 and outcome.invariant_report.ok
        model["transitions"][0]["actions"] = [{"type": "assign", "var": "x", "value": -5}]
        bad = make(tmp_path, model).apply_transition("T1", "A", {"x": 1}, verify_invariants=True)
        assert bad.applied and not bad.invariant_report.ok

    def test_transition_variable_metadata_matches_model_file(self, ev, real_model):
        for transition in real_model["transitions"]:
            assert transition["variables"] == ev.get_transition_variables(transition["id"]), transition["id"]

    def test_transition_variables_expose_required_inputs(self, ev):
        info = ev.get_transition_variables("T04")
        assert {"order_items", "ordered_quantity", "stock"} <= set(info["required_inputs"])
        assert "cart_items_count" in ev.get_transition_variables("T01")["conditional_action_reads"]
        with pytest.raises(KeyError):
            ev.get_transition_variables("T404")


# ==========================================================================
# Nhóm G - Truy vết và cấu trúc mô hình
# ==========================================================================
@pytest.fixture(scope="module")
def rules() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


class TestTraceabilityAndStructure:
    def test_all_rule_refs_exist_in_security_rules(self, real_model, rules):
        known = {r["rule_id"] for r in rules["rules"]}
        for section in ("transitions", "negative_transitions", "invariants"):
            for item in real_model[section]:
                unknown = set(item.get("rule_refs", [])) - known
                assert not unknown, f"{item['id']} references unknown rule(s) {unknown}"

    def test_transition_security_event_matches_rule_event(self, real_model, rules):
        events = {r["rule_id"]: ([r["event"]] if isinstance(r["event"], str) else r["event"]) for r in rules["rules"]}
        for transition in real_model["transitions"]:
            for ref in transition["rule_refs"]:
                assert transition["security_event"] in events[ref], f"{transition['id']} -> {ref}"

    def test_every_implemented_rule_is_modelled(self, real_model, rules):
        referenced = set()
        for section in ("transitions", "negative_transitions", "invariants"):
            for item in real_model[section]:
                referenced |= set(item.get("rule_refs", []))
        implemented = {r["rule_id"] for r in rules["rules"] if r["status"] == "IMPLEMENTED"}
        assert implemented - referenced == set()

    def test_excluded_rules_are_declared_not_modelled(self, real_model, rules):
        excluded = {r["rule_id"] for r in rules["excluded_rules"]}
        assert set(real_model["traceability"]["excluded_rules"]) == excluded

    def test_mutant_refs_exist_in_security_rules(self, real_model, rules):
        known = {m for r in rules["rules"] for m in r["mutant_ids"]}
        for negative in real_model["negative_transitions"]:
            assert set(negative["mutant_refs"]) <= known, negative["id"]

    def test_every_negative_transition_targets_a_valid_base_transition(self, ev, real_model):
        for negative in real_model["negative_transitions"]:
            base = ev.get_transition(negative["base_transition"])
            assert base is not None and base["event"] == negative["event"], negative["id"]
            assert negative["expected"]["accepted"] is False and negative["expected"]["http_status"] in (400, 404, 409, 422)

    def test_terminal_states_have_no_outgoing_transitions(self, real_model, ev):
        for state in real_model["states"]:
            if state["type"] == "terminal":
                assert ev.get_outgoing_transitions(state["id"]) == [], state["id"]

    def test_every_state_is_reachable_from_the_initial_state(self, real_model):
        reachable, frontier = {real_model["initial_state"]}, [real_model["initial_state"]]
        while frontier:
            current = frontier.pop()
            for t in real_model["transitions"]:
                if t["source"] == current and t["target"] not in reachable:
                    reachable.add(t["target"])
                    frontier.append(t["target"])
        assert reachable == {s["id"] for s in real_model["states"]}

    def test_states_declare_entity_and_lifecycle(self, real_model):
        by_id_ = {s["id"]: s for s in real_model["states"]}
        assert by_id_["CART"]["entity_type"] == "cart" and by_id_["CART"]["lifecycle"] == "cart"
        for sid in ("PENDING_CONFIRMATION", "CONFIRMED", "PAID", "CANCELLED"):
            assert by_id_[sid]["entity_type"] == "order" and by_id_[sid]["order_status_value"] == sid

    def test_every_transition_has_api_mapping_and_variables(self, real_model):
        for t in real_model["transitions"]:
            assert t["api"]["method"] in {"GET", "POST", "PATCH", "DELETE"} and t["api"]["path"].startswith("/api/")
            assert "required_inputs" in t["variables"] and isinstance(t["expected_http_status"], int)

    def test_every_invariant_has_scope(self, real_model):
        for inv in real_model["invariants"]:
            assert inv["scope"] in {"CART", "ORDER", "PAYMENT", "GLOBAL"}, inv["id"]

    def test_money_invariant_not_weakened(self, real_model):
        """INV-03 phải còn nói về TỔNG của quantity * unit_price, không chỉ một dòng."""
        inv = next(i for i in real_model["invariants"] if i["id"] == "INV-03")
        assert "SUM_PRODUCT" in inv["check"]["expr"] and "SUM" in inv["expression"]
