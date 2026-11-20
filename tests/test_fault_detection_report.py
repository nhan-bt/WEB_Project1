"""Kiểm thử Fault Detection Report (Phần 8).

* Dữ liệu thực tế: test_validation_results.json (Phần 7) và các file Phần 2–6 (chỉ đọc).
* Lỗi cài đặt: tạo bằng cách chèn vào BẢN SAO kết quả thực thi rồi chạy qua Result Validator thật
  (không tự viết FAIL vào báo cáo). Một số test phân loại dùng bản sao validation result đã chỉnh để kiểm tra nhánh UNCLASSIFIED.
"""
from __future__ import annotations

import copy
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

import fault_detection_report as fdr
import result_validator as rv

ROOT = Path(__file__).resolve().parent.parent
F = {n: ROOT / n for n in ("test_validation_results.json", "test_execution_results.json", "test_cases.json", "efsm_model.json",
                          "security_rules.json", "test_paths.json", "test_path_coverage.json")}


@pytest.fixture(scope="module")
def validation():
    return json.loads(F["test_validation_results.json"].read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def refs():
    r, _ = fdr.References.from_files(F["test_cases.json"], F["efsm_model.json"], F["security_rules.json"], F["test_paths.json"],
                                     F["test_path_coverage.json"], F["test_execution_results.json"])
    return r


@pytest.fixture(scope="module")
def vin(refs):
    return rv.Inputs(refs.cases_doc, refs.execution, refs.model, refs.rules_doc)


@pytest.fixture(scope="module")
def real_report(validation, refs, vin):
    return fdr.build_report(copy.deepcopy(validation), refs, validation_inputs=vin)


@pytest.fixture(scope="module")
def campaign(refs, vin):
    return fdr.run_synthetic_campaign(refs, vin)


def scenario(campaign, sid):
    return next(o for o in campaign["scenarios"] if o["scenario_id"] == sid)


def inject_doc(refs, vin, tcids_and_fns):
    """Chèn lỗi vào bản sao -> Result Validator thật -> validation doc có FAIL; trả (doc, refs_với_kết_quả_đã_sửa)."""
    validator = rv.ResultValidator(vin)
    rows, results = [], dict(refs.results)
    for tcid, fn in tcids_and_fns:
        case = next(c for c in refs.cases_doc["test_cases"] if c["test_case_id"] == tcid)
        mutated = copy.deepcopy(refs.results[tcid])
        fn(mutated, case)
        results[tcid] = mutated
        rows.append(validator.validate_case(copy.deepcopy(case), mutated))
    local = copy.copy(refs)
    local.results = results
    return validator._document(rows, []), local


def case_ending_with(refs, last_transition, n=1, readiness="READY"):
    out = []
    for c in refs.cases_doc["test_cases"]:
        steps = [s.get("transition_id") for s in c["steps"] if s.get("role") == "path"]
        if c.get("type") == "positive" and c["readiness"] == readiness and steps and steps[-1] == last_transition:
            out.append(c["test_case_id"])
    return out[:n]


def unpaid(res, case):
    """Thanh toán thành công nhưng đơn không thành PAID (từ bước cuối trở đi)."""
    n = case["steps"][-1]["step_no"]
    for st in fdr._states_from(res, n):
        for o in st["orders"]:
            o["status"] = "CONFIRMED"


def nt_case(refs, nt):
    return next(c["test_case_id"] for c in refs.cases_doc["test_cases"] if c.get("negative_transition_id") == nt)


# ---------------------------------------------------------------------------
# 1–3. Loader, schema, thống kê
# ---------------------------------------------------------------------------
class TestLoader:
    def test_reads_valid_real_validation(self, validation):
        doc, errors = fdr.load_validation(copy.deepcopy(validation))
        assert doc["summary"]["total"] == len(doc["results"]) and errors == []

    @pytest.mark.parametrize("mutate,msg", [
        (lambda d: d.update(validator="Other"), "Result Validator"),
        (lambda d: d.update(schema_version="9"), "schema_version"),
        (lambda d: d.pop("results"), "results"),
        (lambda d: d["summary"].update(passed=d["summary"]["passed"] + 1), "summary.passed"),
        (lambda d: d["summary"]["assertions"].update(total=1), "assertions.total"),
        (lambda d: d["results"][0].update(validation_status="WEIRD"), "validation_status"),
        (lambda d: d["results"][0]["assertions"][0].update(status="MAYBE"), "assertion"),
    ])
    def test_invalid_schema_is_rejected(self, validation, mutate, msg):
        doc = copy.deepcopy(validation)
        mutate(doc)
        with pytest.raises(fdr.ReportInputError, match=msg):
            fdr.load_validation(doc)

    def test_fail_without_evidence_is_rejected(self, validation):
        doc = copy.deepcopy(validation)
        row = next(r for r in doc["results"] if r["validation_status"] == "PASS")
        row["validation_status"] = "FAIL"
        doc["summary"]["passed"] -= 1
        doc["summary"]["failed"] += 1
        with pytest.raises(fdr.ReportInputError, match="không có assertion FAIL"):
            fdr.load_validation(doc)

    def test_not_a_dict(self):
        with pytest.raises(fdr.ReportInputError):
            fdr.load_validation([])

    def test_duplicate_and_missing_ids_are_recorded(self, validation):
        doc = copy.deepcopy(validation)
        dup = next(r for r in doc["results"] if r["validation_status"] == "PASS")
        doc["results"].append(copy.deepcopy(dup))
        orphan = copy.deepcopy(dup)
        orphan["test_case_id"] = None
        doc["results"].append(orphan)
        doc["summary"]["total"] += 2
        doc["summary"]["passed"] += 2
        doc["summary"]["assertions"]["total"] += 2 * len(dup["assertions"])
        doc["summary"]["assertions"]["passed"] += 2 * len(dup["assertions"])
        _, errors = fdr.load_validation(doc)
        assert {e["code"] for e in errors} == {"DUPLICATE_TEST_CASE_ID", "MISSING_TEST_CASE_ID"}

    def test_unreadable_file(self, tmp_path):
        with pytest.raises(fdr.ReportInputError):
            fdr._read(tmp_path / "none.json", "x")
        bad = tmp_path / "b.json"
        bad.write_text("{x", encoding="utf-8")
        with pytest.raises(fdr.ReportInputError):
            fdr._read(bad, "x")


class TestAggregation:
    def test_summary_is_recomputed_from_input(self, real_report, validation):
        s = real_report["summary"]
        rows = validation["results"]
        for key, status in (("passed", "PASS"), ("failed", "FAIL"), ("skipped", "SKIPPED"), ("inconclusive", "INCONCLUSIVE")):
            assert s[key] == sum(1 for r in rows if r["validation_status"] == status)
        assert s["total_test_cases"] == len(rows) and s["assertions"] == validation["summary"]["assertions"]

    def test_summary_follows_modified_input_not_constants(self, validation, refs):
        doc = copy.deepcopy(validation)
        victim = next(r for r in doc["results"] if r["validation_status"] == "SKIPPED")
        victim["validation_status"] = "INCONCLUSIVE"
        doc["summary"]["skipped"] -= 1
        doc["summary"]["inconclusive"] += 1
        rep = fdr.build_report(doc, refs, with_synthetic=False)
        assert rep["summary"]["skipped"] == validation["summary"]["skipped"] - 1 and rep["summary"]["inconclusive"] == validation["summary"]["inconclusive"] + 1
        assert rep["summary"]["total_findings"] == 0


# ---------------------------------------------------------------------------
# 4–6. Trích xuất; SKIPPED/INCONCLUSIVE không phải lỗi
# ---------------------------------------------------------------------------
class TestExtraction:
    def test_extracts_only_failed_assertions(self, refs, vin):
        doc, local = inject_doc(refs, vin, [(nt_case(refs, "NT08"), fdr._inj_fi01)])
        recs = fdr.extract_faults(doc, local)
        failed = [a for r in doc["results"] for a in r["assertions"] if a["status"] == "FAIL"]
        assert len(recs) == len(failed) > 0
        assert {r["assertion_id"] for r in recs} == {a["assertion_id"] for a in failed}
        assert all(r["test_case_id"] == nt_case(refs, "NT08") for r in recs)

    def test_skipped_is_not_a_fault(self, real_report):
        assert real_report["summary"]["skipped"] == 49 and real_report["findings"] == []
        assert all(not f for f in real_report["findings"])

    def test_inconclusive_is_not_a_confirmed_fault(self, refs, vin):
        def drop(res, case):
            res["steps"][1]["observed_state"] = None

        doc, local = inject_doc(refs, vin, [(case_ending_with(refs, "T06")[0], drop)])
        row = doc["results"][0]
        assert row["validation_status"] == "INCONCLUSIVE"
        rep = fdr.build_report(doc, local, with_synthetic=False)
        assert rep["findings"] == [] and rep["summary"]["inconclusive"] == 1 and rep["summary"]["failed"] == 0
        assert "Không phát hiện sai lệch" in rep["conclusion"]["statement"]
        assert any("INCONCLUSIVE" in x for x in rep["limitations"])

    def test_no_fabricated_findings_without_fail(self, real_report):
        assert real_report["summary"]["total_findings"] == 0 and real_report["fault_categories"] == {
            "LIMITS_AUTHORIZATION": 0, "STATE_RESOURCE_LIFECYCLE": 0, "UNCLASSIFIED": 0}
        assert real_report["conclusion"]["statement"] == fdr.NO_FAULT_STATEMENT and not real_report["conclusion"]["has_findings"]


# ---------------------------------------------------------------------------
# 7–9. Phân loại
# ---------------------------------------------------------------------------
class TestClassification:
    def test_limits_group(self, campaign):
        o = scenario(campaign, "FI04")
        guard = next(f for f in o["findings"] if f["validator_category"] == rv.GUARD_VIOLATION)
        assert guard["group"] == fdr.GROUP_LIMITS and guard["sub_class"] == "BUSINESS_LIMIT"

    @pytest.mark.parametrize("sid,cat", [("FI01", rv.INVALID_STATE_TRANSITION), ("FI02", rv.PAYMENT_LIFECYCLE_VIOLATION),
                                         ("FI03", rv.STOCK_INTEGRITY_VIOLATION), ("FI05", rv.INVALID_STATE_TRANSITION)])
    def test_state_lifecycle_group(self, campaign, sid, cat):
        o = scenario(campaign, sid)
        f = next(x for x in o["findings"] if x["validator_category"] == cat)
        assert f["group"] == fdr.GROUP_STATE and f["classification_basis"] == f"validator_category={cat}"

    def test_validator_code_and_research_group_are_both_kept(self, campaign):
        for o in campaign["scenarios"]:
            for f in o["findings"]:
                assert f["validator_category"] and f["group"] in fdr.GROUPS and f["sub_class"]

    def test_unclassifiable_fault_is_unclassified_with_reason(self, refs, vin):
        doc, local = inject_doc(refs, vin, [(nt_case(refs, "NT08"), fdr._inj_fi01)])
        crafted = copy.deepcopy(doc)
        row = crafted["results"][0]
        for a in row["assertions"]:
            if a["status"] == "FAIL":
                a["category"], a["rule_refs"] = rv.INVARIANT_VIOLATION, []
        local2 = copy.copy(local)
        local2.cases = {k: dict(v, negative_category=None) for k, v in local.cases.items()}
        recs = fdr.build_findings(crafted, local2, fdr.REAL)
        assert recs and {f["group"] for f in recs} == {fdr.GROUP_UNCLASSIFIED}
        assert all("không đủ thông tin" in f["classification_basis"] for f in recs)

    def test_rule_based_fallback(self, refs):
        rec = {"validator_category": rv.INVARIANT_VIOLATION, "rule_refs": ["BSR-CART-01"], "negative_category": None}
        assert fdr.classify(rec, refs)["group"] == fdr.GROUP_LIMITS
        rec["rule_refs"] = ["BSR-PAYMENT-01"]
        assert fdr.classify(rec, refs)["group"] == fdr.GROUP_STATE
        rec["rule_refs"] = ["BSR-PAYMENT-01", "BSR-CART-01"]
        assert fdr.classify(rec, refs)["group"] == fdr.GROUP_UNCLASSIFIED

    def test_no_ownership_or_authentication_label(self, campaign, real_report):
        text = json.dumps([campaign, real_report], ensure_ascii=False).lower()
        for banned in ("ownership_violation", "authentication_bypass", "broken_authentication", "idor"):
            assert banned not in text
        assert "BSR-AUTH-01" in real_report["authorization_scope_note"] and "không triển khai" in real_report["authorization_scope_note"]


# ---------------------------------------------------------------------------
# 10. Severity
# ---------------------------------------------------------------------------
class TestSeverity:
    def test_scores_follow_documented_criteria(self, campaign):
        for o in campaign["scenarios"]:
            for f in o["findings"]:
                c = f["severity_criteria"]
                assert f["severity_score"] == sum(v for k, v in c.items() if k != "impact_domain")
                lvl = f["severity_score"]
                expected = "CRITICAL" if lvl >= 8 else "HIGH" if lvl >= 6 else "MEDIUM" if lvl >= 4 else "LOW" if lvl >= 2 else "INFO"
                if f["validator_category"] in fdr.INTERFACE_ONLY and expected in ("CRITICAL", "HIGH"):
                    expected = "MEDIUM"
                assert f["severity"] == expected and f["severity_reason"]

    def test_http_status_alone_is_never_critical(self, campaign):
        https = [f for o in campaign["scenarios"] for f in o["findings"] if f["validator_category"] == rv.HTTP_STATUS_MISMATCH]
        assert https and all(f["severity"] in ("LOW", "INFO", "MEDIUM", "UNDETERMINED") for f in https)

    def test_payment_and_stock_faults_rank_above_interface_faults(self, campaign):
        order = {"CRITICAL": 5, "HIGH": 4, "MEDIUM": 3, "LOW": 2, "INFO": 1, "UNDETERMINED": 0}
        pay = max(order[f["severity"]] for f in scenario(campaign, "FI02")["findings"] if f["validator_category"] == rv.PAYMENT_LIFECYCLE_VIOLATION)
        stock = max(order[f["severity"]] for f in scenario(campaign, "FI03")["findings"] if f["validator_category"] == rv.STOCK_INTEGRITY_VIOLATION)
        http = max(order[f["severity"]] for f in scenario(campaign, "FI01")["findings"] if f["validator_category"] == rv.HTTP_STATUS_MISMATCH)
        assert pay >= 4 and stock >= 4 and pay > http and stock > http

    def test_failed_payment_turning_paid_touches_payment_rules(self, campaign):
        f = scenario(campaign, "FI05")["findings"][0]
        assert f["severity_criteria"]["impact_domain"] == "MONEY" and f["severity"] in ("HIGH", "CRITICAL")

    def test_undetermined_when_no_criteria(self, refs):
        member = {"validator_category": "SOMETHING_NEW", "rule_refs": [], "test_case_id": "X", "assertion_id": "S1.HTTP"}
        assert fdr.assess_severity([member], refs)["severity"] == "UNDETERMINED"
        member = {"validator_category": rv.HTTP_STATUS_MISMATCH, "rule_refs": [], "test_case_id": "X", "assertion_id": "S1.HTTP"}
        assert fdr.assess_severity([member], refs)["severity"] == "UNDETERMINED"

    def test_reproducibility_raises_score(self, refs, vin):
        ids2 = case_ending_with(refs, "T06", 3)
        doc, local = inject_doc(refs, vin, [(t, unpaid) for t in ids2])
        multi = next(f for f in fdr.build_findings(doc, local, fdr.SYNTHETIC) if f["violated_field"] == "STATE")
        doc1, local1 = inject_doc(refs, vin, [(ids2[0], unpaid)])
        single = next(f for f in fdr.build_findings(doc1, local1, fdr.SYNTHETIC) if f["violated_field"] == "STATE")
        assert multi["severity_criteria"]["reproducible_in_multiple_cases"] == 1 and single["severity_criteria"]["reproducible_in_multiple_cases"] == 0
        assert multi["severity_score"] == single["severity_score"] + 1

    def test_confidence_levels(self, campaign):
        assert all(f["confidence"] in ("HIGH", "MEDIUM", "LOW") for o in campaign["scenarios"] for f in o["findings"])
        assert any(f["confidence"] == "HIGH" for o in campaign["scenarios"] for f in o["findings"])
        http = [f for o in campaign["scenarios"] for f in o["findings"] if f["validator_category"] == rv.HTTP_STATUS_MISMATCH]
        assert all(f["confidence"] != "HIGH" for f in http)


# ---------------------------------------------------------------------------
# 11–13. Truy vết và bằng chứng
# ---------------------------------------------------------------------------
class TestTraceability:
    def test_rule_trace(self, campaign, refs):
        f = next(x for x in scenario(campaign, "FI01")["findings"] if x["validator_category"] == rv.INVALID_STATE_TRANSITION and x["rules"])
        ids = {r["rule_id"] for r in f["rules"]}
        assert ids and ids <= set(refs.rules)
        assert all(r["name"] and r["category"] and r["severity"] for r in f["rules"])
        assert "BSR-CANCEL-02" in {r["rule_id"] for x in scenario(campaign, "FI01")["findings"] for r in x["rules"]}

    def test_transition_trace_uses_base_transition_for_negative(self, campaign):
        for sid, nt, base in (("FI01", "NT08", "T08"), ("FI02", "NT07", "T06"), ("FI03", "NT09", "T08")):
            f = scenario(campaign, sid)["findings"][0]
            assert f["negative_transition_id"] == nt and f["transition_id"] == base
            assert f["endpoint"]["method"] == "POST" and f["endpoint"]["path"].startswith("/api/orders/{order_id}/")

    def test_path_and_test_case_trace(self, campaign, refs):
        o = scenario(campaign, "FI01")
        f = o["findings"][0]
        assert f["test_case_ids"] == [o["test_case_id"]] and f["path_ids"] == [refs.cases[o["test_case_id"]]["source_path_id"]]

    def test_expected_actual_recorded(self, campaign):
        f = next(x for x in scenario(campaign, "FI01")["findings"] if x["validator_category"] == rv.INVALID_STATE_TRANSITION and x["violated_field"] == "STATE")
        occ = f["occurrences"][0]
        assert occ["expected"]["order_status"] and occ["actual"]["order_status"] != occ["expected"]["order_status"]
        assert occ["expected_source"] or f["rules"]

    def test_evidence_comes_from_execution_data(self, campaign, refs):
        f = next(x for x in scenario(campaign, "FI01")["findings"] if x["violated_field"] == "STATE")
        ev = f["evidence"]
        assert ev["source_files"] == ["test_validation_results.json", "test_execution_results.json"]
        assert ev["request"]["method"] == "POST" and "/cancel" in ev["request"]["url"]
        assert ev["response"]["status_code"] == 200  # đúng giá trị đã chèn vào bản sao
        assert ev["observed_state_before"]["orders"][0]["status"] == "PAID" and ev["observed_state_after"]["orders"][0]["status"] == "CANCELLED"
        assert ev["expected_ref"]["test_case_id"] == scenario(campaign, "FI01")["test_case_id"]

    def test_limited_evidence_without_execution_results(self, refs, vin):
        doc, local = inject_doc(refs, vin, [(nt_case(refs, "NT08"), fdr._inj_fi01)])
        bare = copy.copy(local)
        bare.results = {}
        f = fdr.build_findings(doc, bare, fdr.SYNTHETIC)[0]
        assert "limited" in f["evidence"] and "request" not in f["evidence"] and f["confidence"] == "LOW"

    def test_no_sensitive_data_in_evidence(self, campaign, real_report):
        text = json.dumps([campaign, real_report], ensure_ascii=False)
        assert "NNNNNNNNNN" not in text and "customer_name" not in text.replace('"<redacted>"', "") or "<redacted>" in text
        for token in ("0900000000", "AAAAAAAAAAAAAAAAAAAA"):
            assert token not in text
        assert fdr._scrub({"phone": "0123", "n": [{"address": "x"}]}) == {"phone": "<redacted>", "n": [{"address": "<redacted>"}]}


# ---------------------------------------------------------------------------
# 14–15. Trùng lặp
# ---------------------------------------------------------------------------
class TestDeduplication:
    def test_same_fault_in_many_test_cases_is_one_finding(self, refs, vin):
        ids3 = case_ending_with(refs, "T06", 3)
        assert len(ids3) == 3
        doc, local = inject_doc(refs, vin, [(t, unpaid) for t in ids3])
        fs = fdr.build_findings(doc, local, fdr.SYNTHETIC)
        assert len(fs) == 1 and fs[0]["violated_field"] == "STATE"
        c = fs[0]["counts"]
        assert c == {"failed_assertions": 3, "failed_test_cases": 3} and fs[0]["test_case_ids"] == sorted(ids3)
        rep = fdr.build_report(doc, local, source_type=fdr.SYNTHETIC, with_synthetic=False)
        m = rep["summary"]["fault_metrics"]
        assert (m["failed_assertions"], m["failed_test_cases"], m["distinct_findings"]) == (3, 3, 1)

    def test_three_metrics_are_not_identical(self, refs, vin):
        doc, local = inject_doc(refs, vin, [(nt_case(refs, "NT08"), fdr._inj_fi01)])
        rep = fdr.build_report(doc, local, source_type=fdr.SYNTHETIC, with_synthetic=False)
        m = rep["summary"]["fault_metrics"]
        assert m["failed_assertions"] > m["failed_test_cases"] == 1 and m["distinct_findings"] >= 1
        assert rep["summary"]["root_cause_clusters"] == 1

    def test_independent_faults_are_not_merged(self, refs, vin):
        doc, local = inject_doc(refs, vin, [(nt_case(refs, "NT08"), fdr._inj_fi01), (nt_case(refs, "NT09"), fdr._inj_fi03)])
        fs = fdr.build_findings(doc, local, fdr.SYNTHETIC)
        keys = {(f["negative_transition_id"], f["validator_category"], f["violated_field"]) for f in fs}
        assert len(keys) == len(fs)
        assert {f["negative_transition_id"] for f in fs} == {"NT08", "NT09"}

    def test_same_http_status_is_not_enough_to_merge(self, refs, vin):
        def bad_http(code):
            def fn(res, case):
                res["steps"][-1]["response"]["status_code"] = code
            return fn

        doc, local = inject_doc(refs, vin, [(nt_case(refs, "NT08"), bad_http(500)), (nt_case(refs, "NT07"), bad_http(500))])
        fs = [f for f in fdr.build_findings(doc, local, fdr.SYNTHETIC) if f["validator_category"] == rv.HTTP_STATUS_MISMATCH]
        assert len(fs) == 2 and {f["negative_transition_id"] for f in fs} == {"NT08", "NT07"}

    def test_fingerprint_ignores_http_status_values(self, refs):
        a = {"validator_category": rv.HTTP_STATUS_MISMATCH, "negative_transition_id": "NT08", "transition_id": "T08", "rule_refs": ["BSR-CANCEL-02"],
             "assertion_id": "S5.HTTP", "check": "http status", "expected": 409, "actual": 500}
        b = dict(a, actual=404, assertion_id="S9.HTTP")
        assert fdr.fingerprint(a, refs) == fdr.fingerprint(b, refs)
        assert fdr.fingerprint(a, refs) != fdr.fingerprint(dict(a, negative_transition_id="NT07"), refs)


# ---------------------------------------------------------------------------
# 16. Coverage
# ---------------------------------------------------------------------------
class TestCoverage:
    def test_executed_coverage_is_computed(self, real_report, refs):
        cov = real_report["coverage"]
        assert cov["test_cases"]["executed"] == 122 and cov["test_cases"]["not_executed"] == 49 and cov["test_cases"]["inconclusive"] == 0
        ex = cov["executed"]
        assert ex["transitions"]["ratio"] == 1.0 and ex["negative_transitions"]["ratio"] == 1.0 and ex["states"]["not_reached"] == []
        assert ex["transition_pairs"]["possible_by_model"] == refs.coverage["transition_pairs"]["possible"] == 28
        assert 0 < ex["transition_pairs"]["executed"] < 28 and ex["transition_pairs"]["not_executed"]
        assert set(cov["verified"]["rules"]["verified_by_pass"]) == set(refs.rules) and cov["verified"]["rules"]["with_fail"] == []

    def test_structural_vs_feasible_vs_executed_are_distinguished(self, real_report):
        cov = real_report["coverage"]
        assert cov["structural"]["transition_pairs"]["covered_structural"] == 28
        assert cov["structural"]["transition_pairs"]["covered_feasible"] == 14
        assert cov["executed"]["transition_pairs"]["executed"] != cov["structural"]["transition_pairs"]["covered_structural"]
        assert any("100%" in n for n in cov["unverified_areas"]["notes"])

    def test_unverified_areas_list_every_not_executed_path(self, real_report, validation):
        paths = real_report["coverage"]["unverified_areas"]["not_executed_paths"]
        assert len(paths) == 49 and all(p["path_id"] and p["reason"]["code"] == "NO_FEASIBLE_DATA_FOUND" for p in paths)

    def test_failed_transition_is_not_counted_verified(self, refs, vin):
        doc, local = inject_doc(refs, vin, [(nt_case(refs, "NT08"), fdr._inj_fi01)])
        cov = fdr.analyze_coverage(doc, local)
        assert "NT08" in cov["verified"]["transitions_with_fail"] and "NT08" not in cov["verified"]["transitions_with_pass_and_no_fail"]

    def test_coverage_without_optional_files(self, validation, refs):
        bare = copy.copy(refs)
        bare.coverage = None
        bare.paths_doc = None
        cov = fdr.build_report(copy.deepcopy(validation), bare, with_synthetic=False)["coverage"]
        assert cov["structural"] is None and cov["path_planning"] is None


# ---------------------------------------------------------------------------
# 17–19. JSON / HTML / Markdown
# ---------------------------------------------------------------------------
class _Strict(HTMLParser):
    VOID = {"meta", "br", "img", "input", "link", "hr"}

    def __init__(self):
        super().__init__()
        self.stack, self.errors, self.text = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(tag)

    def handle_data(self, data):
        self.text.append(data)


class TestOutputs:
    def test_json_is_valid_and_has_documented_fields(self, real_report):
        again = json.loads(json.dumps(real_report, ensure_ascii=False))
        assert again == real_report
        assert {"schema_version", "project", "report_type", "source_type", "summary", "fault_categories", "findings", "limitations"} <= set(real_report)
        assert real_report["project"] == "BShop" and real_report["report_type"] == "FAULT_DETECTION_REPORT" and real_report["source_type"] == "REAL_EXECUTION"
        assert set(real_report["summary"]) >= {"total_test_cases", "passed", "failed", "skipped", "inconclusive", "total_findings"}

    def test_html_is_well_formed_and_self_contained(self, real_report):
        page = fdr.render_html(real_report)
        p = _Strict()
        p.feed(page)
        assert p.errors == [] and p.stack == []
        assert page.startswith("<!DOCTYPE html>") and "<title>" in page and 'name="viewport"' in page
        for external in ("http://", "https://", "<script", "<link"):
            assert external not in page

    def test_html_has_all_required_sections(self, real_report):
        text = " ".join(_text(fdr.render_html(real_report)))
        for heading in ("Tổng quan kết quả", "Thống kê test case", "Thống kê assertion", "Độ bao phủ", "Phân loại lỗi theo hai nhóm nghiên cứu",
                        "Mức độ nghiêm trọng", "Findings thực tế và bằng chứng", "Chưa được kiểm chứng", "Giới hạn của kết quả"):
            assert heading in text, heading

    def test_no_fault_statement_and_no_absolute_safety_claim(self, real_report):
        for out in (fdr.render_html(real_report), fdr.render_markdown(real_report)):
            assert fdr.NO_FAULT_STATEMENT in out
            low = out.lower()
            assert "hoàn toàn an toàn" not in low.replace("không có nghĩa hệ thống an toàn tuyệt đối", "")
            assert "an toàn tuyệt đối" not in low.replace("không có nghĩa hệ thống an toàn tuyệt đối", "")

    def test_html_escapes_untrusted_values(self, real_report):
        rep = copy.deepcopy(real_report)
        rep["limitations"].append("<script>alert(1)</script>")
        rep["findings"] = [{"finding_id": "F1", "source_type": "REAL_EXECUTION", "title": "<img src=x onerror=1>", "group": "G", "sub_class": "s",
                            "classification_basis": "b", "validator_category": "c", "severity": "LOW", "severity_score": 2, "severity_reason": "r",
                            "confidence": "LOW", "transition_id": None, "negative_transition_id": None, "rules": [], "counts": {"failed_assertions": 1, "failed_test_cases": 1},
                            "occurrences": [{"test_case_id": "T", "assertion_id": "A", "expected": "<b>", "actual": "</pre>"}], "evidence": {}}]
        page = fdr.render_html(rep)
        assert "<script>alert(1)" not in page and "<img src=x" not in page and "&lt;script&gt;" in page

    def test_markdown_structure(self, real_report):
        md = fdr.render_markdown(real_report)
        for h in ("# Báo cáo", "## 1. Kết luận", "## 2. Thống kê test case", "## 4. Độ bao phủ", "## 5. Phân loại lỗi", "## 9. Giới hạn", "## 10. Bốn câu hỏi"):
            assert h in md
        assert "| PASS | 122 |" in md and "| SKIPPED (chưa kiểm chứng) | 49 |" in md

    def test_markdown_and_html_show_findings_when_present(self, campaign, real_report):
        rep = copy.deepcopy(real_report)
        o = scenario(campaign, "FI01")
        rep["findings"] = o["findings"]
        md, page = fdr.render_markdown(rep), fdr.render_html(rep)
        assert o["findings"][0]["finding_id"] in md and o["findings"][0]["finding_id"] in page
        assert fdr.NO_FAULT_STATEMENT not in md.split("## 6.")[1].split("## 7.")[0]

    def test_cli_writes_three_files(self, tmp_path):
        prefix = tmp_path / "rep"
        argv = ["--validation", str(F["test_validation_results.json"]), "--execution-results", str(F["test_execution_results.json"]),
                "--test-cases", str(F["test_cases.json"]), "--model", str(F["efsm_model.json"]), "--rules", str(F["security_rules.json"]),
                "--paths", str(F["test_paths.json"]), "--coverage", str(F["test_path_coverage.json"]), "--out-prefix", str(prefix)]
        assert fdr.main(argv) == 0
        assert all(prefix.with_suffix(s).exists() for s in (".json", ".html", ".md"))
        data = json.loads(prefix.with_suffix(".json").read_text(encoding="utf-8"))
        assert data["summary"]["total_test_cases"] == 171 and data["synthetic_fault_injection"]["summary"]["detected"] == 6

    def test_cli_error_exit_code(self, tmp_path, capsys):
        bad = tmp_path / "v.json"
        bad.write_text(json.dumps({"validator": "x"}), encoding="utf-8")
        assert fdr.main(["--validation", str(bad), "--out-prefix", str(tmp_path / "o")]) == 2
        assert "ERROR" in capsys.readouterr().err and not (tmp_path / "o.json").exists()


def _text(page: str) -> list[str]:
    p = _Strict()
    p.feed(page)
    return p.text


# ---------------------------------------------------------------------------
# 21–22. REAL vs SYNTHETIC, kịch bản fault injection FI01–FI06
# ---------------------------------------------------------------------------
class TestSyntheticInjection:
    def test_real_and_synthetic_are_kept_apart(self, real_report):
        assert real_report["source_type"] == "REAL_EXECUTION" and real_report["summary"]["total_findings"] == 0
        syn = real_report["synthetic_fault_injection"]
        assert syn["source_type"] == "SYNTHETIC_INJECTION" and "KHÔNG phải lỗ hổng" in syn["disclaimer"]
        assert syn["summary"]["findings"] > 0
        assert all(f["source_type"] == "SYNTHETIC_INJECTION" for o in syn["scenarios"] for f in o["findings"])
        assert real_report["fault_categories"]["STATE_RESOURCE_LIFECYCLE"] == 0  # synthetic không cộng vào số liệu thực tế

    def test_all_six_scenarios_present(self, campaign):
        assert [o["scenario_id"] for o in campaign["scenarios"]] == ["FI01", "FI02", "FI03", "FI04", "FI05", "FI06"]

    @pytest.mark.parametrize("sid", ["FI01", "FI02", "FI03", "FI04", "FI05", "FI06"])
    def test_scenario_detected_classified_and_traced(self, campaign, sid):
        o = scenario(campaign, sid)
        assert o["validation_status"] == "FAIL" and o["detected"] and o["failed_assertions"] >= 1
        assert o["category_matched"] and o["group_matched"], (o["expected_category"], o["expected_group"])
        hit = [f for f in o["findings"] if f["validator_category"] == o["expected_category"]]
        assert hit and all(f["group"] == o["expected_group"] for f in hit)
        for f in o["findings"]:
            assert f["source_type"] == "SYNTHETIC_INJECTION" and f["severity"] in fdr.SEVERITIES and f["evidence"].get("request")
            assert f["occurrences"][0]["expected"] is not None and f["occurrences"][0]["actual"] is not None and f["test_case_ids"] == [o["test_case_id"]]

    def test_scenario_specific_expectations(self, campaign):
        fi01 = scenario(campaign, "FI01")
        assert any(f["negative_transition_id"] == "NT08" and f["violated_field"] == "STATE" for f in fi01["findings"])
        fi02 = scenario(campaign, "FI02")
        assert any(f["validator_category"] == rv.PAYMENT_LIFECYCLE_VIOLATION for f in fi02["findings"])
        fi03 = scenario(campaign, "FI03")
        assert any(f["violated_field"] == "INV-05" for f in fi03["findings"])
        assert any(f["violated_field"] == "DATA.stock" for f in fi03["findings"])
        fi04 = scenario(campaign, "FI04")
        assert any(f["validator_category"] == rv.GUARD_VIOLATION and f["negative_transition_id"] == "NT02" for f in fi04["findings"])
        fi06 = scenario(campaign, "FI06")
        assert [f["validator_category"] for f in fi06["findings"]] == [rv.UNEXPECTED_SIDE_EFFECT] and fi06["findings"][0]["evidence"]["response"]["status_class"] == "4xx"

    def test_injection_does_not_modify_original_data(self, refs, vin):
        before = hashlib.sha256(json.dumps(refs.execution, sort_keys=True).encode()).hexdigest()
        fdr.run_synthetic_campaign(refs, vin)
        assert hashlib.sha256(json.dumps(refs.execution, sort_keys=True).encode()).hexdigest() == before
        for name in ("test_execution_results.json", "test_validation_results.json", "test_cases.json"):
            assert F[name].exists()

    def test_original_files_unchanged_after_run(self, tmp_path):
        before = {n: hashlib.sha256(p.read_bytes()).hexdigest() for n, p in F.items()}
        fdr.main(["--validation", str(F["test_validation_results.json"]), "--out-prefix", str(tmp_path / "x"),
                  "--execution-results", str(F["test_execution_results.json"]), "--test-cases", str(F["test_cases.json"]),
                  "--model", str(F["efsm_model.json"]), "--rules", str(F["security_rules.json"]),
                  "--paths", str(F["test_paths.json"]), "--coverage", str(F["test_path_coverage.json"])])
        assert before == {n: hashlib.sha256(p.read_bytes()).hexdigest() for n, p in F.items()}

    def test_html_shows_every_scenario_and_labels_them_synthetic(self, real_report):
        page = fdr.render_html(real_report)
        for sid in ("FI01", "FI02", "FI03", "FI04", "FI05", "FI06"):
            assert sid in page
        assert "SYNTHETIC_INJECTION" in page and "KHÔNG phải lỗ hổng" in page

    def test_answers_cover_the_four_questions(self, real_report):
        a = real_report["answers"]
        assert len(a) == 4 and "6/6" in a["4_phat_hien_loi_cai_dat"] and "SYNTHETIC_INJECTION" in a["4_phat_hien_loi_cai_dat"]
        assert "PASS 122" in a["2_dat_that_bai_chua_kiem_chung"] and "Không có finding thực tế" in a["3_sai_lech_va_bang_chung"]

    def test_campaign_is_not_run_without_execution_results(self, validation, refs):
        rep = fdr.build_report(copy.deepcopy(validation), refs, with_synthetic=True)  # không có validation_inputs
        assert rep["synthetic_fault_injection"] is None and "Không chạy" in rep["answers"]["4_phat_hien_loi_cai_dat"]


# ---------------------------------------------------------------------------
# Độc lập và tái lập
# ---------------------------------------------------------------------------
class TestSafety:
    def test_deterministic(self, validation, refs, vin, real_report):
        again = fdr.build_report(copy.deepcopy(validation), refs, validation_inputs=vin)
        assert again == real_report and fdr.render_html(again) == fdr.render_html(real_report) and fdr.render_markdown(again) == fdr.render_markdown(real_report)

    def test_module_does_not_use_http_backend_or_database(self):
        src = Path(fdr.__file__).read_text(encoding="utf-8")
        for needle in ("import httpx", "import requests", "TestClient", "import urllib", "api_test_executor", "from app", "import app", "sqlite3", "sqlalchemy", "import socket"):
            assert needle not in src, needle

    def test_reruns_on_new_data(self, refs, vin):
        """Báo cáo chạy lại được với dữ liệu mới: kết quả theo đầu vào chứ không theo hằng số."""
        doc, local = inject_doc(refs, vin, [(nt_case(refs, "NT08"), fdr._inj_fi01)])
        rep = fdr.build_report(doc, local, source_type=fdr.SYNTHETIC, with_synthetic=False)
        assert rep["source_type"] == "SYNTHETIC_INJECTION" and rep["summary"]["failed"] == 1 and rep["summary"]["total_findings"] == len(rep["findings"]) > 0
        assert rep["conclusion"]["has_findings"] and fdr.NO_FAULT_STATEMENT not in rep["conclusion"]["statement"]
        assert sum(rep["fault_categories"].values()) == rep["summary"]["total_findings"]
        assert sum(rep["severity_counts"].values()) == rep["summary"]["total_findings"]
