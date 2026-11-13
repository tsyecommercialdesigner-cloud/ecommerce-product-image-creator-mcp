"""Explicitly isolated migration of legacy query tests to the real query gate."""

import time
import uuid

from ecommerce_product_image_creator import original_query, query_registration


def install(monkeypatch):
    execute = original_query.query_once
    monkeypatch.setattr(
        original_query, "_query_account_scope", lambda _: "fixture-account"
    )
    monkeypatch.setattr(
        query_registration,
        "_verify_query_authorization",
        lambda _db, e: e.source_reference == "fixture-query-auth",
    )

    def registered_query(
        store, root, candidate, task, revision, attempt, digest, *, request_id=None
    ):
        if request_id is not None:
            return execute(
                store,
                root,
                candidate,
                task,
                revision,
                attempt,
                digest,
                request_id=request_id,
            )
        try:
            identity = original_query._identity(
                store, root, candidate, task, revision, attempt, digest
            )
            if identity is None:
                return execute(store, root, candidate, task, revision, attempt, digest)
            request_id = uuid.uuid4().hex
            auth_id = uuid.uuid4().hex
            account = original_query._query_account_scope(identity)
            if not account:
                return original_query.QueryResult(
                    "unverified", "account_scope_unverified"
                )
            request = query_registration.QueryRequest(
                request_id,
                task,
                revision,
                identity.stage,
                root,
                candidate,
                attempt,
                digest,
                identity.upstream_id,
                identity.provider,
                identity.model,
                identity.endpoint,
                identity.group,
                account,
                "query_original",
                auth_id,
            )
            auth = query_registration.QueryAuthorization(
                auth_id,
                request_id,
                attempt,
                digest,
                identity.provider,
                account,
                "query_original",
                1,
                "fixture-query-auth",
                time.time() + 300,
                "isolated fixture, unknown query cost",
            )
            query_registration.record_authorization(store, auth)
            query_registration.register_request(store, request)
        except ValueError as exc:
            if str(exc) in (
                "query_observation_conflict",
                "query_observation_unverified",
            ):
                return original_query.QueryResult("conflict", str(exc))
            return original_query.QueryResult(
                "unverified", "identity_or_source_unverified"
            )
        return execute(
            store,
            root,
            candidate,
            task,
            revision,
            attempt,
            digest,
            request_id=request_id,
        )

    monkeypatch.setattr(original_query, "query_once", registered_query)
