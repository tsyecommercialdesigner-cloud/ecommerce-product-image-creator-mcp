from ecommerce_product_image_creator.preparation import _review_is_trusted


def test_concept_review_is_team_only(monkeypatch):
    args = (
        "team-responsible-user",
        "task",
        "review",
        "a" * 64,
        "concept_prototype;internal_mock_allowed;unverified=back,sides,zipper",
    )
    monkeypatch.setenv("TEAM_MODE", "false")
    assert not _review_is_trusted(*args)
    monkeypatch.setenv("TEAM_MODE", "true")
    assert _review_is_trusted(*args)
    monkeypatch.delenv("TEAM_MODE", raising=False)
    assert _review_is_trusted(*args)
    assert not _review_is_trusted("other-source", *args[1:])
