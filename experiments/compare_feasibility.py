"""So sánh kết quả pipeline trước/sau cải thiện bộ phân loại tính khả thi (chỉ đọc các file JSON).

    python experiments/compare_feasibility.py --before experiments/before_fix --after experiments/after_fix \
        [--prune experiments/after_fix_prune] --output experiments/comparison.md
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def load(folder: Path, name: str) -> dict:
    return json.loads((folder / name).read_text(encoding="utf-8"))


def metrics(folder: Path) -> dict:
    paths, cases = load(folder, "test_paths.json"), load(folder, "test_cases.json")
    val = load(folder, "test_validation_results.json")["summary"]
    verdicts = Counter(u.get("feasibility_verdict") or "UNCLASSIFIED" for u in cases["unconverted_paths"])
    evidence = Counter(u.get("evidence") for u in cases["unconverted_paths"])
    return {
        "Tổng đường đi": len(paths["paths"]),
        "Test case được sinh": len(cases["test_cases"]),
        "Unconverted paths": len(cases["unconverted_paths"]),
        "FEASIBLE": (cases.get("feasibility_summary") or {}).get("by_verdict", {}).get("FEASIBLE", "chưa phân loại"),
        "Proven infeasible": verdicts.get("PROVEN_INFEASIBLE", 0) if "feasibility_summary" in cases else "chưa phân loại",
        "Search exhausted": (verdicts.get("SEARCH_EXHAUSTED", 0) if "feasibility_summary" in cases
                             else f"{evidence.get('SEARCH_EXHAUSTED', 0)} (nhãn evidence, không phân biệt chứng minh)"),
        "Generator errors": verdicts.get("GENERATOR_ERROR", 0) if "feasibility_summary" in cases else "chưa xác định",
        "Model errors": verdicts.get("MODEL_ERROR", 0) if "feasibility_summary" in cases else "chưa xác định",
        "Proof conflicts": len((cases.get("feasibility_summary") or {}).get("proof_conflicts", [])) if "feasibility_summary" in cases else "-",
        "Test case PASS": val["passed"],
        "Test case FAIL": val["failed"],
        "Test case SKIPPED": val["skipped"],
        "INCONCLUSIVE": val["inconclusive"],
        "Assertions passed/total": f"{val['assertions']['passed']}/{val['assertions']['total']}",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--prune", default=None)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    cols = {"Trước cải thiện": metrics(Path(args.before)), "Sau cải thiện (annotate)": metrics(Path(args.after))}
    if args.prune:
        cols["Sau cải thiện (prune)"] = metrics(Path(args.prune))
    before_cases = load(Path(args.before), "test_cases.json")["test_cases"]
    after_cases = load(Path(args.after), "test_cases.json")["test_cases"]
    same = before_cases == after_cases
    lines = ["# So sánh trước / sau cải thiện", "", "| Chỉ số | " + " | ".join(cols) + " |",
             "|---|" + "---|" * len(cols)]
    for key in next(iter(cols.values())):
        lines.append(f"| {key} | " + " | ".join(str(c[key]) for c in cols.values()) + " |")
    lines += ["", f"- 122 test case trước/sau giống hệt nhau (so sánh toàn bộ JSON): **{same}**."]
    Path(args.output).write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path(args.output).with_suffix(".json").write_text(json.dumps({"columns": cols, "test_cases_identical": same},
                                                                 ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
