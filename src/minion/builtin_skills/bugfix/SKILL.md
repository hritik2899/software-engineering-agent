---
name: bugfix
description: Evidence-first debugging workflow for regressions and production defects.
markers: []
keywords:
  - bug
  - fix
  - regression
  - failing
  - broken
priority: 5
---
Reproduce or establish concrete evidence before changing code. Use repository_search,
symbol_context and impact_analysis to map the failing path. Form a minimal hypothesis,
make the smallest patch that addresses the root cause, add or update a regression test,
then run focused verification and inspect git_diff before finishing.
