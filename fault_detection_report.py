"""BShop Fault Detection Report (Phần 8).

Tổng hợp ``test_validation_results.json`` (Phần 7) thành báo cáo lỗi logic nghiệp vụ: trích xuất assertion FAIL,
phân loại hai nhóm nghiên cứu, đánh giá severity theo tiêu chí tường minh, gộp trùng lặp, truy vết
test case -> path -> transition -> rule -> expected/actual -> evidence, tổng hợp coverage và xuất JSON/HTML/Markdown.

Nguyên tắc:
* Chỉ ĐỌC file JSON; không gửi HTTP, không import backend/executor, không đọc database.
* Không bao giờ tự tạo finding: mỗi finding đến từ ít nhất một assertion FAIL có trong file Phần 7.
  SKIPPED/INCONCLUSIVE là "chưa kiểm chứng", không phải lỗi đã xác nhận.
* Mọi thống kê được tính lại từ dữ liệu đầu vào (không hardcode).
* ``source_type`` phân biệt REAL_EXECUTION với SYNTHETIC_INJECTION. Chiến dịch fault injection chạy trên BẢN SAO
  kết quả thực thi, qua chính Result Validator, và được báo cáo tách biệt khỏi finding thực tế.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

import result_validator as rv

REPORT_NAME = "BShop Fault Detection Report"
SCHEMA_VERSION = "1.0"
REAL, SYNTHETIC = "REAL_EXECUTION", "SYNTHETIC_INJECTION"

GROUP_LIMITS = "LIMITS_AUTHORIZATION"
GROUP_STATE = "STATE_RESOURCE_LIFECYCLE"
GROUP_UNCLASSIFIED = "UNCLASSIFIED"
GROUPS = (GROUP_LIMITS, GROUP_STATE, GROUP_UNCLASSIFIED)
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO", "UNDETERMINED")

NO_FAULT_STATEMENT = "Không phát hiện sai lệch logic nghiệp vụ trong phạm vi các trường hợp đã thực thi và xác minh."
PREDICATE_STATUSES = ("PASS", "FAIL", "SKIPPED", "INCONCLUSIVE")

# Phân loại xác định theo nhãn của Result Validator (mức ưu tiên 1)
STRONG_GROUP_BY_CATEGORY = {
    rv.INVALID_STATE_TRANSITION: GROUP_STATE,
    rv.STOCK_INTEGRITY_VIOLATION: GROUP_STATE,
    rv.PAYMENT_LIFECYCLE_VIOLATION: GROUP_STATE,
    rv.RESOURCE_LIFECYCLE_VIOLATION: GROUP_STATE,
    rv.GUARD_VIOLATION: GROUP_LIMITS,
}
# Nhóm theo negative_category của test case (mức ưu tiên 2)
GROUP_BY_NEGATIVE_CATEGORY = {
    "INPUT_VALIDATION_VIOLATION": GROUP_LIMITS,
    "BUSINESS_CONSTRAINT_VIOLATION": GROUP_LIMITS,
    "INVALID_STATE_TRANSITION": GROUP_STATE,
}
# Nhóm theo rule (mức ưu tiên 3): rule giới hạn dữ liệu/tài nguyên đầu vào
LIMIT_RULES = {"BSR-CART-01", "BSR-CART-02", "BSR-CART-03", "BSR-CART-04", "BSR-CART-05", "BSR-ORDER-01", "BSR-ORDER-02",
               "BSR-ORDER-03", "BSR-STOCK-01", "BSR-STOCK-03", "BSR-PAYMENT-07"}

# Điểm severity (tiêu chí công khai; xem docs/fault_detection_report_design.md)
IMPACT_POINTS = {
    rv.PAYMENT_LIFECYCLE_VIOLATION: ("MONEY", 3), rv.STOCK_INTEGRITY_VIOLATION: ("INVENTORY", 3),
    rv.INVALID_STATE_TRANSITION: ("ORDER_STATE", 2), rv.INVARIANT_VIOLATION: ("INVARIANT", 2),
    rv.GUARD_VIOLATION: ("BUSINESS_LIMIT", 1), rv.RESOURCE_LIFECYCLE_VIOLATION: ("RESOURCE", 1),
    rv.UNEXPECTED_SIDE_EFFECT: ("SIDE_EFFECT", 1), rv.RESPONSE_BODY_MISMATCH: ("INTERFACE_ONLY", 0),
    rv.HTTP_STATUS_MISMATCH: ("INTERFACE_ONLY", 0),
}
INTERFACE_ONLY = {rv.RESPONSE_BODY_MISMATCH, rv.HTTP_STATUS_MISMATCH}
RULE_SEVERITY_POINTS = {"HIGH": 2, "MEDIUM": 1, "LOW": 0}
PII_KEYS = {"customer_name", "phone", "address"}


class ReportInputError(Exception):
    """File Phần 7 không hợp lệ: không tạo báo cáo."""


# ---------------------------------------------------------------------------
# 4.1 Validation Result Loader
# ---------------------------------------------------------------------------
def _read(path: Path, label: str) -> tuple[Any, str]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ReportInputError(f"không đọc được {label} ({path}): {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportInputError(f"{label} không phải JSON hợp lệ: {exc}") from exc


def load_validation(doc: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Kiểm tra schema + nhất quán summary/results. Trả (doc, data_errors). Sai cấu trúc -> ReportInputError."""
    if not isinstance(doc, dict) or doc.get("validator") != rv.VALIDATOR_NAME:
        raise ReportInputError("không phải đầu ra của BShop Result Validator")
    if doc.get("schema_version") != "1.0":
        raise ReportInputError(f"schema_version {doc.get('schema_version')!r} không được hỗ trợ")
    if not isinstance(doc.get("results"), list) or not isinstance(doc.get("summary"), dict):
        raise ReportInputError("thiếu 'summary' hoặc 'results'")
    rows = doc["results"]
    errors: list[dict[str, Any]] = [dict(e, source="validation_file") for e in doc.get("data_errors", []) if isinstance(e, dict)]
    seen: Counter[str] = Counter()
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or r.get("validation_status") not in PREDICATE_STATUSES or not isinstance(r.get("assertions"), list):
            raise ReportInputError(f"results[{i}] sai cấu trúc hoặc validation_status không hợp lệ")
        for a in r["assertions"]:
            if not isinstance(a, dict) or a.get("status") not in ("PASS", "FAIL", "INCONCLUSIVE") or "assertion_id" not in a:
                raise ReportInputError(f"results[{i}] có assertion sai cấu trúc")
        tcid = r.get("test_case_id")
        if tcid:
            seen[tcid] += 1
        elif r["validation_status"] != "SKIPPED":
            errors.append({"code": "MISSING_TEST_CASE_ID", "path_id": r.get("path_id"), "source": "results"})
        fails = sum(1 for a in r["assertions"] if a["status"] == "FAIL")
        if r["validation_status"] == "FAIL" and not fails:
            raise ReportInputError(f"{tcid}: FAIL nhưng không có assertion FAIL (không có bằng chứng)")
        if r["validation_status"] == "PASS" and (fails or not r["assertions"]):
            raise ReportInputError(f"{tcid}: PASS nhưng có assertion FAIL hoặc không có assertion")
    errors.extend({"code": "DUPLICATE_TEST_CASE_ID", "test_case_id": k, "count": n, "source": "results"} for k, n in seen.items() if n > 1)
    s = doc["summary"]
    by = Counter(r["validation_status"] for r in rows)
    expected = {"total": len(rows), "passed": by["PASS"], "failed": by["FAIL"], "skipped": by["SKIPPED"], "inconclusive": by["INCONCLUSIVE"]}
    for key, value in expected.items():
        if s.get(key) != value:
            raise ReportInputError(f"summary.{key}={s.get(key)!r} không khớp results ({value})")
    all_a = [a for r in rows for a in r["assertions"]]
    sa = s.get("assertions", {})
    for key, value in (("total", len(all_a)), ("failed", sum(1 for a in all_a if a["status"] == "FAIL")),
                       ("passed", sum(1 for a in all_a if a["status"] == "PASS")),
                       ("not_evaluable", sum(1 for a in all_a if a["status"] == "INCONCLUSIVE"))):
        if sa.get(key) != value:
            raise ReportInputError(f"summary.assertions.{key}={sa.get(key)!r} không khớp results ({value})")
    return doc, errors


# ---------------------------------------------------------------------------
# Tham chiếu: dữ liệu Phần 2–6 (chỉ đọc)
# ---------------------------------------------------------------------------
class References:
    def __init__(self, *, cases: dict[str, Any], model: dict[str, Any], rules: dict[str, Any], paths: dict[str, Any] | None = None,
                 coverage: dict[str, Any] | None = None, execution: dict[str, Any] | None = None) -> None:
        self.cases_doc, self.model, self.rules_doc = cases, model, rules
        self.paths_doc, self.coverage, self.execution = paths, coverage, execution
        self.cases = {c["test_case_id"]: c for c in cases.get("test_cases", [])}
        self.transitions = {t["id"]: t for t in model.get("transitions", [])}
        self.negatives = {n["id"]: n for n in model.get("negative_transitions", [])}
        self.rules = {r["rule_id"]: r for r in rules.get("rules", [])}
        self.results = {r["test_case_id"]: r for r in (execution or {}).get("results", []) if r.get("test_case_id")}
        self.path_by_id = {p["path_id"]: p for p in (paths or {}).get("paths", [])}

    @classmethod
    def from_files(cls, cases: Path, model: Path, rules: Path, paths: Path | None, coverage: Path | None, execution: Path | None) -> tuple["References", dict[str, str]]:
        hashes: dict[str, str] = {}
        loaded: dict[str, Any] = {}
        for key, p, label in (("cases", cases, "test_cases.json"), ("model", model, "efsm_model.json"), ("rules", rules, "security_rules.json"),
                              ("paths", paths, "test_paths.json"), ("coverage", coverage, "test_path_coverage.json"),
                              ("execution", execution, "test_execution_results.json")):
            if p is None:
                loaded[key] = None
                continue
            loaded[key], hashes[label] = _read(Path(p), label)
        return cls(cases=loaded["cases"], model=loaded["model"], rules=loaded["rules"], paths=loaded["paths"], coverage=loaded["coverage"],
                   execution=loaded["execution"]), hashes

    @property
    def authorization_note(self) -> str:
        excluded = {r.get("rule_id"): r for r in self.rules_doc.get("excluded_rules", []) if isinstance(r, dict)}
        auth = [rid for rid in excluded if "AUTH" in str(rid)]
        if auth:
            return (f"BShop không triển khai xác thực/phân quyền theo người dùng (quy tắc {', '.join(sorted(auth))} bị loại khỏi phạm vi: "
                    "không áp dụng). Vì vậy nhóm LIMITS_AUTHORIZATION chỉ phản ánh giới hạn và ràng buộc nghiệp vụ; "
                    "không có finding nào được gắn nhãn lỗi ownership hay authentication.")
        return ("Không tìm thấy quy tắc phân quyền trong security_rules.json; nhóm LIMITS_AUTHORIZATION chỉ phản ánh giới hạn nghiệp vụ.")


# ---------------------------------------------------------------------------
# 4.2 Fault Extractor
# ---------------------------------------------------------------------------
def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("<redacted>" if k in PII_KEYS else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _snapshot(step_or_state: Any) -> Any:
    return _scrub(step_or_state) if step_or_state is not None else None


def extract_faults(doc: dict[str, Any], refs: References) -> list[dict[str, Any]]:
    """Mỗi assertion FAIL -> một bản ghi thô kèm ngữ cảnh. Không đụng tới SKIPPED/INCONCLUSIVE."""
    out: list[dict[str, Any]] = []
    for row in doc["results"]:
        if row["validation_status"] != "FAIL":
            continue
        tcid = row.get("test_case_id")
        case = refs.cases.get(tcid) or {}
        exe = refs.results.get(tcid)
        for a in row["assertions"]:
            if a["status"] != "FAIL":
                continue
            ev = dict(a.get("evidence") or {})
            step_no = a.get("step_no")
            tid = a.get("transition_id")
            nt_id = row.get("negative_transition_id") if (tid is None or not str(tid).startswith("NT")) else None
            if tid and str(tid).startswith("NT"):
                nt_id = tid
            base = None
            if nt_id and nt_id in refs.negatives:
                base = refs.negatives[nt_id].get("base_transition")
            elif tid in refs.transitions:
                base = tid
            if tid is None and step_no is not None and exe:
                st = next((s for s in exe.get("steps", []) if s.get("step_no") == step_no), None)
                if st:
                    base = base or st.get("transition_id")
                    if st.get("negative_transition_id"):
                        nt_id = st["negative_transition_id"]
                        base = refs.negatives.get(nt_id, {}).get("base_transition", base)
            ev_step = step_no
            if ev_step is None:  # assertion cấp test case: gắn với bước vi phạm đầu tiên, hoặc bước negative cuối
                viol = (a.get("actual") or {}).get("violations") if isinstance(a.get("actual"), dict) else None
                if viol and isinstance(viol[0], dict):
                    ev_step = viol[0].get("step_no")
                elif row.get("test_type") == "NEGATIVE" and exe and exe.get("steps"):
                    ev_step = exe["steps"][-1].get("step_no")
            record = {
                "test_case_id": tcid, "path_id": row.get("path_id"), "path_key": row.get("path_key"), "test_type": row.get("test_type"),
                "transition_id": base, "negative_transition_id": nt_id, "step_no": step_no, "evidence_step_no": ev_step,
                "assertion_id": a["assertion_id"], "check": a.get("check"), "validator_category": a.get("category"),
                "expected": a.get("expected"), "actual": a.get("actual"), "expected_source": a.get("expected_source"),
                "rule_refs": list(a.get("rule_refs") or []), "validation_evidence": _scrub(ev),
                "negative_category": case.get("negative_category"),
            }
            record["evidence"] = collect_evidence(record, exe)
            out.append(record)
    return out


def collect_evidence(rec: dict[str, Any], exe: dict[str, Any] | None) -> dict[str, Any]:
    """Bằng chứng thật từ execution result; không bịa. Thiếu execution result -> chỉ có evidence của Phần 7."""
    ev: dict[str, Any] = {"source_files": ["test_validation_results.json"], "assertion_id": rec["assertion_id"], "test_case_id": rec["test_case_id"]}
    ev_step = rec.get("evidence_step_no")
    if exe is None or ev_step is None:
        ev["limited"] = "không có test_execution_results.json hoặc assertion không gắn với bước nào"
        return ev
    steps = exe.get("steps", [])
    idx = next((i for i, s in enumerate(steps) if s.get("step_no") == ev_step), None)
    if idx is None:
        ev["limited"] = f"không tìm thấy bước {ev_step} trong kết quả thực thi"
        return ev
    st = steps[idx]
    req, resp = st.get("request") or {}, st.get("response") or {}
    ev["source_files"].append("test_execution_results.json")
    ev["evidence_step_no"] = ev_step
    ev["request"] = {"method": req.get("method"), "url": req.get("url"), "path_template": req.get("path_template"), "body": _scrub(req.get("body"))}
    ev["response"] = {"status_code": resp.get("status_code"), "status_class": resp.get("status_class")}
    ev["expected_ref"] = st.get("expected_ref")
    before = exe.get("initial_observed_state") if idx == 0 else steps[idx - 1].get("observed_state")
    ev["observed_state_before"] = _snapshot(before)
    ev["observed_state_after"] = _snapshot(st.get("observed_state"))
    return ev


# ---------------------------------------------------------------------------
# 4.3 Fault Classifier
# ---------------------------------------------------------------------------
def classify(rec: dict[str, Any], refs: References) -> dict[str, Any]:
    cat = rec["validator_category"]
    rules = rec["rule_refs"]
    if cat in STRONG_GROUP_BY_CATEGORY:
        return {"group": STRONG_GROUP_BY_CATEGORY[cat], "basis": f"validator_category={cat}", "sub_class": _sub_class(cat)}
    if rec.get("negative_category") in GROUP_BY_NEGATIVE_CATEGORY:
        return {"group": GROUP_BY_NEGATIVE_CATEGORY[rec["negative_category"]], "basis": f"negative_category={rec['negative_category']}",
                "sub_class": _sub_class(cat)}
    votes = {GROUP_LIMITS if r in LIMIT_RULES else GROUP_STATE for r in rules if r in refs.rules}
    if len(votes) == 1:
        return {"group": votes.pop(), "basis": "rule_refs", "sub_class": _sub_class(cat)}
    reason = "không có rule_refs" if not rules else "các rule liên quan thuộc cả hai nhóm"
    return {"group": GROUP_UNCLASSIFIED, "basis": f"không đủ thông tin: {reason}; validator_category={cat}", "sub_class": _sub_class(cat)}


def _sub_class(cat: str | None) -> str:
    return {rv.GUARD_VIOLATION: "BUSINESS_LIMIT", rv.INVALID_STATE_TRANSITION: "ORDER_STATE", rv.STOCK_INTEGRITY_VIOLATION: "STOCK_LIFECYCLE",
            rv.PAYMENT_LIFECYCLE_VIOLATION: "PAYMENT_LIFECYCLE", rv.RESOURCE_LIFECYCLE_VIOLATION: "RESOURCE_LIFECYCLE",
            rv.UNEXPECTED_SIDE_EFFECT: "SIDE_EFFECT", rv.INVARIANT_VIOLATION: "INVARIANT"}.get(cat or "", "INTERFACE")


# ---------------------------------------------------------------------------
# 4.6 Fault Deduplicator
# ---------------------------------------------------------------------------
def _norm_check(rec: dict[str, Any]) -> str:
    """Trường/ý nghĩa bị vi phạm, độc lập với số bước và ID cụ thể."""
    aid = rec["assertion_id"]
    if aid.startswith("INV-"):
        return aid
    m = re.match(r"S\d+\.(.*)", aid)
    return m.group(1) if m else (rec.get("check") or aid)


def fingerprint(rec: dict[str, Any], refs: References) -> tuple[Any, ...]:
    tr = refs.transitions.get(rec.get("transition_id") or "") or {}
    api = tr.get("api") or {}
    endpoint = f"{api.get('method')} {api.get('path')}" if api else None
    return (rec["validator_category"], rec.get("negative_transition_id") or rec.get("transition_id"), tuple(sorted(rec["rule_refs"])),
            endpoint, _norm_check(rec))


def deduplicate(records: list[dict[str, Any]], refs: References) -> list[list[dict[str, Any]]]:
    """Nhóm bản ghi có cùng loại sai lệch + transition + rule + endpoint + trường vi phạm. Không dựa vào mã HTTP."""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        groups[fingerprint(rec, refs)].append(rec)
    return [groups[k] for k in sorted(groups, key=lambda k: tuple(str(x) for x in k))]


# ---------------------------------------------------------------------------
# 4.4 Severity Analyzer
# ---------------------------------------------------------------------------
def assess_severity(members: list[dict[str, Any]], refs: References) -> dict[str, Any]:
    first = members[0]
    cat = first["validator_category"]
    if cat not in IMPACT_POINTS:
        return {"severity": "UNDETERMINED", "score": None, "criteria": {}, "reason": f"nhãn {cat!r} không có tiêu chí đánh giá"}
    impact_name, impact = IMPACT_POINTS[cat]
    if cat not in INTERFACE_ONLY and any((refs.rules.get(r) or {}).get("category") == "PAYMENT" for r in first["rule_refs"]) and impact < 3:
        impact_name, impact = "MONEY", 3  # sai lệch liên quan trực tiếp quy tắc thanh toán
    rule_pts = max((RULE_SEVERITY_POINTS.get((refs.rules.get(r) or {}).get("severity"), 0) for r in first["rule_refs"]), default=0)
    accepted = any(m["assertion_id"].endswith(".REJECTED") for m in members)
    persisted = any(m["assertion_id"].split(".", 1)[-1].startswith(("STATE", "DATA")) or m["assertion_id"].startswith(("INV-", "A")) for m in members)
    tcs = {m["test_case_id"] for m in members}
    criteria = {"unauthorized_operation_accepted": 2 if accepted else 0, "impact": impact, "impact_domain": impact_name,
                "rule_severity": rule_pts, "persisted_state_or_data_change": 1 if persisted else 0, "reproducible_in_multiple_cases": 1 if len(tcs) >= 2 else 0}
    score = sum(v for k, v in criteria.items() if k != "impact_domain")
    if not first["rule_refs"] and impact == 0:
        return {"severity": "UNDETERMINED", "score": score, "criteria": criteria, "reason": "không có rule liên quan và chỉ sai lệch giao diện"}
    level = "CRITICAL" if score >= 8 else "HIGH" if score >= 6 else "MEDIUM" if score >= 4 else "LOW" if score >= 2 else "INFO"
    if cat in INTERFACE_ONLY and level in ("CRITICAL", "HIGH"):
        level = "MEDIUM"  # sai mã HTTP/body đơn thuần không bao giờ vượt MEDIUM
    return {"severity": level, "score": score, "criteria": criteria,
            "reason": "tổng điểm = " + " + ".join(f"{k}:{v}" for k, v in criteria.items() if k != "impact_domain")}


def assess_confidence(members: list[dict[str, Any]]) -> str:
    ev = members[0]["evidence"]
    strong = ev.get("request") and ev.get("response") and ev.get("observed_state_after") is not None and ev.get("observed_state_before") is not None
    if strong and members[0]["validator_category"] not in INTERFACE_ONLY:
        return "HIGH"
    if ev.get("request") and ev.get("response"):
        return "MEDIUM"
    return "LOW"


# ---------------------------------------------------------------------------
# Tổng hợp finding
# ---------------------------------------------------------------------------
def build_findings(doc: dict[str, Any], refs: References, source_type: str, prefix: str = "F") -> list[dict[str, Any]]:
    records = extract_faults(doc, refs)
    findings: list[dict[str, Any]] = []
    for n, members in enumerate(deduplicate(records, refs), 1):
        first = members[0]
        cls = classify(first, refs)
        sev = assess_severity(members, refs)
        tcs = sorted({m["test_case_id"] for m in members if m["test_case_id"]})
        rules = first["rule_refs"]
        rule_source = "assertion"
        if not rules:
            nt = refs.negatives.get(first.get("negative_transition_id") or "")
            tr = refs.transitions.get(first.get("transition_id") or "")
            rules = list((nt or tr or {}).get("rule_refs", []))
            rule_source = "efsm_model" if rules else "none"
        tr = refs.transitions.get(first.get("transition_id") or "") or {}
        findings.append({
            "finding_id": f"{prefix}{n:03d}", "source_type": source_type, "title": _title(first, refs),
            "group": cls["group"], "sub_class": cls["sub_class"], "classification_basis": cls["basis"],
            "validator_category": first["validator_category"],
            "severity": sev["severity"], "severity_score": sev["score"], "severity_criteria": sev["criteria"], "severity_reason": sev["reason"],
            "confidence": assess_confidence(members),
            "transition_id": first.get("transition_id"), "negative_transition_id": first.get("negative_transition_id"),
            "endpoint": ({"method": tr["api"]["method"], "path": tr["api"]["path"]} if tr.get("api") else None),
            "violated_field": _norm_check(first),
            "rules": [{"rule_id": r, "name": (refs.rules.get(r) or {}).get("name"), "category": (refs.rules.get(r) or {}).get("category"),
                       "severity": (refs.rules.get(r) or {}).get("severity")} for r in rules],
            "rule_refs_source": rule_source,
            "counts": {"failed_assertions": len(members), "failed_test_cases": len(tcs)},
            "test_case_ids": tcs, "path_ids": sorted({m["path_id"] for m in members if m["path_id"]}),
            "occurrences": [{k: m[k] for k in ("test_case_id", "path_id", "path_key", "step_no", "assertion_id", "expected", "actual", "expected_source")}
                            for m in members],
            "evidence": first["evidence"], "validation_evidence": first["validation_evidence"],
        })
    return findings


def _title(rec: dict[str, Any], refs: References) -> str:
    where = rec.get("negative_transition_id") or rec.get("transition_id") or "case-level"
    inv = (refs.negatives.get(rec.get("negative_transition_id") or "") or {}).get("invalid_condition")
    return f"{rec['validator_category']} tại {where}" + (f" ({inv})" if inv else "") + f": {_norm_check(rec)}"


# ---------------------------------------------------------------------------
# 4.7 Coverage Analyzer
# ---------------------------------------------------------------------------
def analyze_coverage(doc: dict[str, Any], refs: References) -> dict[str, Any]:
    model, rows = refs.model, doc["results"]
    transitions = [t["id"] for t in model["transitions"]]
    negs = [n["id"] for n in model["negative_transitions"]]
    states = [s["id"] for s in model["states"]]
    struct = (refs.coverage or {})
    exe_rows = [r for r in rows if r["execution_status"] == "COMPLETED"]
    exec_t: set[str] = set()
    exec_n: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    reached = {"CART"} if exe_rows else set()
    for r in exe_rows:
        res = refs.results.get(r["test_case_id"])
        if not res:
            continue
        seq = []
        for s in res.get("steps", []):
            if s.get("execution_status") != "SENT":
                continue
            if s.get("transition_id"):
                exec_t.add(s["transition_id"])
                if s.get("role") == "path":
                    seq.append(s["transition_id"])
            if s.get("negative_transition_id"):
                exec_n.add(s["negative_transition_id"])
            for o in ((s.get("observed_state") or {}).get("orders") or []):
                reached.add(o.get("status"))
        pairs.update(zip(seq, seq[1:]))
    verified_t: dict[str, str] = {}
    fail_t: set[str] = set()
    for r in rows:
        for a in r["assertions"]:
            tid = a.get("transition_id")
            if not tid:
                continue
            if a["status"] == "FAIL":
                fail_t.add(tid)
            elif a["status"] == "PASS":
                verified_t.setdefault(tid, "PASS")
    verified = sorted(t for t in verified_t if t not in fail_t)
    possible_pairs = sorted((a["id"], b["id"]) for a in model["transitions"] for b in model["transitions"] if a["target"] == b["source"])
    rule_ids = sorted(refs.rules)
    rules_pass = {rid for r in rows for a in r["assertions"] if a["status"] == "PASS" for rid in a.get("rule_refs", [])}
    rules_fail = {rid for r in rows for a in r["assertions"] if a["status"] == "FAIL" for rid in a.get("rule_refs", [])}
    # rule thực thi: rule_refs của test case đã chạy
    rules_exec = {rid for r in exe_rows for rid in ((refs.cases.get(r["test_case_id"]) or {}).get("security_rule_ids") or [])}
    not_exec = [r for r in rows if r["validation_status"] == "SKIPPED"]
    inconclusive = [r for r in rows if r["validation_status"] == "INCONCLUSIVE"]
    skipped_reasons = Counter((r["reasons"][0].get("code") if r.get("reasons") else "UNKNOWN") for r in not_exec)
    path_info = (refs.paths_doc or {}).get("summary", {})
    unexecuted_paths = [{"path_id": r.get("path_id"), "path_key": r.get("path_key"),
                         "reason": (r["reasons"][0] if r.get("reasons") else None)} for r in not_exec]
    return {
        "test_cases": {"total": len(rows), "executed": len(exe_rows), "not_executed": len(not_exec), "inconclusive": len(inconclusive),
                       "executed_by_type": dict(Counter(r["test_type"] for r in exe_rows)), "not_executed_by_reason": dict(skipped_reasons)},
        "structural": {k: struct.get(k) for k in ("states", "positive_transitions", "transition_pairs", "negative_transitions")} if struct else None,
        "path_planning": {"paths_total": path_info.get("total_paths"), "paths_by_feasibility": path_info.get("paths_by_feasibility")} if path_info else None,
        "executed": {
            "states": {"total": len(states), "reached": sorted(s for s in reached if s in states), "not_reached": sorted(set(states) - reached)},
            "transitions": {"total": len(transitions), "executed": sorted(exec_t), "not_executed": sorted(set(transitions) - exec_t),
                            "ratio": _ratio(len(exec_t), len(transitions))},
            "negative_transitions": {"total": len(negs), "executed": sorted(exec_n), "not_executed": sorted(set(negs) - exec_n),
                                     "ratio": _ratio(len(exec_n), len(negs))},
            "transition_pairs": {"possible_by_model": len(possible_pairs), "executed": len(pairs), "ratio_of_possible": _ratio(len(pairs), len(possible_pairs)),
                                 "feasible_reference": (struct.get("transition_pairs") or {}).get("covered_feasible") if struct else None,
                                 "not_executed": [f"{a}>{b}" for a, b in possible_pairs if (a, b) not in pairs]},
        },
        "verified": {
            "transitions_with_pass_and_no_fail": verified, "transitions_with_fail": sorted(fail_t),
            "rules": {"implemented_total": len(rule_ids), "verified_by_pass": sorted(rules_pass - rules_fail), "with_fail": sorted(rules_fail),
                      "executed": sorted(rules_exec), "not_verified": sorted(set(rule_ids) - rules_pass),
                      "ratio_verified": _ratio(len(rules_pass - rules_fail), len(rule_ids))},
        },
        "unverified_areas": {"not_executed_paths": unexecuted_paths, "inconclusive_cases": [r.get("test_case_id") for r in inconclusive],
                             "notes": ["Độ bao phủ cấu trúc (structural) khác độ bao phủ khả thi (feasible) và khác độ bao phủ thực thi (executed).",
                                       "Bao phủ 100% transition không đồng nghĩa với 100% bảo mật nghiệp vụ."]},
    }


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


# ---------------------------------------------------------------------------
# Synthetic fault injection (chạy qua Result Validator, trên bản sao)
# ---------------------------------------------------------------------------
def _states_from(result: dict[str, Any], step_no: int):
    for s in result["steps"]:
        if s["step_no"] >= step_no and isinstance(s.get("observed_state"), dict):
            yield s["observed_state"]
    if isinstance(result.get("final_observed_state"), dict):
        yield result["final_observed_state"]


def _apply(result: dict[str, Any], from_step: int, fn: Callable[[dict[str, Any]], None]) -> None:
    for st in _states_from(result, from_step):
        fn(st)


def _step(result: dict[str, Any], n: int) -> dict[str, Any]:
    return next(s for s in result["steps"] if s["step_no"] == n)


def _find_case(refs: References, *, nt: str | None = None, last_transition: str | None = None) -> dict[str, Any]:
    for c in refs.cases_doc["test_cases"]:
        if nt and c.get("negative_transition_id") == nt and c["test_case_id"] in refs.results:
            return c
        if last_transition and c.get("type") == "positive" and c.get("readiness") == "READY" and c["test_case_id"] in refs.results:
            trs = [s.get("transition_id") for s in c["steps"] if s.get("role") == "path"]
            if trs and trs[-1] == last_transition and "T07" in trs[-1:]:
                return c
    raise ReportInputError(f"không tìm thấy test case phù hợp cho fault injection ({nt or last_transition})")


def _inj_fi01(res, case):  # PAID -> CANCELLED được chấp nhận
    n = case["steps"][-1]["step_no"]
    st = _step(res, n)
    st["response"].update(status_code=200, status_class="2xx")
    _apply(res, n, lambda s: [o.update(status="CANCELLED") for o in s["orders"]])


def _inj_fi02(res, case):  # thanh toán lại đơn PAID thành công
    n = case["steps"][-1]["step_no"]
    _step(res, n)["response"].update(status_code=200, status_class="2xx")

    def dup(s):
        last = max(s["payments"], key=lambda p: p["id"])
        s["payments"].append(dict(last, id=last["id"] + 1000, result="SUCCESS"))
    _apply(res, n, dup)


def _inj_fi03(res, case):  # hủy lần hai bị từ chối nhưng vẫn hoàn kho
    n = case["steps"][-1]["step_no"]
    order = next(o for o in _step(res, n)["observed_state"]["orders"])
    qty = {i["product_id"]: i["quantity"] for i in order["items"]}

    def refund(s):
        for pid, q in qty.items():
            s["product_stock"][str(pid)] += q
    _apply(res, n, refund)


def _inj_fi04(res, case):  # API chấp nhận số lượng vượt tồn kho
    n = case["steps"][-1]["step_no"]
    body = _step(res, n)["request"]["body"]
    _step(res, n)["response"].update(status_code=200, status_class="2xx")

    def add(s):
        s["cart"]["items"].append({"product_id": body["product_id"], "quantity": body["quantity"]})
    _apply(res, n, add)


def _inj_fi05(res, case):  # thanh toán thất bại nhưng đơn thành PAID
    n = case["steps"][-1]["step_no"]
    _apply(res, n, lambda s: [o.update(status="PAID") for o in s["orders"]])


def _inj_fi06(res, case):  # mã từ chối đúng nhưng dữ liệu vẫn đổi
    n = case["steps"][-1]["step_no"]
    _apply(res, n, lambda s: s["cart"]["items"].append({"product_id": 2, "quantity": 1}))


SCENARIOS: list[dict[str, Any]] = [
    {"id": "FI01", "title": "Hủy đơn đã thanh toán (PAID → CANCELLED)", "case": {"nt": "NT08"}, "inject": _inj_fi01,
     "expect": {"group": GROUP_STATE, "validator_category": rv.INVALID_STATE_TRANSITION}},
    {"id": "FI02", "title": "Thanh toán lại đơn đã PAID thành công", "case": {"nt": "NT07"}, "inject": _inj_fi02,
     "expect": {"group": GROUP_STATE, "validator_category": rv.PAYMENT_LIFECYCLE_VIOLATION}},
    {"id": "FI03", "title": "Hủy hai lần làm tồn kho được hoàn hai lần", "case": {"nt": "NT09"}, "inject": _inj_fi03,
     "expect": {"group": GROUP_STATE, "validator_category": rv.STOCK_INTEGRITY_VIOLATION}},
    {"id": "FI04", "title": "Chấp nhận số lượng vượt tồn kho", "case": {"nt": "NT02"}, "inject": _inj_fi04,
     "expect": {"group": GROUP_LIMITS, "validator_category": rv.GUARD_VIOLATION}},
    {"id": "FI05", "title": "Thanh toán thất bại nhưng đơn chuyển PAID", "case": {"last_transition": "T07"}, "inject": _inj_fi05,
     "expect": {"group": GROUP_STATE, "validator_category": rv.INVALID_STATE_TRANSITION}},
    {"id": "FI06", "title": "Mã từ chối hợp lệ nhưng dữ liệu vẫn bị thay đổi", "case": {"nt": "NT01"}, "inject": _inj_fi06,
     "expect": {"group": GROUP_LIMITS, "validator_category": rv.UNEXPECTED_SIDE_EFFECT}},
]


def run_synthetic_campaign(refs: References, validation_inputs: rv.Inputs) -> dict[str, Any]:
    """Cài lỗi vào BẢN SAO execution result -> Result Validator -> Fault Reporter. Không sửa dữ liệu gốc, không gọi API."""
    validator = rv.ResultValidator(validation_inputs)
    outcomes = []
    for sc in SCENARIOS:
        case = _find_case(refs, **sc["case"])
        original = refs.results[case["test_case_id"]]
        snapshot = json.dumps(original, sort_keys=True)
        mutated = copy.deepcopy(original)
        sc["inject"](mutated, case)
        row = validator.validate_case(copy.deepcopy(case), mutated)
        vdoc = validator._document([row], [])
        scenario_refs = copy.copy(refs)
        scenario_refs.results = {**refs.results, case["test_case_id"]: mutated}
        findings = build_findings(vdoc, scenario_refs, SYNTHETIC, prefix=f"{sc['id']}-")
        exp = sc["expect"]
        hit = [f for f in findings if f["validator_category"] == exp["validator_category"]]
        outcomes.append({
            "scenario_id": sc["id"], "title": sc["title"], "source_type": SYNTHETIC, "test_case_id": case["test_case_id"],
            "negative_transition_id": case.get("negative_transition_id"), "validation_status": row["validation_status"],
            "detected": row["validation_status"] == "FAIL", "expected_category": exp["validator_category"], "expected_group": exp["group"],
            "category_matched": bool(hit), "group_matched": any(f["group"] == exp["group"] for f in hit),
            "failed_assertions": row["assertion_counts"]["failed"], "findings": findings,
            "original_data_unchanged": json.dumps(original, sort_keys=True) == snapshot and mutated is not original,
        })
    return {
        "source_type": SYNTHETIC, "disclaimer": "Lỗi cài đặt có chủ đích vào bản sao dữ liệu thực thi; KHÔNG phải lỗ hổng của BShop.",
        "scenarios": outcomes,
        "summary": {"total": len(outcomes), "detected": sum(o["detected"] for o in outcomes),
                    "category_matched": sum(o["category_matched"] for o in outcomes), "group_matched": sum(o["group_matched"] for o in outcomes),
                    "findings": sum(len(o["findings"]) for o in outcomes)},
    }


# ---------------------------------------------------------------------------
# Báo cáo
# ---------------------------------------------------------------------------
def build_report(doc: dict[str, Any], refs: References, *, validation_inputs: rv.Inputs | None = None, source_type: str = REAL,
                 hashes: dict[str, str] | None = None, with_synthetic: bool = True, validation_hash: str | None = None) -> dict[str, Any]:
    doc, data_errors = load_validation(doc)
    findings = build_findings(doc, refs, source_type)
    s = doc["summary"]
    group_counts = Counter(f["group"] for f in findings)
    sev_counts = Counter(f["severity"] for f in findings)
    cat_counts = Counter(f["validator_category"] for f in findings)
    failed_assertions = s["assertions"]["failed"]
    failed_cases = sum(1 for r in doc["results"] if r["validation_status"] == "FAIL")
    not_evaluable = s["assertions"]["not_evaluable"]
    coverage = analyze_coverage(doc, refs)
    fallbacks = sum(1 for r in doc["results"] for a in r["assertions"] if "final_observed_state" in str((a.get("evidence") or {}).get("actual_from", "")))
    skipped = s["skipped"]
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "project": "BShop", "report_type": "FAULT_DETECTION_REPORT", "reporter": REPORT_NAME,
        "source_type": source_type,
        "inputs": {"validation_file_sha256": validation_hash, "validation_sources": doc.get("sources"), "files_sha256": hashes or {}},
        "summary": {
            "total_test_cases": s["total"], "passed": s["passed"], "failed": s["failed"], "skipped": s["skipped"], "inconclusive": s["inconclusive"],
            "by_test_type": s.get("by_test_type"), "assertions": s["assertions"],
            "total_findings": len(findings),
            "fault_metrics": {"failed_assertions": failed_assertions, "failed_test_cases": failed_cases, "distinct_findings": len(findings),
                              "note": "ba chỉ số khác nhau: số assertion FAIL, số test case FAIL, số finding sau khi gộp trùng lặp; root_cause_clusters (trong summary) đếm theo transition/negative transition"},
            "findings_by_source": dict(Counter(f["source_type"] for f in findings)),
            "root_cause_clusters": len({(f["negative_transition_id"] or f["transition_id"]) for f in findings}),
        },
        "fault_categories": {g: group_counts.get(g, 0) for g in GROUPS},
        "severity_counts": {sv: sev_counts.get(sv, 0) for sv in SEVERITIES},
        "validator_categories": dict(sorted(cat_counts.items())),
        "authorization_scope_note": refs.authorization_note,
        "findings": findings,
        "coverage": coverage,
        "data_errors": data_errors,
        "conclusion": {"has_findings": bool(findings),
                       "statement": (NO_FAULT_STATEMENT if not findings and not data_errors else
                                     f"Phát hiện {len(findings)} finding ({failed_assertions} assertion FAIL, {failed_cases} test case FAIL) trong phạm vi đã thực thi và xác minh."
                                     if findings else NO_FAULT_STATEMENT + " Lưu ý: dữ liệu đầu vào có lỗi cần xem xét."),
                       "scope_caveat": (f"Phạm vi: {coverage['test_cases']['executed']} test case đã thực thi; {skipped} chưa thực thi, "
                                        f"{s['inconclusive']} chưa kết luận được. PASS không có nghĩa hệ thống an toàn tuyệt đối.")},
    }
    synthetic = None
    if with_synthetic and validation_inputs is not None:
        synthetic = run_synthetic_campaign(refs, validation_inputs)
    report["synthetic_fault_injection"] = synthetic
    report["limitations"] = _limitations(report, skipped, fallbacks, refs)
    report["answers"] = _answers(report)
    return report


def _limitations(report: dict[str, Any], skipped: int, fallbacks: int, refs: References) -> list[str]:
    cov = report["coverage"]
    lim = [f"{skipped} test case/path chưa được thực thi ({_fmt_counter(cov['test_cases']['not_executed_by_reason'])}); khu vực này chưa được kiểm chứng.",
           f"{report['summary']['inconclusive']} test case INCONCLUSIVE (chưa kết luận)."]
    lim.append("Kết quả chỉ phản ánh những gì quan sát được qua các GET API của Phần 6 và các assertion đã mô hình hóa; không đọc database trực tiếp.")
    if fallbacks:
        lim.append(f"{fallbacks} assertion hậu điều kiện negative dùng final_observed_state thay cho probe độc lập (probe thiếu path param ở Phần 6).")
    lim.append(report["authorization_scope_note"])
    lim.append("Độ bao phủ cấu trúc, khả thi và thực thi là ba khái niệm khác nhau; bao phủ transition không đồng nghĩa bảo mật nghiệp vụ đầy đủ.")
    lim.append("Fault injection chỉ chứng minh khả năng phát hiện của công cụ trên các mẫu lỗi đã chọn; không phải lỗ hổng thực tế của BShop và không chứng minh phát hiện được mọi lỗi.")
    if cov["executed"]["transition_pairs"]["not_executed"]:
        lim.append("Các cặp transition chưa thực thi: " + ", ".join(cov["executed"]["transition_pairs"]["not_executed"]) + ".")
    return lim


def _fmt_counter(c: dict[str, int]) -> str:
    return ", ".join(f"{k}: {v}" for k, v in sorted(c.items())) or "không có"


def _answers(report: dict[str, Any]) -> dict[str, str]:
    s, cov = report["summary"], report["coverage"]
    syn = report.get("synthetic_fault_injection")
    return {
        "1_da_kiem_thu_gi": (f"{cov['test_cases']['executed']} test case đã thực thi ({_fmt_counter(cov['test_cases']['executed_by_type'])}); "
                             f"{s['assertions']['total']} assertion; transition thực thi {len(cov['executed']['transitions']['executed'])}/{cov['executed']['transitions']['total']}, "
                             f"negative transition {len(cov['executed']['negative_transitions']['executed'])}/{cov['executed']['negative_transitions']['total']}, "
                             f"rule được xác minh bằng PASS {len(cov['verified']['rules']['verified_by_pass'])}/{cov['verified']['rules']['implemented_total']}."),
        "2_dat_that_bai_chua_kiem_chung": f"PASS {s['passed']}, FAIL {s['failed']}, SKIPPED {s['skipped']} (chưa kiểm chứng), INCONCLUSIVE {s['inconclusive']} (chưa kết luận).",
        "3_sai_lech_va_bang_chung": ("Không có finding thực tế." if not report["findings"] else
                                     f"{len(report['findings'])} finding; xem mục findings (nhóm, severity, expected/actual, evidence)."),
        "4_phat_hien_loi_cai_dat": ("Không chạy chiến dịch fault injection." if not syn else
                                    f"Phát hiện {syn['summary']['detected']}/{syn['summary']['total']} lỗi cài đặt; đúng nhãn {syn['summary']['category_matched']}/{syn['summary']['total']}; "
                                    f"đúng nhóm nghiên cứu {syn['summary']['group_matched']}/{syn['summary']['total']} (source_type=SYNTHETIC_INJECTION, không phải lỗi thực tế)."),
    }


# ---------------------------------------------------------------------------
# Xuất Markdown / HTML (tách khỏi phân tích)
# ---------------------------------------------------------------------------
def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def render_markdown(r: dict[str, Any]) -> str:
    s, cov = r["summary"], r["coverage"]
    L: list[str] = [f"# Báo cáo phát hiện sai lệch logic nghiệp vụ – {r['project']}", "",
                    f"Loại nguồn: **{r['source_type']}**. Báo cáo được tính lại từ `test_validation_results.json`.", "",
                    "## 1. Kết luận", "", f"> {r['conclusion']['statement']}", "", r["conclusion"]["scope_caveat"], "",
                    "## 2. Thống kê test case", "", "| Trạng thái | Số lượng |", "|---|---|"]
    for k, lab in (("total_test_cases", "Tổng"), ("passed", "PASS"), ("failed", "FAIL"), ("skipped", "SKIPPED (chưa kiểm chứng)"), ("inconclusive", "INCONCLUSIVE")):
        L.append(f"| {lab} | {s[k]} |")
    a = s["assertions"]
    L += ["", "## 3. Thống kê assertion", "", f"Tổng {a['total']}; đã đánh giá {a['evaluated']}; PASS {a['passed']}; FAIL {a['failed']}; không thể đánh giá {a['not_evaluable']}.", "",
          f"Ba chỉ số lỗi: {s['fault_metrics']['failed_assertions']} assertion FAIL, {s['fault_metrics']['failed_test_cases']} test case FAIL, "
          f"{s['fault_metrics']['distinct_findings']} finding sau khi gộp trùng lặp.", "", "## 4. Độ bao phủ", ""]
    ex = cov["executed"]
    L += ["| Chỉ số | Thực thi |", "|---|---|",
          f"| Trạng thái | {len(ex['states']['reached'])}/{ex['states']['total']} |",
          f"| Transition | {len(ex['transitions']['executed'])}/{ex['transitions']['total']} |",
          f"| Negative transition | {len(ex['negative_transitions']['executed'])}/{ex['negative_transitions']['total']} |",
          f"| Cặp transition (theo mô hình) | {ex['transition_pairs']['executed']}/{ex['transition_pairs']['possible_by_model']} |",
          f"| Rule được xác minh (PASS) | {len(cov['verified']['rules']['verified_by_pass'])}/{cov['verified']['rules']['implemented_total']} |", ""]
    if cov.get("structural"):
        st = cov["structural"]
        L += [f"Tham chiếu cấu trúc (Phần 4): cặp transition cấu trúc {st['transition_pairs']['covered_structural']}/{st['transition_pairs']['possible']}, "
              f"khả thi {st['transition_pairs']['covered_feasible']}. Cấu trúc ≠ khả thi ≠ thực thi.", ""]
    L += ["## 5. Phân loại lỗi", "", "| Nhóm | Số finding |", "|---|---|"] + [f"| {g} | {n} |" for g, n in r["fault_categories"].items()]
    L += ["", "| Severity | Số finding |", "|---|---|"] + [f"| {k} | {v} |" for k, v in r["severity_counts"].items()]
    L += ["", f"_{r['authorization_scope_note']}_", "", "## 6. Findings thực tế", ""]
    L += _md_findings(r["findings"]) if r["findings"] else [NO_FAULT_STATEMENT, ""]
    syn = r.get("synthetic_fault_injection")
    if syn:
        L += ["## 7. Fault injection (SYNTHETIC_INJECTION)", "", f"_{syn['disclaimer']}_", "",
              "| ID | Kịch bản | Test case | Phát hiện | Đúng nhãn | Đúng nhóm | Severity |", "|---|---|---|---|---|---|---|"]
        for o in syn["scenarios"]:
            sev = ", ".join(sorted({f["severity"] for f in o["findings"] if f["validator_category"] == o["expected_category"]})) or "-"
            L.append(f"| {o['scenario_id']} | {o['title']} | {o['test_case_id']} | {'có' if o['detected'] else 'KHÔNG'} | {'có' if o['category_matched'] else 'không'} | "
                     f"{'có' if o['group_matched'] else 'không'} | {sev} |")
        L += ["", f"Tổng: phát hiện {syn['summary']['detected']}/{syn['summary']['total']}, đúng nhãn {syn['summary']['category_matched']}, "
              f"đúng nhóm {syn['summary']['group_matched']}.", ""]
    L += ["## 8. Chưa được kiểm chứng", ""]
    L += [f"- {p['path_id']} `{p['path_key']}`: {(p['reason'] or {}).get('code')}" for p in cov["unverified_areas"]["not_executed_paths"][:60]]
    if len(cov["unverified_areas"]["not_executed_paths"]) > 60:
        L.append(f"- … và {len(cov['unverified_areas']['not_executed_paths']) - 60} path khác (xem JSON).")
    L += ["", "## 9. Giới hạn", ""] + [f"- {x}" for x in r["limitations"]]
    L += ["", "## 10. Bốn câu hỏi", ""] + [f"- **{k}**: {v}" for k, v in r["answers"].items()]
    if r["data_errors"]:
        L += ["", "## Lỗi dữ liệu đầu vào", ""] + [f"- {_j(e)}" for e in r["data_errors"]]
    return "\n".join(L) + "\n"


def _md_findings(findings: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for f in findings:
        out += [f"### {f['finding_id']} – {f['title']}", "",
                f"- Nhóm: **{f['group']}** ({f['sub_class']}); nhãn Result Validator: `{f['validator_category']}`; cơ sở: {f['classification_basis']}",
                f"- Severity: **{f['severity']}** (điểm {f['severity_score']}); độ tin cậy: {f['confidence']}; nguồn: {f['source_type']}",
                f"- Transition: {f['transition_id']}; negative: {f['negative_transition_id']}; rule: {', '.join(x['rule_id'] for x in f['rules']) or '-'}",
                f"- Số assertion FAIL: {f['counts']['failed_assertions']}; số test case: {f['counts']['failed_test_cases']} ({', '.join(f['test_case_ids'])})"]
        for o in f["occurrences"][:3]:
            out.append(f"- {o['test_case_id']} / {o['assertion_id']}: expected `{_j(o['expected'])}` — actual `{_j(o['actual'])}`")
        out.append("")
    return out


_CSS = """:root{--bg:#fff;--fg:#1d2430;--mut:#5b6678;--line:#d9dee7;--card:#f5f7fa;--ok:#17653a;--bad:#a1260f;--warn:#8a5a00;--acc:#1f4fa3}
@media (prefers-color-scheme:dark){:root{--bg:#12161c;--fg:#e6eaf0;--mut:#9aa6b8;--line:#2b3340;--card:#1a2029;--ok:#6fd39a;--bad:#ff8c78;--warn:#f1c068;--acc:#8db4ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,Segoe UI,sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 64px}h1{font-size:1.6rem}h2{margin-top:2rem;border-bottom:1px solid var(--line);padding-bottom:.3rem}
table{border-collapse:collapse;width:100%;margin:.6rem 0}th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{background:var(--card)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:.7rem 0}.mut{color:var(--mut)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.tag{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:0 8px;font-size:.8rem}
code,pre{font:13px/1.45 ui-monospace,Consolas,monospace}pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px}
.table-wrap{overflow-x:auto}@media(max-width:600px){main{padding:16px 12px}}"""


def render_html(r: dict[str, Any]) -> str:
    e = html.escape
    s, cov = r["summary"], r["coverage"]
    ex = cov["executed"]

    def table(head: list[str], rows: list[list[Any]]) -> str:
        h = "".join(f"<th>{e(str(x))}</th>" for x in head)
        b = "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in row) + "</tr>" for row in rows)
        return f'<div class="table-wrap"><table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>'

    parts = [f"<h1>Báo cáo phát hiện sai lệch logic nghiệp vụ – {e(r['project'])}</h1>",
             f'<p class="mut">Nguồn dữ liệu: <span class="tag">{e(r["source_type"])}</span> · Tính lại từ <code>test_validation_results.json</code></p>',
             "<h2>1. Tổng quan kết quả</h2>",
             f'<div class="card"><strong class="{"bad" if r["findings"] else "ok"}">{e(r["conclusion"]["statement"])}</strong>'
             f'<p class="mut">{e(r["conclusion"]["scope_caveat"])}</p></div>',
             "<h2>2. Thống kê test case</h2>",
             table(["Trạng thái", "Số lượng"], [["Tổng", s["total_test_cases"]], ["PASS", s["passed"]], ["FAIL", s["failed"]],
                                                ["SKIPPED (chưa kiểm chứng)", s["skipped"]], ["INCONCLUSIVE", s["inconclusive"]]]),
             "<h2>3. Thống kê assertion</h2>",
             table(["Tổng", "Đã đánh giá", "PASS", "FAIL", "Không thể đánh giá"],
                   [[s["assertions"]["total"], s["assertions"]["evaluated"], s["assertions"]["passed"], s["assertions"]["failed"], s["assertions"]["not_evaluable"]]]),
             f'<p>Ba chỉ số khác nhau: <strong>{s["fault_metrics"]["failed_assertions"]}</strong> assertion FAIL, <strong>{s["fault_metrics"]["failed_test_cases"]}</strong> test case FAIL, '
             f'<strong>{s["fault_metrics"]["distinct_findings"]}</strong> finding sau khi gộp trùng lặp.</p>',
             "<h2>4. Độ bao phủ</h2>",
             table(["Chỉ số", "Thực thi", "Chưa thực thi"],
                   [["Trạng thái", f"{len(ex['states']['reached'])}/{ex['states']['total']}", ", ".join(ex["states"]["not_reached"]) or "-"],
                    ["Transition", f"{len(ex['transitions']['executed'])}/{ex['transitions']['total']}", ", ".join(ex["transitions"]["not_executed"]) or "-"],
                    ["Negative transition", f"{len(ex['negative_transitions']['executed'])}/{ex['negative_transitions']['total']}", ", ".join(ex["negative_transitions"]["not_executed"]) or "-"],
                    ["Cặp transition (theo mô hình)", f"{ex['transition_pairs']['executed']}/{ex['transition_pairs']['possible_by_model']}", ", ".join(ex["transition_pairs"]["not_executed"]) or "-"],
                    ["Rule xác minh bằng PASS", f"{len(cov['verified']['rules']['verified_by_pass'])}/{cov['verified']['rules']['implemented_total']}",
                     ", ".join(cov["verified"]["rules"]["not_verified"]) or "-"]]),
             '<p class="mut">Độ bao phủ cấu trúc, khả thi và thực thi là ba khái niệm khác nhau; bao phủ transition không đồng nghĩa bảo mật nghiệp vụ đầy đủ.</p>',
             "<h2>5. Phân loại lỗi theo hai nhóm nghiên cứu</h2>",
             table(["Nhóm", "Số finding"], [[g, n] for g, n in r["fault_categories"].items()]),
             f'<p class="mut">{e(r["authorization_scope_note"])}</p>',
             "<h2>6. Mức độ nghiêm trọng</h2>", table(["Severity", "Số finding"], [[k, v] for k, v in r["severity_counts"].items()]),
             "<h2>7. Findings thực tế và bằng chứng</h2>"]
    if not r["findings"]:
        parts.append(f'<div class="card ok">{e(NO_FAULT_STATEMENT)}</div>')
    parts.extend(_html_finding(f) for f in r["findings"])
    syn = r.get("synthetic_fault_injection")
    if syn:
        parts += ["<h2>8. Fault injection <span class='tag'>SYNTHETIC_INJECTION</span></h2>", f'<p class="warn">{e(syn["disclaimer"])}</p>',
                  table(["ID", "Kịch bản", "Test case", "Phát hiện", "Đúng nhãn", "Đúng nhóm", "Severity"],
                        [[o["scenario_id"], o["title"], o["test_case_id"], "có" if o["detected"] else "KHÔNG", "có" if o["category_matched"] else "không",
                          "có" if o["group_matched"] else "không",
                          ", ".join(sorted({f["severity"] for f in o["findings"] if f["validator_category"] == o["expected_category"]})) or "-"] for o in syn["scenarios"]])]
        for o in syn["scenarios"]:
            parts.extend(_html_finding(f) for f in o["findings"])
    parts += ["<h2>9. Chưa được kiểm chứng</h2>",
              table(["Path", "Path key", "Lý do"], [[p["path_id"], p["path_key"], (p["reason"] or {}).get("code")] for p in cov["unverified_areas"]["not_executed_paths"]]),
              "<h2>10. Giới hạn của kết quả</h2>", "<ul>" + "".join(f"<li>{e(x)}</li>" for x in r["limitations"]) + "</ul>",
              "<h2>Bốn câu hỏi</h2>", "<ul>" + "".join(f"<li><strong>{e(k)}</strong>: {e(v)}</li>" for k, v in r["answers"].items()) + "</ul>"]
    if r["data_errors"]:
        parts += ["<h2>Lỗi dữ liệu đầu vào</h2><ul>" + "".join(f"<li><code>{e(_j(x))}</code></li>" for x in r["data_errors"]) + "</ul>"]
    return ('<!DOCTYPE html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Fault Detection Report</title><style>{_CSS}</style></head><body><main>{''.join(parts)}</main></body></html>\n")


def _html_finding(f: dict[str, Any]) -> str:
    e = html.escape
    occ = "".join(f"<li>{e(str(o['test_case_id']))} / {e(o['assertion_id'])}: expected <code>{e(_j(o['expected']))}</code> — actual <code>{e(_j(o['actual']))}</code></li>"
                  for o in f["occurrences"][:5])
    ev = {k: f["evidence"].get(k) for k in ("request", "response", "expected_ref", "observed_state_before", "observed_state_after") if f["evidence"].get(k) is not None}
    return (f'<div class="card"><h3>{e(f["finding_id"])} – {e(f["title"])}</h3>'
            f'<p><span class="tag">{e(f["source_type"])}</span> <span class="tag">{e(f["group"])}</span> <span class="tag">{e(f["validator_category"])}</span> '
            f'<span class="tag">severity {e(f["severity"])}</span> <span class="tag">confidence {e(f["confidence"])}</span></p>'
            f'<p class="mut">Cơ sở phân loại: {e(f["classification_basis"])} · Severity: {e(f["severity_reason"])}</p>'
            f'<p>Transition {e(str(f["transition_id"]))}; negative {e(str(f["negative_transition_id"]))}; rule {e(", ".join(x["rule_id"] for x in f["rules"]) or "-")}; '
            f'{f["counts"]["failed_assertions"]} assertion FAIL / {f["counts"]["failed_test_cases"]} test case</p><ul>{occ}</ul>'
            f'<details><summary>Bằng chứng</summary><pre>{e(json.dumps(ev, ensure_ascii=False, indent=1, sort_keys=True))}</pre></details></div>')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=REPORT_NAME)
    p.add_argument("--validation", default="test_validation_results.json")
    p.add_argument("--execution-results", default="test_execution_results.json")
    p.add_argument("--test-cases", default="test_cases.json")
    p.add_argument("--model", default="efsm_model.json")
    p.add_argument("--rules", default="security_rules.json")
    p.add_argument("--paths", default="test_paths.json")
    p.add_argument("--coverage", default="test_path_coverage.json")
    p.add_argument("--out-prefix", default="fault_detection_report", help="ghi <prefix>.json/.html/.md")
    p.add_argument("--no-synthetic", action="store_true", help="bỏ chiến dịch fault injection")
    args = p.parse_args(argv)
    try:
        doc, vh = _read(Path(args.validation), "test_validation_results.json")
        refs, hashes = References.from_files(Path(args.test_cases), Path(args.model), Path(args.rules),
                                             Path(args.paths) if Path(args.paths).exists() else None,
                                             Path(args.coverage) if Path(args.coverage).exists() else None,
                                             Path(args.execution_results) if Path(args.execution_results).exists() else None)
        vin = None
        if not args.no_synthetic and refs.execution is not None:
            vin = rv.Inputs(refs.cases_doc, refs.execution, refs.model, refs.rules_doc)
        report = build_report(doc, refs, validation_inputs=vin, hashes=hashes, validation_hash=vh, with_synthetic=not args.no_synthetic)
    except (ReportInputError, rv.ValidatorError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    base = Path(args.out_prefix)
    base.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    base.with_suffix(".html").write_text(render_html(report), encoding="utf-8")
    base.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")
    s = report["summary"]
    print(f"total={s['total_test_cases']} pass={s['passed']} fail={s['failed']} skipped={s['skipped']} inconclusive={s['inconclusive']} findings={s['total_findings']}")
    syn = report.get("synthetic_fault_injection")
    if syn:
        print(f"synthetic: {syn['summary']}")
    print(f"wrote {base.with_suffix('.json')}, {base.with_suffix('.html')}, {base.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
