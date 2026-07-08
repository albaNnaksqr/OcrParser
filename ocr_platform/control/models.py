from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: Optional[datetime], dialect) -> Optional[datetime]:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="offline", nullable=False)
    capacity_slots: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    capabilities_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    archived_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)

    jobs: Mapped[list["Job"]] = relationship(back_populates="assigned_server")


class ModelProfile(Base):
    __tablename__ = "model_profiles"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    engine: Mapped[str] = mapped_column(String(64), nullable=False)
    ip: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    page_concurrency: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    extra_args_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_key_env_var: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    requires_api_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status_created", "status", "created_at"),
        Index("ix_jobs_archived_created", "archived_at", "created_at"),
        Index("ix_jobs_archived_status_created", "archived_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    input_dir: Mapped[str] = mapped_column(Text, nullable=False)
    output_dir: Mapped[str] = mapped_column(Text, nullable=False)
    engine: Mapped[str] = mapped_column(String(64), nullable=False)
    input_mode: Mapped[str] = mapped_column(String(64), default="directory", nullable=False)
    model_profile_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    manifest_root: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_files_per_shard: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    max_shard_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    engine_config: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    assigned_server_id: Mapped[str] = mapped_column(ForeignKey("servers.id"), nullable=False)
    allowed_server_ids_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    failure_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    page_concurrency: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    extra_args_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    command_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    force_reprocess: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    archived_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)

    assigned_server: Mapped[Server] = relationship(back_populates="jobs")
    files: Mapped[list["JobFile"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    events: Mapped[list["JobEvent"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    logs: Mapped[list["JobLog"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    counter: Mapped[Optional["JobCounter"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    manifests: Mapped[list["Manifest"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    work_shards: Mapped[list["WorkShard"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    shard_attempts: Mapped[list["ShardAttempt"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    scan_units: Mapped[list["ScanUnit"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Manifest(Base):
    __tablename__ = "manifests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    input_mode: Mapped[str] = mapped_column(String(64), nullable=False)
    input_root: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    manifest_path: Mapped[str] = mapped_column(Text, nullable=False)
    meta_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    next_shard_index: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    scanner_version: Mapped[str] = mapped_column(String(32), default="1", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ready", nullable=False)
    frozen_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    freeze_report_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    worker_integrity_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    worker_integrity_requested_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    worker_integrity_started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    worker_integrity_finished_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    worker_integrity_server_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    worker_integrity_report_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)

    job: Mapped[Job] = relationship(back_populates="manifests")
    shards: Mapped[list["WorkShard"]] = relationship(
        back_populates="manifest",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class WorkShard(Base):
    __tablename__ = "work_shards"
    __table_args__ = (
        Index("ix_work_shards_job_status_index", "job_id", "status", "shard_index"),
        Index("ix_work_shards_job_server_status", "job_id", "assigned_server_id", "status"),
        Index("ux_work_shards_job_index", "job_id", "shard_index", unique=True),
        Index("ux_work_shards_manifest_index", "manifest_id", "shard_index", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    manifest_id: Mapped[int] = mapped_column(ForeignKey("manifests.id", ondelete="CASCADE"), nullable=False)
    shard_index: Mapped[int] = mapped_column(Integer, nullable=False)
    shard_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    assigned_server_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    api_inflight: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    api_inflight_peak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    api_waiting: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    oldest_api_inflight: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    execution_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    api_concurrency_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    execution_control_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)

    job: Mapped[Job] = relationship(back_populates="work_shards")
    manifest: Mapped[Manifest] = relationship(back_populates="shards")
    attempts: Mapped[list["ShardAttempt"]] = relationship(
        back_populates="shard",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ShardAttempt(Base):
    __tablename__ = "shard_attempts"
    __table_args__ = (
        Index("ux_shard_attempts_shard_attempt", "shard_id", "attempt_number", unique=True),
        Index("ix_shard_attempts_job_status", "job_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    shard_id: Mapped[int] = mapped_column(ForeignKey("work_shards.id", ondelete="CASCADE"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    server_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    processed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    execution_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    api_concurrency_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    execution_control_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)

    job: Mapped[Job] = relationship(back_populates="shard_attempts")
    shard: Mapped[WorkShard] = relationship(back_populates="attempts")


class ScanUnit(Base):
    __tablename__ = "scan_units"
    __table_args__ = (
        Index("ix_scan_units_job_status", "job_id", "status"),
        Index("ux_scan_units_job_path", "job_id", "path", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    assigned_server_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    manifest_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    failure_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)

    job: Mapped[Job] = relationship(back_populates="scan_units")


class JobFile(Base):
    __tablename__ = "job_files"
    __table_args__ = (
        Index("ix_job_files_job_status", "job_id", "status"),
        Index("ix_job_files_job_path", "job_id", "file_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    total_pages: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    done_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)

    job: Mapped[Job] = relationship(back_populates="files")


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (
        Index("ix_job_events_job_created", "job_id", "created_at"),
        Index("ix_job_events_job_failure_created", "job_id", "failure_category", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    file_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    page_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    failure_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)

    job: Mapped[Job] = relationship(back_populates="events")


class JobCounter(Base):
    __tablename__ = "job_counters"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    started_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    degraded_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recent_failed_files_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    recent_errors_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    failure_category_counts_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    first_event_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    last_event_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)

    job: Mapped[Job] = relationship(back_populates="counter")


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    server_id: Mapped[str] = mapped_column(String(128), nullable=False)
    stream: Mapped[str] = mapped_column(String(16), nullable=False)
    line: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)

    job: Mapped[Job] = relationship(back_populates="logs")
