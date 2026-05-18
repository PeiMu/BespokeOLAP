from pathlib import Path

from dataset.gen_job.gen_job_query import JOB_QUERIES_DIR, JOB_QUERY_IDS, _load_sql

job_queries = {f"Q{qid}": _load_sql(qid) for qid in JOB_QUERY_IDS}
