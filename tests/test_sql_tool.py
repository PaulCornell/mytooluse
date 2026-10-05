import pytest

from agent.errors import ToolExecutionError
from agent.tools.sql import GetSchemaInput, GetSchemaTool, RunSqlInput, RunSqlTool


def run(ctx, sql, tool=None):
    return (tool or RunSqlTool()).run(RunSqlInput(sql=sql), ctx)


def test_select_returns_markdown_table(ctx):
    out = run(ctx, "SELECT ev_type, COUNT(*) AS n FROM vehicles GROUP BY ev_type ORDER BY ev_type")
    assert out.splitlines()[0] == "| ev_type | n |"
    assert "| BEV |" in out and "(2 rows)" in out


@pytest.mark.parametrize("sql", [
    "INSERT INTO vehicles (dol_vehicle_id) VALUES (1)",
    "DELETE FROM vehicles",
    "DROP TABLE vehicles",
    "UPDATE vehicles SET make = 'X'",
    "CREATE TABLE t (x)",
    "ATTACH DATABASE ':memory:' AS other",
    "PRAGMA writable_schema = ON",
])
def test_writes_and_escapes_are_rejected(ctx, sql):
    with pytest.raises(ToolExecutionError):
        run(ctx, sql)
    assert "| 500 |" in run(ctx, "SELECT COUNT(*) FROM vehicles")  # data untouched


def test_unknown_column_error_is_actionable(ctx):
    with pytest.raises(ToolExecutionError, match="no such column: manufacturer.*get_schema"):
        run(ctx, "SELECT manufacturer FROM vehicles")


def test_multiple_statements_rejected(ctx):
    with pytest.raises(ToolExecutionError):
        run(ctx, "SELECT 1; SELECT 2")


def test_rows_are_capped(ctx):
    out = run(ctx, "SELECT dol_vehicle_id FROM vehicles")
    assert "(200 rows; truncated at 200" in out


def test_slow_query_is_cancelled(ctx):
    tool = RunSqlTool()
    tool.timeout_s = 0.3
    endless = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT MAX(x) FROM c"
    with pytest.raises(ToolExecutionError, match="cancelled"):
        run(ctx, endless, tool)


def test_schema_lists_columns_docs_and_values(ctx):
    out = GetSchemaTool().run(GetSchemaInput(), ctx)
    assert "## vehicles (500 rows)" in out
    assert "electric_range INTEGER -- All-electric range" in out
    assert "'BEV'" in out
    assert "_column_docs" not in out  # internal tables are hidden
