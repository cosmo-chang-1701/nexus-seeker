import os
import time
import sys

HEALTH_FILE = "/tmp/bot_healthy"

def main():
    if not os.path.exists(HEALTH_FILE):
        print("Health file not found.", file=sys.stderr)
        sys.exit(1)
        
    mtime = os.path.getmtime(HEALTH_FILE)
    if time.time() - mtime > 120:
        print("Health file is outdated.", file=sys.stderr)
        sys.exit(1)
        
    print("Bot is healthy.")
    sys.exit(0)

if __name__ == "__main__":
    main()
