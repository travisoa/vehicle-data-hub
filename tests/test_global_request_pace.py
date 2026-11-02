"""并发调用时，全局请求起始间隔仍由一个节流器控制。"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from miit_gonggao import core


def test_concurrent_requests_share_global_spacing(monkeypatch):
    monkeypatch.setattr(core, "REQUEST_MIN_INTERVAL", (0.04, 0.04))
    monkeypatch.setattr(core, "_last_request_at", 0.0)
    barrier = threading.Barrier(4)

    def start(_index):
        barrier.wait(timeout=5)
        core._throttle()
        return time.monotonic()

    with ThreadPoolExecutor(max_workers=4) as pool:
        times = sorted(pool.map(start, range(4)))
    assert all(b - a >= 0.025 for a, b in zip(times, times[1:]))
