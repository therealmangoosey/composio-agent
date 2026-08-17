"""Lightweight parallel task runner for Tab Assistant.

Uses a small bounded ThreadPoolExecutor so multiple independent research/tasks
can run concurrently without spawning expensive processes on Termux/Android.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import os


def run_parallel(tasks, worker, max_workers=None):
    tasks = [str(t).strip() for t in tasks if str(t).strip()]
    if not tasks:
        return []
    limit = max_workers or int(os.getenv("TAB_AGENT_WORKERS", "4"))
    limit = max(1, min(6, limit, len(tasks)))
    results = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="tab-agent") as pool:
        futures = {pool.submit(worker, task): i for i, task in enumerate(tasks)}
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = (True, future.result())
            except Exception as exc:
                results[i] = (False, str(exc))
    return results
