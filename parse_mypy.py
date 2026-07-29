import re
from collections import Counter

error_types = Counter()
with open(
    "/home/cosmo_chang/.gemini/antigravity-cli/brain/83977ddf-082d-4415-a87c-c9231725fd2f/.system_generated/tasks/task-145.log"
) as f:
    for line in f:
        match = re.search(r"\[([^\]]+)\]$", line.strip())
        if match:
            error_types[match.group(1)] += 1

for err, count in error_types.most_common():
    print(f"{err}: {count}")
