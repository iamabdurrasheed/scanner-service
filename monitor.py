"""
monitor.py - Resource monitoring using psutil
Tracks CPU and memory usage of scan processes.
"""

import psutil
import time
import threading
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ResourceMetrics:
    pid: Optional[int] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    cpu_samples: list = field(default_factory=list)
    mem_samples: list = field(default_factory=list)  # in MB

    @property
    def duration_seconds(self) -> float:
        if self.start_time and self.end_time:
            return round(self.end_time - self.start_time, 2)
        return 0.0

    @property
    def cpu_avg(self) -> str:
        if not self.cpu_samples:
            return "0%"
        return f"{round(sum(self.cpu_samples) / len(self.cpu_samples), 1)}%"

    @property
    def cpu_peak(self) -> str:
        if not self.cpu_samples:
            return "0%"
        return f"{round(max(self.cpu_samples), 1)}%"

    @property
    def mem_avg(self) -> str:
        if not self.mem_samples:
            return "0MB"
        return f"{round(sum(self.mem_samples) / len(self.mem_samples), 1)}MB"

    @property
    def mem_peak(self) -> str:
        if not self.mem_samples:
            return "0MB"
        return f"{round(max(self.mem_samples), 1)}MB"

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_seconds": self.duration_seconds,
            "cpu_avg": self.cpu_avg,
            "cpu_peak": self.cpu_peak,
            "memory_avg": self.mem_avg,
            "memory_peak": self.mem_peak,
        }


class ProcessMonitor:
    """
    Polls a process every `interval` seconds collecting CPU% and RSS memory.
    Call start(pid) to begin monitoring, stop() to end it.
    """

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.metrics = ResourceMetrics()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self, pid: int):
        self.metrics = ResourceMetrics(pid=pid, start_time=time.time())
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.metrics.end_time = time.time()

    def _poll(self):
        try:
            proc = psutil.Process(self.metrics.pid)
            # First call initialises the CPU interval counter
            proc.cpu_percent(interval=None)
            while not self._stop_event.is_set():
                time.sleep(self.interval)
                if not proc.is_running():
                    break
                try:
                    cpu = proc.cpu_percent(interval=None)
                    mem_mb = proc.memory_info().rss / (1024 * 1024)
                    self.metrics.cpu_samples.append(cpu)
                    self.metrics.mem_samples.append(mem_mb)

                    # Also capture child processes (e.g. docker sub-processes)
                    for child in proc.children(recursive=True):
                        try:
                            cpu += child.cpu_percent(interval=None)
                            mem_mb += child.memory_info().rss / (1024 * 1024)
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


class SystemMonitor:
    """
    Monitors overall system CPU and memory (useful for parallel mode).
    """

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.metrics = ResourceMetrics()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self):
        self.metrics = ResourceMetrics(start_time=time.time())
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.metrics.end_time = time.time()

    def _poll(self):
        psutil.cpu_percent(interval=None)  # initialise
        while not self._stop_event.is_set():
            time.sleep(self.interval)
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            mem_mb = (mem.total - mem.available) / (1024 * 1024)
            self.metrics.cpu_samples.append(cpu)
            self.metrics.mem_samples.append(mem_mb)


def merge_metrics(metrics_list: list[ResourceMetrics]) -> dict:
    """Merge multiple ResourceMetrics into a single summary dict."""
    all_cpu = []
    all_mem = []
    for m in metrics_list:
        all_cpu.extend(m.cpu_samples)
        all_mem.extend(m.mem_samples)

    cpu_avg = f"{round(sum(all_cpu) / len(all_cpu), 1)}%" if all_cpu else "0%"
    cpu_peak = f"{round(max(all_cpu), 1)}%" if all_cpu else "0%"
    mem_avg = f"{round(sum(all_mem) / len(all_mem), 1)}MB" if all_mem else "0MB"
    mem_peak = f"{round(max(all_mem), 1)}MB" if all_mem else "0MB"

    return {
        "cpu_avg": cpu_avg,
        "cpu_peak": cpu_peak,
        "memory_avg": mem_avg,
        "memory_peak": mem_peak,
    }
