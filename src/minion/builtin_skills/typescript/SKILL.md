---
name: typescript
description: TypeScript/JavaScript changes with package-aware validation.
markers:
  - package.json
keywords:
  - typescript
  - javascript
  - react
  - node
  - npm
  - pnpm
priority: 20
---
Inspect package.json and the lockfile before choosing commands. Reuse the repository's
package manager and scripts. Prefer typed changes over broad any casts, preserve
existing lint/format conventions, and run the smallest affected test plus the relevant
typecheck/lint script when available.
