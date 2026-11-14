#!/usr/bin/env python3
"""Regenerate the fictional follow-up samples under samples/ (deterministic).

Five batches are produced:

* ``ok``          — clean notebook; rerun matches the manuscript values.
* ``stale_cache`` — the submitted notebook carries stale cached outputs
                    (estimate 999.0 from an old pilot run); the service must
                    ignore them and still match.
* ``error``       — a cell raises; the batch must fail with execution_error.
* ``slow``        — the notebook spawns a slow compute child (same process
                    group) and sleeps past the limit; must fail with timeout
                    and the child must be cleaned up.
* ``late_write``  — the notebook spawns a *detached* child that writes a
                    perfectly matching result.json a few seconds later, then
                    crashes immediately; the failed verdict must stand.

Run from the repo root:  python scripts/make_samples.py
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import nbformat
import numpy as np
import pandas as pd
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook, new_output
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
SEED = 20261114
N_PER_GROUP = 24

MD_CELL = new_markdown_cell(
    "# Fictional follow-up study — recomputation\n"
    "\n"
    "Reads `observations.csv` (baseline / 12-week follow-up systolic blood\n"
    "pressure, mmHg), computes the treatment-vs-control difference in mean\n"
    "change from baseline with a Welch 95% CI, and writes `result.json`."
)

IMPORTS_CELL = """\
import json

import numpy as np
import pandas as pd
from scipy import stats

df = pd.read_csv("observations.csv")
df["change"] = df["followup_mmhg"] - df["baseline_mmhg"]
treat = df.loc[df["group"] == "treatment", "change"]
ctrl = df.loc[df["group"] == "control", "change"]
n1, n2 = len(treat), len(ctrl)
print(f"n_treatment={n1}, n_control={n2}")
"""

COMPUTE_CELL = """\
estimate = float(treat.mean() - ctrl.mean())
s1, s2 = float(treat.var(ddof=1)), float(ctrl.var(ddof=1))
se = float(np.sqrt(s1 / n1 + s2 / n2))
welch_df = (s1 / n1 + s2 / n2) ** 2 / (
    (s1 / n1) ** 2 / (n1 - 1) + (s2 / n2) ** 2 / (n2 - 1)
)
ci_low, ci_high = stats.t.interval(0.95, welch_df, loc=estimate, scale=se)
result = {"estimate": estimate, "ci_low": float(ci_low), "ci_high": float(ci_high)}
print(json.dumps(result, indent=2))
"""

WRITE_CELL = """\
with open("result.json", "w") as fh:
    json.dump(result, fh, indent=2)
print("result.json written")
"""

ERROR_CELL = """\
raise ValueError("simulated analysis failure: bad contrast coding in cell 3")
"""

SLOW_CELL = """\
import subprocess
import sys
import time

# A heavy compute child that stays in the kernel's process group.
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(f"spawned slow compute child pid={child.pid}")
time.sleep(120)  # far beyond the service time limit
"""

LATE_WRITE_TEMPLATE = """\
import subprocess
import sys

# Detached child: survives the kernel and writes a *matching* result.json
# a few seconds after this notebook has already crashed.
LATE_CHILD = {late_child!r}
subprocess.Popen([sys.executable, "-c", LATE_CHILD], start_new_session=True)
raise RuntimeError("simulated failure: analysis crashed before writing result.json")
"""


def make_observations() -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    rows = []
    for i in range(N_PER_GROUP * 2):
        group = "treatment" if i < N_PER_GROUP else "control"
        baseline = rng.normal(148.0 if group == "treatment" else 149.0, 9.0)
        change = rng.normal(-6.0 if group == "treatment" else -1.2, 3.5)
        rows.append(
            {
                "patient_id": f"P{i + 1:03d}",
                "group": group,
                "baseline_mmhg": round(float(baseline), 1),
                "followup_mmhg": round(float(baseline + change), 1),
            }
        )
    return pd.DataFrame(rows)


def compute_expected(csv_path: Path) -> dict:
    """Apply the exact notebook math to the CSV as written on disk."""
    df = pd.read_csv(csv_path)
    df["change"] = df["followup_mmhg"] - df["baseline_mmhg"]
    treat = df.loc[df["group"] == "treatment", "change"]
    ctrl = df.loc[df["group"] == "control", "change"]
    n1, n2 = len(treat), len(ctrl)
    estimate = float(treat.mean() - ctrl.mean())
    s1, s2 = float(treat.var(ddof=1)), float(ctrl.var(ddof=1))
    se = float(np.sqrt(s1 / n1 + s2 / n2))
    welch_df = (s1 / n1 + s2 / n2) ** 2 / (
        (s1 / n1) ** 2 / (n1 - 1) + (s2 / n2) ** 2 / (n2 - 1)
    )
    ci_low, ci_high = stats.t.interval(0.95, welch_df, loc=estimate, scale=se)
    p_value = float(stats.ttest_ind(treat, ctrl, equal_var=False).pvalue)
    # Manuscripts report rounded numbers; 6 decimals stays well within the
    # 1e-6 recomputation tolerance.
    return {
        "estimate": round(estimate, 6),
        "ci_low": round(float(ci_low), 6),
        "ci_high": round(float(ci_high), 6),
        "p_value": round(p_value, 6),  # extra field: ignored by the comparator
        "source": "manuscript Table 2 (fictional)",
    }


def base_notebook() -> nbformat.NotebookNode:
    nb = new_notebook()
    nb.metadata["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    nb.metadata["language_info"] = {"name": "python", "version": "3"}
    return nb


def build_ok() -> nbformat.NotebookNode:
    nb = base_notebook()
    nb.cells = [
        MD_CELL,
        new_code_cell(IMPORTS_CELL),
        new_code_cell(COMPUTE_CELL),
        new_code_cell(WRITE_CELL),
    ]
    return nb


def build_stale_cache() -> nbformat.NotebookNode:
    """Same analysis, but the file carries outputs from an old pilot run."""
    nb = build_ok()
    compute = nb.cells[2]
    compute.execution_count = 7
    compute.outputs = [
        new_output(
            "stream",
            name="stdout",
            text="{\n  \"estimate\": 999.0,\n  \"ci_low\": 998.0,\n"
            '  "ci_high": 1000.0\n}\n',
        ),
        new_output(
            "execute_result",
            data={"text/plain": "{'estimate': 999.0, 'ci_low': 998.0, 'ci_high': 1000.0}"},
            execution_count=7,
        ),
    ]
    write = nb.cells[3]
    write.execution_count = 8
    write.outputs = [
        new_output("stream", name="stdout", text="result.json written  # STALE CACHE, pilot 2025\n")
    ]
    return nb


def build_error() -> nbformat.NotebookNode:
    nb = base_notebook()
    nb.cells = [
        MD_CELL,
        new_code_cell(IMPORTS_CELL),
        new_code_cell(ERROR_CELL),
        new_code_cell(WRITE_CELL),
    ]
    return nb


def build_slow() -> nbformat.NotebookNode:
    nb = base_notebook()
    nb.cells = [
        MD_CELL,
        new_code_cell(IMPORTS_CELL),
        new_code_cell(SLOW_CELL),
        new_code_cell(WRITE_CELL),
    ]
    return nb


def build_late_write(expected: dict) -> nbformat.NotebookNode:
    late_child = (
        "import json, time\n"
        "time.sleep(3)\n"
        f"json.dump({{'estimate': {expected['estimate']!r}, "
        f"'ci_low': {expected['ci_low']!r}, "
        f"'ci_high': {expected['ci_high']!r}}}, open('result.json', 'w'))\n"
    )
    nb = base_notebook()
    nb.cells = [
        MD_CELL,
        new_code_cell(LATE_WRITE_TEMPLATE.format(late_child=late_child)),
    ]
    return nb


def write_sample(name: str, nb: nbformat.NotebookNode, csv: str, expected: dict) -> None:
    d = SAMPLES / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    nbformat.write(nb, d / "analysis.ipynb")
    (d / "observations.csv").write_text(csv)
    (d / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")
    print(f"  samples/{name}/")


def main() -> None:
    df = make_observations()
    csv_text = df.to_csv(index=False)

    tmp_csv = SAMPLES / ".tmp_observations.csv"
    SAMPLES.mkdir(exist_ok=True)
    tmp_csv.write_text(csv_text)
    expected = compute_expected(tmp_csv)
    tmp_csv.unlink()
    print("expected values:", json.dumps(expected, indent=2))

    write_sample("ok", build_ok(), csv_text, expected)
    write_sample("stale_cache", build_stale_cache(), csv_text, expected)
    write_sample("error", build_error(), csv_text, expected)
    write_sample("slow", build_slow(), csv_text, expected)
    write_sample("late_write", build_late_write(expected), csv_text, expected)
    print("done.")


if __name__ == "__main__":
    main()
