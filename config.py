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
MAX_CONCURRENT_REQUESTS = 15
CHECK_INTERVAL_HOURS = 2

# ---------------------------------------------------------------------------
# Notifications  (set in .env)
# ---------------------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
SLACK_WEBHOOK_URL   = os.getenv("SLACK_WEBHOOK_URL", "")
