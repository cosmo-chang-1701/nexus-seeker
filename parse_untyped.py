note_none = 0
arg_missing = 0
general_untyped = 0

with open(
    "/home/cosmo_chang/.gemini/antigravity-cli/brain/83977ddf-082d-4415-a87c-c9231725fd2f/.system_generated/tasks/task-145.log"
) as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "no-untyped-def" in line and "error:" in line:
        if 'Use "-> None"' in lines[i + 1] if i + 1 < len(lines) else False:
            note_none += 1
        elif "for one or more arguments" in line:
            arg_missing += 1
        else:
            general_untyped += 1

print(f"note_none: {note_none}")
print(f"arg_missing: {arg_missing}")
print(f"general_untyped: {general_untyped}")
