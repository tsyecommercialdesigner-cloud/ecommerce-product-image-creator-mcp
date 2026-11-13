"""B1b query execution must require the registered independent authorization."""

import pytest

from ecommerce_product_image_creator import original_query

from .test_original_query import setup_query


def test_legacy_query_cannot_bypass_query_authorization(tmp_path, monkeypatch):
    service, args, attempt, sends = setup_query(
        tmp_path, monkeypatch, authorized_legacy=False
    )
    monkeypatch.setattr(original_query, "_verify_original_binding", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda identity: (
            calls.append(identity)
            or {"data": {"task_id": identity.upstream_id, "status": "SUCCESS"}}
        ),
    )
    result = original_query.query_once(
        service.store,
        "op",
        "a",
        args[0].task_id,
        1,
        attempt.attempt_id,
        args[4].execution_digest,
    )
    assert result.state == "unverified"
    assert calls == [] and sends == [1]
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (0,)


def test_registered_request_dispatches_once_and_replays(tmp_path, monkeypatch):
    from ecommerce_product_image_creator import query_registration, recovery

    from .test_query_registration import setup_registration

    service, req, auth, sends = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda identity: (
            calls.append(identity.upstream_id)
            or {"data": {"task_id": identity.upstream_id, "status": "SUCCESS"}}
        ),
    )
    query_registration.record_authorization(service.store, auth)
    registered = query_registration.register_request(service.store, req)
    identity = (
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
    )
    first = original_query.query_once(
        service.store, *identity, request_id=req.request_id
    )
    second = original_query.query_once(
        service.store, *identity, request_id=req.request_id
    )
    wrong_scope = original_query.query_once(
        service.store, "other-root", *identity[1:], request_id=req.request_id
    )
    assert wrong_scope.state == "unverified"
    assert wrong_scope.reason == "query_request_scope_conflict"
    assert first.state == second.state == "succeeded"
    assert first.invocation_id == second.invocation_id == registered.invocation_id
    assert first.observation_id == second.observation_id and calls == [
        "fixture-upstream"
    ]
    assert sends == [1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT status,observation_id FROM query_invocation WHERE invocation_id=?",
            (registered.invocation_id,),
        ).fetchone() == ("observed", first.observation_id)
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM query_authorization_consumption"
        ).fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM operation_attempt").fetchone() == (1,)


@pytest.mark.parametrize("reason", ["revoked", "expired", "scope", "entered"])
def test_execution_fails_closed_before_adapter(tmp_path, monkeypatch, reason):
    from ecommerce_product_image_creator import query_registration

    from .test_query_registration import setup_registration

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    intent = query_registration.register_request(service.store, req)
    calls = []
    monkeypatch.setattr(original_query, "_query_adapter", lambda i: calls.append(i))
    if reason == "revoked":
        monkeypatch.setattr(
            query_registration, "_verify_query_revocation", lambda *_: True
        )
        query_registration.revoke_authorization(
            service.store, auth.authorization_id, "fixture-revoke"
        )
    elif reason == "expired":
        monkeypatch.setattr(
            query_registration.time, "time", lambda: auth.expires_at + 1
        )
    elif reason == "scope":
        monkeypatch.setattr(
            original_query, "_query_account_scope", lambda _: "other-account"
        )
    else:
        with service.store._connect() as db:
            db.execute(
                "UPDATE query_invocation SET status='entered' WHERE invocation_id=?",
                (intent.invocation_id,),
            )
    result = original_query.query_once(
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
        request_id=req.request_id,
    )
    assert result.state == (
        "unknown"
        if reason == "entered"
        else "stopped"
        if reason in ("revoked", "expired")
        else "unverified"
    )
    assert result.invocation_id == intent.invocation_id and not calls
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM query_invocation").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM query_authorization_consumption"
        ).fetchone() == (1,)


def test_observation_write_failure_never_requeries(tmp_path, monkeypatch):
    from ecommerce_product_image_creator import query_registration

    from .test_query_registration import setup_registration

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    intent = query_registration.register_request(service.store, req)
    calls = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: (
            calls.append(i.upstream_id)
            or {"data": {"task_id": i.upstream_id, "status": "SUCCESS"}}
        ),
    )
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER injected_observation_failure BEFORE INSERT ON recovery_observation BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    args = (
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
    )
    first = original_query.query_once(*args, request_id=req.request_id)
    replay = original_query.query_once(*args, request_id=req.request_id)
    assert first.state == replay.state == "unknown"
    assert first.reason == "query_result_unrecorded"
    assert replay.reason == "query_may_have_executed"
    assert calls == ["fixture-upstream"]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT status,observation_id FROM query_invocation WHERE invocation_id=?",
            (intent.invocation_id,),
        ).fetchone() == ("response_received", None)
        assert db.execute("SELECT count(*) FROM recovery_observation").fetchone() == (
            0,
        )


@pytest.mark.parametrize("reference", ["fixture-confirmation", "op"])
def test_generation_authorization_and_local_eligibility_are_not_query_authorizations(
    tmp_path, monkeypatch, reference
):
    import dataclasses

    from ecommerce_product_image_creator import query_registration

    from .test_query_registration import setup_registration

    service, req, _auth, sends = setup_registration(tmp_path, monkeypatch)
    with service.store._connect() as db:
        assert db.execute(
            "SELECT 1 FROM local_eligibility WHERE operation_id=? AND confirmation_id=?",
            ("op", "fixture-confirmation"),
        ).fetchone()
        before = list(db.iterdump())
    bad = dataclasses.replace(req, query_authorization_reference=reference)
    with pytest.raises(ValueError, match="query_authorization_missing"):
        query_registration.register_request(service.store, bad)
    with service.store._connect() as db:
        assert list(db.iterdump()) == before
    assert sends == [1]


from .test_linked_atomic_submission import linked_context  # noqa: F401


def test_linked_risk_authorization_is_not_query_authorization(linked_context):  # noqa: F811
    from ecommerce_product_image_creator import query_registration

    service, args, original, sends = linked_context
    snap = args[4]
    request = query_registration.QueryRequest(
        "not-risk-request",
        snap.task_id,
        1,
        snap.stage,
        "op",
        "source-candidate",
        original.attempt_id,
        snap.execution_digest,
        "not-a-known-upstream",
        "candidate_gateway",
        snap.model,
        snap.endpoint,
        snap.group,
        "test-account",
        "query_original",
        "risk-1",
    )
    with service.store._connect() as db:
        assert db.execute(
            "SELECT 1 FROM risk_reservation WHERE authorization_id='risk-1'"
        ).fetchone()
        before = list(db.iterdump())
    with pytest.raises(ValueError, match="query_authorization_missing"):
        query_registration.register_request(service.store, request)
    with service.store._connect() as db:
        assert list(db.iterdump()) == before
    assert sends == [1]


def test_recover_once_uses_registered_request_without_second_query(
    tmp_path, monkeypatch
):
    from ecommerce_product_image_creator import query_registration, recovery

    from .test_query_registration import setup_registration

    service, req, auth, sends = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        original_query,
        "_query_adapter",
        lambda i: (
            calls.append(i.upstream_id)
            or {"data": {"task_id": i.upstream_id, "status": "IN_PROGRESS"}}
        ),
    )
    query_registration.record_authorization(service.store, auth)
    intent = query_registration.register_request(service.store, req)
    identity = (
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.stage,
        req.attempt_id,
        req.execution_digest_v2,
    )
    first = recovery.recover_once(service.store, *identity, request_id=req.request_id)
    replay = recovery.recover_once(service.store, *identity, request_id=req.request_id)
    assert first.state == replay.state == "running"
    assert first.observation_id == replay.observation_id and calls == [
        "fixture-upstream"
    ]
    assert sends == [1]
    with service.store._connect() as db:
        assert db.execute(
            "SELECT observation_id FROM query_invocation WHERE invocation_id=?",
            (intent.invocation_id,),
        ).fetchone() == (first.observation_id,)


def test_default_query_verifier_rejects_persisted_test_authorization(
    tmp_path, monkeypatch
):
    from ecommerce_product_image_creator import query_registration

    from .test_query_registration import setup_registration

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    intent = query_registration.register_request(service.store, req)
    monkeypatch.setattr(
        query_registration, "_verify_query_authorization", lambda *_: False
    )
    calls = []
    monkeypatch.setattr(original_query, "_query_adapter", lambda i: calls.append(i))
    result = original_query.query_once(
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
        request_id=req.request_id,
    )
    assert (
        result.state == "unverified" and result.reason == "query_authorization_invalid"
    )
    assert result.invocation_id == intent.invocation_id and not calls


def test_revocation_after_entered_prevents_adapter_without_erasing_boundary(
    tmp_path, monkeypatch
):
    from ecommerce_product_image_creator import query_registration

    from .test_query_registration import setup_registration

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    intent = query_registration.register_request(service.store, req)
    monkeypatch.setattr(query_registration, "_verify_query_revocation", lambda *_: True)
    monkeypatch.setattr(
        original_query,
        "_before_query_dispatch",
        lambda: query_registration.revoke_authorization(
            service.store, auth.authorization_id, "test-revoke"
        ),
    )
    calls = []
    monkeypatch.setattr(original_query, "_query_adapter", lambda i: calls.append(i))
    args = (
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
    )
    first = original_query.query_once(*args, request_id=req.request_id)
    assert first.state == "stopped" and first.reason == "stopped_before_dispatch"
    replay = original_query.query_once(*args, request_id=req.request_id)
    assert replay.state == "stopped" and replay.reason == "stopped_before_dispatch"
    assert not calls
    with service.store._connect() as db:
        assert db.execute(
            "SELECT status FROM query_invocation WHERE invocation_id=?",
            (intent.invocation_id,),
        ).fetchone() == ("entered",)
        assert db.execute(
            "SELECT count(*) FROM query_authorization_consumption"
        ).fetchone() == (1,)


def test_replay_keeps_original_observation_after_later_query(tmp_path, monkeypatch):
    import dataclasses

    from ecommerce_product_image_creator import query_registration, recovery

    from .test_query_registration import setup_registration

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    states = iter(["IN_PROGRESS", "SUCCESS"])
    calls = []

    def adapter(i):
        calls.append(i.upstream_id)
        return {"data": {"task_id": i.upstream_id, "status": next(states)}}

    monkeypatch.setattr(original_query, "_query_adapter", adapter)
    identities = (
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
    )
    other_req = dataclasses.replace(
        req, request_id="req-2", query_authorization_reference="auth-2"
    )
    other_auth = dataclasses.replace(
        auth, request_id="req-2", authorization_id="auth-2"
    )
    results = []
    for request, evidence in ((req, auth), (other_req, other_auth)):
        query_registration.record_authorization(service.store, evidence)
        query_registration.register_request(service.store, request)
        results.append(
            original_query.query_once(
                service.store, *identities, request_id=request.request_id
            )
        )
    replay = original_query.query_once(
        service.store, *identities, request_id=req.request_id
    )
    assert [r.state for r in results] == ["running", "succeeded"]
    assert (
        replay.state == "running" and replay.observation_id == results[0].observation_id
    )
    assert len(calls) == 2


def test_new_observation_conflict_blocks_previously_registered_intent(
    tmp_path, monkeypatch
):
    import dataclasses

    from ecommerce_product_image_creator import query_registration, recovery

    from .test_query_registration import setup_registration

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery, "_verify_observation_source", lambda *_: True)
    query_registration.record_authorization(service.store, auth)
    intent = query_registration.register_request(service.store, req)
    statuses = iter(["SUCCESS", "FAILURE"])
    calls = []

    def adapter(i):
        calls.append(i.upstream_id)
        return {"data": {"task_id": i.upstream_id, "status": next(statuses)}}

    monkeypatch.setattr(original_query, "_query_adapter", adapter)
    args = (
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
    )
    for suffix in ("2", "3"):
        other_req = dataclasses.replace(
            req,
            request_id="req-" + suffix,
            query_authorization_reference="auth-" + suffix,
        )
        other_auth = dataclasses.replace(
            auth, request_id="req-" + suffix, authorization_id="auth-" + suffix
        )
        query_registration.record_authorization(service.store, other_auth)
        query_registration.register_request(service.store, other_req)
        original_query.query_once(*args, request_id=other_req.request_id)
    result = original_query.query_once(*args, request_id=req.request_id)
    assert result.state == "conflict" and len(calls) == 2
    with service.store._connect() as db:
        assert db.execute(
            "SELECT status FROM query_invocation WHERE invocation_id=?",
            (intent.invocation_id,),
        ).fetchone() == ("intent",)


@pytest.mark.parametrize("field", ["approved", "authorized"])
def test_ordinary_boolean_cannot_authorize_query(tmp_path, monkeypatch, field):
    from ecommerce_product_image_creator import query_registration

    from .test_query_registration import setup_registration

    service, req, auth, _ = setup_registration(tmp_path, monkeypatch)
    query_registration.record_authorization(service.store, auth)
    intent = query_registration.register_request(service.store, req)
    calls = []
    monkeypatch.setattr(original_query, "_query_adapter", lambda i: calls.append(i))
    args = (
        service.store,
        req.root_operation_id,
        req.candidate_id,
        req.task_id,
        req.revision,
        req.attempt_id,
        req.execution_digest_v2,
    )
    with pytest.raises(TypeError):
        original_query.query_once(*args, request_id=req.request_id, **{field: True})
    assert not calls
    with service.store._connect() as db:
        assert db.execute(
            "SELECT status FROM query_invocation WHERE invocation_id=?",
            (intent.invocation_id,),
        ).fetchone() == ("intent",)
