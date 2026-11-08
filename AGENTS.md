# Repository instructions

- Application code lives in the root-level `advisor/` and `meeting-listener/` modules. Each module owns its configuration, virtual environment, tests and `pyproject.toml`. Root `integrations/` contains integration tests; `data/` holds local runtime data.
- When the user says to "add a TODO" or “加个 TODO”, only add the requested item to the relevant `TODO.md` file, and Do NOT implement the TODO in that turn.
