import pytest

from internal.config import ConfigurationError, load_settings


def test_complete_settings_load(environment):
    settings = load_settings()
    assert settings.TELEGRAM_GROUPS == frozenset({-1001, -1002})
    assert settings.ROLE_OWNER == "owner"


@pytest.mark.parametrize(
    "name", ["OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_ROLES_CLAIM", "ROLE_SUPPORT"]
)
def test_no_start_without_the_provider(environment, monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(ConfigurationError, match=name):
        load_settings()


@pytest.mark.parametrize("name", ["ROLE_ANALYTICS", "ANALYTICS_ROLES_CLAIM"])
def test_no_start_without_the_role_for_aggregate_questions(environment, monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(ConfigurationError, match=name):
        load_settings()


def test_no_start_without_the_owner_role(environment, monkeypatch):
    monkeypatch.delenv("ROLE_OWNER")
    with pytest.raises(ConfigurationError, match="ROLE_OWNER"):
        load_settings()


def test_empty_group_list_means_private_chats_only(environment, monkeypatch):
    monkeypatch.setenv("TELEGRAM_GROUPS", "")
    assert load_settings().TELEGRAM_GROUPS == frozenset()


def test_group_list_is_required_even_if_empty(environment, monkeypatch):
    monkeypatch.delenv("TELEGRAM_GROUPS")
    with pytest.raises(ConfigurationError, match="TELEGRAM_GROUPS"):
        load_settings()


def test_group_is_a_chat_number_not_a_name(environment, monkeypatch):
    monkeypatch.setenv("TELEGRAM_GROUPS", "-1001,support-team")
    with pytest.raises(ConfigurationError, match="support-team"):
        load_settings()


def test_error_names_the_setting_but_not_the_secret(environment, monkeypatch):
    monkeypatch.setenv("TELEGRAM_EGRESS", "proxy")
    with pytest.raises(ConfigurationError) as error:
        load_settings()
    assert "TELEGRAM_PROXY_URL" in str(error.value)
    assert "test-token" not in str(error.value)


def test_secrets_do_not_show_in_the_settings_repr(environment):
    shown = repr(load_settings())
    for secret in ("test-token", "client-secret", "test-model-key"):
        assert secret not in shown


def test_model_through_a_proxy_needs_its_address(environment, monkeypatch):
    monkeypatch.setenv("MODEL_EGRESS", "proxy")
    with pytest.raises(ConfigurationError, match="MODEL_PROXY_URL"):
        load_settings()


def test_model_address_of_a_proxy_with_direct_egress_is_a_mistake(environment, monkeypatch):
    monkeypatch.setenv("MODEL_PROXY_URL", "socks5://user:secret@proxy.test:1080")
    with pytest.raises(ConfigurationError, match="MODEL_PROXY_URL") as error:
        load_settings()
    assert "secret" not in str(error.value)
