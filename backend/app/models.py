from sqlalchemy import Integer, String, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base

class CandidateProfile(Base):
    __tablename__ = "candidate_profile"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    full_name: Mapped[str] = mapped_column(String(160), default="")
    email: Mapped[str] = mapped_column(String(254), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    location: Mapped[str] = mapped_column(String(160), default="")
    experience_years: Mapped[str] = mapped_column(String(40), default="")
    current_employer: Mapped[str] = mapped_column(String(160), default="")
    current_role: Mapped[str] = mapped_column(String(160), default="")
    skills: Mapped[str] = mapped_column(Text, default="")
    preferred_roles: Mapped[str] = mapped_column(Text, default="")
    preferred_locations: Mapped[str] = mapped_column(Text, default="")
    expected_compensation: Mapped[str] = mapped_column(String(100), default="")
    notice_period: Mapped[str] = mapped_column(String(100), default="")
    resume_path: Mapped[str] = mapped_column(Text, default="")
    application_preferences: Mapped[str] = mapped_column(Text, default="{}")
    resume_text: Mapped[str] = mapped_column(Text, default="")
    resume_filename: Mapped[str] = mapped_column(String(255), default="")


class JobListing(Base):
    __tablename__ = "job_listings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    company: Mapped[str] = mapped_column(String(240), default="")
    location: Mapped[str] = mapped_column(String(300), default="")
    source: Mapped[str] = mapped_column(String(80), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[str] = mapped_column(String(80), default="")
    work_mode: Mapped[str] = mapped_column(String(40), default="")
    salary: Mapped[str] = mapped_column(String(160), default="")
    match_score: Mapped[int] = mapped_column(Integer, default=0)
    saved_at: Mapped[str] = mapped_column(String(40), default="")


class ApplicationRun(Base):
    __tablename__ = "application_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job_listings.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    step: Mapped[str] = mapped_column(String(120), default="queued")
    message: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), default="")
    updated_at: Mapped[str] = mapped_column(String(40), default="")
    submitted_at: Mapped[str] = mapped_column(String(40), default="")
