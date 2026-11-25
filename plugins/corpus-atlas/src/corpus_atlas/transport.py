"""Bulk download transport: aria2c when available, Python otherwise.

aria2 only moves bytes. Every payload it produces is validated by the same rules
as the Python path (PDF magic / OA package extraction for main files, supplement
acceptance rules) and stored through :func:`corpus_atlas.fulltext.store_file`, so
the recorded rows (sha256, size, source, license) are identical whichever
transport fetched the file.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

ARIA2_TIMEOUT = int(os.environ.get("CORPUS_ATLAS_ARIA2_TIMEOUT", "1800"))
# Hosts whose bot protection rejects aria2/curl client fingerprints but serves the same OA file
# to the Python client (measured: MDPI and Europe PMC render return 403 to aria2c).
PYTHON_ONLY_HOSTS = tuple(h.strip().lower() for h in os.environ.get(
    "CORPUS_ATLAS_ARIA2_PYTHON_HOSTS", "mdpi.com,europepmc.org").split(",") if h.strip())


def python_only(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in PYTHON_ONLY_HOSTS)


def aria2_path() -> str | None:
    return shutil.which("aria2c")


def choose(transport: str) -> str:
    if transport == "aria2":
        if not aria2_path():
            raise SystemExit("--transport aria2 requested but aria2c is not on PATH (brew/apt install aria2)")
        return "aria2"
    if transport == "auto":
        return "aria2" if aria2_path() else "python"
    return "python"


class Aria2Unavailable(RuntimeError):
    """aria2c could not run at all; callers fall back to the Python transport."""


def aria2_download(jobs: list[dict[str, str]], staging: Path, *, user_agent: str, accept: str,
                   max_concurrent: int = 8, connections_per_server: int = 2, split: int = 2,
                   timeout: int = ARIA2_TIMEOUT) -> dict[str, dict[str, Any]]:
    """Download ``jobs`` ([{"id", "url"}]) in one aria2c run.

    Returns {id: {"path": Path} | {"error": str}}. ``--continue`` plus deterministic
    staging names make an interrupted run resumable; ``--max-connection-per-server``
    bounds per-host load. Raises :class:`Aria2Unavailable` if aria2c cannot start.
    """
    staging.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {}
    if not jobs:
        return results
    names = {}
    lines = []
    for job in jobs:
        name = ".dl-" + hashlib.sha256(f"{job['id']}\n{job['url']}".encode()).hexdigest()[:24] + ".part"
        names[job["id"]] = staging / name
        lines += [job["url"], f"  out={name}"]
    queue = staging / f"aria2-input-{os.getpid()}.txt"
    queue.write_text("\n".join(lines) + "\n", encoding="utf-8")
    hosts = {urllib.parse.urlparse(job["url"]).netloc for job in jobs}
    command = [
        aria2_path() or "aria2c", f"--input-file={queue}", f"--dir={staging}",
        f"--max-concurrent-downloads={max(1, min(max_concurrent, 16))}",
        f"--max-connection-per-server={max(1, min(connections_per_server, 4))}",
        f"--split={max(1, min(split, 4))}", "--min-split-size=1M",
        "--continue=true", "--auto-file-renaming=false", "--allow-overwrite=true",
        "--file-allocation=none", "--max-tries=2", "--retry-wait=2", "--connect-timeout=15",
        "--timeout=60", "--lowest-speed-limit=1K", "--max-file-not-found=1",
        "--summary-interval=0", "--console-log-level=warn", "--download-result=hide",
        "--follow-metalink=false", "--follow-torrent=false", "--remote-time=false",
        f"--user-agent={user_agent}", f"--header=Accept: {accept}",
    ]
    if len(hosts) == 1 and len(jobs) > 1:
        # a single host: keep the politeness bound of the Python transport
        command[3] = f"--max-concurrent-downloads={max(1, min(max_concurrent, 4))}"
    try:
        completed = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=timeout, text=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise Aria2Unavailable(f"aria2c failed to run: {type(error).__name__}: {error}") from error
    finally:
        queue.unlink(missing_ok=True)
    log = re.sub(r"\x1b\[[0-9;]*m", "", completed.stdout or "")
    for job in jobs:
        path = names[job["id"]]
        control = path.with_name(path.name + ".aria2")
        if path.is_file() and not control.exists() and path.stat().st_size > 0:
            results[job["id"]] = {"path": path}
        else:
            reason = f"aria2c exit {completed.returncode}" + (": incomplete" if control.exists() else ": no file")
            lines = log.splitlines()
            for n, line in enumerate(lines):
                if f"URI={job['url']}" in line:
                    detail = next((l.strip() for l in lines[n:n + 3] if "status=" in l or "->" in l), line.strip())
                    reason += f" ({detail[:200]})"
                    break
            results[job["id"]] = {"error": reason, "path": path}
    return results


def cleanup(result: dict[str, Any]) -> None:
    path = result.get("path")
    if isinstance(path, Path):
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".aria2").unlink(missing_ok=True)
