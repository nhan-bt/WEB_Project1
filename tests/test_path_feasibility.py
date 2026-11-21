"""Kiểm thử hồi quy cho phân loại tính khả thi đường đi EFSM (efsm_feasibility, feasibility_path_generator,
và phần phân loại / backtracking của test_data_generator.PathDataGenerator).

Không gọi API, không dùng database: chỉ mô hình EFSM, api_test_mapping.json và mô phỏng ShadowState.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from efsm_feasibility import EfsmPathProver, FeasibilityVerdict
from feasibility_path_generator import FeasibilityAwarePathGenerator
from guard_evaluator import GuardEvaluator, GuardStatus
from test_data_generator import ApiMapping, PathDataGenerator, Readiness, ShadowState, TestDataGenerator, generate_test_cases
from test_path_generator import Feasibility, PathContext, TestPathGenerator

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "efsm_model.json"
RULES = ROOT / "security_rules.json"
MAPPING = ROOT / "api_test_mapping.json"
PATHS = ROOT / "test_paths.json"
CONTEXT = ROOT / "test_path_context.bluemoon.json"


@pytest.fixture(scope="module")
def ev():
    return GuardEvaluator(MODEL)


@pytest.fixture(scope="module")
def mapping():
    return ApiMapping.load(MAPPING)


@pytest.fixture(scope="module")
def prover(ev, mapping):
    return EfsmPathProver.from_api_mapping(ev, mapping.data, ShadowState.LINE_VARIABLES)


@pytest.fixture(scope="module")
def datagen():
    return TestDataGenerator(MODEL, RULES)


@pytest.fixture()
def planner(datagen, mapping):
    return PathDataGenerator(datagen, mapping)


@pytest.fixture(scope="module")
def paths_doc():
    return json.loads(PATHS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def generated(datagen, mapping, paths_doc):
    return generate_test_cases(datagen, mapping, paths_doc, paths_file=PATHS.name)


def synthetic(key, ptype="positive", negative_id=None):
    tids = [t for t in key.split(">") if not t.startswith("!")]
    path = {"path_id": "X001", "path_type": ptype, "path_key": key, "transition_ids": tids, "feasibility": "UNKNOWN",
            "security_rule_ids": [], "severity": "HIGH", "steps": [{"kind": "transition", "transition_id": t} for t in tids]}
    if negative_id:
        path["negative_transition_id"] = negative_id
    return path


def single_product_mapping(mapping, stock=4, hint=3):
    """Catalog một sản phẩm, gợi ý quantity lớn: lựa chọn đầu vào đầu tiên làm bước sau thất bại."""
    data = copy.deepcopy(mapping.data)
    product = dict(data["catalog"]["products"][0], stock=stock)
    data["catalog"]["products"] = [product]
    data["catalog"]["primary_product"] = product["id"]
    data["input_hints"]["quantity"] = hint
    return ApiMapping(data, "<single-product>")


# ---------------------------------------------------------------------------
# TEST 01 - positive path hợp lệ
# ---------------------------------------------------------------------------
class TestValidPositivePath:
    def test_add_then_order_is_feasible(self, prover, planner):
        assert prover.prove(["T01", "T04"]).proven_infeasible is False
        converted = planner.convert_path(synthetic("T01>T04"))
        assert converted["readiness"] is None and converted["plan"] is not None
        assert converted["proof_conflict"] is None
        roles = [s["role"] for s in converted["plan"].steps]
        assert roles == ["path", "path"]  # không cần setup, không chèn transition vào path
        assert [s["transition_id"] for s in converted["plan"].steps] == ["T01", "T04"]


# ---------------------------------------------------------------------------
# TEST 02 - positive path không khả thi (T01 > T10 > T04)
# ---------------------------------------------------------------------------
class TestProvenInfeasible:
    def test_clear_before_order_is_proven(self, prover):
        proof = prover.prove(["T01", "T10", "T04"])
        assert proof.proven_infeasible and proof.kind == "GUARD_CONTRADICTION"
        assert (proof.failed_step, proof.transition_id) == (3, "T04")
        assert {"var": "cart_items_count", "op": ">", "value": 0} in proof.failed_conditions
        assert proof.context_before["cart_items_count"] == 0
        assert proof.provenance["cart_items_count"] == {"step": 2, "transition_id": "T10"}

    def test_data_generator_labels_it_proven(self, planner):
        out = planner.convert_path(synthetic("T01>T10>T04>T05>T06"))
        assert out["readiness"] == Readiness.BLOCKED
        assert out["feasibility_verdict"] == FeasibilityVerdict.PROVEN_INFEASIBLE
        assert out["evidence"] == "PROVEN_INFEASIBLE"
        assert out["reason_code"] == "NO_FEASIBLE_DATA_FOUND"  # mã cũ giữ nguyên cho executor/validator
        rep = out["feasibility_report"]
        assert rep["failed_step"] == 3 and rep["transition_id"] == "T04" and rep["caused_by"]
        assert rep["observed_by_search"]["failed_step"] == 3  # tìm kiếm cụ thể cũng dừng đúng bước đó

    def test_selection_on_empty_cart_reports_first_failing_step(self, prover, planner):
        proof = prover.prove(["T10", "T02", "T04"])
        assert proof.proven_infeasible and (proof.failed_step, proof.transition_id, proof.kind) == (2, "T02", "EMPTY_CART_SELECTION")
        out = planner.convert_path(synthetic("T10>T02>T04"))
        assert out["feasibility_report"]["failed_step"] == 2
        assert "bước 2 của path (T02)" in out["reason"]  # trước đây: "không có bước nào khả thi"


# ---------------------------------------------------------------------------
# TEST 03 - tạo đơn khi giỏ rỗng
# ---------------------------------------------------------------------------
class TestEmptyCartOrder:
    def test_positive_is_rejected_negative_is_generated(self, prover, planner):
        assert prover.prove(["T10", "T04"]).proven_infeasible
        assert planner.convert_path(synthetic("T10>T04"))["feasibility_verdict"] == FeasibilityVerdict.PROVEN_INFEASIBLE
        neg = planner.convert_path(synthetic("T10>!NT03", "negative", "NT03"))
        assert neg["readiness"] is None, neg
        last = neg["plan"].steps[-1]
        assert last["role"] == "negative" and last["negative_transition_id"] == "NT03"
        assert last["expected"]["outcome"] == "REJECT" and last["expected"]["http_status"] == 400
        assert last["data_before"] == last["data_after"]  # action của T04 không được áp dụng

    def test_order_without_known_cart_is_not_proven(self, prover):
        # giỏ ban đầu chưa biết (có thể có setup) -> không được kết luận vô nghiệm
        assert prover.prove(["T04"]).proven_infeasible is False


# ---------------------------------------------------------------------------
# TEST 04 - dữ liệu ngoài miền hợp lệ
# ---------------------------------------------------------------------------
class TestOutOfDomainData:
    BASE = {"cart_items_count": 1, "ordered_quantity": 2, "stock": 10, "customer_name_length": 10,
            "phone_length": 10, "address_length": 16}

    def test_guard_detects_out_of_domain_payment_method(self, ev, prover):
        variables = {**self.BASE, "payment_method": "PAYPAL"}
        assert ev.check_transition("T04", "CART", variables).status is GuardStatus.GUARD_NOT_SATISFIED
        failed: list = []
        assert prover.evaluate(ev.get_transition("T04")["guard"], variables, failed) is False
        assert failed == [{"var": "payment_method", "op": "in", "value": ["MOCK", "COD"]}]

    def test_positive_inputs_stay_in_domain(self, planner):
        ctx = {**self.BASE, "order_items": [{"quantity": 2, "unit_price": 150000}]}
        planner._extended = True
        options, _ = planner._input_options("T04", ctx)
        assert options and all(o["payment_method"] in ("MOCK", "COD") for o in options)
        assert all(2 <= o["customer_name_length"] <= 100 for o in options)


# ---------------------------------------------------------------------------
# TEST 05 + 07 - backtracking theo dữ liệu / hết ngân sách không phải là vô nghiệm
# ---------------------------------------------------------------------------
class TestBacktrackingAndSearchExhausted:
    PATH = "T01>T01>T01"  # tồn kho 4, gợi ý 3: lựa chọn đầu tiên (3) làm bước 3 thất bại

    def test_without_data_backtracking_search_is_exhausted_not_proven(self, prover, datagen, mapping):
        tight = single_product_mapping(mapping)
        planner = PathDataGenerator(datagen, tight, max_input_options=1)
        assert prover.prove(self.PATH.split(">")).proven_infeasible is False
        out = planner.convert_path(synthetic(self.PATH))
        assert out["readiness"] == Readiness.BLOCKED
        assert out["feasibility_verdict"] == FeasibilityVerdict.SEARCH_EXHAUSTED
        assert out["evidence"] == "SEARCH_EXHAUSTED" and "not a proof" in out["proof_scope"]

    def test_backtracking_tries_other_input_values(self, datagen, mapping):
        tight = single_product_mapping(mapping)
        planner = PathDataGenerator(datagen, tight, max_input_options=4)
        out = planner.convert_path(synthetic(self.PATH))
        assert out["readiness"] is None, out
        assert [p["phase"] for p in out["meta"]["phases"]] == [1, 2]
        assert out["meta"]["phases"][0]["search"] == "EXHAUSTED" and out["meta"]["phases"][1]["search"] == "FOUND"
        quantities = [s["request_data"]["quantity"] for s in out["plan"].steps if s["role"] == "path"]
        assert quantities[0] != 3 and sum(quantities) <= 4  # lựa chọn ban đầu (3) đã được thay

    def test_budget_exhausted_is_never_proven(self, planner):
        planner.max_nodes = 0
        out = planner.convert_path(synthetic("T01>T02>T04"))
        assert out["reason_code"] == "SEARCH_BUDGET_EXHAUSTED"
        assert out["feasibility_verdict"] == FeasibilityVerdict.SEARCH_EXHAUSTED


# ---------------------------------------------------------------------------
# TEST 06 - cô lập nhánh
# ---------------------------------------------------------------------------
class TestBranchIsolation:
    def test_prover_step_does_not_mutate_parent(self, prover):
        parent = {"cart_items_count": 2, "order_status": "CONFIRMED"}
        prov = {"cart_items_count": {"step": 1, "transition_id": "T01"}}
        snapshot = (copy.deepcopy(parent), copy.deepcopy(prov))
        a, _, _ = prover.step(parent, prov, "T10", 2)
        b, _, _ = prover.step(parent, prov, "T02", 2)
        assert (parent, prov) == snapshot
        assert a["cart_items_count"] == 0 and b["cart_items_count"] == 2

    def test_plan_branches_are_independent(self, planner):
        start = planner._initial()
        branches = list(planner._candidates(start, "T01", "path"))
        assert len(branches) >= 2
        assert branches[0].shadow.cart != branches[1].shadow.cart
        assert start.shadow.cart == {} and start.steps == []
        branches[0].shadow.cart_clear()
        assert branches[1].shadow.cart  # nhánh khác không bị ảnh hưởng


# ---------------------------------------------------------------------------
# TEST 08 - bảo toàn negative test
# ---------------------------------------------------------------------------
class TestNegativePreservation:
    def test_all_negative_transitions_still_have_test_cases(self, generated, ev):
        cases = [c for c in generated[0]["test_cases"] if c["type"] == "negative"]
        assert {c["negative_transition_id"] for c in cases} == set(ev.negative_transition_ids)
        for case in cases:
            last = case["steps"][-1]
            assert last["role"] == "negative" and last["expected"]["outcome"] == "REJECT"
            assert last["data_before"] == last["data_after"]
            assert all(s["guard_check"]["status"] == "GUARD_SATISFIED" for s in case["steps"][:-1] if s["kind"] == "transition")

    def test_prune_mode_keeps_every_negative_path(self):
        gen = FeasibilityAwarePathGenerator(MODEL, RULES, api_mapping=MAPPING, dfs_feasibility="prune")
        result = gen.generate(strategy="both", include_negative=True, context=PathContext.from_file(CONTEXT))
        assert sum(p.is_negative for p in result.paths) == 20
        assert all(p.extra["feasibility_proof"]["status"] == "NOT_PROVEN_INFEASIBLE" for p in result.paths if not p.is_negative)
        assert "T10>T04" in {x["path_key"] for x in gen.pruned_extensions}


# ---------------------------------------------------------------------------
# TEST 09 - guard trước action; context cập nhật đúng; input không ghi đè state
# ---------------------------------------------------------------------------
class TestGuardActionConsistency:
    def test_guard_uses_pre_action_context(self, prover):
        known, _, proof = prover.step({}, {}, "T10", 1)
        assert proof is None and known == {"cart_items_count": 0, "current_qty": 0, "cart_item_exists": False}
        _, _, proof = prover.step(known, {}, "T04", 2)
        assert proof.proven_infeasible and proof.context_before["cart_items_count"] == 0

    def test_conditional_action_with_unknown_condition_loses_information(self, prover):
        known, _, proof = prover.step({"cart_items_count": 0}, {}, "T01", 1)
        assert proof is None and "cart_items_count" not in known  # when current_qty == 0 chưa biết -> TOP
        assert known["cart_item_exists"] is True

    def test_context_input_cannot_override_state_written_by_action(self):
        ctx = PathContext.from_dict({
            "initial_variables": {"product_exists": True, "cart_items_count": 0, "cart_item_exists": False,
                                  "current_qty": 0, "stock": 10, "unit_price": 150000},
            "step_inputs": {"T01": {"quantity": 2},
                            "T04": {"cart_items_count": 0, "ordered_quantity": 2, "payment_method": "MOCK",
                                    "customer_name_length": 10, "phone_length": 10, "address_length": 16,
                                    "order_items": [{"quantity": 2, "unit_price": 150000}]}}})
        original = TestPathGenerator(MODEL).evaluate_path_feasibility(["T01", "T04"], ctx)
        assert original.status is Feasibility.INFEASIBLE  # D1: input ghi đè cart_items_count := 0
        fixed = FeasibilityAwarePathGenerator(MODEL, api_mapping=MAPPING).evaluate_path_feasibility(["T01", "T04"], ctx)
        assert fixed.status is Feasibility.FEASIBLE
        step = fixed.steps[1]
        assert step["rejected_input_overrides"][0]["variable"] == "cart_items_count"
        assert step["rejected_input_overrides"][0]["state_value"] == 1

    def test_line_variables_match_shadow_projection(self, mapping):
        sh = ShadowState(mapping.products)
        sh.cart_add(1, 2)
        a, b = sh.project(1, "cart"), sh.project(2, "cart")
        changed = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
        assert changed <= ShadowState.LINE_VARIABLES


# ---------------------------------------------------------------------------
# TEST 10 - hồi quy so với kết quả đã bàn giao
# ---------------------------------------------------------------------------
class TestRegression:
    def test_test_cases_unchanged_and_classified(self, generated):
        delivered = json.loads((ROOT / "test_cases.json").read_text(encoding="utf-8"))
        out = generated[0]
        assert out["test_cases"] == json.loads(json.dumps(delivered["test_cases"]))
        assert [u["source_path_id"] for u in out["unconverted_paths"]] == [u["source_path_id"] for u in delivered["unconverted_paths"]]
        fs = out["feasibility_summary"]
        assert fs["by_verdict"] == {"FEASIBLE": 122, "PROVEN_INFEASIBLE": 49, "SEARCH_EXHAUSTED": 0,
                                    "GENERATOR_ERROR": 0, "MODEL_ERROR": 0}
        assert fs["proof_conflicts"] == []
        assert len(out["feasibility_index"]) == 171

    def test_prover_never_contradicts_a_found_plan(self, generated, prover):
        for case in generated[0]["test_cases"]:
            path = [s["transition_id"] for s in case["steps"] if s.get("role") == "path" and s["kind"] == "transition"]
            assert prover.prove(path).proven_infeasible is False, case["path_key"]

    def test_annotate_mode_keeps_original_paths(self, paths_doc):
        gen = FeasibilityAwarePathGenerator(MODEL, RULES, api_mapping=MAPPING, dfs_feasibility="annotate")
        result = gen.generate(strategy="both", include_negative=True, context=PathContext.from_file(CONTEXT),
                              max_depth=8, max_paths=200)
        doc = json.loads(json.dumps(gen.to_output_dict(result), default=str))
        assert [{k: v for k, v in p.items() if k != "feasibility_proof"} for p in doc["paths"]] == paths_doc["paths"]
        assert doc["summary"]["paths_by_proof"]["positive"] == {"PROVEN_INFEASIBLE": 49, "NOT_PROVEN_INFEASIBLE": 102}

    def test_off_mode_needs_no_mapping(self):
        gen = FeasibilityAwarePathGenerator(MODEL, dfs_feasibility="off")
        result = gen.generate(strategy="bfs", include_negative=False)
        assert all("feasibility_proof" not in p.extra for p in result.paths)
        with pytest.raises(ValueError):
            FeasibilityAwarePathGenerator(MODEL, dfs_feasibility="annotate")
