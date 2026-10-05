"""SQLite tools: get_schema and run_sql (PRD 002 §3.1-3.2, TOOL-1..TOOL-6)."""

from __future__ import annotations

import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent.errors import ToolExecutionError
from agent.tools.base import Tool, ToolContext

MAX_ROWS = 200
MAX_CELL_CHARS = 120
MAX_OUTPUT_CHARS = 12_000

# Operations a read-only query may perform. Everything else is denied by the authorizer.
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}


def _authorizer(action: int, arg1, arg2, db_name, trigger) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def connect_readonly(db_path: Path, *, authorize: bool = True) -> sqlite3.Connection:
    if not db_path.exists():
        raise ToolExecutionError(f"Database not found at {db_path}. Run `askdata load-data` first.")
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    if authorize:
        conn.set_authorizer(_authorizer)
    return conn


# --- get_schema --------------------------------------------------------------


class GetSchemaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetSchemaTool(Tool):
    name = "get_schema"
    description = (
        "Describe the SQLite database: tables, columns with types and meanings, row counts, and the most "
        "common values of categorical text columns. Call this before writing SQL so you use the real "
        "column names and value spellings."
    )
    Input = GetSchemaInput

    def run(self, args: BaseModel, ctx: ToolContext) -> str:
        return _describe(str(ctx.db_path.resolve()), ctx.db_path.stat().st_mtime)


@lru_cache(maxsize=4)
def _describe(db_path: str, _mtime: float) -> str:
    conn = connect_readonly(Path(db_path), authorize=False)
    try:
        docs = _column_docs(conn)
        out: list[str] = []
        info = dict(conn.execute("SELECT key, value FROM _dataset_info").fetchall()) if docs else {}
        if info:
            out.append(f"Dataset: {info.get('title')} (source: {info.get('source_url')}, loaded {info.get('loaded_at')})\n")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE '\\_%' ESCAPE '\\' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            (count,) = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            out.append(f"## {table} ({count:,} rows)")
            for _, col, ctype, *_ in conn.execute(f'PRAGMA table_info("{table}")'):
                line = f"- {col} {ctype or ''}".rstrip()
                if doc := docs.get((table, col)):
                    line += f" -- {doc}"
                if (ctype or "").upper() == "TEXT":
                    line += _top_values(conn, table, col)
                out.append(line)
            out.append("")
        return "\n".join(out).strip()
    finally:
        conn.close()


def _column_docs(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    try:
        return {(t, c): d for t, c, d in conn.execute("SELECT table_name, column_name, description FROM _column_docs")}
    except sqlite3.OperationalError:
        return {}


def _top_values(conn: sqlite3.Connection, table: str, col: str) -> str:
    (distinct,) = conn.execute(f'SELECT COUNT(DISTINCT "{col}") FROM "{table}"').fetchone()
    if distinct > 5000:
        return f" [{distinct:,} distinct]"
    top = conn.execute(
        f'SELECT "{col}", COUNT(*) c FROM "{table}" WHERE "{col}" IS NOT NULL GROUP BY 1 ORDER BY c DESC LIMIT 6'
    ).fetchall()
    values = ", ".join(f"'{v}'" for v, _ in top)
    return f" [{distinct:,} distinct; most common: {values}]"


# --- run_sql -----------------------------------------------------------------


class RunSqlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(description="A single read-only SQLite SELECT statement (CTEs allowed).", min_length=1)


class RunSqlTool(Tool):
    name = "run_sql"
    description = (
        "Run one read-only SQL query against the SQLite database and return the result as a table. "
        f"Only SELECT/WITH queries are allowed. At most {MAX_ROWS} rows are returned, so aggregate in SQL "
        "(GROUP BY, COUNT, AVG) rather than fetching raw rows. Queries that run longer than the timeout are "
        "cancelled. SQLite dialect: use strftime for dates, || for concatenation, and CAST(x AS REAL) "
        "before dividing integers."
    )
    Input = RunSqlInput
    timeout_s = 10.0

    def run(self, args: BaseModel, ctx: ToolContext) -> str:
        assert isinstance(args, RunSqlInput)
        conn = connect_readonly(ctx.db_path)
        deadline = time.monotonic() + self.timeout_s
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            cursor = conn.execute(args.sql)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchmany(MAX_ROWS + 1)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc):
                raise ToolExecutionError(
                    f"Query cancelled after {self.timeout_s:.0f}s. Simplify it: add WHERE filters, "
                    "aggregate earlier, or avoid cross joins."
                ) from exc
            if "not authorized" in str(exc):
                raise ToolExecutionError("Not allowed: the database is read-only and only SELECT queries may run.") from exc
            raise ToolExecutionError(f"SQLite error: {exc}. Call get_schema if you're unsure of names.") from exc
        except (sqlite3.DatabaseError, sqlite3.Warning, sqlite3.ProgrammingError) as exc:
            raise ToolExecutionError(f"SQLite error: {exc}") from exc
        finally:
            conn.close()

        if not columns:
            return "Query ran but returned no result set."
        truncated = len(rows) > MAX_ROWS
        return format_table(columns, rows[:MAX_ROWS], truncated)


def format_table(columns: list[str], rows: list[tuple], truncated: bool) -> str:
    def cell(v: object) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, float):
            v = round(v, 4)
        s = str(v).replace("|", "\\|").replace("\n", " ")
        return s if len(s) <= MAX_CELL_CHARS else s[: MAX_CELL_CHARS - 1] + "…"

    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    text = "\n".join(lines)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n…(output truncated)"
    footer = f"\n({len(rows)} row{'s' if len(rows) != 1 else ''}"
    footer += f"; truncated at {MAX_ROWS}, aggregate further)" if truncated else ")"
    return text + footer
