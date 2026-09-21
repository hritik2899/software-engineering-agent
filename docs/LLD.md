# Low-Level Design

## Component map

```text
FastAPI
 |
 +-- Orchestrator
 |    +-- WorkQueue
 |    +-- AuthorizationPolicy
 |    +-- EnvironmentProvider
 |    +-- GitHubPublisher
 |
 +-- TaskRepository
 +-- SessionRepository
 +-- EventStore
 +-- EventBus

EnvironmentProvider
 |
 +-- LocalEnvironmentProvider
 +-- DockerEnvironmentProvider
 |
 +--> Workspace
       +-- environment_id
       +-- root
       +-- repositories
       +-- container_name

CodingAgent
 |
 +-- ContextManager
 +-- LLMClient
 +-- ToolRegistry
      +-- list_files
      +-- read_file
      +-- write_file
      +-- search_code
      +-- run_command
      +-- git_status
      +-- git_diff
      +-- checkpoint
```

## Task state machine

```text
CREATED -> QUEUED -> PROVISIONING -> RUNNING -> COMPLETED
                     |              |
                     |              +-> WAITING_FOR_USER -> RUNNING
                     |              |
                     +--------------+-> FAILED -> QUEUED (explicit retry)

Any active state -> CANCELLING -> CANCELLED
```

Optimistic `version` updates prevent duplicate queue deliveries from starting the
same logical task twice.

## Core tables

### tasks
`id, session_id, environment_id, user_id, instruction, status, repositories,
publish_pr, version, error, result, created_at, updated_at`

### agent_sessions
`id, task_id, summary, current_plan, active_constraints, last_event_sequence,
created_at, updated_at`

### events
`id, task_id, session_id, sequence, type, payload, created_at`

The unique `(task_id, sequence)` index provides replay ordering.

## Agent versus runtime

The **agent** answers: *what should I do next?*

The **runtime** answers: *how do I execute that safely and preserve enough state to
continue?*

```text
ContextManager -> LLM -> tool decision
                      |
                      v
                ToolRegistry
                      |
        +-------------+-------------+
        |             |             |
      files          shell          git
```

## Adding a real DevPod provider

Implement:

```python
class DevPodEnvironmentProvider(EnvironmentProvider):
    async def prepare(self): ...
    async def allocate(self, task_id, repositories): ...
    async def attach(self, environment_id, repositories): ...
    async def healthy(self, workspace): ...
    async def release(self, workspace): ...
```

The rest of the control plane and agent runtime does not change. A production
implementation would map `environment_id` to the DevPod/Kubernetes workload ID,
mount a persistent or checkpointed workspace, use scoped repo credentials, emit
heartbeats, and maintain a warm pool keyed by environment/toolchain class.

## Security boundaries

* API/IAM decides which user may access which repositories.
* LLM never receives GitHub credentials directly.
* PR publishing is a separate backend service.
* Filesystem tools reject path escape.
* Docker provider executes arbitrary build/test shell commands inside the sandbox.
* Production should additionally apply network egress policy, CPU/memory quotas,
  seccomp/AppArmor, secret scoping and auditable tool policies.
