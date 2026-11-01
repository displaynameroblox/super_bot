import os

# Tests exercise main() entry points; they must not trigger the start-up test run.
os.environ["SUPER_BOT_SKIP_TESTS"] = "1"
