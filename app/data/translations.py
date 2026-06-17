"""
Translation registry for demo scenarios.

Every entry here must have a corresponding PS API test in tests/test_pii_translations.py.
See CLAUDE.md §Demo Scenario Translations for the full checklist.
"""

LANG_NAMES: dict[str, str] = {
    "en": "English",
    "hi": "हिन्दी",
    "he": "עברית",
    "zh": "中文",
    "de": "Deutsch",
    "ja": "日本語",
    "pt": "Português",
    "ms": "Bahasa Malaysia",
}

# Keys match scenario `key` field in scenarios.json.
# Values are dicts of prompt_XX (excluding prompt_en — that lives in the scenario itself).
# Keep in sync with _SCENARIO_TRANSLATIONS in app/static/index.html.
SCENARIO_TRANSLATIONS: dict[str, dict[str, str]] = {
    # India — Hindi
    "pii_IN": {"lang": "hi"},
    # Israel — Hebrew
    "pii_IL": {"lang": "he"},
    # Singapore — English-only (primary working language is English; no native picker shown)
    # Germany — German
    "pii_DE": {"lang": "de"},
    # Japan — Japanese
    "pii_JP": {"lang": "ja"},
    # Brazil — Portuguese
    "pii_BR": {"lang": "pt"},
    # Malaysia — Malay
    "pii_MY": {"lang": "ms"},
    # Prompt injection — all non-English languages
    "injection": {"langs": ["ja", "hi", "he", "zh", "de", "pt", "ms"]},
    "injSoft": {"langs": ["ja", "hi", "he", "zh", "de", "pt", "ms"]},
}
