"""Cron expressions, time zones and the schedule models."""

import datetime

import pydantic
import pytest

from aje.application import schemas
from aje.domain import cron as cron_lib
from common import specs

JOB = specs.template()


def utc(*parts: int) -> datetime.datetime:
    return datetime.datetime(*parts, tzinfo=datetime.UTC)


class NextFireTest:
    @pytest.mark.parametrize(
        "cron, tz, after, expected",
        [
            # 02:00 in Rome is 01:00 UTC in winter (CET) and 00:00 UTC in summer (CEST)
            ("0 2 * * *", "Europe/Rome", utc(2026, 1, 10), utc(2026, 1, 10, 1)),
            ("0 2 * * *", "Europe/Rome", utc(2026, 7, 9, 23), utc(2026, 7, 10)),
            ("@hourly", "UTC", utc(2026, 1, 1, 10), utc(2026, 1, 1, 11)),
            ("0 * * * *", "UTC", utc(2026, 1, 1, 10), utc(2026, 1, 1, 11)),  # strictly after
        ],
    )
    def test_is_timezone_aware_and_strictly_after(self, cron, tz, after, expected):
        assert cron_lib.next_fire(cron, tz, after) == expected

    def test_aware_datetimes_are_converted_to_utc(self):
        rome = datetime.timezone(datetime.timedelta(hours=1))
        assert cron_lib.as_utc(datetime.datetime(2026, 1, 1, 13, tzinfo=rome)) == utc(2026, 1, 1, 12)

    def test_naive_datetimes_are_rejected(self):
        with pytest.raises(ValueError, match="naive"):
            cron_lib.as_utc(datetime.datetime(2026, 1, 1, 12))


class ValidationTest:
    @pytest.mark.parametrize("expr", ["* * * * *", "*/5 9-17 * * mon-fri", "@daily", "  0   2 * * * "])
    def test_valid_cron_is_normalised(self, expr):
        assert cron_lib.check_cron(expr) == " ".join(expr.split())

    @pytest.mark.parametrize("expr", ["61 * * * *", "* * * *", "0 0 0 0 0 0 0", "@sometimes", "bad"])
    def test_invalid_cron(self, expr):
        with pytest.raises(ValueError):
            cron_lib.check_cron(expr)

    def test_timezone(self):
        assert cron_lib.check_timezone("Europe/Rome") == "Europe/Rome"
        with pytest.raises(ValueError):
            cron_lib.check_timezone("Mars/Base")


class ScheduleModelsTest:
    def test_defaults(self):
        s = schemas.ScheduleCreate(name="nightly", cron="0 2 * * *", timezone="Europe/Rome", job=JOB)
        assert s.concurrency_policy == "forbid" and s.job.pattern == "farm"

    @pytest.mark.parametrize(
        "bad",
        [{"name": "a" * 32}, {"cron": "61 * * * *"}, {"timezone": "Nowhere"}, {"concurrency_policy": "x"}],
    )
    def test_invalid_fields(self, bad):
        with pytest.raises(pydantic.ValidationError):
            schemas.ScheduleCreate(**{"name": "n", "cron": "@daily", "job": JOB, **bad})

    def test_update_keeps_only_the_fields_set(self):
        assert schemas.ScheduleUpdate(cron="@hourly").model_dump(exclude_unset=True) == {"cron": "@hourly"}
