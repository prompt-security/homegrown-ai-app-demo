"""
Real PS API integration tests for translated demo scenarios.

Requires env vars:
  PS_BASE_URL  — e.g. https://your-tenant.promptsecurity.ai
  PS_APP_ID    — APP-ID header value

Without these vars, all tests skip (CI shows yellow, not red).

PII tests: only Sensitive Data detector enabled, all others disabled. This
isolates PII detection from unrelated block-capable detectors (Data Privacy
Guidelines, Natural Language Guardrails, Topics Detector, etc.) that would
otherwise fire on financial/HR content in the test prompts and return
action=block instead of action=modify.

Injection tests: only Prompt Injection Engine enabled, all others disabled.

Coverage guard: any scenario in scenarios.json with a meta.prompt_XX key must
appear in _registered_translations() — pytest.fail() if not.
"""

import copy
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from prompt_security import PromptSecurityClient  # noqa: E402

SCENARIOS_PATH = Path(__file__).parent.parent / "app" / "data" / "scenarios.json"
POLICY_PATH = Path(__file__).parent / "fixtures" / "ps_policy_reference.json"

_REPO = "prompt-security/homegrown-ai-app-demo"
_DEFAULT_SCENARIOS_URL = f"https://raw.githubusercontent.com/{_REPO}/main/app/data/scenarios.json"


def _load_scenarios_data() -> list:
    """Load scenarios from URL (preferred) or local file (fallback).

    URL priority:
      1. SCENARIOS_URL env var — explicit override
      2. GITHUB_SHA + GITHUB_REPOSITORY — exact commit in CI
      3. Local file
    """
    url = os.environ.get("SCENARIOS_URL", "")
    if not url:
        sha = os.environ.get("GITHUB_SHA", "")
        repo = os.environ.get("GITHUB_REPOSITORY", _REPO)
        if sha:
            url = f"https://raw.githubusercontent.com/{repo}/{sha}/app/data/scenarios.json"
    if url:
        print(f"\nLoading scenarios from URL: {url}")
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    with open(SCENARIOS_PATH, encoding="utf-8") as f:
        return json.load(f)

# Entity types to scan per country (all entities from reference policy thresholds)
COUNTRY_ENTITIES = {
    "JP": [
        "JAPAN_MY_NUMBER_PERSONAL", "JAPAN_MY_NUMBER_CORPORATE",
        "JAPAN_PASSPORT_NUMBER", "JAPAN_DRIVER_LICENSE_NUMBER",
        "JAPAN_BANK_ACCOUNT_NUMBER", "JAPAN_SOCIAL_INSURANCE_NUMBER_SIN",
        "JAPAN_RESIDENCE_CARD_NUMBER", "JAPAN_RESIDENT_REGISTRATION_NUMBER",
    ],
    "DE": [
        "GERMANY_ID_NUMBER", "GERMANY_PASSPORT_NUMBER",
        "GERMANY_DRIVERS_LICENSE_NUMBER", "GERMANY_TAX_ID_NUMBER", "GERMANY_VAT_NUMBER",
    ],
    "IN": ["INDIA_AADHAAR_NUMBER", "INDIA_PAN_NUMBER"],
    "IL": ["IL_ID_NUMBER", "IL_PASSPORT_RE", "IL_BANK_NUMBER", "IBAN_CODE"],
    "SG": ["SG_NRIC_FIN", "SINGAPORE_PASSPORT_NUMBER", "SINGAPORE_DRIVER_LICENSE_NUMBER"],  # English-only, no native lang picker
    "BR": ["BR_CPF_NUMBER", "BRAZIL_CNPJ_NUMBER"],
    "MY": ["MALAYSIA_ID_NUMBER"],
}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def ps_client():
    base = os.environ.get("PS_BASE_URL", "").rstrip("/")
    app_id = os.environ.get("PS_APP_ID", "")
    if not base or not app_id:
        pytest.skip("PS_BASE_URL / PS_APP_ID not set — skipping PS API tests")
    return PromptSecurityClient(base_url=base, app_id=app_id)


@pytest.fixture(scope="session")
def scenarios():
    data = _load_scenarios_data()
    return {s["key"]: s for s in data}


@pytest.fixture(scope="session")
def base_policy():
    with open(POLICY_PATH, encoding="utf-8") as f:
        return json.load(f)


_BLOCKING_DETECTORS = [
    "Language Detector", "Natural Language Guardrails", "Data Privacy Guidelines",
    "Topics Detector", "Prompt Injection Engine", "Harmful Content Moderator",
    "Code Detector", "URLs Detector", "Secrets", "Regex", "Sentiment",
    "Unicode Detector", "Token Limitation", "Token Rate Limit",
]


def _pii_policy(base_policy: dict, country_code: str) -> dict:
    """Only Sensitive Data enabled — all other detectors off to prevent spurious blocks."""
    policy = copy.deepcopy(base_policy)
    for det in _BLOCKING_DETECTORS:
        if det in policy["prompt"]:
            policy["prompt"][det]["enabled"] = False
    sd = policy["prompt"]["Sensitive Data"]
    sd["entity_types"] = ["EMAIL_ADDRESS"] + COUNTRY_ENTITIES.get(country_code, [])
    return policy


def _injection_policy(base_policy: dict) -> dict:
    """Only Prompt Injection Engine enabled — all other detectors off."""
    policy = copy.deepcopy(base_policy)
    off = [d for d in _BLOCKING_DETECTORS if d != "Prompt Injection Engine"]
    off.append("Sensitive Data")
    for det in off:
        if det in policy["prompt"]:
            policy["prompt"][det]["enabled"] = False
    return policy


def _findings_entities(result) -> set:
    found = set()
    findings = result.raw.get("result", {}).get("prompt", {}).get("findings", {})
    for detections in findings.values():
        if not isinstance(detections, list):
            continue
        for d in detections:
            if isinstance(d, dict) and "entity_type" in d:
                found.add(d["entity_type"])
    return found


async def _assert_pii_modify(ps_client, scenarios, key, lang, policy):
    s = scenarios[key]
    prompt = s["meta"][f"prompt_{lang}"] if lang != "en" else s["prompt"]
    result = await ps_client.protect_prompt(prompt, policy=policy)
    detected = _findings_entities(result)
    assert result.action == "modify", (
        f"{key}/{lang}: expected action=modify, got {result.action!r}. "
        f"Detected: {detected or '(none)'}"
    )
    return detected


# ── Coverage guard ────────────────────────────────────────────────────────────

_INJ_LANGS = ["ja", "hi", "he", "zh", "de", "pt", "ms"]


def _registered_translations():
    pairs = {
        ("pii_JP", "ja"), ("pii_DE", "de"), ("pii_IN", "hi"),
        ("pii_IL", "he"), ("pii_BR", "pt"), ("pii_MY", "ms"),
    }
    for lang in _INJ_LANGS:
        pairs.add(("injection", lang))
        pairs.add(("injSoft", lang))
    return pairs


def test_translation_coverage():
    scenarios = _load_scenarios_data()
    registered = _registered_translations()
    missing = []
    for s in scenarios:
        for k in s.get("meta", {}):
            m = re.match(r"^prompt_([a-z]+)$", k)
            if m and m.group(1) != "en":
                pair = (s["key"], m.group(1))
                if pair not in registered:
                    missing.append(pair)
    if missing:
        pytest.fail(
            "These translations in scenarios.json have no PS API test:\n"
            + "\n".join(f"  {key!r} / {lang!r}" for key, lang in missing)
            + "\nAdd them to test_pii_translations.py and _registered_translations()."
        )


# ── Japan ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_japan_english(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "JP")
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_JP", "en", policy)
    print(f"\npii_JP/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_japan_japanese(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "JP")
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_JP", "ja", policy)
    print(f"\npii_JP/ja detected: {detected}")


# ── Germany ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_germany_english(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "DE")
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_DE", "en", policy)
    print(f"\npii_DE/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_germany_german(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "DE")
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_DE", "de", policy)
    print(f"\npii_DE/de detected: {detected}")


# ── India ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_india_english(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "IN")
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_IN", "en", policy)
    print(f"\npii_IN/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_india_hindi(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "IN")
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_IN", "hi", policy)
    print(f"\npii_IN/hi detected: {detected}")


# ── Israel ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_israel_english(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "IL")
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_IL", "en", policy)
    print(f"\npii_IL/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_israel_hebrew(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "IL")
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_IL", "he", policy)
    print(f"\npii_IL/he detected: {detected}")


# ── Singapore ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_singapore_english(ps_client, scenarios, base_policy):
    # Singapore is English-only — no native language picker (primary working language is English)
    policy = _pii_policy(base_policy, "SG")
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_SG", "en", policy)
    print(f"\npii_SG/en detected: {detected}")


# ── Brazil ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_brazil_english(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "BR")
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_BR", "en", policy)
    print(f"\npii_BR/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_brazil_portuguese(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "BR")
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_BR", "pt", policy)
    print(f"\npii_BR/pt detected: {detected}")


# ── Malaysia ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_malaysia_english(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "MY")
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_MY", "en", policy)
    print(f"\npii_MY/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_malaysia_malay(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "MY")
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_MY", "ms", policy)
    print(f"\npii_MY/ms detected: {detected}")


# ── Prompt Injection ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en"] + _INJ_LANGS)
async def test_injection(ps_client, scenarios, base_policy, lang):
    policy = _injection_policy(base_policy)
    s = scenarios["injection"]
    prompt = s["prompt"] if lang == "en" else s["meta"][f"prompt_{lang}"]
    result = await ps_client.protect_prompt(prompt, policy=policy)
    assert result.action == "block", f"injection/{lang}: expected block, got {result.action!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en"] + _INJ_LANGS)
async def test_injection_soft(ps_client, scenarios, base_policy, lang):
    policy = _injection_policy(base_policy)
    s = scenarios["injSoft"]
    prompt = s["prompt"] if lang == "en" else s["meta"][f"prompt_{lang}"]
    result = await ps_client.protect_prompt(prompt, policy=policy)
    assert result.action == "block", f"injSoft/{lang}: expected block, got {result.action!r}"
