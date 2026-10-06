import os
import subprocess
import sys

root = os.path.dirname(os.path.dirname(__file__))
env = os.environ.copy()
env.update({
    "PYTHONPATH": os.path.join(root, "src"),
    "TRANSFER_CONCURRENCY": "bad",
    "TRANSFER_MODE": "unsupported",
    "FIRST_DAY_FREE": "bad",
    "DAILY_FREE": "-2",
    "MAIN_DATA": "-1001",
    "MOVIE_CHANNELS": "-1002,-1003",
})
code = "import config; print(config.TRANSFER_CONCURRENCY, config.TRANSFER_MODE, config.FIRST_DAY_FREE, config.DAILY_FREE, config.MAIN_CHANNELS)"
result = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True)
if result.returncode != 0:
    raise SystemExit(result.stderr)
expected = "10 auto 3 0 [-1001, -1002, -1003]"
if result.stdout.strip() != expected:
    raise SystemExit(f"unexpected config output: {result.stdout!r}")
print("config environment regression check passed")
