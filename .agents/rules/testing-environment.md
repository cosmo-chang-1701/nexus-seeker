---
trigger: always_on
---

---
trigger: always_on
description: When working on all test case.
---

# Testing Environment & Standards

- **Framework**: Use Python `unittest` for all test suites.
- **Consistency**: All tests **must** be executed within the Docker container to ensure environment parity.
- **Execution Context**: 
  - **Root Directory**: `/app` (mapped from host `$(pwd)`)
  - **Command Pattern**: 
    ```bash
    docker compose run --rm -v "$(pwd):/app" nexus_seeker python -m unittest tests.{file_name}
    ```
- **Requirements**:
  - Ensure all dependencies are resolved via the `nexus_seeker` service definition.
  - Use **mock objects** where external API calls are involved to maintain test isolation.
  - If multiple files are tested, provide the specific execution command for each individual file.