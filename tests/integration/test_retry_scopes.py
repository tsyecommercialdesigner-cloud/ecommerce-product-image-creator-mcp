"""Independent retry-scope evidence must gate actual local reservations."""

import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from ecommerce_product_image_creator import (
    generation_cost,
    offline_submission,
    retry_scope,
)
from ecommerce_product_image_creator.execution_candidates import (
    read_candidate,
    register_root_candidate,
)
from integration.test_attempt002 import prepare_retry
from integration.test_cross_candidate_submission import setup_cross


def test_scope_required_path_rejects_unscoped_historical_root(tmp_path, monkeypatch):
    service, args, _, calls = prepare_retry(tmp_path, monkeypatch)
    with pytest.raises(
        offline_submission.SubmissionError, match="reservation is required"
    ):
        offline_submission.submit_offline(
            service, *args, retry_of="op", require_retry_scope=True
        )
    with service.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM operation").fetchone() == (1,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)
    assert len(calls) == 1


def test_scoped_root_cannot_bypass_risk_reservation(tmp_path, monkeypatch):
    service, args, _, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    with pytest.raises(
        offline_submission.SubmissionError, match="reservation is required"
    ):
        offline_submission.submit_offline(service, *args, retry_of="op")
    assert len(calls) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM operation WHERE operation_id=?", (args[6],)
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id=?", (args[6],)
        ).fetchone() == (0,)


def test_unverified_price_source_rejects_valid_cost_scope(tmp_path, monkeypatch):
    service, args, _, calls, _, cost = make_scopes(tmp_path, monkeypatch)
    monkeypatch.setattr(retry_scope, "_verify_price_source", lambda db, value: False)
    with pytest.raises(ValueError, match="retry_scope_unverified_or_expired"):
        retry_scope.reserve_eligibility(
            service,
            *args,
            root="op",
            candidate_id="source",
            host_id="host",
            cost_id=cost.scope_id,
        )
    assert len(calls) == 1


def test_reservation_failure_rolls_back_derived_eligibility(tmp_path, monkeypatch):
    service, args, first, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    confirmation = replace(args[5], confirmation_id="rollback-confirmation")
    request = (*args[:5], confirmation, "rollback-op", args[7])
    with service.store._connect() as db:
        db.execute(
            "INSERT INTO fixture_retry_evidence VALUES(?,?,?)",
            ("op", first.attempt_id, confirmation.confirmation_id),
        )
        db.execute(
            "CREATE TRIGGER fail_scope_reservation BEFORE INSERT ON retry_scope_reservation BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(ValueError):
        retry_scope.reserve_eligibility(
            service,
            *request,
            root="op",
            candidate_id="source",
            host_id="host",
            cost_id="cost",
        )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM local_eligibility WHERE operation_id='rollback-op'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT invalidated,consumed FROM local_eligibility WHERE operation_id=?",
            (args[6],),
        ).fetchone() == (0, 0)
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (1,)
    assert len(calls) == 1


def test_existing_reservation_replays_after_process_restart(tmp_path, monkeypatch):
    service, args, _, calls, host, cost = make_scopes(tmp_path, monkeypatch)
    first = retry_scope.reserve_eligibility(
        service, *args, root="op", candidate_id="source", host_id="host", cost_id="cost"
    )
    ctx = multiprocessing.get_context("spawn")
    ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    process = ctx.Process(
        target=_process_reserve, args=(service, args, host, cost, ready, start, output)
    )
    process.start()
    try:
        assert ready.get(timeout=20)
        start.set()
        assert output.get(timeout=30) == "reserved"
        process.join(timeout=30)
        assert process.exitcode == 0
    finally:
        start.set()
        if process.is_alive():
            process.terminate()
            process.join()
    assert first == {"currency": "USD", "reserved_minor": 20}
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*),sum(reserved_minor) FROM retry_scope_reservation"
        ).fetchone() == (1, 20)
    assert len(calls) == 1


def test_expired_scope_and_unverified_capability_stop_reservation(
    tmp_path, monkeypatch
):
    service, args, _, calls, host, cost = make_scopes(tmp_path, monkeypatch)
    monkeypatch.setattr(retry_scope, "_verify_candidate_capability", lambda *_: False)
    with pytest.raises(ValueError, match="business_or_capability_unverified"):
        retry_scope.reserve_eligibility(
            service,
            *args,
            root="op",
            candidate_id="source",
            host_id="host",
            cost_id="cost",
        )
    monkeypatch.setattr(
        retry_scope,
        "_verify_candidate_capability",
        lambda db, candidate_id, snapshot: (
            candidate_id == "source"
            and snapshot.execution_digest == host.grants[0].digest
        ),
    )
    monkeypatch.setattr(
        retry_scope.time, "time", lambda: max(host.expires_at, cost.expires_at) + 1
    )
    with pytest.raises(ValueError, match="retry_scope_unverified_or_expired"):
        retry_scope.reserve_eligibility(
            service,
            *args,
            root="op",
            candidate_id="source",
            host_id="host",
            cost_id="cost",
        )
    assert len(calls) == 1


def _process_reserve(
    service, request, host, cost, ready, start, output, detailed=False
):
    from ecommerce_product_image_creator import offline_submission as submission
    from ecommerce_product_image_creator import retry_scope as scoped

    scoped._verify_host_source = lambda db, value: value == host
    scoped._verify_cost_source = lambda db, value: value == cost
    scoped._verify_price_source = lambda db, value: value == cost
    scoped._verify_candidate_capability = lambda db, candidate_id, snap: (
        candidate_id == "source" and snap.execution_digest == host.grants[0].digest
    )
    submission._verify_business_choices = lambda snapshot: (
        snapshot.execution_digest == host.grants[0].digest
    )
    submission._verify_retry_evidence = (
        lambda db, root, attempt, snapshot, confirmation_id: bool(
            db.execute(
                "SELECT 1 FROM fixture_retry_evidence WHERE operation_id=? AND attempt_id=? AND confirmation_id=?",
                (root, attempt, confirmation_id),
            ).fetchone()
        )
    )
    ready.put(True)
    if not start.wait(20):
        output.put("timeout")
        return
    try:
        scoped.reserve_eligibility(
            service,
            *request,
            root="op",
            candidate_id="source",
            host_id="host",
            cost_id="cost",
        )
        output.put("reserved")
    except (ValueError, OSError) as exc:
        output.put(str(exc) if detailed else type(exc).__name__)


def _process_upgrade_and_submit(service, request, host, cost, output):
    from ecommerce_product_image_creator.storage import Store

    service.store = Store(service.store.path)
    retry_scope._verify_host_source = lambda db, value: value == host
    retry_scope._verify_cost_source = lambda db, value: value == cost
    retry_scope._verify_price_source = lambda db, value: value == cost
    retry_scope._verify_candidate_capability = lambda db, candidate, snap: (
        candidate == "source" and snap.execution_digest == host.grants[0].digest
    )
    offline_submission._verify_authorization = lambda *_: True
    offline_submission._verify_business_choices = lambda snap: (
        snap.execution_digest == host.grants[0].digest
    )

    def fixture_evidence(db, root, attempt, snapshot, confirmation):
        return bool(
            db.execute(
                "SELECT 1 FROM fixture_retry_evidence WHERE operation_id=? AND attempt_id=? AND confirmation_id=?",
                (root, attempt, confirmation),
            ).fetchone()
        )

    offline_submission._verify_retry_evidence = fixture_evidence
    offline_submission._verify_retry_authorization = fixture_evidence
    calls = []

    def dispatch(_):
        calls.append(1)
        return offline_submission.ProviderResult("failed", task_id="fixture-id")

    offline_submission._offline_dispatch = dispatch
    try:
        retry_scope.record_host_scope(service.store, host)
        retry_scope.record_cost_scope(service.store, cost)
        retry_scope.record_host_scope(service.store, host)
        retry_scope.reserve_eligibility(
            service,
            *request,
            root="op",
            candidate_id="source",
            host_id=host.scope_id,
            cost_id=cost.scope_id,
        )
        result = offline_submission.submit_offline(
            service, *request, retry_of="op", require_retry_scope=True
        )
        output.put(("ok", result.attempt_id, len(calls)))
    except (ValueError, OSError) as exc:
        output.put(("error", type(exc).__name__, str(exc)))


def make_scopes(tmp_path, monkeypatch, *, max_minor=100, first_state="failed"):
    service, args, first, calls = prepare_retry(tmp_path, monkeypatch, first_state)
    candidate = register_root_candidate(service.store, "op", "source")
    s = candidate.snapshot
    grant = retry_scope.Grant(
        "source", s.execution_digest, s.model, s.endpoint, s.group, 3
    )
    common = (
        "op",
        s.task_id,
        s.revision,
        s.stage,
        s.input_digest,
        (grant,),
        3,
        time.time() + 120,
        "fixture",
        "evidence",
    )
    host = retry_scope.HostScope("host", *common)
    cost = retry_scope.CostScope("cost", *common, "USD", max_minor, 20, "fixture-price")
    with pytest.raises(ValueError):
        retry_scope.record_host_scope(service.store, host)
    monkeypatch.setattr(
        retry_scope, "_verify_host_source", lambda db, value: value == host
    )
    monkeypatch.setattr(
        retry_scope, "_verify_cost_source", lambda db, value: value == cost
    )
    monkeypatch.setattr(
        retry_scope, "_verify_price_source", lambda db, value: value == cost
    )
    monkeypatch.setattr(
        retry_scope,
        "_verify_candidate_capability",
        lambda db, candidate_id, snapshot: (
            candidate_id == "source"
            and snapshot.execution_digest == host.grants[0].digest
        ),
    )
    retry_scope.record_host_scope(service.store, host)
    retry_scope.record_cost_scope(service.store, cost)
    return service, args, first, calls, host, cost


def test_scope_reservation_enters_existing_submit_and_preserves_unknown_cost(
    tmp_path, monkeypatch
):
    service, args, first, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    result = retry_scope.reserve_eligibility(
        service, *args, root="op", candidate_id="source", host_id="host", cost_id="cost"
    )
    assert result["reserved_minor"] == 20 and result["currency"] == "USD"
    again = retry_scope.reserve_eligibility(
        service, *args, root="op", candidate_id="source", host_id="host", cost_id="cost"
    )
    assert again == result
    second = offline_submission.submit_offline(
        service, *args, retry_of="op", require_retry_scope=True
    )
    assert second.attempt_id != first.attempt_id and len(calls) == 2
    replay = offline_submission.submit_offline(
        service, *args, retry_of="op", require_retry_scope=True
    )
    assert replay.replayed and replay.attempt_id == second.attempt_id
    assert len(calls) == 2
    assert retry_scope.read_reservation(service.store, args[6])["actual_cost"] is None
    assert (
        retry_scope.read_reservation(service.store, args[6])["attempt_id"]
        == second.attempt_id
    )
    snap = args[4]
    evidence = generation_cost.GenerationCostEvidence(
        "cost-event",
        snap.task_id,
        snap.revision,
        snap.stage,
        "op",
        "source",
        second.attempt_id,
        snap.execution_digest,
        "candidate_gateway",
        snap.model,
        snap.endpoint,
        snap.group,
        "fixture-account",
        "fixture",
        "fixture-cost",
        "event",
        "0.11",
        "USD",
    )
    monkeypatch.setattr(
        generation_cost, "_verify_evidence_source", lambda db, value: value == evidence
    )
    generation_cost.record_generation_cost(service.store, evidence)
    cost_view = retry_scope.read_reservation(service.store, args[6])
    assert cost_view["known_actual_subtotal"] == {"USD": "0.11"}
    assert cost_view["actual_cost"] is None and cost_view["reserved_minor"] == 20
    next_args = (
        *args[:5],
        replace(args[5], confirmation_id="next"),
        "next-op",
        args[7],
    )
    with pytest.raises(ValueError):
        retry_scope.reserve_eligibility(
            service,
            *next_args,
            root="op",
            candidate_id="source",
            host_id="host",
            cost_id="missing",
        )
    service.store.record_retry_wait(
        "op", second.attempt_id, time.time() - 10, None, 500
    )
    with service.store._connect() as db:
        for table in ("fixture_retry_evidence", "fixture_retry_authorization"):
            db.execute(
                f"INSERT INTO {table} VALUES(?,?,?)", ("op", second.attempt_id, "next")
            )
    reserved = retry_scope.reserve_eligibility(
        service,
        *next_args,
        root="op",
        candidate_id="source",
        host_id="host",
        cost_id="cost",
    )
    assert reserved == {"currency": "USD", "reserved_minor": 20}
    third = offline_submission.submit_offline(service, *next_args, retry_of="op")
    assert third.attempt_id not in {first.attempt_id, second.attempt_id}
    assert len(calls) == 3
    with service.store._connect() as db:
        assert db.execute(
            "SELECT SUM(reserved_minor),COUNT(*) FROM retry_scope_reservation WHERE cost_id='cost'"
        ).fetchone() == (40, 2)
    assert retry_scope.read_cost_scope_balance(service.store, "cost") == {
        "currency": "USD",
        "limit_minor": 100,
        "reserved_minor": 40,
        "available_minor": 60,
        "billing_completeness": "unverified",
    }


def test_revocation_between_intent_and_dispatch_preserves_history(
    tmp_path, monkeypatch
):
    service, args, first, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    retry_scope.reserve_eligibility(
        service, *args, root="op", candidate_id="source", host_id="host", cost_id="cost"
    )
    monkeypatch.setattr(
        retry_scope,
        "_verify_revocation_source",
        lambda db, scope_id, ref: (scope_id, ref) == ("host", "revoked-fixture"),
    )
    monkeypatch.setattr(
        offline_submission,
        "_after_intent_recorded",
        lambda: retry_scope.revoke_scope(service.store, "host", "revoked-fixture"),
    )
    result = offline_submission.submit_offline(service, *args, retry_of="op")
    assert result.state == "unknown" and result.attempt_id != first.attempt_id
    assert len(calls) == 1
    assert (
        retry_scope.read_reservation(service.store, args[6])["attempt_id"]
        == result.attempt_id
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id=?", (args[6],)
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT reserved_minor FROM retry_scope_reservation WHERE operation_id=?",
            (args[6],),
        ).fetchone() == (20,)


def test_competing_derivations_cannot_occupy_same_position(tmp_path, monkeypatch):
    service, args, first, calls, _, _ = make_scopes(tmp_path, monkeypatch, max_minor=20)
    requests = []
    for suffix in ("a", "b"):
        confirmation = replace(args[5], confirmation_id=f"race-{suffix}")
        request = (*args[:5], confirmation, f"race-op-{suffix}", args[7])
        requests.append(request)
        with service.store._connect() as db:
            db.execute(
                "INSERT INTO fixture_retry_evidence VALUES(?,?,?)",
                ("op", first.attempt_id, confirmation.confirmation_id),
            )

    def run(request):
        try:
            return retry_scope.reserve_eligibility(
                service,
                *request,
                root="op",
                candidate_id="source",
                host_id="host",
                cost_id="cost",
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, requests))
    assert sum(result is not None for result in results) == 1
    assert len(calls) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*),sum(reserved_minor) FROM retry_scope_reservation"
        ).fetchone() == (1, 20)
        assert db.execute(
            "SELECT count(*) FROM local_eligibility WHERE operation_id LIKE 'race-op-%' AND invalidated=0"
        ).fetchone() == (1,)


def test_alternate_candidate_scope_reserves_and_consumes_existing_path(
    tmp_path, monkeypatch
):
    service, args, _, _, _, candidate, sent = setup_cross(tmp_path, monkeypatch)
    snap = candidate.snapshot
    grant = retry_scope.Grant(
        candidate.candidate_id,
        snap.execution_digest,
        snap.model,
        snap.endpoint,
        snap.group,
        3,
    )
    source = read_candidate(service.store, "a").snapshot
    source_grant = retry_scope.Grant(
        "a", source.execution_digest, source.model, source.endpoint, source.group, 3
    )
    common = (
        "op",
        snap.task_id,
        snap.revision,
        snap.stage,
        snap.input_digest,
        (source_grant, grant),
        6,
        time.time() + 120,
        "fixture",
        "evidence",
    )
    host = retry_scope.HostScope("alternate-host", *common)
    cost = retry_scope.CostScope(
        "alternate-cost", *common, "USD", 50, 25, "fixture-price"
    )
    monkeypatch.setattr(
        retry_scope, "_verify_host_source", lambda db, value: value == host
    )
    monkeypatch.setattr(
        retry_scope, "_verify_cost_source", lambda db, value: value == cost
    )
    monkeypatch.setattr(
        retry_scope, "_verify_price_source", lambda db, value: value == cost
    )
    monkeypatch.setattr(
        retry_scope,
        "_verify_candidate_capability",
        lambda db, candidate_id, snapshot: (
            candidate_id == candidate.candidate_id and snapshot == snap
        ),
    )
    retry_scope.record_host_scope(service.store, host)
    retry_scope.record_cost_scope(service.store, cost)
    reserved = retry_scope.reserve_eligibility(
        service,
        *args,
        root="op",
        candidate_id=candidate.candidate_id,
        host_id=host.scope_id,
        cost_id=cost.scope_id,
    )
    assert reserved == {"currency": "USD", "reserved_minor": 25}
    result = offline_submission.submit_offline(service, *args, retry_of="op")
    assert result.state == "accepted" and len(sent) == 1
    assert (
        retry_scope.read_reservation(service.store, args[6])["attempt_id"]
        == result.attempt_id
    )


def test_two_processes_compete_for_position_and_risk(tmp_path, monkeypatch):
    service, args, first, calls, host, cost = make_scopes(
        tmp_path, monkeypatch, max_minor=20
    )
    ctx = multiprocessing.get_context("spawn")
    ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    requests = []
    for suffix in ("a", "b"):
        confirmation = replace(args[5], confirmation_id=f"process-{suffix}")
        requests.append((*args[:5], confirmation, f"process-op-{suffix}", args[7]))
        with service.store._connect() as db:
            db.execute(
                "INSERT INTO fixture_retry_evidence VALUES(?,?,?)",
                ("op", first.attempt_id, confirmation.confirmation_id),
            )
    processes = [
        ctx.Process(
            target=_process_reserve,
            args=(service, request, host, cost, ready, start, output),
        )
        for request in requests
    ]
    for process in processes:
        process.start()
    try:
        assert ready.get(timeout=20) and ready.get(timeout=20)
        start.set()
        results = [output.get(timeout=30), output.get(timeout=30)]
        for process in processes:
            process.join(timeout=30)
        assert all(process.exitcode == 0 for process in processes)
        assert sorted(results) == ["ValueError", "reserved"]
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
    assert len(calls) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*),sum(reserved_minor) FROM retry_scope_reservation"
        ).fetchone() == (1, 20)


def test_cost_exhaustion_is_independent_of_unoccupied_position(tmp_path, monkeypatch):
    service, args, _, calls, host, cost = make_scopes(
        tmp_path, monkeypatch, max_minor=20
    )
    retry_scope.reserve_eligibility(
        service, *args, root="op", candidate_id="source", host_id="host", cost_id="cost"
    )
    second = offline_submission.submit_offline(
        service, *args, retry_of="op", require_retry_scope=True
    )
    service.store.record_retry_wait(
        "op", second.attempt_id, time.time() - 10, None, 500
    )
    ctx = multiprocessing.get_context("spawn")
    ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    requests = []
    for suffix in ("a", "b"):
        confirmation = replace(args[5], confirmation_id=f"cost-{suffix}")
        requests.append((*args[:5], confirmation, f"cost-op-{suffix}", args[7]))
        with service.store._connect() as db:
            db.execute(
                "INSERT INTO fixture_retry_evidence VALUES(?,?,?)",
                ("op", second.attempt_id, confirmation.confirmation_id),
            )
    processes = [
        ctx.Process(
            target=_process_reserve,
            args=(service, request, host, cost, ready, start, output, True),
        )
        for request in requests
    ]
    for process in processes:
        process.start()
    try:
        assert ready.get(timeout=20) and ready.get(timeout=20)
        start.set()
        assert [output.get(timeout=30) for _ in processes] == [
            "cost_scope_exhausted"
        ] * 2
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*), SUM(reserved_minor) FROM retry_scope_reservation"
        ).fetchone() == (1, 20)
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation WHERE position=3"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM local_eligibility WHERE operation_id LIKE 'cost-op-%'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (2,)
    assert retry_scope.read_reservation(service.store, args[6])["actual_cost"] is None
    assert (
        retry_scope.read_cost_scope_balance(service.store, "cost")["available_minor"]
        == 0
    )
    assert len(calls) == 2


def test_scope_source_scope_identity_and_price_fail_closed(tmp_path, monkeypatch):
    service, args, _, calls, host, cost = make_scopes(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        retry_scope.record_cost_scope(
            service.store, replace(cost, scope_id="invalid-currency", currency="")
        )
    with pytest.raises(ValueError):
        retry_scope.record_cost_scope(
            service.store,
            replace(cost, scope_id="invalid-amount", per_attempt_minor=0.1),
        )
    with pytest.raises(ValueError):
        retry_scope.record_host_scope(
            service.store,
            replace(host, scope_id="wrong-revision", revision=host.revision + 1),
        )
    with pytest.raises(ValueError):
        retry_scope.reserve_eligibility(
            service,
            *args,
            root="op",
            candidate_id="missing",
            host_id="host",
            cost_id="cost",
        )
    with pytest.raises(ValueError):
        retry_scope.reserve_eligibility(
            service,
            *args,
            root="wrong-root",
            candidate_id="source",
            host_id="host",
            cost_id="cost",
        )
    monkeypatch.setattr(retry_scope, "_verify_cost_source", lambda db, value: False)
    with pytest.raises(ValueError):
        retry_scope.reserve_eligibility(
            service,
            *args,
            root="op",
            candidate_id="source",
            host_id="host",
            cost_id="cost",
        )
    assert len(calls) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM retry_scope_reservation"
        ).fetchone() == (0,)


def test_revocation_after_derivation_stops_before_attempt(tmp_path, monkeypatch):
    service, args, _, calls, _, _ = make_scopes(tmp_path, monkeypatch)
    retry_scope.reserve_eligibility(
        service, *args, root="op", candidate_id="source", host_id="host", cost_id="cost"
    )
    monkeypatch.setattr(
        retry_scope,
        "_verify_revocation_source",
        lambda db, scope_id, reference: (scope_id, reference) == ("cost", "revoked"),
    )
    retry_scope.revoke_scope(service.store, "cost", "revoked")
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args, retry_of="op")
    assert len(calls) == 1
    with service.store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM operation WHERE operation_id=?", (args[6],)
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id=?", (args[6],)
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT reserved_minor,attempt_id FROM retry_scope_reservation WHERE operation_id=?",
            (args[6],),
        ).fetchone() == (20, None)


def test_known_pre_scope_schema_upgrade_and_submit_in_new_process(
    tmp_path, monkeypatch
):
    service, args, first, calls = prepare_retry(
        tmp_path, monkeypatch, first_authorizations=True
    )
    candidate = register_root_candidate(service.store, "op", "source")
    s = candidate.snapshot
    grant = retry_scope.Grant(
        "source", s.execution_digest, s.model, s.endpoint, s.group, 3
    )
    common = (
        "op",
        s.task_id,
        s.revision,
        s.stage,
        s.input_digest,
        (grant,),
        3,
        time.time() + 120,
        "fixture",
        "evidence",
    )
    host = retry_scope.HostScope("host", *common)
    cost = retry_scope.CostScope("cost", *common, "USD", 100, 20, "fixture-price")
    with service.store._connect() as db:
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='retry_scope_evidence'"
        ).fetchone()
        old_attempt = db.execute(
            "SELECT * FROM attempt WHERE attempt_id=?", (first.attempt_id,)
        ).fetchone()
        old_chain = db.execute("SELECT * FROM operation_attempt").fetchall()
        old_eligibility = db.execute(
            "SELECT * FROM local_eligibility WHERE operation_id='op'"
        ).fetchone()
        old_authorizations = db.execute(
            "SELECT * FROM first_authorization ORDER BY authorization_id"
        ).fetchall()
        old_consumptions = db.execute(
            "SELECT * FROM first_authorization_consumption ORDER BY authorization_id"
        ).fetchall()
        assert len(old_authorizations) == len(old_consumptions) == 2
    ctx = multiprocessing.get_context("spawn")
    output = ctx.Queue()
    process = ctx.Process(
        target=_process_upgrade_and_submit, args=(service, args, host, cost, output)
    )
    process.start()
    try:
        result = output.get(timeout=45)
        process.join(timeout=30)
        assert process.exitcode == 0
        assert result[0] == "ok", result
        assert result[2] == 1
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
    with service.store._connect() as db:
        assert (
            db.execute(
                "SELECT * FROM attempt WHERE attempt_id=?", (first.attempt_id,)
            ).fetchone()
            == old_attempt
        )
        assert (
            db.execute(
                "SELECT * FROM local_eligibility WHERE operation_id='op'"
            ).fetchone()
            == old_eligibility
        )
        assert (
            db.execute(
                "SELECT * FROM first_authorization ORDER BY authorization_id"
            ).fetchall()
            == old_authorizations
        )
        assert (
            db.execute(
                "SELECT * FROM first_authorization_consumption ORDER BY authorization_id"
            ).fetchall()
            == old_consumptions
        )
        assert (
            db.execute("SELECT * FROM operation_attempt WHERE sequence=1").fetchall()
            == old_chain
        )
        assert db.execute(
            "SELECT COUNT(*) FROM operation_attempt WHERE operation_id='op'"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT reserved_minor,attempt_id FROM retry_scope_reservation WHERE operation_id=?",
            (args[6],),
        ).fetchone() == (20, result[1])
    assert len(calls) == 1


def test_existing_nonempty_database_is_compatible_and_initialization_is_idempotent(
    tmp_path, monkeypatch
):
    from ecommerce_product_image_creator.storage import Store

    service, _args, first, calls = prepare_retry(tmp_path, monkeypatch)
    candidate = register_root_candidate(service.store, "op", "source")
    snap = candidate.snapshot
    grant = retry_scope.Grant(
        "source", snap.execution_digest, snap.model, snap.endpoint, snap.group, 3
    )
    host = retry_scope.HostScope(
        "migration-host",
        "op",
        snap.task_id,
        snap.revision,
        snap.stage,
        snap.input_digest,
        (grant,),
        3,
        time.time() + 120,
        "fixture",
        "evidence",
    )
    with service.store._connect() as db:
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='retry_scope_evidence'"
        ).fetchone()
        before = db.execute(
            "SELECT * FROM operation_attempt ORDER BY sequence"
        ).fetchall()
        counter = db.execute("SELECT * FROM attempt_counter").fetchall()
        attempt = db.execute(
            "SELECT * FROM attempt WHERE attempt_id=?", (first.attempt_id,)
        ).fetchone()
    monkeypatch.setattr(
        retry_scope, "_verify_host_source", lambda db, value: value == host
    )
    retry_scope.record_host_scope(service.store, host)
    retry_scope.record_host_scope(Store(service.store.path), host)
    with service.store._connect() as db:
        assert (
            db.execute("SELECT * FROM operation_attempt ORDER BY sequence").fetchall()
            == before
        )
        assert db.execute("SELECT * FROM attempt_counter").fetchall() == counter
        assert (
            db.execute(
                "SELECT * FROM attempt WHERE attempt_id=?", (first.attempt_id,)
            ).fetchone()
            == attempt
        )
        assert db.execute("SELECT COUNT(*) FROM retry_scope_evidence").fetchone() == (
            1,
        )
    assert len(calls) == 1
