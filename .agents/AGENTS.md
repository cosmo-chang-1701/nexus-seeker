# Nexus Seeker - Custom Agent Rules

## Strict Type Annotations for Empty Collections
- **Always provide explicit type annotations** when initializing empty collections (e.g., `set`, `list`, `dict`).
  - Correct: `_my_set: set[str] = set()`
  - Incorrect: `_my_set = set()`
- Adhere to `mypy` strict typing standards across the entire project to prevent `[var-annotated]` and similar errors.

## Documentation Updates Exemption from Testing
- **Pure documentation updates** (e.g., modifying only `AGENTS.md`, `README.md`, or `.md` files under `docs/`) **do not require running test suites or containerized testing**.
- Only run tests when modifying or adding Python code, configuration files, or database schemas.
