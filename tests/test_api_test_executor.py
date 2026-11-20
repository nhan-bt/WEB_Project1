"""Kiểm thử API Test Executor (Phần 6).

* Integration: dùng FastAPI TestClient thật trên database SQLite kiểm thử (không mock backend).
* Unit: dùng ứng dụng FastAPI nhỏ / socket thật để tạo 5xx, timeout, lỗi kết nối.
Executor chỉ ghi nhận; các test ở đây không kiểm tra PASS/FAIL nghiệp vụ.
"""
from __future__ import annotations

import copy
import hashlib
import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_test_executor import (
    ApiTestExecutor,
    ExecutionContext,
    ExternalEnvironment,
    HttpxTransport,
    InProcessEnvironment,
    MappingMismatch,
    StateObserver,
    SuiteLoader,
    TestCaseSchemaError,
    TransitionApiMapper,
    TransportError,
    UnresolvedReference,
    UnsafeEnvironmentError,
    _assert_safe_database,
    load_suite,
    main,
    redact,
)

ROOT = Path(__file__).resolve().parent.parent
CASES_FILE = ROOT / "test_cases.json"
MODEL_FILE = ROOT / "efsm_model.json"
PROD_DB = ROOT / "online_shop.db"


# ---------------------------------------------------------------------------
# Fixtures dùng chung
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def env():
    environment = InProcessEnvironment(project_root=ROOT)
    yield environment
    environment.cleanup()
    environment.close()


@pytest.fixture(scope="module")
def suite():
    return load_suite(CASES_FILE)


@pytest.fixture(scope="module")
def mapper(env):
    return TransitionApiMapper.from_file(MODEL_FILE, env.routes())


@pytest.fixture()
def executor(env, mapper):
    return ApiTestExecutor(env.transport(), env, mapper)


@pytest.fixture(scope="module")
def full_run(env, mapper, suite):
    """Chạy toàn bộ test_cases.json một lần (integration thật)."""
    return ApiTestExecutor(env.transport(), env, mapper).run_suite(suite)


def case_by(suite, path_key, kind="positive"):
    for c in suite.cases:
        if c["path_key"] == path_key and c["type"] == kind:
            return c
    raise AssertionError(f"không có test case {path_key}/{kind}")


def result_of(run, test_case_id):
    return next(r for r in run["results"] if r["test_case_id"] == test_case_id)


def all_keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from all_keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from all_keys(v)


class NullEnvironment:
    """Môi trường không có database (dùng với ứng dụng mini)."""

    supports_fixture = False
    isolation = "NONE"

    def prepare(self): ...
    def cleanup(self): ...
    def close(self): ...
    def describe(self):
        return {"mode": "null", "isolation": self.isolation}


def mini_case(test_case_id="M1", steps=None, readiness="READY", kind="positive"):
    return {"test_case_id": test_case_id, "source_path_id": "X1", "path_key": "TX", "type": kind, "readiness": readiness,
            "initial_data": {}, "steps": steps or [{"step_no": 1, "role": "path", "kind": "transition", "transition_id": "TX",
                                                    "request": {"method": "GET", "path": "/ok", "path_params": {}, "body": {}}}]}


MINI_MODEL = {"transitions": [{"id": "TX", "api": {"method": "GET", "path": "/ok"}},
                              {"id": "TY", "api": {"method": "GET", "path": "/items/{item_id}"}}],
              "negative_transitions": [{"id": "NTY", "base_transition": "TY"}]}


# ---------------------------------------------------------------------------
# 1–2. Đọc test case, schema không hợp lệ
# ---------------------------------------------------------------------------
class TestLoader:
    def test_reads_real_part5_output(self, suite):
        data = json.loads(CASES_FILE.read_text(encoding="utf-8"))
        assert len(suite.cases) == len(data["test_cases"]) == data["summary"]["test_cases"]
        assert len(suite.not_converted) == data["summary"]["unconverted_paths"]
        assert suite.sha256 == hashlib.sha256(CASES_FILE.read_bytes()).hexdigest()
        assert suite.meta["generator"] == "PathAwareTestDataGenerator"

    def test_every_real_case_is_structurally_valid(self, suite):
        from api_test_executor import validate_case

        assert [validate_case(c) for c in suite.cases if validate_case(c)] == []

    @pytest.mark.parametrize("payload", ["not json", "[]", "{}", '{"test_cases": 5}', '{"schema_version": "1.0"}'])
    def test_invalid_file_raises(self, tmp_path, payload):
        p = tmp_path / "x.json"
        p.write_text(payload, encoding="utf-8")
        with pytest.raises(TestCaseSchemaError):
            load_suite(p)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(TestCaseSchemaError):
            load_suite(tmp_path / "nope.json")

    def test_unsupported_schema_version_raises(self):
        with pytest.raises(TestCaseSchemaError, match="schema_version"):
            SuiteLoader().from_data({"schema_version": "2.0", "test_cases": []})

    def test_invalid_case_is_skipped_not_fatal(self):
        bad = [{"test_case_id": "B1"}, mini_case("B2", steps=[{"step_no": 7, "kind": "transition"}]),
               mini_case("B3", steps=[{"step_no": 1, "kind": "WEIRD"}])]
        good = mini_case("G1")
        suite = SuiteLoader().from_data({"schema_version": "1.0", "test_cases": bad + [good]})
        run = ApiTestExecutor(_MiniTransport(), NullEnvironment(), TransitionApiMapper(MINI_MODEL), observe=False).run_suite(suite)
        statuses = {r["test_case_id"]: r["execution_status"] for r in run["results"]}
        assert statuses == {"B1": "SKIPPED", "B2": "SKIPPED", "B3": "SKIPPED", "G1": "COMPLETED"}
        assert {r["skip_reason"]["code"] for r in run["results"] if r["execution_status"] == "SKIPPED"} == {"INVALID_TEST_CASE"}


class _MiniTransport:
    """Transport giả chỉ dùng cho unit test thuần logic (không dùng cho integration)."""

    def __init__(self, behaviour=None):
        self.calls = []
        self.behaviour = behaviour or (lambda m, u: SimpleNamespace(status_code=200, body={"ok": True}, body_format="json", duration_ms=0.1))

    def request(self, method, url, *, json_body=None, params=None):
        self.calls.append((method, url, json_body))
        return self.behaviour(method, url)


# ---------------------------------------------------------------------------
# 3. Ánh xạ transition → API
# ---------------------------------------------------------------------------
class TestMapper:
    def test_all_real_steps_match_efsm_and_backend_routes(self, suite, mapper):
        checked = 0
        for case in suite.cases:
            for step in case["steps"]:
                if step["kind"] != "FIXTURE":
                    info = mapper.validate_step(step)
                    assert info["route_verified"] is True
                    checked += 1
        assert checked == 693

    def test_negative_transition_uses_base_transition_endpoint(self, suite, mapper):
        case = case_by(suite, "T01>T04>T05>T06>!NT08", "negative")
        step = case["steps"][-1]
        assert step["kind"] == "negative_transition"
        info = mapper.validate_step(step)
        assert info["efsm_id"] == "T08" and (info["method"], info["path"]) == ("POST", "/api/orders/{order_id}/cancel")

    def test_mismatching_request_is_rejected(self, suite, mapper):
        step = copy.deepcopy(case_by(suite, "T10")["steps"][0])
        step["request"]["method"] = "GET"
        with pytest.raises(MappingMismatch, match="không khớp EFSM"):
            mapper.validate_step(step)

    def test_unknown_route_in_backend_is_rejected(self, suite, env):
        routes = env.routes() - {("DELETE", "/api/cart")}
        with pytest.raises(MappingMismatch, match="backend không có route"):
            TransitionApiMapper.from_file(MODEL_FILE, routes).validate_step(case_by(suite, "T10")["steps"][0])

    def test_path_params_must_match_placeholders(self):
        mapper = TransitionApiMapper(MINI_MODEL)
        step = {"kind": "transition", "transition_id": "TY",
                "request": {"method": "GET", "path": "/items/{item_id}", "path_params": {}, "body": {}}}
        with pytest.raises(MappingMismatch, match="path params"):
            mapper.validate_step(step)

    def test_unknown_transition_or_negative(self):
        mapper = TransitionApiMapper(MINI_MODEL)
        with pytest.raises(MappingMismatch):
            mapper.validate_step({"kind": "transition", "transition_id": "T404", "request": {}})
        with pytest.raises(MappingMismatch):
            mapper.validate_step({"kind": "negative_transition", "negative_transition_id": "NT404", "request": {}})

    def test_mismatch_step_is_not_sent_and_is_an_execution_error(self):
        transport = _MiniTransport()
        case = mini_case(steps=[{"step_no": 1, "role": "path", "kind": "transition", "transition_id": "TX",
                                 "request": {"method": "POST", "path": "/ok", "path_params": {}, "body": {}}}])
        run = ApiTestExecutor(transport, NullEnvironment(), TransitionApiMapper(MINI_MODEL), observe=False).run_case(case)
        assert run["execution_status"] == "EXECUTION_ERROR" and transport.calls == []
        assert run["steps"][0]["error"]["kind"] == "MAPPING_MISMATCH"


# ---------------------------------------------------------------------------
# 5. Execution context: truyền dữ liệu giữa các bước
# ---------------------------------------------------------------------------
class TestExecutionContext:
    def make(self):
        ctx = ExecutionContext()
        ctx.record(2, 201, {"order": {"id": 41, "items": [{"product_id": 1}]}, "message": "ok"})
        return ctx

    def test_lookup_nested_and_list(self):
        ctx = self.make()
        assert ctx.lookup("$ref:steps.2.response.order.id") == 41
        assert ctx.lookup("$ref:steps.2.response.order.items.0.product_id") == 1

    def test_resolve_walks_structures(self):
        assert self.make().resolve({"a": ["$ref:steps.2.response.order.id", "x"], "b": 3}) == {"a": [41, "x"], "b": 3}

    @pytest.mark.parametrize("ref", ["$ref:steps.9.response.order.id", "$ref:steps.2.response.order.nope",
                                     "$ref:steps.2.response.order.items.5", "$ref:steps.two.response.x", "$ref:garbage"])
    def test_unresolved_references_raise(self, ref):
        with pytest.raises(UnresolvedReference):
            self.make().lookup(ref)

    def test_null_value_is_unresolved(self):
        ctx = ExecutionContext()
        ctx.record(1, 200, {"order": None})
        with pytest.raises(UnresolvedReference):
            ctx.lookup("$ref:steps.1.response.order")

    def test_capture_reports_missing(self):
        ctx = ExecutionContext()
        ctx.record(1, 400, {"detail": "x"})
        got, missing = ctx.capture({"order_id": "$ref:steps.1.response.order.id"})
        assert got == {} and "order_id" in missing and ctx.variables == {}


# ---------------------------------------------------------------------------
# 4. Thực thi chuỗi nhiều bước trên backend thật
# ---------------------------------------------------------------------------
class TestChainedExecution:
    def test_order_id_flows_from_create_to_pay(self, suite, executor):
        run = executor.run_case(case_by(suite, "T01>T04>T05>T06"))
        assert run["execution_status"] == "COMPLETED"
        steps = {s["transition_id"]: s for s in run["steps"]}
        order_id = steps["T04"]["response"]["body"]["order"]["id"]
        assert steps["T04"]["captured"] == {"order_id": order_id}
        assert steps["T05"]["request"]["url"] == f"/api/orders/{order_id}/confirm"
        assert steps["T06"]["request"]["url"] == f"/api/orders/{order_id}/pay"
        assert steps["T06"]["request"]["path_params"] == {"order_id": order_id}
        assert steps["T06"]["captured"]["payment_id"] == steps["T06"]["response"]["body"]["payment"]["id"]

    def test_step_ids_are_real_ids_after_prior_orders(self, env, mapper):
        """Chạy trực tiếp một test case 'mini' có sẵn đơn trước đó và không quan sát: ID thật khác 1 vẫn được dùng."""
        t = env.transport()
        env.prepare()
        for _ in range(2):
            t.request("POST", "/api/cart/items", json_body={"product_id": 3, "quantity": 1})
            t.request("POST", "/api/orders", json_body={"customer_name": "Seed User", "phone": "0900000000",
                                                       "address": "Seed Address", "payment_method": "MOCK"})
        t.request("POST", "/api/cart/items", json_body={"product_id": 2, "quantity": 1})

        class NoReset(NullEnvironment):
            supports_fixture = False

        steps = [
            {"step_no": 1, "role": "path", "kind": "transition", "transition_id": "T04", "captures": {"order_id": "$ref:steps.1.response.order.id"},
             "request": {"method": "POST", "path": "/api/orders", "path_params": {},
                         "body": {"customer_name": "Seed User", "phone": "0900000000", "address": "Seed Address", "payment_method": "MOCK"}}},
            {"step_no": 2, "role": "path", "kind": "transition", "transition_id": "T05",
             "request": {"method": "POST", "path": "/api/orders/{order_id}/confirm", "path_params": {"order_id": "$ref:steps.1.response.order.id"}, "body": {}}},
        ]
        run = ApiTestExecutor(t, NoReset(), mapper, observe=False).run_case(mini_case("R1", steps))
        order_id = run["steps"][0]["captured"]["order_id"]
        assert order_id == 3 and run["steps"][1]["request"]["url"] == "/api/orders/3/confirm"
        assert run["steps"][1]["response"]["status_code"] == 200
        env.prepare()

    def test_failed_payment_then_retry(self, suite, executor):
        run = executor.run_case(case_by(suite, "T01>T04>T05>T07>T07>T06"))
        pays = [s for s in run["steps"] if s.get("transition_id") in ("T06", "T07")]
        assert [s["request"]["body"]["simulate_result"] for s in pays] == ["FAILED", "FAILED", "SUCCESS"]
        assert len({s["captured"]["payment_id"] for s in pays}) == 3
        assert [p["result"] for p in run["final_observed_state"]["payments"]] == ["FAILED", "FAILED", "SUCCESS"]
        assert run["final_observed_state"]["orders"][0]["status"] == "PAID"

    def test_long_path_runs_every_step_in_order(self, suite, executor):
        case = case_by(suite, "T01>T01>T04>T05>T07>T07>T06")
        run = executor.run_case(case)
        assert run["execution_status"] == "COMPLETED"
        assert [s["step_no"] for s in run["steps"]] == [c["step_no"] for c in case["steps"]]
        assert all(s["execution_status"] == "SENT" for s in run["steps"])

    def test_cancel_pending_and_confirmed_orders(self, suite, executor):
        for key in ("T01>T04>T08", "T01>T04>T05>T09"):
            run = executor.run_case(case_by(suite, key))
            assert run["execution_status"] == "COMPLETED"
            assert run["final_observed_state"]["orders"][0]["status"] == "CANCELLED"
            assert run["final_observed_state"]["product_stock"]["1"] == 10

    def test_cart_update_and_remove(self, suite, executor):
        run = executor.run_case(case_by(suite, "T01>T02"))
        assert run["execution_status"] == "COMPLETED"
        qty = run["steps"][-1]["request"]["body"]["quantity"]
        assert run["final_observed_state"]["cart"]["items"][0]["quantity"] == qty
        run = executor.run_case(case_by(suite, "T01>T03"))
        assert run["final_observed_state"]["cart"]["items"] == []

    def test_unresolved_reference_is_not_sent_and_run_continues(self, env, mapper):
        """Bước tạo đơn bị từ chối (giỏ rỗng) → các bước sau không gửi được, nhưng không phải lỗi kỹ thuật."""
        t = env.transport()
        steps = [
            {"step_no": 1, "role": "path", "kind": "transition", "transition_id": "T04", "captures": {"order_id": "$ref:steps.1.response.order.id"},
             "request": {"method": "POST", "path": "/api/orders", "path_params": {},
                         "body": {"customer_name": "Seed User", "phone": "0900000000", "address": "Seed Address", "payment_method": "MOCK"}}},
            {"step_no": 2, "role": "path", "kind": "transition", "transition_id": "T05",
             "request": {"method": "POST", "path": "/api/orders/{order_id}/confirm", "path_params": {"order_id": "$ref:steps.1.response.order.id"}, "body": {}}},
            {"step_no": 3, "role": "path", "kind": "transition", "transition_id": "T10",
             "request": {"method": "DELETE", "path": "/api/cart", "path_params": {}, "body": {}}},
        ]
        env.prepare()
        run = ApiTestExecutor(t, NullEnvironment(), mapper, observe=False).run_case(mini_case("U1", steps))
        s1, s2, s3 = run["steps"]
        assert s1["response"]["status_code"] == 400 and s1["capture_missing"]
        assert s2["execution_status"] == "NOT_SENT" and s2["error"]["kind"] == "UNRESOLVED_REF"
        assert s3["execution_status"] == "SENT"  # chuỗi vẫn tiếp tục
        assert run["execution_status"] == "PARTIAL" and run["errors"] == []


# ---------------------------------------------------------------------------
# Negative flows: phản hồi từ chối không bị nhầm với lỗi kỹ thuật
# ---------------------------------------------------------------------------
class TestNegativeFlows:
    @pytest.mark.parametrize("key", ["!NT01", "!NT02", "!NT03", "T01>T04>!NT06", "T01>T04>T05>T06>!NT07",
                                     "T01>T04>T05>T06>!NT08", "T01>T04>T08>!NT09", "T01>T04>T08>!NT10", "T01>T04>T08>!NT11", "T01>T04>T05>!NT12"])
    def test_rejections_are_completed_not_errors(self, suite, executor, key):
        case = next((c for c in suite.cases if c["type"] == "negative" and c["path_key"] == key), None)
        if case is None:
            pytest.skip(f"không có path {key}")
        run = executor.run_case(case)
        last = run["steps"][-1]
        assert run["execution_status"] == "COMPLETED" and run["errors"] == []
        assert last["execution_status"] == "SENT" and last["response"]["status_class"] == "4xx"
        assert last["error"] is None

    def test_every_negative_case_is_recorded_with_its_http_response(self, full_run):
        negatives = [r for r in full_run["results"] if r["test_type"] == "NEGATIVE" and r["execution_status"] != "SKIPPED"]
        assert len(negatives) == 20
        for r in negatives:
            assert r["steps"][-1]["negative_transition_id"] == r["negative_transition_id"]
            assert r["steps"][-1]["execution_status"] == "SENT"

    def test_scenarios_required_by_part6(self, suite, executor):
        """Các kịch bản negative bắt buộc: vượt tồn kho, giỏ rỗng, pay trước confirm, pay lại, hủy đơn đã thanh toán/hủy, confirm/pay đơn đã hủy."""
        wanted = {"NT02": "vượt tồn kho", "NT03": "tạo đơn giỏ rỗng", "NT06": "thanh toán trước khi xác nhận",
                  "NT07": "thanh toán lại đơn đã thanh toán", "NT08": "hủy đơn đã thanh toán", "NT09": "hủy đơn đã hủy",
                  "NT10": "xác nhận đơn đã hủy", "NT11": "thanh toán đơn đã hủy"}
        found = {c["negative_transition_id"] for c in suite.cases if c["type"] == "negative"}
        assert set(wanted) <= found, sorted(set(wanted) - found)
        for nt in wanted:
            case = next(c for c in suite.cases if c.get("negative_transition_id") == nt)
            run = executor.run_case(case)
            assert run["execution_status"] == "COMPLETED", nt
            assert run["steps"][-1]["response"]["status_class"] == "4xx", nt


# ---------------------------------------------------------------------------
# 6–7. Response 2xx/4xx/5xx, exception, timeout
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def mini_app_transport():
    from fastapi import FastAPI, HTTPException

    from api_test_executor import TestClientTransport

    app = FastAPI()

    @app.get("/ok")
    def ok():
        return {"ok": True}

    @app.get("/items/{item_id}")
    def item(item_id: int):
        if item_id == 404:
            raise HTTPException(status_code=404, detail="no item")
        if item_id == 500:
            raise RuntimeError("boom")
        return {"id": item_id}

    @app.get("/text")
    def text():
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse("plain", status_code=418)

    return TestClientTransport(app)


class TestHttpClasses:
    def run(self, transport, item_id):
        step = {"step_no": 1, "role": "path", "kind": "transition", "transition_id": "TY",
                "request": {"method": "GET", "path": "/items/{item_id}", "path_params": {"item_id": item_id}, "body": {}}}
        return ApiTestExecutor(transport, NullEnvironment(), TransitionApiMapper(MINI_MODEL), observe=False).run_case(mini_case("H", [step]))

    @pytest.mark.parametrize("item_id,klass", [(7, "2xx"), (404, "4xx"), (500, "5xx")])
    def test_status_classes_are_recorded_not_judged(self, mini_app_transport, item_id, klass):
        run = self.run(mini_app_transport, item_id)
        assert run["execution_status"] == "COMPLETED" and run["errors"] == []
        assert run["steps"][0]["response"]["status_class"] == klass

    def test_server_exception_becomes_a_500_response(self, mini_app_transport):
        res = mini_app_transport.request("GET", "/items/500")
        assert res.status_code == 500

    def test_non_json_body_is_kept_as_text(self, mini_app_transport):
        res = mini_app_transport.request("GET", "/text")
        assert (res.status_code, res.body_format, res.body) == (418, "text", {"_raw_text": "plain"})

    def test_422_validation_body_is_recorded(self, mini_app_transport):
        run = self.run(mini_app_transport, "abc")
        assert run["steps"][0]["response"]["status_code"] == 422 and run["execution_status"] == "COMPLETED"


class TestTechnicalErrors:
    def test_timeout_and_connection_errors_are_execution_errors_and_suite_continues(self):
        def behaviour(method, url):
            if url == "/boom":
                raise TransportError("TIMEOUT", "ReadTimeout: simulated")
            return SimpleNamespace(status_code=200, body={}, body_format="json", duration_ms=0.1)

        model = {"transitions": [{"id": "TX", "api": {"method": "GET", "path": "/ok"}}, {"id": "TB", "api": {"method": "GET", "path": "/boom"}}]}
        bad = mini_case("E1", [{"step_no": 1, "role": "path", "kind": "transition", "transition_id": "TB",
                                "request": {"method": "GET", "path": "/boom", "path_params": {}, "body": {}}}])
        suite = SuiteLoader().from_data({"schema_version": "1.0", "test_cases": [bad, mini_case("E2")]})
        run = ApiTestExecutor(_MiniTransport(behaviour), NullEnvironment(), TransitionApiMapper(model), observe=False).run_suite(suite)
        by_id = {r["test_case_id"]: r for r in run["results"]}
        assert by_id["E1"]["execution_status"] == "EXECUTION_ERROR" and by_id["E1"]["steps"][0]["error"]["kind"] == "TIMEOUT"
        assert by_id["E2"]["execution_status"] == "COMPLETED"
        assert run["summary"]["execution_errors"] == 1 and run["summary"]["steps"]["error"] == 1

    def test_unexpected_exception_in_a_case_is_contained(self):
        class Exploding(NullEnvironment):
            def prepare(self):
                raise RuntimeError("cannot reset")

        run = ApiTestExecutor(_MiniTransport(), Exploding(), TransitionApiMapper(MINI_MODEL), observe=False).run_case(mini_case())
        assert run["execution_status"] == "EXECUTION_ERROR" and "cannot reset" in run["errors"][0]["message"]

    def test_real_connection_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with pytest.raises(TransportError) as exc:
            HttpxTransport(f"http://127.0.0.1:{port}", timeout=1).request("GET", "/ok")
        assert exc.value.kind == "CONNECTION_ERROR"

    def test_real_timeout(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            with pytest.raises(TransportError) as exc:
                HttpxTransport(f"http://127.0.0.1:{server.getsockname()[1]}", timeout=0.3).request("GET", "/ok")
            assert exc.value.kind == "TIMEOUT"
        finally:
            server.close()

    def test_observation_failure_is_recorded_but_not_fatal(self):
        def behaviour(method, url):
            return SimpleNamespace(status_code=500 if url.startswith("/api/") else 200, body={}, body_format="json", duration_ms=0.1)

        obs, errors = StateObserver(_MiniTransport(behaviour)).observe()
        assert obs == {} and len(errors) == 4


# ---------------------------------------------------------------------------
# 8. Bỏ qua test case không thực thi được
# ---------------------------------------------------------------------------
class TestSkipping:
    def test_blocked_and_unconverted_are_skipped_without_requests(self):
        transport = _MiniTransport()
        data = {"schema_version": "1.0",
                "test_cases": [mini_case("S1", readiness="BLOCKED"), mini_case("S2", readiness="UNSUPPORTED"), mini_case("S3")],
                "unconverted_paths": [{"source_path_id": "P9", "path_key": "T10>T04", "type": "positive", "readiness": "BLOCKED",
                                       "reason_code": "NO_FEASIBLE_DATA_FOUND", "reason": "giỏ trống"}]}
        run = ApiTestExecutor(transport, NullEnvironment(), TransitionApiMapper(MINI_MODEL), observe=False).run_suite(SuiteLoader().from_data(data))
        by = {(r["test_case_id"] or r["path_id"]): r for r in run["results"]}
        assert by["S1"]["skip_reason"]["code"] == "READINESS_BLOCKED" and by["S2"]["skip_reason"]["code"] == "READINESS_UNSUPPORTED"
        assert by["P9"]["skip_reason"]["code"] == "NO_FEASIBLE_DATA_FOUND" and by["P9"]["skip_reason"]["message"] == "giỏ trống"
        assert by["S3"]["execution_status"] == "COMPLETED"
        assert len(transport.calls) == 1  # chỉ S3 được gửi
        assert run["summary"]["total"] == 4 and run["summary"]["skipped"] == 3

    def test_real_unconverted_paths_are_reported_with_reasons(self, full_run, suite):
        skipped = [r for r in full_run["results"] if r["execution_status"] == "SKIPPED"]
        assert len(skipped) == len(suite.not_converted) == 49
        assert all(r["skip_reason"]["code"] == "NO_FEASIBLE_DATA_FOUND" and r["skip_reason"]["message"] for r in skipped)
        assert all(r["steps"] == [] for r in skipped)

    def test_partially_ready_is_executed(self, full_run):
        partial = [r for r in full_run["results"] if r["readiness"] == "PARTIALLY_READY"]
        assert len(partial) == 3 and all(r["execution_status"] == "COMPLETED" for r in partial)

    def test_fixture_case_is_skipped_when_environment_cannot_apply_it(self, suite):
        case = case_by(suite, "!NT04", "negative")
        run = ApiTestExecutor(_MiniTransport(), NullEnvironment(), TransitionApiMapper.from_file(MODEL_FILE), observe=False).run_case(case)
        assert run["execution_status"] == "SKIPPED" and run["skip_reason"]["code"] == "FIXTURE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# 9 & 12. Cô lập database, không đụng dữ liệu thật
# ---------------------------------------------------------------------------
class TestIsolation:
    def test_engine_never_points_to_production_database(self, env):
        assert env.db_name != PROD_DB.name
        assert Path(env.database.engine.url.database).resolve() != PROD_DB.resolve()

    def test_unsafe_database_is_refused(self):
        fake = SimpleNamespace(engine=SimpleNamespace(url=SimpleNamespace(drivername="sqlite", database=str(PROD_DB))),
                               DEFAULT_DB_PATH=PROD_DB)
        with pytest.raises(UnsafeEnvironmentError, match="database thật"):
            _assert_safe_database(fake)
        other = SimpleNamespace(engine=SimpleNamespace(url=SimpleNamespace(drivername="postgresql", database="x")), DEFAULT_DB_PATH=PROD_DB)
        with pytest.raises(UnsafeEnvironmentError):
            _assert_safe_database(other)

    def test_cannot_switch_database_after_app_import(self):
        with pytest.raises(UnsafeEnvironmentError):
            InProcessEnvironment(db_path="x.db", project_root=ROOT)

    def test_state_does_not_leak_between_cases(self, suite, executor):
        dirty = case_by(suite, "T01>T04>T05>T06")  # để lại đơn PAID, tồn kho giảm, giỏ rỗng
        clean = case_by(suite, "!NT01", "negative")  # cần trạng thái seed
        r1 = executor.run_case(dirty)
        assert r1["final_observed_state"]["product_stock"]["1"] < 10 and r1["final_observed_state"]["orders"]
        r2 = executor.run_case(clean)
        assert r2["isolation"]["initial_state_matches_test_case"] is True
        assert r2["initial_observed_state"]["product_stock"] == {"1": 10, "2": 5, "3": 3}
        assert r2["initial_observed_state"]["orders"] == [] and r2["initial_observed_state"]["payments"] == []

    def test_database_is_reset_after_run(self, suite, executor, env):
        executor.run_case(case_by(suite, "T01>T04>T08"))
        state, errors = StateObserver(env.transport()).observe()
        assert errors == [] and state["orders"] == [] and state["cart"]["items"] == [] and state["product_stock"]["1"] == 10

    def test_environment_mismatch_is_not_run(self, suite, executor):
        case = copy.deepcopy(case_by(suite, "T10"))
        case["initial_data"]["product_stock"] = {"1": 99, "2": 5, "3": 3}
        run = executor.run_case(case)
        assert run["execution_status"] == "EXECUTION_ERROR" and run["steps"] == []
        assert run["errors"][0]["kind"] == "ENVIRONMENT_MISMATCH" and run["isolation"]["initial_state_matches_test_case"] is False

    def test_production_database_file_is_never_modified(self, full_run):
        if not PROD_DB.exists():
            pytest.skip("không có online_shop.db")
        assert full_run["summary"]["executed"] > 0
        expected = json.loads((ROOT / "tests" / "baseline_hashes.json").read_text(encoding="utf-8"))
        assert "online_shop.db" not in expected  # DB không nằm trong danh sách hash; kiểm bằng thời gian sửa và kích thước
        assert PROD_DB.stat().st_mtime < time.time() - 1  # chưa bị ghi trong lần chạy này

    def test_external_mode_requires_explicit_consent(self):
        with pytest.raises(UnsafeEnvironmentError, match="allow_dirty_target"):
            ExternalEnvironment("http://127.0.0.1:9")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------
class TestFixtures:
    def test_stock_fixture_is_applied_through_test_database(self, suite, executor):
        case = case_by(suite, "!NT04", "negative")
        run = executor.run_case(case)
        fixture = next(s for s in run["steps"] if s["kind"] == "FIXTURE")
        assert fixture["execution_status"] == "FIXTURE_APPLIED" and fixture["fixture"]["result"] == {"rows_updated": 1}
        assert fixture["observed_state"]["product_stock"]["1"] == case["steps"][1]["value"]
        assert run["steps"][-1]["response"]["status_class"] == "4xx"

    def test_unsupported_fixture_is_an_error_step(self, suite, env, mapper):
        case = copy.deepcopy(case_by(suite, "!NT04", "negative"))
        case["steps"][1]["target"] = {"table": "users", "column": "role", "where": {"id": 1}}
        run = ApiTestExecutor(env.transport(), env, mapper).run_case(case)
        assert run["execution_status"] == "EXECUTION_ERROR"
        assert run["steps"][1]["error"]["kind"] == "FIXTURE_FAILED"
        assert run["steps"][1]["observed_state"]["product_stock"]["1"] == 10  # không có gì bị ghi


# ---------------------------------------------------------------------------
# 10. State observer
# ---------------------------------------------------------------------------
class TestObservedState:
    def test_observed_state_after_each_step(self, suite, executor):
        run = executor.run_case(case_by(suite, "T01>T04>T05>T06"))
        t01, t04, t05, t06 = run["steps"]
        assert t01["observed_state"]["cart"]["items"][0]["quantity"] == t01["request"]["body"]["quantity"]
        assert t04["observed_state"]["cart"]["items"] == []
        assert t04["observed_state"]["orders"][0]["status"] == "PENDING_CONFIRMATION"
        assert t04["observed_state"]["product_stock"]["1"] == 10 - t01["request"]["body"]["quantity"]
        assert t05["observed_state"]["orders"][0]["status"] == "CONFIRMED"
        assert t06["observed_state"]["orders"][0]["status"] == "PAID"
        assert t06["observed_state"]["payments"][0]["result"] == "SUCCESS"
        assert t06["observed_state"]["payments"][0]["amount"] == t06["observed_state"]["orders"][0]["total_amount"]

    def test_observed_state_keys_and_no_personal_data(self, suite, executor):
        run = executor.run_case(case_by(suite, "T01>T04>T08"))
        state = run["steps"][-1]["observed_state"]
        assert set(state) == {"cart", "product_stock", "orders", "payments"}
        assert not {"customer_name", "phone", "address"} & set(all_keys(state))

    def test_observation_can_be_disabled(self, suite, env, mapper):
        run = ApiTestExecutor(env.transport(), env, mapper, observe=False).run_case(case_by(suite, "T10"))
        assert run["steps"][0]["observed_state"] is None and run["initial_observed_state"] is None
        assert run["isolation"]["initial_state_matches_test_case"] is None

    def test_assertion_probes_are_collected_once_per_url(self, suite, executor):
        run = executor.run_case(case_by(suite, "T01>T04>T05>T06>!NT08", "negative"))
        urls = [p["request"].get("url") for p in run["assertion_probes"] if p["request"].get("url")]
        assert len(urls) == len(set(urls))
        merged = next(p for p in run["assertion_probes"] if p["request"].get("url") == "/api/cart")
        assert merged["assertion_ids"] == ["A1", "A5"] and merged["response"]["status_code"] == 200
        unbound = [p for p in run["assertion_probes"] if p["execution_status"] == "NOT_SENT"]
        assert unbound and all(p["error"]["kind"] == "UNBOUND_PATH_PARAM" for p in unbound)


# ---------------------------------------------------------------------------
# 11. Kết quả JSON
# ---------------------------------------------------------------------------
class TestResults:
    def test_summary_counts_are_consistent(self, full_run, suite):
        s = full_run["summary"]
        assert s["total"] == len(suite.cases) + len(suite.not_converted) == len(full_run["results"]) == 171
        assert s["executed"] + s["skipped"] + s["execution_errors"] == s["total"]
        assert s["executed"] == 122 and s["skipped"] == 49 and s["execution_errors"] == 0
        assert s["executed_by_type"] == {"POSITIVE": 102, "NEGATIVE": 20}
        assert sum(s["http_status_classes"].values()) == s["steps"]["sent"] == 693
        assert sum(s["http_status_codes"].values()) == 693
        assert s["steps"]["fixture_applied"] == 1 and s["steps"]["error"] == 0 and s["steps"]["not_sent"] == 0

    def test_step_counts_match_test_cases(self, full_run, suite):
        for case in suite.cases:
            rec = result_of(full_run, case["test_case_id"])
            assert [s["step_no"] for s in rec["steps"]] == [s["step_no"] for s in case["steps"]]
            assert rec["path_id"] == case["source_path_id"] and rec["test_type"] == case["type"].upper()

    def test_every_sent_step_is_traceable(self, full_run):
        for rec in full_run["results"]:
            for s in rec["steps"]:
                assert s["expected_ref"] == {"test_case_id": rec["test_case_id"], "step_no": s["step_no"]}
                if s["execution_status"] == "SENT":
                    assert s["mapped_from"]["route_verified"] is True and s["request"]["url"] and "status_code" in s["response"]
                    assert s["duration_ms"] >= 0 and s["observed_state"] is not None

    def test_dynamic_ids_come_from_responses(self, full_run, suite):
        for case in suite.cases:
            rec = result_of(full_run, case["test_case_id"])
            for step, src in zip(rec["steps"], case["steps"]):
                for param, ref in src.get("request", {}).get("path_params", {}).items():
                    if isinstance(ref, str) and ref.startswith("$ref:"):
                        n = int(ref.split(".")[1])
                        assert step["request"]["path_params"][param] == rec["steps"][n - 1]["response"]["body"]["order"]["id"]

    def test_no_verdict_is_recorded(self, full_run):
        forbidden = {"passed", "failed", "verdict", "pass", "fail", "vulnerability", "vulnerable", "security_finding"}
        assert not forbidden & {k.lower() for k in all_keys(full_run)}
        assert not any(r["execution_status"] in ("PASSED", "FAILED") for r in full_run["results"])

    def test_sensitive_values_are_not_logged(self, full_run):
        text = json.dumps(full_run, ensure_ascii=False)
        assert "NNNNN" not in text and "9999999" not in text and "AAAAAAA" not in text
        assert "<redacted len=10>" in text

    def test_result_is_valid_json_and_roundtrips(self, full_run):
        assert json.loads(json.dumps(full_run, ensure_ascii=False)) == full_run
        assert full_run["schema_version"] == "1.0" and full_run["executor"] == "BShop API Test Executor"
        assert full_run["environment"]["isolation"] == "RESET_BETWEEN_CASES"

    def test_input_test_cases_are_not_modified(self, env, mapper):
        before = CASES_FILE.read_bytes()
        suite = load_suite(CASES_FILE)
        snapshot = copy.deepcopy(suite.cases)
        ApiTestExecutor(env.transport(), env, mapper).run_suite(suite, limit=5)
        assert suite.cases == snapshot and CASES_FILE.read_bytes() == before

    def test_redact_helper(self):
        assert redact({"phone": "0123456789", "n": {"address": "abcde"}, "x": [{"customer_name": "ab"}], "k": 1}) == {
            "phone": "<redacted len=10>", "n": {"address": "<redacted len=5>"}, "x": [{"customer_name": "<redacted len=2>"}], "k": 1}

    def test_only_and_limit_options(self, env, mapper, suite):
        ex = ApiTestExecutor(env.transport(), env, mapper, observe=False)
        run = ex.run_suite(suite, only=["TC001", "TC002"])
        assert [r["test_case_id"] for r in run["results"]] == ["TC001", "TC002"]
        assert len(ex.run_suite(suite, limit=3)["results"]) == 3


# ---------------------------------------------------------------------------
# CLI và chế độ external
# ---------------------------------------------------------------------------
class TestCliAndExternal:
    def test_cli_writes_results(self, tmp_path):
        out = tmp_path / "res.json"
        assert main(["--test-cases", str(CASES_FILE), "--output", str(out), "--limit", "4"]) == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["summary"]["total"] == 4

    def test_cli_errors(self, tmp_path, capsys):
        assert main(["--test-cases", str(tmp_path / "missing.json"), "--output", str(tmp_path / "o.json")]) == 2
        assert main(["--test-cases", str(CASES_FILE), "--mode", "external", "--output", str(tmp_path / "o.json")]) == 2
        assert main(["--test-cases", str(CASES_FILE), "--mode", "external", "--base-url", "http://127.0.0.1:9",
                     "--output", str(tmp_path / "o.json")]) == 2
        assert "ERROR" in capsys.readouterr().err

    def test_external_http_against_a_live_server(self, env, suite, tmp_path):
        """Chế độ external qua HTTP thật tới uvicorn chạy trong luồng (cổng loopback ngẫu nhiên, DB kiểm thử)."""
        import uvicorn

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        server = uvicorn.Server(uvicorn.Config(env.app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        try:
            env.prepare()
            external = ExternalEnvironment(f"http://127.0.0.1:{port}", allow_dirty_target=True)
            mapper = TransitionApiMapper.from_file(MODEL_FILE, external.routes())
            assert mapper.routes and ("POST", "/api/orders") in mapper.routes
            case = case_by(suite, "T01>T04>T08")
            run = ApiTestExecutor(external.transport(), external, mapper).run_case(case)
            assert run["execution_status"] == "COMPLETED" and run["isolation"]["isolation"] == "NOT_GUARANTEED"
            assert run["final_observed_state"]["orders"][0]["status"] == "CANCELLED"
            # lần chạy thứ hai: server không được reset → executor phát hiện trạng thái ban đầu không còn khớp
            again = ApiTestExecutor(external.transport(), external, mapper).run_case(case)
            assert again["execution_status"] == "EXECUTION_ERROR" and again["errors"][0]["kind"] == "ENVIRONMENT_MISMATCH"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            env.prepare()

    def test_external_fixture_cases_are_skipped(self, suite, env):
        external = ExternalEnvironment("http://127.0.0.1:9", allow_dirty_target=True)
        run = ApiTestExecutor(external.transport(), external, TransitionApiMapper.from_file(MODEL_FILE)).run_case(case_by(suite, "!NT04", "negative"))
        assert run["skip_reason"]["code"] == "FIXTURE_UNAVAILABLE"
