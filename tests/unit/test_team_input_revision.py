from ecommerce_product_image_creator.input_revision import _source_is_trusted


def test_team_local_input_change_does_not_enable_strict_mode(monkeypatch):
    record = {
        "source": "team-local-input-change",
        "evidence_reference": "team-explicit-preparation",
    }
    monkeypatch.setenv("TEAM_MODE", "false")
    assert not _source_is_trusted(record)
    monkeypatch.setenv("TEAM_MODE", "true")
    assert _source_is_trusted(record)
    monkeypatch.delenv("TEAM_MODE", raising=False)
    assert _source_is_trusted(record)
    assert not _source_is_trusted({**record, "source": "caller"})
    assert not _source_is_trusted({**record, "evidence_reference": "unknown"})
