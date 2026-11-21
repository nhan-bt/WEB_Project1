# WEB_Project1 — FastAPI + SQLite

## Run from the project root

```cmd
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` to test the APIs.

## Tests and test path generation

```cmd
python -m pytest -v
python test_path_generator.py --model efsm_model.json --strategy both --max-depth 8 --max-paths 200 --include-negative --context test_path_context.bluemoon.json --output test_paths.json
python test_data_generator.py --model efsm_model.json --context test_data_context.bluemoon.json --output generated_guard_data.json
python test_data_generator.py --model efsm_model.json --test-paths test_paths.json --test-cases-output test_cases.json --coverage-output test_data_coverage.json
python api_test_executor.py --test-cases test_cases.json --model efsm_model.json --output test_execution_results.json
python result_validator.py --test-cases test_cases.json --execution-results test_execution_results.json --model efsm_model.json --rules security_rules.json --output test_validation_results.json
python fault_detection_report.py --out-prefix fault_detection_report
```

Run these from the project root. The generators only read the EFSM files; they do not call the API or touch `online_shop.db`. `api_test_executor.py` is the only step that sends requests: it runs against a separate temporary SQLite database in-process (FastAPI TestClient) and refuses to run on `online_shop.db`.

## Path feasibility classification (PowerShell, `.venv`)

Classifies every path as `FEASIBLE`, `PROVEN_INFEASIBLE` (logical contradiction, valid for every data/setup/product/input), `SEARCH_EXHAUSTED` (bounded search only, not a proof), `GENERATOR_ERROR` or `MODEL_ERROR`. Outputs go to `experiments\after_fix\`; the delivered files in the project root are not overwritten.

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_path_feasibility.py
.\.venv\Scripts\python.exe guard_evaluator.py efsm_model.json
.\.venv\Scripts\python.exe feasibility_path_generator.py --model efsm_model.json --strategy both --max-depth 8 --max-paths 200 --include-negative --context test_path_context.bluemoon.json --dfs-feasibility annotate --output experiments\after_fix\test_paths.json --coverage-output experiments\after_fix\test_path_coverage.json
.\.venv\Scripts\python.exe test_data_generator.py --model efsm_model.json --test-paths experiments\after_fix\test_paths.json --test-cases-output experiments\after_fix\test_cases.json --coverage-output experiments\after_fix\test_data_coverage.json --feasibility-report-output experiments\after_fix\feasibility_report.md
.\.venv\Scripts\python.exe api_test_executor.py --test-cases experiments\after_fix\test_cases.json --model efsm_model.json --output experiments\after_fix\test_execution_results.json
.\.venv\Scripts\python.exe result_validator.py --test-cases experiments\after_fix\test_cases.json --execution-results experiments\after_fix\test_execution_results.json --model efsm_model.json --rules security_rules.json --output experiments\after_fix\test_validation_results.json
.\.venv\Scripts\python.exe experiments\compare_feasibility.py --before experiments\before_fix --after experiments\after_fix --output experiments\comparison.md
```

`--dfs-feasibility`: `annotate` (default: same paths as `test_path_generator.py`, plus `feasibility_proof`), `prune` (DFS cuts proven-infeasible extensions while traversing; listed in `pruned_infeasible_extensions`), `off`. `test_data_generator.py --max-input-options N` sets how many valid input sets per step the second search phase may try (1 disables data backtracking).

## Structure

- `app/main.py`: creates the FastAPI app, initializes SQLite, and registers routers.
- `app/routes/`: HTTP endpoints.
- `app/service/`: product/cart/order/payment business rules.
- `app/database.py`: SQLite engine, SQLAlchemy sessions, table initialization, demo seed data.
- `app/models.py`: ORM tables for products, cart items, orders, order items, and payments.
- `app/schemas.py`: Pydantic request validation.
- `tests/`: all tests. `test_api.py` and `test_efsm_backend_conformance.py` use a separate `test_online_shop.db` database; `test_guard_evaluator.py` and `test_test_path_generator.py` are unit tests that never call the API or touch a database.
- `efsm_model.json`, `security_rules.json`, `guard_evaluator.py`: EFSM model of the business workflow, Business Security Rules, and the guard/invariant evaluator.
- `test_path_generator.py`: generates positive and negative test paths from the EFSM. Outputs: `test_paths.json`, `test_path_coverage.json`. `test_path_context.bluemoon.json` is an optional example runtime context.
- `test_data_generator.py`: generates valid/invalid test data for each guard (equivalence partitioning + boundary value analysis) and re-checks every sample with `guard_evaluator.py`. Output: `generated_guard_data.json`. `test_data_context.bluemoon.json` is an optional example context.
- `test_data_generator.py --test-paths`: path-aware mode. Turns every path of `test_paths.json` into a concrete, not-yet-executed test case (`test_cases.json`, coverage in `test_data_coverage.json`) using `api_test_mapping.json`, which describes how each EFSM transition is called through the API (it describes calls only; nothing is sent).
- `api_test_executor.py`: API Test Executor (Part 6). Reads `test_cases.json`, maps each transition to its real endpoint, runs the steps in order against an isolated test database, and records HTTP status, response and observed state in `test_execution_results.json`. It only records; it does not decide PASS/FAIL (that is the Result Validator, Part 7). Tests: `tests/test_api_test_executor.py`.
- `result_validator.py`: Result Validator (Part 7). Reads `test_cases.json`, `test_execution_results.json`, `efsm_model.json`, `security_rules.json` (no HTTP, no database) and writes `test_validation_results.json` with PASS / FAIL / SKIPPED / INCONCLUSIVE per test case and expected/actual/evidence per assertion for Part 8. Tests: `tests/test_result_validator.py` (includes synthetic fault injection).
- `fault_detection_report.py`: Fault Detection Report (Part 8). Aggregates `test_validation_results.json` into findings (two research groups, severity, dedup, traceability, coverage) and writes `fault_detection_report.json/.html/.md`. Runs a separate synthetic fault-injection campaign (FI01-FI06) on copies of the execution results; synthetic findings are never counted as real. Tests: `tests/test_fault_detection_report.py`.
- `docs/`: EFSM-to-backend mapping, validation reports, and the design/validation notes of the test path generator and the test data generator.

## SQLite data

The application creates `online_shop.db` in this project directory the first time it starts. Sample products are inserted only when the product table is empty. Orders, order items, cart items, product stock, and simulated payments are saved in SQLite and survive a server restart.

This is still a learning/demo app: the cart is shared because user accounts and authentication have not been implemented. Payments are simulated; no real payment provider is connected. Do not use it for real customer or payment data.
