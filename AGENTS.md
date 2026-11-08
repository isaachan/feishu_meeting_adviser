# Repository instructions

- Application modules live at the repo root: `advisor/` (local meeting sessions, decisions, remote model backend), `local-meeting-recorder/` (fake replay now, real capture deferred) and `llm-gateway/` (localhost model execution, OpenCode only, no auth by design). `meeting-listener/` is DISABLED (kept for archive; see plan.md). Each module owns its configuration, virtual environment, tests and `pyproject.toml`. Root `integrations/` contains integration tests; `data/` holds local runtime data. The active architecture plan and frozen contracts are in `plan.md` and `docs/CONTRACT_*.md`.
- When the user says to "add a TODO" or “加个 TODO”, only add the requested item to the relevant `TODO.md` file, and Do NOT implement the TODO in that turn.
