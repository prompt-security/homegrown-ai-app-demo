# Changelog

## [2026-05-03]
### Added
- `CHANGELOG.md` and `CLAUDE.md` for change tracking going forward

### Fixed
- Hybrid diagram: Internal ChatBot → API GW arrow is now green
- Hybrid diagram: API GW → ps-openai-gw shows dual purple + green arrows with stacked HTTPS/443 labels
- Hybrid diagram: Org Proxy → 3rd Party LLMs shows "Private Link" label below HTTPS/443

## [2026-05-02]
### Added
- Ollama local model support (`gemma3:270m`) via host-machine Ollama
- `OLLAMA_BASE_URL`, `OLLAMA_MODEL_IDS`, `OLLAMA_API_KEY` env vars for local/remote Ollama config
- Model selector "Local Models (Ollama)" group
- `CODEOWNERS` + branch protection: `tabac-ps` required reviewer on all PRs to main

### Changed
- Remote Ollama uses HTTPS without a custom port (reverse-proxy pattern)

## [2026-04-30]
### Added
- Compare mode auto-activates when PS is configured and enabled
- gpt-5-nano added to OpenAI model list in LiteLLM

## [2026-04-28]
### Added
- CI: test, security, and code-scanning GitHub Actions workflows

### Fixed
- Gateway mode: `skip_ps` flag now respected in compare view

### Security
- Reject unauthorized `skip_ps` requests
- Harden chat and upload input handling
- Close remaining P1 security gaps

## [2026-04-20]
### Added
- PS Gateway mode: route all LLM traffic through PS proxy
- Architecture diagrams: SaaS, Hybrid, On-Prem SVG diagrams in Intro modal
- API Flow diagram with bidirectional arrows and dual PS scan indicators
- Side-by-side compare mode (PS vs raw LLM)
- PS API inspector: collapsible raw request/response JSON per violation card
- File sanitization demo tab (PDF/DOCX/XLSX/TXT via PS `/api/sanitizeFile`)
- Interactive walkthrough: step-by-step code tour in Intro modal

## [2026-04-10]
### Added
- 17 free OpenRouter models (llama-3.3-70b, deepseek-r1, qwen3, hermes-405b, and more)
- Gemini models routed via OpenRouter
- `POST /admin/refresh-models` endpoint

### Fixed
- Removed 3 broken free OpenRouter models

## [2026-04-01]
### Added
- Public test API (`POST /v1/responses`) with app-issued bearer keys
- Live token estimation and usage badges
- Demo scenarios: PII, topic policy, token DoS, prompt injection

## [2026-03-15] — Initial release
### Added
- Multi-user chat with streaming responses, session history, daily limits
- LiteLLM proxy: OpenAI, Anthropic, Google, OpenRouter
- Prompt Security integration (API mode) with violation detail cards
- Admin dashboard: stats, charts, user/tenant management, activity log
- PostgreSQL persistence + Docker Compose setup
