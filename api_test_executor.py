"""BShop API Test Executor (Phần 6).

Đọc ``test_cases.json`` (do Test Data Generator sinh), thực thi từng test case trên backend BShop và ghi lại
request / response / trạng thái quan sát được vào ``test_execution_results.json``.

Phạm vi: CHỈ thực thi và ghi nhận. Module này không so sánh expected với actual, không gán PASS/FAIL và không kết luận
lỗ hổng bảo mật (việc của Result Validator, Phần 7). ``COMPLETED`` nghĩa là đã gửi hết các bước dự kiến, không phải "đạt".

Kiến trúc (xem docs/api_test_executor_design.md)::

    SuiteLoader ─► ApiTestExecutor ─► TransitionApiMapper (kiểm tra request khớp EFSM + route thật)
                        │                ExecutionContext   (response từng bước, giải $ref)
                        │                Transport          (TestClient trong tiến trình | httpx tới server ngoài)
                        │                EnvironmentManager (DB kiểm thử riêng, reset, fixture)
                        └──────────────► StateObserver      (GET /api/cart, /api/products, /api/orders, /api/payments)

Chạy::

    python api_test_executor.py --test-cases test_cases.json --output test_execution_results.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import quote

EXECUTOR_NAME = "BShop API Test Executor"
RESULT_SCHEMA_VERSION = "1.0"
SUPPORTED_TEST_CASE_SCHEMA_MAJOR = "1"
EXECUTABLE_READINESS = ("READY", "PARTIALLY_READY")
DEFAULT_REDACT_KEYS = ("customer_name", "phone", "address")
OBSERVE_PROBES = (("cart", "/api/cart"), ("products", "/api/products"), ("orders", "/api/orders"),
                  ("payments", "/api/payments"))

__all__ = [
    "ApiTestExecutor", "SuiteLoader", "LoadedSuite", "TransitionApiMapper", "ExecutionContext", "StateObserver",
    "InProcessEnvironment", "ExternalEnvironment", "TestClientTransport", "HttpxTransport", "HttpResult",
    "ExecutorError", "TestCaseSchemaError", "UnsafeEnvironmentError", "UnresolvedReference", "MappingMismatch",
    "TransportError", "redact", "load_suite", "main",
]


# ---------------------------------------------------------------------------
# Lỗi
# ---------------------------------------------------------------------------
class ExecutorError(Exception):
    """Lớp cơ sở của lỗi kỹ thuật trong executor."""


class TestCaseSchemaError(ExecutorError):
    """File test case không đúng cấu trúc (không thể đọc cả file)."""

    __test__ = False


class UnsafeEnvironmentError(ExecutorError):
    """Môi trường có thể chạm vào database thật hoặc không đảm bảo cô lập."""


class UnresolvedReference(ExecutorError):
    """Tham chiếu ``$ref:steps.N...`` chưa có giá trị (bước trước không tạo ra nó)."""


class MappingMismatch(ExecutorError):
    """Request của test case không khớp API của transition trong EFSM / route thật của backend."""


class TransportError(ExecutorError):
    """Lỗi kỹ thuật khi gửi request: ``TIMEOUT``, ``CONNECTION_ERROR`` hoặc ``EXCEPTION``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# Tiện ích
# ---------------------------------------------------------------------------
def redact(value: Any, keys: Sequence[str] = DEFAULT_REDACT_KEYS) -> Any:
    """Che giá trị của các khóa nhạy cảm (giữ lại độ dài để Phần 7 vẫn phân tích được)."""
    if isinstance(value, dict):
        return {k: (f"<redacted len={len(v)}>" if k in keys and isinstance(v, str) else redact(v, keys))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, keys) for v in value]
    return value


def _status_class(code: int | None) -> str | None:
    if code is None:
        return None
    return f"{code // 100}xx" if 100 <= code < 600 else "other"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Test Case Loader
# ---------------------------------------------------------------------------
@dataclass
class LoadedSuite:
    """Kết quả đọc ``test_cases.json``."""

    source: str
    sha256: str | None
    meta: dict[str, Any]
    cases: list[dict[str, Any]]
    not_converted: list[dict[str, Any]]


def validate_case(case: Any) -> list[str]:
    """Trả về danh sách lỗi cấu trúc của một test case (rỗng nếu hợp lệ)."""
    if not isinstance(case, dict):
        return ["test case không phải đối tượng JSON"]
    problems = [f"thiếu trường '{k}'" for k in ("test_case_id", "source_path_id", "type", "readiness", "steps", "initial_data")
                if k not in case]
    steps = case.get("steps")
    if not isinstance(steps, list) or not steps:
        return problems + ["'steps' phải là danh sách không rỗng"]
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            problems.append(f"bước {index}: không phải đối tượng")
            continue
        if step.get("step_no") != index:
            problems.append(f"bước {index}: step_no={step.get('step_no')!r}, cần {index}")
        kind = step.get("kind")
        if kind == "FIXTURE":
            for k in ("action", "target", "value"):
                if k not in step:
                    problems.append(f"bước {index}: fixture thiếu '{k}'")
        elif kind in ("transition", "negative_transition"):
            idkey = "transition_id" if kind == "transition" else "negative_transition_id"
            if not step.get(idkey):
                problems.append(f"bước {index}: thiếu '{idkey}'")
            req = step.get("request")
            if not isinstance(req, dict) or not all(k in req for k in ("method", "path", "path_params", "body")):
                problems.append(f"bước {index}: 'request' phải có method, path, path_params, body")
        else:
            problems.append(f"bước {index}: kind không hỗ trợ: {kind!r}")
    return problems


def load_suite(path: str | Path) -> LoadedSuite:
    """Đọc và kiểm tra cấu trúc mức file; test case lỗi riêng lẻ được giữ lại để báo ``SKIPPED``."""
    return SuiteLoader().load(path)


class SuiteLoader:
    """Đọc ``test_cases.json`` và kiểm tra các trường bắt buộc (không thay đổi định dạng gốc)."""

    def load(self, path: str | Path) -> LoadedSuite:
        p = Path(path)
        try:
            raw = p.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TestCaseSchemaError(f"không đọc được {p}: {exc}") from exc
        return self.from_data(data, str(p), hashlib.sha256(raw).hexdigest())

    def from_data(self, data: Any, source: str = "<memory>", sha256: str | None = None) -> LoadedSuite:
        if not isinstance(data, dict) or not isinstance(data.get("test_cases"), list):
            raise TestCaseSchemaError("file test case phải là đối tượng có danh sách 'test_cases'")
        version = str(data.get("schema_version", ""))
        if version.split(".")[0] != SUPPORTED_TEST_CASE_SCHEMA_MAJOR:
            raise TestCaseSchemaError(f"schema_version '{version}' chưa được hỗ trợ (cần {SUPPORTED_TEST_CASE_SCHEMA_MAJOR}.x)")
        not_converted = data.get("unconverted_paths", [])
        if not isinstance(not_converted, list):
            raise TestCaseSchemaError("'unconverted_paths' phải là danh sách")
        meta = {k: v for k, v in data.items() if k not in ("test_cases", "unconverted_paths")}
        return LoadedSuite(source, sha256, meta, list(data["test_cases"]), list(not_converted))


# ---------------------------------------------------------------------------
# Transition-to-API Mapper
# ---------------------------------------------------------------------------
class TransitionApiMapper:
    """Đối chiếu request trong test case với API của transition ở EFSM và với route thật của backend.

    Mapper KHÔNG tự tạo endpoint: method/path luôn lấy từ ``efsm_model.json`` (``transitions[].api``). Negative transition
    dùng chung endpoint của ``base_transition`` (không phải API độc lập).
    """

    def __init__(self, model: Mapping[str, Any], routes: set[tuple[str, str]] | None = None) -> None:
        self.transitions = {t["id"]: t for t in model["transitions"]}
        self.negatives = {n["id"]: n for n in model.get("negative_transitions", [])}
        self.routes = routes

    @classmethod
    def from_file(cls, model_path: str | Path, routes: set[tuple[str, str]] | None = None) -> "TransitionApiMapper":
        try:
            return cls(json.loads(Path(model_path).read_text(encoding="utf-8")), routes)
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise ExecutorError(f"không đọc được EFSM {model_path}: {exc}") from exc

    def api_for(self, step: Mapping[str, Any]) -> dict[str, str]:
        if step["kind"] == "transition":
            tid = step["transition_id"]
        else:
            neg = self.negatives.get(step["negative_transition_id"])
            if neg is None:
                raise MappingMismatch(f"negative transition '{step['negative_transition_id']}' không có trong EFSM")
            tid = neg["base_transition"]
        tr = self.transitions.get(tid)
        if tr is None or not tr.get("api"):
            raise MappingMismatch(f"transition '{tid}' không có API trong EFSM")
        return {"efsm_id": tid, "method": tr["api"]["method"], "path": tr["api"]["path"]}

    def validate_step(self, step: Mapping[str, Any]) -> dict[str, Any]:
        api = self.api_for(step)
        req = step["request"]
        if (req["method"], req["path"]) != (api["method"], api["path"]):
            raise MappingMismatch(f"request {req['method']} {req['path']} không khớp EFSM {api['efsm_id']}: {api['method']} {api['path']}")
        placeholders = set(re.findall(r"\{(\w+)\}", req["path"]))
        if placeholders != set(req["path_params"]):
            raise MappingMismatch(f"path params {sorted(req['path_params'])} không khớp {sorted(placeholders)} của {req['path']}")
        verified = None
        if self.routes is not None:
            verified = (api["method"], api["path"]) in self.routes
            if not verified:
                raise MappingMismatch(f"backend không có route {api['method']} {api['path']}")
        return {**api, "route_verified": verified}


def routes_from_openapi(doc: Mapping[str, Any]) -> set[tuple[str, str]]:
    """Tập (METHOD, path) từ tài liệu OpenAPI của backend."""
    return {(m.upper(), p) for p, item in doc.get("paths", {}).items() for m in item
            if m.lower() in ("get", "post", "put", "patch", "delete")}


# ---------------------------------------------------------------------------
# Execution Context
# ---------------------------------------------------------------------------
_REF = re.compile(r"^\$ref:steps\.(\d+)\.response((?:\.[A-Za-z0-9_\-]+)*)$")


class ExecutionContext:
    """Lưu response từng bước và giải tham chiếu ``$ref:steps.N.response.<đường dẫn>``."""

    def __init__(self) -> None:
        self.responses: dict[int, dict[str, Any]] = {}
        self.variables: dict[str, Any] = {}

    def record(self, step_no: int, status_code: int, body: Any) -> None:
        self.responses[step_no] = {"status_code": status_code, "body": body}

    def lookup(self, ref: str) -> Any:
        m = _REF.match(ref)
        if not m:
            raise UnresolvedReference(f"tham chiếu sai cú pháp: {ref}")
        step_no, path = int(m.group(1)), m.group(2)
        resp = self.responses.get(step_no)
        if resp is None:
            raise UnresolvedReference(f"{ref}: bước {step_no} chưa có response")
        node: Any = resp["body"]
        for part in [p for p in path.split(".") if p]:
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                raise UnresolvedReference(f"{ref}: response bước {step_no} (HTTP {resp['status_code']}) không có '{part}'")
        if node is None:
            raise UnresolvedReference(f"{ref}: giá trị là null")
        return node

    def resolve(self, value: Any) -> Any:
        if isinstance(value, str) and value.startswith("$ref:"):
            return self.lookup(value)
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        return value

    def capture(self, captures: Mapping[str, str] | None) -> tuple[dict[str, Any], dict[str, str]]:
        """Lưu các giá trị ``captures`` của bước vừa chạy; trả về (đã bắt được, không bắt được + lý do)."""
        got: dict[str, Any] = {}
        missing: dict[str, str] = {}
        for name, ref in (captures or {}).items():
            try:
                got[name] = self.lookup(ref)
                self.variables[name] = got[name]
            except UnresolvedReference as exc:
                missing[name] = str(exc)
        return got, missing


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
@dataclass
class HttpResult:
    status_code: int
    body: Any
    body_format: str
    duration_ms: float


class Transport(Protocol):
    def request(self, method: str, url: str, *, json_body: Any = None, params: Mapping[str, Any] | None = None) -> HttpResult: ...


def _parse_body(response: Any) -> tuple[Any, str]:
    try:
        return response.json(), "json"
    except ValueError:
        return {"_raw_text": response.text[:2000]}, "text"


class _HttpxBase:
    """Gửi request bằng một ``httpx.Client`` (TestClient cũng là ``httpx.Client``)."""

    client: Any

    def request(self, method: str, url: str, *, json_body: Any = None, params: Mapping[str, Any] | None = None) -> HttpResult:
        import httpx

        started = time.perf_counter()
        try:
            kwargs: dict[str, Any] = {"params": dict(params)} if params else {}
            if method.upper() in ("POST", "PUT", "PATCH") or json_body:
                kwargs["json"] = json_body if json_body is not None else {}
            resp = self.client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise TransportError("TIMEOUT", f"{type(exc).__name__}: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransportError("CONNECTION_ERROR", f"{type(exc).__name__}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - mọi lỗi kỹ thuật khác đều phải được ghi nhận, không làm dừng suite
            raise TransportError("EXCEPTION", f"{type(exc).__name__}: {exc}") from exc
        body, fmt = _parse_body(resp)
        return HttpResult(resp.status_code, body, fmt, (time.perf_counter() - started) * 1000)


class TestClientTransport(_HttpxBase):
    """Gọi ứng dụng FastAPI trong cùng tiến trình (không mở cổng mạng). 5xx được trả về như response, không ném lỗi."""

    __test__ = False

    def __init__(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        self.client = TestClient(app, raise_server_exceptions=False)


class HttpxTransport(_HttpxBase):
    """Gọi một server đang chạy (chế độ external)."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        import httpx

        self.client = httpx.Client(base_url=base_url, timeout=timeout)


# ---------------------------------------------------------------------------
# Environment Manager
# ---------------------------------------------------------------------------
def _assert_safe_database(database: Any) -> str:
    """Từ chối chạy nếu engine đang trỏ tới ``online_shop.db`` (database thật) hoặc không phải SQLite."""
    url = database.engine.url
    if not url.drivername.startswith("sqlite"):
        raise UnsafeEnvironmentError(f"chỉ hỗ trợ SQLite kiểm thử, đang dùng '{url.drivername}'")
    if url.database in (None, "", ":memory:"):
        return ":memory:"
    target = Path(url.database).resolve()
    if target == Path(database.DEFAULT_DB_PATH).resolve():
        raise UnsafeEnvironmentError(
            f"engine đang trỏ tới database thật ({target.name}). Đặt ONLINE_SHOP_DATABASE_URL tới file kiểm thử "
            "trước khi import app, hoặc để executor tự tạo database tạm.")
    return target.name


class InProcessEnvironment:
    """Chạy backend trong tiến trình, trên database SQLite kiểm thử riêng; reset giữa các test case."""

    supports_fixture = True
    isolation = "RESET_BETWEEN_CASES"

    def __init__(self, db_path: str | Path | None = None, project_root: str | Path | None = None) -> None:
        root = Path(project_root) if project_root else Path(__file__).resolve().parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        self._tmpdir: str | None = None
        if "app.database" not in sys.modules:
            if db_path is not None:
                os.environ["ONLINE_SHOP_DATABASE_URL"] = f"sqlite:///{Path(db_path).resolve().as_posix()}"
            elif "ONLINE_SHOP_DATABASE_URL" not in os.environ:
                self._tmpdir = tempfile.mkdtemp(prefix="bshop_executor_")
                os.environ["ONLINE_SHOP_DATABASE_URL"] = f"sqlite:///{(Path(self._tmpdir) / 'executor_test.db').as_posix()}"
        elif db_path is not None:
            raise UnsafeEnvironmentError("app.database đã được import; không thể đổi database sau đó")
        import app.database as database  # noqa: PLC0415 - phải import SAU khi đã đặt biến môi trường

        self.database = database
        self.db_name = _assert_safe_database(database)
        from app.main import app  # noqa: PLC0415

        self.app = app

    def transport(self) -> TestClientTransport:
        return TestClientTransport(self.app)

    def routes(self) -> set[tuple[str, str]]:
        return routes_from_openapi(self.app.openapi())

    def prepare(self) -> None:
        self.database.reset_database()

    def cleanup(self) -> None:
        self.database.reset_database()

    def close(self) -> None:
        if self._tmpdir:
            self.database.engine.dispose()
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    _FIXTURE_COLUMNS = {("products", "stock"): "stock"}

    def apply_fixture(self, step: Mapping[str, Any]) -> dict[str, Any]:
        """Thực hiện bước FIXTURE ``SET_DB_VALUE`` trên database kiểm thử (không qua API)."""
        from sqlalchemy import update

        from app.models import Product

        target, value = step["target"], step["value"]
        column = self._FIXTURE_COLUMNS.get((target.get("table"), target.get("column")))
        where = target.get("where", {})
        if step["action"] != "SET_DB_VALUE" or column is None or set(where) != {"id"} or not isinstance(where["id"], int) \
                or isinstance(value, bool) or not isinstance(value, int):
            raise ExecutorError(f"fixture chưa được hỗ trợ: {step['action']} {target} = {value!r}")
        with self.database.SessionLocal() as db:
            result = db.execute(update(Product).where(Product.id == where["id"]).values(**{column: value}))
            db.commit()
        return {"rows_updated": result.rowcount}

    def describe(self) -> dict[str, Any]:
        return {"mode": "in_process", "database": self.db_name, "isolation": self.isolation,
                "reset": "app.database.reset_database() trước và sau mỗi test case"}


class ExternalEnvironment:
    """Server đang chạy bên ngoài: KHÔNG có reset/fixture nên không bảo đảm cô lập; phải cho phép tường minh."""

    supports_fixture = False
    isolation = "NOT_GUARANTEED"

    def __init__(self, base_url: str, *, allow_dirty_target: bool = False, timeout: float = 10.0) -> None:
        if not allow_dirty_target:
            raise UnsafeEnvironmentError(
                "chế độ external không reset được database của server; cần allow_dirty_target=True "
                "(--allow-dirty-target) và chỉ dùng với server kiểm thử.")
        self.base_url, self.timeout = base_url.rstrip("/"), timeout
        self._transport = HttpxTransport(self.base_url, timeout)

    def transport(self) -> HttpxTransport:
        return self._transport

    def routes(self) -> set[tuple[str, str]] | None:
        try:
            return routes_from_openapi(self._transport.request("GET", "/openapi.json").body)
        except (TransportError, AttributeError):
            return None

    def prepare(self) -> None:
        return None

    def cleanup(self) -> None:
        return None

    def close(self) -> None:
        return None

    def apply_fixture(self, step: Mapping[str, Any]) -> dict[str, Any]:  # pragma: no cover - không được gọi
        raise ExecutorError("chế độ external không áp dụng được fixture database")

    def describe(self) -> dict[str, Any]:
        return {"mode": "external_http", "base_url": self.base_url, "isolation": self.isolation,
                "reset": "không có; dữ liệu của test case trước có thể còn lại trên server"}


# ---------------------------------------------------------------------------
# State Observer
# ---------------------------------------------------------------------------
class StateObserver:
    """Thu thập trạng thái quan sát được qua các API GET đã có (không đọc database trực tiếp)."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def observe(self) -> tuple[dict[str, Any], list[str]]:
        state: dict[str, Any] = {}
        errors: list[str] = []
        for name, path in OBSERVE_PROBES:
            try:
                res = self.transport.request("GET", path)
            except TransportError as exc:
                errors.append(f"{path}: {exc.kind}: {exc}")
                continue
            if res.status_code != 200:
                errors.append(f"{path}: HTTP {res.status_code}")
                continue
            state[name] = res.body
        return self._normalize(state), errors

    @staticmethod
    def _normalize(raw: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            if "cart" in raw:
                cart = raw["cart"]
                out["cart"] = {"items": [{"product_id": i["product_id"], "quantity": i["quantity"]} for i in cart["items"]],
                               "total_quantity": cart["total_quantity"], "total_amount": cart["total_amount"]}
            if "products" in raw:
                out["product_stock"] = {str(p["id"]): p["stock"] for p in raw["products"]}
            if "orders" in raw:
                out["orders"] = [{"id": o["id"], "status": o["status"], "total_amount": o["total_amount"],
                                  "payment_method": o.get("payment_method"),
                                  "items": [{k: i[k] for k in ("product_id", "unit_price", "quantity", "subtotal")} for i in o["items"]]}
                                 for o in raw["orders"]]
            if "payments" in raw:
                out["payments"] = [{k: p[k] for k in ("id", "order_id", "amount", "result")} for p in raw["payments"]]
        except (KeyError, TypeError) as exc:
            out["_normalization_error"] = f"{type(exc).__name__}: {exc}"
        return out


def _initial_mismatches(observed: Mapping[str, Any], initial: Mapping[str, Any]) -> list[str]:
    """So trạng thái ban đầu thực tế với ``initial_data`` của test case (điều kiện môi trường, không phải kết luận nghiệp vụ)."""
    problems = []
    cart = [{"product_id": i["product_id"], "quantity": i["quantity"]} for i in observed.get("cart", {}).get("items", [])]
    if cart != initial.get("cart_items"):
        problems.append(f"giỏ hàng ban đầu {cart} khác {initial.get('cart_items')}")
    if observed.get("product_stock") != initial.get("product_stock"):
        problems.append(f"tồn kho ban đầu {observed.get('product_stock')} khác {initial.get('product_stock')}")
    if len(observed.get("orders", [])) != len(initial.get("orders", [])):
        problems.append("số đơn hàng ban đầu khác test case")
    if len(observed.get("payments", [])) != len(initial.get("payments", [])):
        problems.append("số thanh toán ban đầu khác test case")
    for key in ("cart", "product_stock", "orders", "payments"):
        if key not in observed:
            problems.append(f"không quan sát được '{key}'")
    return problems


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------
class ApiTestExecutor:
    """Thực thi test case theo thứ tự các bước và ghi nhận kết quả (không đánh giá đúng/sai)."""

    def __init__(self, transport: Transport, environment: Any, mapper: TransitionApiMapper, *,
                 observe: bool = True, redact_keys: Sequence[str] = DEFAULT_REDACT_KEYS) -> None:
        self.transport, self.env, self.mapper = transport, environment, mapper
        self.observer = StateObserver(transport) if observe else None
        self.redact_keys = tuple(redact_keys)

    # --- suite -----------------------------------------------------------
    def run_suite(self, suite: LoadedSuite, *, only: Sequence[str] | None = None, limit: int | None = None,
                  extra_meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        selected = [c for c in suite.cases if only is None or (isinstance(c, dict) and c.get("test_case_id") in only)]
        if limit is not None:
            selected = selected[:limit]
        for case in selected:
            results.append(self.run_case(case))
        if only is None and limit is None:
            for item in suite.not_converted:
                results.append(self._skipped_unconverted(item))
        return {
            "schema_version": RESULT_SCHEMA_VERSION, "executor": EXECUTOR_NAME,
            "scope": "chỉ thực thi và ghi nhận; không so sánh expected/actual, không kết luận lỗ hổng (Phần 7–8)",
            "source": {"test_cases": suite.source, "sha256": suite.sha256, "generator": suite.meta.get("generator"),
                       "schema_version": suite.meta.get("schema_version")},
            "environment": {**self.env.describe(), "redacted_keys": list(self.redact_keys), "observation": self.observer is not None},
            "summary": self._summary(results), "results": results,
            **({"run": dict(extra_meta)} if extra_meta else {}),
        }

    @staticmethod
    def _skipped_unconverted(item: Mapping[str, Any]) -> dict[str, Any]:
        return {"test_case_id": None, "path_id": item.get("source_path_id"), "path_key": item.get("path_key"),
                "test_type": str(item.get("type", "")).upper() or None, "readiness": item.get("readiness"),
                "execution_status": "SKIPPED",
                "skip_reason": {"code": item.get("reason_code") or "NOT_CONVERTED",
                                "message": item.get("reason") or "path không được chuyển thành test case ở Phần 5",
                                "source": "test_cases.json/unconverted_paths",
                                # PROVEN_INFEASIBLE / SEARCH_EXHAUSTED / GENERATOR_ERROR / MODEL_ERROR (None với file cũ)
                                "feasibility_verdict": item.get("feasibility_verdict")},
                "steps": [], "errors": []}

    @staticmethod
    def _summary(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        reasons: dict[str, int] = {}
        classes: dict[str, int] = {}
        codes: dict[str, int] = {}
        steps = {"sent": 0, "not_sent": 0, "error": 0, "skipped": 0, "fixture_applied": 0}
        for r in results:
            by_status[r["execution_status"]] = by_status.get(r["execution_status"], 0) + 1
            if r["execution_status"] == "SKIPPED":
                code = r["skip_reason"]["code"]
                reasons[code] = reasons.get(code, 0) + 1
            for s in r.get("steps", []):
                key = {"SENT": "sent", "NOT_SENT": "not_sent", "ERROR": "error", "SKIPPED": "skipped",
                       "FIXTURE_APPLIED": "fixture_applied"}[s["execution_status"]]
                steps[key] += 1
                if s["execution_status"] == "SENT":
                    classes[s["response"]["status_class"]] = classes.get(s["response"]["status_class"], 0) + 1
                    code = str(s["response"]["status_code"])
                    codes[code] = codes.get(code, 0) + 1
        ran = [r for r in results if r["execution_status"] in ("COMPLETED", "PARTIAL")]
        return {
            "total": len(results), "executed": len(ran), "skipped": by_status.get("SKIPPED", 0),
            "execution_errors": by_status.get("EXECUTION_ERROR", 0),
            "by_execution_status": dict(sorted(by_status.items())),
            "executed_by_type": {t: sum(1 for r in ran if r["test_type"] == t) for t in ("POSITIVE", "NEGATIVE")},
            "skipped_by_reason": dict(sorted(reasons.items())),
            "steps": steps, "http_status_classes": dict(sorted(classes.items())), "http_status_codes": dict(sorted(codes.items())),
            "note": "COMPLETED = đã gửi hết các bước; không có nghĩa là đạt. 4xx là phản hồi nghiệp vụ, không phải lỗi kỹ thuật.",
        }

    # --- test case -------------------------------------------------------
    def run_case(self, case: Any) -> dict[str, Any]:
        problems = validate_case(case)
        base = self._case_header(case)
        if problems:
            return {**base, "execution_status": "SKIPPED", "steps": [], "errors": [],
                    "skip_reason": {"code": "INVALID_TEST_CASE", "message": "; ".join(problems)}}
        if case["readiness"] not in EXECUTABLE_READINESS:
            return {**base, "execution_status": "SKIPPED", "steps": [], "errors": [],
                    "skip_reason": {"code": f"READINESS_{case['readiness']}",
                                    "message": f"readiness={case['readiness']} không được thực thi"}}
        needs_fixture = any(s["kind"] == "FIXTURE" for s in case["steps"])
        if needs_fixture and not getattr(self.env, "supports_fixture", False):
            return {**base, "execution_status": "SKIPPED", "steps": [], "errors": [],
                    "skip_reason": {"code": "FIXTURE_UNAVAILABLE",
                                    "message": "test case cần fixture database mà môi trường hiện tại không cung cấp"}}
        started = time.perf_counter()
        record: dict[str, Any] = {**base, "skip_reason": None, "steps": [], "errors": []}
        try:
            self._execute(case, record)
        except Exception as exc:  # noqa: BLE001 - một test case lỗi không được làm dừng cả suite
            record["errors"].append({"step_no": None, "kind": "EXCEPTION", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            try:
                self.env.cleanup()
            except Exception as exc:  # noqa: BLE001
                record["errors"].append({"step_no": None, "kind": "CLEANUP_FAILED", "message": f"{type(exc).__name__}: {exc}"})
        record["execution_status"] = self._final_status(record)
        record["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return record

    @staticmethod
    def _case_header(case: Any) -> dict[str, Any]:
        c = case if isinstance(case, dict) else {}
        return {"test_case_id": c.get("test_case_id"), "path_id": c.get("source_path_id"), "path_key": c.get("path_key"),
                "test_type": str(c.get("type", "")).upper() or None, "readiness": c.get("readiness"),
                "negative_transition_id": c.get("negative_transition_id")}

    @staticmethod
    def _final_status(record: Mapping[str, Any]) -> str:
        if record["errors"] or any(s["execution_status"] == "ERROR" for s in record["steps"]):
            return "EXECUTION_ERROR"
        if not record["steps"] or any(s["execution_status"] in ("NOT_SENT", "SKIPPED") for s in record["steps"]):
            return "PARTIAL"
        return "COMPLETED"

    def _observe(self) -> tuple[dict[str, Any] | None, list[str]]:
        if self.observer is None:
            return None, []
        return self.observer.observe()

    def _execute(self, case: Mapping[str, Any], record: dict[str, Any]) -> None:
        self.env.prepare()
        initial, obs_errors = self._observe()
        mismatches = _initial_mismatches(initial, case["initial_data"]) if initial is not None else []
        record["isolation"] = {**{k: v for k, v in self.env.describe().items() if k in ("mode", "isolation")},
                               "initial_state_matches_test_case": (not mismatches) if initial is not None else None,
                               "initial_state_mismatches": mismatches}
        record["initial_observed_state"] = initial
        if obs_errors:
            record["errors"].append({"step_no": None, "kind": "OBSERVATION_FAILED", "message": "; ".join(obs_errors)})
        if mismatches:
            record["errors"].append({"step_no": None, "kind": "ENVIRONMENT_MISMATCH",
                                     "message": "trạng thái ban đầu không khớp test case: " + "; ".join(mismatches)})
            return
        ctx = ExecutionContext()
        for step in case["steps"]:
            if step["kind"] == "FIXTURE":
                rec = self._run_fixture(case, step)
            else:
                rec = self._run_request_step(case, step, ctx)
            if rec["execution_status"] in ("SENT", "FIXTURE_APPLIED", "ERROR"):
                rec["observed_state"], rec["observation_errors"] = self._observe()
            else:
                rec["observed_state"], rec["observation_errors"] = None, []
            if rec["execution_status"] == "ERROR":
                record["errors"].append({"step_no": step["step_no"], **rec["error"]})
            record["steps"].append(rec)
        record["assertion_probes"] = self._run_probes(case, ctx)
        record["final_observed_state"] = record["steps"][-1]["observed_state"] if record["steps"] else initial

    def _step_header(self, case: Mapping[str, Any], step: Mapping[str, Any]) -> dict[str, Any]:
        head = {"step_no": step["step_no"], "role": step.get("role"), "kind": step["kind"]}
        for key in ("transition_id", "negative_transition_id", "base_transition_id"):
            if key in step:
                head[key] = step[key]
        head["expected_ref"] = {"test_case_id": case["test_case_id"], "step_no": step["step_no"]}
        return head

    def _run_fixture(self, case: Mapping[str, Any], step: Mapping[str, Any]) -> dict[str, Any]:
        rec: dict[str, Any] = {**self._step_header(case, step), "fixture": {"action": step["action"], "target": step["target"],
                                                                         "value": step["value"]}, "error": None}
        started = time.perf_counter()
        try:
            rec["fixture"]["result"] = self.env.apply_fixture(step)
            rec["execution_status"] = "FIXTURE_APPLIED"
        except Exception as exc:  # noqa: BLE001
            rec["execution_status"] = "ERROR"
            rec["error"] = {"kind": "FIXTURE_FAILED", "message": f"{type(exc).__name__}: {exc}"}
        rec["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return rec

    def _run_request_step(self, case: Mapping[str, Any], step: Mapping[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        rec: dict[str, Any] = {**self._step_header(case, step), "error": None}
        req = step["request"]
        try:
            rec["mapped_from"] = self.mapper.validate_step(step)
        except MappingMismatch as exc:
            return {**rec, "execution_status": "ERROR", "request": self._req_view(req, req["path_params"], req["body"], None),
                    "error": {"kind": "MAPPING_MISMATCH", "message": str(exc)}, "duration_ms": 0.0}
        try:
            params = ctx.resolve(req["path_params"])
            body = ctx.resolve(req["body"])
            query = ctx.resolve(req.get("query") or {})
        except UnresolvedReference as exc:
            return {**rec, "execution_status": "NOT_SENT", "request": self._req_view(req, req["path_params"], req["body"], None),
                    "error": {"kind": "UNRESOLVED_REF", "message": str(exc)}, "duration_ms": 0.0}
        url = req["path"]
        for name, value in params.items():
            url = url.replace("{" + name + "}", quote(str(value), safe=""))
        rec["request"] = self._req_view(req, params, body, url, query)
        try:
            res = self.transport.request(req["method"], url, json_body=body, params=query or None)
        except TransportError as exc:
            rec.update(execution_status="ERROR", error={"kind": exc.kind, "message": str(exc)}, duration_ms=0.0)
            return rec
        ctx.record(step["step_no"], res.status_code, res.body)
        captured, missing = ctx.capture(step.get("captures"))
        rec.update(
            execution_status="SENT",
            response={"status_code": res.status_code, "status_class": _status_class(res.status_code),
                      "body_format": res.body_format, "body": redact(res.body, self.redact_keys)},
            captured=captured, capture_missing=missing, duration_ms=round(res.duration_ms, 3))
        return rec

    def _req_view(self, req: Mapping[str, Any], params: Any, body: Any, url: str | None, query: Any = None) -> dict[str, Any]:
        return {"method": req["method"], "path_template": req["path"], "url": url, "path_params": params,
                "query": query or {}, "body": redact(body, self.redact_keys)}

    def _run_probes(self, case: Mapping[str, Any], ctx: ExecutionContext) -> list[dict[str, Any]]:
        """Gửi các probe GET trong ``assertions`` (mỗi URL một lần) và ghi lại phản hồi; không đánh giá."""
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        out: list[dict[str, Any]] = []
        for a in case.get("assertions", []):
            probe = a.get("probe")
            if not isinstance(probe, dict) or probe.get("method") != "GET":
                continue
            try:
                params = ctx.resolve(a.get("path_params", {}))
            except UnresolvedReference as exc:
                out.append({"assertion_ids": [a["id"]], "execution_status": "NOT_SENT", "request": {"method": "GET", "path_template": probe["path"]},
                            "error": {"kind": "UNRESOLVED_REF", "message": str(exc)}})
                continue
            unbound = set(re.findall(r"\{(\w+)\}", probe["path"])) - set(params)
            if unbound:
                out.append({"assertion_ids": [a["id"]], "execution_status": "NOT_SENT", "request": {"method": "GET", "path_template": probe["path"]},
                            "error": {"kind": "UNBOUND_PATH_PARAM", "message": f"probe thiếu path param {sorted(unbound)}"}})
                continue
            url = probe["path"]
            for k, v in params.items():
                url = url.replace("{" + k + "}", quote(str(v), safe=""))
            key = ("GET", url)
            if key in grouped:
                grouped[key]["assertion_ids"].append(a["id"])
                continue
            entry: dict[str, Any] = {"assertion_ids": [a["id"]], "request": {"method": "GET", "path_template": probe["path"], "url": url}}
            try:
                res = self.transport.request("GET", url)
                entry.update(execution_status="SENT", response={"status_code": res.status_code, "status_class": _status_class(res.status_code),
                                                                "body": redact(res.body, self.redact_keys)})
            except TransportError as exc:
                entry.update(execution_status="ERROR", error={"kind": exc.kind, "message": str(exc)})
            grouped[key] = entry
            out.append(entry)
        return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=EXECUTOR_NAME)
    parser.add_argument("--test-cases", default="test_cases.json")
    parser.add_argument("--model", default=None, help="efsm_model.json (mặc định: cùng thư mục test case)")
    parser.add_argument("--output", default="test_execution_results.json")
    parser.add_argument("--mode", choices=("inprocess", "external"), default="inprocess")
    parser.add_argument("--db-path", default=None, help="file SQLite kiểm thử (mặc định: database tạm, tự xóa)")
    parser.add_argument("--base-url", default=None, help="URL server (chế độ external)")
    parser.add_argument("--allow-dirty-target", action="store_true", help="chấp nhận server external không reset được")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--only", default=None, help="danh sách test_case_id, phân cách bằng dấu phẩy")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-observe", action="store_true", help="không thu thập trạng thái sau mỗi bước")
    args = parser.parse_args(argv)
    cases_path = Path(args.test_cases)
    env: Any = None
    try:
        suite = load_suite(cases_path)
        model_path = Path(args.model) if args.model else cases_path.with_name("efsm_model.json")
        if args.mode == "external":
            if not args.base_url:
                raise ExecutorError("--base-url là bắt buộc ở chế độ external")
            env = ExternalEnvironment(args.base_url, allow_dirty_target=args.allow_dirty_target, timeout=args.timeout)
        else:
            env = InProcessEnvironment(args.db_path, project_root=cases_path.resolve().parent)
        mapper = TransitionApiMapper.from_file(model_path, env.routes())
        executor = ApiTestExecutor(env.transport(), env, mapper, observe=not args.no_observe)
        only = [x.strip() for x in args.only.split(",")] if args.only else None
        results = executor.run_suite(suite, only=only, limit=args.limit)
        Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except ExecutorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if env is not None:
            env.close()
    s = results["summary"]
    print(f"total={s['total']} executed={s['executed']} skipped={s['skipped']} execution_errors={s['execution_errors']} "
          f"steps={s['steps']} http={s['http_status_classes']}")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
