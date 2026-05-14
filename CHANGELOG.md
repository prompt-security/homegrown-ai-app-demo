# Changelog

## [2026-05-14]
### Added
- First-time open-mode startup wizard: 3-step onboarding (name → PS setup → preferences) shown on first visit; collects guest name, optional PS tenant + API key, history retention, and SE notes preference; sets hgapp_hide_tips so the help modal doesn't double-open — @pj.norris
- Activity log scenario rows are now clickable: `scenario_created`/`scenario_updated`/`scenario_deleted` entries with parseable JSON detail open a full scenario preview modal (category, severity, expected action, description, prompt, attacker goal, why caught, entities, talking point) with an ⬇ Export JSON button to download the scenario as a ready-to-import file; copy/import origin shown as chips — @pj.norris
- Scenario audit log entries now store the full scenario JSON: `AuditEvent.detail` widened from `VARCHAR(500)` to `TEXT` (startup migration); `/guest/log-event` limit raised to 8,000 chars; all `logGuestEvent` scenario calls now pass `JSON.stringify(s)` — @pj.norris

### Fixed
- Scenario imports, duplicates, creates, updates, and deletes via the 🎭 Demo Scenarios modal now all fire open-mode audit events; all `logGuestEvent` calls guarded with `OPEN_MODE` — @pj.norris

## [2026-05-13]
### Added
- Open mode config changes now appear in the activity log: `POST /guest/log-event` endpoint accepts a whitelist of event types (`ps_config_changed`, `scenario_created`, `scenario_updated`, `scenario_deleted`) and writes an `AuditEvent` with the guest name + IP as identifier; `logGuestEvent()` helper fires-and-forgets from PS config save and custom scenario create/update/delete — @pj.norris
- Activity log now records demo scenario changes: `scenario_created`, `scenario_updated`, `scenario_deleted` audit events written on each admin CRUD operation with title/category/severity in the detail; admin.html `AUDIT_ICONS`/`AUDIT_LABELS` updated to display them (🎭) alongside existing `api_key_created`/`api_key_deleted` entries that were previously unrendered — @pj.norris
- Compare mode left pane (Prompt Security) has a subtle green background; right pane (raw LLM) has a subtle amber background — same tints applied to the main chat area (`#chat`) when PS is active (green) or off/unconfigured (amber), updating live via `updatePsStatus()` — @pj.norris
- Main send button (`sendBtn`) turns red ⏹ Stop in compare mode during streaming and aborts both streams on click — @pj.norris
- User menu labels updated: "Export Chat (MD)", "Export History (JSON)", "Import History (JSON)"; dropdown widened to 210px — @pj.norris
- Admin user menu icon changed to 🔒 padlock — @pj.norris
- Admin user chat history modal: LLM response rows now show a favicon **Prompt Security** chip (green) or 🤖 **Raw LLM** chip (amber) indicating which path each response took; session separator changed from a highlighted row to a plain `<hr>` — @pj.norris

### Fixed
- Chat history now restores all bubble chips on reload: token count badge (`N tok`), BLOCKED chip (blocked/revoked messages now persisted to localStorage), MODIFIED/ALLOWED chips, and PS insight panel — `done` handler saves `prompt_tokens`/`completion_tokens`/`total_tokens`; `blocked` and `revoke` handlers now write user+assistant entries to localStorage; `loadSession` passes `usage` and `blocked` flag when rebuilding bubbles — @pj.norris

### Added
- Compare mode "⚡ Compare Both" button turns into a red "⏹ Stop" button while streaming; clicking it aborts both PS and raw LLM streams via `AbortController`; bubbles show "⏹ Stopped." on abort; demo panel Compare buttons also use stop/abort — @pj.norris

### Fixed
- Compare mode bubbles now match normal mode footer: token count badge (`N tok`), copy button (visible on hover), `✅ Prompt Security: pass` badge, and API inspector — compare `inner` div now carries `msg-inner` class so hover CSS applies; `cmp-footer` gains flex layout; `done` event handler reads `total_tokens`/`prompt_tokens`/`completion_tokens` from SSE — @pj.norris
- "No Prompt Security" chip on raw LLM compare bubbles is now red to clearly indicate unprotected responses — @pj.norris


### Fixed
- Help modal now shows correctly on page load and when clicking `?`: a missing `</div>` on the `settingsPs` section inside `psBackdrop` left the div unclosed, making every modal on the page (help, demo scenarios, edit, preview, etc.) a hidden child of `psBackdrop` — they only appeared when PS Settings was opened. Added the missing closing tag to restore correct DOM structure — @pj.norris
- Help content inlined as static JS data (removed `fetch('/static/help.md')` entirely): eliminates all async race conditions between help load and other UI interactions — @pj.norris


### Added
- Chat history retention setting in Prompt Settings → PS tab: dropdown (1/3/5/7/10/14/21/28 days, default 7); stored in localStorage; `pruneOldSessions()` runs on save and on every page load — removes sessions (and their messages, PS flags, compare markers) older than the threshold in open mode, calls DELETE API in auth mode — @pj.norris
- Demo Scenarios modal: Import (JSON file) and Export (downloads `demo-scenarios-<date>.json`) buttons for custom scenarios stored in localStorage; import deduplicates by title (case-insensitive), validates required fields, and shows a styled result modal listing imported/duplicate/invalid scenarios — @pj.norris
- Chat history Export (💾) and Import (📂) in user menu: export bundles all sessions and their messages as a dated JSON file (open mode reads localStorage, auth mode fetches from API); import restores sessions into open mode localStorage, deduplicates by title, and shows the same styled result modal — @pj.norris
- Open mode messages now persist full metadata (model, ps_scanned, ps_action, ps_violations) to localStorage so imported sessions restore with PS chips and model labels intact; export falls back to in-memory messages array for the current active session — @pj.norris
- Fixed chat history export/import icons and compare sessions: export now reads `hgapp_cmp_left_`/`hgapp_cmp_right_` for compare sessions and derives `ps_scanned` from message metadata when the localStorage flag is absent; import always writes the PS icon flag (derived from messages if not explicit) and restores compare sessions into the correct left/right keys — @pj.norris

### Changed
- Admin Demo Scenarios preview modal: expanded from prompt-only to full rich layout matching user-side preview — category/severity/expected-action chips, Built In badge, description block, prompt pre, 2-column grid cards (attacker goal, why caught, country), entities pills, SE talking point callout — @pj.norris


### Changed
- Demo Scenarios preview modal: expanded from prompt-only to full scenario view with category/severity/expected-action chips, description block, attacker goal, why-caught, country metadata, entities pills, and SE talking point callout — @pj.norris

### Added
- User menu → "🎭 Demo Scenarios" modal: shows all DB scenarios (read-only, Duplicate + Preview) combined with user-created custom scenarios (Edit + Delete + Duplicate + Preview); custom scenarios stored in `hgapp_user_scenarios` localStorage with full field parity to admin (title, category, severity, expected action, prompt, description, attacker goal, why caught, talking point, entities, country metadata); grouped by category with collapsible accordion — @pj.norris
- Compare mode toggle now creates a "New comparison…" placeholder in history on click; placeholder removed if user exits without sending; exiting compare also creates a fresh "New conversation…" regular chat placeholder — @pj.norris
- Session retention: switching history entries keeps background streams running; DOM nodes detached (not copied) so live bubbles continue updating off-screen and are restored on return — @pj.norris
- Input history navigation: ↑/↓ arrows cycle through previously sent prompts; draft saved on first ↑ press and restored on ↓ past newest entry — @pj.norris
- PS toggle button uses favicon instead of shield emoji; "With PS" compare button renamed to "Prompt Security" with favicon; header robot emoji replaced with favicon — @pj.norris

## [2026-05-12]
### Added
- Session retention (multi-threaded browsing): switching between history entries now restores the full conversation thread without re-fetching; `sessionSnapshots` map caches each session's DOM and message state client-side; active streams are aborted cleanly before switching so the in-progress bubble is preserved in the snapshot — @pj.norris


### Added
- Settings tab Danger Zone: "Reset All Stats" button wipes chat sessions, messages, token records, and audit events via two-step confirmation (first warns what's deleted, second is a final "Reset Everything" gate); user accounts, tenants, settings, and scenarios are preserved — @pj.norris
- Overview dashboard token stat card: user mode shows all-time / this month / today; open mode shows all-time / this month; sourced from `total_tokens` on assistant messages — @pj.norris
- Guest Users tab shows per-guest token totals (all-time and this calendar month) as columns in the table; aggregate stat card shows combined token usage across all guests with monthly breakdown — @pj.norris
- Token usage captured per assistant message: `prompt_tokens`, `completion_tokens`, `total_tokens` columns added to `messages` table (schema migration runs at startup); guest chat stream stores actual usage from LiteLLM (with estimation fallback); guest activity modal shows total token count with prompt/completion breakdown on hover — @pj.norris

### Fixed
- Guest activity rows in Users tab (open mode) now correctly open the chat history modal; previous `onclick` used `JSON.stringify()` which embedded double-quoted strings inside `onclick="..."` HTML attributes, terminating the attribute early and silently preventing any click from firing — @pj.norris

### Fixed
- Admin init: `addEventListener` calls for post-script modals wrapped in `DOMContentLoaded` — previously threw `TypeError` on null and silently prevented `init()` from running, causing blank overview and missing mode chip on load — @pj.norris
### Added
- Overview dashboard adapts to user mode: User Management mode shows registered-user stats/charts/top-users; Open Mode shows unique guests, guest chats per day, and top-guests table sourced from `/admin/guest-stats` — @pj.norris

## [2026-05-11]
### Fixed
- Activity Log: "🗑 Clear Logs" button deletes all messages, chat sessions, and audit events after dark confirm modal; `DELETE /admin/activity` endpoint — @pj.norris
- Guest PS blocks fully persisted: all four early-exit paths (gateway block, API prompt block, API response block) now save an assistant message with `[BLOCKED by Prompt Security]` content and `ps_action="block"` before returning — @pj.norris
- Guest chat history persisted: `ChatSession` and `Message` rows now written for every guest exchange (user message on request, assistant reply after streaming); `user_id` made nullable on both tables, `guest_id` string column added; schema migration runs at startup via `ALTER TABLE IF NOT EXISTS` — @pj.norris
- Guest activity modal redesigned: shows session-grouped chat bubbles (👤 User / 🤖 Assistant) with PS action chips, model label, and full message content; filters by date range, model, and PS action — @pj.norris
- Guest activity modal: click any guest row to see full chat history with filters for date range, model, and PS action; powered by new `GET /admin/guest-activity?identifier=` endpoint — @pj.norris
- Users tab: when User Management is disabled (Open Mode), tab switches to a Guest Activity audit view — unique guests, total/today chat counts, chats-per-day chart, and top-guest table sourced from `GET /admin/guest-stats` — @pj.norris
- Users tab: Export (downloads `users-<date>.json`, strips id/ps credentials) and Import (requires `password` field per row, skips duplicates by email and rows missing password, dark-modal reporting) buttons — @pj.norris
- PS Tenants tab: inline Duplicate and Delete buttons on each row (Delete uses dark confirm modal) — @pj.norris
- PS Tenants tab: Export (downloads `ps-tenants-<date>.json`) and Import (with duplicate name detection and dark-modal warnings) buttons added alongside New Tenant — @pj.norris
- Overview tab charts blank on hard reload: `await loadOverview()` in `init()` to surface errors, plus `requestAnimationFrame` yield before Chart.js instantiation so canvas dimensions are stable — @pj.norris


### Added
- `DemoScenario` ORM model (`demo_scenarios` table) with full scenario metadata: key, title, category, severity, prompt, expected_action, description, attacker_goal, why_caught, talking_point, entities (JSON), meta (JSON), sort_order, is_active — @pj.norris
- Seed function `_seed_demo_scenarios` populates 15 scenarios on first boot (10 PII country variants + Prompt Injection, Soft Injection, Prompt Leak, Topic Policy, Token DoS) — @pj.norris
- `GET /demo-scenarios` public endpoint returns active scenarios ordered by sort_order — @pj.norris
- `GET /admin/demo-scenarios`, `POST /admin/demo-scenarios`, `PATCH /admin/demo-scenarios/{id}`, `DELETE /admin/demo-scenarios/{id}` admin CRUD endpoints — @pj.norris
- Admin → Demo Scenarios tab: table with severity/category/title/expected/active toggle, Edit and Preview buttons — @pj.norris
- Scenario edit modal in admin with all fields including country metadata section for PII variants — @pj.norris
- Prompt preview modal in admin dashboard — @pj.norris
- Chat UI demo panel now loads scenarios dynamically from `/demo-scenarios` instead of hardcoded JS; tabs and content are built at runtime — @pj.norris
### Added
- Guest name prompt on first open-mode visit; stored in localStorage as `hgapp_guest_name` — @pj.norris
- "Change name" option in user menu dropdown (open mode only) — @pj.norris
- Guest name passed to `/guest/chat/stream` and used as activity log identifier; IP appended in parentheses — @pj.norris
- Fix Model admin setting: locks all users to a chosen model and hides the model selector in the chat UI — @pj.norris
- `guest_chat` event type in admin activity log with 👤 icon — @pj.norris
- `AuditEvent.user_id` made nullable to support guest (unauthenticated) activity log entries — @pj.norris

## [2026-05-09]
### Added
- User Management toggle in Admin → Settings: persisted in `app_settings` DB table — @pj.norris
- Open Mode (User Mgmt off): no authentication required; each browser gets isolated localStorage sessions, messages, and PS config — @pj.norris
- `GET /app/settings` public endpoint returns current `user_mgmt_enabled` flag — @pj.norris
- `PATCH /admin/app-settings` admin endpoint to upsert app settings — @pj.norris
- `GET /guest/models`, `GET /guest/ps-tenants`, `POST /guest/chat/stream` unauthenticated endpoints for open mode — @pj.norris
- Admin auth modal in open mode: clicking "⚙️ Admin" prompts for credentials and redirects to /admin on success — @pj.norris

## [2026-05-03]
### Added
- Demo storytelling: severity badges (HIGH/MEDIUM), attacker-goal, and SE talking-point on every scenario card
- Practice Mode toggle (🎭 Practice / 🎤 Demo) hides SE coaching notes for live demos; state persisted in localStorage
- 2 new attack scenarios: Soft Injection, Prompt Leak
- Status chips (BLOCKED / MODIFIED / ALLOWED) on bot messages in chat and compare mode
- "Why PS caught this" panel in PS insight with scenario-specific explanation and SE coaching note
- Pulse animations on blocked (red) and modified (amber) message bubbles
- `activeScenario` tracking: set when loading a demo prompt, cleared on manual input edit

### Changed
- All scenario cards now show severity pill + category row and attacker-goal text

### Fixed
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
