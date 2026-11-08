# Repository instructions

- Application code lives in the root-level `advisor/` module (local meeting sessions, model decisions). `meeting-listener/` is DISABLED (kept for archive; see plan.md). Each module owns its configuration, virtual environment, tests and `pyproject.toml`. Root `integrations/` contains integration tests; `data/` holds local runtime data. The active architecture plan and frozen contracts are in `plan.md` and `docs/CONTRACT_*.md`.
- When the user says to "add a TODO" or “加个 TODO”, only add the requested item to the relevant `TODO.md` file, and Do NOT implement the TODO in that turn.
