"""Unit test cho test_data_generator.py (phần lõi sinh dữ liệu mức guard).

Các nhóm đầu dùng một EFSM nhỏ tự dựng, độc lập với BlueMoon; phần cuối chạy trên efsm_model.json thật.
Không gọi API, không dùng database.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from guard_evaluator import GuardEvaluation, GuardEvaluator, GuardStatus
from test_data_generator import (
    OFF_DOMAIN_VALUE,
    DataGeneratorError,
    ModelFormatError,
    TestDataGenerator,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
BLUEMOON_MODEL = ROOT / "efsm_model.json"
BLUEMOON_RULES = ROOT / "security_rules.json"
BLUEMOON_CONTEXT = ROOT / "test_data_context.bluemoon.json"

VALID, INVALID = "VERIFIED_VALID", "VERIFIED_INVALID"


def leaf(var, op, value=None, **kw):
    return {"var": var, "op": op, **({"value_var": kw["value_var"]} if "value_var" in kw else {"value": value})}


# Các guard của EFSM nhỏ: tên -> guard JSON
GUARDS = {
    "G1": {"all": [leaf("x", ">", 0), leaf("x", "<=", value_var="y")]},          # ví dụ chính: quantity > 0 AND quantity <= stock
    "G2": {"any": [leaf("x", "<", 0), leaf("x", ">", 100)]},                       # OR
    "G3": {"not": leaf("x", "==", 5)},                                              # NOT
    "G4": {"expr": "y + x <= 10 and x >= 1"},                                       # expr có BoolOp, tổng hai biến
    "G5": leaf("mode", "in", ["A", "B"]),                                           # enum / membership
    "G6": {"all": [leaf("x", ">", 5), leaf("x", "<", 3)]},                          # vô nghiệm (khoảng số nguyên)
    "G7": {"expr": "x * z == 12 and x > 1 and z > 1"},                              # phi tuyến, có nghiệm
    "G8": leaf("flag", "==", True),
    "G9": {"var": "x", "op": "exists"},                                             # evaluator hỗ trợ, bộ sinh chưa
    "G10": {"expr": "x > SUM_PRODUCT(items, 'a', 'b')"},                            # hàm SUM_PRODUCT
    "G11": {"expr": "x > y"},                                                       # ràng buộc hai biến
    "G12": {"expr": "x * x == 2"},                                                  # không có nghiệm nguyên, nhưng KHÔNG chứng minh được
    "G13": {"all": [leaf("flag", "==", True), leaf("flag", "==", False)]},          # vô nghiệm (miền boolean hữu hạn)
    "G14": {"all": [leaf("mode", "==", "A"), leaf("mode", "==", "B")]},             # vô nghiệm (enum khai báo)
    "G15": leaf("s", "==", "go"),                                                   # chuỗi không khai báo miền
    "G16": None,                                                                    # không có guard
    "G17": {"expr": "x * z == 3000009 and x > 1 and z > 1"},                        # có nghiệm (3 * 1000003) nhưng ngoài ứng viên
    "G20": {"expr": "x * z == 1000003 and x > 1 and z > 1"},                        # 1000003 là số nguyên tố: vô nghiệm, nhưng không chứng minh được
    "G18": {"expr": "x + z == 10 and x > z"},                                       # hai biến request ràng buộc nhau
    "G19": {"all": [leaf("x", ">=", 3), leaf("x", "<", 8)]},                        # biên khác với G1
}


def fixture_model() -> dict:
    return {
        "model_name": "Mini", "schema_version": "2.0", "initial_state": "S0",
        "states": [{"id": "S0", "type": "initial"}, {"id": "S1"}],
        "variables": [
            {"name": "x", "type": "integer", "origin": "request"},
            {"name": "z", "type": "integer", "origin": "request"},
            {"name": "y", "type": "integer", "origin": "database"},
            {"name": "flag", "type": "boolean", "origin": "database"},
            {"name": "mode", "type": "string", "origin": "request", "allowed_values": ["A", "B", "C"]},
            {"name": "s", "type": "string", "origin": "request"},
            {"name": "items", "type": "list", "origin": "database"},
        ],
        "transitions": [
            {"id": name, "source": "S0", "target": "S1", "event": f"e_{name}", "guard": copy.deepcopy(guard),
             "actions": [], "rule_refs": []}
            for name, guard in GUARDS.items()
        ],
        "invariants": [], "negative_transitions": [],
    }


def make(tmp_path: Path, model: dict | None = None, **kwargs) -> TestDataGenerator:
    path = tmp_path / "model.json"
    path.write_text(json.dumps(model or fixture_model()), encoding="utf-8")
    return TestDataGenerator(path, **kwargs)


@pytest.fixture
def gen(tmp_path) -> TestDataGenerator:
    return make(tmp_path)


def values(dataset, var, **filters):
    """Giá trị của biến ``var`` trong các mẫu thỏa bộ lọc (bỏ mẫu thiếu biến)."""
    out = {}
    for s in dataset.samples:
        if var in s.data and (s.target_variable == var or s.technique == "BASE") \
                and all(getattr(s, k) == v for k, v in filters.items()):
            out[s.data[var]] = s
    return out


# ==========================================================================
# Phân tích biến
# ==========================================================================
class TestVariableAnalysis:
    def test_variables_roles_and_dependencies(self, gen):
        analysis = gen.analyze_guard(GUARDS["G1"])
        by_name = {v["name"]: v for v in analysis.variables}
        assert analysis.supported and set(by_name) == {"x", "y"}
        assert by_name["x"]["role"] == "input" and by_name["y"]["role"] == "context"
        bound = [c for c in by_name["x"]["constraints"] if c["operator"] == ">"][0]
        assert bound["constant_bound"] == 0 and bound["depends_on_variables"] == []
        relation = [c for c in by_name["x"]["constraints"] if c["operator"] == "<="][0]
        assert relation["constant_bound"] is None and relation["depends_on_variables"] == ["y"]

    def test_context_variable_is_not_treated_as_a_constant(self, gen):
        y = {v["name"]: v for v in gen.analyze_guard(GUARDS["G1"]).variables}["y"]
        assert y["role"] == "context"
        assert all(c["constant_bound"] is None for c in y["constraints"] if c["depends_on_variables"])

    def test_expression_guard_is_analysed(self, gen):
        analysis = gen.analyze_guard(GUARDS["G4"])
        assert [v["name"] for v in analysis.variables] == ["y", "x"]
        assert [c["condition"] for c in analysis.conjuncts] == ["y + x <= 10", "x >= 1"]
        assert all(c["linear"] for v in analysis.variables for c in v["constraints"])

    def test_types_and_domains_come_from_the_model_only(self, gen):
        by_name = {v["name"]: v for v in gen.analyze_guard({"all": [GUARDS["G5"], GUARDS["G1"]]}).variables}
        assert by_name["mode"]["domain"]["values"] == ["A", "B", "C"] and by_name["mode"]["domain"]["complete"] is True
        assert by_name["x"]["type"] == "integer" and by_name["x"]["domain"]["range"] == "UNDETERMINED_IN_EFSM"
        assert by_name["x"]["domain"]["values"] is None  # không tự đặt min/max

    def test_nonlinear_constraint_is_flagged(self, gen):
        analysis = gen.analyze_guard(GUARDS["G7"])
        assert any(not c["linear"] for v in analysis.variables for c in v["constraints"])
        assert any("non-linear" in n for n in analysis.notes)

    def test_undetermined_domain_is_reported(self, gen):
        assert any("not declared in the EFSM" in n for n in gen.analyze_guard(GUARDS["G1"]).notes)

    @pytest.mark.parametrize("name, fragment", [("G9", "operator"), ("G10", "SUM_PRODUCT"), ("G12", None)])
    def test_unsupported_constructs_are_named(self, gen, name, fragment):
        analysis = gen.analyze_guard(GUARDS[name])
        if fragment is None:
            assert analysis.supported
        else:
            assert not analysis.supported and fragment in analysis.reason

    def test_no_guard(self, gen):
        analysis = gen.analyze_guard(None)
        assert analysis.supported and analysis.variables == []

    def test_string_and_malformed_guards(self, gen):
        assert gen.analyze_guard("x > 0").supported
        for bad in ({"foo": 1}, 42, {"expr": "x >"}, {"expr": "nope > 1"}, {"all": []}):
            assert not gen.analyze_guard(bad).supported

    def test_invariants_that_need_runtime_data(self):
        analysis = {a["invariant_id"]: a for a in TestDataGenerator(BLUEMOON_MODEL).analyze_invariants()}
        assert analysis["INV-03"]["variables"] == ["order_items", "order_total"]
        assert analysis["INV-03"]["generator_support"] == "UNSUPPORTED"          # SUM_PRODUCT trên danh sách
        assert analysis["INV-05"]["variables"] == ["order_status", "stock_restoration_count"]
        assert analysis["INV-05"]["generator_support"] == "SUPPORTED"
        assert analysis["INV-06"]["generator_support"] == "NOT_APPLICABLE"       # kiểm tra cấu trúc mô hình
        assert analysis["INV-04"]["runtime_origins"]["payment_count"] == "derived"


# ==========================================================================
# EP, BVA
# ==========================================================================
class TestPartitionsAndBoundaries:
    def test_valid_partition_exists_and_is_verified(self, gen):
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 10})
        valid = ds.valid_samples()
        assert valid and all(s.verification_status == VALID for s in valid)
        assert all(0 < s.data["x"] <= 10 for s in valid)

    def test_invalid_partitions_exist_below_and_above(self, gen):
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 10})
        invalid_x = {s.data["x"] for s in ds.samples if s.expected_guard is False and s.data["y"] == 10}
        assert any(v <= 0 for v in invalid_x) and any(v > 10 for v in invalid_x)

    def test_spec_boundary_table_is_derived_from_the_guard(self, gen):
        """quantity > 0 AND quantity <= stock, stock = 10  ->  -1, 0, 1, 9, 10, 11."""
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 10})
        by_x = values(ds, "x", expected_status=None)
        expected = {-1: False, 0: False, 1: True, 9: True, 10: True, 11: False}
        for x, expected_guard in expected.items():
            assert by_x[x].expected_guard is expected_guard, x
            assert by_x[x].verification_status == (VALID if expected_guard else INVALID), x

    def test_boundaries_move_with_the_context_value(self, gen):
        """Chứng minh không hardcode 10: đổi stock thì biên đổi theo."""
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 5})
        by_x = values(ds, "x", expected_status=None)
        assert by_x[5].expected_guard is True and by_x[6].expected_guard is False and by_x[4].expected_guard is True
        assert 10 not in by_x or by_x[10].expected_guard is False

    def test_boundaries_follow_the_operators_and_constants(self, gen):
        ds = gen.generate_for_guard(GUARDS["G19"])  # x >= 3 AND x < 8  ->  [3, 7]
        by_x = values(ds, "x", expected_status=None)
        assert {k: v.expected_guard for k, v in by_x.items() if k in (2, 3, 4, 6, 7, 8)} == {
            2: False, 3: True, 4: True, 6: True, 7: True, 8: False}

    def test_boundaries_of_a_sum_expression_depend_on_the_other_variable(self, gen):
        ds = gen.generate_for_guard(GUARDS["G4"], {"y": 4})  # 4 + x <= 10 AND x >= 1  ->  x in [1, 6]
        by_x = values(ds, "x", expected_status=None)
        assert by_x[6].expected_guard is True and by_x[7].expected_guard is False
        assert by_x[1].expected_guard is True and by_x[0].expected_guard is False

    def test_partitions_reflect_the_exact_derived_bounds(self, gen):
        labels = {s.partition.get("partition") for s in gen.generate_for_guard(GUARDS["G1"], {"y": 10}).samples}
        assert {"x in [-inf, 0]", "x in [1, 10]", "x in [11, +inf]"} <= labels
        labels = {s.partition.get("partition") for s in gen.generate_for_guard(GUARDS["G19"]).samples}
        assert {"x in [-inf, 2]", "x in [3, 7]", "x in [8, +inf]"} <= labels
        labels = {s.partition.get("partition") for s in gen.generate_for_guard(GUARDS["G4"], {"y": 4}).samples}
        assert {"x in [-inf, 0]", "x in [1, 6]", "x in [7, +inf]"} <= labels

    def test_techniques_are_recorded(self, gen):
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 10})
        techniques = {t for s in ds.samples for t in s.techniques}
        assert {"BASE", "BVA", "EP", "EP_MISSING", "EP_NULL", "EP_WRONG_TYPE"} <= techniques
        boundary = values(ds, "x", expected_status=None)[10]
        assert boundary.technique == "BVA" and boundary.partition["variable"] == "x"
        assert boundary.changed_variables in (["x"], [])

    def test_equivalence_partition_representatives_are_inside_their_partition(self, gen):
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 10})
        for s in ds.samples:
            if "EP" in s.techniques and s.partition.get("partition", "").startswith("x in ["):
                lo, hi = s.partition["partition"][6:-1].split(", ")
                x = s.data["x"]
                assert (lo == "-inf" or x >= int(lo)) and (hi == "+inf" or x <= int(hi))

    def test_changed_variables_are_reported(self, gen):
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 10})
        eleven = values(ds, "x", expected_status=None)[11]
        assert eleven.changed_variables == ["x"] and eleven.target_variable == "x"


# ==========================================================================
# AND / OR / NOT, nhiều biến, enum
# ==========================================================================
class TestLogicAndConstraints:
    def test_and_negatives_violate_exactly_one_condition(self, gen):
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 10})
        by_x = values(ds, "x", expected_status=None)
        assert by_x[0].violated_conditions["verified"] == ["x > 0"] and by_x[0].violated_conditions["single_condition"] is True
        assert by_x[11].violated_conditions["verified"] == ["x <= y"] and by_x[11].violated_conditions["single_condition"] is True
        assert by_x[0].violated_conditions["prediction_matches_evaluator"] is True

    def test_single_condition_is_never_assumed(self, gen):
        ds = gen.generate_for_guard({"all": [leaf("x", ">", 0), leaf("x", ">", 5)]})
        by_x = values(ds, "x", expected_status=None)
        assert by_x[0].violated_conditions["verified"] == ["x > 0", "x > 5"]
        assert by_x[0].violated_conditions["single_condition"] is False
        assert by_x[5].violated_conditions["verified"] == ["x > 5"] and by_x[5].violated_conditions["single_condition"] is True
        assert by_x[0].verification_status == INVALID  # vẫn là dữ liệu không hợp lệ, chỉ không phải vi phạm đơn

    def test_or_guard(self, gen):
        ds = gen.generate_for_guard(GUARDS["G2"])
        by_x = values(ds, "x", expected_status=None)
        assert [by_x[k].expected_guard for k in (-1, 0, 100, 101)] == [True, False, False, True]
        assert all(s.verification_status in (VALID, INVALID) for s in ds.samples)

    def test_not_guard(self, gen):
        ds = gen.generate_for_guard(GUARDS["G3"])
        by_x = values(ds, "x", expected_status=None)
        assert by_x[5].expected_guard is False and by_x[4].expected_guard is True and by_x[6].expected_guard is True

    def test_two_variable_relation_is_solved_jointly(self, gen):
        ds = gen.generate_for_guard(GUARDS["G18"])  # x + z == 10 AND x > z
        base = ds.base["assignment"]
        assert base["x"] + base["z"] == 10 and base["x"] > base["z"]
        assert ds.samples[0].verification_status == VALID  # không sinh độc lập từng biến

    def test_relation_between_request_and_context_variables(self, gen):
        ds = gen.generate_for_guard(GUARDS["G11"], {"y": 10})  # x > y
        by_x = values(ds, "x", expected_status=None)
        assert by_x[10].expected_guard is False and by_x[11].expected_guard is True

    def test_nonlinear_guard_is_solved_by_search_and_verified(self, gen):
        ds = gen.generate_for_guard(GUARDS["G7"])
        base = ds.base["assignment"]
        assert base["x"] * base["z"] == 12 and base["x"] > 1 and base["z"] > 1
        assert ds.samples[0].verification_status == VALID
        assert all(s.verification_status in (VALID, INVALID) for s in ds.samples)

    def test_enum_membership_partitions(self, gen):
        ds = gen.generate_for_guard(GUARDS["G5"])
        by_mode = values(ds, "mode", expected_status=None)
        assert by_mode["A"].expected_guard is True and by_mode["B"].expected_guard is True
        assert by_mode["C"].expected_guard is False                      # trong miền khai báo nhưng không thỏa guard
        assert by_mode[OFF_DOMAIN_VALUE].expected_guard is False        # ngoài miền khai báo
        assert by_mode[OFF_DOMAIN_VALUE].partition["partition"] == "OUT_OF_DECLARED_DOMAIN"
        assert all(s.verification_status in (VALID, INVALID) for s in ds.samples)

    def test_boolean_and_undeclared_string_domains(self, gen):
        assert {s.data["flag"]: s.expected_guard for s in gen.generate_for_guard(GUARDS["G8"]).samples
                if "flag" in s.data and s.expected_status is None} == {True: True, False: False}
        ds = gen.generate_for_guard(GUARDS["G15"])
        by_s = values(ds, "s", expected_status=None)
        assert by_s["go"].expected_guard is True and by_s[OFF_DOMAIN_VALUE].partition["partition"] == "OTHER_VALUE"

    def test_each_top_level_condition_gets_a_single_violation_sample(self, gen):
        ds = gen.generate_for_guard({"all": [GUARDS["G1"], leaf("mode", "in", ["A", "B"])]}, {"y": 10})
        violated = {tuple(s.violated_conditions["verified"]) for s in ds.samples
                    if s.violated_conditions.get("single_condition") is True}
        assert violated == {("x > 0",), ("x <= y",), ('mode in ["A", "B"]',)}


# ==========================================================================
# Missing / wrong type
# ==========================================================================
class TestMalformedInputs:
    def test_missing_variable_sample(self, gen):
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 10})
        missing = [s for s in ds.samples if s.technique == "EP_MISSING"][0]
        assert "x" not in missing.data and missing.expected_guard is None
        assert missing.expected_status == "MISSING_VARIABLE" and missing.verification_status == INVALID
        assert missing.evaluator_verdict["status"] == "MISSING_VARIABLE" and missing.evaluator_verdict["guard_result"] is None

    @pytest.mark.parametrize("technique", ["EP_NULL", "EP_WRONG_TYPE"])
    def test_wrong_type_samples(self, gen, technique):
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 10})
        sample = [s for s in ds.samples if s.technique == technique][0]
        assert sample.expected_status == "TYPE_ERROR" and sample.verification_status == INVALID
        assert sample.evaluator_verdict["status"] == "TYPE_ERROR"

    def test_wrong_type_values_depend_on_the_declared_type(self, gen):
        ds = gen.generate_for_guard({"all": [GUARDS["G5"], GUARDS["G8"]]}, {"flag": True})
        wrong = {s.target_variable: s.data[s.target_variable] for s in ds.samples if s.technique == "EP_WRONG_TYPE"}
        assert wrong == {"mode": 12345}  # flag là biến ngữ cảnh (database) nên không sinh mẫu sai định dạng

    def test_malformed_samples_can_be_disabled(self, gen):
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 10}, include_malformed=False)
        assert not any(s.expected_status for s in ds.samples)

    def test_negative_samples_can_be_disabled(self, gen):
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 10}, include_negative=False, include_malformed=False)
        assert [s.technique for s in ds.samples] == ["BASE"]


# ==========================================================================
# UNSAT / UNKNOWN / UNSUPPORTED
# ==========================================================================
class TestSolverStatuses:
    def test_unsat_proven_by_integer_interval(self, gen):
        result = gen.solve(GUARDS["G6"])
        assert result.status == "UNSAT" and "contradictory" in result.reason
        ds = gen.generate_for_guard(GUARDS["G6"])
        assert ds.status == "UNSAT" and ds.samples == []

    @pytest.mark.parametrize("name", ["G13", "G14"])
    def test_unsat_proven_by_exhausting_a_declared_finite_domain(self, gen, name):
        result = gen.solve(GUARDS[name])
        assert result.status == "UNSAT" and "complete declared finite domain" in result.reason

    def test_unsat_reports_the_fixed_assumptions(self, gen):
        result = gen.solve(GUARDS["G1"], fixed={"y": 0})  # x > 0 AND x <= 0 với y cố định
        assert result.status == "UNSAT" and result.assumptions == ["y fixed to 0"]

    def test_search_exhaustion_is_never_reported_as_unsat(self, gen):
        """x*x == 2 vô nghiệm trên số nguyên, nhưng bộ giải không chứng minh được nên chỉ được trả UNKNOWN."""
        result = gen.solve(GUARDS["G12"])
        assert result.status == "UNKNOWN"
        assert gen.generate_for_guard(GUARDS["G12"]).status == "UNKNOWN"

    def test_satisfiable_guard_outside_the_candidate_pool_is_unknown_not_unsat(self, gen):
        result = gen.solve(GUARDS["G17"])  # x=3, z=1000003 là nghiệm thật
        assert result.status == "UNKNOWN" and result.assignment is None
        assert gen.evaluator.evaluate_guard(GUARDS["G17"], {"x": 3, "z": 1000003}).result is True

    def test_unsolvable_but_unproven_guard_is_unknown(self, gen):
        """1000003 là số nguyên tố nên không có x, z > 1 thỏa guard; bộ giải không chứng minh được nên chỉ UNKNOWN."""
        assert gen.solve(GUARDS["G20"]).status == "UNKNOWN"

    def test_divisors_of_constants_are_candidates(self, gen):
        result = gen.solve({"expr": "x * z == 12 and x > 1 and z > 1"})
        assert result.status == "SAT" and result.assignment["x"] * result.assignment["z"] == 12

    def test_budget_exhaustion_is_reported_as_unknown(self, tmp_path):
        small = make(tmp_path, max_nodes=10)
        result = small.solve(GUARDS["G17"])
        assert result.status == "UNKNOWN" and "budget" in result.reason

    def test_dnf_explosion_is_unknown(self, tmp_path):
        small = make(tmp_path, max_dnf_terms=4)
        guard = {"all": [{"any": [leaf("x", "==", i), leaf("x", "==", i + 100)]} for i in range(4)]}
        assert small.solve(guard).status == "UNKNOWN" and "DNF" in small.solve(guard).reason

    @pytest.mark.parametrize("name", ["G9", "G10"])
    def test_unsupported_guards(self, gen, name):
        assert gen.solve(GUARDS[name]).status == "UNSUPPORTED"
        ds = gen.generate_for_guard(GUARDS[name])
        assert ds.status == "UNSUPPORTED" and ds.reason and ds.samples == []
        # GuardEvaluator vẫn hỗ trợ guard này: giới hạn nằm ở bộ sinh dữ liệu, không phải evaluator
        assert gen.evaluator.evaluate_guard(GUARDS[name], {"x": 1, "items": []}).status in (
            GuardStatus.GUARD_SATISFIED, GuardStatus.GUARD_NOT_SATISFIED)

    def test_solution_found_for_satisfiable_guard(self, gen):
        result = gen.solve(GUARDS["G1"], fixed={"y": 10})
        assert result.status == "SAT" and 0 < result.assignment["x"] <= 10

    def test_violation_search(self, gen):
        result = gen.solve(GUARDS["G1"], fixed={"y": 10}, want_violation=True)
        assert result.status == "SAT" and not 0 < result.assignment["x"] <= 10

    def test_a_condition_that_cannot_be_violated_is_reported(self, gen):
        ds = gen.generate_for_guard({"all": [leaf("x", ">", 0), leaf("flag", "in", [True, False])]})
        assert any(n["condition"] == "flag in [true, false]" and n["single_violation_sample"] == "UNSAT"
                   for n in ds.conjunct_notes)


# ==========================================================================
# Đối chiếu với GuardEvaluator
# ==========================================================================
class _LyingEvaluator(GuardEvaluator):
    """GuardEvaluator đảo kết quả: dùng để chứng minh generator không tự gắn nhãn VERIFIED."""

    def evaluate_guard(self, guard, variables):
        real = super().evaluate_guard(guard, variables)
        if real.status is GuardStatus.GUARD_SATISFIED:
            return GuardEvaluation(GuardStatus.GUARD_NOT_SATISFIED, False, "lie")
        if real.status is GuardStatus.GUARD_NOT_SATISFIED:
            return GuardEvaluation(GuardStatus.GUARD_SATISFIED, True, "lie")
        return real


class _WholeGuardLiar(GuardEvaluator):
    """Chỉ nói dối khi đánh giá CẢ guard (không nói dối ở mức từng điều kiện con)."""

    whole = None

    def evaluate_guard(self, guard, variables):
        real = super().evaluate_guard(guard, variables)
        if guard != self.whole or real.status not in (GuardStatus.GUARD_SATISFIED, GuardStatus.GUARD_NOT_SATISFIED):
            return real
        flipped = real.status is GuardStatus.GUARD_SATISFIED
        return GuardEvaluation(GuardStatus.GUARD_NOT_SATISFIED if flipped else GuardStatus.GUARD_SATISFIED, not flipped, "lie")


class _ConjunctLiar(GuardEvaluator):
    """Đúng ở mức cả guard nhưng nói dối ở mức từng điều kiện con."""

    whole = None

    def evaluate_guard(self, guard, variables):
        real = super().evaluate_guard(guard, variables)
        if guard == self.whole or real.status not in (GuardStatus.GUARD_SATISFIED, GuardStatus.GUARD_NOT_SATISFIED):
            return real
        flipped = real.status is GuardStatus.GUARD_SATISFIED
        return GuardEvaluation(GuardStatus.GUARD_NOT_SATISFIED if flipped else GuardStatus.GUARD_SATISFIED, not flipped, "lie")


class TestEvaluatorCrossCheck:
    def _generator(self, tmp_path, cls):
        path = tmp_path / "model.json"
        path.write_text(json.dumps(fixture_model()), encoding="utf-8")
        evaluator = cls(path)
        evaluator.whole = GUARDS["G1"]
        return TestDataGenerator(path, evaluator=evaluator)

    def test_a_wrong_overall_verdict_blocks_verification(self, tmp_path):
        ds = self._generator(tmp_path, _WholeGuardLiar).generate_for_guard(GUARDS["G1"], {"y": 10})
        for s in ds.samples:
            if s.expected_guard is not None:
                assert s.verification_status == "UNVERIFIED", s.data

    def test_a_wrong_per_condition_verdict_blocks_verification_of_negatives(self, tmp_path):
        ds = self._generator(tmp_path, _ConjunctLiar).generate_for_guard(GUARDS["G1"], {"y": 10})
        negatives = [s for s in ds.samples if s.expected_guard is False]
        assert negatives and all(s.verification_status == "UNVERIFIED" for s in negatives)
        assert all(s.violated_conditions["prediction_matches_evaluator"] is False for s in negatives)
        assert all(s.verification_status == VALID for s in ds.samples if s.expected_guard is True)

    def test_every_sample_of_a_supported_guard_is_verified(self, gen):
        for name in ("G1", "G2", "G3", "G4", "G5", "G7", "G8", "G11", "G15", "G18", "G19"):
            ds = gen.generate_for_guard(GUARDS[name], {"y": 10})
            assert ds.samples, name
            assert {s.verification_status for s in ds.samples} <= {VALID, INVALID}, name

    def test_verified_status_matches_the_evaluator_verdict(self, gen):
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 10})
        for s in ds.samples:
            verdict = gen.evaluator.evaluate_guard(GUARDS["G1"], s.data)
            assert s.evaluator_verdict["status"] == verdict.status.value
            if s.verification_status == VALID:
                assert verdict.result is True
            if s.verification_status == INVALID:
                assert verdict.result is not True

    def test_disagreeing_evaluator_means_unverified(self, tmp_path):
        path = tmp_path / "model.json"
        path.write_text(json.dumps(fixture_model()), encoding="utf-8")
        liar = TestDataGenerator(path, evaluator=_LyingEvaluator(path))
        ds = liar.generate_for_guard(GUARDS["G1"], {"y": 10})
        by_x = values(ds, "x", expected_status=None)
        assert by_x[5].verification_status == "UNVERIFIED" and by_x[0].verification_status == "UNVERIFIED"
        assert not any(s.verification_status in (VALID,) for s in ds.samples if s.expected_guard is True)
        # mẫu sai định dạng không bị đảo (evaluator trả lỗi chứ không phải kết quả guard) nên vẫn khớp
        assert [s.verification_status for s in ds.samples if s.expected_status] == [INVALID] * 3
        assert all(s.violated_conditions["prediction_matches_evaluator"] is not True
                   for s in ds.samples if s.expected_guard is False)

    def test_transition_samples_are_checked_through_check_transition(self, gen):
        ds = gen.generate_for_transition("G1", {"y": 10})
        assert ds.transition_id == "G1" and ds.source_state == "S0" and ds.event == "e_G1"
        assert {s.verification_status for s in ds.samples} <= {VALID, INVALID}
        check = gen.evaluator.check_transition("G1", "S0", ds.samples[0].data)
        assert check.enabled and ds.samples[0].evaluator_verdict["status"] == "GUARD_SATISFIED"

    def test_no_guard_transition(self, gen):
        ds = gen.generate_for_transition("G16")
        assert ds.status == "NO_GUARD" and ds.samples[0].data == {} and ds.samples[0].verification_status == VALID

    def test_context_that_contradicts_the_guard_is_relaxed_and_reported(self, gen):
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 0})  # y = 0 làm x > 0 AND x <= y vô nghiệm
        assert ds.status == "OK" and ds.base["mode"] == "context_as_hint"
        assert ds.base["context_adjusted"] == ["y"] and ds.base["assignment"]["y"] > 0
        assert ds.samples[0].verification_status == VALID

    def test_unknown_transition(self, gen):
        with pytest.raises(KeyError):
            gen.generate_for_transition("NOPE")


# ==========================================================================
# Output, tính ổn định, bất biến của đầu vào
# ==========================================================================
class TestOutput:
    def test_output_is_stable_and_json_serialisable(self, tmp_path):
        first = make(tmp_path)
        second = make(tmp_path)
        a = json.dumps(first.to_output_dict(first.generate_all({"y": 10})), default=str)
        b = json.dumps(second.to_output_dict(second.generate_all({"y": 10})), default=str)
        assert a == b and json.loads(a)["schema_version"] == "1.0"
        assert "timestamp" not in a and "generated_at" not in a

    def test_sample_ids_are_unique_and_sequential(self, gen):
        datasets = gen.generate_all({"y": 10})
        ids = [s.sample_id for d in datasets for s in d.samples]
        assert ids == [f"GD{i:03d}" for i in range(1, len(ids) + 1)]

    def test_output_structure_matches_the_documented_schema(self, gen):
        out = gen.to_output_dict(gen.generate_all({"y": 10}, transition_ids=["G1"]))
        dataset = out["datasets"][0]
        assert dataset["transition_id"] == "G1" and dataset["schema_version"] == "1.0"
        sample = dataset["samples"][0]
        for key in ("sample_id", "data", "expected_guard", "technique", "verification_status", "changed_variables",
                    "violated_conditions", "evaluator_verdict", "partition"):
            assert key in sample
        assert {"summary", "model", "configuration", "invariant_analysis", "datasets"} <= set(out)

    def test_summary_counts_are_consistent(self, gen):
        datasets = gen.generate_all({"y": 10})
        summary = gen.summarize(datasets)
        samples = [s for d in datasets for s in d.samples]
        assert summary["samples_total"] == len(samples)
        assert summary["valid_samples"] + summary["invalid_samples"] + summary["malformed_samples"] <= len(samples)
        assert sum(summary["verification"].values()) == len(samples)
        assert summary["guards_analyzed"] == len(GUARDS) - 1 and summary["transitions_without_guard"] == 1
        assert summary["guards_unsupported"] == 2  # G9, G10
        assert {u["label"] for u in summary["unsolved"]} >= {"G6", "G9", "G10", "G12", "G13", "G14", "G17", "G20"}

    def test_inputs_are_not_modified(self, tmp_path):
        model_path = tmp_path / "model.json"
        model_path.write_text(json.dumps(fixture_model()), encoding="utf-8")
        before = model_path.read_bytes()
        generator = TestDataGenerator(model_path)
        guard = copy.deepcopy(GUARDS["G1"])
        context = {"y": 10}
        generator.generate_for_guard(guard, context)
        generator.generate_all({"y": 10})
        assert guard == GUARDS["G1"] and context == {"y": 10} and model_path.read_bytes() == before

    def test_data_contains_only_guard_variables(self, gen):
        ds = gen.generate_for_guard(GUARDS["G1"], {"y": 10, "unrelated": 99})
        assert all(set(s.data) <= {"x", "y"} for s in ds.samples)

    def test_missing_files(self, tmp_path):
        with pytest.raises(ModelFormatError):
            TestDataGenerator(tmp_path / "nope.json")
        with pytest.raises(ModelFormatError):
            TestDataGenerator(BLUEMOON_MODEL, tmp_path / "nope.json")

    def test_cli(self, tmp_path, capsys):
        model = tmp_path / "model.json"
        model.write_text(json.dumps(fixture_model()), encoding="utf-8")
        context = tmp_path / "ctx.json"
        context.write_text(json.dumps({"defaults": {"y": 10}}), encoding="utf-8")
        out = tmp_path / "out" / "data.json"
        assert main(["--model", str(model), "--context", str(context), "--transition", "G1", "--output", str(out)]) == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert [d["transition_id"] for d in data["datasets"]] == ["G1"] and "guards=1" in capsys.readouterr().out

    def test_cli_errors(self, tmp_path, capsys):
        assert main(["--model", str(tmp_path / "nope.json"), "--output", str(tmp_path / "o.json")]) == 2
        assert "ERROR" in capsys.readouterr().err
        model = tmp_path / "model.json"
        model.write_text(json.dumps(fixture_model()), encoding="utf-8")
        assert main(["--model", str(model), "--transition", "NOPE", "--output", str(tmp_path / "o.json")]) == 2
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"oops": 1}), encoding="utf-8")
        assert main(["--model", str(model), "--context", str(bad), "--output", str(tmp_path / "o.json")]) == 2


# ==========================================================================
# Integration - efsm_model.json thật của BlueMoon
# ==========================================================================
@pytest.fixture(scope="module")
def bluemoon():
    return TestDataGenerator(BLUEMOON_MODEL, BLUEMOON_RULES)


@pytest.fixture(scope="module")
def bluemoon_data(bluemoon):
    defaults = json.loads(BLUEMOON_CONTEXT.read_text(encoding="utf-8"))["defaults"]
    return {d.label: d for d in bluemoon.generate_all(defaults)}


class TestBlueMoonIntegration:
    def test_every_real_guard_is_supported_and_every_sample_verified(self, bluemoon, bluemoon_data):
        assert set(bluemoon_data) == set(bluemoon._transitions)
        for tid, ds in bluemoon_data.items():
            if tid == "T10":
                assert ds.status == "NO_GUARD"
                continue
            assert ds.analysis.supported and ds.status == "OK", tid
            assert {s.verification_status for s in ds.samples} <= {VALID, INVALID}, tid

    def test_t01_boundaries_use_the_real_expression(self, bluemoon_data):
        """T01: product_exists AND quantity > 0 AND current_qty + quantity <= stock, stock = 10, current_qty = 0."""
        by_q = values(bluemoon_data["T01"], "quantity", expected_status=None)
        for q, expected in {-1: False, 0: False, 1: True, 9: True, 10: True, 11: False}.items():
            assert by_q[q].expected_guard is expected, q
            assert by_q[q].data["stock"] == 10 and by_q[q].data["current_qty"] == 0
        assert by_q[0].violated_conditions["verified"] == ["quantity > 0"]
        assert by_q[11].violated_conditions["verified"] == ["current_qty + quantity <= stock"]

    def test_t01_boundary_depends_on_current_qty(self, bluemoon):
        ds = bluemoon.generate_for_transition("T01", {"stock": 10, "current_qty": 4, "product_exists": True})
        by_q = values(ds, "quantity", expected_status=None)
        assert by_q[6].expected_guard is True and by_q[7].expected_guard is False  # 4 + quantity <= 10

    def test_t02_uses_a_two_variable_relation(self, bluemoon_data):
        by_q = values(bluemoon_data["T02"], "quantity", expected_status=None)
        assert by_q[10].expected_guard is True and by_q[11].expected_guard is False
        names = {v["name"]: v for v in bluemoon_data["T02"].analysis.variables}
        assert names["stock"]["role"] == "context" and names["quantity"]["role"] == "input"

    def test_t04_each_condition_has_a_verified_single_violation(self, bluemoon_data):
        ds = bluemoon_data["T04"]
        wanted = {c["condition"] for c in ds.analysis.conjuncts}
        violated = {c for s in ds.samples if s.violated_conditions.get("single_condition") is True
                    for c in s.violated_conditions["verified"]}
        assert len(wanted) == 10 and violated == wanted
        by_len = values(ds, "customer_name_length", expected_status=None)
        assert by_len[1].expected_guard is False and by_len[2].expected_guard is True
        assert by_len[100].expected_guard is True and by_len[101].expected_guard is False

    def test_t04_payment_method_enum(self, bluemoon_data):
        by_method = values(bluemoon_data["T04"], "payment_method", expected_status=None)
        assert by_method["MOCK"].expected_guard is True and by_method["COD"].expected_guard is True
        assert by_method[OFF_DOMAIN_VALUE].expected_guard is False

    def test_state_only_guards_vary_the_state_variable(self, bluemoon_data):
        by_status = values(bluemoon_data["T05"], "order_status", expected_status=None)
        assert by_status["PENDING_CONFIRMATION"].expected_guard is True
        assert [by_status[k].expected_guard for k in ("CONFIRMED", "PAID", "CANCELLED")] == [False] * 3
        assert any(s.expected_status == "MISSING_VARIABLE" for s in bluemoon_data["T06"].samples)

    def test_t06_and_t07_are_complementary(self, bluemoon_data):
        t06 = values(bluemoon_data["T06"], "payment_result", expected_status=None)
        t07 = values(bluemoon_data["T07"], "payment_result", expected_status=None)
        assert t06["SUCCESS"].expected_guard is True and t06["FAILED"].expected_guard is False
        assert t07["FAILED"].expected_guard is True and t07["SUCCESS"].expected_guard is False

    def test_summary(self, bluemoon, bluemoon_data):
        summary = bluemoon.summarize(list(bluemoon_data.values()))
        assert (summary["guards_analyzed"], summary["guards_supported"], summary["guards_unsupported"]) == (9, 9, 0)
        assert summary["verification"]["UNVERIFIED"] == 0 and summary["verification"]["UNSUPPORTED"] == 0
        assert summary["verified_by_guard_evaluator"] == summary["samples_total"] > 100
        assert summary["unsolved"] == []

    def test_output_records_the_model_hash_and_is_stable(self, bluemoon, bluemoon_data):
        out = bluemoon.to_output_dict(list(bluemoon_data.values()))
        assert out["model"]["sha256"] == hashlib.sha256(BLUEMOON_MODEL.read_bytes()).hexdigest()
        again = TestDataGenerator(BLUEMOON_MODEL, BLUEMOON_RULES)
        defaults = json.loads(BLUEMOON_CONTEXT.read_text(encoding="utf-8"))["defaults"]
        assert json.dumps(again.to_output_dict(again.generate_all(defaults)), default=str) == json.dumps(out, default=str)

    def test_works_without_any_context(self):
        generator = TestDataGenerator(BLUEMOON_MODEL)
        datasets = generator.generate_all()
        summary = generator.summarize(datasets)
        assert summary["guards_supported"] == 9 and summary["verification"]["UNVERIFIED"] == 0
        assert all(d.status in ("OK", "NO_GUARD") for d in datasets)


# ===========================================================================
# PHẦN 2: sinh dữ liệu theo test path (path-aware)
# ===========================================================================
from test_data_generator import (  # noqa: E402
    ApiMapping,
    PathDataGenerator,
    Readiness,
    ShadowState,
    SolveStatus,
    generate_test_cases,
    request_signature,
)

PATHS_FILE = ROOT / "test_paths.json"
MAPPING_FILE = ROOT / "api_test_mapping.json"


@pytest.fixture(scope="module")
def bm():
    return TestDataGenerator(BLUEMOON_MODEL, BLUEMOON_RULES)


@pytest.fixture(scope="module")
def mapping():
    return ApiMapping.load(MAPPING_FILE)


@pytest.fixture(scope="module")
def paths_doc():
    return json.loads(PATHS_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def generated(bm, mapping, paths_doc):
    return generate_test_cases(bm, mapping, paths_doc, paths_file=PATHS_FILE.name)


@pytest.fixture(scope="module")
def cases(generated):
    return generated[0]["test_cases"]


def by_key(cases, key, kind=None):
    found = [c for c in cases if c["path_key"] == key and (kind is None or c["type"] == kind)]
    assert found, f"no test case for path {key}"
    return found[0]


def synthetic_path(key, ptype="positive", negative_id=None):
    tids = [t for t in key.split(">") if not t.startswith("!")]
    path = {"path_id": "X001", "path_type": ptype, "path_key": key, "transition_ids": tids, "feasibility": "INFEASIBLE",
            "security_rule_ids": [], "severity": "HIGH", "steps": [{"kind": "transition", "transition_id": t} for t in tids]}
    if negative_id:
        path["negative_transition_id"] = negative_id
    return path


def step_vars(step):
    return {**step["context_before"], **step["request_data"]}


class TestApiMapping:
    def test_mapping_matches_efsm(self, bm, mapping):
        assert mapping.validate(bm.model) == []

    def test_every_transition_is_mapped_through_api(self, bm, mapping):
        for t in bm.model["transitions"]:
            assert mapping.transition(t["id"])["implemented_by"] == "API"

    def test_mapping_mismatch_is_reported(self, bm, mapping):
        broken = copy.deepcopy(mapping.data)
        del broken["transitions"]["T01"]["request"]["body"]["quantity"]
        broken["transitions"]["T05"]["request"]["path_params"] = {}
        issues = ApiMapping(broken).validate(bm.model)
        assert any("T01" in i and "body" in i for i in issues)
        assert any("T05" in i and "path params" in i for i in issues)

    def test_missing_transition_in_mapping_is_reported(self, bm, mapping):
        broken = copy.deepcopy(mapping.data)
        del broken["transitions"]["T10"]
        assert any("T10" in i for i in ApiMapping(broken).validate(bm.model))

    def test_capture_must_exist(self, bm, mapping):
        broken = copy.deepcopy(mapping.data)
        broken["transitions"]["T04"]["capture"] = {}
        assert any("capture" in i for i in ApiMapping(broken).validate(bm.model))


class TestShadowState:
    def make(self):
        return ShadowState({1: {"id": 1, "price": 100, "stock": 10}, 2: {"id": 2, "price": 50, "stock": 5}})

    def test_multi_product_order_and_cancel(self):
        sh = self.make()
        sh.cart_add(1, 3)
        sh.cart_add(2, 2)
        sh.order_create("$ref:o")
        order = sh.orders["$ref:o"]
        assert order["total"] == 3 * 100 + 2 * 50
        assert (sh.products[1]["stock"], sh.products[2]["stock"]) == (7, 3) and not sh.cart
        sh.order_cancel()
        assert (sh.products[1]["stock"], sh.products[2]["stock"]) == (10, 5)
        assert order["status"] == "CANCELLED" and order["restoration_count"] == 1

    def test_payment_count_counts_only_success(self):
        sh = self.make()
        sh.cart_add(1, 1)
        sh.order_create("$ref:o")
        sh.payment_record("FAILED")
        sh.payment_record("FAILED")
        sh.payment_record("SUCCESS")
        assert len(sh.payments) == 3 and sh.orders["$ref:o"]["payment_count"] == 1

    def test_clone_is_independent(self):
        sh = self.make()
        copy_ = sh.clone()
        copy_.cart_add(1, 5)
        assert sh.cart == {} and copy_.cart == {1: 5}

    def test_projection_names_are_efsm_variables(self, bm):
        sh = self.make()
        sh.cart_add(1, 2)
        names = {v["name"] for v in bm.model["variables"]}
        assert set(sh.project(1, "cart")) <= names
        sh.order_create("$ref:o")
        assert set(sh.project(None, "order")) <= names


class TestPositivePaths:
    def test_single_transition_path(self, cases):
        case = by_key(cases, "T10", "positive")
        assert case["readiness"] == "READY" and len(case["steps"]) == 1
        assert case["steps"][0]["state_after"] == "CART" and case["expected_final_data"]["cart_items"] == []

    def test_multi_transition_chain_is_consistent(self, cases):
        case = by_key(cases, "T01>T04>T08", "positive")
        steps = case["steps"]
        assert [s["transition_id"] for s in steps] == ["T01", "T04", "T08"]
        for before, after in zip(steps, steps[1:]):
            assert before["state_after"] == after["state_before"]
        assert case["expected_final_state"] == "CANCELLED"

    def test_every_positive_step_passes_guard_evaluator_again(self, bm, cases):
        checked = 0
        for case in cases:
            for s in case["steps"]:
                if s["kind"] == "transition":
                    res = bm.evaluator.check_transition(s["transition_id"], s["state_before"], step_vars(s))
                    assert res.status is GuardStatus.GUARD_SATISFIED, (case["test_case_id"], s["step_no"], res.reason)
                    checked += 1
        assert checked > 300

    def test_context_updates_across_transitions(self, cases):
        steps = by_key(cases, "T01>T04>T08", "positive")["steps"]
        t01, t04, t08 = steps
        qty = t01["request_data"]["quantity"]
        assert t01["context_after"]["current_qty"] == t01["context_before"]["current_qty"] + qty
        assert t04["context_before"]["ordered_quantity"] == qty
        assert t04["context_after"]["stock"] == t04["context_before"]["stock"] - qty
        assert t04["context_after"]["cart_items_count"] == 0
        assert t08["context_before"]["stock"] == t04["context_after"]["stock"]
        assert t08["context_after"]["stock"] == t04["context_before"]["stock"]

    def test_quantity_chosen_in_t01_is_used_in_the_order(self, cases):
        steps = by_key(cases, "T01>T04>T08", "positive")["steps"]
        order_check = next(c for c in steps[1]["expected"]["response_checks"] if c["target"] == "response.order.items")
        assert order_check["value"][0]["quantity"] == steps[0]["request"]["body"]["quantity"]
        assert order_check["value"][0]["product_id"] == steps[0]["request"]["body"]["product_id"]

    def test_derived_values_come_from_shadow_state(self, mapping, cases):
        steps = by_key(cases, "T01>T04>T08", "positive")["steps"]
        price = mapping.products[steps[0]["request"]["body"]["product_id"]]["price"]
        total = next(c for c in steps[1]["expected"]["response_checks"] if c["target"] == "response.order.total_amount")
        assert total["value"] == price * steps[0]["request"]["body"]["quantity"]
        assert total["source"] == "shadow_state_simulation"
        assert steps[1]["context_after"]["order_total"] == total["value"]

    def test_http_status_comes_from_efsm(self, bm, cases):
        efsm = {t["id"]: t["expected_http_status"] for t in bm.model["transitions"]}
        for case in cases:
            for s in case["steps"]:
                if s["kind"] == "transition":
                    assert s["expected"]["http_status"] == efsm[s["transition_id"]]
                    assert s["expected"]["http_status_source"].startswith("efsm_model.transitions[")

    def test_symbolic_refs_not_fake_ids(self, cases):
        steps = by_key(cases, "T01>T04>T05>T06", "positive")["steps"]
        t04 = next(s for s in steps if s["transition_id"] == "T04")
        for s in steps:
            if s["transition_id"] in ("T05", "T06"):
                ref = s["request"]["path_params"]["order_id"]
                assert ref == f"$ref:steps.{t04['step_no']}.response.order.id"
        t06 = next(s for s in steps if s["transition_id"] == "T06")
        assert t06["captures"]["payment_id"] == f"$ref:steps.{t06['step_no']}.response.payment.id"

    def test_infeasible_path_in_test_paths_is_replanned_with_setup(self, paths_doc, cases):
        infeasible = {p["path_id"]: p for p in paths_doc["paths"] if p["path_type"] == "positive" and p["feasibility"] == "INFEASIBLE"}
        replanned = [c for c in cases if c["source_path_id"] in infeasible]
        assert len(replanned) >= 50
        assert all(c["replanned"] for c in replanned)
        with_setup = [c for c in replanned if c["setup"]]
        assert with_setup and all(s["role"] == "setup" for c in with_setup for s in c["steps"][: len(c["setup"])])

    def test_all_cases_start_not_executed(self, cases):
        assert all(c["execution"] == {"status": "NOT_EXECUTED"} for c in cases)
        assert all(a["status"] == "NOT_EXECUTED" for c in cases for a in c["assertions"])


class TestNegativePaths:
    def negatives(self, cases):
        return [c for c in cases if c["type"] == "negative"]

    def test_negative_step_is_last_and_only_one(self, cases):
        for c in self.negatives(cases):
            roles = [s["role"] for s in c["steps"]]
            assert roles[-1] == "negative" and roles.count("negative") == 1

    def test_violation_is_confirmed_by_evaluator(self, bm, cases):
        for c in self.negatives(cases):
            s = c["steps"][-1]
            res = bm.evaluator.check_negative_transition(s["negative_transition_id"], s["state_before"], step_vars(s))
            assert res.status is GuardStatus.NEGATIVE_APPLICABLE, (c["test_case_id"], res.reason)

    def test_prefix_is_valid_before_the_negative_step(self, bm, cases):
        c = by_key(cases, "T01>T04>T05>T06>!NT07", "negative")
        for s in c["steps"][:-1]:
            assert bm.evaluator.check_transition(s["transition_id"], s["state_before"], step_vars(s)).status is GuardStatus.GUARD_SATISFIED
        assert c["steps"][-2]["state_after"] == c["steps"][-1]["state_before"] == "PAID"

    def test_guard_violation_isolated_to_one_condition(self, cases):
        c = by_key(cases, "!NT02", "negative")
        iso = c["steps"][-1]["violation"]["isolation"]
        assert iso["status"] == "ISOLATED" and iso["failed_conditions"] == ["current_qty + quantity <= stock"]
        body = c["steps"][-1]["request"]["body"]
        assert body["quantity"] > 10 and c["steps"][-1]["violation"]["changed_variables"] == ["quantity"]

    def test_isolation_keeps_other_conditions_valid(self, cases):
        c = by_key(cases, "!NT16", "negative")  # quantity > stock, dòng giỏ có sẵn nên cart_item_exists vẫn đúng
        s = c["steps"][-1]
        assert s["context_before"]["cart_item_exists"] is True and s["violation"]["isolation"]["status"] == "ISOLATED"
        assert c["setup"], "phải thêm dòng giỏ hàng bằng API để chỉ vi phạm một điều kiện"

    def test_invalid_state_negative_changes_no_input(self, cases):
        s = by_key(cases, "T01>T04>T05>T06>!NT08", "negative")["steps"][-1]
        assert s["context_before"]["order_status"] == "PAID"
        assert s["violation"]["technique"] == "STATE_OR_CONTEXT_VIOLATION" and s["violation"]["changed_variables"] == []

    def test_input_validation_negative(self, cases):
        s = by_key(cases, "!NT14", "negative")["steps"][-1]
        assert s["expected"]["http_status"] == 422 and s["violation"]["technique"] == "EP_BVA_INPUT_VIOLATION"
        lengths = {k: s["request_data"][k] for k in ("customer_name_length", "phone_length", "address_length")}
        assert not (2 <= lengths["customer_name_length"] <= 100 and 9 <= lengths["phone_length"] <= 15
                    and 5 <= lengths["address_length"] <= 255)
        assert len(s["request"]["body"]["customer_name"]) == lengths["customer_name_length"]

    def test_off_domain_enum_negative(self, mapping, cases):
        s = by_key(cases, "T01>T04>T05>!NT12", "negative")["steps"][-1]
        assert s["request"]["body"]["simulate_result"] == mapping.data["off_domain_literal"]
        assert s["expected"]["http_status"] == 400
        s13 = by_key(cases, "!NT13", "negative")["steps"][-1]
        assert s13["request"]["body"]["payment_method"] == mapping.data["off_domain_literal"]

    def test_negative_http_status_comes_from_efsm(self, bm, cases):
        efsm = {n["id"]: n["expected"]["http_status"] for n in bm.model["negative_transitions"]}
        for c in self.negatives(cases):
            s = c["steps"][-1]
            assert s["expected"]["http_status"] == efsm[s["negative_transition_id"]]

    def test_state_and_data_unchanged_and_no_side_effects(self, cases):
        for c in self.negatives(cases):
            s = c["steps"][-1]
            assert s["state_after"] == s["state_before"] and s["data_after"] == s["data_before"]
            assert s["expected"]["state_unchanged"] and s["expected"]["data_unchanged"]
            assert s["expected"]["state_after"] == s["state_before"] == c["expected_final_state"]
            probes = {a["probe"]["path"] for a in c["assertions"] if "probe" in a}
            assert {"/api/cart", "/api/products", "/api/payments"} <= probes
            assert c["expected_final_data"] == s["data_before"]

    def test_fixture_negative_is_partially_ready_and_described_only(self, cases):
        c = by_key(cases, "!NT04", "negative")
        assert c["readiness"] == "PARTIALLY_READY" and c["readiness_reasons"] == ["REQUIRES_FIXTURE"]
        fixture = next(s for s in c["steps"] if s["kind"] == "FIXTURE")
        assert fixture["via_api"] is False and fixture["status"] == "REQUIRES_FIXTURE"
        assert fixture["value"] < c["steps"][-1]["context_before"]["ordered_quantity"]
        assert c["steps"].index(fixture) < len(c["steps"]) - 1
        assert c["cleanup"][0]["via_api"] is False

    def test_unevaluable_isolation_is_not_ready(self, cases):
        c = by_key(cases, "!NT03", "negative")
        assert c["readiness"] == "PARTIALLY_READY" and c["steps"][-1]["violation"]["isolation"]["status"] == "UNEVALUATED"

    def test_request_data_contains_only_needed_keys(self, cases):
        assert by_key(cases, "T01>T04>!NT06", "negative")["steps"][-1]["request_data"] == {"payment_result": "SUCCESS"}
        assert by_key(cases, "T01>T04>T05>T06>!NT08", "negative")["steps"][-1]["request_data"] == {}


class TestEdgeCases:
    def test_missing_context_no_order_exists(self, bm, mapping):
        res = PathDataGenerator(bm, mapping).convert_path(synthetic_path("T05"))
        assert res["readiness"] == Readiness.BLOCKED and res["reason_code"] == "NO_FEASIBLE_DATA_FOUND"
        assert "STATE_MISMATCH" in res["reason"]

    def test_no_valid_data_is_bounded_not_proof(self, bm, mapping):
        res = PathDataGenerator(bm, mapping).convert_path(synthetic_path("T01>T10>T04"))
        assert res["readiness"] == Readiness.BLOCKED
        assert res["evidence"] == "SEARCH_EXHAUSTED" and "not a proof" in res["proof_scope"]

    def test_search_budget_is_reported_as_budget_not_unsat(self, bm, mapping):
        res = PathDataGenerator(bm, mapping, max_nodes=2).convert_path(synthetic_path("T01>T01>T04>T05>T07>T07>T06"))
        assert res["readiness"] == Readiness.BLOCKED and res["reason_code"] == "SEARCH_BUDGET_EXHAUSTED"

    def test_contradictory_context_zero_stock(self, bm, mapping):
        data = copy.deepcopy(mapping.data)
        for p in data["catalog"]["products"]:
            p["stock"] = 0
        res = PathDataGenerator(bm, ApiMapping(data)).convert_path(synthetic_path("T01"))
        assert res["readiness"] == Readiness.BLOCKED and "UNSAT" in res["reason"]

    def test_unsupported_action_gives_unresolved_context(self, tmp_path, mapping):
        model = json.loads(BLUEMOON_MODEL.read_text(encoding="utf-8"))
        model["variables"].append({"name": "loyalty_points", "type": "integer", "scope": "cart", "origin": "derived",
                                   "description": "biến mới mà ShadowState không mô phỏng"})
        t01 = next(t for t in model["transitions"] if t["id"] == "T01")
        t01["actions"].insert(0, {"type": "assign", "var": "loyalty_points", "expr": "quantity"})
        t01["variables"]["action_writes"].append("loyalty_points")
        t01["variables"]["action_reads"].append("quantity")
        path = tmp_path / "efsm_model.json"
        path.write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
        gen = TestDataGenerator(path, BLUEMOON_RULES)
        res = PathDataGenerator(gen, mapping).convert_path(synthetic_path("T01"))
        assert res["readiness"] == Readiness.BLOCKED and res["reason_code"] == "UNRESOLVED_CONTEXT"
        assert "loyalty_points" in res["reason"]

    def test_unknown_transition_is_unsupported(self, bm, mapping):
        res = PathDataGenerator(bm, mapping).convert_path(synthetic_path("T99"))
        assert res["readiness"] == Readiness.UNSUPPORTED and res["reason_code"] == "UNKNOWN_TRANSITION"

    def test_transition_without_api_mapping_is_unsupported(self, bm, mapping):
        data = copy.deepcopy(mapping.data)
        data["transitions"]["T10"]["implemented_by"] = "DATABASE"
        res = PathDataGenerator(bm, ApiMapping(data)).convert_path(synthetic_path("T10"))
        assert res["readiness"] == Readiness.UNSUPPORTED and res["reason_code"] == "NO_API_MAPPING"

    def test_long_path(self, cases):
        c = by_key(cases, "T01>T01>T04>T05>T07>T07>T06", "positive")
        assert c["readiness"] == "READY" and sum(s["role"] == "path" for s in c["steps"]) == 7

    def test_self_loop_accumulates_quantity(self, cases):
        c = by_key(cases, "T01>T01>T04>T05>T07>T07>T06", "positive")
        t01a, t01b = c["steps"][0], c["steps"][1]
        assert t01a["request"]["body"]["product_id"] == t01b["request"]["body"]["product_id"]
        assert t01b["context_before"]["current_qty"] == t01a["request_data"]["quantity"]
        assert t01b["context_after"]["current_qty"] == t01a["request_data"]["quantity"] + t01b["request_data"]["quantity"]
        assert t01b["context_after"]["cart_items_count"] == 1  # dòng đã có: không tăng cart_items_count
        assert t01b["data_after"]["cart_items"][0]["quantity"] == t01b["context_after"]["current_qty"]

    def test_multi_product_cart_and_remove_one_line(self, bm, mapping):
        res = PathDataGenerator(bm, mapping).convert_path(synthetic_path("T03>T04>T05"))
        assert res["readiness"] is None  # có kế hoạch
        plan = res["plan"]
        setup = [s for s in plan.steps if s["role"] == "setup"]
        assert len({s["request"]["body"]["product_id"] for s in setup}) == 2
        order = plan.shadow.orders[plan.shadow.active]
        assert len(order["items"]) == 1 and order["items"][0]["product_id"] != plan.steps[2]["request"]["path_params"]["product_id"]

    def test_each_cart_line_must_satisfy_create_order_guard(self, bm, mapping):
        """T04 áp guard cho TỪNG dòng giỏ: một dòng vượt tồn kho thì không được lập kế hoạch."""
        planner = PathDataGenerator(bm, mapping)
        plan = planner._initial()
        plan.shadow.cart_add(1, 2)
        assert list(planner._candidates(plan, "T04", "path"))  # một dòng hợp lệ: được
        plan.shadow.cart_add(2, mapping.products[2]["stock"] + 1)  # dòng thứ hai vượt tồn kho
        assert list(planner._candidates(plan, "T04", "path")) == []
        assert any(f["code"] == "GUARD_NOT_SATISFIED" and "dòng giỏ hàng" in f["detail"] for f in planner._failures)

    def test_multiple_payment_attempts(self, cases):
        c = by_key(cases, "T01>T04>T05>T07>T07>T06", "positive")
        pays = [s for s in c["steps"] if s["transition_id"] in ("T06", "T07")]
        assert [s["transition_id"] for s in pays] == ["T07", "T07", "T06"]
        assert len({s["captures"]["payment_id"] for s in pays}) == 3
        final = c["expected_final_data"]
        assert [p["result"] for p in final["payments"]] == ["FAILED", "FAILED", "SUCCESS"]
        assert pays[-1]["context_after"]["payment_amount"] == pays[-1]["context_before"]["order_total"]

    def test_database_generated_ids_are_never_literal(self, cases):
        for c in cases:
            captured = {}
            for s in c["steps"]:
                if s["kind"] == "FIXTURE":
                    continue
                for name, value in s["request"]["path_params"].items():
                    if name == "order_id":
                        assert isinstance(value, str) and value.startswith("$ref:steps.")
                        n = int(value.split(".")[1])
                        assert n < s["step_no"] and "order_id" in c["steps"][n - 1]["captures"]
                captured.update(s["captures"])
            for step in c["steps"]:
                for check in step.get("expected", {}).get("response_checks", []):
                    if check["target"].endswith(".id"):
                        assert isinstance(check["value"], str)

    def test_shadow_simulation_defect_is_detected_by_evaluator(self, bm, mapping, monkeypatch):
        """Mutation: nếu mô phỏng hủy đơn không hoàn kho thì phải bị đối chiếu với action EFSM phát hiện."""
        monkeypatch.setattr(ShadowState, "order_cancel", lambda self: self.orders[self.active].update(status="CANCELLED"))
        res = PathDataGenerator(bm, mapping).convert_path(synthetic_path("T04>T08"))
        assert res["readiness"] == Readiness.BLOCKED and res["reason_code"] == "UNRESOLVED_CONTEXT"

    def test_shadow_defect_in_order_total_is_detected(self, bm, mapping, monkeypatch):
        original = ShadowState.order_create

        def wrong(self, ref):
            original(self, ref)
            self.orders[ref]["total"] += 1

        monkeypatch.setattr(ShadowState, "order_create", wrong)
        res = PathDataGenerator(bm, mapping).convert_path(synthetic_path("T04"))
        assert res["readiness"] == Readiness.BLOCKED and res["reason_code"] == "UNRESOLVED_CONTEXT"


class TestSelectionAndOutput:
    def test_summary_numbers_are_consistent(self, generated, paths_doc):
        out, cov = generated
        s = out["summary"]
        assert s["input_paths"] == len(paths_doc["paths"]) == 171
        assert s["test_cases"] == len(out["test_cases"]) == s["positive_cases"] + s["negative_cases"]
        assert s["test_cases"] + s["unconverted_paths"] == s["input_paths"]
        assert s["readiness"]["READY"] + s["readiness"]["PARTIALLY_READY"] == s["test_cases"]
        assert s["readiness"]["BLOCKED"] + s["readiness"]["UNSUPPORTED"] + sum(
            1 for u in out["unconverted_paths"] if u["readiness"] == "NOT_SELECTED") == s["unconverted_paths"]
        assert cov["summary"] == s

    def test_blocked_paths_are_reported_with_reason(self, generated):
        out = generated[0]
        assert out["unconverted_paths"] and all(u["reason_code"] and u["reason"] for u in out["unconverted_paths"])
        assert all(u["reason_code"] == "NO_FEASIBLE_DATA_FOUND" and u["evidence"] == "SEARCH_EXHAUSTED"
                   for u in out["unconverted_paths"] if u["readiness"] == "BLOCKED")

    def test_ids_unique_and_ordered(self, cases):
        ids = [c["test_case_id"] for c in cases]
        assert ids == [f"TC{i:03d}" for i in range(1, len(cases) + 1)]
        assert len({c["source_path_id"] for c in cases}) == len(cases)

    def test_schema_fields(self, generated):
        out = generated[0]
        assert out["schema_version"] == "1.0" and out["generator"] == "PathAwareTestDataGenerator"
        for c in out["test_cases"]:
            for key in ("test_case_id", "source_path_id", "type", "readiness", "setup", "steps", "assertions", "cleanup",
                        "security_rule_ids", "invariant_checkpoints", "invariant_assertions", "execution", "symbolic_refs",
                        "expected_final_state", "expected_final_data", "initial_data"):
                assert key in c, key
            assert c["readiness"] in ("READY", "PARTIALLY_READY")

    def test_max_cases_prefers_negative_and_reports_rest(self, bm, mapping, paths_doc):
        out, _ = generate_test_cases(bm, mapping, paths_doc, max_cases=8)
        assert len(out["test_cases"]) == 8 and all(c["type"] == "negative" for c in out["test_cases"])
        reasons = {u["reason_code"] for u in out["unconverted_paths"]}
        assert "NOT_SELECTED_MAX_CASES" in reasons

    def test_max_cases_prefers_new_transition_coverage(self, bm, mapping, paths_doc):
        out, _ = generate_test_cases(bm, mapping, paths_doc, max_cases=30, include_negative=False)
        covered = {s["transition_id"] for c in out["test_cases"] for s in c["steps"] if s["role"] == "path"}
        assert covered == {t["id"] for t in bm.model["transitions"]}

    def test_duplicates_are_dropped(self, bm, mapping, paths_doc):
        doc = copy.deepcopy(paths_doc)
        clone = copy.deepcopy(next(p for p in doc["paths"] if p["path_key"] == "T10"))
        clone["path_id"] = "P999"
        doc["paths"].append(clone)
        out, _ = generate_test_cases(bm, mapping, doc)
        assert any(u["reason_code"] == "DUPLICATE" and u["source_path_id"] == "P999" for u in out["unconverted_paths"])

    def test_request_signature_is_stable_and_discriminating(self, cases):
        assert request_signature(cases[0]) == request_signature(copy.deepcopy(cases[0]))
        assert request_signature(cases[0]) != request_signature(cases[1])

    def test_rules_are_linked(self, generated, bm):
        out = generated[0]
        assert out["summary"]["rules_linked"] == len(out["rules_index"]) >= 20
        assert set(out["rules_index"]) <= set(bm.rules)

    def test_coverage_report(self, generated, bm):
        cov = generated[1]
        assert cov["transition_coverage"]["not_covered"] == []
        assert cov["negative_transition_coverage"]["not_covered"] == []
        assert cov["negative_isolation"]["ISOLATED"] + cov["negative_isolation"]["UNEVALUATED"] + cov["negative_isolation"]["NOT_ISOLATED"] == 20
        assert len(cov["fixture_steps"]) == 1 and cov["fixture_steps"][0]["step_no"] >= 1

    def test_invariants_never_claimed_verified_at_runtime(self, generated, cases):
        cov = generated[1]
        assert all(v["runtime_status"] == "NOT_VERIFIED_UNTIL_EXECUTION" for v in cov["invariants"]["per_invariant"].values())
        inv06 = cov["invariants"]["per_invariant"]["INV-06"]
        assert inv06["kind"] == "model_structure" and inv06["simulated_basis"] == "MODEL_STRUCTURE" and "NT08" in inv06["covered_by"]
        assert cov["invariants"]["per_invariant"]["INV-03"]["simulated_basis"] == "EFSM_SIMULATED_DATA"
        assert all(v["simulated_checkpoints"]["VIOLATED"] == 0 for v in cov["invariants"]["per_invariant"].values())
        for c in cases:
            for cp in c["invariant_checkpoints"]:
                assert cp["runtime_status"] == "NOT_VERIFIED_UNTIL_EXECUTION" and cp["simulated_violations"] == []
            assert all(a["status"] == "NOT_EXECUTED" for a in c["invariant_assertions"])

    def test_inputs_are_not_modified_and_output_is_reproducible(self, bm, mapping, paths_doc):
        before_doc, before_model = copy.deepcopy(paths_doc), copy.deepcopy(bm.model)
        before_map = copy.deepcopy(mapping.data)
        first = json.dumps(generate_test_cases(bm, mapping, paths_doc)[0], sort_keys=False, default=str)
        second = json.dumps(generate_test_cases(bm, mapping, paths_doc)[0], sort_keys=False, default=str)
        assert first == second
        assert paths_doc == before_doc and bm.model == before_model and mapping.data == before_map

    def test_delivered_files_match_generator_output(self, generated):
        delivered = json.loads((ROOT / "test_cases.json").read_text(encoding="utf-8"))
        assert delivered["test_cases"] == json.loads(json.dumps(generated[0]["test_cases"]))
        assert delivered["sources"]["test_paths"]["sha256"] == hashlib.sha256(PATHS_FILE.read_bytes()).hexdigest()
        cov = json.loads((ROOT / "test_data_coverage.json").read_text(encoding="utf-8"))
        assert cov["summary"] == json.loads(json.dumps(generated[1]["summary"]))


class TestCoreExtensionAndScope:
    def test_off_domain_makes_enum_violation_solvable(self, tmp_path):
        model = fixture_model()
        guard = leaf("mode", "in", ["A", "B", "C"])
        model["transitions"].append({"id": "GE", "source": "S0", "target": "S1", "event": "e_ge", "guard": guard,
                                     "actions": [], "rule_refs": []})
        path = tmp_path / "m.json"
        path.write_text(json.dumps(model), encoding="utf-8")
        g = TestDataGenerator(path)
        plain = g.solve(guard, want_violation=True)
        off = g.solve(guard, want_violation=True, allow_off_domain=True)
        assert plain.status == SolveStatus.UNSAT
        assert off.status == SolveStatus.SAT and off.assignment == {"mode": OFF_DOMAIN_VALUE}
        assert g.solve(guard, want_violation=True).status == SolveStatus.UNSAT  # cờ không bị "rò" sang lần gọi sau

    def test_part1_guard_data_is_byte_identical(self, tmp_path):
        out = tmp_path / "g.json"
        assert main(["--model", str(BLUEMOON_MODEL), "--context", str(BLUEMOON_CONTEXT), "--output", str(out)]) == 0
        assert out.read_bytes() == (ROOT / "generated_guard_data.json").read_bytes()

    def test_module_has_no_http_or_database_imports(self):
        source = (ROOT / "test_data_generator.py").read_text(encoding="utf-8")
        tree = __import__("ast").parse(source)
        imported = set()
        for node in __import__("ast").walk(tree):
            if isinstance(node, __import__("ast").Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, __import__("ast").ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & {"requests", "httpx", "urllib", "socket", "http", "fastapi", "sqlalchemy", "sqlite3", "app", "subprocess"}

    def test_baseline_files_are_untouched(self):
        # sha256 của các file baseline tại thời điểm bàn giao Phần 1 (không được đổi ở Phần 2)
        expected = json.loads((ROOT / "tests" / "baseline_hashes.json").read_text(encoding="utf-8"))
        for name, digest in expected.items():
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name

    def test_cli_path_mode(self, tmp_path):
        tc, cov = tmp_path / "tc.json", tmp_path / "cov.json"
        code = main(["--model", str(BLUEMOON_MODEL), "--test-paths", str(PATHS_FILE), "--test-cases-output", str(tc),
                     "--coverage-output", str(cov), "--max-cases", "12"])
        assert code == 0
        assert json.loads(tc.read_text(encoding="utf-8"))["summary"]["test_cases"] == 12 and cov.exists()

    def test_cli_rejects_bad_test_paths(self, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("{\"nope\": 1}", encoding="utf-8")
        assert main(["--model", str(BLUEMOON_MODEL), "--test-paths", str(bad),
                     "--test-cases-output", str(tmp_path / "a.json"), "--coverage-output", str(tmp_path / "b.json")]) == 2
        assert "ERROR" in capsys.readouterr().err
