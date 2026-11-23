"""The contract language: parsing, compilation errors and rule evaluation."""

import pathlib

import pytest

from aje.domain import contracts
from aje.domain import dsl

REPO = pathlib.Path(__file__).parents[3]


def job_fact(**fields) -> dict:
    """A valid farm Job fact, with ``fields`` overridden."""
    return {
        "id": "j1",
        "name": "j",
        "pattern": "farm",
        "status": "running",
        "workers": 2,
        "active": 2,
        "ready": 2,
        "min_workers": 1,
        "max_workers": 10,
        "group_id": None,
        "budget_free": None,
        "closed": False,
        "max_retries": 3,
        **fields,
    }


def metrics_fact(**fields) -> dict:
    """A valid farm Metrics fact (zeros, null latencies), with ``fields`` overridden."""
    base = dict.fromkeys(dsl.FARM.facts["Metrics"], 0)
    return {**base, "avg_latency": None, "p95_latency": None, "avg_service_time": None, **fields}


def farm_facts(job: dict | None = None, metrics: dict | None = None) -> dict:
    """Working memory for a farm contract."""
    return {"Job": [job or job_fact()], "Metrics": [metrics or metrics_fact()]}


def compile_farm(source: str) -> dsl.RuleBase:
    return dsl.RuleBase.compile(source, dsl.FARM)


def calls(firings) -> list[tuple[str, list]]:
    return [(c.name, c.args) for f in firings for c in f.calls]


class BuiltinContractsTest:
    @pytest.mark.parametrize("name", list(contracts.BUILTINS))
    def test_compile(self, name):
        assert dsl.RuleBase.compile(
            contracts.builtin_source(name), dsl.CATALOGS[contracts.BUILTINS[name][0]]
        ).rules

    @pytest.mark.parametrize(
        "path, scope",
        [
            ("examples/contracts/farm-latency-sla.drl", dsl.FARM),
            ("examples/contracts/dp-deadline.drl", dsl.DATA_PARALLEL),
        ],
    )
    def test_examples_compile(self, path, scope):
        assert dsl.RuleBase.compile((REPO / path).read_text(), scope).rules


class GlobalsTest:
    SOURCE = 'global int a = 2 * 3; global double b = 1; global String s = "x" + "y"; global flag = !false;'

    def test_types_and_constant_expressions(self):
        assert dsl.parse_contract(self.SOURCE).globals == {"a": 6, "b": 1.0, "s": "xy", "flag": True}

    def test_overrides(self):
        assert dsl.RuleBase(dsl.parse_contract(self.SOURCE), dsl.FARM, {"a": 10}).globals["a"] == 10

    def test_unknown_override(self):
        with pytest.raises(dsl.ContractError, match="unknown contract parameter"):
            dsl.RuleBase(dsl.parse_contract(self.SOURCE), dsl.FARM, {"zzz": 1})

    def test_type_mismatch(self):
        with pytest.raises(dsl.ContractError, match="declared int"):
            dsl.parse_contract('global int a = "no";')


class EvaluationTest:
    def test_salience_order_and_activation_group(self):
        firings = compile_farm("""
        rule "low" salience 1 activation-group "g" when Job() then scale(1); end
        rule "high" salience 9 activation-group "g" when Job() then scale(9); end
        rule "free" when Job() then log("x"); end
        """).run(farm_facts())
        assert [f.rule for f in firings] == ["high", "free"]
        assert firings[0].calls[0].args == [9]

    def test_cooldown(self):
        rb = compile_farm('rule "r" cooldown "10s" when Job() then scale_up(); end')
        assert [len(rb.run(farm_facts(), now=t)) for t in (100, 105, 111)] == [1, 0, 1]

    def test_bindings_joins_not_exists_eval(self):
        rb = dsl.RuleBase.compile(
            """
        rule "join"
        when
            $g : Group( free > 0 )
            $e : Escalation( $amt : amount )
            $j : JobStat( id == $e.job_id, workers < max_workers )
            not JobStat( name == "blocker" )
            exists JobStat( queue_depth > 100 )
            eval( $amt <= $g.free )
        then
            scale_job($j.id, $j.workers + $amt);
        end
        """,
            dsl.GROUP,
        )

        def stat(i, **kw):
            return {"id": i, "name": i, "workers": 1, "max_workers": 5, "queue_depth": 0, **kw}

        facts = {
            "Group": [{"free": 3}],
            "Escalation": [{"job_id": "a", "amount": 2}, {"job_id": "b", "amount": 5}],
            "JobStat": [stat("a", queue_depth=500), stat("b")],
        }
        assert calls(rb.run(facts)) == [("scale_job", ["a", 3])]
        facts["JobStat"].append(stat("blocker"))
        assert not rb.run(facts)

    def test_null_never_matches_a_comparison(self):
        rb = compile_farm('rule "r" when Metrics( avg_latency > 1 ) then scale(1); end')
        assert not rb.run(farm_facts(metrics=metrics_fact(avg_latency=None)))

    def test_null_check_and_arithmetic(self):
        rb = compile_farm('rule "r" when Metrics( avg_latency == null ) then scale(ceil(7 / 2) % 3); end')
        assert calls(rb.run(farm_facts())) == [("scale", [1])]

    def test_division_by_zero_is_null_not_crash(self):
        rb = compile_farm('rule "r" when Metrics( queue_depth / live_workers > 1 ) then scale(2); end')
        assert not rb.run(farm_facts(metrics=metrics_fact(queue_depth=5, live_workers=0)))

    def test_set_partitions_is_allowed_for_data_parallel(self):
        rb = dsl.RuleBase.compile(
            'rule "x" when $j : Job( partitions < workers ) then set_partitions($j.workers * 2); end',
            dsl.DATA_PARALLEL,
        )
        assert calls(rb.run({"Job": [job_fact(partitions=1, workers=3)], "Metrics": [{}]})) == [
            ("set_partitions", [6])
        ]


class CompileErrorsTest:
    @pytest.mark.parametrize(
        "source, message",
        [
            ('rule "x" when Foo() then end', "unknown fact type"),
            ('rule "x" when Job( nope > 1 ) then end', "unknown name 'nope'"),
            ('rule "x" when $j : Job() then scale($j.nope); end', "has no field 'nope'"),
            ('rule "x" when Job() then scale($k); end', "used before it is bound"),
            ('rule "x" when Job() then set_partitions(2); end', "not allowed for farm"),
            ('rule "x" when Job() then scale(); end', "takes 1"),
            ('rule "x" when Job() then scale(sin(1)); end', "unknown function"),
            ('rule "x" when Job() then end rule "x" when Job() then end', "duplicate rule"),
            ('rule "x" cooldown "soon" when Job() then end', "invalid duration"),
            ('rule "x" when Job( then end', "syntax error"),
        ],
    )
    def test_is_reported_with_a_message(self, source, message):
        with pytest.raises(dsl.ContractError, match=message):
            compile_farm(source)


class SyntaxTest:
    def test_comments_package_import_and_disabled_rules(self):
        contract = dsl.parse_contract("""
        package my.rules;   // line comment
        import aje.facts.Job;
        # hash comment
        /* block
           comment */
        rule "r" no-loop true enabled false when Job() then scale(1); end
        """)
        assert contract.package == "my.rules" and contract.rules[0].enabled is False
        assert not dsl.RuleBase(contract, dsl.FARM).run(farm_facts())
