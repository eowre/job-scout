import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Job keyword filters
# ---------------------------------------------------------------------------
JOB_KEYWORDS = [
    "solution engineer",
    "solutions engineer",
    "solutions architect",
    "software engineer",
    "software developer",
    "site reliability engineer",
    "sre",
    "devops engineer",
    "platform engineer",
    "backend engineer",
    "full stack engineer",
    "full-stack engineer",
    "infrastructure engineer",
    "systems engineer",
    "application engineer",
    "implementation engineer",
    "integration engineer",
    "technical account",
    "forward deployed",
]

# If a title contains ANY of these, skip it (even if it matched above).
EXCLUDE_KEYWORDS: list[str] = [
    # "intern",
    # "staff",
    # "principal",
    # "director",
    # "manager",
    # "sales",
]

# ---------------------------------------------------------------------------
# Scraping behaviour
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT_SECS = 15
# Lower this to reduce CPU spikes during scans — each unit is one concurrent
# Playwright browser context or ATS API call running at the same time.
# 15 = aggressive (fast scans, high CPU), 5 = conservative (slower, gentler)
MAX_CONCURRENT_REQUESTS = 8
CHECK_INTERVAL_HOURS = 2

# ---------------------------------------------------------------------------
# Notifications  (set in .env)
# ---------------------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
SLACK_WEBHOOK_URL   = os.getenv("SLACK_WEBHOOK_URL", "")
