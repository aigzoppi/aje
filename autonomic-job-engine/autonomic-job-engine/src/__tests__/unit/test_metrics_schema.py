"""The typed metrics schema agrees with the contract catalog and appears in the OpenAPI document."""

import pydantic
import pytest

from aje import main
from aje.application import schemas
from aje.domain import dsl

FARM = set(dsl.FARM.facts["Metrics"])
DATA_PARALLEL = set(dsl.DATA_PARALLEL.facts["Metrics"])
FIELDS = dict(schemas.JobMetrics.model_fields)


def farm_metrics(**fields) -> dict:
    values = dict.fromkeys(FARM, 0) | {
        "ts": 1.0,
        "avg_latency": None,
        "p95_latency": None,
        "avg_service_time": None,
    }
    return values | fields


class CatalogAgreementTest:
    def test_fields_are_the_metrics_fact_plus_ts(self):
        assert set(FIELDS) == DATA_PARALLEL | {"ts"}

    def test_farm_metrics_are_required_and_batch_metrics_optional(self):
        required = {name for name, f in FIELDS.items() if f.is_required()}
        assert required == FARM | {"ts"}
        assert all(FIELDS[name].default is None for name in DATA_PARALLEL - FARM)

    def test_descriptions_come_from_the_catalog(self):
        for name in DATA_PARALLEL:
            assert FIELDS[name].description == dsl.DATA_PARALLEL.facts["Metrics"][name]


class ValidationTest:
    def test_a_farm_snapshot_validates(self):
        metrics = schemas.JobMetrics.model_validate(farm_metrics(queue_depth=3))
        assert metrics.queue_depth == 3 and metrics.partitions is None

    def test_unknown_metrics_are_rejected(self):
        with pytest.raises(pydantic.ValidationError, match="extra"):
            schemas.JobMetrics.model_validate(farm_metrics(made_up=1))

    def test_missing_metrics_are_rejected(self):
        values = farm_metrics()
        del values["throughput"]
        with pytest.raises(pydantic.ValidationError, match="throughput"):
            schemas.JobMetrics.model_validate(values)


class OpenApiTest:
    @pytest.fixture(name="components")
    def components_fixture(self) -> dict:
        return main.app.openapi()["components"]["schemas"]

    def test_metrics_models_are_published(self, components):
        assert {"JobMetrics", "JobMetricsOut", "MetricsPoint", "Escalation"} <= set(components)
        assert components["JobMetrics"]["properties"]["throughput"]["description"]

    def test_jobs_and_the_metrics_endpoint_reference_them(self, components):
        assert "JobMetrics" in str(components["JobOut"]["properties"]["metrics"])
        endpoint = main.app.openapi()["paths"]["/v1/jobs/{job_id}/metrics"]["get"]
        assert "JobMetricsOut" in str(endpoint["responses"]["200"])
