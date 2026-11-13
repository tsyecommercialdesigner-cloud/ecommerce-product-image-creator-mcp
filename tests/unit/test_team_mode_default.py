from ecommerce_product_image_creator import config, provider, team_query_policy


def test_default_team_and_explicit_strict_in_query_policy(monkeypatch):
    monkeypatch.delenv("TEAM_MODE", raising=False)
    monkeypatch.setenv("TEAM_GATEWAY_URL", provider.ENDPOINT)
    monkeypatch.setenv("ENABLE_REAL_GENERATION", "true")
    monkeypatch.setenv("ENABLE_TEAM_QUERY", "true")
    with team_query_policy.explicit_refresh(object()):
        assert config.team_mode_enabled()
        assert team_query_policy.enabled()
        monkeypatch.setenv("TEAM_MODE", "false")
        assert not config.team_mode_enabled()
        assert not team_query_policy.enabled()
        monkeypatch.setenv("TEAM_MODE", "invalid")
        assert not config.team_mode_enabled()
        assert not team_query_policy.enabled()
