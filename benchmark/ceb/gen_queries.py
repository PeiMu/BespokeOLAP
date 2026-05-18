"""
Step 3: Generate 1 concrete query instantiation per CEB template with seed=42.

Uses the same shared Random(42) state as the official run.py pipeline
and the official format_args_string() for Bespoke args formatting.

SQL is post-processed to remove NULL-related clauses since the Bespoke C++
code ignores NULL in all its filters (never matches NULL rows).

Outputs:
  - benchmark/ceb/sql/<template>.sql     (for DuckDB golden reference)
  - benchmark/ceb/queries_bespoke.txt    (for Bespoke engine, one line per query)

Usage:
    python benchmark/ceb/gen_queries.py
"""
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dataset.gen_ceb.gen_ceb_query import gen_query_single_only


def format_args_string(query_list, placeholder_list):
    """Format query args for Bespoke stdin protocol.

    Identical to tools.validate_tool.query_validator_class.format_args_string.
    """
    args_list = []
    for qid_str, placeholders in zip(query_list, placeholder_list):
        tmp_vals = []
        for v in placeholders.values():
            if isinstance(v, str) and v.startswith("("):
                tmp_vals.append(v)
            else:
                tmp_vals.append(f'"{v}"')
        args_list.append(f"{qid_str} {' '.join(tmp_vals)}")
    return args_list


def strip_null_from_sql(sql: str) -> str:
    """Remove NULL-related clauses to match Bespoke's behavior.

    The Bespoke C++ code ignores NULL in all filters (build_string_filter
    skips <<NULL>>/NULL values, allow_null stays false). So DuckDB SQL must
    also not match NULL rows for a fair comparison.
    """
    lines = sql.splitlines()
    out = []
    for line in lines:
        # Remove " OR col IS NULL" suffix from IN-clause lines
        line = re.sub(r"\s+OR\s+\S+\s+IS\s+NULL\)", ")", line, flags=re.IGNORECASE)
        # Remove bare NULL from IN-lists: IN ('val',NULL) -> IN ('val')
        line = re.sub(r",\s*NULL", "", line, flags=re.IGNORECASE)
        line = re.sub(r"NULL\s*,", "", line, flags=re.IGNORECASE)
        out.append(line)
    return "\n".join(out)

CEB_DIR = Path("/mnt/labstore/bespoke_olap/datasets/ceb/imdb")
OUTPUT_DIR = Path(__file__).resolve().parent
SQL_DIR = OUTPUT_DIR / "sql"
BESPOKE_QUERIES_FILE = OUTPUT_DIR / "queries_bespoke.txt"

QUERY_TEMPLATES = [
    "1a", "2a", "2b", "2c", "3a", "3b", "4a", "5a",
    "6a", "7a", "8a", "9a", "9b", "10a", "11a", "11b",
]


def main():
    SQL_DIR.mkdir(parents=True, exist_ok=True)

    rnd = random.Random(42)
    query_list = []
    placeholder_list = []
    sql_list = []

    for qname in QUERY_TEMPLATES:
        template_str, sql, placeholders = gen_query_single_only(
            ceb_dir=CEB_DIR,
            query_name=f"Q{qname}",
            rnd=rnd,
        )

        sql_list.append(sql)
        query_list.append(qname)
        placeholder_list.append(placeholders)

        # Write SQL for DuckDB (strip NULL clauses to match Bespoke behavior)
        sql_path = SQL_DIR / f"{qname}.sql"
        sql_path.write_text(strip_null_from_sql(sql) + "\n")

        print(f"  {qname}: {len(placeholders)} params -> {sql_path.name}")

    # Format Bespoke args using the official format_args_string
    bespoke_lines = format_args_string(query_list, placeholder_list)
    BESPOKE_QUERIES_FILE.write_text("\n".join(bespoke_lines) + "\n")
    print(f"\nWrote {len(bespoke_lines)} queries to {BESPOKE_QUERIES_FILE}")


if __name__ == "__main__":
    main()
