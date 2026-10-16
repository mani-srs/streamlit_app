"""Named worker pools under one global concurrency cap."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Sequence

from core.state import merge_by_index

GLOBAL_CAP = 16
POOLS = {"translate": 4, "evaluate": 2, "introspect": 4, "probe": 12}

_global = threading.BoundedSemaphore(GLOBAL_CAP)


def map_ordered(pool: str, fn: Callable[[Any], Any], items: Sequence[Any],
                on_error: Callable[[int, Exception], Any] | None = None) -> list:
    """Run fn over items in the named pool; results merged by index (input order)."""
    if not items:
        return []
    workers = min(POOLS.get(pool, 2), len(items))

    def run(i: int):
        with _global:
            try:
                return i, fn(items[i])
            except Exception as e:
                if on_error is None:
                    raise
                return i, on_error(i, e)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=pool) as ex:
        futs = [ex.submit(run, i) for i in range(len(items))]
        return merge_by_index(len(items), (f.result() for f in as_completed(futs)))
