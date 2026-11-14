"""Batch runner — executed as a standalone subprocess, one per batch.

Usage:
    python runner.py <batch_dir> <cell_timeout_sec>

Executes ``analysis.ipynb`` inside *batch_dir* from top to bottom with a
brand-new Python kernel (working directory = batch_dir). Any cached
outputs stored in the uploaded notebook are discarded first, so stale
results can never leak into the rerun. The executed notebook is always
written to ``analysis_executed.ipynb`` — even when a cell fails — so it
can be downloaded afterwards.

The kernel's PID is written to ``kernel.pid`` as soon as the kernel is
up. jupyter_client always starts kernels in their own session/process
group (pgid == kernel pid), and compute children spawned by the notebook
land in that group — the service uses the file to supervise and reap the
kernel's whole group.

Exit code 0 means every cell ran; 1 means execution failed (cell error,
kernel failure, ...). The service applies its own hard wall-clock limit
to the whole process tree, so this script does not enforce it.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import nbformat
from jupyter_client import KernelManager
from nbclient import NotebookClient


def _kernel_pid(km: KernelManager):
    """PID of the running kernel process, across jupyter_client versions."""
    proc = getattr(km, "kernel", None)  # jupyter_client < 8
    pid = getattr(proc, "pid", None)
    if pid is None:
        provisioner = getattr(km, "provisioner", None)  # jupyter_client >= 8
        pid = getattr(getattr(provisioner, "process", None), "pid", None)
    return pid


def main() -> int:
    batch_dir = Path(sys.argv[1]).resolve()
    cell_timeout = int(sys.argv[2])
    nb_path = batch_dir / "analysis.ipynb"
    out_path = batch_dir / "analysis_executed.ipynb"
    kernel_pid_path = batch_dir / "kernel.pid"

    nb = nbformat.read(nb_path, as_version=4)

    # Drop cached outputs / execution counts from the submitted file:
    # only what this run produces may be trusted.
    for cell in nb.cells:
        if cell.cell_type == "code":
            cell.outputs = []
            cell.execution_count = None

    km = KernelManager(kernel_name="python3")
    client = NotebookClient(
        nb,
        km=km,
        timeout=cell_timeout,
        kernel_name="python3",
        resources={"metadata": {"path": str(batch_dir)}},
    )

    # Record the kernel PID as soon as it exists — even if this runner is
    # later SIGKILLed by the service, the kernel's process group can then
    # still be found and reaped.
    stop = threading.Event()

    def record_kernel_pid() -> None:
        while not stop.is_set():
            pid = _kernel_pid(km)
            if pid is not None:
                try:
                    kernel_pid_path.write_text(str(pid))
                except OSError:
                    pass
                return
            stop.wait(0.1)

    threading.Thread(target=record_kernel_pid, daemon=True).start()

    rc = 0
    try:
        client.execute()
    except Exception as exc:  # cell error, cell timeout, dead kernel, ...
        print(
            f"notebook execution failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        rc = 1
    finally:
        stop.set()
        # Backup PID record (covers kernels started just before exit).
        pid = _kernel_pid(km)
        if pid is not None and not kernel_pid_path.exists():
            try:
                kernel_pid_path.write_text(str(pid))
            except OSError:
                pass
        # nbclient normally shuts the kernel down itself; best-effort retry.
        try:
            if km.has_kernel:
                km.shutdown_kernel(now=True)
        except Exception:
            pass
        try:
            nbformat.write(nb, out_path)
        except Exception as exc:
            print(f"could not write executed notebook: {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
