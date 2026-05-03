from pathlib import Path
import sys

log_file = Path("shift_log.txt")
if not log_file.exists():
    print("log file missing")
    sys.exit(1)

if log_file.stat().st_size == 0:
    print("log file empty")
    sys.exit(1)

print("ok")
sys.exit(0)
