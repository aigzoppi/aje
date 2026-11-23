"""Final counters of a job, stored when it finishes (one shape for live and finished jobs)."""

revision = "0002"
down_revision = "0001"

upgrade = """
ALTER TABLE jobs ADD COLUMN counts JSON;
"""

downgrade = """
ALTER TABLE jobs DROP COLUMN counts;
"""
