"""Schema-grounded SQL compilation and execution against the reactor database.

The structured data pipeline has seven stages: runtime schema loading,
constrained planning, relational validation, allowlisted entity resolution,
parameterised SQL compilation, BigQuery dry-run validation, and execution
into typed records.
"""

from dataclasses import dataclass, field
from typing import Any


# ---- data classes ----

@dataclass
class ColumnInfo:
    """Metadata for a single database column."""
    name: str
    data_type: str = "STRING"
    description: str = ""
    filterable: bool = True


@dataclass
class DatabaseQuerySpec:
    """Parsed query specification produced by the LLM planner."""
    operation: str = "rows"
    table: str = ""
    columns: list[str] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)


# ---- exceptions ----

class DatabaseQueryError(RuntimeError):
    """Base for all structured-data query errors."""


class UnsupportedDatabaseQuery(DatabaseQueryError):
    """Query type not handled by the structured lane."""


class QueryClarificationRequired(DatabaseQueryError):
    """Query is ambiguous and needs user clarification."""


class QueryExecutionError(DatabaseQueryError):
    """Query compiled but failed during execution."""


# ---- public functions ----

def field_filter_capability(column_name: str) -> dict[str, Any]:
    """Return the set of filter operators available for *column_name*."""
    raise NotImplementedError


def parse_query_spec(raw: dict) -> DatabaseQuerySpec:
    """Parse raw LLM output into a validated query spec."""
    raise NotImplementedError


def load_runtime_schema() -> dict[str, ColumnInfo]:
    """Fetch the live table schema from BigQuery."""
    raise NotImplementedError


def query_spec_output_schema() -> dict:
    """JSON schema that the LLM planner must conform to."""
    raise NotImplementedError


def compile_query(spec: DatabaseQuerySpec) -> str:
    """Compile a query spec into parameterised SQL."""
    raise NotImplementedError


def dry_run_query(sql: str) -> dict:
    """Validate SQL via BigQuery dry-run (no data scanned)."""
    raise NotImplementedError


def execute_query(sql: str) -> list[dict]:
    """Execute SQL and return typed result rows."""
    raise NotImplementedError


def resolve_query_values(spec: DatabaseQuerySpec) -> DatabaseQuerySpec:
    """Normalise entity values against the allowlisted vocabulary."""
    raise NotImplementedError


def build_database_packet(*args, **kwargs):
    """Package query results into an :class:`EvidencePacket`."""
    raise NotImplementedError
