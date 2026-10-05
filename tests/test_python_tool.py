import pytest

from agent.errors import ToolExecutionError
from agent.tools.python_exec import RunPythonInput, RunPythonTool


def run(ctx, code, tool=None):
    return (tool or RunPythonTool()).run(RunPythonInput(code=code), ctx)


def test_prints_output(ctx):
    assert run(ctx, "print(sum(range(10)))").strip() == "45"


def test_secrets_are_not_inherited(ctx, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    out = run(ctx, "import os; print(sorted(os.environ)); print(os.environ.get('ANTHROPIC_API_KEY'))")
    assert "sk-ant-secret" not in out and "ANTHROPIC_API_KEY" not in out


def test_can_read_database(ctx):
    code = ("import os, sqlite3\n"
            "c = sqlite3.connect(f\"file:{os.environ['AGENT_DB_PATH']}?mode=ro\", uri=True)\n"
            "print(c.execute('SELECT COUNT(*) FROM vehicles').fetchone()[0])")
    assert run(ctx, code).strip() == "500"


def test_exception_returns_traceback(ctx):
    with pytest.raises(ToolExecutionError, match="ZeroDivisionError"):
        run(ctx, "1 / 0")


def test_infinite_loop_is_killed(ctx):
    tool = RunPythonTool()
    tool.timeout_s = 1.5
    with pytest.raises(ToolExecutionError, match="timeout|CPU time"):
        run(ctx, "while True: pass", tool)


def test_files_persist_within_run(ctx):
    run(ctx, "open('x.txt', 'w').write('hello')")
    assert run(ctx, "print(open('x.txt').read())").strip() == "hello"


macos_sandbox = pytest.mark.skipif(not RunPythonTool().use_os_sandbox, reason="sandbox-exec only on macOS")


@macos_sandbox
def test_network_is_blocked(ctx):
    code = ("import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=2); print('CONNECTED')\n"
            "except OSError as e:\n"
            "    print('BLOCKED', e)")
    assert "BLOCKED" in run(ctx, code)


@macos_sandbox
def test_writes_outside_workdir_are_blocked(ctx, tmp_path):
    target = tmp_path / "outside.txt"
    with pytest.raises(ToolExecutionError, match="PermissionError|Operation not permitted"):
        run(ctx, f"open({str(target)!r}, 'w').write('x')")
    assert not target.exists()


@macos_sandbox
def test_database_cannot_be_modified(ctx):
    code = ("import os, sqlite3\n"
            "c = sqlite3.connect(os.environ['AGENT_DB_PATH'])\n"
            "c.execute('DELETE FROM vehicles'); c.commit()")
    with pytest.raises(ToolExecutionError):
        run(ctx, code)

