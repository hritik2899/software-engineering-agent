---
name: python-testing
description: Reliable Python changes with focused pytest and static verification.
markers:
  - pyproject.toml
  - requirements.txt
keywords:
  - python
  - pytest
  - fastapi
priority: 20
---
Before editing, identify the smallest Python module and tests affected by the task.

Prefer:
1. repository_search or symbol_context to find definitions and callers;
2. apply_patch for existing Python files;
3. the narrowest relevant pytest target first, followed by the broader suite;
4. Ruff/type checks when the repository already configures them.

Do not add a new testing framework when the repository already has one. Preserve
async/sync conventions and public type contracts unless the task explicitly changes them.
