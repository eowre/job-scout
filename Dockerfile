# ---------------------------------------------------------------------------
# Job Scout — Dockerfile
#
# Base: official Playwright Python image (ships Chromium + all system deps)
# Node: added for generate_resume.js (docx npm package)
# ---------------------------------------------------------------------------

FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# ── System: Node.js 20 LTS ─────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y nodejs \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# ── Working directory ───────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ─────────────────────────────────────────────────────
# Copy requirements first so Docker layer cache is reused on code-only changes
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browsers are pre-installed in the base image (Chromium).
# Run install to make sure they're linked to this Python version's playwright.
RUN playwright install chromium --with-deps

# ── Node.js dependencies ─────────────────────────────────────────────────────
COPY package.json package-lock.json* ./
RUN npm install --omit=dev

# ── Application source ───────────────────────────────────────────────────────
COPY . .

# ── Runtime directories (DB + file uploads/generated docs) ──────────────────
# These are declared as VOLUME mount points so docker-compose / -v flags
# can persist them outside the container.
RUN mkdir -p /app/uploads /app/generated
VOLUME ["/app/uploads", "/app/generated", "/app/data"]

# ── Environment defaults ─────────────────────────────────────────────────────
# The real API key is injected at runtime via docker-compose env_file or -e flag.
# DATABASE_URL points at the persistent /app/data volume so the SQLite file
# survives container restarts.
ENV DATABASE_URL="sqlite:////app/data/jobscout.db" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ── Expose port ──────────────────────────────────────────────────────────────
EXPOSE 8000

# ── Entrypoint ───────────────────────────────────────────────────────────────
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
