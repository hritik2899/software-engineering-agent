"""Prometheus lifecycle metrics.

Metrics describe stable business/runtime boundaries—submitted, running, completed,
failed, duration and environment allocation—rather than fragile internal function
counts. This keeps operational dashboards useful as implementation details evolve.
"""
from prometheus_client import Counter, Gauge, Histogram

TASKS_SUBMITTED = Counter("minion_tasks_submitted_total", "Tasks accepted by the API")
TASKS_COMPLETED = Counter("minion_tasks_completed_total", "Tasks completed successfully")
TASKS_FAILED = Counter("minion_tasks_failed_total", "Tasks ending in failure")
TASKS_RUNNING = Gauge("minion_tasks_running", "Currently executing tasks")
TASK_DURATION = Histogram(
    "minion_task_duration_seconds",
    "End-to-end orchestrator execution duration",
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600),
)
ENVIRONMENT_ALLOCATIONS = Counter(
    "minion_environment_allocations_total",
    "Execution environments allocated",
    ["provider"],
)
