import os

from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, Boolean, Date, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

# Local default:  ./jobscout.db (relative to CWD)
# Docker default: /app/data/jobscout.db  (set via DATABASE_URL env var)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./jobscout.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Resume(Base):
    __tablename__ = "resumes"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)
    raw_text = Column(Text, nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    company = Column(String, default="")
    jd_text = Column(Text, nullable=False, default="")
    stage = Column(String, default="Saved")
    fit_score = Column(Float, nullable=True)
    fit_breakdown = Column(Text, nullable=True)
    tailored_bullets = Column(Text, nullable=True)
    original_bullets = Column(Text, nullable=True)
    tailored_resume_text = Column(Text, nullable=True)
    generated_resume_path = Column(String, nullable=True)
    generated_pdf_path = Column(String, nullable=True)
    contact_info = Column(Text, default="")
    notes = Column(Text, default="")
    resume_id = Column(Integer, nullable=True)
    ats_job_id = Column(String, nullable=True, index=True)
    source = Column(String, default="manual")
    job_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DiscoveredJob(Base):
    """Every job found by the scraper lands here before any user action."""
    __tablename__ = "discovered_jobs"

    id = Column(Integer, primary_key=True, index=True)
    ats_job_id = Column(String, unique=True, nullable=False, index=True)
    company = Column(String, nullable=False)
    title = Column(String, nullable=False)
    url = Column(String, nullable=False)
    location = Column(String, default="")
    department = Column(String, default="")
    # ATS-provided date (when the role was posted)
    posted_date = Column(DateTime, nullable=True)
    # Raw description fetched from ATS
    raw_description = Column(Text, default="")
    # Claude-parsed fields (populated async after discovery)
    parsed_summary = Column(Text, nullable=True)
    compensation = Column(String, nullable=True)
    responsibilities = Column(Text, nullable=True)  # JSON array
    years_of_experience = Column(Float, nullable=True)
    parsed_at = Column(DateTime, nullable=True)
    # Pipeline promotion
    added_to_pipeline = Column(Boolean, default=False)
    pipeline_job_id = Column(Integer, nullable=True)
    # scouted_at is the canonical name; discovered_at kept as column name for compat
    discovered_at = Column(DateTime, default=datetime.utcnow)


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id = Column(Integer, primary_key=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    companies_scraped = Column(Integer, default=0)
    jobs_found = Column(Integer, default=0)
    jobs_new = Column(Integer, default=0)
    # JSON list of company names that returned 0 keyword-matching jobs this run —
    # powers the "zero-result companies" RCA report in the analytics UI.
    zero_result_companies = Column(Text, nullable=True)


class Setting(Base):
    """Key/value store for runtime-configurable app settings."""
    __tablename__ = "settings"

    key   = Column(String, primary_key=True)
    value = Column(Text, nullable=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

_SETTING_DEFAULTS = {
    "parser_backend":  "ollama",
    "ollama_model":    "llama3.1:8b",
    "ollama_base_url": "http://host.docker.internal:11434",
    "anthropic_model": "claude-haiku-4-5-20251001",
}


def load_settings() -> dict:
    """Return all settings as a plain dict, falling back to defaults."""
    db = SessionLocal()
    try:
        rows = db.query(Setting).all()
        result = dict(_SETTING_DEFAULTS)
        result.update({r.key: r.value for r in rows})
        return result
    finally:
        db.close()


def save_setting(key: str, value: str) -> None:
    """Upsert a single setting."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == key).first()
        if row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value))
        db.commit()
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate()
    _seed_settings()


def _migrate():
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    if "discovered_jobs" in tables:
        existing = {col["name"] for col in inspector.get_columns("discovered_jobs")}
        dj_migrations = {
            "posted_date":         "ALTER TABLE discovered_jobs ADD COLUMN posted_date DATETIME",
            "years_of_experience": "ALTER TABLE discovered_jobs ADD COLUMN years_of_experience REAL",
        }
        with engine.connect() as conn:
            for col, sql in dj_migrations.items():
                if col not in existing:
                    conn.execute(text(sql))
                    conn.commit()

    if "scrape_runs" in tables:
        existing = {col["name"] for col in inspector.get_columns("scrape_runs")}
        sr_migrations = {
            "zero_result_companies": "ALTER TABLE scrape_runs ADD COLUMN zero_result_companies TEXT",
        }
        with engine.connect() as conn:
            for col, sql in sr_migrations.items():
                if col not in existing:
                    conn.execute(text(sql))
                    conn.commit()

    if "settings" not in tables:
        # Table will be created by create_all above; nothing to migrate
        pass

    if "jobs" in tables:
        existing = {col["name"] for col in inspector.get_columns("jobs")}
        migrations = {
            "tailored_resume_text":  "ALTER TABLE jobs ADD COLUMN tailored_resume_text TEXT",
            "generated_resume_path": "ALTER TABLE jobs ADD COLUMN generated_resume_path TEXT",
            "generated_pdf_path":    "ALTER TABLE jobs ADD COLUMN generated_pdf_path TEXT",
            "ats_job_id":            "ALTER TABLE jobs ADD COLUMN ats_job_id TEXT",
            "source":                "ALTER TABLE jobs ADD COLUMN source TEXT DEFAULT 'manual'",
            "job_url":               "ALTER TABLE jobs ADD COLUMN job_url TEXT",
        }
        with engine.connect() as conn:
            for col, sql in migrations.items():
                if col not in existing:
                    conn.execute(text(sql))
                    conn.commit()


def _seed_settings() -> None:
    """Write default values for any settings not yet in the DB.

    env vars take precedence over the hard-coded defaults so existing
    deployments keep their current .env behaviour on first boot.
    """
    import os
    env_overrides = {
        "parser_backend":  os.getenv("PARSER_BACKEND"),
        "ollama_model":    os.getenv("OLLAMA_MODEL"),
        "ollama_base_url": os.getenv("OLLAMA_BASE_URL"),
        "anthropic_model": os.getenv("ANTHROPIC_MODEL"),
    }
    db = SessionLocal()
    try:
        existing_keys = {r.key for r in db.query(Setting.key).all()}
        for key, default in _SETTING_DEFAULTS.items():
            if key not in existing_keys:
                value = env_overrides.get(key) or default
                db.add(Setting(key=key, value=value))
        db.commit()
    finally:
        db.close()
