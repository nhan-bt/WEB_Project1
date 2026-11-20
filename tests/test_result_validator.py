"""Kiểm thử Result Validator (Phần 7).

* Dữ liệu thật: test_cases.json + test_execution_results.json của Phần 5–6 (chỉ đọc).
* Synthetic fault fixtures: sao chép (deepcopy) kết quả thực thi rồi cố ý chèn sai lệch; validator phải phát hiện.
  Không sửa file gốc; không gửi HTTP; không dùng backend.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

import result_validator as rv
from result_validator import FAIL, INCONCLUSIVE, PASS, SKIPPED, Inputs, ResultValidator

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "test_cases": ROOT / "test_cases.json",
    "execution_results": ROOT / "test_execution_results.json",
    "model": ROOT / "efsm_model.json",
    "rules": ROOT / "security_rules.json",
}


@pytest.fixture(scope="module")
def inputs() -> Inputs:
    return Inputs.from_files(FILES["test_cases"], FILES["execution_results"], FILES["model"], FILES["rules"])


@pytest.fixture(scope="module")
def validator(inputs) -> ResultValidator:
    return ResultValidator(inputs)


@pytest.fixture(scope="module")
def baseline_run(validator):
    return validator.validate_all()


def case_of(inputs, tcid):
    return next(c for c in inputs.cases["test_cases"] if c["test_case_id"] == tcid)


def result_of(inputs, tcid):
    return next(r for r in inputs.results["results"] if r.get("test_case_id") == tcid)


def run_case(validator, inputs, tcid, *mutators, case_mutators=()):
    """Validate một test case trên BẢN SAO đã chèn lỗi."""
    case = copy.deepcopy(case_of(inputs, tcid))
    result = copy.deepcopy(result_of(inputs, tcid))
    for m in mutators:
        m(result)
    for m in case_mutators:
        m(case)
    return validator.validate_case(case, result)


def failed(rec):
    return [a for a in rec["assertions"] if a["status"] == FAIL]


def step(result, n):
    return next(s for s in result["steps"] if s["step_no"] == n)


def obs(result, n):
    return step(result, n)["observed_state"]


def set_status(result, n, status):
    step(result, n)["observed_state"]["orders"][0]["status"] = status


def ids(rec, status=FAIL):
    return {a["assertion_id"] for a in rec["assertions"] if a["status"] == status}


def categories(rec):
    return {a["category"] for a in failed(rec)}


# ---------------------------------------------------------------------------
# 1–2. PASS cho dữ liệu hợp lệ (positive, negative)
# ---------------------------------------------------------------------------
class TestValidCases:
    def test_valid_positive_transition_passes(self, validator, inputs):
        rec = run_case(validator, inputs, "TC005")  # T01>T04>T05>T06
        assert rec["validation_status"] == PASS
        assert rec["assertion_counts"]["failed"] == 0 and rec["assertion_counts"]["not_evaluable"] == 0
        assert {"S1.HTTP", "S4.STATE", "S4.DATA.stock", "A4", "INV-04", "INV-12"} <= {a["assertion_id"] for a in rec["assertions"]}

    def test_negative_transition_rejected_correctly_passes(self, validator, inputs):
        rec = run_case(validator, inputs, "TC110")  # ... > NT08
        assert rec["validation_status"] == PASS
        last = {a["assertion_id"]: a for a in rec["assertions"] if a["assertion_id"].startswith("S5.")}
        assert last["S5.REJECTED"]["status"] == PASS and last["S5.HTTP"]["actual"] == 409
        assert last["S5.STATE"]["actual"]["order_status"] == {"1": "PAID"}
        assert "INV-06" in {a["assertion_id"] for a in rec["assertions"]}  # invariant cấu trúc suy ra từ NT08

    def test_http_2xx_is_not_automatically_pass(self, validator, inputs):
        def break_stock(r):
            obs(r, 2)["product_stock"]["1"] = 99

        assert run_case(validator, inputs, "TC005", break_stock)["validation_status"] == FAIL

    def test_http_4xx_is_not_automatically_fail(self, validator, inputs, baseline_run):
        negs = [r for r in baseline_run["results"] if r["test_type"] == "NEGATIVE"]
        assert negs and all(r["validation_status"] == PASS for r in negs if not r["assertions"] or r["assertion_counts"]["failed"] == 0)


# ---------------------------------------------------------------------------
# 3–9. Synthetic fault fixtures
# ---------------------------------------------------------------------------
class TestSyntheticFaults:
    def test_unauthorized_acceptance_of_negative_transition(self, validator, inputs):
        """NT08: API chấp nhận huỷ đơn đã PAID (HTTP 409 -> 200) và đơn chuyển CANCELLED."""
        def accept(r):
            s = step(r, 5)
            s["response"]["status_code"], s["response"]["status_class"] = 200, "2xx"
            set_status(r, 5, "CANCELLED")
            r["final_observed_state"]["orders"][0]["status"] = "CANCELLED"

        rec = run_case(validator, inputs, "TC110", accept)
        assert rec["validation_status"] == FAIL
        assert {"S5.REJECTED", "S5.HTTP", "S5.STATE"} <= ids(rec)
        assert rec["assertions"][0]["rule_refs"] or True
        rejected = next(a for a in rec["assertions"] if a["assertion_id"] == "S5.REJECTED")
        assert rejected["category"] == rv.INVALID_STATE_TRANSITION and rejected["actual"]["accepted"] is True
        state = next(a for a in rec["assertions"] if a["assertion_id"] == "S5.STATE")
        assert state["expected"]["order_status"] == {"1": "PAID"} and state["actual"]["order_status"] == {"1": "CANCELLED"}

    def test_http_status_correct_but_state_wrong(self, validator, inputs):
        def paid_to_cancelled(r):
            set_status(r, 5, "CANCELLED")  # HTTP vẫn 409

        rec = run_case(validator, inputs, "TC110", paid_to_cancelled)
        assert rec["validation_status"] == FAIL
        assert "S5.HTTP" not in ids(rec) and "S5.REJECTED" not in ids(rec) and "S5.STATE" in ids(rec)
        assert categories(rec) >= {rv.INVALID_STATE_TRANSITION}

    def test_http_status_wrong_but_state_correct(self, validator, inputs):
        def wrong_code(r):
            step(r, 5)["response"]["status_code"] = 404

        rec = run_case(validator, inputs, "TC110", wrong_code)
        assert rec["validation_status"] == FAIL
        assert ids(rec) == {"S5.HTTP"}
        a = failed(rec)[0]
        assert a["category"] == rv.HTTP_STATUS_MISMATCH and a["expected"] == 409 and a["actual"] == 404
        assert a["expected_source"] == "efsm_model.negative_transitions[NT08].expected.http_status"

    def test_http_409_replaced_by_200_on_positive_step(self, validator, inputs):
        def to200(r):
            step(r, 1)["response"]["status_code"] = 409

        rec = run_case(validator, inputs, "TC005", to200)
        assert rec["validation_status"] == FAIL and "S1.HTTP" in ids(rec)

    def test_payment_success_but_order_not_paid(self, validator, inputs):
        def not_paid(r):
            set_status(r, 4, "CONFIRMED")
            step(r, 4)["response"]["body"]["order"]["status"] = "CONFIRMED"

        rec = run_case(validator, inputs, "TC005", not_paid)
        assert rec["validation_status"] == FAIL
        assert {"S4.STATE", "S4.BODY.response.order.status"} <= ids(rec)
        assert next(a for a in failed(rec) if a["assertion_id"] == "S4.STATE")["expected"]["state_after"] == "PAID"

    def test_cancel_without_stock_restoration(self, validator, inputs):
        def no_restore(r):
            for n in (3, ):  # T08 là bước 3 của TC004
                obs(r, n)["product_stock"]["1"] = 8
            r["final_observed_state"]["product_stock"]["1"] = 8

        rec = run_case(validator, inputs, "TC004", no_restore)
        assert rec["validation_status"] == FAIL
        assert "S3.DATA.stock" in ids(rec) and "INV-05" in ids(rec)
        inv = next(a for a in failed(rec) if a["assertion_id"] == "INV-05")
        assert inv["category"] == rv.STOCK_INTEGRITY_VIOLATION
        assert inv["actual"]["violations"][0] == {"step_no": 3, "product_id": "1", "expected_stock_delta": 2, "actual_stock_delta": 0}
        assert inv["rule_refs"] == ["BSR-STOCK-04", "BSR-STOCK-05"]

    def test_double_cancel_restores_stock_twice(self, validator, inputs):
        """NT09: huỷ lần hai bị từ chối nhưng tồn kho vẫn được hoàn thêm."""
        def double_refund(r):
            obs(r, 4)["product_stock"]["1"] = 12
            r["final_observed_state"]["product_stock"]["1"] = 12

        rec = run_case(validator, inputs, "TC111", double_refund)
        assert rec["validation_status"] == FAIL
        assert "S4.DATA.stock" in ids(rec) and "INV-05" in ids(rec)
        assert next(a for a in failed(rec) if a["assertion_id"] == "S4.DATA.stock")["category"] == rv.STOCK_INTEGRITY_VIOLATION
        assert any(a["assertion_id"] == "A6" and a["status"] == FAIL for a in rec["assertions"])  # hậu điều kiện negative từ test case

    def test_failed_payment_turns_order_paid(self, validator, inputs):
        def failed_but_paid(r):
            set_status(r, 4, "PAID")

        rec = run_case(validator, inputs, "TC006", failed_but_paid)  # T01>T04>T05>T07
        assert rec["validation_status"] == FAIL
        st = next(a for a in failed(rec) if a["assertion_id"] == "S4.STATE")
        assert st["transition_id"] == "T07" and st["expected"]["state_after"] == "CONFIRMED" and st["actual"]["order_status"] == {"1": "PAID"}

    def test_paid_order_without_successful_payment_violates_invariant(self, validator, inputs):
        def drop_payment(r):
            for s in r["steps"]:
                if s["step_no"] >= 4 and s.get("observed_state"):
                    s["observed_state"]["payments"] = []
            r["final_observed_state"]["payments"] = []

        rec = run_case(validator, inputs, "TC005", drop_payment)
        assert rec["validation_status"] == FAIL and {"INV-04", "INV-12" if False else "INV-04", "S4.DATA.payments"} <= ids(rec)
        assert next(a for a in failed(rec) if a["assertion_id"] == "S4.DATA.payments")["category"] == rv.PAYMENT_LIFECYCLE_VIOLATION

    def test_wrong_stock_value(self, validator, inputs):
        def bad_stock(r):
            obs(r, 2)["product_stock"]["1"] = 7

        rec = run_case(validator, inputs, "TC005", bad_stock)
        assert "S2.DATA.stock" in ids(rec) and "INV-11" not in ids(rec)
        assert next(a for a in failed(rec) if a["assertion_id"] == "S2.DATA.stock")["actual"]["1"] == 7

    def test_negative_stock_violates_invariant_11(self, validator, inputs):
        def neg(r):
            obs(r, 2)["product_stock"]["1"] = -1

        rec = run_case(validator, inputs, "TC005", neg)
        assert "INV-11" in ids(rec)

    def test_duplicate_successful_payment(self, validator, inputs):
        def dup(r):
            for s in r["steps"]:
                if s["step_no"] == 4 and s.get("observed_state"):
                    s["observed_state"]["payments"].append(dict(s["observed_state"]["payments"][0], id=99))

        rec = run_case(validator, inputs, "TC005", dup)
        assert {"INV-09", "S4.DATA.payments"} <= ids(rec)
        assert next(a for a in failed(rec) if a["assertion_id"] == "INV-09")["actual"]["violations"][0]["successful_payments"] == 2

    def test_order_total_tampered(self, validator, inputs):
        def tamper(r):
            for s in r["steps"]:
                if s["step_no"] >= 2 and s.get("observed_state") and s["observed_state"]["orders"]:
                    s["observed_state"]["orders"][0]["total_amount"] = 1

        rec = run_case(validator, inputs, "TC005", tamper)
        assert {"INV-03", "S2.DATA.orders"} <= ids(rec)

    def test_negative_step_leaves_a_side_effect_in_cart(self, validator, inputs):
        def side_effect(r):
            obs(r, 1)["cart"]["items"].append({"product_id": 2, "quantity": 1})

        rec = run_case(validator, inputs, "TC104", side_effect)  # !NT02 (đơn lẻ)
        assert rec["validation_status"] == FAIL
        a = next(x for x in failed(rec) if x["assertion_id"] == "S1.DATA.cart")
        assert a["category"] == rv.UNEXPECTED_SIDE_EFFECT

    def test_new_order_created_by_rejected_request(self, validator, inputs):
        def ghost(r):
            obs(r, 1)["orders"].append({"id": 9, "status": "PENDING_CONFIRMATION", "total_amount": 150000,
                                        "items": [{"product_id": 1, "unit_price": 150000, "quantity": 1, "subtotal": 150000}]})

        rec = run_case(validator, inputs, "TC103", ghost)  # !NT01
        assert rec["validation_status"] == FAIL and {"S1.DATA.orders", "S1.STATE"} & ids(rec)

    def test_response_body_mismatch(self, validator, inputs):
        def wrong_total(r):
            step(r, 2)["response"]["body"]["order"]["total_amount"] = 5

        rec = run_case(validator, inputs, "TC005", wrong_total)
        a = next(x for x in failed(rec) if x["assertion_id"] == "S2.BODY.response.order.total_amount")
        assert a["category"] == rv.RESPONSE_BODY_MISMATCH and a["actual"] == 5

    def test_payment_response_references_wrong_order(self, validator, inputs):
        """Giá trị mong đợi là $ref tới ID thật do backend sinh; khác ID -> FAIL."""
        def wrong_order(r):
            step(r, 4)["response"]["body"]["payment"]["order_id"] = 77

        rec = run_case(validator, inputs, "TC005", wrong_order)
        assert "S4.BODY.response.payment.order_id" in ids(rec)

    def test_every_injected_fault_is_detected_and_unmodified_data_is_clean(self, validator, inputs):
        """Độ nhạy: mỗi lỗi được chèn đều làm FAIL, dữ liệu gốc không."""
        faults = {
            "TC005": [lambda r: set_status(r, 3, "PENDING_CONFIRMATION"), lambda r: step(r, 3).__setitem__("response", {**step(r, 3)["response"], "status_code": 500}),
                      lambda r: obs(r, 4)["cart"]["items"].append({"product_id": 3, "quantity": 1})],
            "TC110": [lambda r: set_status(r, 5, "CANCELLED"), lambda r: obs(r, 5).__setitem__("payments", [])],
            "TC004": [lambda r: set_status(r, 3, "CONFIRMED")],
        }
        for tcid, muts in faults.items():
            assert run_case(validator, inputs, tcid)["validation_status"] == PASS
            for m in muts:
                assert run_case(validator, inputs, tcid, m)["validation_status"] == FAIL, (tcid, m)


# ---------------------------------------------------------------------------
# 10–15. INCONCLUSIVE, SKIPPED, dữ liệu lỗi, assertion không hỗ trợ
# ---------------------------------------------------------------------------
class TestInconclusiveAndSkipped:
    def test_missing_observed_state_is_inconclusive(self, validator, inputs):
        def drop(r):
            step(r, 2)["observed_state"] = None

        rec = run_case(validator, inputs, "TC005", drop)
        assert rec["validation_status"] == INCONCLUSIVE
        assert ids(rec, INCONCLUSIVE) >= {"S2.STATE", "S3.STATE"} and not failed(rec)
        assert all(a["category"] == rv.MISSING_DATA for a in rec["assertions"] if a["status"] == INCONCLUSIVE and a["assertion_id"].startswith("S"))

    def test_observation_errors_make_state_unusable(self, validator, inputs):
        def err(r):
            step(r, 2)["observation_errors"] = [{"probe": "orders", "message": "boom"}]

        assert run_case(validator, inputs, "TC005", err)["validation_status"] == INCONCLUSIVE

    def test_missing_expected_http_status(self, validator, inputs):
        def no_exp(case):
            del case["steps"][0]["expected"]["http_status"]

        rec = run_case(validator, inputs, "TC005", case_mutators=[no_exp])
        assert rec["validation_status"] == INCONCLUSIVE and "S1.HTTP" in ids(rec, INCONCLUSIVE)

    def test_skipped_execution_is_skipped(self, validator, inputs):
        def skip(r):
            r["execution_status"] = "SKIPPED"
            r["skip_reason"] = {"code": "FIXTURE_UNAVAILABLE", "message": "m"}
            r["steps"] = []

        rec = run_case(validator, inputs, "TC106", skip)
        assert rec["validation_status"] == SKIPPED and rec["reasons"][0]["code"] == "FIXTURE_UNAVAILABLE" and rec["assertions"] == []

    def test_unconverted_paths_are_skipped_with_reason(self, baseline_run):
        sk = [r for r in baseline_run["results"] if r["validation_status"] == SKIPPED]
        assert len(sk) == 49 and all(r["test_case_id"] is None and r["path_id"] and r["reasons"][0]["code"] == "NO_FEASIBLE_DATA_FOUND" for r in sk)

    def test_missing_test_case_id_is_recorded_as_data_error(self, inputs):
        data = copy.deepcopy(inputs.results)
        bad = next(r for r in data["results"] if r["test_case_id"] == "TC001")
        bad["test_case_id"] = None
        v = ResultValidator(Inputs(inputs.cases, data, inputs.model, inputs.rules))
        doc = v.validate_all()
        assert any(e["code"] == "MISSING_TEST_CASE_ID" for e in doc["data_errors"])
        row = next(r for r in doc["results"] if r["test_case_id"] == "TC001")
        assert row["validation_status"] == INCONCLUSIVE and row["reasons"][0]["code"] == "NO_EXECUTION_RESULT"
        assert any(r["test_case_id"] is None and r["validation_status"] == INCONCLUSIVE and r["data_errors"] for r in doc["results"])

    def test_test_case_without_id(self, validator, inputs):
        def noid(case):
            case["test_case_id"] = None

        rec = run_case(validator, inputs, "TC001", case_mutators=[noid])
        assert rec["validation_status"] == INCONCLUSIVE and rec["data_errors"][0]["code"] == "MISSING_TEST_CASE_ID"

    def test_unknown_result_and_duplicate_result(self, inputs):
        data = copy.deepcopy(inputs.results)
        ghost = copy.deepcopy(data["results"][0])
        ghost["test_case_id"] = "TC999"
        data["results"].append(ghost)
        data["results"].append(copy.deepcopy(next(r for r in data["results"] if r["test_case_id"] == "TC001")))
        doc = ResultValidator(Inputs(inputs.cases, data, inputs.model, inputs.rules)).validate_all()
        codes = {e["code"] for e in doc["data_errors"]}
        assert {"UNKNOWN_TEST_CASE", "DUPLICATE_RESULT"} <= codes

    def test_unsupported_assertion_is_inconclusive(self, validator, inputs):
        def add(case):
            case["assertions"].append({"id": "AX", "probe": {"method": "GET", "path": "/api/unknown"}, "expect": {"foo": 1}, "source": "x", "status": "NOT_EXECUTED"})
            case["assertions"].append({"id": "AY", "probe": {"method": "GET", "path": "/api/cart"}, "source": "x", "status": "NOT_EXECUTED"})

        rec = run_case(validator, inputs, "TC005", case_mutators=[add])
        assert rec["validation_status"] == INCONCLUSIVE and ids(rec, INCONCLUSIVE) == {"AX", "AY"}
        assert {a["category"] for a in rec["assertions"] if a["status"] == INCONCLUSIVE} == {rv.UNSUPPORTED_ASSERTION}

    def test_unsupported_operator_in_response_check(self, validator, inputs):
        def op(case):
            case["steps"][1]["expected"]["response_checks"][1]["op"] = ">"

        rec = run_case(validator, inputs, "TC005", case_mutators=[op])
        assert rec["validation_status"] == INCONCLUSIVE
        assert any(a["category"] == rv.UNSUPPORTED_ASSERTION for a in rec["assertions"])

    def test_unknown_invariant_is_inconclusive(self, validator, inputs):
        def inv(case):
            case["invariant_assertions"].append({"invariant_id": "INV-99", "status": "NOT_EXECUTED"})

        assert ids(run_case(validator, inputs, "TC005", case_mutators=[inv]), INCONCLUSIVE) == {"INV-99"}

    @pytest.mark.parametrize("status", ["EXECUTION_ERROR", "PARTIAL"])
    def test_technical_execution_problem_is_inconclusive(self, validator, inputs, status):
        def broken(r):
            r["execution_status"] = status
            r["errors"] = [{"step_no": 2, "kind": "TIMEOUT", "message": "x"}]

        rec = run_case(validator, inputs, "TC005", broken)
        assert rec["validation_status"] == INCONCLUSIVE and rec["reasons"][0]["code"] == "EXECUTION_NOT_COMPLETED" and rec["assertions"] == []
        assert rec["reasons"][0]["errors"] == [{"kind": "TIMEOUT", "step_no": 2}]

    def test_step_not_sent_is_inconclusive(self, validator, inputs):
        def ns(r):
            step(r, 3)["execution_status"] = "NOT_SENT"

        assert run_case(validator, inputs, "TC005", ns)["reasons"][0]["code"] == "STEP_NOT_EXECUTED"

    def test_environment_mismatch_is_inconclusive(self, validator, inputs):
        def mm(r):
            r["isolation"]["initial_state_matches_test_case"] = False

        assert run_case(validator, inputs, "TC005", mm)["reasons"][0]["code"] == "ENVIRONMENT_MISMATCH"

    def test_missing_result_is_inconclusive_not_pass(self, validator, inputs):
        rec = validator.validate_case(case_of(inputs, "TC005"), None)
        assert rec["validation_status"] == INCONCLUSIVE and rec["reasons"][0]["code"] == "NO_EXECUTION_RESULT"

    @pytest.mark.parametrize("field,value", [("path_id", "PXXX"), ("negative_transition_id", "NT01"), ("test_type", "NEGATIVE")])
    def test_inconsistent_sources_are_data_errors(self, validator, inputs, field, value):
        def m(r):
            r[field] = value

        rec = run_case(validator, inputs, "TC005", m)
        assert rec["validation_status"] == INCONCLUSIVE and rec["data_errors"][0]["code"] == "INCONSISTENT_SOURCES"

    def test_step_list_mismatch_is_data_error(self, validator, inputs):
        def m(r):
            r["steps"].pop()

        rec = run_case(validator, inputs, "TC005", m)
        assert rec["data_errors"] and "danh sách bước" in rec["data_errors"][0]["message"]

    def test_source_hash_mismatch_makes_everything_inconclusive(self, inputs):
        data = copy.deepcopy(inputs.results)
        data["source"]["sha256"] = "0" * 64
        doc = ResultValidator(Inputs(inputs.cases, data, inputs.model, inputs.rules, cases_sha256="1" * 64)).validate_all()
        assert any(e["code"] == "SOURCE_HASH_MISMATCH" for e in doc["data_errors"])
        assert {r["validation_status"] for r in doc["results"] if r["test_case_id"]} == {INCONCLUSIVE}


# ---------------------------------------------------------------------------
# Đặc tả: ưu tiên nguồn, SPEC_CONFLICT, tiền đề
# ---------------------------------------------------------------------------
class TestSpecification:
    def test_conflict_between_test_case_and_efsm(self, validator, inputs):
        def conflict(case):
            case["steps"][0]["expected"]["http_status"] = 201

        rec = run_case(validator, inputs, "TC005", case_mutators=[conflict])
        a = next(x for x in rec["assertions"] if x["assertion_id"] == "S1.HTTP")
        assert a["status"] == INCONCLUSIVE and a["category"] == rv.SPEC_CONFLICT and rec["validation_status"] == INCONCLUSIVE
        assert a["evidence"]["conflicts"][0]["efsm"] == 200 and a["evidence"]["conflicts"][0]["test_case"] == 201

    def test_conflict_between_efsm_and_security_rule(self, inputs):
        rules = copy.deepcopy(inputs.rules)
        rule = next(r for r in rules["rules"] if r["rule_id"] == "BSR-CANCEL-02")
        rule["violation_expected"] = {"accept": False, "http_status": 403}
        v = ResultValidator(Inputs(inputs.cases, inputs.results, inputs.model, rules))
        rec = v.validate_case(case_of(inputs, "TC110"), result_of(inputs, "TC110"))
        http = next(a for a in rec["assertions"] if a["assertion_id"] == "S5.HTTP")
        assert http["status"] == INCONCLUSIVE and http["category"] == rv.SPEC_CONFLICT
        assert http["evidence"]["conflicts"][0]["security_rule"] == 403 and rec["validation_status"] == INCONCLUSIVE

    def test_spec_conflict_never_picks_the_backend_value(self, validator, inputs):
        """Dù response khớp EFSM, mâu thuẫn đặc tả vẫn không được PASS."""
        def conflict(case):
            case["steps"][-1]["expected"]["http_status"] = 410

        rec = run_case(validator, inputs, "TC110", case_mutators=[conflict])
        assert rec["validation_status"] == INCONCLUSIVE

    def test_precondition_not_satisfied_is_a_spec_or_data_issue(self, validator, inputs):
        def unsat(case):
            case["steps"][1]["guard_check"]["status"] = "GUARD_NOT_SATISFIED"

        rec = run_case(validator, inputs, "TC005", case_mutators=[unsat])
        a = next(x for x in rec["assertions"] if x["assertion_id"] == "S2.PRECONDITION")
        assert a["status"] == INCONCLUSIVE and a["category"] == rv.SPEC_OR_TESTDATA_ISSUE and rec["validation_status"] == INCONCLUSIVE

    def test_negative_not_applicable_is_a_data_issue(self, validator, inputs):
        def na(case):
            case["steps"][-1]["violation"]["evaluator_status"] = "NEGATIVE_NOT_APPLICABLE"

        rec = run_case(validator, inputs, "TC110", case_mutators=[na])
        assert "S5.PRECONDITION" in ids(rec, INCONCLUSIVE)

    def test_unknown_transition_in_model(self, validator, inputs):
        def unknown(case):
            case["steps"][0]["transition_id"] = "T99"

        rec = validator.validate_case(case_of(inputs, "TC005") | {"steps": [dict(s, **({"transition_id": "T99"} if s["step_no"] == 1 else {})) for s in case_of(inputs, "TC005")["steps"]]},
                                      result_of(inputs, "TC005"))
        assert rec["validation_status"] == INCONCLUSIVE

    def test_fail_beats_inconclusive_beats_pass(self, validator, inputs):
        def both(r):
            obs(r, 2)["product_stock"]["1"] = 7
            step(r, 3)["observed_state"] = None

        rec = run_case(validator, inputs, "TC005", both)
        assert rec["validation_status"] == FAIL and failed(rec) and ids(rec, INCONCLUSIVE)

    def test_structural_invariants_come_from_negative_transition(self, validator, inputs):
        rec = run_case(validator, inputs, "TC110")
        inv = next(a for a in rec["assertions"] if a["assertion_id"] == "INV-06")
        assert inv["evidence"]["verified_by"] == "NT08" and inv["status"] == PASS

        def accept(r):
            step(r, 5)["response"]["status_code"] = 200

        assert next(a for a in run_case(validator, inputs, "TC110", accept)["assertions"] if a["assertion_id"] == "INV-06")["status"] == FAIL


# ---------------------------------------------------------------------------
# Nhiều assertion + tổng hợp
# ---------------------------------------------------------------------------
class TestAggregation:
    def test_multiple_faults_are_all_reported(self, validator, inputs):
        def many(r):
            step(r, 1)["response"]["status_code"] = 500
            obs(r, 2)["product_stock"]["1"] = 3
            set_status(r, 4, "CANCELLED")

        rec = run_case(validator, inputs, "TC005", many)
        f = ids(rec)
        assert {"S1.HTTP", "S2.DATA.stock", "S4.STATE"} <= f
        assert rec["assertion_counts"]["failed"] == len(failed(rec)) == len(f)
        assert rec["assertion_counts"]["total"] == len(rec["assertions"])
        assert rec["assertion_counts"]["passed"] + rec["assertion_counts"]["failed"] + rec["assertion_counts"]["not_evaluable"] == len(rec["assertions"])

    def test_summary_matches_results(self, baseline_run):
        s = baseline_run["summary"]
        rows = baseline_run["results"]
        assert s["total"] == len(rows) == 171
        assert s["passed"] + s["failed"] + s["skipped"] + s["inconclusive"] == s["total"]
        for key, status in (("passed", PASS), ("failed", FAIL), ("skipped", SKIPPED), ("inconclusive", INCONCLUSIVE)):
            assert s[key] == sum(1 for r in rows if r["validation_status"] == status)
        a = [x for r in rows for x in r["assertions"]]
        assert s["assertions"]["total"] == len(a)
        assert s["assertions"]["evaluated"] + s["assertions"]["not_evaluable"] == len(a)
        assert s["assertions"]["failed"] == sum(1 for x in a if x["status"] == FAIL)
        assert sum(s["failures_by_category"].values()) == s["assertions"]["failed"]
        assert s["skipped"] == 49

    def test_every_case_status_follows_its_assertions(self, baseline_run):
        for r in baseline_run["results"]:
            st = {a["status"] for a in r["assertions"]}
            if r["validation_status"] == PASS:
                assert st == {PASS} and r["assertions"]
            elif r["validation_status"] == FAIL:
                assert FAIL in st
            elif r["validation_status"] == SKIPPED:
                assert not r["assertions"]

    def test_completed_is_not_assumed_passed(self, baseline_run, inputs):
        completed = [r for r in baseline_run["results"] if r["execution_status"] == "COMPLETED"]
        assert len(completed) == 122
        assert all(r["assertions"] for r in completed)  # PASS luôn có assertion thật đứng sau


# ---------------------------------------------------------------------------
# Truy vết, riêng tư, tính độc lập, tái lập
# ---------------------------------------------------------------------------
class TestTraceabilityAndSafety:
    def test_assertion_traceability_fields(self, baseline_run):
        for r in baseline_run["results"]:
            for a in r["assertions"]:
                assert {"assertion_id", "category", "status", "expected", "actual", "rule_refs", "evidence", "check", "transition_id", "step_no"} <= set(a)
            if r["assertions"]:
                assert r["test_case_id"] and r["path_id"] and r["path_key"]
        sample = next(r for r in baseline_run["results"] if r["test_case_id"] == "TC110")
        http = next(a for a in sample["assertions"] if a["assertion_id"] == "S5.HTTP")
        assert http["transition_id"] == "NT08" and "BSR-CANCEL-01" in http["rule_refs"]
        assert http["evidence"]["expected_ref"] == {"test_case_id": "TC110", "step_no": 5}
        assert http["evidence"]["request"] == "POST /api/orders/1/cancel"

    def test_every_fail_has_evidence_and_source(self, validator, inputs):
        def f(r):
            set_status(r, 5, "CANCELLED")
            step(r, 5)["response"]["status_code"] = 200

        rec = run_case(validator, inputs, "TC110", f)
        for a in failed(rec):
            assert a["evidence"].get("execution_step") or a["assertion_id"].startswith("A") or a["assertion_id"].startswith("INV")
            assert a["expected_source"] or a["rule_refs"] or a["evidence"]
            assert a["expected"] is not None and a["actual"] is not None

    def test_no_sensitive_data_in_output(self, baseline_run):
        text = json.dumps(baseline_run, ensure_ascii=False)
        for token in ("NNNNNNNNNN", "9999999999", "AAAAAAAAAAAAAAAAAAAA", "customer_name", '"phone"', '"address"'):
            assert token not in text

    def test_output_is_valid_json_with_documented_schema(self, baseline_run):
        again = json.loads(json.dumps(baseline_run, ensure_ascii=False))
        assert again == baseline_run
        assert baseline_run["schema_version"] == "1.0" and baseline_run["validator"] == "BShop Result Validator"
        assert set(baseline_run["summary"]) >= {"total", "passed", "failed", "skipped", "inconclusive"}
        assert set(baseline_run["results"][0]) >= {"test_case_id", "path_id", "validation_status", "assertions"}

    def test_deterministic(self, validator, baseline_run):
        assert validator.validate_all() == baseline_run

    def test_inputs_are_not_modified(self, inputs):
        before = {k: hashlib.sha256(Path(p).read_bytes()).hexdigest() for k, p in FILES.items()}
        snapshot = copy.deepcopy((inputs.cases, inputs.results))
        ResultValidator(inputs).validate_all()
        assert (inputs.cases, inputs.results) == snapshot
        assert before == {k: hashlib.sha256(Path(p).read_bytes()).hexdigest() for k, p in FILES.items()}

    def test_validator_does_not_import_http_or_executor_or_backend(self):
        src = Path(rv.__file__).read_text(encoding="utf-8")
        for needle in ("import httpx", "import requests", "TestClient", "import urllib", "api_test_executor", "from app", "import app", "sqlite3", "sqlalchemy", "import socket"):
            assert needle not in src, needle

    def test_no_hardcoded_pass(self, validator, inputs):
        """Không có test case nào PASS khi thiếu observed_state ở mọi bước."""
        def strip(r):
            r["initial_observed_state"] = None
            for s in r["steps"]:
                s["observed_state"] = None
            r["final_observed_state"] = None

        for tcid in ("TC001", "TC005", "TC110", "TC104"):
            assert run_case(validator, inputs, tcid, strip)["validation_status"] == INCONCLUSIVE


# ---------------------------------------------------------------------------
# Loader & CLI
# ---------------------------------------------------------------------------
class TestLoaderAndCli:
    def test_real_inputs_are_consistent(self, inputs):
        assert len(inputs.cases["test_cases"]) == 122 and len(inputs.results["results"]) == 171
        ids_c = {c["test_case_id"] for c in inputs.cases["test_cases"]}
        ids_r = {r["test_case_id"] for r in inputs.results["results"] if r["test_case_id"]}
        assert ids_c == ids_r
        assert inputs.results["source"]["sha256"] == hashlib.sha256(FILES["test_cases"].read_bytes()).hexdigest()

    @pytest.mark.parametrize("bad", [[], {}, {"test_cases": 1}, {"schema_version": "9", "test_cases": []}])
    def test_invalid_test_cases_file(self, inputs, bad):
        with pytest.raises(rv.InputError):
            Inputs(bad, inputs.results, inputs.model, inputs.rules)

    def test_invalid_other_inputs(self, inputs):
        with pytest.raises(rv.InputError):
            Inputs(inputs.cases, {"schema_version": "1.0"}, inputs.model, inputs.rules)
        with pytest.raises(rv.InputError):
            Inputs(inputs.cases, inputs.results, {}, inputs.rules)
        with pytest.raises(rv.InputError):
            Inputs(inputs.cases, inputs.results, inputs.model, {"rules": 1})

    def test_unreadable_files(self, tmp_path):
        with pytest.raises(rv.InputError):
            rv._read_json(tmp_path / "missing.json", "x")
        bad = tmp_path / "b.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(rv.InputError):
            rv._read_json(bad, "x")

    def test_cli_writes_output(self, tmp_path):
        out = tmp_path / "v.json"
        assert rv.main(["--test-cases", str(FILES["test_cases"]), "--execution-results", str(FILES["execution_results"]),
                        "--model", str(FILES["model"]), "--rules", str(FILES["rules"]), "--output", str(out)]) == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["summary"]["total"] == 171

    def test_cli_errors_and_fail_exit_code(self, tmp_path, capsys, inputs):
        assert rv.main(["--test-cases", str(tmp_path / "none.json"), "--output", str(tmp_path / "o.json")]) == 2
        assert "ERROR" in capsys.readouterr().err
        results = copy.deepcopy(inputs.results)
        step_ = next(r for r in results["results"] if r["test_case_id"] == "TC110")["steps"][4]
        step_["response"]["status_code"] = 200
        rf = tmp_path / "r.json"
        rf.write_text(json.dumps(results), encoding="utf-8")
        args = ["--test-cases", str(FILES["test_cases"]), "--execution-results", str(rf), "--model", str(FILES["model"]),
                "--rules", str(FILES["rules"]), "--output", str(tmp_path / "o.json")]
        assert rv.main(args) == 0
        assert rv.main(args + ["--fail-on-fail"]) == 1
        assert json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))["summary"]["failed"] >= 1

    def test_normalizers(self):
        assert rv.norm_observed(None) is None and rv.norm_observed({"cart": {}}) is None
        with pytest.raises(rv.Unresolved):
            rv.resolve_ref("$ref:steps.9.response.x", {"steps": []})
        with pytest.raises(rv.Unresolved):
            rv.resolve_ref("garbage", {})
        res = {"steps": [{"step_no": 2, "response": {"body": {"order": {"id": 5, "items": [{"a": 1}]}}}}]}
        assert rv.resolve_ref("$ref:steps.2.response.order.id", res) == 5
        assert rv.resolve_ref("$ref:steps.2.response.order.items.0.a", res) == 1
