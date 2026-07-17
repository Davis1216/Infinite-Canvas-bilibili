from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_SETTINGS_HTML = (ROOT / "static" / "api-settings.html").read_text(encoding="utf-8")
API_SETTINGS_JS = (ROOT / "static" / "js" / "api-settings.js").read_text(encoding="utf-8")


def test_optional_and_cli_providers_are_not_exposed_in_api_settings():
    assert "HIDDEN_API_SETTINGS_PROVIDER_IDS = new Set(['modelscope', 'runninghub'])" in API_SETTINGS_JS
    assert "HIDDEN_API_SETTINGS_PROTOCOLS = new Set(['runninghub', 'jimeng', 'codex', 'gemini-cli'])" in API_SETTINGS_JS
    assert "HIDDEN_API_SETTINGS_PROVIDER_IDS.has(id)" in API_SETTINGS_JS
    assert "HIDDEN_API_SETTINGS_PROTOCOLS.has(protocol)" in API_SETTINGS_JS


def test_recommendation_and_cli_entries_are_removed():
    assert 'onclick="openRecommendApi()"' not in API_SETTINGS_HTML
    assert 'class="cli-quick-group"' not in API_SETTINGS_HTML
    assert 'id="recommendContent"' not in API_SETTINGS_HTML
    assert 'id="recommendApiOverlay"' not in API_SETTINGS_HTML


def test_removed_provider_protocols_cannot_be_selected_for_new_platforms():
    for protocol in ("runninghub", "jimeng", "codex", "gemini-cli"):
        assert f'<option value="{protocol}">' not in API_SETTINGS_HTML


def test_loading_api_settings_does_not_open_recommendations():
    load_block = API_SETTINGS_JS.split("async function loadProviders(){", 1)[1].split("async function saveProviders(){", 1)[0]
    assert "openRecommendApi()" not in load_block
