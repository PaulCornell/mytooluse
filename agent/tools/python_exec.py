"""run_python tool: sandboxed subprocess execution (PRD 002 §3.4, TOOL-11..TOOL-16).

Isolation layers, outermost first:
1. A separate process (`python -I -B`) with a cleared environment: no API keys are inherited.
2. On macOS, `sandbox-exec` with a profile that denies all network access and limits
   writes to the run's working directory.
3. Resource limits set by a prelude in the child (hard limits, so user code can't raise them):
   CPU seconds, maximum file size, and address space on Linux.
4. A wall-clock timeout enforced by the parent.

See the PRD 002 threat model for what this does and doesn't protect against.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent.errors import ToolExecutionError
from agent.tools.base import Tool, ToolContext

MAX_STREAM_CHARS = 8_000
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_MEMORY_BYTES = 1024 * 1024 * 1024

_PRELUDE = """\
import resource as _r, sys as _s
def _cap(kind, value):
    try:
        _r.setrlimit(kind, (value, value))
    except (ValueError, OSError):
        pass
_cap(_r.RLIMIT_CPU, {cpu_s})
_cap(_r.RLIMIT_FSIZE, {fsize})
if _s.platform.startswith("linux"):
    _cap(_r.RLIMIT_AS, {mem})
del _r, _s, _cap
"""

_MACOS_PROFILE = """\
(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write* (subpath "{workdir}") (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))
"""


class RunPythonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(description="A complete Python 3 script. Print the results you need.", min_length=1)


class RunPythonTool(Tool):
    name = "run_python"
    description = (
        "Run a Python 3 script in a sandbox and return its stdout and stderr. Use it for statistics, "
        "correlations, growth rates, or anything awkward in SQL. The standard library is available "
        "(sqlite3, statistics, math, json, csv). To read data, connect read-only: "
        "sqlite3.connect(f\"file:{os.environ['AGENT_DB_PATH']}?mode=ro\", uri=True). "
        "There is no network access. Files written to the current directory persist for the rest of this "
        "run. Always print() the results; return values are not shown."
    )
    Input = RunPythonInput
    timeout_s = 30.0

    def __init__(self, use_os_sandbox: bool | None = None):
        if use_os_sandbox is None:
            use_os_sandbox = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None
        self.use_os_sandbox = use_os_sandbox

    def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, RunPythonInput)
        workdir = ctx.workdir.resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        script = workdir / f"snippet_{uuid.uuid4().hex[:8]}.py"
        prelude = _PRELUDE.format(cpu_s=int(self.timeout_s) + 1, fsize=MAX_FILE_BYTES, mem=MAX_MEMORY_BYTES)
        script.write_text(prelude + "\n" + args.code, encoding="utf-8")

        cmd = [sys.executable, "-I", "-B", str(script)]
        if self.use_os_sandbox:
            cmd = ["sandbox-exec", "-p", _MACOS_PROFILE.format(workdir=os.path.realpath(workdir)), *cmd]

        env = {  # deliberately minimal: nothing from the parent environment (TOOL-12)
            "PATH": "/usr/bin:/bin",
            "HOME": str(workdir),
            "TMPDIR": str(workdir),
            "MPLCONFIGDIR": str(workdir),
            "AGENT_DB_PATH": str(ctx.db_path.resolve()),
            "PYTHONIOENCODING": "utf-8",
        }
        try:
            proc = subprocess.run(
                cmd, cwd=workdir, env=env, capture_output=True, text=True,
                timeout=self.timeout_s, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolExecutionError(
                f"Script killed after {self.timeout_s:.0f}s wall-clock timeout. Avoid long loops; push "
                "aggregation into SQL. Partial stdout:\n" + _clip(_text(exc.stdout))
            ) from exc

        stdout, stderr = _clip(proc.stdout), _clip(proc.stderr)
        if proc.returncode != 0:
            reason = _describe_exit(proc.returncode)
            raise ToolExecutionError(f"Script failed ({reason}).\nstderr:\n{stderr}\nstdout:\n{stdout}".rstrip())
        if not stdout.strip() and not stderr.strip():
            return "(script ran successfully but printed nothing; use print() to show results)"
        return stdout + (f"\n[stderr]\n{stderr}" if stderr.strip() else "")


def _describe_exit(code: int) -> str:
    if code == -24 or code == 152:
        return "CPU time limit exceeded"
    if code < 0:
        return f"killed by signal {-code}"
    return f"exit code {code}"


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _clip(text: str) -> str:
    if len(text) <= MAX_STREAM_CHARS:
        return text
    half = MAX_STREAM_CHARS // 2
    return text[:half] + f"\n…({len(text) - MAX_STREAM_CHARS} chars omitted)…\n" + text[-half:]
