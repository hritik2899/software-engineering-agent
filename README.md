# Minion-Style Software Engineering Agent

A runnable reference implementation of a distributed remote coding-agent platform.
It takes a natural-language engineering task, checks out one or more repositories,
builds task-specific context, lets an autonomous agent inspect/edit/test the code,
persists progress, survives control-plane failures, and can publish pull requests.

> This is **not Uber source code** and does not claim proprietary Minion internals.
> It is a production-oriented educational implementation of the architecture
> discussed in this repository.

## Start here

- [Feature matrix: exactly what is and is not implemented](docs/FEATURE_MATRIX.md)
- [High-Level Architecture](docs/ARCHITECTURE.md)
- [Low-Level Design](docs/LLD.md)

## Implemented system

```text
UI / API
   |
FastAPI
   +-- SQL task/session/event/checkpoint store
   +-- Redis Streams work queue (optional)
   +-- Redis Pub/Sub live events (optional)
   |
Orchestrator
   +-- distributed execution lease + heartbeat
   +-- recovery scanner
   +-- pause/resume/cancel/retry
   +-- EnvironmentProvider
          |
          +-- local
          +-- per-task Docker
          +-- single-tenant warm Docker pool
                    |
               CodingAgent
                    |
              ContextManager
        +-----------+------------+
        |                        |
 durable memory          repo+commit index
        |                        |
        +-----------+------------+
                    |
                   LLM
                    |
                ToolRegistry
          files / search / shell / Git
                    |
             verification gate
                    |
          durable Git checkpoint
                    |
               optional PR
```

## Quickest proof that it runs

The smoke test does **not** require OpenAI, Redis, Docker or internet access:

```bash
git clone https://github.com/hritik2899/software-engineering-agent.git
cd software-engineering-agent

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

make smoke
```

It creates a temporary Git repository and exercises:

```text
task -> queue -> orchestrator -> task lease -> checkout -> code index
     -> mock LLM -> tools -> file edit -> verification -> Git diff
     -> durable checkpoint -> events -> COMPLETED
```

Run the complete verification suite:

```bash
make verify
```

## Run the API

```bash
cp .env.example .env
uvicorn minion.api:app --reload
```

Open:

```text
http://localhost:8000/docs
```

The default example configuration uses:

```text
MINION_LLM_PROVIDER=mock
MINION_ENVIRONMENT_PROVIDER=local
```

so the control plane is runnable before adding external credentials.

To use a real model:

```text
MINION_LLM_PROVIDER=openai
MINION_LLM_MODEL=<your supported OpenAI model>
OPENAI_API_KEY=...
```

## Submit a task

```bash
curl -X POST http://localhost:8000/tasks \
  -H 'content-type: application/json' \
  -d '{
    "instruction": "Add input validation and regression tests",
    "repositories": [{
      "url": "https://github.com/YOUR_USER/YOUR_TEST_REPO.git",
      "base_branch": "main",
      "name": "demo"
    }],
    "publish_pr": false
  }'
```

Use a disposable repository while learning.

## Inspect and control a running task

```bash
curl http://localhost:8000/tasks/TASK_ID
curl 'http://localhost:8000/tasks/TASK_ID/events?after=0'

curl -X POST http://localhost:8000/tasks/TASK_ID/messages \
  -H 'content-type: application/json' \
  -d '{"message":"Do not modify the legacy implementation; use the new adapter."}'

curl -X POST http://localhost:8000/tasks/TASK_ID/pause
curl -X POST http://localhost:8000/tasks/TASK_ID/resume
curl -X POST http://localhost:8000/tasks/TASK_ID/cancel
curl -X POST http://localhost:8000/tasks/TASK_ID/retry
```

Live stream:

```text
ws://localhost:8000/tasks/TASK_ID/events/ws?after=0
```

If `MINION_API_KEY` is configured, REST requests need
`X-Minion-Api-Key`; WebSocket clients send the same header.

## PostgreSQL + Redis

```bash
docker compose up -d postgres redis
```

Configure:

```text
MINION_DATABASE_URL=postgresql+asyncpg://minion:minion@localhost:5432/minion
MINION_REDIS_URL=redis://localhost:6379/0
```

For a migration-managed deployment:

```bash
alembic upgrade head
```

The pre-0.2 educational release did not have Alembic. If you created an old local
`minion.db`, delete that throwaway DB before running 0.2, or migrate/stamp it
manually instead of expecting `create_all()` to alter existing columns.

## Docker sandbox

Build the polyglot learning sandbox:

```bash
make sandbox
```

Then set:

```text
MINION_ENVIRONMENT_PROVIDER=docker
MINION_DOCKER_IMAGE=minion-sandbox:latest
```

This gives every task a separate Docker container and task workspace. The image
contains Python, Node/npm, Java/Maven and Go tooling; real production systems
normally use toolchain-specific images.

### Warm pool experiment

```text
MINION_ENVIRONMENT_PROVIDER=docker_pool
MINION_WARM_POOL_SIZE=2
```

This pre-starts Docker containers to remove container startup from the task's hot
path. It is intentionally **single-tenant only** because pooled containers mount
the workspace parent. Do not treat it as a multi-tenant security boundary.

## Caching

The implementation has distinct caches for distinct costs:

- bare Git repository mirrors + reference clones;
- warmed execution images/containers;
- pip/npm/Maven/Gradle dependency caches;
- repository context index keyed by origin + commit SHA;
- durable session memory, which avoids starting understanding from zero after a
  control-plane restart.

These are the mechanisms behind the latency-reduction architecture discussion.
This repository does not invent proprietary latency measurements.

## GitHub PR publishing

Set `GITHUB_TOKEN` and submit with `"publish_pr": true`.

The token is used only by backend checkout/push/PR code. It is never exposed as an
agent tool or placed in the model context.

The runtime refuses to complete an edited task until it has successfully:

1. run a verification command;
2. inspected Git state;
3. created a Git checkpoint whose binary patch is persisted.

That invariant ensures the branch has a durable commit before PR publication.

## Observability

- structured JSON logs;
- `GET /health`;
- `GET /ready`;
- `GET /metrics` in Prometheus format;
- task/environment allocation and duration metrics;
- persistent event history for audit/replay.

## Recommended reading order

1. `docs/FEATURE_MATRIX.md`
2. `docs/ARCHITECTURE.md`
3. `src/minion/domain.py`
4. `src/minion/models.py`
5. `src/minion/orchestrator.py`
6. `src/minion/runtime/workspace.py`
7. `src/minion/runtime/intelligence.py`
8. `src/minion/runtime/context.py`
9. `src/minion/runtime/agent.py`
10. `src/minion/runtime/tools.py`
11. `src/minion/events.py`
12. `src/minion/api.py`

Read the repository together with the architecture docs: every important box in the
HLD maps to a concrete class/interface.

## What production still needs

The code is intentionally honest about its boundary. A company deployment should
still replace or extend:

- local Docker with a tenant-isolated Kubernetes/DevPod/cloud provider;
- local authorization policy with enterprise IAM/repository ACLs;
- local secret handling with a secrets broker;
- lightweight symbol/import indexing with compiler-grade call graphs/vector search
  if those capabilities are required;
- local metrics endpoints with the organization's tracing/SLO stack.

See the feature matrix for the precise status of each capability.
