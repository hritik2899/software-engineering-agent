# Minion-Style Software Engineering Agent

A runnable, production-oriented reference implementation of a **remote agentic
software-engineering platform**. It converts a natural-language engineering task
into repository investigation, code edits, build/test execution, Git checkpoints
and optionally GitHub pull requests.

This project was built specifically as an architecture-learning codebase: every
important Minion-style concept discussed in the design is represented by a concrete
interface, model or execution path.

> This is an educational reference implementation. It is not Uber source code and
> does not claim to reproduce proprietary Minion internals.

## Features

- FastAPI control plane and REST API
- persistent task/session/event state with SQLite or PostgreSQL
- asynchronous Redis or in-memory task queue
- explicit task state machine + optimistic idempotency
- local and Docker-isolated execution environments
- replaceable `environmentId` with startup reconciliation/recovery
- multi-repository workspaces
- OpenAI-backed autonomous coding-agent loop
- bounded context manager over durable history
- file, search, shell/build/test and Git tools
- Git checkpoint recovery boundary
- replayable bidirectional WebSocket event stream
- mid-execution user instructions
- repository mirror cache
- shared pip/npm/Maven/Gradle dependency caches
- warmed Docker image
- optional GitHub branch push + pull-request publishing
- CI tests and architecture/LLD documentation

## Read these first

1. [Architecture / HLD](docs/ARCHITECTURE.md)
2. [Low-Level Design](docs/LLD.md)
3. `src/minion/domain.py` — task/session/environment mental model
4. `src/minion/orchestrator.py` — lifecycle, scheduling, recovery
5. `src/minion/runtime/agent.py` — reasoning/action loop
6. `src/minion/runtime/context.py` — model context construction
7. `src/minion/runtime/workspace.py` — environment + repo caches
8. `src/minion/runtime/tools.py` — real code/shell/Git actions
9. `src/minion/events.py` — durable replay + live streaming
10. `src/minion/api.py` — external control plane

## Quick start

Requires Python 3.11+ and Git.

```bash
git clone https://github.com/hritik2899/software-engineering-agent.git
cd software-engineering-agent
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Put your API key in `.env`:

```bash
OPENAI_API_KEY=...
MINION_LLM_MODEL=gpt-5
```

Run:

```bash
uvicorn minion.api:app --reload
```

Open API docs:

```text
http://localhost:8000/docs
```

### Create a task

```bash
curl -X POST http://localhost:8000/tasks \
  -H 'content-type: application/json' \
  -d '{
    "instruction": "Add a README section explaining how to run the tests",
    "repositories": [{
      "url": "https://github.com/YOUR_USER/YOUR_TEST_REPO.git",
      "base_branch": "main",
      "name": "demo"
    }],
    "publish_pr": false
  }'
```

Use a disposable repository first.

### Watch events

```bash
curl 'http://localhost:8000/tasks/TASK_ID/events?after=0'
```

WebSocket:

```text
ws://localhost:8000/tasks/TASK_ID/events/ws?after=0
```

### Redirect an agent while it runs

```bash
curl -X POST http://localhost:8000/tasks/TASK_ID/messages \
  -H 'content-type: application/json' \
  -d '{"message":"Do not modify the legacy implementation; use the new adapter."}'
```

The message is durably appended to the task event stream and appears in the next
context window.

## Docker sandbox mode

Build the sandbox image:

```bash
make sandbox
```

Then set:

```bash
MINION_ENVIRONMENT_PROVIDER=docker
MINION_DOCKER_IMAGE=minion-sandbox:latest
```

In Docker mode repository files are bind-mounted but arbitrary build/test commands
run inside the isolated container. Shared dependency cache mounts accelerate repeat
tasks.

## PostgreSQL + Redis

```bash
docker compose up -d postgres redis
```

Then:

```bash
MINION_DATABASE_URL=postgresql+asyncpg://minion:minion@localhost:5432/minion
MINION_REDIS_URL=redis://localhost:6379/0
```

Redis is an accelerator/queue, not the source of truth.

## PR publishing

Set `GITHUB_TOKEN`, make sure Git credentials can push the task branch, and submit
with `"publish_pr": true`. The LLM never receives the GitHub token; publication is
owned by the backend `GitHubPublisher`.

## Six-commit learning path

The history is intentionally structured as six conceptual layers:

1. repository initialization
2. configuration + domain contracts
3. durable tasks/sessions/events
4. agent runtime + tools/context
5. orchestration + Docker/caches/recovery/API
6. hardening + CI/tests/HLD/LLD/runbook

Read the commits in order to reconstruct the platform the same way you would design
it in a system-design interview.

## Important production notes

The default local provider is **not a security sandbox**. Use Docker for untrusted
commands and implement a Kubernetes/DevPod `EnvironmentProvider` for a real remote
fleet. Production deployments should add organization IAM, network egress controls,
resource quotas, secrets broker, migrations, distributed tracing/metrics, rate
limits and a durable queue with delivery/visibility semantics.

The interfaces in this repository are intentionally designed so those infrastructure
upgrades do not require rewriting the coding agent.
