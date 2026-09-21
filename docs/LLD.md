# Low-Level Design

## Core objects

```text
TaskRow
  id
  session_id
  environment_id
  instruction
  repositories[]
  status
  version          <- optimistic state transition guard
  result/error

SessionRow
  id
  task_id
  summary
  current_plan[]
  active_constraints[]
  last_event_sequence
  last_compacted_sequence

TaskLeaseRow
  task_id
  owner_id
  expires_at_epoch

EnvironmentLeaseRow
  environment_id
  task_id
  provider/status
  workspace metadata
  last heartbeat

CheckpointRow
  task/session/repo
  commit SHA
  binary patch
```

## Control-plane classes

```text
FastAPI
  |
  +-- Orchestrator
  |     +-- AuthorizationPolicy
  |     +-- WorkQueue
  |     +-- TaskLeaseRepository
  |     +-- EnvironmentProvider
  |     +-- GitHubPublisher
  |
  +-- TaskRepository
  +-- SessionRepository
  +-- EventStore
  +-- EventBus
```

### Orchestrator responsibility

The orchestrator answers **when and where** work runs:

- queue consumption;
- bounded concurrency;
- distributed task lease acquisition;
- heartbeat renewal;
- environment allocate/attach/replace;
- startup/periodic recovery;
- pause/resume/cancel/retry;
- PR publication;
- terminal cleanup.

It does not decide which file to edit.

## Execution-plane classes

```text
Workspace
  environment_id
  repositories{name -> path}
  base_branches{name -> branch}
  optional container identity

CodingAgent
  |
  +-- AgentProfile/router
  +-- ContextManager
  +-- LLMClient
  +-- ToolRegistry
          |
          +-- list_files
          +-- read_file
          +-- write_file
          +-- search_code
          +-- search_index
          +-- dependency_neighbors
          +-- run_command
          +-- update_plan
          +-- git_status
          +-- git_diff
          +-- checkpoint
```

## Agent vs runtime

**Agent:** chooses the next engineering action.

**Runtime:** executes tools, owns context/persistence/control signals and mediates the
environment.

```text
durable memory + repo context
            |
       ContextManager
            |
            v
           LLM
            |
       next tool call
            |
       ToolRegistry
            |
 filesystem / shell / Git
            |
        observation
            +-----------> durable event log
```

## Completion invariant

For a task that calls `write_file`, the runtime will not accept model completion
until all three are true:

1. a command has run successfully (verification);
2. Git was inspected;
3. a checkpoint commit and persisted patch were created.

Prompt instructions alone are not considered a sufficient correctness mechanism.

## Event sequencing

`EventStore.append()` atomically increments the session sequence with
`UPDATE ... RETURNING`, inserts the event, commits it, then publishes it live.

SQL is authoritative. Redis Pub/Sub only lowers UI latency. A reconnecting client
can always request `events?after=N`.

## Pause and redirection

A user instruction is both:

- an immutable event;
- an active durable instruction in `SessionRow.active_constraints`.

Pause sets task state to `WAITING_FOR_USER`. The agent checks that state between
reasoning/tool steps and waits. Resume changes the state back to RUNNING.

## Checkpoint recovery

`checkpoint` performs:

```text
git add/commit
   |
git diff --binary origin/base...HEAD
   |
CheckpointRow(binary_patch)
```

If a workspace is lost:

```text
new checkout
   |
latest patch per repository
   |
git apply --index --3way
   |
recovery commit
   |
persistent session + event history
   |
agent continues
```

## Environment providers

- `LocalEnvironmentProvider`: learning/CI only; not sandboxed.
- `DockerEnvironmentProvider`: per-task container and per-task workspace mount.
- `PooledDockerEnvironmentProvider`: pre-started single-tenant warm-container
  experiment. It shares the workspace parent and is explicitly not multi-tenant.

A production DevPod/Kubernetes provider implements the same five operations:
`prepare`, `allocate`, `attach`, `healthy`, `release`.

## Security principles

- repository/API authorization happens before scheduling;
- GitHub credentials stay in backend Git operations, not LLM tools;
- filesystem paths are confined to the selected repository;
- untrusted commands should use an isolated environment provider;
- Docker sandbox applies CPU/memory/PID limits and `no-new-privileges`;
- production still needs network policy, IAM, secret brokering and tenant-isolated
  storage.
