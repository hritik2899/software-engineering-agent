"""Prometheus metrics for the control plane."""
from prometheus_client import Counter, Gauge, Histogram

TASKS_SUBMITTED = Counter("minion_tasks_submitted_total", "Tasks accepted by the API")
TASKS_COMPLETED = Counter("minion_tasks_completed_total", "Tasks completed successfully")
TASKS_FAILED = Counter("minion_tasks_failed_total", "Tasks that ended in failure")
TASKS_RECOVERED = Counter("minion_tasks_recovered_total", "Stale tasks requeued for recovery")
ACTIVE_TASKS = Gauge("minion_active_tasks", "Tasks executing in this process")
TASK_DURATION = Histogram(
    "minion_task_duration_seconds",
    "End-to-end agent execution duration",
    buckets=(5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600),
)
ENVIRONMENT_ALLOCATION = Histogram(
    "minion_environment_allocation_seconds",
    "Environment allocation/attachment latency",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
