"""Tests for model categorization and metadata — Sprint 3."""

import os
import pytest


@pytest.fixture(autouse=True)
def _chdir_to_app():
    """main.py mounts StaticFiles(directory='static') — ensure CWD is app/."""
    original = os.getcwd()
    os.chdir(os.path.join(os.path.dirname(__file__), "..", "app"))
    yield
    os.chdir(original)


def test_detect_provider_openai():
    from main import _detect_provider
    assert _detect_provider("gpt-4o") == "openai"
    assert _detect_provider("gpt-4o-mini") == "openai"
    assert _detect_provider("o1-preview") == "openai"
    assert _detect_provider("o3-mini") == "openai"


def test_detect_provider_anthropic():
    from main import _detect_provider
    assert _detect_provider("claude-3-5-sonnet-20241022") == "anthropic"
    assert _detect_provider("claude-3-5-haiku-20241022") == "anthropic"


def test_detect_provider_google():
    from main import _detect_provider
    assert _detect_provider("gemini-2.0-flash") == "google"
    assert _detect_provider("gemini-1.5-pro") == "google"


def test_detect_provider_openrouter_default():
    from main import _detect_provider
    assert _detect_provider("meta-llama/llama-3.1-8b-instruct:free") == "openrouter"
    assert _detect_provider("nvidia/nemotron-nano-9b-v2:free") == "openrouter"
    assert _detect_provider("mistralai/mistral-7b-instruct:free") == "openrouter"


def test_local_openai_model_metadata(monkeypatch):
    import main

    model_id = "huggingface/Qwen3VL-8B-Instruct-F16"
    monkeypatch.setattr(main, "_LOCAL_OPENAI_MODEL_IDS", {model_id})

    assert main._detect_provider(model_id) == "local_openai"
    assert main._model_meta(model_id) == {
        "category": "local",
        "provider": "Local OpenAI",
        "requires_key": None,
    }


def test_model_meta_free():
    from main import _model_meta
    meta = _model_meta("meta-llama/llama-3.1-8b-instruct:free")
    assert meta["category"] == "free"
    assert meta["provider"] == "OpenRouter"
    assert meta["requires_key"] is None


def test_model_meta_paid_openai():
    from main import _model_meta
    meta = _model_meta("gpt-4o")
    assert meta["category"] == "paid"
    assert meta["provider"] == "OpenAI"
    assert meta["requires_key"] == "openai"


def test_model_meta_paid_anthropic():
    from main import _model_meta
    meta = _model_meta("claude-3-5-sonnet-20241022")
    assert meta["category"] == "paid"
    assert meta["provider"] == "Anthropic"
    assert meta["requires_key"] == "anthropic"


def test_model_meta_paid_google():
    from main import _model_meta
    meta = _model_meta("gemini-2.0-flash")
    assert meta["category"] == "paid"
    assert meta["provider"] == "Google"
    assert meta["requires_key"] == "google"


def test_model_meta_paid_openrouter():
    from main import _model_meta
    meta = _model_meta("qwen/qwen-2.5-72b-instruct")
    assert meta["category"] == "paid"
    assert meta["provider"] == "OpenRouter"
    assert meta["requires_key"] == "openrouter"


def test_free_suffix_detection():
    """Any model ending in :free should be categorized as free."""
    from main import _model_meta
    for model_id in [
        "nvidia/nemotron-nano-9b-v2:free",
        "mistralai/mistral-7b-instruct:free",
        "deepseek/deepseek-r1-0528:free",
    ]:
        meta = _model_meta(model_id)
        assert meta["category"] == "free", f"{model_id} should be free"
        assert meta["requires_key"] is None, f"{model_id} should not require key"
