---
trigger: model_decision
description: When working on all test case.
---

# Testing Environment
- Testing Framework & Execution: Use Python's built-in unittest framework for Python testing, and ensure tests are executed within the Docker container when applicable to maintain environmental consistency.
- Test command:
docker compose run --rm -v "$(pwd):/app" nexus_seeker python tests/{file_name}