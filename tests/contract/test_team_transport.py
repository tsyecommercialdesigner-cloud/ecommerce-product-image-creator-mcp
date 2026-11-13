"""The proposed transport stays isolated behind a deny-by-default gate."""

import hashlib
from io import BytesIO

import pytest
from PIL import Image

from ecommerce_product_image_creator import config, provider, team_transport


def request():
    buf = BytesIO()
    Image.new("RGB", (3, 3)).save(buf, format="PNG")
    data = buf.getvalue()
    candidate = provider.CandidateRequest(
        "gpt-image-2.5-sunburst",
        provider.ENDPOINT,
        "reverse",
        "test prompt",
        (
            provider.ImagePart(
                "one", "image/png", hashlib.sha256(data).hexdigest(), data
            ),
        ),
        stage="form",
    )
    return team_transport.TeamRequest(
        "operation",
        "attempt",
        "a" * 64,
        candidate.model,
        provider.serialize_request(candidate),
    )


def test_defaults_and_unverified_authorization_do_not_open_socket(monkeypatch):
    def forbidden(*_args):
        raise AssertionError("network boundary reached")

    monkeypatch.setattr(team_transport, "_open_connection", forbidden)
    monkeypatch.setattr(config, "load_config", forbidden)
    with pytest.raises(ValueError, match="team_generation_disabled"):
        team_transport.send_once(request(), team_transport.TeamSettings())
    with pytest.raises(ValueError, match="team_authorization_unverified"):
        team_transport.send_once(request(), team_transport.TeamSettings(True, True))


@pytest.mark.parametrize("failure", [False, True])
def test_fake_transport_sends_only_one_post(monkeypatch, failure):
    calls = []

    class Response:
        status = 200

        def read(self, _limit):
            return b'{"task_id":"test-id"}'

    class Connection:
        def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            if failure:
                raise TimeoutError("sensitive transport detail")

        def getresponse(self):
            return Response()

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(team_transport, "_verify_team_authorization", lambda _: True)
    monkeypatch.setattr(team_transport, "_open_connection", lambda *_: Connection())
    monkeypatch.setattr(config, "load_config", lambda: config.Config("fixture-key"))
    result = team_transport.send_once(
        request(), team_transport.TeamSettings(True, True)
    )
    assert result.state == ("unknown" if failure else "accepted")
    assert len([item for item in calls if isinstance(item, tuple)]) == 1
    assert calls[-1] == "closed"
    assert "fixture-key" not in repr(result)


def test_team_is_default_but_paid_generation_is_opt_in(monkeypatch):
    monkeypatch.delenv("TEAM_MODE", raising=False)
    monkeypatch.delenv("ENABLE_REAL_GENERATION", raising=False)
    assert (
        team_transport.TeamSettings.from_environment()
        == team_transport.TeamSettings(enabled=True)
    )
    monkeypatch.setenv("TEAM_MODE", "false")
    monkeypatch.setenv("ENABLE_REAL_GENERATION", "true")
    assert (
        team_transport.TeamSettings.from_environment()
        == team_transport.TeamSettings(enabled=False, generation_enabled=True)
    )
    monkeypatch.setenv("TEAM_MODE", "invalid")
    assert not team_transport.TeamSettings.from_environment().enabled


def test_team_query_is_opt_in_and_uses_original_task_id(monkeypatch):
    calls = []

    class Response:
        status = 200

        def read(self, _limit):
            return b'{"data":{"task_id":"up-1","status":"SUCCESS"}}'

    class Connection:
        def request(self, method, path, **kwargs):
            calls.append((method, path))

        def getresponse(self):
            return Response()

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(team_transport, "_open_connection", lambda *_: Connection())
    monkeypatch.setattr(config, "load_config", lambda: config.Config("fixture-key"))
    monkeypatch.delenv("ENABLE_TEAM_QUERY", raising=False)
    with pytest.raises(ValueError, match="team_query_disabled"):
        team_transport.query_once(
            "up-1", provider.ENDPOINT, team_transport.TeamSettings(True, True)
        )
    assert calls == []
    monkeypatch.setenv("ENABLE_TEAM_QUERY", "true")
    with pytest.raises(ValueError, match="team_query_invalid"):
        team_transport.query_once(
            "../other", provider.ENDPOINT, team_transport.TeamSettings(True, True)
        )
    payload = team_transport.query_once(
        "up-1", provider.ENDPOINT, team_transport.TeamSettings(True, True)
    )
    assert (
        provider.parse_query(200, __import__("json").loads(payload), "up-1").state
        == "succeeded"
    )
    assert calls == [("GET", "/v1/images/tasks/up-1"), "closed"]
