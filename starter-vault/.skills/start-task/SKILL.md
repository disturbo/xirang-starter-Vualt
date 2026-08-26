---
name: start-task
description: Start a task in a persistent knowledge workspace by locating current rules, translating the goal, and establishing a safe execution boundary. Use when new work may read or change the Vault, project files, standards, deliverables, or automation.
---

# Start Task

Establish enough current context to work safely without loading the whole knowledge base.

1. Read the workspace `AGENTS.md`, `.xirang/adapters/PROTOCOL.md` when present, `🏠-Home.md`, and the relevant project or topic entry.
2. Translate the request into an objective, expected deliverable, acceptance condition, and explicit exclusions. Mark assumptions and missing facts.
3. Identify whether the task changes infrastructure, platform configuration, temporary task artifacts, or project content.
4. Keep read-only requests read-only. Before any side effect, show the exact write scope, operation types, external effects, recovery path, and acceptance owner.
5. If a XiRang StateStore is active, use its existing task and envelope interfaces. Do not invent task IDs or treat Markdown projections as authority.
6. Read only the source files, standards, and Skills needed for the selected work.

Return a compact start summary with objective, current evidence, target paths, exclusions, next action, and blockers.
