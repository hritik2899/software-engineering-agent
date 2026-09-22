# Minion — Remote Software Engineering Agent

A runnable, production-oriented backend for long-running autonomous software
engineering tasks.

Minion accepts a natural-language engineering task, creates an isolated repository
workspace, pre-indexes the codebase, loads relevant Agent Skills, lets a tool-calling
model inspect/edit/test the code, persists every meaningful action, survives worker
restarts, and can publish the verified branch as a GitHub pull request.

> This is an independent educational/reference implementation. It is not Uber source
> code and does not claim to reproduce proprietary internals.

## Why this codebase is different

Many coding-agent demos are one loop around an LLM and a shell. This repository also
implements the systems problems that appear when the task lasts minutes or hours:

- durable task/session/event state;
- Redis Streams at-least-once worker delivery;
- startup reconciliation and retry recovery;
- environment leases and heartbeats;
- isolated reusable Docker warm sandboxes;
- multi-repository task workspaces;
- Git mirror and language dependency caches;
- **content-addressed repository indexing**;
- dependency graph + ranked repository map;
- symbol lookup and blast-radius analysis;
- **Agent Skills** with built-in/user/repository scopes;
- bounded context + durable compaction;
- parallel read-only exploration;
- deterministic command safety policy;
- validated patch editing;
- model-independent completion evidence;
- backend-owned GitHub push/PR credentials;
- replayable WebSocket events;
- Prometheus metrics;
- Alembic migrations and CI integration tests.

## Canonical production architecture

The design uses one explicit stack:

```text
FastAPI
   │
   ├── PostgreSQL 16       authoritative task/session/event/lease state
   ├── Redis 7 Streams     worker queue
   └── Redis 7 Pub/Sub     live event fan-out
            │
            ▼
      Orchestrator workers
            │
            ▼
      Docker warm pool
            │
       task workspace
       /      |      \
      /       |       \
 repo index  skills   CodingAgent
                         │
                    OpenAI model
                         │
                    Tool Registry
                         │
                 policy → code/test/Git
                         │
                       GitHub
```

See **[Architecture](docs/ARCHITECTURE.md)** for the full communication/data-flow
diagram and **[LLD](docs/LLD.md)** for class, state, indexing and runtime details.

## Repository indexing

Repositories are indexed **before the first model turn**.

Parsed file information is stored by SHA-256 of file contents, while each Git commit
gets a lightweight manifest. If one file changes between commits, only the new file
content is parsed—the other file objects are reused.

Model-visible index tools:

```text
repository_overview
repository_search
symbol_context
impact_analysis
dependency_hints
index_stats
```

See `src/minion/runtime/code_index.py`.

## Agent Skills

Skills are Markdown instruction bundles with YAML metadata.

```text
src/minion/builtin_skills/*/SKILL.md
~/.minion/skills/*/SKILL.md
<repo>/.minion/skills/*/SKILL.md
```

Built-in examples cover Python testing, Java/Kotlin builds, TypeScript and
evidence-first bug fixing. Marker files and task keywords activate relevant skills,
or the model can call `list_skills` / `activate_skill`.

Repository skills are **instructions only**; they cannot silently execute host code.

See `src/minion/runtime/skills.py`.

## Start locally

Requirements: Python 3.11+, Git.

```bash
git clone https://github.com/hritik2899/software-engineering-agent.git
cd software-engineering-agent

cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Set:

```bash
OPENAI_API_KEY=...
MINION_LLM_MODEL=gpt-5
```

Run:

```bash
uvicorn minion.api:app --reload
```

Open:

```text
http://localhost:8000/docs
```

## Run the production-style local stack

Start PostgreSQL and Redis:

```bash
docker compose up -d postgres redis
```

Build the sandbox:

```bash
make sandbox
```

Configure:

```bash
MINION_DATABASE_URL=postgresql+asyncpg://minion:minion@localhost:5432/minion
MINION_REDIS_URL=redis://localhost:6379/0
MINION_ENVIRONMENT_PROVIDER=docker
MINION_DOCKER_IMAGE=minion-sandbox:latest
MINION_AUTO_CREATE_SCHEMA=false
```

Migrate then serve:

```bash
alembic upgrade head
uvicorn minion.api:app
```

## Submit a task

```bash
curl -X POST http://localhost:8000/tasks \
  -H 'content-type: application/json' \
  -d '{
    "instruction": "Fix the retry bug and add a regression test",
    "repositories": [{
      "url": "https://github.com/YOUR_USER/YOUR_REPO.git",
      "base_branch": "main",
      "name": "service"
    }],
    "publish_pr": false
  }'
```

## Redirect a running task

```bash
curl -X POST http://localhost:8000/tasks/TASK_ID/messages \
  -H 'content-type: application/json' \
  -d '{"message":"Keep the public response schema backwards compatible."}'
```

The instruction becomes a durable active constraint in the session and is included in
future context windows.

## Watch durable progress

Replay:

```bash
curl 'http://localhost:8000/tasks/TASK_ID/events?after=0'
```

Live stream:

```text
ws://localhost:8000/tasks/TASK_ID/events/ws?after=0
```

## Security model

The model is not the security boundary.

- API authentication protects the control plane.
- Repository host policy limits clone targets.
- GitHub/OpenAI/API secrets are scrubbed from agent child commands.
- Shell commands pass deterministic command policy.
- Docker sandboxes have dropped capabilities and resource limits.
- Each warm container sees only its own task source slot.
- PR publication runs in backend code using backend-owned credentials.

## Verification model

The agent cannot finish merely by saying “tests pass”. `finish_task` inspects the
durable event history and requires evidence of:

- a successful `run_command`; and
- a successful `git_diff`.

This makes completion a runtime property rather than an LLM statement.

## Documentation

- [Production Architecture / HLD](docs/ARCHITECTURE.md)
- [Low-Level Design](docs/LLD.md)
- [Code Walkthrough](docs/CODE_WALKTHROUGH.md)
- [Open-Source Agent Benchmark](docs/OPEN_SOURCE_BENCHMARK.md)

## CI gate

Every push verifies:

```text
editable package install
Python compilation
Ruff
Alembic migrations
FastAPI import
pytest unit + integration suite
```

## About “better than Claude Code”

The architecture is intentionally ambitious, but a serious project should not declare
itself superior to another coding agent without measured results. The benchmark doc
defines the next proof points: task success, tokens, latency, recovery, index reuse,
security violations and regression rate.

The goal of this repository is to make those comparisons **measurable**, not to hide
behind feature-count marketing.
