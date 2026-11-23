"""Initial schema: groups, jobs, tasks, batches, events, schedules, schedule_runs.

Same DDL Alembic generated for this revision, so databases it migrated stay compatible.
"""

revision = "0001"
down_revision = None

upgrade = """
CREATE TABLE groups (
    id VARCHAR(16) NOT NULL,
    name VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    management VARCHAR(16) NOT NULL,
    max_workers INTEGER,
    contract JSON NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE jobs (
    id VARCHAR(16) NOT NULL,
    group_id VARCHAR(16),
    name VARCHAR(64) NOT NULL,
    pattern VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL,
    management VARCHAR(16) NOT NULL,
    workers INTEGER NOT NULL,
    partitions INTEGER,
    closed BOOLEAN NOT NULL,
    spec JSON NOT NULL,
    contract JSON NOT NULL,
    message TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    FOREIGN KEY (group_id) REFERENCES groups (id)
);
CREATE INDEX ix_jobs_group_id ON jobs (group_id);
CREATE INDEX ix_jobs_status ON jobs (status);

CREATE TABLE tasks (
    id VARCHAR(32) NOT NULL,
    job_id VARCHAR(16) NOT NULL,
    batch_id VARCHAR(32),
    partition INTEGER,
    status VARCHAR(16) NOT NULL,
    attempts INTEGER NOT NULL,
    payload JSON,
    result JSON,
    error TEXT,
    worker VARCHAR(128),
    submitted_at TIMESTAMP WITH TIME ZONE NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    FOREIGN KEY (job_id) REFERENCES jobs (id)
);
CREATE INDEX ix_tasks_job_id ON tasks (job_id);
CREATE INDEX ix_tasks_batch_id ON tasks (batch_id);
CREATE INDEX ix_tasks_status ON tasks (status);

CREATE TABLE batches (
    id VARCHAR(32) NOT NULL,
    job_id VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL,
    size INTEGER NOT NULL,
    partitions INTEGER NOT NULL,
    completed_partitions INTEGER NOT NULL,
    result JSON,
    error TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    duration FLOAT,
    PRIMARY KEY (id),
    FOREIGN KEY (job_id) REFERENCES jobs (id)
);
CREATE INDEX ix_batches_job_id ON batches (job_id);

CREATE TABLE events (
    id SERIAL NOT NULL,
    ts TIMESTAMP WITH TIME ZONE NOT NULL,
    job_id VARCHAR(16),
    group_id VARCHAR(16),
    kind VARCHAR(32) NOT NULL,
    message TEXT NOT NULL,
    data JSON,
    PRIMARY KEY (id)
);
CREATE INDEX ix_events_ts ON events (ts);
CREATE INDEX ix_events_job_id ON events (job_id);
CREATE INDEX ix_events_group_id ON events (group_id);

CREATE TABLE schedules (
    id VARCHAR(16) NOT NULL,
    name VARCHAR(64) NOT NULL,
    cron VARCHAR(128) NOT NULL,
    timezone VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    concurrency_policy VARCHAR(16) NOT NULL,
    starting_deadline_seconds INTEGER,
    group_id VARCHAR(16),
    job JSON NOT NULL,
    next_run_at TIMESTAMP WITH TIME ZONE,
    last_run_at TIMESTAMP WITH TIME ZONE,
    last_job_id VARCHAR(16),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id)
);
CREATE INDEX ix_schedules_status ON schedules (status);

CREATE TABLE schedule_runs (
    id SERIAL NOT NULL,
    schedule_id VARCHAR(16) NOT NULL,
    scheduled_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    trigger VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL,
    job_id VARCHAR(16),
    message TEXT,
    PRIMARY KEY (id),
    FOREIGN KEY (schedule_id) REFERENCES schedules (id)
);
CREATE INDEX ix_schedule_runs_schedule_id ON schedule_runs (schedule_id);
CREATE INDEX ix_schedule_runs_job_id ON schedule_runs (job_id);
"""

downgrade = """
DROP TABLE schedule_runs;
DROP TABLE schedules;
DROP TABLE events;
DROP TABLE batches;
DROP TABLE tasks;
DROP TABLE jobs;
DROP TABLE groups;
"""
