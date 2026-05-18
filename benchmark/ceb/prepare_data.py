"""
Step 2: Create imdb.duckdb from IMDB CSV files.
FK constraints are NOT enforced (IMDB data has referential integrity violations),
but relationship metadata is written to imdb.relationships.json for the scaler.

Usage:
    python benchmark/ceb/prepare_data.py
"""
import json
import os
import sys

import duckdb

CSV_DIR = "/home/pei/Project/benchmarks/imdb_job-postgres/csv"
OUTPUT_DB = os.path.join(os.path.dirname(__file__), "imdb.duckdb")
OUTPUT_REL = os.path.join(os.path.dirname(__file__), "imdb.relationships.json")

CREATE_STMTS = [
    """CREATE TABLE comp_cast_type (
        id INTEGER PRIMARY KEY, kind VARCHAR)""",
    """CREATE TABLE company_name (
        id INTEGER PRIMARY KEY, name VARCHAR, country_code VARCHAR,
        imdb_id INTEGER, name_pcode_nf VARCHAR, name_pcode_sf VARCHAR, md5sum VARCHAR)""",
    """CREATE TABLE company_type (
        id INTEGER PRIMARY KEY, kind VARCHAR)""",
    """CREATE TABLE info_type (
        id INTEGER PRIMARY KEY, info VARCHAR)""",
    """CREATE TABLE keyword (
        id INTEGER PRIMARY KEY, keyword VARCHAR, phonetic_code VARCHAR)""",
    """CREATE TABLE kind_type (
        id INTEGER PRIMARY KEY, kind VARCHAR)""",
    """CREATE TABLE link_type (
        id INTEGER PRIMARY KEY, link VARCHAR)""",
    """CREATE TABLE role_type (
        id INTEGER PRIMARY KEY, role VARCHAR)""",
    """CREATE TABLE char_name (
        id INTEGER PRIMARY KEY, name VARCHAR, imdb_index VARCHAR,
        imdb_id INTEGER, name_pcode_nf VARCHAR, surname_pcode VARCHAR, md5sum VARCHAR)""",
    """CREATE TABLE name (
        id INTEGER PRIMARY KEY, name VARCHAR, imdb_index VARCHAR,
        imdb_id INTEGER, gender VARCHAR, name_pcode_cf VARCHAR,
        name_pcode_nf VARCHAR, surname_pcode VARCHAR, md5sum VARCHAR)""",
    """CREATE TABLE title (
        id INTEGER PRIMARY KEY, title VARCHAR, imdb_index VARCHAR,
        kind_id INTEGER, production_year INTEGER, imdb_id INTEGER,
        phonetic_code VARCHAR, episode_of_id INTEGER, season_nr INTEGER,
        episode_nr INTEGER, series_years VARCHAR, md5sum VARCHAR)""",
    """CREATE TABLE aka_name (
        id INTEGER PRIMARY KEY, person_id INTEGER, name VARCHAR,
        imdb_index VARCHAR, name_pcode_cf VARCHAR, name_pcode_nf VARCHAR,
        surname_pcode VARCHAR, md5sum VARCHAR)""",
    """CREATE TABLE aka_title (
        id INTEGER PRIMARY KEY, movie_id INTEGER, title VARCHAR,
        imdb_index VARCHAR, kind_id INTEGER, production_year INTEGER,
        phonetic_code VARCHAR, episode_of_id INTEGER, season_nr INTEGER,
        episode_nr INTEGER, note VARCHAR, md5sum VARCHAR)""",
    """CREATE TABLE cast_info (
        id INTEGER PRIMARY KEY, person_id INTEGER, movie_id INTEGER,
        person_role_id INTEGER, note VARCHAR, nr_order INTEGER, role_id INTEGER)""",
    """CREATE TABLE complete_cast (
        id INTEGER PRIMARY KEY, movie_id INTEGER,
        subject_id INTEGER, status_id INTEGER)""",
    """CREATE TABLE movie_companies (
        id INTEGER PRIMARY KEY, movie_id INTEGER, company_id INTEGER,
        company_type_id INTEGER, note VARCHAR)""",
    """CREATE TABLE movie_info (
        id INTEGER PRIMARY KEY, movie_id INTEGER,
        info_type_id INTEGER, info VARCHAR, note VARCHAR)""",
    """CREATE TABLE movie_info_idx (
        id INTEGER PRIMARY KEY, movie_id INTEGER,
        info_type_id INTEGER, info VARCHAR, note VARCHAR)""",
    """CREATE TABLE movie_keyword (
        id INTEGER PRIMARY KEY, movie_id INTEGER, keyword_id INTEGER)""",
    """CREATE TABLE movie_link (
        id INTEGER PRIMARY KEY, movie_id INTEGER,
        linked_movie_id INTEGER, link_type_id INTEGER)""",
    """CREATE TABLE person_info (
        id INTEGER PRIMARY KEY, person_id INTEGER,
        info_type_id INTEGER, info VARCHAR, note VARCHAR)""",
]

# FK relationships: (table, [from_cols], ref_table, [to_cols])
RELATIONSHIPS = [
    ("title", ["kind_id"], "kind_type", ["id"]),
    ("aka_name", ["person_id"], "name", ["id"]),
    ("aka_title", ["movie_id"], "title", ["id"]),
    ("aka_title", ["kind_id"], "kind_type", ["id"]),
    ("cast_info", ["person_id"], "name", ["id"]),
    ("cast_info", ["movie_id"], "title", ["id"]),
    ("cast_info", ["person_role_id"], "char_name", ["id"]),
    ("cast_info", ["role_id"], "role_type", ["id"]),
    ("complete_cast", ["movie_id"], "title", ["id"]),
    ("complete_cast", ["subject_id"], "comp_cast_type", ["id"]),
    ("complete_cast", ["status_id"], "comp_cast_type", ["id"]),
    ("movie_companies", ["movie_id"], "title", ["id"]),
    ("movie_companies", ["company_id"], "company_name", ["id"]),
    ("movie_companies", ["company_type_id"], "company_type", ["id"]),
    ("movie_info", ["movie_id"], "title", ["id"]),
    ("movie_info", ["info_type_id"], "info_type", ["id"]),
    ("movie_info_idx", ["movie_id"], "title", ["id"]),
    ("movie_info_idx", ["info_type_id"], "info_type", ["id"]),
    ("movie_keyword", ["movie_id"], "title", ["id"]),
    ("movie_keyword", ["keyword_id"], "keyword", ["id"]),
    ("movie_link", ["movie_id"], "title", ["id"]),
    ("movie_link", ["linked_movie_id"], "title", ["id"]),
    ("movie_link", ["link_type_id"], "link_type", ["id"]),
    ("person_info", ["person_id"], "name", ["id"]),
    ("person_info", ["info_type_id"], "info_type", ["id"]),
]


def _table_name(stmt: str) -> str:
    return stmt.split("CREATE TABLE ")[1].split("(")[0].strip()


def main():
    if os.path.exists(OUTPUT_DB):
        os.remove(OUTPUT_DB)
    wal = OUTPUT_DB + ".wal"
    if os.path.exists(wal):
        os.remove(wal)

    con = duckdb.connect(OUTPUT_DB)

    for stmt in CREATE_STMTS:
        table = _table_name(stmt)
        csv_path = os.path.join(CSV_DIR, f"{table}.csv")
        if not os.path.exists(csv_path):
            print(f"WARNING: {csv_path} not found, skipping {table}")
            continue

        con.execute(stmt)
        cols = con.execute(f"PRAGMA table_info('{table}')").fetchall()
        col_names = ", ".join(f'"{row[1]}"' for row in cols)

        con.execute(
            f"COPY {table}({col_names}) FROM '{csv_path}' "
            f"(DELIMITER ',', HEADER false, NULL '', ESCAPE '\\', QUOTE '\"')"
        )
        count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} rows")

    con.close()

    # Write relationship metadata for the scaler
    with open(OUTPUT_REL, "w") as f:
        json.dump(RELATIONSHIPS, f, indent=2)

    print(f"\nCreated: {OUTPUT_DB}")
    print(f"Created: {OUTPUT_REL}")


if __name__ == "__main__":
    main()
