"""Batch lifecycle management.

Design notes
------------
* Batch state lives in memory only (a plain dict); nothing is persisted.
* Each batch runs in its own fresh directory. The uploaded
  notebook is executed top to bottom with a brand-new Python kernel;
  cached outputs in the submitted file are discarded before execution.
* The runner subprocess is started with ``start_new_session=True`` (its
  own process group). jupyter_client additionally starts each kernel in
  its *own* session, so the runner records the kernel's pid in
  ``kernel.pid``; compute children spawned by the notebook land in the
  kernel's process group. The wall-clock limit is enforced on both
  groups, and the verdict is published only after both are completely
  empty (runner + kernel + compute children gone). The concurrency slot
  is released at that same moment — never earlier.
* A batch is finalized exactly once. Anything a stray process writes into
  the batch directory afterwards ("late writes") cannot change the
  published verdict.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .compare import FIELDS, TOLERANCE, compare_results

log = logging.getLogger("recalc.manager")

STATUS_RUNNING = "running"
STATUS_MATCH = "match"          # rerun is consistent with the manuscript
STATUS_MISMATCH = "mismatch"    # rerun finished but numbers differ
STATUS_FAILED = "failed"        # timeout / execution error / missing or invalid result
FINAL_STATUSES = {STATUS_MATCH, STATUS_MISMATCH, STATUS_FAILED}

RUNNER_PATH = Path(__file__).resolve().parent / "runner.py"
POLL_INTERVAL_SEC = 0.2
# After SIGKILL a process group disappears almost immediately; this grace
# period only guards against pathological uninterruptible-sleep corners.
POST_KILL_GRACE_SEC = 30.0


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class Batch:
    """In-memory state of one recomputation batch."""

    def __init__(self, batch_id: str, directory: Path, expected: dict):
        self.id = batch_id
        self.dir = directory
        self.expected = expected
        self.status = STATUS_RUNNING
        self.report: Optional[dict] = None
        self.submitted_at = time.time()
        self.finished_at: Optional[float] = None
        self.proc: Optional[subprocess.Popen] = None


class BatchManager:
    def __init__(self, work_dir: Path, max_parallel: int = 2, timeout_sec: int = 60):
        self.work_dir = work_dir
        self.max_parallel = max_parallel
        self.timeout_sec = timeout_sec
        self.batches: dict[str, Batch] = {}
        self.active = 0
        self._slot_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # submission
    # ------------------------------------------------------------------ #
    async def try_create(
        self, notebook_bytes: bytes, csv_bytes: bytes, expected: dict
    ) -> Optional[Batch]:
        """Reserve a slot and start a batch, or return None when full."""
        async with self._slot_lock:
            if self.active >= self.max_parallel:
                return None
            self.active += 1
        try:
            batch_id = uuid.uuid4().hex[:12]
            batch_dir = self.work_dir / batch_id
            batch_dir.mkdir(parents=True, exist_ok=False)
            (batch_dir / "analysis.ipynb").write_bytes(notebook_bytes)
            (batch_dir / "observations.csv").write_bytes(csv_bytes)
            (batch_dir / "expected.json").write_text(
                json.dumps(expected, indent=2, ensure_ascii=False)
            )
            batch = Batch(batch_id, batch_dir, expected)
            self._spawn(batch)
        except Exception:
            async with self._slot_lock:
                self.active -= 1
            shutil.rmtree(self.work_dir / batch_id, ignore_errors=True)
            raise
        self.batches[batch_id] = batch
        asyncio.create_task(self._watch(batch))
        log.info("batch %s started (active=%d)", batch_id, self.active)
        return batch

    def _spawn(self, batch: Batch) -> None:
        out_log = open(batch.dir / "runner.stdout.log", "wb")
        err_log = open(batch.dir / "runner.stderr.log", "wb")
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(RUNNER_PATH),
                    str(batch.dir),
                    # Per-cell limit stays above the batch hard limit so the
                    # service-level timeout is always the authoritative one.
                    str(self.timeout_sec + 5),
                ],
                cwd=str(batch.dir),
                stdout=out_log,
                stderr=err_log,
                # Own session/process group. The kernel gets yet another
                # group from jupyter_client; the runner records its pid in
                # kernel.pid so both groups can be supervised and reaped.
                start_new_session=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        finally:
            out_log.close()
            err_log.close()
        batch.proc = proc

    # ------------------------------------------------------------------ #
    # supervision
    # ------------------------------------------------------------------ #
    async def _watch(self, batch: Batch) -> None:
        proc = batch.proc
        runner_pgid = proc.pid  # start_new_session => runner is group leader
        deadline = time.monotonic() + self.timeout_sec
        timed_out = False
        kernel_pgid: Optional[int] = None

        # Phase 1: wait for the runner, enforcing the hard limit.
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                kernel_pgid = kernel_pgid or self._kernel_pgid(batch)
                self._kill_group(batch, runner_pgid)
                if kernel_pgid is not None:
                    self._kill_group(batch, kernel_pgid)
                break
            await asyncio.sleep(POLL_INTERVAL_SEC)
        try:
            await asyncio.to_thread(proc.wait)  # reap our direct child
        except Exception:
            pass

        # Phase 2: the kernel and every compute child of this batch must be
        # gone before any verdict is published. jupyter_client always starts
        # the kernel in its own process group (pgid == kernel pid, recorded
        # by the runner in kernel.pid); compute children spawned by the
        # notebook land in that group. The deadline still applies, so a
        # notebook that "finished" but left a busy child behind becomes a
        # timeout once the deadline passes.
        while True:
            if kernel_pgid is None:
                kernel_pgid = self._kernel_pgid(batch)
            groups = [runner_pgid] + ([kernel_pgid] if kernel_pgid is not None else [])
            if all(self._group_empty(g) for g in groups):
                break
            now = time.monotonic()
            if now >= deadline and not timed_out:
                timed_out = True
                self._kill_group(batch, runner_pgid)
                if kernel_pgid is not None:
                    self._kill_group(batch, kernel_pgid)
            if timed_out and now >= deadline + POST_KILL_GRACE_SEC:
                log.error(
                    "batch %s: process group(s) %s not empty %.0fs after SIGKILL; "
                    "finalizing anyway",
                    batch.id,
                    groups,
                    POST_KILL_GRACE_SEC,
                )
                break
            await asyncio.sleep(POLL_INTERVAL_SEC)

        self._finalize(batch, timed_out=timed_out, returncode=proc.returncode)

    @staticmethod
    def _kernel_pgid(batch: Batch) -> Optional[int]:
        """Process group id of this batch's Jupyter kernel, if recorded."""
        try:
            return int((batch.dir / "kernel.pid").read_text().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _kill_group(batch: Batch, pgid: int) -> None:
        log.warning("batch %s: killing process group %d", batch.id, pgid)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError) as exc:
            log.warning("batch %s: killpg(%d) failed: %s", batch.id, pgid, exc)

    @staticmethod
    def _group_empty(pgid: int) -> bool:
        # No process left in the group ...
        try:
            os.killpg(pgid, 0)
            return False
        except (ProcessLookupError, PermissionError):
            pass
        # ... and no single process with that id either (covers kernels
        # that somehow are not group leaders).
        try:
            os.kill(pgid, 0)
            return False
        except (ProcessLookupError, PermissionError):
            return True

    # ------------------------------------------------------------------ #
    # finalization (exactly once per batch)
    # ------------------------------------------------------------------ #
    def _finalize(self, batch: Batch, timed_out: bool, returncode: Optional[int]) -> None:
        if batch.status != STATUS_RUNNING:
            # Already finalized — a late write or a stray signal must never
            # resurrect or change a finished batch.
            return

        report = {
            "batch_id": batch.id,
            "compared_fields": list(FIELDS),
            "tolerance": TOLERANCE,
            "submitted_at": _iso(batch.submitted_at),
            "finished_at": None,
            "duration_sec": None,
            "timed_out": timed_out,
            "runner_exit_code": returncode,
            "checks": None,
            "error": None,
        }

        if timed_out:
            batch.status = STATUS_FAILED
            report["error"] = {
                "kind": "timeout",
                "message": f"execution exceeded the {self.timeout_sec}s limit",
            }
        elif returncode != 0:
            batch.status = STATUS_FAILED
            report["error"] = {
                "kind": "execution_error",
                "message": "notebook execution failed",
                "stderr_tail": self._stderr_tail(batch),
            }
        else:
            result_path = batch.dir / "result.json"
            actual = None
            if result_path.exists():
                try:
                    actual = json.loads(result_path.read_text())
                    if not isinstance(actual, dict):
                        raise ValueError("top-level JSON value must be an object")
                except Exception as exc:
                    batch.status = STATUS_FAILED
                    report["error"] = {
                        "kind": "missing_result",
                        "message": f"result.json is not a valid JSON object: {exc}",
                    }
            else:
                batch.status = STATUS_FAILED
                report["error"] = {
                    "kind": "missing_result",
                    "message": "result.json was not produced by this run",
                }

            if actual is not None:
                cmp_result = compare_results(batch.expected, actual)
                report["checks"] = cmp_result["checks"]
                if not cmp_result["all_finite"]:
                    batch.status = STATUS_FAILED
                    report["error"] = {
                        "kind": "invalid_result",
                        "message": "estimate, ci_low and ci_high must all be finite numbers",
                    }
                elif cmp_result["consistent"]:
                    batch.status = STATUS_MATCH
                else:
                    batch.status = STATUS_MISMATCH

        batch.finished_at = time.time()
        report["status"] = batch.status
        report["finished_at"] = _iso(batch.finished_at)
        report["duration_sec"] = round(batch.finished_at - batch.submitted_at, 3)
        batch.report = report
        try:
            (batch.dir / "report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False)
            )
        except OSError:
            log.exception("batch %s: could not persist report.json", batch.id)

        self.active -= 1
        log.info(
            "batch %s finalized: %s in %.1fs (active=%d)",
            batch.id,
            batch.status,
            report["duration_sec"],
            self.active,
        )

    @staticmethod
    def _stderr_tail(batch: Batch, limit: int = 4000) -> str:
        try:
            return (batch.dir / "runner.stderr.log").read_text(errors="replace")[-limit:]
        except OSError:
            return ""

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    def get(self, batch_id: str) -> Optional[Batch]:
        return self.batches.get(batch_id)

    def summary(self) -> dict:
        return {
            "slots": {"active": self.active, "max_parallel": self.max_parallel},
            "timeout_sec": self.timeout_sec,
            "batches": [
                {
                    "batch_id": b.id,
                    "status": b.status,
                    "submitted_at": _iso(b.submitted_at),
                    "finished_at": _iso(b.finished_at),
                }
                for b in sorted(self.batches.values(), key=lambda b: b.submitted_at)
            ],
        }
