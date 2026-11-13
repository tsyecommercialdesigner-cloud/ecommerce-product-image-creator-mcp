"""Crash after a fake Team response preserves the existing unknown intent."""

import pickle
import subprocess
import sys
from pathlib import Path

import pytest

from .test_offline_submission import setup_context


def test_team_response_lost_before_persistence_does_not_resend(
    tmp_path: Path, monkeypatch
):
    service, args = setup_context(tmp_path)
    fixture = tmp_path / "context.bin"
    fixture.write_bytes(pickle.dumps((service, args)))
    marker = tmp_path / "send-count"
    child = r"""
import os,pickle,sys
from pathlib import Path
from ecommerce_product_image_creator import config, offline_submission as sub, team_transport
service,args=pickle.loads(Path(sys.argv[1]).read_bytes())
os.environ['TEAM_MODE']='true'; os.environ['ENABLE_REAL_GENERATION']='true'
config.load_config=lambda: config.Config('fake-key')
class Response:
    status=200
    def read(self,n): return b'{"task_id":"fake-upstream"}'
class Connection:
    def request(self,*a,**kw): Path(sys.argv[2]).write_text('one')
    def getresponse(self): return Response()
    def close(self): pass
team_transport._open_connection=lambda *a: Connection()
sub._after_team_response_received=lambda: os._exit(71)
sub.submit_offline(service,*args,team_live=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", child, str(fixture), str(marker)],
        capture_output=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 71, result.stderr
    assert marker.read_text() == "one"
    with service.store._connect() as db:
        assert db.execute(
            "SELECT upstream_status,upstream_id FROM attempt"
        ).fetchall() == [("submission_uncertain", None)]
        assert db.execute("SELECT count(*) FROM operation").fetchone() == (1,)
    monkeypatch.setenv("TEAM_MODE", "true")
    monkeypatch.setenv("ENABLE_REAL_GENERATION", "true")
    from ecommerce_product_image_creator import offline_submission, team_transport

    def forbidden(*_args):
        raise AssertionError("duplicate send")

    monkeypatch.setattr(team_transport, "_open_connection", forbidden)
    replay = offline_submission.submit_offline(service, *args, team_live=True)
    assert replay.state == "unknown" and replay.replayed
    assert marker.read_text() == "one"


def test_team_retry_cannot_enter_transport(tmp_path: Path, monkeypatch):
    from ecommerce_product_image_creator import offline_submission, team_transport

    service, args = setup_context(tmp_path)
    monkeypatch.setenv("TEAM_MODE", "true")
    monkeypatch.setenv("ENABLE_REAL_GENERATION", "true")

    def forbidden(*_args):
        raise AssertionError("unexpected retry")

    monkeypatch.setattr(team_transport, "_open_connection", forbidden)
    with pytest.raises(
        offline_submission.SubmissionError, match="Team submission unavailable"
    ):
        offline_submission.submit_offline(
            service, *args, team_live=True, retry_of="root"
        )
    with service.store._connect() as db:
        assert db.execute("SELECT count(*) FROM attempt").fetchone() == (0,)
