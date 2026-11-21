"""
efsm_feasibility.py - Chứng minh một đường đi EFSM là KHÔNG KHẢ THI cho MỌI dữ liệu (diễn giải trừu tượng).

Vấn đề cần giải quyết
---------------------
Bộ sinh dữ liệu (``test_data_generator.PathDataGenerator``) chỉ tìm kiếm trong một không gian hữu hạn
(catalog seed, tối đa N bước setup, một số lựa chọn đầu vào). Không tìm thấy nghiệm trong không gian đó
(``SEARCH_EXHAUSTED``) KHÔNG phải là chứng minh path vô nghiệm. Module này bổ sung một chứng minh độc lập:

* Mỗi biến EFSM mang giá trị trừu tượng ``CONST(v)`` hoặc ``TOP`` (chưa biết / có thể là bất kỳ giá trị nào).
* Trạng thái ban đầu: MỌI biến là ``TOP``  -> kết luận đúng với mọi database ban đầu và mọi prefix setup
  được chèn TRƯỚC path.
* Biến ``origin == "request"``: luôn ``TOP`` ở mỗi bước -> đúng với mọi dữ liệu đầu vào.
* Bước có chọn sản phẩm (``selection[tid] != "none"``): các biến gắn với một dòng hàng (``line_variables``)
  trở về ``TOP`` vì test có thể chọn một dòng/sản phẩm khác -> đúng với mọi lựa chọn sản phẩm.
* Action ``assign`` không điều kiện với giá trị/biểu thức tính được từ hằng số -> ``CONST``;
  ``assign`` có ``when`` chưa quyết định được -> ``TOP``; biểu thức chứa ``TOP`` -> ``TOP``.
* Guard được đánh giá ba giá trị (đúng / sai / chưa biết). Chỉ khi một guard CHẮC CHẮN sai
  (một điều kiện con của ``all`` sai trên các biến đều là ``CONST``) mới kết luận ``PROVEN_INFEASIBLE``.

Tính đúng đắn (soundness): phép trừu tượng chỉ làm MẤT thông tin (``TOP``), không bao giờ bịa thông tin,
nên mọi kết luận ``PROVEN_INFEASIBLE`` đúng với mọi lần thực thi cụ thể có thể có. Ngược lại, "không chứng
minh được" KHÔNG có nghĩa là khả thi. Invariant không được dùng để chứng minh (chỉ guard) - càng an toàn hơn.

Module chỉ đọc mô hình; không sửa guard/action, không gọi HTTP, không chạm database.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from guard_evaluator import GuardEvaluator, GuardEvaluatorError, GuardStatus

__all__ = [
    "FeasibilityVerdict",
    "ProofResult",
    "EfsmPathProver",
    "PROOF_SCOPE",
    "render_feasibility_markdown",
]

PROOF_SCOPE = ("every initial database state, every setup prefix placed before the path, every product choice "
               "and every request input (EFSM abstract interpretation over guards and assign actions)")


class FeasibilityVerdict(StrEnum):
    """Kết quả phân loại tính khả thi của một đường đi."""

    FEASIBLE = "FEASIBLE"                    # đã tìm được dữ liệu + chuỗi thực thi thỏa mô hình
    PROVEN_INFEASIBLE = "PROVEN_INFEASIBLE"  # có chứng cứ mâu thuẫn logic (module này)
    SEARCH_EXHAUSTED = "SEARCH_EXHAUSTED"    # chưa tìm được nghiệm trong giới hạn; KHÔNG phải chứng minh
    GENERATOR_ERROR = "GENERATOR_ERROR"      # bộ sinh gặp lỗi xử lý / giới hạn khả năng
    MODEL_ERROR = "MODEL_ERROR"              # mô hình / mapping thiếu hoặc không nhất quán


@dataclass
class ProofResult:
    """Kết quả chứng minh cho một chuỗi transition."""

    proven_infeasible: bool
    kind: str | None = None                  # GUARD_CONTRADICTION | EMPTY_CART_SELECTION | STATE_MISMATCH
    failed_step: int | None = None
    transition_id: str | None = None
    guard: Any = None
    failed_conditions: list[Any] = field(default_factory=list)
    context_before: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        if not self.proven_infeasible:
            return {"status": "NOT_PROVEN_INFEASIBLE",
                    "reason": self.reason or "no guard is certainly false under the abstraction; this is NOT a proof of feasibility"}
        return {
            "status": FeasibilityVerdict.PROVEN_INFEASIBLE.value,
            "kind": self.kind,
            "failed_step": self.failed_step,
            "transition_id": self.transition_id,
            "guard": self.guard,
            "failed_conditions": self.failed_conditions,
            "context_before": self.context_before,
            "caused_by": self.provenance,
            "reason": self.reason,
            "proof_scope": PROOF_SCOPE,
        }


class EfsmPathProver:
    """Diễn giải trừu tượng (CONST | TOP) trên mô hình EFSM để chứng minh path vô nghiệm.

    ``selection``: transition ID -> ``"none" | "catalog" | "cart"`` (cách bước đó chọn sản phẩm,
    lấy từ ``api_test_mapping.json``). Transition không có trong ``selection`` được coi là CÓ chọn
    sản phẩm (``catalog``) - lựa chọn an toàn nhất.
    ``line_variables``: biến EFSM mô tả MỘT dòng hàng / sản phẩm đang được chọn.
    ``cart_count_variable``: biến đếm số dòng giỏ (dùng cho quy tắc "chọn dòng trong giỏ rỗng").
    """

    def __init__(
        self,
        evaluator: GuardEvaluator,
        *,
        selection: Mapping[str, str] | None = None,
        line_variables: Iterable[str] = (),
        cart_count_variable: str | None = "cart_items_count",
    ) -> None:
        self.ev = evaluator
        self.model = evaluator.model
        self._tr = {t["id"]: t for t in self.model["transitions"]}
        self._origin = {v["name"]: v.get("origin") for v in self.model["variables"]}
        self.selection = dict(selection or {})
        self.line_variables = frozenset(line_variables)
        self.cart_count_variable = cart_count_variable if cart_count_variable in self._origin else None

    @classmethod
    def from_api_mapping(cls, evaluator: GuardEvaluator, mapping_data: Mapping[str, Any],
                         line_variables: Iterable[str]) -> "EfsmPathProver":
        selection = {tid: str(spec.get("select_product", "catalog"))
                     for tid, spec in (mapping_data.get("transitions") or {}).items()}
        return cls(evaluator, selection=selection, line_variables=line_variables)

    # ------------------------------------------------------------------
    # Đánh giá ba giá trị
    # ------------------------------------------------------------------
    def _leaf(self, leaf: Any, known: Mapping[str, Any]) -> bool | None:
        try:
            names = set(self.ev.get_guard_variables(leaf))
        except GuardEvaluatorError:
            return None
        if not names <= set(known):
            return None
        result = self.ev.evaluate_guard(leaf, dict(known))
        if result.status is GuardStatus.GUARD_SATISFIED:
            return True
        if result.status is GuardStatus.GUARD_NOT_SATISFIED:
            return False
        return None  # lỗi kiểu / biểu thức không hỗ trợ: không kết luận

    def evaluate(self, guard: Any, known: Mapping[str, Any], failed: list[Any] | None = None) -> bool | None:
        """True / False / None (chưa biết). ``failed`` nhận các điều kiện con chắc chắn sai."""
        failed = failed if failed is not None else []
        if guard is None:
            return True
        if isinstance(guard, Mapping) and "all" in guard:
            values = [self.evaluate(g, known, failed) for g in guard["all"]]
            if False in values:
                return False
            return True if all(v is True for v in values) else None
        if isinstance(guard, Mapping) and "any" in guard:
            sub: list[Any] = []
            values = [self.evaluate(g, known, sub) for g in guard["any"]]
            if values and all(v is False for v in values):
                failed.extend(sub)
                return False
            return True if True in values else None
        if isinstance(guard, Mapping) and "not" in guard:
            inner = self.evaluate(guard["not"], known, [])
            if inner is True:
                failed.append(dict(guard))
            return None if inner is None else not inner
        value = self._leaf(guard, known)
        if value is False:
            failed.append(copy.deepcopy(guard))
        return value

    # ------------------------------------------------------------------
    # Một bước trừu tượng
    # ------------------------------------------------------------------
    def step(
        self, known: Mapping[str, Any], prov: Mapping[str, dict[str, Any]], tid: str, index: int
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], ProofResult | None]:
        """Áp dụng ``tid`` lên trạng thái trừu tượng. Trả về (biến đã biết, nguồn gốc, chứng minh nếu vô nghiệm).

        Không bao giờ sửa ``known``/``prov`` đầu vào (mỗi nhánh tìm kiếm có bản sao riêng).
        """
        tr = self._tr[tid]
        cur = {k: v for k, v in known.items() if self._origin.get(k) != "request"}
        src = {k: dict(v) for k, v in prov.items() if k in cur}
        mode = self.selection.get(tid, "catalog")
        count = self.cart_count_variable
        if mode == "cart" and count is not None and cur.get(count) == 0:
            return cur, src, ProofResult(
                True, "EMPTY_CART_SELECTION", index, tid, tr.get("guard"),
                [{"implicit_precondition": f"{tid} must select an existing cart line ({count} > 0)"}],
                dict(cur), {count: src[count]} if count in src else {},
                f"step {index} ({tid}): the transition acts on a cart line but {count} is certainly 0"
                + (f" (set by {src[count]['transition_id']} at step {src[count]['step']})" if count in src else ""),
            )  # fmt: skip
        if mode != "none":
            for name in self.line_variables:
                cur.pop(name, None)
                src.pop(name, None)
        failed: list[Any] = []
        if self.evaluate(tr.get("guard"), cur, failed) is False:
            used: set[str] = set()
            for cond in failed:
                try:
                    used |= set(self.ev.get_guard_variables(cond))
                except GuardEvaluatorError:
                    pass
            causes = {k: src[k] for k in sorted(used) if k in src}
            texts = "; ".join(self._describe(c, cur) for c in failed)
            why = ", ".join(f"{k} set by {v['transition_id']} at step {v['step']}" for k, v in causes.items())
            return cur, src, ProofResult(
                True, "GUARD_CONTRADICTION", index, tid, tr.get("guard"), failed, dict(cur), causes,
                f"step {index} ({tid}): guard is certainly false: {texts}" + (f" [{why}]" if why else ""),
            )  # fmt: skip
        new, new_src = dict(cur), dict(src)
        for action in tr.get("actions", []):
            if not isinstance(action, Mapping) or action.get("type") != "assign":
                continue
            var = action["var"]
            if action.get("when") is not None:
                holds = self.evaluate(action["when"], new)
                if holds is False:
                    continue
                if holds is None:  # có thể chạy hoặc không: mất thông tin
                    new.pop(var, None)
                    new_src.pop(var, None)
                    continue
            trial = copy.deepcopy(new)
            try:
                self.ev._execute_assign(self.ev._normalize_action(dict(action)), trial)  # noqa: SLF001 - tái sử dụng engine của evaluator
            except GuardEvaluatorError:
                new.pop(var, None)
                new_src.pop(var, None)
                continue
            if var in trial:
                new[var] = trial[var]
                new_src[var] = {"step": index, "transition_id": tid}
            else:
                new.pop(var, None)
                new_src.pop(var, None)
        return new, new_src, None

    def _describe(self, cond: Any, known: Mapping[str, Any]) -> str:
        if isinstance(cond, Mapping) and "var" in cond:
            rhs = cond.get("value_var") if "value_var" in cond else cond.get("value")
            return f"{cond['var']} {cond.get('op')} {rhs!r} with {cond['var']}={known.get(cond['var'])!r}"
        if isinstance(cond, Mapping) and "expr" in cond:
            return str(cond["expr"])
        return str(cond)

    # ------------------------------------------------------------------
    # Cả đường đi
    # ------------------------------------------------------------------
    def prove(self, transition_ids: Sequence[str]) -> ProofResult:
        """Chứng minh chuỗi transition (bắt đầu tại initial state) vô nghiệm, nếu được."""
        state = self.model["initial_state"]
        known: dict[str, Any] = {}
        prov: dict[str, dict[str, Any]] = {}
        for index, tid in enumerate(transition_ids, start=1):
            tr = self._tr.get(tid)
            if tr is None:
                return ProofResult(True, "UNKNOWN_TRANSITION", index, tid, None, [], dict(known), {},
                                   f"step {index}: transition {tid} is not in the model")
            if tr["source"] != state:
                return ProofResult(True, "STATE_MISMATCH", index, tid, tr.get("guard"), [], dict(known), {},
                                   f"step {index} ({tid}): starts at {tr['source']} but the path is at {state}")
            known, prov, proof = self.step(known, prov, tid, index)
            if proof is not None:
                return proof
            state = tr["target"]
        return ProofResult(False)


# --------------------------------------------------------------------------
# Báo cáo Markdown
# --------------------------------------------------------------------------
_SUGGESTION = {
    FeasibilityVerdict.FEASIBLE: "Không cần xử lý.",
    FeasibilityVerdict.PROVEN_INFEASIBLE: "Giữ ở trạng thái SKIPPED; không sửa guard. Nếu cần kiểm tra hành vi từ chối, dùng negative test tương ứng (ví dụ NT03 cho giỏ rỗng).",
    FeasibilityVerdict.SEARCH_EXHAUSTED: "Tăng --max-setup-steps / --max-path-nodes hoặc mở rộng catalog seed rồi chạy lại; chưa được coi là vô nghiệm.",
    FeasibilityVerdict.GENERATOR_ERROR: "Kiểm tra bộ sinh (mô phỏng ShadowState / solver) - không phải lỗi của path.",
    FeasibilityVerdict.MODEL_ERROR: "Bổ sung mapping / sửa mô hình cho transition liên quan.",
}


def suggestion_for(verdict: str) -> str:
    try:
        return _SUGGESTION[FeasibilityVerdict(verdict)]
    except ValueError:
        return ""


def render_feasibility_markdown(index: Sequence[Mapping[str, Any]], summary: Mapping[str, Any]) -> str:
    """Bảng Markdown cho ``feasibility_index`` của test_cases.json."""
    lines = ["# Báo cáo tính khả thi đường đi EFSM", "",
             "| Kết quả | Số path |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary.get("by_verdict", {}).items()]
    lines += ["", f"Phạm vi chứng minh PROVEN_INFEASIBLE: {PROOF_SCOPE}.", "",
              "| Path | Loại | Chuỗi transition | Kết quả | Bước lỗi đầu tiên | Guard / điều kiện | Context trước bước | Nguyên nhân | Chứng cứ / giới hạn | Đề xuất |",
              "|---|---|---|---|---|---|---|---|---|---|"]

    def cell(value: Any) -> str:
        text = value if isinstance(value, str) else ("" if value is None else str(value))
        return text.replace("|", "\\|").replace("\n", " ")

    for row in index:
        rep = row.get("feasibility_report") or {}
        step = f"{rep.get('failed_step')} ({rep.get('transition_id')})" if rep.get("failed_step") else ""
        lines.append("| " + " | ".join(cell(x) for x in (
            row.get("source_path_id"), row.get("type"), row.get("path_key"), row.get("feasibility_verdict"), step,
            rep.get("failed_conditions"), rep.get("context_before"), rep.get("reason"),
            rep.get("evidence") or rep.get("proof_scope"), rep.get("suggestion"))) + " |")
    return "\n".join(lines) + "\n"
