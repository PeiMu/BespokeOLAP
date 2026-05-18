import random
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

JOB_QUERIES_DIR = Path("/home/pei/Project/benchmarks/imdb_job-postgres/queries")

JOB_QUERY_IDS = [
    "1a", "1b", "1c", "1d",
    "2a", "2b", "2c", "2d",
    "3a", "3b", "3c",
    "4a", "4b", "4c",
    "5a", "5b", "5c",
    "6a", "6b", "6c", "6d", "6e", "6f",
    "7a", "7b", "7c",
    "8a", "8b", "8c", "8d",
    "9a", "9b", "9c", "9d",
    "10a", "10b", "10c",
    "11a", "11b", "11c", "11d",
    "12a", "12b", "12c",
    "13a", "13b", "13c", "13d",
    "14a", "14b", "14c",
    "15a", "15b", "15c", "15d",
    "16a", "16b", "16c", "16d",
    "17a", "17b", "17c", "17d", "17e", "17f",
    "18a", "18b", "18c",
    "19a", "19b", "19c", "19d",
    "20a", "20b", "20c",
    "21a", "21b", "21c",
    "22a", "22b", "22c", "22d",
    "23a", "23b", "23c",
    "24a", "24b",
    "25a", "25b", "25c",
    "26a", "26b", "26c",
    "27a", "27b", "27c",
    "28a", "28b", "28c",
    "29a", "29b", "29c",
    "30a", "30b", "30c",
    "31a", "31b", "31c",
    "32a", "32b",
    "33a", "33b", "33c",
]


@lru_cache(maxsize=None)
def _load_sql(query_id: str, queries_dir: str = str(JOB_QUERIES_DIR)) -> str:
    path = Path(queries_dir) / f"{query_id}.sql"
    if not path.exists():
        raise FileNotFoundError(f"JOB query file not found: {path}")
    return path.read_text().strip()


def gen_query(
    query_name: str = "Q1a",
    rnd: Optional[random.Random] = None,
    seed: int = 42,
    queries_dir: Path = JOB_QUERIES_DIR,
) -> Tuple[str, str, Dict]:
    if query_name.upper().startswith("Q"):
        query_id = query_name[1:]
    else:
        query_id = query_name

    sql = _load_sql(query_id, str(queries_dir))
    return sql, sql, {}
