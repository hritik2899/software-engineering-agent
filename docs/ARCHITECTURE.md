# Architecture / HLD

## Mental model

The platform has a **control plane** and an **execution plane**.

```text
                         Engineer / Web UI
                                |
                         REST + WebSocket
                                |
        +---------------- CONTROL PLANE ----------------+
        |                                               |
        |  FastAPI                                      |
        |    |                                          |
        |    +--> API auth / repository policy          |
        |    +--> SQL task + session + event store      |
        |    +--> Redis Streams work queue              |
        |    +--> Redis Pub/Sub live events             |
        |    |                                          |
        |    +--> Orchestrator ----------------------+  |
        |           |       |          |             |  |
        |           |       |          |             |  |
        |        task lease heartbeat  recovery    metrics|
        |           |       |          |             |  |
        |           +-------+----------+-------------+  |
        |                       |                       |
        |                EnvironmentProvider            |
        +-----------------------|-----------------------+
                                |
               +----------------+----------------+
               |                |                |
             local            docker       docker_pool
                                |
        +--------------- EXECUTION PLANE ----------------+
        |                                                 |
        | task environment / environmentId                |
        |   +-- repo A                                    |
        |   +-- repo B                                    |
        |   +-- dependency/build caches                   |
        |                                                 |
        |   CodingAgent                                   |
        |      |                                          |
        |      +--> ContextManager                        |
        |      |      +-- durable session memory          |
        |      |      +-- recent events                   |
        |      |      +-- repo+commit context index       |
        |      |                                          |
        |      +--> LLMClient                             |
        |      +--> ToolRegistry                          |
        |             +-- read/write/search               |
        |             +-- shell/build/test                |
        |             +-- Git diff/status/checkpoint      |
        |                                                 |
        +-------------------------|-----------------------+
                                  |
                           Git branch / PR
```

## Why three identifiers exist

| Identifier | Question answered | Failure behavior |
|---|---|---|
| `taskId` | What engineering task is this? | survives everything |
| `sessionId` | What has the agent learned/done? | survives runtime/environment restart |
| `environmentId` | Where is execution happening? | replaceable |

A browser disconnect changes none of them. A backend restart should leave the task
and session intact and reacquire a task lease. If the execution environment is
gone, the orchestrator allocates another environment and restores the latest
durable checkpoint patches.

## Normal task sequence

```text
UI         API/DB        Queue       Orchestrator      Environment       Agent
|            |             |              |                |              |
| POST task  |             |              |                |              |
|----------->| persist     |              |                |              |
|            |--enqueue--->|              |                |              |
|<-- 202 ----|             |              |                |              |
|            |             |--task------->|                |              |
|            |             |              | acquire lease  |              |
|            |             |              |--------------->| allocate     |
|            |             |              |                |------------->|
|            |<================ durable ordered events ===================|
|<===================== replay + WebSocket ===============================|
|            |             |              |                | inspect/edit |
|            |             |              |                | test/diff    |
|            |             |              |                | checkpoint   |
|            |             |              |                |              |
|            |             |              |<---------------| result       |
|            | complete    |              |                               |
```

## Distributed ownership

Queue delivery is at-least-once. Receiving a message is not sufficient to run it.

```text
Redis Stream message
       |
       v
TaskLeaseRepository.acquire(taskId, workerId)
       |
   +---+---+
   |       |
 acquired  owned elsewhere
   |       |
 execute   acknowledge duplicate
```

The owner renews the lease. If heartbeats stop, the recovery scanner requeues the
logical task. This is why duplicate queue delivery does not imply duplicate code
execution.

## Context architecture

```text
                         all durable memory
                               |
        +----------------------+----------------------+
        |                      |                      |
 original task           session state           event log
                                |
                       current plan/constraints
                                |
                    repo+commit context partition
                                |
                    ContextManager selection
                                |
                         bounded LLM context
```

The reusable code index is keyed by **repository origin + commit SHA**. A task with
multiple repositories combines only its authorized partitions. The implementation
extracts text, symbols and imports and exposes retrieval/dependency-neighbor tools.

## Recovery levels

### Model call failure
The model call is retried with exponential backoff. Files are untouched.

### Browser failure
The browser reconnects using its last event sequence and replays SQL events.

### Control-plane instance failure
The execution lease expires. Another worker requeues/acquires the task and attempts
to attach to the previous environment.

### Agent process / environment failure
If the environment cannot be attached, a replacement workspace is created. The
latest checkpoint patch for each repository is applied and committed before the
agent resumes with its persistent session/event context.

### Uncheckpointed changes
They can be lost when the *entire* environment disappears. The runtime therefore
refuses to complete an edited task until a checkpoint exists.

## Performance path

The architecture attacks cold-start latency in different layers:

1. repository bare-mirror cache;
2. reference clones from that mirror;
3. warmed sandbox image;
4. shared dependency caches for pip/npm/Maven/Gradle;
5. reusable code index keyed by repo+commit;
6. persistent session state so a restart does not restart understanding;
7. optional warm Docker pool for trusted single-tenant experiments.

The historical "~20-30 min to ~10 min" story should only be quoted when backed by
actual measurements. This repo implements the mechanisms, not those proprietary
measurements.

## Production boundary

For a real multi-tenant deployment, implement `EnvironmentProvider` against
DevPod/Kubernetes/cloud sandboxes with per-task persistent volumes, scoped network
egress and a secrets broker. The task/session/agent APIs do not need to change.
