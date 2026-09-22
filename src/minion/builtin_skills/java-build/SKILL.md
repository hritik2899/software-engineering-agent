---
name: java-build
description: Java/Kotlin changes that respect Gradle/Maven modules and test boundaries.
markers:
  - pom.xml
  - build.gradle
  - build.gradle.kts
keywords:
  - java
  - kotlin
  - spring
  - gradle
  - maven
priority: 20
---
Use the repository index to identify the owning module before editing.

For Maven, prefer the narrow module/test command instead of rebuilding an unrelated
monorepo. For Gradle, inspect settings.gradle/settings.gradle.kts and module build
files before assuming task names. Preserve package structure, dependency injection
patterns, generated-code boundaries, and existing formatter/checkstyle rules.
