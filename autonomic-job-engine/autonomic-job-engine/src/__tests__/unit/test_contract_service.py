"""ContractService: contract catalogs and validation (no I/O)."""

import pytest

from aje.application import schemas
from aje.application import services
from aje.domain import dsl
from aje.domain import errors


@pytest.fixture(name="contracts")
def contracts_fixture() -> services.ContractService:
    return services.ContractService()


class ContractServiceTest:
    def test_catalog_lists_the_actions_of_a_scope(self, contracts):
        assert "scale" in contracts.catalog("farm")["actions"]

    def test_unknown_scope_is_not_found(self, contracts):
        with pytest.raises(errors.NotFoundError):
            contracts.catalog("nope")

    def test_validate_describes_a_valid_contract(self, contracts):
        body = schemas.ContractValidateIn(scope="data_parallel", contract={"builtin": "dp-balance"})
        assert contracts.validate(body)["valid"] is True

    def test_validate_rejects_a_contract_of_another_scope(self, contracts):
        body = schemas.ContractValidateIn(scope="farm", contract={"builtin": "dp-balance"})
        with pytest.raises(dsl.ContractError):
            contracts.validate(body)
