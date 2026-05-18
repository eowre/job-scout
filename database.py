from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, Boolean, Date, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

DATABASE_URL = "sqlite:///./jobscout.db"

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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate()


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
