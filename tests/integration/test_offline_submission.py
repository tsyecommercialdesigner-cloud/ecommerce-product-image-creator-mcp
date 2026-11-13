import time
from pathlib import Path

import pytest
from PIL import Image

from ecommerce_product_image_creator import offline_submission
from ecommerce_product_image_creator.domain import (
    Confirmation,
    ExecutionSnapshot,
    PromptRevision,
    Reference,
    ReferenceCategory,
    Revision,
    Task,
    TaskStatus,
)
from ecommerce_product_image_creator.preparation import prepare_form, read_prepared
from ecommerce_product_image_creator.storage import ResourceBudget, Store
from ecommerce_product_image_creator.tasks import LocalEligibility, OperationScope


def setup_context(tmp_path: Path, prompt_text: str = "secret prompt"):
    refs, raw = [], []
    for category in ("artwork", "form"):
        path = tmp_path / (category + ".png")
        Image.new("RGB", (3, 3)).save(path)
        raw.append(
            {
                "ref_id": category,
                "category": category,
                "path": str(path),
                "mime": "image/png",
            }
        )
    store = Store(tmp_path / "state.sqlite")
    budget = ResourceBudget(100000, 10000, 100000)
    output = prepare_form(
        store,
        tmp_path,
        budget,
        raw,
        "request",
        prompt_text,
        "gpt-image-2.5-sunburst",
        "https://ai.t8star.org/v1/images/edits?async=true",
        "reverse",
    )
    import hashlib

    for item in raw:
        data = Path(item["path"]).read_bytes()
        refs.append(
            Reference(
                item["ref_id"],
                ReferenceCategory(item["category"]),
                Path(item["path"]),
                item["mime"],
                len(data),
                hashlib.sha256(data).hexdigest(),
                task_id=output["task_id"],
            )
        )
    task = Task(output["task_id"], output["input_digest"], "request", TaskStatus.READY)
    revision = Revision(task.task_id, 1, task.input_digest)
    prompt = PromptRevision.from_text(task.task_id, 1, 1, "form", prompt_text)
    snapshot = ExecutionSnapshot.from_payload(
        read_prepared(store, task.task_id, 1)["snapshot_payload"]
    )
    confirmation = Confirmation(
        task.task_id,
        1,
        task.input_digest,
        prompt.prompt_digest,
        1,
        snapshot.execution_digest,
        confirmation_id="fixture-confirmation",
    )
    scope = OperationScope("generate", None, "six-view-composite", 3)
    store.transition_task(task.task_id, TaskStatus.AWAITING_CONFIRMATION)
    store.transition_task(task.task_id, TaskStatus.READY)
    service = LocalEligibility(store, tmp_path, budget)
    args = (task, revision, refs, prompt, snapshot, confirmation, "op", scope)
    service.prepare(*args, expires_at=time.time() + 120)
    return service, args


def test_default_authorization_refuses_before_intent_or_transport(tmp_path: Path):
    service, args = setup_context(tmp_path)
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    assert service.store.get_operation("op") is None


def test_fixture_accepted_then_duplicate_is_not_sent(tmp_path: Path, monkeypatch):
    service, args = setup_context(tmp_path)
    calls = []
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)

    def fake(request):
        calls.append(request)
        return offline_submission.ProviderResult("accepted", task_id="test-id")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", fake)
    result = offline_submission.submit_offline(service, *args)
    assert result.state == "accepted" and len(calls) == 1
    assert args[2][1].path.read_bytes() in calls[0].body
    assert calls[0].body.count(b'name="image[]"') == 1
    repeated = offline_submission.submit_offline(service, *args)
    assert repeated.state == "accepted" and len(calls) == 1
    operation = service.store.get_operation("op")
    assert operation is not None and operation.upstream_status == "accepted"


def test_timeout_stays_unknown_and_no_replay(tmp_path: Path, monkeypatch):
    service, args = setup_context(tmp_path)
    calls = []
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)

    def fake(request):
        calls.append(request)
        raise TimeoutError("secret prompt")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", fake)
    result = offline_submission.submit_offline(service, *args)
    assert result.state == "unknown" and "secret prompt" not in repr(result)
    assert offline_submission.submit_offline(service, *args).state == "unknown"
    assert len(calls) == 1


def test_parallel_reservation_is_single_dispatch(tmp_path: Path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    service, args = setup_context(tmp_path)
    calls = []
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)

    def fake(request):
        calls.append(request)
        return offline_submission.ProviderResult("accepted", task_id="fake-id")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", fake)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda _: offline_submission.submit_offline(service, *args), range(2)
            )
        )
    assert len(calls) == 1
    assert outcomes[0].attempt_id == outcomes[1].attempt_id
    with service.store._connect() as db:
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM operation WHERE operation_id='op'"
        ).fetchone() == (1,)


def test_intent_survives_new_process(tmp_path: Path, monkeypatch):
    import subprocess
    import sys

    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: offline_submission.ProviderResult("accepted", task_id="fixture"),
    )
    result = offline_submission.submit_offline(service, *args)
    code = "from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store; s=Store(Path(sys.argv[1])).get_operation('op'); print(s.upstream_status, s.upstream_id)"
    child = subprocess.run(
        [sys.executable, "-c", code, str(service.store.path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.strip() == "accepted fixture"
    assert result.local_delivery == "not_available"


def test_tampered_input_and_conflicts_never_dispatch(tmp_path: Path, monkeypatch):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda request: calls.append(request)
    )
    with service.store._connect() as db:
        db.execute(
            "UPDATE prepared_conflicts SET reported=? WHERE task_id=? AND revision=1",
            ('["unresolved"]', args[0].task_id),
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    with service.store._connect() as db:
        db.execute(
            "UPDATE prepared_conflicts SET reported='[]' WHERE task_id=? AND revision=1",
            (args[0].task_id,),
        )
        db.execute(
            "UPDATE prepared_reference SET data=? WHERE task_id=? AND revision=1 AND ref_id='form'",
            (b"altered", args[0].task_id),
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    assert calls == [] and service.store.get_operation("op") is None


def test_intent_transaction_rolls_back_on_insert_failure(tmp_path: Path, monkeypatch):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda req: calls.append(req)
    )
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER reject_test_operation BEFORE INSERT ON operation BEGIN SELECT RAISE(ABORT, 'blocked'); END"
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    with service.store._connect() as db:
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
        ).fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM attempt").fetchone() == (0,)
    assert calls == []


def test_crash_before_response_keeps_intent_uncertain(tmp_path: Path, monkeypatch):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []

    def crash(request):
        calls.append(request)
        raise KeyboardInterrupt()

    monkeypatch.setattr(offline_submission, "_offline_dispatch", crash)
    with pytest.raises(KeyboardInterrupt):
        offline_submission.submit_offline(service, *args)
    operation = service.store.get_operation("op")
    assert operation is not None and operation.upstream_status == "submission_uncertain"
    assert offline_submission.submit_offline(service, *args).state == "unknown"
    assert len(calls) == 1


def test_operation_identity_conflict_does_not_dispatch(tmp_path: Path, monkeypatch):
    from dataclasses import replace

    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: offline_submission.ProviderResult("accepted", task_id="fixture"),
    )
    offline_submission.submit_offline(service, *args)
    other = replace(args[0], task_id="different")
    with pytest.raises(offline_submission.SubmissionError, match="conflicts"):
        offline_submission.submit_offline(service, other, *args[1:])
    altered = ExecutionSnapshot.create(
        args[1],
        args[3],
        "form",
        "gpt-image-2.5-flare",
        args[4].endpoint,
        args[4].group,
        references=args[4].references,
        operation_scope=args[4].operation_scope,
    )
    with pytest.raises(offline_submission.SubmissionError, match="conflicts"):
        offline_submission.submit_offline(service, *args[:4], altered, *args[5:])


def test_legacy_snapshot_rejected_before_dispatch(tmp_path: Path):
    service, args = setup_context(tmp_path)
    legacy = ExecutionSnapshot.create(
        args[1], args[3], "form", args[4].model, args[4].endpoint, args[4].group
    )
    with pytest.raises(offline_submission.SubmissionError, match="v2"):
        offline_submission.submit_offline(service, *args[:4], legacy, *args[5:])


@pytest.mark.parametrize(
    "reported, expected",
    [
        ("pending", "accepted"),
        ("running", "running"),
        ("failed", "failed"),
        ("succeeded", "succeeded"),
    ],
)
def test_fake_upstream_states_do_not_imply_delivery(
    tmp_path: Path, monkeypatch, reported: str, expected: str
):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: offline_submission.ProviderResult(reported, task_id="fake-id"),
    )
    result = offline_submission.submit_offline(service, *args)
    assert result.state == expected and result.local_delivery == "not_available"


def test_unconfirmed_packaging_versions_stop_before_intent(tmp_path: Path, monkeypatch):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    from ecommerce_product_image_creator import preparation

    original = preparation._response_details

    def changed(*args):
        details = original(*args)
        details["packaging_versions"]["status"] = "multiple_declared_versions"
        return details

    monkeypatch.setattr(preparation, "_response_details", changed)
    calls = []
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda request: calls.append(request)
    )
    with pytest.raises(offline_submission.SubmissionError, match="clarification"):
        offline_submission.submit_offline(service, *args)
    assert not calls and service.store.get_operation("op") is None


def test_unconfirmed_subset_proposal_blocks_offline_transport(
    tmp_path: Path, monkeypatch
):
    from ecommerce_product_image_creator import business_choices

    service, args = setup_context(tmp_path)
    business_choices.record_subset_proposal(
        service.store, "proposal", args[0].task_id, 1, ["form"], "fixture", "cost"
    )
    calls = []
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda request: calls.append(request)
    )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    assert calls == []
    assert service.store.get_operation("op") is None


def test_subset_decision_revocation_and_expiry_do_not_release_gate(
    tmp_path: Path, monkeypatch
):
    from ecommerce_product_image_creator import business_choices

    service, args = setup_context(tmp_path)
    business_choices.record_subset_proposal(
        service.store, "proposal", args[0].task_id, 1, ["form"], "fixture", "cost"
    )
    business_choices.record_subset_decision(
        service.store, "proposal", "decision", "rejected", "fixture", time.time() + 100
    )
    monkeypatch.setattr(business_choices, "_subset_source_is_trusted", lambda *_: True)
    business_choices.invalidate_subset_decision(service.store, "proposal", "revoked")
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda req: calls.append(req)
    )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    assert calls == []
    assert (
        business_choices.read_subset_proposal(service.store, "proposal")[
            "invalidation_reason"
        ]
        == "revoked"
    )


def test_confirmed_structure_preview_can_pass_offline_gate(tmp_path: Path, monkeypatch):
    from ecommerce_product_image_creator import business_choices

    service, args = setup_context(tmp_path)
    task, revision, _, _, snapshot, *_ = args
    with service.store._connect() as db:
        db.execute(
            "UPDATE prepared_conflicts SET reported=? WHERE task_id=? AND revision=?",
            ('["seal differs"]', task.task_id, revision.revision),
        )
    c = business_choices.BusinessChoice(
        "c",
        task.task_id,
        revision.revision,
        revision.input_digest,
        snapshot.execution_digest,
        "uncertainty",
        {
            "conflicts": ["seal differs"],
            "assumptions": {"bottom": "W"},
            "mode": "preview",
        },
        "fixture",
        time.time(),
        expires_at=time.time() + 120,
    )
    business_choices.record_choice(service.store, c)
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []

    def send(req):
        calls.append(req)
        return offline_submission.ProviderResult("accepted", task_id="fake")

    monkeypatch.setattr(offline_submission, "_offline_dispatch", send)
    assert offline_submission.submit_offline(service, *args).state == "accepted"
    assert len(calls) == 1


def test_revocation_after_intent_before_dispatch_is_not_sent(
    tmp_path: Path, monkeypatch
):
    from ecommerce_product_image_creator import business_choices

    service, args = setup_context(tmp_path)
    business_choices.record_subset_proposal(
        service.store, "proposal", args[0].task_id, 1, ["form"], "fixture", "cost"
    )
    business_choices.record_subset_decision(
        service.store, "proposal", "decision", "rejected", "fixture", time.time() + 120
    )
    monkeypatch.setattr(business_choices, "_subset_source_is_trusted", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission,
        "_after_intent_recorded",
        lambda: business_choices.invalidate_subset_decision(
            service.store, "proposal", "revoked"
        ),
    )
    calls = []
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda req: calls.append(req)
    )
    outcome = offline_submission.submit_offline(service, *args)
    assert outcome.state == "unknown"
    assert calls == []
    assert service.store.get_operation("op") is not None
    assert offline_submission.submit_offline(service, *args).state == "unknown"
    assert calls == []


def test_dispatch_and_revocation_are_locally_ordered(tmp_path: Path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from ecommerce_product_image_creator import business_choices

    service, args = setup_context(tmp_path)
    business_choices.record_subset_proposal(
        service.store, "proposal", args[0].task_id, 1, ["form"], "fixture", "cost"
    )
    business_choices.record_subset_decision(
        service.store, "proposal", "decision", "rejected", "fixture", time.time() + 120
    )
    monkeypatch.setattr(business_choices, "_subset_source_is_trusted", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    entered, revoke_started, release = Event(), Event(), Event()
    calls = []

    def send(req):
        calls.append(req)
        entered.set()
        assert release.wait(5)
        raise TimeoutError("fixture")

    def revoke():
        revoke_started.set()
        business_choices.invalidate_subset_decision(
            service.store, "proposal", "revoked"
        )

    monkeypatch.setattr(offline_submission, "_offline_dispatch", send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        submission = pool.submit(offline_submission.submit_offline, service, *args)
        assert entered.wait(5)
        revocation = pool.submit(revoke)
        assert revoke_started.wait(5)
        assert not revocation.done()
        release.set()
        assert submission.result(timeout=10).state == "unknown"
        revocation.result(timeout=10)
    assert offline_submission.submit_offline(service, *args).state == "unknown"
    assert len(calls) == 1


def test_preview_evidence_survives_submission_artifact_and_revocation(
    tmp_path: Path, monkeypatch
):
    import hashlib

    from ecommerce_product_image_creator import business_choices
    from ecommerce_product_image_creator.artifact_reader import read_artifact

    service, args = setup_context(tmp_path)
    task, revision, _, _, snapshot, *_ = args
    with service.store._connect() as db:
        db.execute(
            "UPDATE prepared_conflicts SET reported=? WHERE task_id=? AND revision=?",
            ('["seal differs"]', task.task_id, revision.revision),
        )
    c = business_choices.BusinessChoice(
        "preview-decision",
        task.task_id,
        revision.revision,
        revision.input_digest,
        snapshot.execution_digest,
        "uncertainty",
        {
            "conflicts": ["seal differs"],
            "assumptions": {"bottom": "W"},
            "mode": "preview",
        },
        "fixture",
        time.time(),
        expires_at=time.time() + 120,
    )
    business_choices.record_choice(service.store, c)
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: offline_submission.ProviderResult("succeeded", task_id="fake"),
    )
    result = offline_submission.submit_offline(service, *args)
    assert result.preview["decision_id"] == "preview-decision"
    assert result.preview["assumptions"] == {"bottom": "W"}
    assert result.preview["structure_verified"] is False
    assert result.preview["source_trust"] == "unverified"
    assert result.preview["local_source_check"] == "accepted_by_configured_verifier"
    assert result.preview["execution_digest"] == snapshot.execution_digest
    assert result.preview["operation_id"] == "op"
    assert result.preview["attempt_id"] == result.attempt_id
    artifact = tmp_path / "candidate.png"
    Image.new("RGB", (4, 4), "green").save(artifact)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    with service.store._connect() as db:
        db.execute(
            "UPDATE attempt SET delivery_status='delivered' WHERE attempt_id=?",
            (result.attempt_id,),
        )
        db.execute(
            "INSERT INTO artifact VALUES(?,?,?,?)",
            ("artifact", result.attempt_id, str(artifact), digest),
        )
    from ecommerce_product_image_creator.review import FormReview, save_review

    save_review(
        service.store,
        FormReview(
            "fixture-review",
            task.task_id,
            revision.revision,
            "artifact",
            digest,
            "accept",
            "fixture",
            time.time(),
            None,
        ),
    )
    query = read_artifact(
        service.store,
        task.task_id,
        "form",
        revision.revision,
        result.attempt_id,
        "artifact",
        tmp_path,
        service.budget,
    )
    assert query["preview"]["decision_id"] == "preview-decision"
    assert query["preview"]["assumptions"] == {"bottom": "W"}
    assert query["preview"]["structure_verified"] is False
    with service.store._connect() as db:
        db.execute(
            "UPDATE business_choice SET source='changed' WHERE choice_id='preview-decision'"
        )
    mismatch = read_artifact(
        service.store,
        task.task_id,
        "form",
        revision.revision,
        result.attempt_id,
        "artifact",
        tmp_path,
        service.budget,
    )
    assert mismatch["preview"]["current_decision"] == "mismatch"
    assert mismatch["preview"]["assumptions"] == {"bottom": "W"}
    with service.store._connect() as db:
        db.execute(
            "UPDATE business_choice SET source='fixture' WHERE choice_id='preview-decision'"
        )
    with service.store._connect() as db:
        db.execute(
            "UPDATE business_choice_detail SET expires_at=? WHERE choice_id='preview-decision'",
            (time.time() - 1,),
        )
    expired = read_artifact(
        service.store,
        task.task_id,
        "form",
        revision.revision,
        result.attempt_id,
        "artifact",
        tmp_path,
        service.budget,
    )
    assert expired["preview"]["current_decision"] == "expired"
    with service.store._connect() as db:
        db.execute(
            "UPDATE business_choice_detail SET expires_at=? WHERE choice_id='preview-decision'",
            (time.time() + 120,),
        )
    raw_refs = [
        {
            "ref_id": r.ref_id,
            "category": r.category.value,
            "path": str(r.path),
            "mime": r.mime,
        }
        for r in args[2]
    ]
    prepare_form(
        service.store,
        tmp_path,
        service.budget,
        raw_refs,
        "request",
        "new prompt",
        snapshot.model,
        snapshot.endpoint,
        snapshot.group,
        task_id=task.task_id,
    )
    superseded = read_artifact(
        service.store,
        task.task_id,
        "form",
        revision.revision,
        result.attempt_id,
        "artifact",
        tmp_path,
        service.budget,
    )
    assert superseded["preview"]["current_decision"] == "superseded"
    business_choices.invalidate_choice(service.store, "preview-decision", "withdrawn")
    again = read_artifact(
        service.store,
        task.task_id,
        "form",
        revision.revision,
        result.attempt_id,
        "artifact",
        tmp_path,
        service.budget,
    )
    assert again["preview"]["current_decision"] == "invalidated"
    assert again["preview"]["assumptions"] == {"bottom": "W"}
    repeated = offline_submission.submit_offline(service, *args)
    assert repeated.preview["current_decision"] == "invalidated"
    assert repeated.preview["decision_id"] == "preview-decision"
    import subprocess
    import sys

    code = "from pathlib import Path; import sys; from ecommerce_product_image_creator.storage import Store,ResourceBudget; from ecommerce_product_image_creator.artifact_reader import read_artifact; p=read_artifact(Store(Path(sys.argv[1])),sys.argv[2],'form',1,sys.argv[3],'artifact',Path(sys.argv[4]),ResourceBudget(100000,10000,100000))['preview']; print(p['decision_id'],p['assumptions']['bottom'],p['current_decision'],p['structure_verified'])"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(service.store.path),
            task.task_id,
            result.attempt_id,
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "preview-decision W invalidated False"
    artifact.write_bytes(b"corrupt")
    corrupt = read_artifact(
        service.store,
        task.task_id,
        "form",
        revision.revision,
        result.attempt_id,
        "artifact",
        tmp_path,
        service.budget,
    )
    assert (
        corrupt["delivery_status"] == "delivered"
        and corrupt["file"]["state"] == "invalid_or_unreadable"
    )
    assert corrupt["preview"]["assumptions"] == {"bottom": "W"}
    assert corrupt["review"]["decision"] == "accept"
    assert corrupt["review_applicability"] == "invalid_artifact"


def test_preview_choice_does_not_grant_billing_authorization(
    tmp_path: Path, monkeypatch
):
    from ecommerce_product_image_creator import business_choices

    service, args = setup_context(tmp_path)
    task, revision, _, _, snapshot, *_ = args
    with service.store._connect() as db:
        db.execute(
            "UPDATE prepared_conflicts SET reported=? WHERE task_id=? AND revision=?",
            ('["conflict"]', task.task_id, revision.revision),
        )
    business_choices.record_choice(
        service.store,
        business_choices.BusinessChoice(
            "c",
            task.task_id,
            revision.revision,
            revision.input_digest,
            snapshot.execution_digest,
            "uncertainty",
            {
                "conflicts": ["conflict"],
                "assumptions": {"bottom": "W"},
                "mode": "preview",
            },
            "fixture",
            time.time(),
        ),
    )
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda req: calls.append(req)
    )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    assert calls == [] and service.store.get_operation("op") is None


def test_missing_historical_preview_is_unknown(tmp_path: Path):
    from ecommerce_product_image_creator.preview_evidence import read_preview

    service, _ = setup_context(tmp_path)
    with service.store._connect() as db:
        preview = read_preview(db, "historical-attempt")
    assert preview["status"] == "unknown" and preview["structure_verified"] is False


def test_unobserved_structure_preview_is_not_lost_without_reported_conflict(
    tmp_path: Path, monkeypatch
):
    from ecommerce_product_image_creator import business_choices

    service, args = setup_context(tmp_path)
    task, revision, _, _, snapshot, *_ = args
    choice = business_choices.BusinessChoice(
        "unknown-preview",
        task.task_id,
        revision.revision,
        revision.input_digest,
        snapshot.execution_digest,
        "uncertainty",
        {"conflicts": [], "assumptions": {"bottom": "W"}, "mode": "preview"},
        "fixture",
        time.time(),
    )
    business_choices.record_choice(service.store, choice)
    monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: offline_submission.ProviderResult("accepted", task_id="fake"),
    )
    result = offline_submission.submit_offline(service, *args)
    assert result.preview["decision_id"] == "unknown-preview"
    assert result.preview["conflicts"] == [] and result.preview["assumptions"] == {
        "bottom": "W"
    }
    assert result.preview["structure_verified"] is False


@pytest.mark.parametrize("condition", ["unverified", "expired", "revoked"])
def test_preview_decision_failure_never_dispatches(
    tmp_path: Path, monkeypatch, condition: str
):
    from ecommerce_product_image_creator import business_choices

    service, args = setup_context(tmp_path)
    task, revision, _, _, snapshot, *_ = args
    with service.store._connect() as db:
        db.execute(
            "UPDATE prepared_conflicts SET reported=? WHERE task_id=? AND revision=?",
            ('["conflict"]', task.task_id, revision.revision),
        )
    now = time.time()
    choice = business_choices.BusinessChoice(
        "preview",
        task.task_id,
        revision.revision,
        revision.input_digest,
        snapshot.execution_digest,
        "uncertainty",
        {"conflicts": ["conflict"], "assumptions": {"bottom": "W"}, "mode": "preview"},
        "fixture",
        now - 30,
        expires_at=now - 1 if condition == "expired" else now + 120,
    )
    business_choices.record_choice(service.store, choice)
    if condition != "unverified":
        monkeypatch.setattr(business_choices, "_source_is_trusted", lambda *_: True)
    if condition == "revoked":
        business_choices.invalidate_choice(service.store, "preview", "revoked")
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []
    monkeypatch.setattr(
        offline_submission, "_offline_dispatch", lambda req: calls.append(req)
    )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    assert calls == [] and service.store.get_operation("op") is None


def test_first_attempt_budget_is_recorded_with_submission_intent(
    tmp_path: Path, monkeypatch
):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: offline_submission.ProviderResult("accepted", task_id="fixture"),
    )
    result = offline_submission.submit_offline(service, *args)
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count FROM attempt_counter WHERE operation_id=? AND model=? AND endpoint=?",
            (result.operation_id, args[4].model, args[4].endpoint),
        ).fetchone() == (1,)


def test_first_attempt_budget_rolls_back_with_intent_failure(
    tmp_path: Path, monkeypatch
):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER fail_counter BEFORE INSERT ON attempt_counter BEGIN SELECT RAISE(ABORT, 'fixture'); END"
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM operation WHERE operation_id='op'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
        ).fetchone() == (0,)


def test_new_operation_id_cannot_repeat_same_unknown_execution(
    tmp_path: Path, monkeypatch
):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    calls = []

    def fake(_):
        calls.append(1)
        raise TimeoutError()

    monkeypatch.setattr(offline_submission, "_offline_dispatch", fake)
    assert offline_submission.submit_offline(service, *args).state == "unknown"
    from dataclasses import replace

    new_confirmation = replace(args[5], confirmation_id="other-confirmation")
    new_args = (*args[:5], new_confirmation, "other-op", args[7])
    service.prepare(*new_args, expires_at=time.time() + 120)
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *new_args)
    assert len(calls) == 1


def test_initial_attempt_chain_is_atomic_and_persists(tmp_path: Path, monkeypatch):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    monkeypatch.setattr(
        offline_submission,
        "_offline_dispatch",
        lambda _: offline_submission.ProviderResult("accepted", task_id="fixture"),
    )
    result = offline_submission.submit_offline(service, *args)
    assert (
        Store(service.store.path).attempt_chain_status(result.operation_id)
        == "initial_recorded"
    )
    with Store(service.store.path)._connect() as db:
        row = db.execute(
            "SELECT sequence,attempt_id,model,endpoint,execution_digest FROM operation_attempt WHERE operation_id=?",
            (result.operation_id,),
        ).fetchone()
    assert row == (
        1,
        result.attempt_id,
        args[4].model,
        args[4].endpoint,
        args[4].execution_digest,
    )


def test_attempt_chain_failure_rolls_back_eligibility(tmp_path: Path, monkeypatch):
    service, args = setup_context(tmp_path)
    monkeypatch.setattr(offline_submission, "_verify_authorization", lambda *_: True)
    monkeypatch.setattr(offline_submission, "_verify_business_choices", lambda *_: True)
    with service.store._connect() as db:
        db.execute(
            "CREATE TRIGGER fail_chain BEFORE INSERT ON operation_attempt BEGIN SELECT RAISE(ABORT, 'fixture'); END"
        )
    with pytest.raises(offline_submission.SubmissionError):
        offline_submission.submit_offline(service, *args)
    with service.store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM operation WHERE operation_id='op'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT count(*) FROM attempt_counter WHERE operation_id='op'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT consumed FROM local_eligibility WHERE operation_id='op'"
        ).fetchone() == (0,)
