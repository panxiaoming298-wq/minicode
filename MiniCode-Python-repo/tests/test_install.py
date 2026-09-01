from __future__ import annotations

from typing import Any

import minicode.install as installer


def test_installer_defaults_to_first_class_deepseek_configuration(
    monkeypatch,
) -> None:
    saved: dict[str, Any] = {}
    required_values = iter(["deepseek-v4-pro", "https://api.deepseek.com"])

    monkeypatch.setattr(installer, "load_effective_settings", lambda: {})
    monkeypatch.setattr(installer, "_read_provider", lambda default: default)
    monkeypatch.setattr(
        installer,
        "_require_input",
        lambda prompt, default=None: next(required_values),
    )
    monkeypatch.setattr(
        installer,
        "_read_input",
        lambda prompt, default=None: "deepseek-test-key",
    )
    monkeypatch.setattr(installer, "save_mini_code_settings", saved.update)
    monkeypatch.setattr(installer, "_install_launcher_script", lambda: None)

    installer.main()

    assert saved["provider"] == "deepseek"
    assert saved["model"] == "deepseek-v4-pro"
    assert saved["env"] == {
        "MINI_CODE_PROVIDER": "deepseek",
        "MINI_CODE_MODEL": "deepseek-v4-pro",
        "DEEPSEEK_MODEL": "deepseek-v4-pro",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "DEEPSEEK_API_KEY": "deepseek-test-key",
    }


def test_installer_profiles_cover_every_supported_provider() -> None:
    assert set(installer._PROVIDER_INSTALL_DEFAULTS) == {
        "deepseek",
        "anthropic",
        "openai",
        "openrouter",
        "custom",
    }
    assert all(
        {"model", "model_env", "base_env", "base_url", "key_env"}
        <= set(profile)
        for profile in installer._PROVIDER_INSTALL_DEFAULTS.values()
    )
