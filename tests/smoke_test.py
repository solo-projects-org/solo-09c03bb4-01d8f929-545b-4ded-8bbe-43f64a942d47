#!/usr/bin/env python3
"""End-to-end smoke test for the recomputation service.

Starts uvicorn on a test port with a short time limit, then exercises:
  1. ok sample            -> match
  2. stale_cache sample   -> match (cached outputs ignored)
  3. error sample         -> failed / execution_error
  4. doctored expected    -> mismatch
  5. NaN result           -> failed / invalid_result
  6. no result.json       -> failed / missing_result
  7. concurrency          -> 3rd batch rejected with 429, slots released after
  8. slow sample          -> failed / timeout, compute child reaped
  9. late_write sample    -> failed verdict stands after the late write

Run from the repo root with the venv python:
    /tmp/recalc-venv/bin/python tests/smoke_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import nbformat
from nbformat.v4 import new_code_cell, new_notebook

ROOT = Path(__file__).resolve().parent.parent
PORT = 8011
BASE = f"http://127.0.0.1:{PORT}"
TIMEOUT_SEC = 8  # service-level hard limit for the test run
WORK_DIR = Path("/tmp/recalc_test_batches")

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def submit(client: httpx.Client, sample_dir: Path, expected_override: dict | None = None):
    expected = (
        json.dumps(expected_override)
        if expected_override is not None
        else (sample_dir / "expected.json").read_text()
    )
    files = {
        "notebook": ("analysis.ipynb", (sample_dir / "analysis.ipynb").read_bytes()),
        "observations": ("observations.csv", (sample_dir / "observations.csv").read_bytes()),
        "expected": ("expected.json", expected.encode()),
    }
    return client.post(f"{BASE}/batches", files=files)


def wait_final(client: httpx.Client, batch_id: str, max_sec: float = 40) -> dict:
    deadline = time.time() + max_sec
    while time.time() < deadline:
        body = client.get(f"{BASE}/batches/{batch_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.5)
    raise AssertionError(f"batch {batch_id} still running after {max_sec}s")


def notebook_with(cells: list[str]) -> bytes:
    nb = new_notebook()
    nb.cells = [new_code_cell(c) for c in cells]
    return nbformat.writes(nb).encode()


def submit_inline(client: httpx.Client, nb_bytes: bytes, expected: dict):
    csv_bytes = (ROOT / "samples/ok/observations.csv").read_bytes()
    files = {
        "notebook": ("analysis.ipynb", nb_bytes),
        "observations": ("observations.csv", csv_bytes),
        "expected": ("expected.json", json.dumps(expected).encode()),
    }
    return client.post(f"{BASE}/batches", files=files)


def main() -> int:
    env = {
        **os.environ,
        "RECALC_WORK_DIR": str(WORK_DIR),
        "RECALC_TIMEOUT_SEC": str(TIMEOUT_SEC),
        "RECALC_MAX_PARALLEL": "2",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        # server runs from the repo root so "app" is importable
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        with httpx.Client(timeout=30) as client:
            for _ in range(60):
                try:
                    if client.get(f"{BASE}/docs").status_code == 200:
                        break
                except httpx.TransportError:
                    time.sleep(0.5)
            else:
                raise RuntimeError("server did not come up")

            # 1. ok -> match
            r = submit(client, ROOT / "samples/ok")
            check("ok: accepted", r.status_code == 202, f"got {r.status_code}")
            body = wait_final(client, r.json()["batch_id"])
            check("ok: match", body["status"] == "match", body["status"])
            checks = {c["field"]: c for c in body["report"]["checks"]}
            check(
                "ok: abs_error <= 1e-6",
                all(c["ok"] and c["abs_error"] <= 1e-6 for c in checks.values()),
            )
            bid = body["batch_id"]
            nb_dl = client.get(f"{BASE}/batches/{bid}/notebook")
            check("ok: executed notebook downloadable", nb_dl.status_code == 200)
            nb_json = nb_dl.json()
            outs = [
                o
                for c in nb_json["cells"]
                if c["cell_type"] == "code"
                for o in c.get("outputs", [])
            ]
            check("ok: executed notebook has fresh outputs", len(outs) > 0)
            rep_dl = client.get(f"{BASE}/batches/{bid}/report")
            check("ok: report downloadable", rep_dl.status_code == 200)

            # 2. stale_cache -> match (cache ignored)
            r = submit(client, ROOT / "samples/stale_cache")
            body = wait_final(client, r.json()["batch_id"])
            check(
                "stale_cache: match despite stale outputs",
                body["status"] == "match",
                body["status"],
            )
            est = [c for c in body["report"]["checks"] if c["field"] == "estimate"][0]
            check(
                "stale_cache: actual is the rerun value, not 999.0",
                abs(est["actual"] - est["expected"]) <= 1e-6,
                f"actual={est['actual']}",
            )

            # 3. error -> failed / execution_error
            r = submit(client, ROOT / "samples/error")
            body = wait_final(client, r.json()["batch_id"])
            check("error: failed", body["status"] == "failed", body["status"])
            check(
                "error: kind=execution_error",
                body["report"]["error"]["kind"] == "execution_error",
            )
            nb_dl = client.get(f"{BASE}/batches/{body['batch_id']}/notebook")
            check("error: partial notebook still downloadable", nb_dl.status_code == 200)

            # 4. doctored expected -> mismatch
            good = json.loads((ROOT / "samples/ok/expected.json").read_text())
            bad = {**good, "estimate": good["estimate"] + 0.01}
            r = submit(client, ROOT / "samples/ok", expected_override=bad)
            body = wait_final(client, r.json()["batch_id"])
            check("mismatch: status", body["status"] == "mismatch", body["status"])
            est = [c for c in body["report"]["checks"] if c["field"] == "estimate"][0]
            check("mismatch: estimate flagged", not est["ok"] and est["abs_error"] > 1e-6)

            # 5. NaN result -> failed / invalid_result
            nb = notebook_with(
                [
                    "import json",
                    "json.dump({'estimate': float('nan'), 'ci_low': -1.0, 'ci_high': 1.0}, open('result.json', 'w'))",
                ]
            )
            r = submit_inline(client, nb, good)
            body = wait_final(client, r.json()["batch_id"])
            check(
                "nan: failed / invalid_result",
                body["status"] == "failed"
                and body["report"]["error"]["kind"] == "invalid_result",
                f"{body['status']} / {body['report']['error']}",
            )

            # 6. no result.json -> failed / missing_result
            nb = notebook_with(["print('no result written')"])
            r = submit_inline(client, nb, good)
            body = wait_final(client, r.json()["batch_id"])
            check(
                "missing: failed / missing_result",
                body["status"] == "failed"
                and body["report"]["error"]["kind"] == "missing_result",
                f"{body['status']} / {body['report']['error']}",
            )

            # 7. concurrency: 2 slow batches occupy slots, 3rd rejected
            def slow_children() -> int:
                ps = subprocess.run(
                    ["ps", "-eo", "args"], capture_output=True, text=True
                ).stdout
                return sum(
                    1 for line in ps.splitlines() if "time.sleep(120)" in line
                )

            baseline_strays = slow_children()
            r1 = submit(client, ROOT / "samples/slow")
            r2 = submit(client, ROOT / "samples/slow")
            check("concurrency: two accepted", r1.status_code == 202 and r2.status_code == 202)
            time.sleep(1.0)  # let both actually start
            r3 = submit(client, ROOT / "samples/ok")
            check("concurrency: third rejected with 429", r3.status_code == 429, f"got {r3.status_code}")
            b1 = wait_final(client, r1.json()["batch_id"], max_sec=TIMEOUT_SEC + 30)
            b2 = wait_final(client, r2.json()["batch_id"], max_sec=TIMEOUT_SEC + 30)
            check(
                "slow: both failed with timeout",
                b1["status"] == "failed"
                and b2["status"] == "failed"
                and b1["report"]["error"]["kind"] == "timeout"
                and b2["report"]["error"]["kind"] == "timeout",
                f"{b1['status']}, {b2['status']}",
            )
            check(
                "slow: ran at least the hard limit",
                b1["report"]["duration_sec"] >= TIMEOUT_SEC,
                f"duration={b1['report']['duration_sec']}",
            )
            nb_dl = client.get(f"{BASE}/batches/{b1['batch_id']}/notebook")
            check("slow: killed run has no executed notebook (404)", nb_dl.status_code == 404)
            slots = client.get(f"{BASE}/batches").json()["slots"]
            check("slots released after timeouts", slots["active"] == 0, str(slots))
            # slow compute child (sleep 120) must be gone with its batch
            time.sleep(1.0)
            check(
                "slow: compute child reaped",
                slow_children() == baseline_strays,
                f"before={baseline_strays} after={slow_children()}",
            )

            # 8. slots usable again
            r = submit(client, ROOT / "samples/ok")
            check("slots: new batch accepted after release", r.status_code == 202)
            body = wait_final(client, r.json()["batch_id"])
            check("slots: batch completes", body["status"] == "match", body["status"])

            # 9. late_write: fails fast; matching result.json lands later; verdict stands
            r = submit(client, ROOT / "samples/late_write")
            bid = r.json()["batch_id"]
            body = wait_final(client, bid)
            check(
                "late_write: failed / execution_error",
                body["status"] == "failed"
                and body["report"]["error"]["kind"] == "execution_error",
                f"{body['status']} / {body['report']['error']}",
            )
            time.sleep(5)  # detached child writes a *matching* result.json now
            late_result = WORK_DIR / bid / "result.json"
            check(
                "late_write: late result.json actually landed",
                late_result.exists(),
            )
            again = client.get(f"{BASE}/batches/{bid}").json()
            check(
                "late_write: verdict unchanged after late write",
                again["status"] == "failed",
                again["status"],
            )
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
