# Code Walkthrough: Know Where Everything Lives

Use this document when you want to answer interview questions like “where is that
implemented?”, “who owns this state?”, or “what happens after this method returns?”

## Recommended reading order

1. `domain.py` — learn the vocabulary and IDs.
2. `state_machine.py` — learn legal task transitions.
3. `api.py` — see how users enter/control the system.
4. `orchestrator.py` — follow a task from queue to completion.
5. `runtime/workspace.py` — understand sandboxes, worktrees and caches.
6. `runtime/code_index.py` — understand repository pre-indexing.
7. `runtime/skills.py` — understand reusable project guidance.
8. `runtime/context.py` — see exactly what reaches the model.
9. `runtime/agent.py` — follow one reasoning/tool loop.
10. `runtime/tools.py` + `runtime/policy.py` — see actual execution.
11. `events.py` / `repositories.py` / `models.py` — understand recovery.
12. `github.py` — see how verified code becomes a PR.

---

## Root package

### `src/minion/domain.py`

Defines shared Pydantic contracts and enums.

Know these:
- `TaskStatus`: logical task lifecycle.
- `EnvironmentStatus`: physical sandbox lease lifecycle.
- `EventType`: durable audit/replay vocabulary.
- `RepositorySpec`: one repository checked out into a task workspace.
- `TaskCreate` / `TaskView`: API/domain task forms.
- `SessionView`: compact durable working memory.

If asked “what is the difference between task/session/environment?”, start here.

### `src/minion/state_machine.py`

Single source of truth for legal status transitions. The orchestrator does not mutate
status ad hoc.

### `src/minion/config.py`

Typed configuration boundary. Important settings include:
- PostgreSQL/Redis endpoints;
- context/agent limits;
- skills path;
- command-policy mode;
- Docker resource limits;
- queue visibility and worker concurrency;
- GitHub publication settings.

### `src/minion/models.py`

SQLAlchemy durable schema. If asked “what survives a restart?”, inspect these tables.

### `src/minion/repositories.py`

All common task/session/environment SQL operations. `TaskRepository.transition()`
implements optimistic state updates using `TaskRow.version`.

---

## Control plane

### `src/minion/api.py`

FastAPI surface.

Important routes:
- `POST /tasks`;
- `GET /tasks/{id}`;
- `POST /tasks/{id}/messages`;
- `POST /tasks/{id}/retry`;
- `POST /tasks/{id}/cancel`;
- `GET /tasks/{id}/events`;
- `WS /tasks/{id}/events/ws`;
- health/metrics.

The API submits work; it does not run the coding agent inline.

### `src/minion/orchestrator.py`

Central control-plane implementation.

Key flow:
- `start()` prepares Redis queue + warm environments + reconciliation.
- `submit()` creates durable state and enqueues task ID.
- `_worker_loop()` consumes at-least-once deliveries.
- `_execute()` owns the environment/agent lifecycle.
- `_preindex_workspace()` indexes repositories before the first model turn.
- `_heartbeat()` refreshes the sandbox lease.
- `reconcile()` requeues nonterminal tasks after process restart.

If someone asks “where does crash recovery begin?”, answer `reconcile()` plus
`_execute()`.

### `src/minion/queue.py`

Production class: `RedisStreamWorkQueue`.

Important behavior:
- `XADD` on submit;
- consumer-group `XREADGROUP`;
- `XAUTOCLAIM` for stale pending work;
- `XACK` only after execution returns.

### `src/minion/events.py`

`EventStore` is authoritative event persistence. `EventBus` is live fan-out.
The database event is committed before Pub/Sub notification.

### `src/minion/auth.py` + `security.py`

Two different boundaries:
- `security.py`: API caller authentication.
- `auth.py`: allowed repository URL/host policy.

---

## Execution plane

### `src/minion/runtime/workspace.py`

Important structures:
- `Workspace`: environment ID, root, repo-name → path map.
- `LocalEnvironmentProvider`: trusted developer path.
- `DockerEnvironmentProvider`: production reference execution path.
- `WarmSlot`: already-running isolated container slot.

Performance features in this file:
- Git mirror cache;
- branch-per-task checkout;
- shared package caches;
- prestarted containers;
- failed-workspace preservation.

Security features:
- one source slot per container;
- dropped Linux capabilities;
- `no-new-privileges`;
- CPU/memory/PID limits.

### `src/minion/runtime/code_index.py`

Repository intelligence.

Important methods:
- `ensure_index()`: incremental content-addressed build.
- `overview()`: ranked repo map.
- `search()`: cheap relevance lookup.
- `symbol_context()`: definitions + neighboring dependency files.
- `impact_analysis()`: reverse-dependency blast radius.
- `dependency_hints()`: per-file imports/resolved edges.

The crucial optimization is not “cache by commit only”; it is **object reuse by file
content hash across commits**.

### `src/minion/runtime/skills.py`

Reusable Agent Skills.

`SkillManager.discover()` merges:
1. built-ins,
2. user-installed skills,
3. repository skills.

`auto_activate()` scores marker files and task keywords. `render()` loads only the
active bounded set into context.

### `src/minion/runtime/context.py`

Everything the model knows on one step is assembled here.

Control tools:
- `set_plan`;
- `remember_constraint`;
- `list_skills`;
- `activate_skill`;
- `finish_task`.

`maybe_compact()` prevents an unbounded event transcript from becoming an unbounded
prompt.

`finish_task` checks persisted tool evidence before allowing completion.

### `src/minion/runtime/agent.py`

The autonomous loop.

Each iteration:
1. compact if required;
2. append `agent.step`;
3. build context;
4. call model;
5. record model message;
6. announce tools;
7. run pure read-only batches concurrently when safe;
8. run control/mutations sequentially;
9. persist every result;
10. repeat until `finish_task`.

### `src/minion/runtime/tools.py`

Actual model-visible capabilities.

Repository intelligence:
- `repository_overview`;
- `repository_search`;
- `symbol_context`;
- `impact_analysis`;
- `dependency_hints`;
- `index_stats`.

Filesystem/code:
- `list_files`;
- `read_file`;
- `search_code`;
- `write_file`;
- `apply_patch`.

Execution/Git:
- `run_command`;
- `git_status`;
- `git_diff`;
- `checkpoint`.

The Tool object marks whether calls are `parallel_safe` or `mutating`.

### `src/minion/runtime/policy.py`

Deterministic guardrail before shell execution. This file is intentionally independent
of the model prompt—prompt instructions are not a security boundary.

### `src/minion/runtime/mcp_tools.py`

Operator-owned MCP integration. `MCPManager.schemas()` discovers remote tool JSON
Schemas and namespaces them into the model toolset; `call()` routes selected tools
back to the configured Streamable HTTP endpoint. Repository content cannot add an MCP
server.

### `src/minion/runtime/llm.py`

Small provider adapter. The rest of the runtime sees `ModelTurn` and `ToolCall`,
not raw OpenAI SDK objects.

### `src/minion/runtime/factory.py`

Composition root for one running agent: constructs skill manager, context manager,
tool registry, model adapter and coding loop against one workspace/session.

---

## Publication and credentials

### `src/minion/git_auth.py`

Creates ephemeral Git subprocess authentication and strips secrets from
agent-controlled child commands.

### `src/minion/github.py`

Backend publisher:
- push task branch with backend-owned credentials;
- find existing PR;
- otherwise create PR.

The LLM never receives `GITHUB_TOKEN`.

---

## Observability and operations

### `src/minion/metrics.py`

Prometheus counters/gauges/histograms for task and environment lifecycles.

### `src/minion/logging.py`

Structured process logging.

### `migrations/`

Alembic schema history. Run migrations before production startup.

---

## “Where does this happen?” quick lookup

| Question | Answer |
|---|---|
| Task is accepted | `api.create_task` → `Orchestrator.submit` |
| Task gets queued | `queue.RedisStreamWorkQueue.put` |
| Crashed queue work is recovered | `RedisStreamWorkQueue._claim_stale` |
| Sandbox is selected | `DockerEnvironmentProvider.allocate/attach` |
| Repository is cloned/cached | `LocalEnvironmentProvider._populate` |
| Repository is indexed | `Orchestrator._preindex_workspace` → `RepositoryContextIndex.ensure_index` |
| Agent learns project conventions | `SkillManager` → `ContextManager` |
| Prompt is built | `ContextManager.build_messages` |
| LLM is called | `CodingAgent._model_turn` → `OpenAIClient.complete` |
| Tool is selected | model returns `ToolCall` |\n| External MCP tool is discovered/called | `MCPManager.schemas/call` via `ToolRegistry` |
| Read-only tools run in parallel | `CodingAgent._execute_turn_tools` |
| Shell command is blocked/allowed | `CommandPolicy.evaluate` |
| File patch is applied | `ToolRegistry.apply_patch` handler |
| Test/build runs | `ToolRegistry.run_command` handler |
| Progress is persisted | `EventStore.append` |
| History is compacted | `ContextManager.maybe_compact` |
| Agent proves it is done | `ContextManager.execute_control_tool("finish_task")` |
| PR is created | `GitHubPublisher.publish` |
| Process restart recovers work | `Orchestrator.reconcile` |
