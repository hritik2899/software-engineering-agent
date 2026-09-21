# Feature Matrix

This file is the contract between the architecture discussion and the code. It is
intentionally conservative: **implemented** means there is executable code and a
testable path in this repository, not merely a diagram.

## Implemented and runnable

| Capability | Implementation |
|---|---|
| Natural-language engineering task | `TaskCreate.instruction` + REST `POST /tasks` |
| Separate task/session/environment identity | `domain.py`, SQL task/session records, replaceable environment ID |
| Persistent task/session/event state | SQLAlchemy models + repositories |
| Explicit task state machine | CREATED → QUEUED → PROVISIONING → RUNNING / WAITING → terminal |
| Async scheduling | in-memory queue locally; Redis Streams when Redis is configured |
| Duplicate-execution protection | renewable SQL `TaskLeaseRow` ownership lease |
| Heartbeats and stale-task recovery | orchestrator heartbeat + periodic recovery scanner |
| Agent/runtime separation | `CodingAgent` vs `ToolRegistry` / `Workspace` |
| LLM abstraction | OpenAI provider + deterministic local mock provider |
| Specialized agent behavior | shared runtime with bug/test/refactor/review profiles |
| Context management | durable task memory + recent event selection + bounded model context |
| Context compaction | high-signal durable summary with compaction watermark |
| Mid-run user redirection | user messages persisted as active instructions and events |
| Pause/resume | persisted WAITING_FOR_USER state + distributed pause/resume APIs |
| File/search/shell/Git tools | tool registry with path confinement |
| Completion verification gate | edited tasks must verify, inspect Git and checkpoint |
| Multi-repository workspace | one task can clone and operate on multiple named repositories |
| Reusable code context pool | index cache partitioned by repository origin + commit SHA |
| Dependency context | lightweight import/symbol dependency-neighbor index |
| Durable code checkpoint | Git commit + persisted binary patch |
| Environment replacement recovery | new workspace can restore latest persisted patches |
| Event replay | ordered SQL event log with monotonic sequence |
| Cross-process live events | Redis Pub/Sub when Redis is configured |
| WebSocket progress stream | replay + live event endpoint |
| Local execution provider | zero-setup local workspace |
| Per-task Docker isolation | per-task container, resource limits, no-new-privileges |
| Warm environment demonstration | explicit single-tenant `docker_pool` provider |
| Repository checkout cache | bare mirror + reference clone |
| Dependency caches | shared pip/npm/Maven/Gradle cache mounts |
| Private GitHub clone/push | backend-only Git auth using `GITHUB_TOKEN` |
| PR publication | backend GitHub publisher; token is not exposed as an LLM tool |
| API authentication | optional `X-Minion-Api-Key` |
| Observability | structured logs, health/readiness, Prometheus metrics |
| Schema migrations | Alembic baseline migration |
| Retry/cancel controls | REST endpoints + persisted state |
| Full no-key smoke run | `python scripts/smoke_test.py` |

## Implemented as a reference/local analogue

These are real code paths but not a substitute for company infrastructure:

- **Docker environment ≈ remote DevPod execution boundary.** The interface is real;
  the provider runs locally.
- **`docker_pool` ≈ warm execution pool.** It demonstrates allocation latency
  reduction on one trusted machine. It deliberately shares the workspace parent
  and therefore is *not* a multi-tenant production sandbox.
- **Code intelligence** is a commit-partitioned symbol/import/text index. It is not a
  seven-language compiler-grade AST/call graph.
- **AuthorizationPolicy** is a boundary and local URL policy, not enterprise IAM.
- **Context compaction** is deterministic high-signal compaction, not a separate
  learned summarization service.
- **Redis Streams + SQL leases** demonstrate at-least-once scheduling and execution
  ownership. A large fleet may use Kafka/SQS/Temporal/etc. instead.

## Deliberately not claimed as implemented

These require organization-specific infrastructure or much larger subsystems:

- actual Uber DevPod APIs or proprietary Minion internals;
- Kubernetes/DevPod fleet autoscaling and multi-tenant network policies;
- organization IAM/repository ACL integration and secrets broker;
- full AST/call-graph construction across seven languages;
- semantic embedding/vector-search service and federated knowledge graph;
- distributed Bazel/Gradle build-cache service;
- production service mesh, OpenTelemetry collector deployment and SLO stack;
- image attachments and multimodal coding-agent input;
- cross-repository dependent PR merge orchestration;
- billing/quota/cost-control service.

The interfaces were chosen so these can be added without rewriting the agent loop.
