# Architecture

This document maps the implementation to a Minion-style remote coding platform.
It is deliberately explicit so you can use the repository as an HLD study guide.

## High-level architecture

```text
Engineer / Web UI
        |
        | REST + WebSocket
        v
+------------------------- CONTROL PLANE --------------------------+
| FastAPI                                                        |
|   |                                                            |
|   +--> AuthorizationPolicy                                     |
|   +--> TaskRepository ------> SQL task/session/event store      |
|   +--> WorkQueue -----------> Redis (or in-memory locally)      |
|   +--> EventBus -----------> replayable WebSocket stream        |
|                              |                                 |
|                              v                                 |
|                        Orchestrator                             |
|                        /    |     \                            |
|                recovery  scheduling  lifecycle                  |
|                              |                                 |
|                              v                                 |
|                     EnvironmentProvider                         |
+------------------------------|----------------------------------+
                               |
              +----------------+----------------+
              |                                 |
              v                                 v
    LocalEnvironmentProvider           DockerEnvironmentProvider
              |                                 |
              +---------------+-----------------+
                              |
                              v
+------------------------ EXECUTION PLANE -------------------------+
| Workspace / environmentId                                      |
|   +-- repo A  (agent/<taskId>)                                 |
|   +-- repo B  (agent/<taskId>)                                 |
|   |                                                            |
|   +--> CodingAgent                                             |
|         |                                                      |
|         +--> ContextManager --> durable session/events          |
|         +--> LLMClient                                         |
|         +--> ToolRegistry                                      |
|                +-- list/read/write/search                       |
|                +-- shell/build/test                             |
|                +-- git diff/status/checkpoint                   |
+------------------------------|----------------------------------+
                               |
                               v
                         Git / Pull Request
```

## The three IDs

| ID | Meaning | Lifetime |
|---|---|---|
| `taskId` | What engineering work exists | survives all runtime failures |
| `sessionId` | What the agent has learned/done | survives runtime restart |
| `environmentId` | Where code is executing | replaceable after environment failure |

A browser disconnect changes none of them. An agent-runtime restart changes none of
them. If the whole execution environment dies, only `environmentId` needs to be
replaced; the task/session continue from durable context and Git checkpoints.

## End-to-end sequence

```text
UI        API       DB       Queue    Orchestrator Environment Agent      Git
|          |         |         |          |            |        |         |
| POST task|         |         |          |            |        |         |
|--------->| create  |         |          |            |        |         |
|          |-------->|         |          |            |        |         |
|          | enqueue |-------->|          |            |        |         |
|<--202----|         |         |          |            |        |         |
|          |         |         | task id  |            |        |         |
|          |         |         |--------->| allocate   |        |         |
|          |         |         |          |----------->|        |         |
|          |         |         |          |            | start  |         |
|          |         |         |          |            |------->|         |
|          |         |<---------------- durable events ----------|         |
|<======================= WebSocket events =====================|         |
|          |         |         |          |            |        | edit    |
|          |         |         |          |            |        | test    |
|          |         |         |          |            |        |-------> |
|          |         |         |          |            |        | PR      |
```

## Context management

The runtime distinguishes **memory** from **context**:

* Memory: the full durable task/session/event history plus current Git/workspace state.
* Context: the bounded subset supplied to the model for the current reasoning step.

`ContextManager` currently includes the original task, durable session summary,
plan/constraints and a recent event window. Tool outputs are truncated to avoid
unbounded context growth. A production extension can add semantic code retrieval,
dependency-graph retrieval and LLM-based history compaction behind the same interface.

## Crash recovery

### LLM request fails
`CodingAgent._model_turn` retries with exponential backoff. Workspace is untouched.

### Agent/control-plane process fails
Tasks/sessions/events remain in SQL. On startup `Orchestrator.reconcile()` requeues
active tasks and attempts `EnvironmentProvider.attach()`.

### Environment fails
If attach fails, a new environment is allocated. The logical task/session remain.
Git checkpoint commits are the durable code checkpoint boundary. Uncheckpointed
filesystem state can be lost if the entire environment disappears.

### Browser disconnects
Events were already persisted. The client reconnects with the last event sequence,
replays missed events via `GET /tasks/{id}/events?after=N`, then resumes WebSocket.

## Multi-repository tasks

A task accepts multiple `RepositorySpec` entries. Each is checked out into one
workspace with an independent `agent/<taskId>` branch. The agent gets all repo names
in its context and can coordinate schema/backend/client changes explicitly.

## Performance/caching path

The reference implementation contains the concrete optimizations behind the
20-30 minute -> ~10 minute design story:

1. **Repository mirror cache**: repeat tasks use a local Git mirror/reference clone.
2. **Warm image cache**: Docker provider pulls the sandbox image once at startup.
3. **Dependency caches**: pip/npm/Maven/Gradle cache directories are shared across sandboxes.
4. **Persistent task/session state**: control-plane restart does not restart understanding from zero.
5. **Git checkpoints**: recovery can continue from stable code checkpoints.
6. **Bounded context**: avoids repeatedly sending an unbounded history to the model.

A real fleet can extend `EnvironmentProvider` with a DevPod/Kubernetes warm-pool
implementation. The provider boundary is intentionally the only required change.

Illustrative latency decomposition:

```text
Before                          After
cold env        2-4 min         warmed image/env     seconds-1 min
repo checkout    1-3 min         mirror/reference     seconds
dependencies     3-6 min         shared caches        ~1 min
repo discovery   2-4 min         reusable context     ~1-2 min
build/test       5-8 min         incremental/cache    ~2-3 min
agent work       remainder       remainder
```

Only claim exact numbers in an interview if you have measured them.
