"""Unit test cho test_path_generator.py.

Nhóm A: đồ thị      B: BFS       C: DFS        D: negative path
Nhóm E: khả thi     F: coverage  G: output/CLI
Phần cuối: integration với efsm_model.json thật của BlueMoon.

Các nhóm A-G dùng một EFSM nhỏ tự dựng (độc lập với BlueMoon). Không gọi API, không dùng DB.
"""
import copy
import json
from pathlib import Path

import pytest

from guard_evaluator import GuardEvaluator
from test_path_generator import (
    ContextError,
    Feasibility,
    ModelFormatError,
    NegativeCategory,
    PathContext,
    PathStructureError,
    TestPathGenerator,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
BLUEMOON_MODEL = ROOT / "efsm_model.json"
BLUEMOON_RULES = ROOT / "security_rules.json"
BLUEMOON_CONTEXT = ROOT / "test_path_context.bluemoon.json"


# --------------------------------------------------------------------------
# EFSM nhỏ dùng cho test thuật toán
#
#   S0 --A(self-loop, add)--> S0        S0 --B1,B2 (song song)--> S1
#   S1 --C--> S2 --E--> S3 (terminal)   S2 --D--> S0 (chu trình)
#   S1 --F(self-loop, làm n âm)--> S1   S9 --U--> S0 (S9 không đạt tới được)
# --------------------------------------------------------------------------
def fixture_model() -> dict:
    assign = lambda var, **kw: {"type": "assign", "var": var, **kw}  # noqa: E731
    return {
        "model_name": "Mini", "schema_version": "2.0", "initial_state": "S0",
        "states": [{"id": "S0", "type": "initial"}, {"id": "S1"}, {"id": "S2"}, {"id": "S3", "type": "terminal"}, {"id": "S9"}],
        "variables": [
            {"name": "n", "type": "integer", "origin": "database"},
            {"name": "limit", "type": "integer", "origin": "database"},
            {"name": "bonus", "type": "integer", "origin": "database"},
            {"name": "amount", "type": "integer", "origin": "request"},
            {"name": "status", "type": "string", "origin": "database", "allowed_values": ["NEW", "DONE"]},
        ],
        "transitions": [
            {"id": "A", "source": "S0", "target": "S0", "event": "add", "expected_http_status": 200, "rule_refs": ["R1"],
             "guard": {"all": [{"var": "amount", "op": ">", "value": 0}, {"expr": "n + amount <= limit"}]},
             "actions": [assign("n", expr="n + amount")]},
            {"id": "B1", "source": "S0", "target": "S1", "event": "open", "rule_refs": ["R2"],
             "guard": {"var": "n", "op": ">", "value": 0}, "actions": [assign("status", value="NEW")]},
            {"id": "B2", "source": "S0", "target": "S1", "event": "open_alt", "rule_refs": [],
             "guard": {"var": "n", "op": ">", "value": 1}, "actions": [assign("status", value="NEW")]},
            {"id": "C", "source": "S1", "target": "S2", "event": "close", "expected_http_status": 200, "rule_refs": ["R3"],
             "guard": {"var": "status", "op": "==", "value": "NEW"},
             "actions": [assign("status", value="DONE"), {"type": "effect", "description": "write closing row"}]},
            {"id": "D", "source": "S2", "target": "S0", "event": "reopen", "rule_refs": [],
             "guard": {"var": "status", "op": "==", "value": "DONE"}, "actions": [assign("n", value=0)]},
            {"id": "E", "source": "S2", "target": "S3", "event": "finish", "rule_refs": ["R3"],
             "guard": {"var": "status", "op": "==", "value": "DONE"}, "actions": []},
            {"id": "F", "source": "S1", "target": "S1", "event": "penalty", "rule_refs": [],
             "guard": {"var": "n", "op": ">=", "value": 0}, "actions": [assign("n", expr="n - 100")]},
            {"id": "U", "source": "S9", "target": "S0", "event": "revive", "rule_refs": [], "guard": None, "actions": []},
        ],
        "invariants": [{"id": "I1", "scope": "GLOBAL", "kind": "state_predicate", "check": {"var": "n", "op": ">=", "value": 0}}],
        "negative_transitions": [
            {"id": "NA", "source": "S3", "event": "finish", "base_transition": "E", "invalid_condition": "status == DONE",
             "expected": {"accepted": False, "http_status": 409, "state_unchanged": True, "data_unchanged": True}, "rule_refs": ["R3"], "mutant_refs": []},
            {"id": "NB", "source": "S0", "event": "add", "base_transition": "A", "invalid_condition": "amount <= 0",
             "expected": {"accepted": False, "http_status": 422, "state_unchanged": True, "data_unchanged": True}, "rule_refs": ["R1"], "mutant_refs": ["M1"]},
            {"id": "NC", "source": "S0", "event": "add", "base_transition": "A", "invalid_condition": "n + amount > limit",
             "expected": {"accepted": False, "http_status": 400, "state_unchanged": True, "data_unchanged": True}, "rule_refs": ["R1"], "mutant_refs": []},
            {"id": "ND", "source": "S9", "event": "revive", "base_transition": "U", "invalid_condition": "n < 0",
             "expected": {"accepted": False}, "rule_refs": [], "mutant_refs": []},
            {"id": "NE", "source": "S2", "event": "close", "base_transition": "C", "invalid_condition": "status != NEW",
             "expected": {"accepted": False, "state_unchanged": True}, "rule_refs": ["R4"], "mutant_refs": []},
        ],
    }


FIXTURE_RULES = {
    "rules": [
        {"rule_id": "R1", "name": "Add rule", "category": "X", "severity": "HIGH", "status": "IMPLEMENTED"},
        {"rule_id": "R2", "name": "Open rule", "category": "X", "severity": "MEDIUM", "status": "IMPLEMENTED"},
        {"rule_id": "R3", "name": "Close rule", "category": "X", "severity": "LOW", "status": "IMPLEMENTED"},
        {"rule_id": "R4", "name": "Close only from S1", "category": "X", "severity": "HIGH", "status": "IMPLEMENTED"},
        {"rule_id": "R5", "name": "Never linked", "category": "X", "severity": "LOW", "status": "IMPLEMENTED"},
    ],
    "excluded_rules": [{"rule_id": "R6", "name": "Out of scope", "status": "NOT_APPLICABLE"}],
}

FIXTURE_CONTEXT = {
    "initial_variables": {"n": 0, "limit": 10},
    "step_inputs": {"A": {"amount": 3}},
    "negative_inputs": {"NB": {"amount": 0}, "NC": {"amount": 20}, "NA": {}, "NE": {}},
}


def write(tmp_path: Path, name: str, data: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def make_generator(tmp_path: Path, model: dict | None = None, rules: bool = True, **kwargs) -> TestPathGenerator:
    model_path = write(tmp_path, "model.json", model or fixture_model())
    rules_path = write(tmp_path, "rules.json", FIXTURE_RULES) if rules else None
    return TestPathGenerator(model_path, rules_path, **kwargs)


@pytest.fixture
def gen(tmp_path) -> TestPathGenerator:
    return make_generator(tmp_path)


@pytest.fixture
def ctx() -> PathContext:
    return PathContext.from_dict(FIXTURE_CONTEXT)


def keys(paths) -> list[str]:
    return [p.path_key for p in paths]


# ==========================================================================
# Nhóm A - Graph construction
# ==========================================================================
class TestGraphConstruction:
    def test_valid_model_builds_graph(self, gen):
        graph = gen.build_graph()
        assert graph.states == ["S0", "S1", "S2", "S3", "S9"] and graph.initial_state == "S0"
        assert [e.transition_id for e in graph.edges] == ["A", "B1", "B2", "C", "D", "E", "F", "U"]

    def test_edges_carry_source_target_event(self, gen):
        edge = gen.graph.edge("C")
        assert (edge.source, edge.target, edge.event) == ("S1", "S2", "close")

    def test_self_loops_are_kept(self, gen):
        assert [e.transition_id for e in gen.graph.self_loops()] == ["A", "F"]
        assert [e.transition_id for e in gen.graph.out_edges("S0")][0] == "A"
        assert "A" in [e.transition_id for e in gen.graph.in_edges("S0")]

    def test_parallel_edges_between_same_states(self, gen):
        assert gen.graph.parallel_edges() == {("S0", "S1"): ["B1", "B2"]}

    def test_unreachable_and_dead_end_states(self, gen):
        assert gen.graph.unreachable_states() == ["S9"]
        assert gen.graph.reachable_states() == ["S0", "S1", "S2", "S3"]
        assert gen.graph.dead_ends() == ["S3"]

    def test_cycle_detection(self, gen, tmp_path):
        assert gen.graph.has_cycle() is True
        acyclic = fixture_model()
        acyclic["transitions"] = [t for t in acyclic["transitions"] if t["id"] in ("B1", "C", "E")]
        acyclic["negative_transitions"] = []
        assert make_generator(tmp_path, acyclic).graph.has_cycle() is False

    def test_self_loop_alone_is_a_cycle(self, tmp_path):
        model = fixture_model()
        model["transitions"] = [t for t in model["transitions"] if t["id"] == "A"]
        model["negative_transitions"] = []
        assert make_generator(tmp_path, model).graph.has_cycle() is True

    def test_negative_transitions_are_not_graph_edges(self, gen):
        ids = {e.transition_id for e in gen.graph.edges}
        assert ids.isdisjoint({"NA", "NB", "NC", "ND", "NE"})
        with pytest.raises(PathStructureError):
            gen.graph.edge("NA")

    def test_initial_state_falls_back_to_state_type(self, tmp_path):
        model = fixture_model()
        del model["initial_state"]
        assert make_generator(tmp_path, model).graph.initial_state == "S0"

    def test_missing_initial_state_is_an_error(self, tmp_path):
        model = fixture_model()
        del model["initial_state"]
        model["states"][0]["type"] = "intermediate"
        with pytest.raises(ModelFormatError, match="initial"):
            make_generator(tmp_path, model)

    def test_invalid_model_is_reported_as_model_format_error(self, tmp_path):
        model = fixture_model()
        model["transitions"][0]["target"] = "NOWHERE"
        with pytest.raises(ModelFormatError):
            make_generator(tmp_path, model)

    def test_missing_files(self, tmp_path):
        with pytest.raises(ModelFormatError):
            TestPathGenerator(tmp_path / "nope.json")
        with pytest.raises(ModelFormatError, match="rules"):
            TestPathGenerator(write(tmp_path, "m.json", fixture_model()), tmp_path / "nope.json")

    def test_model_is_not_modified(self, tmp_path):
        path = write(tmp_path, "m.json", fixture_model())
        before = path.read_bytes()
        TestPathGenerator(path).generate(strategy="both", include_negative=True)
        assert path.read_bytes() == before


# ==========================================================================
# Nhóm B - BFS
# ==========================================================================
class TestBfs:
    def test_shortest_path_to_state(self, gen):
        assert [e.transition_id for e in gen.graph.shortest_path("S2")] == ["B1", "C"]
        assert [e.transition_id for e in gen.graph.shortest_path("S3")] == ["B1", "C", "E"]
        assert gen.graph.shortest_path("S0") == []
        assert gen.graph.shortest_path("S9") is None

    def test_tie_between_parallel_edges_uses_model_order(self, gen):
        assert gen.graph.shortest_path("S1")[0].transition_id == "B1"

    def test_shortest_path_to_edge_ends_with_that_edge(self, gen):
        assert [e.transition_id for e in gen.graph.shortest_path_to_edge("D")] == ["B1", "C", "D"]
        assert gen.graph.shortest_path_to_edge("U") is None

    def test_paths_start_at_initial_and_are_consecutive(self, gen):
        for path in gen.generate_bfs_paths():
            assert path.states[0] == "S0"
            edges = [gen.graph.edge(t) for t in path.transition_ids]
            assert all(a.target == b.source for a, b in zip(edges, edges[1:]))
            assert path.states == ["S0"] + [e.target for e in edges]

    def test_bfs_covers_every_reachable_transition(self, gen):
        covered = {t for p in gen.generate_bfs_paths() for t in p.transition_ids}
        assert covered == {"A", "B1", "B2", "C", "D", "E", "F"}

    def test_bfs_covers_every_reachable_state(self, gen):
        states = {s for p in gen.generate_bfs_paths() for s in p.states}
        assert states == {"S0", "S1", "S2", "S3"}

    def test_prefix_pruning_keeps_only_maximal_paths(self, gen):
        pruned = keys(gen.generate_bfs_paths(prune_prefixes=True))
        full = keys(gen.generate_bfs_paths(prune_prefixes=False))
        assert len(pruned) < len(full) and gen.last_bfs_info["pruned_prefix_paths"] == 0
        assert not any(a != b and b.startswith(a + ">") for a in pruned for b in pruned)
        assert {t for k in pruned for t in k.split(">")} == {t for k in full for t in k.split(">")}

    def test_goals_are_recorded(self, gen):
        paths = {p.path_key: p for p in gen.generate_bfs_paths(prune_prefixes=False)}
        assert {"type": "state", "id": "S3"} in paths["B1>C>E"].goals
        assert {"type": "transition", "id": "E"} in paths["B1>C>E"].goals

    def test_bfs_is_deterministic(self, tmp_path):
        first = keys(make_generator(tmp_path).generate_bfs_paths())
        second = keys(make_generator(tmp_path).generate_bfs_paths())
        assert first == second

    def test_max_depth_is_respected(self, gen):
        paths = gen.generate_bfs_paths(max_depth=2)
        assert all(len(p.transition_ids) <= 2 for p in paths)
        assert gen.last_bfs_info["targets_over_max_depth"] > 0

    def test_max_paths_truncates_and_reports(self, gen):
        paths = gen.generate_bfs_paths(max_paths=2)
        assert len(paths) == 2 and gen.last_bfs_info["truncated"] is True

    def test_path_ids_are_sequential(self, gen):
        assert [p.path_id for p in gen.generate_bfs_paths()] == [f"P{i:03d}" for i in range(1, len(gen.generate_bfs_paths()) + 1)]

    def test_feasible_mode_prefers_prefix_that_satisfies_guards(self, gen, ctx):
        structural = keys(gen.generate_bfs_paths(prefix_mode="structural"))
        feasible = keys(gen.generate_bfs_paths(ctx, prefix_mode="feasible"))
        assert "B1>C>E" in structural and "A>B1>C>E" in feasible
        assert "A>B1>C>E" not in structural

    def test_feasible_mode_requires_context(self, gen):
        with pytest.raises(ValueError):
            gen.generate_bfs_paths(prefix_mode="feasible")
        with pytest.raises(ValueError):
            gen.generate_bfs_paths(prefix_mode="bogus")


# ==========================================================================
# Nhóm C - DFS
# ==========================================================================
class TestDfs:
    def test_generates_many_paths(self, gen):
        paths = gen.generate_dfs_paths(max_paths=500)
        assert len(paths) > 5 and len(set(keys(paths))) == len(paths)

    def test_terminates_on_cycles_and_self_loops(self, gen):
        paths = gen.generate_dfs_paths(max_depth=50, max_paths=100000, max_visits_per_state=2, max_transition_repeats=1)
        assert paths and gen.last_dfs_info["truncated"] is False
        assert all(len(p.transition_ids) <= 8 for p in paths)  # chặn bởi giới hạn lặp, không phải max_depth

    @pytest.mark.parametrize("depth", [1, 2, 3, 5])
    def test_max_depth(self, gen, depth):
        paths = gen.generate_dfs_paths(max_depth=depth, max_paths=10000)
        assert paths and max(len(p.transition_ids) for p in paths) <= depth

    @pytest.mark.parametrize("limit", [1, 3, 10])
    def test_max_paths(self, gen, limit):
        paths = gen.generate_dfs_paths(max_paths=limit)
        assert len(paths) == limit and gen.last_dfs_info["truncated"] is True

    def test_exact_count_is_not_reported_as_truncated(self, gen):
        total = len(gen.generate_dfs_paths(max_paths=100000))
        assert len(gen.generate_dfs_paths(max_paths=total)) == total
        assert gen.last_dfs_info["truncated"] is False

    def test_max_visits_per_state(self, gen):
        for p in gen.generate_dfs_paths(max_visits_per_state=1, max_paths=10000):
            assert all(p.states.count(s) <= 1 for s in set(p.states))
        for p in gen.generate_dfs_paths(max_visits_per_state=2, max_paths=10000):
            assert all(p.states.count(s) <= 2 for s in set(p.states))

    def test_max_transition_repeats(self, gen):
        for p in gen.generate_dfs_paths(max_transition_repeats=1, max_paths=10000):
            assert len(p.transition_ids) == len(set(p.transition_ids))
        repeated = gen.generate_dfs_paths(max_transition_repeats=2, max_visits_per_state=3, max_paths=10000)
        assert any("A>A" in p.path_key for p in repeated)

    def test_self_loop_and_cycle_paths_are_found(self, gen):
        joined = keys(gen.generate_dfs_paths(max_paths=10000))
        assert any(k.startswith("A>") for k in joined)  # self-loop
        assert any("D" in k.split(">") for k in joined)  # chu trình S0->S1->S2->S0

    def test_paths_are_maximal_and_structurally_valid(self, gen):
        for p in gen.generate_dfs_paths(max_paths=10000):
            gen._edges_for(p.transition_ids)
            assert p.extra["dfs_termination"] in ("TERMINAL_STATE", "LIMIT")
            if p.extra["dfs_termination"] == "TERMINAL_STATE":
                assert gen.graph.out_edges(p.states[-1]) == []

    def test_dfs_is_deterministic(self, tmp_path):
        a = keys(make_generator(tmp_path).generate_dfs_paths(max_paths=50))
        b = keys(make_generator(tmp_path).generate_dfs_paths(max_paths=50))
        assert a == b

    @pytest.mark.parametrize("name", ["max_depth", "max_paths", "max_visits_per_state", "max_transition_repeats"])
    def test_invalid_limits(self, gen, name):
        with pytest.raises(ValueError, match=name):
            gen.generate_dfs_paths(**{name: 0})


# ==========================================================================
# Nhóm D - Negative paths
# ==========================================================================
class TestNegativePaths:
    def test_negative_step_is_last_and_unique(self, gen, ctx):
        for path in gen.generate_negative_paths(ctx):
            kinds = [s["kind"] for s in path.steps]
            assert kinds[-1] == "negative_transition" and kinds.count("negative_transition") == 1
            assert path.steps[-1]["step_index"] == len(path.steps)

    def test_prefix_is_a_positive_path_to_the_source_state(self, gen, ctx):
        for path in gen.generate_negative_paths(ctx):
            gen._edges_for(path.transition_ids)  # prefix hợp lệ trên đồ thị positive
            assert path.states[-1] == gen._negatives[path.extra["negative_transition_id"]]["source"]
            assert path.states[0] == "S0"

    def test_no_negative_id_inside_prefix(self, gen, ctx):
        negative_ids = set(gen._negatives)
        for path in gen.generate_negative_paths(ctx):
            assert negative_ids.isdisjoint(path.transition_ids)
            assert all(s["kind"] == "transition" for s in path.steps[:-1])

    def test_structural_prefix_is_the_shortest_positive_path(self, gen):
        by_negative = {p.extra["negative_transition_id"]: p for p in gen.generate_negative_paths(prefix_mode="structural")}
        assert by_negative["NA"].transition_ids == ["B1", "C", "E"]
        assert by_negative["NB"].transition_ids == []
        assert by_negative["NE"].transition_ids == ["B1", "C"]

    def test_feasible_prefix_includes_setup_steps(self, gen, ctx):
        by_negative = {p.extra["negative_transition_id"]: p for p in gen.generate_negative_paths(ctx)}
        assert by_negative["NA"].transition_ids == ["A", "B1", "C", "E"]

    def test_unreachable_source_is_skipped_and_reported(self, gen):
        paths = gen.generate_negative_paths()
        assert "ND" not in {p.extra["negative_transition_id"] for p in paths}
        assert gen.last_negative_skipped == [{"negative_transition_id": "ND", "reason": "SOURCE_STATE_UNREACHABLE"}]

    def test_negative_prefix_longer_than_max_depth_is_skipped(self, gen):
        paths = gen.generate_negative_paths(prefix_mode="structural", max_depth=1)
        assert {p.extra["negative_transition_id"] for p in paths} == {"NB", "NC"}
        assert {s["reason"] for s in gen.last_negative_skipped} >= {"PREFIX_EXCEEDS_MAX_DEPTH"}

    def test_expected_rejection_data_comes_from_the_model(self, gen):
        paths = {p.extra["negative_transition_id"]: p for p in gen.generate_negative_paths()}
        rejection = paths["NB"].extra["expected_rejection"]
        assert rejection["rejected"] is True and rejection["http_status"] == 422
        assert rejection["violated_condition"] == "amount <= 0"
        assert "negative_transitions[NB]" in rejection["http_status_source"]
        assert paths["NB"].extra["expected_state_unchanged"] is True

    def test_http_status_is_never_invented(self, gen):
        path = next(p for p in gen.generate_negative_paths() if p.extra["negative_transition_id"] == "NE")
        assert path.extra["expected_rejection"]["http_status"] is None
        assert path.extra["expected_rejection"]["http_status_source"] is None
        assert path.steps[-1]["expected_http_status"] is None

    def test_forbidden_effects_come_from_the_base_transition(self, gen):
        path = next(p for p in gen.generate_negative_paths() if p.extra["negative_transition_id"] == "NE")
        assert path.extra["expected_rejection"]["forbidden_backend_effects"] == ["write closing row"]

    def test_category_invalid_state_transition(self, gen):
        info = gen.classify_negative("NA")
        assert info["categories"] == ["INVALID_STATE_TRANSITION"]
        assert info["evidence"]["allowed_source_states_for_event"] == ["S2"]
        assert gen.classify_negative("NE")["primary"] == "INVALID_STATE_TRANSITION"

    def test_category_input_validation_and_guard_violation(self, gen):
        info = gen.classify_negative("NB")
        assert info["categories"] == ["INPUT_VALIDATION_VIOLATION", "GUARD_VIOLATION"]
        assert info["primary"] == "INPUT_VALIDATION_VIOLATION"

    def test_category_business_constraint_keeps_multiple_labels(self, gen):
        info = gen.classify_negative("NC")
        assert info["categories"] == ["BUSINESS_CONSTRAINT_VIOLATION", "GUARD_VIOLATION"]
        assert info["evidence"]["condition_variables"] == {"amount": "request", "limit": "database", "n": "database"}

    def test_input_validation_statuses_are_configurable(self, tmp_path):
        generator = make_generator(tmp_path, input_validation_statuses=(409,))
        assert "INPUT_VALIDATION_VIOLATION" in generator.classify_negative("NA")["categories"]

    def test_negative_metadata_is_complete(self, gen, ctx):
        for path in gen.generate_negative_paths(ctx):
            for key in ("negative_transition_id", "negative_category", "negative_categories", "expected_rejection",
                        "expected_state_unchanged", "base_transition_id", "category_evidence"):
                assert key in path.extra, (path.path_key, key)
            assert path.path_type == "negative" and path.expected_outcome == "REJECT_FINAL_STEP"
            assert path.extra["negative_category"] in {c.value for c in NegativeCategory}

    def test_rule_ids_and_severity_of_negative_path(self, gen):
        path = next(p for p in gen.generate_negative_paths() if p.extra["negative_transition_id"] == "NB")
        assert path.security_rule_ids == ["R1"] and path.severity == "HIGH"
        assert path.extra["mutant_refs"] == ["M1"]

    def test_path_key_of_negative_paths(self, gen):
        keys_ = keys(gen.generate_negative_paths(prefix_mode="structural"))
        assert "!NB" in keys_ and "B1>C>E>!NA" in keys_

    def test_violated_guard_is_identified_with_context(self, gen, ctx):
        path = next(p for p in gen.generate_negative_paths(ctx) if p.extra["negative_transition_id"] == "NB")
        violated = path.extra["expected_rejection"]["violated_transition_guards"]
        assert violated[0]["transition_id"] == "A" and violated[0]["status"] == "GUARD_NOT_SATISFIED"
        assert violated[0]["failed_conditions"][0]["condition"] == "amount > 0"

    def test_model_without_negative_transitions(self, tmp_path):
        model = fixture_model()
        del model["negative_transitions"]
        assert make_generator(tmp_path, model).generate_negative_paths() == []


# ==========================================================================
# Nhóm E - Feasibility
# ==========================================================================
class TestFeasibility:
    def test_all_guards_satisfied_is_feasible(self, gen, ctx):
        result = gen.evaluate_path_feasibility(["A", "B1", "C", "E"], ctx)
        assert result.status is Feasibility.FEASIBLE and result.proof_scope == "GIVEN_CONTEXT"
        assert result.final_state == "S3"
        assert [s["verdict"] for s in result.steps] == ["OK"] * 4
        assert result.witness["step_inputs"][0] == {"step_index": 1, "transition_id": "A", "inputs": {"amount": 3}}

    def test_guard_not_satisfied_is_infeasible(self, gen, ctx):
        result = gen.evaluate_path_feasibility(["B1"], ctx)  # n == 0 nên n > 0 sai
        assert result.status is Feasibility.INFEASIBLE and result.reason_code == "GUARD_NOT_SATISFIED"
        assert result.failed_step == 1 and result.proof_scope == "GIVEN_CONTEXT"
        assert result.steps[0]["failed_conditions"][0]["condition"] == "n > 0"

    def test_each_guard_individually_satisfiable_is_not_enough(self, gen):
        """Mỗi guard thỏa mãn riêng lẻ, nhưng chuỗi A,B2 thì không: B2 cần n > 1 mà A chỉ cộng 1."""
        context = PathContext.from_dict({"initial_variables": {"n": 0, "limit": 10}, "step_inputs": {"A": {"amount": 1}}})
        assert gen.evaluator.check_transition("B2", "S0", {"n": 2}).enabled
        assert gen.evaluator.check_transition("A", "S0", {"n": 0, "limit": 10, "amount": 1}).enabled
        assert gen.evaluate_path_feasibility(["A", "B2"], context).status is Feasibility.INFEASIBLE

    def test_no_context_is_unknown(self, gen):
        result = gen.evaluate_path_feasibility(["A"], None)
        assert result.status is Feasibility.UNKNOWN and result.reason_code == "NO_RUNTIME_CONTEXT"
        assert result.proof_scope is None and result.witness is None

    def test_missing_variable_is_unknown_not_feasible(self, gen):
        context = PathContext.from_dict({"initial_variables": {"n": 0}, "step_inputs": {"A": {"amount": 3}}})  # thiếu limit
        result = gen.evaluate_path_feasibility(["A"], context)
        assert result.status is Feasibility.UNKNOWN and result.reason_code == "MISSING_VARIABLE"
        assert result.steps[0]["missing_variables"] == ["limit"]

    def test_type_error_is_unknown(self, gen):
        context = PathContext.from_dict({"initial_variables": {"n": "zero", "limit": 10}, "step_inputs": {"A": {"amount": 1}}})
        assert gen.evaluate_path_feasibility(["A"], context).status is Feasibility.UNKNOWN

    def test_action_that_cannot_be_simulated_is_unknown(self, tmp_path):
        model = fixture_model()
        model["transitions"][0]["actions"] = [{"type": "assign", "var": "n", "expr": "n + bonus"}]  # bonus không có trong context
        generator = make_generator(tmp_path, model)
        context = PathContext.from_dict({"initial_variables": {"n": 0, "limit": 10}, "step_inputs": {"A": {"amount": 1}}})
        result = generator.evaluate_path_feasibility(["A"], context)
        assert result.status is Feasibility.UNKNOWN and result.reason_code == "ACTION_MISSING_VARIABLE"
        assert result.steps[0]["guard_status"] == "GUARD_SATISFIED"  # guard đúng nhưng action không mô phỏng được

    def test_guard_depends_on_data_from_previous_transition(self, gen, ctx):
        # C chỉ thỏa mãn vì B1 đã gán status = NEW
        assert gen.evaluator.check_transition("C", "S1", {}).missing_variables == ["status"]
        assert gen.evaluate_path_feasibility(["A", "B1", "C"], ctx).status is Feasibility.FEASIBLE

    def test_state_is_updated_between_steps(self, gen, ctx):
        result = gen.evaluate_path_feasibility(["A", "B1"], ctx)
        assert result.final_variables["n"] == 3 and result.final_variables["status"] == "NEW"
        again = gen.evaluate_path_feasibility(["A", "A"], ctx)  # 3 + 3 = 6 <= 10
        assert again.status is Feasibility.FEASIBLE and again.final_variables["n"] == 6

    def test_limit_reached_across_steps(self, gen):
        context = PathContext.from_dict({"initial_variables": {"n": 0, "limit": 5}, "step_inputs": {"A": {"amount": 3}}})
        result = gen.evaluate_path_feasibility(["A", "A"], context)  # 3 + 3 > 5
        assert result.status is Feasibility.INFEASIBLE and result.failed_step == 2

    def test_request_inputs_are_not_carried_to_later_steps(self, gen):
        context = PathContext.from_dict({"initial_variables": {"n": 0, "limit": 10}, "step_inputs": {"1": {"amount": 3}}})
        result = gen.evaluate_path_feasibility(["A", "A"], context)  # bước 2 không có amount
        assert result.status is Feasibility.UNKNOWN and result.steps[1]["missing_variables"] == ["amount"]

    def test_step_index_key_overrides_transition_key(self, gen):
        context = PathContext.from_dict({"initial_variables": {"n": 0, "limit": 10},
                                         "step_inputs": {"A": {"amount": 1}, "2": {"amount": 4}}})
        result = gen.evaluate_path_feasibility(["A", "A"], context)
        assert result.final_variables["n"] == 5

    def test_invariant_violation_makes_path_infeasible(self, gen, ctx):
        result = gen.evaluate_path_feasibility(["A", "B1", "F"], ctx)  # F: n := n - 100 < 0
        assert result.status is Feasibility.INFEASIBLE and result.reason_code == "INVARIANT_VIOLATED"
        assert result.steps[2]["invariant_violations"] == ["I1"]

    def test_unsimulated_effects_are_listed_not_executed(self, gen, ctx):
        result = gen.evaluate_path_feasibility(["A", "B1", "C"], ctx)
        assert result.steps[2]["unsimulated_effects"] == ["write closing row"]

    def test_strict_policy_limits_verdict_to_unknown(self, tmp_path, ctx):
        strict = make_generator(tmp_path, effects_policy="strict")
        assert strict.evaluate_path_feasibility(["A", "B1"], ctx).status is Feasibility.FEASIBLE  # chưa có effect
        result = strict.evaluate_path_feasibility(["A", "B1", "C"], ctx)
        assert result.status is Feasibility.UNKNOWN and result.reason_code == "EFFECTS_NOT_SIMULATED"

    def test_strict_policy_does_not_conclude_infeasible_after_an_effect(self, tmp_path):
        model = fixture_model()
        model["transitions"][3]["guard"] = None  # C luôn mở
        model["transitions"][4]["guard"] = {"var": "status", "op": "==", "value": "NEW"}  # D sai sau C
        strict = make_generator(tmp_path, model, effects_policy="strict")
        context = PathContext.from_dict({"initial_variables": {"n": 3, "limit": 10, "status": "NEW"}})
        result = strict.evaluate_path_feasibility(["B1", "C", "D"], context)
        assert result.status is Feasibility.UNKNOWN and result.reason_code == "UNCONCLUSIVE_AFTER_UNSIMULATED_EFFECTS"
        default = make_generator(tmp_path, model)
        assert default.evaluate_path_feasibility(["B1", "C", "D"], context).status is Feasibility.INFEASIBLE

    def test_invalid_effects_policy(self, tmp_path):
        with pytest.raises(ValueError):
            make_generator(tmp_path, effects_policy="yolo")

    def test_structurally_invalid_sequence_raises(self, gen, ctx):
        with pytest.raises(PathStructureError, match="starts at"):
            gen.evaluate_path_feasibility(["C"], ctx)
        with pytest.raises(PathStructureError, match="unknown transition"):
            gen.evaluate_path_feasibility(["ZZ"], ctx)
        with pytest.raises(PathStructureError):
            gen.evaluate_path_feasibility([], ctx, negative_transition_id="NOPE")

    def test_negative_applicable_makes_negative_path_feasible(self, gen, ctx):
        result = gen.evaluate_path_feasibility([], ctx, negative_transition_id="NB")
        assert result.status is Feasibility.FEASIBLE and result.negative_check["status"] == "NEGATIVE_APPLICABLE"
        assert result.witness["step_inputs"][-1] == {"step_index": 1, "negative_transition_id": "NB", "inputs": {"amount": 0}}

    def test_negative_not_applicable_is_infeasible(self, gen):
        context = PathContext.from_dict({"initial_variables": {"n": 0, "limit": 10}, "negative_inputs": {"NB": {"amount": 5}}})
        result = gen.evaluate_path_feasibility([], context, negative_transition_id="NB")
        assert result.status is Feasibility.INFEASIBLE and result.reason_code == "NEGATIVE_CONDITION_NOT_TRIGGERED"

    def test_negative_without_inputs_is_unknown(self, gen):
        context = PathContext.from_dict({"initial_variables": {"n": 0, "limit": 10}})
        result = gen.evaluate_path_feasibility([], context, negative_transition_id="NB")
        assert result.status is Feasibility.UNKNOWN and result.reason_code == "NEGATIVE_INPUT_NOT_PROVIDED"

    def test_negative_with_infeasible_prefix_is_infeasible(self, gen, ctx):
        assert gen.evaluate_path_feasibility(["B1"], ctx, negative_transition_id="NE").status is Feasibility.INFEASIBLE

    def test_explore_finds_only_proven_paths(self, gen, ctx):
        explored = gen.explore_feasible(ctx)
        assert explored["state_paths"]["S3"] == ["A", "B1", "C", "E"]
        assert "U" not in explored["edge_paths"] and "S9" not in explored["state_paths"]
        for tids in list(explored["edge_paths"].values()) + list(explored["state_paths"].values()):
            assert gen.evaluate_path_feasibility(tids, ctx).status is Feasibility.FEASIBLE

    def test_explore_respects_limits(self, gen, ctx):
        assert gen.explore_feasible(ctx, max_depth=1)["state_paths"].keys() == {"S0"}
        assert gen.explore_feasible(ctx, max_nodes=2)["truncated"] is True

    def test_bfs_without_context_never_claims_feasible(self, gen):
        assert {p.feasibility.status for p in gen.generate_bfs_paths()} == {Feasibility.UNKNOWN}
        assert {p.feasibility.status for p in gen.generate_negative_paths()} == {Feasibility.UNKNOWN}

    def test_required_context_is_reported_without_context(self, gen):
        path = next(p for p in gen.generate_bfs_paths(prefix_mode="structural") if p.path_key == "B1>C>E")
        assert path.required_context["initial_variables"] == ["n"]  # B1 cần n; status do B1 gán nên C không cần
        assert path.required_context["step_inputs"] == {}
        with_input = gen._make_positive(["A", "B1"], "BFS", None)
        assert with_input.required_context["initial_variables"] == ["limit", "n"]
        assert with_input.required_context["step_inputs"] == {"1": ["amount"]}  # amount là đầu vào của bước 1

    def test_context_validation(self, tmp_path):
        with pytest.raises(ContextError, match="unknown"):
            PathContext.from_dict({"initial": {}})
        with pytest.raises(ContextError):
            PathContext.from_dict({"step_inputs": {"A": 5}})
        with pytest.raises(ContextError):
            PathContext.from_dict([])
        with pytest.raises(ContextError, match="not found"):
            PathContext.from_file(tmp_path / "missing.json")
        bad = tmp_path / "bad.json"
        bad.write_text("{", encoding="utf-8")
        with pytest.raises(ContextError, match="valid JSON"):
            PathContext.from_file(bad)

    def test_context_fingerprint_changes_with_content(self):
        a = PathContext.from_dict({"initial_variables": {"n": 1}})
        assert a.fingerprint() == PathContext.from_dict({"initial_variables": {"n": 1}}).fingerprint()
        assert a.fingerprint() != PathContext.from_dict({"initial_variables": {"n": 2}}).fingerprint()


# ==========================================================================
# Nhóm F - Coverage
# ==========================================================================
class TestCoverage:
    def test_state_and_transition_coverage_numbers(self, gen):
        paths = [gen._make_positive(["B1", "C"], "BFS", None)]
        cov = gen.calculate_coverage(paths)
        assert cov["states"]["total"] == 5 and cov["states"]["reachable"] == 4
        assert cov["states"]["covered_structural"] == 3 and cov["states"]["uncovered_structural"] == ["S3"]
        assert cov["positive_transitions"]["total"] == 8
        assert cov["positive_transitions"]["covered_structural"] == 2
        assert cov["positive_transitions"]["structural_ratio"] == round(2 / 7, 4)  # 7 transition đạt tới được
        assert set(cov["positive_transitions"]["uncovered_structural"]) == {"A", "B2", "D", "E", "F", "U"}

    def test_full_coverage_only_when_everything_is_covered(self, gen):
        paths = gen.generate_bfs_paths() + gen.generate_dfs_paths(max_paths=1000)
        cov = gen.calculate_coverage(paths)
        assert cov["states"]["structural_ratio"] == 1.0
        assert cov["positive_transitions"]["structural_ratio"] == 1.0
        assert cov["positive_transitions"]["uncovered_structural"] == ["U"]  # U ở state không đạt tới: không bao giờ phủ được
        assert cov["positive_transitions"]["covered_structural"] < cov["positive_transitions"]["total"]

    def test_unknown_is_never_counted_as_feasible(self, gen):
        cov = gen.calculate_coverage(gen.generate_bfs_paths())
        assert cov["states"]["covered_feasible"] == 0 and cov["positive_transitions"]["covered_feasible"] == 0
        assert cov["paths"]["by_feasibility"]["positive"] == {"UNKNOWN": len(gen.generate_bfs_paths())}

    def test_feasible_coverage_counts_only_feasible_paths(self, gen, ctx):
        paths = [gen._make_positive(["A", "B1", "C"], "BFS", ctx), gen._make_positive(["B1"], "BFS", ctx)]
        assert [p.feasibility.status for p in paths] == [Feasibility.FEASIBLE, Feasibility.INFEASIBLE]
        cov = gen.calculate_coverage(paths)
        assert cov["positive_transitions"]["covered_structural"] == 3
        assert cov["positive_transitions"]["covered_feasible"] == 3
        assert cov["states"]["covered_feasible"] == 3
        only_infeasible = gen.calculate_coverage([paths[1]])
        assert only_infeasible["positive_transitions"]["covered_feasible"] == 0

    def test_negative_transition_coverage(self, gen, ctx):
        cov = gen.calculate_coverage(gen.generate_negative_paths(ctx))
        negatives = cov["negative_transitions"]
        assert negatives["total"] == 5 and negatives["with_generated_path"] == 4
        assert negatives["without_path"] == ["ND"] and negatives["structural_ratio"] == 0.8
        assert negatives["with_feasible_path"] == 4 and negatives["feasible_ratio"] == 0.8

    def test_negative_without_context_has_no_feasible_coverage(self, gen):
        cov = gen.calculate_coverage(gen.generate_negative_paths())
        assert cov["negative_transitions"]["with_feasible_path"] == 0

    def test_negative_prefix_contributes_structural_coverage(self, gen):
        cov = gen.calculate_coverage(gen.generate_negative_paths(prefix_mode="structural"))
        assert cov["positive_transitions"]["covered_structural"] >= 3  # B1, C, E nằm trong prefix

    def test_transition_pair_coverage(self, gen):
        cov = gen.calculate_coverage([gen._make_positive(["B1", "C", "E"], "BFS", None)])
        assert cov["transition_pairs"]["covered_structural"] == 2
        assert "C>D" in cov["transition_pairs"]["uncovered_structural"]
        assert cov["transition_pairs"]["possible"] > 2

    def test_executed_coverage_is_never_claimed(self, gen, ctx):
        cov = gen.calculate_coverage(gen.generate_bfs_paths(ctx) + gen.generate_negative_paths(ctx))
        assert cov["executed_coverage"]["status"] == "NOT_MEASURED"
        rules = cov["security_rules"]
        assert rules["executed_on_backend"]["status"] == "NOT_MEASURED"
        assert rules["verified_by_oracle"]["status"] == "NOT_MEASURED"
        assert rules["with_generated_test_data"]["status"] == "NOT_MEASURED"
        assert rules["level"] == "PATH_COVERAGE_ONLY"

    def test_rule_coverage_levels(self, gen, ctx):
        cov = gen.calculate_coverage(gen.generate_bfs_paths(ctx) + gen.generate_negative_paths(ctx))
        rules = cov["security_rules"]
        assert rules["implemented_total"] == 5
        assert set(rules["linked_to_paths"]) == {"R1", "R2", "R3", "R4"}
        assert rules["not_linked"] == ["R5"]
        assert rules["excluded_not_applicable"] == ["R6"]  # R6 không được coi là đã bao phủ
        assert "R5" not in rules["with_feasibility_witness"]
        assert set(rules["with_feasibility_witness"]) <= set(rules["linked_to_paths"])

    def test_witness_rules_require_feasible_paths(self, gen):
        cov = gen.calculate_coverage(gen.generate_bfs_paths())
        assert cov["security_rules"]["with_feasibility_witness"] == []

    def test_coverage_without_rules_file(self, tmp_path):
        generator = make_generator(tmp_path, rules=False)
        cov = generator.calculate_coverage(generator.generate_bfs_paths())
        assert cov["security_rules"]["rules_file_loaded"] is False


# ==========================================================================
# Nhóm G - Output và CLI
# ==========================================================================
class TestOutput:
    def build(self, gen, ctx=None, **kwargs):
        result = gen.generate(strategy="both", include_negative=True, context=ctx, **kwargs)
        return gen.to_output_dict(result), result

    def test_output_is_valid_json_with_required_fields(self, gen, ctx, tmp_path):
        data, _ = self.build(gen, ctx)
        out = TestPathGenerator.export_json(data, tmp_path / "out" / "paths.json")
        loaded = json.loads(out.read_text(encoding="utf-8"))
        assert loaded["generator"] == "TestPathGenerator" and loaded["schema_version"] == "1.0"
        for path in loaded["paths"]:
            for key in ("path_id", "path_type", "strategy", "transition_ids", "states", "steps", "feasibility",
                        "security_rule_ids", "expected_outcome", "path_key", "structure", "execution"):
                assert key in path, key
            assert path["structure"] == "STRUCTURALLY_VALID" and path["execution"] == {"status": "NOT_EXECUTED"}

    def test_path_ids_and_keys_are_unique(self, gen, ctx):
        data, _ = self.build(gen, ctx)
        ids = [p["path_id"] for p in data["paths"]]
        assert len(ids) == len(set(ids))
        assert len({p["path_key"] for p in data["paths"]}) == len(ids)

    def test_every_transition_id_exists(self, gen, ctx):
        data, _ = self.build(gen, ctx)
        known = {t["id"] for t in fixture_model()["transitions"]}
        for path in data["paths"]:
            assert set(path["transition_ids"]) <= known
            assert set(path["transition_ids"]) <= set(data["transition_catalog"])

    def test_state_sequence_matches_transitions(self, gen, ctx):
        data, _ = self.build(gen, ctx)
        by_id = {t["id"]: t for t in fixture_model()["transitions"]}
        for path in data["paths"]:
            assert len(path["states"]) == len(path["transition_ids"]) + 1 and path["states"][0] == "S0"
            for index, tid in enumerate(path["transition_ids"]):
                assert (path["states"][index], path["states"][index + 1]) == (by_id[tid]["source"], by_id[tid]["target"])
            positive_steps = [s for s in path["steps"] if s["kind"] == "transition"]
            assert [s["transition_id"] for s in positive_steps] == path["transition_ids"]
            assert [s["step_index"] for s in path["steps"]] == list(range(1, len(path["steps"]) + 1))
            if path["path_type"] == "negative":
                assert path["steps"][-1]["from_state"] == path["states"][-1]

    def test_negative_metadata_in_output(self, gen, ctx):
        data, _ = self.build(gen, ctx)
        negatives = [p for p in data["paths"] if p["path_type"] == "negative"]
        assert negatives
        for path in negatives:
            assert path["negative_transition_id"] in data["negative_catalog"]
            assert path["expected_rejection"]["rejected"] is True
            assert path["negative_category"] in path["negative_categories"]

    def test_dedup_merges_strategies(self, gen):
        data, _ = self.build(gen)
        merged = [p for p in data["paths"] if len(p["strategies"]) > 1]
        assert merged and all(p["strategy"] == "BFS" and p["strategies"] == ["BFS", "DFS"] for p in merged)

    def test_output_is_byte_stable(self, tmp_path, ctx):
        first = json.dumps(self.build(make_generator(tmp_path), ctx)[0], sort_keys=False, default=str)
        second = json.dumps(self.build(make_generator(tmp_path), PathContext.from_dict(FIXTURE_CONTEXT))[0], sort_keys=False, default=str)
        assert first == second

    def test_no_timestamps_in_output(self, gen):
        text = json.dumps(self.build(gen)[0])
        assert "generated_at" not in text and "timestamp" not in text

    def test_catalog_contains_guard_and_variables_for_data_generation(self, gen, ctx):
        data, _ = self.build(gen, ctx)
        entry = data["transition_catalog"]["A"]
        assert entry["guard"]["all"][0]["var"] == "amount" and entry["required_inputs"] == ["amount", "limit", "n"]
        assert data["rules_index"]["R1"]["severity"] == "HIGH"
        step = next(p for p in data["paths"] if p["path_key"].startswith("A"))["steps"][0]
        assert step["required_inputs"] == ["amount", "limit", "n"] and step["expected_outcome"] == "ACCEPT"

    def test_assumptions_are_stated_once_at_file_level(self, gen, ctx):
        data, _ = self.build(gen, ctx)
        assert any("NOT touched" in a for a in data["feasibility_assumptions"])
        assert any("GIVEN_CONTEXT" in a for a in data["feasibility_assumptions"])
        assert all("assumptions" not in p["feasibility_detail"] for p in data["paths"])

    def test_steps_reference_the_catalog_instead_of_copying_it(self, gen, ctx):
        data, _ = self.build(gen, ctx)
        for path in data["paths"]:
            for step in path["steps"]:
                assert "guard" not in step and "unsimulated_effects" not in step
                if step["kind"] == "transition":
                    assert step["transition_id"] in data["transition_catalog"]
                    assert {"event", "from_state", "to_state", "expected_outcome", "required_inputs"} <= set(step)

    def test_summary_counts_match_paths(self, gen, ctx):
        data, result = self.build(gen, ctx)
        assert data["summary"]["total_paths"] == len(data["paths"])
        assert sum(data["summary"]["paths_by_type"].values()) == len(data["paths"])
        flat = sum(sum(v.values()) for v in data["summary"]["paths_by_feasibility"].values())
        assert flat == len(data["paths"])

    def test_warnings_report_unreachable_state_and_skipped_negative(self, gen):
        _, result = self.build(gen)
        assert any("S9" in w for w in result.warnings) and any("ND skipped" in w for w in result.warnings)

    def test_unknown_rule_reference_is_warned_not_invented(self, tmp_path):
        model = fixture_model()
        model["transitions"][0]["rule_refs"] = ["R1", "R99"]
        _, result = self.build(make_generator(tmp_path, model))
        assert any("R99" in w for w in result.warnings)

    def test_limits_hit_are_reported(self, gen):
        result = gen.generate(strategy="dfs", max_paths=3)
        assert result.summary["limits_hit"]["dfs_max_paths"] is True

    def test_generate_rejects_bad_strategy(self, gen):
        with pytest.raises(ValueError):
            gen.generate(strategy="random")

    def test_cli_writes_paths_and_coverage(self, tmp_path, capsys):
        model = write(tmp_path, "model.json", fixture_model())
        write(tmp_path, "security_rules.json", FIXTURE_RULES)
        context = write(tmp_path, "ctx.json", FIXTURE_CONTEXT)
        out = tmp_path / "result" / "test_paths.json"
        code = main(["--model", str(model), "--strategy", "both", "--max-depth", "6", "--max-paths", "50",
                     "--include-negative", "--context", str(context), "--output", str(out)])
        assert code == 0
        assert json.loads(out.read_text(encoding="utf-8"))["paths"]
        coverage = json.loads((tmp_path / "result" / "test_path_coverage.json").read_text(encoding="utf-8"))
        assert coverage["executed_coverage"]["status"] == "NOT_MEASURED"
        assert "paths=" in capsys.readouterr().out

    def test_cli_reports_errors_without_traceback(self, tmp_path, capsys):
        assert main(["--model", str(tmp_path / "nope.json"), "--output", str(tmp_path / "o.json")]) == 2
        assert "ERROR" in capsys.readouterr().err
        model = write(tmp_path, "model.json", fixture_model())
        assert main(["--model", str(model), "--context", str(tmp_path / "nope.json"), "--output", str(tmp_path / "o.json")]) == 2

    def test_cli_default_has_no_negative_paths(self, tmp_path):
        model = write(tmp_path, "model.json", fixture_model())
        out = tmp_path / "o.json"
        assert main(["--model", str(model), "--output", str(out)]) == 0
        assert all(p["path_type"] == "positive" for p in json.loads(out.read_text(encoding="utf-8"))["paths"])


# ==========================================================================
# Integration - efsm_model.json thật của BlueMoon
# ==========================================================================
@pytest.fixture(scope="module")
def bluemoon() -> TestPathGenerator:
    return TestPathGenerator(BLUEMOON_MODEL, BLUEMOON_RULES)


@pytest.fixture(scope="module")
def bluemoon_ctx() -> PathContext:
    return PathContext.from_file(BLUEMOON_CONTEXT)


class TestBlueMoonIntegration:
    def test_graph_matches_model(self, bluemoon):
        graph = bluemoon.graph
        assert graph.initial_state == "CART" and graph.unreachable_states() == []
        assert graph.dead_ends() == ["PAID", "CANCELLED"]
        assert {e.transition_id for e in graph.self_loops()} == {"T01", "T02", "T03", "T10", "T07"}
        assert graph.parallel_edges()[("CART", "CART")] == ["T01", "T02", "T03", "T10"]
        assert {e.transition_id for e in graph.in_edges("CANCELLED")} == {"T08", "T09"}

    def test_structural_shortest_paths_skip_setup_steps(self, bluemoon):
        """Cấu trúc đúng nhưng không thực thi được: T04 cần giỏ không rỗng (do T01 tạo)."""
        assert [e.transition_id for e in bluemoon.graph.shortest_path("PAID")] == ["T04", "T05", "T06"]
        assert bluemoon.evaluate_path_feasibility(["T04", "T05", "T06"], None).status is Feasibility.UNKNOWN

    def test_structural_path_is_infeasible_from_an_empty_cart(self, bluemoon, bluemoon_ctx):
        result = bluemoon.evaluate_path_feasibility(["T04", "T05", "T06"], bluemoon_ctx)
        assert result.status is Feasibility.INFEASIBLE
        assert result.steps[0]["failed_conditions"][0]["condition"] == "cart_items_count > 0"

    def test_feasible_search_adds_the_setup_step(self, bluemoon, bluemoon_ctx):
        explored = bluemoon.explore_feasible(bluemoon_ctx)
        assert explored["state_paths"]["PAID"] == ["T01", "T04", "T05", "T06"]
        assert explored["state_paths"]["CANCELLED"] == ["T01", "T04", "T08"]
        assert set(explored["edge_paths"]) == {t["id"] for t in json.loads(BLUEMOON_MODEL.read_text(encoding="utf-8"))["transitions"]}

    def test_bfs_with_context_covers_all_transitions_feasibly(self, bluemoon, bluemoon_ctx):
        paths = bluemoon.generate_bfs_paths(bluemoon_ctx)
        assert {p.feasibility.status for p in paths} == {Feasibility.FEASIBLE}
        cov = bluemoon.calculate_coverage(paths)
        assert cov["positive_transitions"]["covered_feasible"] == cov["positive_transitions"]["total"] == 10
        assert cov["states"]["feasible_ratio"] == 1.0

    def test_bfs_without_context_is_unknown_only(self, bluemoon):
        assert {p.feasibility.status for p in bluemoon.generate_bfs_paths()} == {Feasibility.UNKNOWN}

    def test_all_negative_transitions_get_a_path(self, bluemoon, bluemoon_ctx):
        paths = bluemoon.generate_negative_paths(bluemoon_ctx)
        assert len(paths) == 20
        assert {p.extra["negative_transition_id"] for p in paths} == set(bluemoon.evaluator.negative_transition_ids)
        for path in paths:
            assert set(path.transition_ids).isdisjoint(bluemoon._negatives)
            assert path.feasibility.status is Feasibility.FEASIBLE, (path.path_key, path.feasibility.reason)

    def test_negative_prefix_examples(self, bluemoon, bluemoon_ctx):
        by_id = {p.extra["negative_transition_id"]: p for p in bluemoon.generate_negative_paths(bluemoon_ctx)}
        assert by_id["NT08"].transition_ids == ["T01", "T04", "T05", "T06"] and by_id["NT08"].states[-1] == "PAID"
        assert by_id["NT01"].transition_ids == [] and by_id["NT01"].path_key == "!NT01"
        assert by_id["NT08"].extra["negative_categories"] == ["INVALID_STATE_TRANSITION"]
        assert by_id["NT08"].extra["expected_rejection"]["http_status"] == 409
        assert by_id["NT01"].extra["negative_category"] == "INPUT_VALIDATION_VIOLATION"
        assert by_id["NT02"].extra["negative_categories"] == ["BUSINESS_CONSTRAINT_VIOLATION", "GUARD_VIOLATION"]
        assert by_id["NT08"].security_rule_ids == ["BSR-CANCEL-01", "BSR-CANCEL-02"] and by_id["NT08"].severity == "HIGH"
        assert by_id["NT04"].extra["api_reachable"] is False

    def test_negative_paths_forbid_the_base_transition_effects(self, bluemoon, bluemoon_ctx):
        nt03 = next(p for p in bluemoon.generate_negative_paths(bluemoon_ctx) if p.extra["negative_transition_id"] == "NT03")
        assert any("products.stock" in e for e in nt03.extra["expected_rejection"]["forbidden_backend_effects"])
        violated = nt03.extra["expected_rejection"]["violated_transition_guards"][0]
        assert violated["transition_id"] == "T04" and violated["failed_conditions"][0]["condition"] == "cart_items_count > 0"

    def test_dfs_on_real_model_is_bounded_and_valid(self, bluemoon, bluemoon_ctx):
        paths = bluemoon.generate_dfs_paths(bluemoon_ctx, max_depth=8, max_paths=200)
        assert 0 < len(paths) <= 200 and max(len(p.transition_ids) for p in paths) <= 8
        assert {p.feasibility.status for p in paths} <= {Feasibility.FEASIBLE, Feasibility.INFEASIBLE}

    def test_full_generation_and_rule_linking(self, bluemoon, bluemoon_ctx):
        result = bluemoon.generate(strategy="both", include_negative=True, context=bluemoon_ctx)
        cov = bluemoon.calculate_coverage(result.paths)
        assert cov["negative_transitions"]["total"] == cov["negative_transitions"]["with_generated_path"] == 20
        rules = cov["security_rules"]
        assert rules["not_linked"] == [] and rules["unknown_rule_ids_in_paths"] == []
        assert set(rules["excluded_not_applicable"]) == {"BSR-AUTH-01", "BSR-CANCEL-COMPLETED", "BSR-PAYMENT-EXTERNAL"}
        assert cov["executed_coverage"]["status"] == "NOT_MEASURED"
        assert result.warnings == []

    def test_real_output_is_stable(self, bluemoon_ctx):
        def run():
            generator = TestPathGenerator(BLUEMOON_MODEL, BLUEMOON_RULES)
            return json.dumps(generator.to_output_dict(generator.generate(strategy="both", include_negative=True, context=bluemoon_ctx)), default=str)
        assert run() == run()

    def test_feasibility_uses_the_current_guard_evaluator_interface(self, bluemoon, bluemoon_ctx):
        assert isinstance(bluemoon.evaluator, GuardEvaluator)
        result = bluemoon.evaluate_path_feasibility(["T01", "T04", "T05", "T06"], bluemoon_ctx)
        assert result.final_state == "PAID" and result.final_variables["payment_count"] == 1
        assert result.final_variables["order_total"] == 300000

    def test_invariants_are_checked_along_the_path(self, bluemoon, bluemoon_ctx):
        tampered = copy.deepcopy(bluemoon_ctx)
        tampered.step_inputs["T04"]["order_items"] = [{"quantity": 2, "unit_price": 1}]  # vẫn khớp INV-03 vì order_total tính từ order_items
        assert bluemoon.evaluate_path_feasibility(["T01", "T04"], tampered).status is Feasibility.FEASIBLE
        tampered.initial_variables["stock"] = 1  # đặt 2 > tồn kho 1: guard ordered_quantity <= stock sai
        assert bluemoon.evaluate_path_feasibility(["T01", "T04"], tampered).status is Feasibility.INFEASIBLE
