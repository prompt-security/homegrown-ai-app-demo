# Changelog

## [2026-05-28]
### Removed
- `PUBLIC_API_ENABLED` feature and `POST /v1/responses` endpoint removed entirely: env vars (`PUBLIC_API_ENABLED`, `PUBLIC_API_MAX_PROMPT_TOKENS`, `PUBLIC_API_MAX_OUTPUT_TOKENS`, `PUBLIC_API_ALLOW_SYSTEM_PROMPT`), the endpoint handler, the `_ensure_public_api_enabled()` helper, and the four `PublicResponse*` Pydantic schemas (`PublicResponseRequest`, `PublicResponseOutput`, `PublicResponseUsage`, `PublicResponseOut`) have all been deleted; README documentation for the public test API has been removed — @pj.norris

## [2026-05-27]
### Added
- Model discovery: when an OpenAI or Anthropic provider key is saved in Settings → LLM Provider Keys, the app automatically queries that provider's models API, injects the full list of chat-capable models into the chat dropdown, and displays the discovered model names as inline chips below the provider row in the LLM Keys settings table (first 8 visible, remainder shown as "+N more"); the table auto-refreshes ~3.5 s after save to show the chips once discovery completes; OpenAI discovery uses `GET /v1/models` filtered to chat-capable prefixes; Anthropic uses their models API with a static fallback list; discovered models are stored with a provider-prefix ID (e.g. `openai/gpt-4.1`) and bypass LiteLLM entirely — routed direct to the provider using the shared or per-user key (`_user_llm_client` priority: per-user key → shared key direct bypass → LiteLLM for unprefixed config models); no LiteLLM config changes or restarts needed; discovered lists persist in `app_settings` and survive restarts — @pj.norris
- Unavailable models (those requiring a provider key that isn't set) are now hidden entirely from the chat model dropdown instead of being shown greyed-out — @pj.norris
- Ollama active model selection is now instant: clicking a model chip in Settings → Ollama saves and activates it immediately with no LiteLLM restart; the orange "pending" state and restart countdown are removed; the nav dot no longer shows orange for an unsaved model selection — @pj.norris
- Stream cancellation on client disconnect: Ollama inference is now called directly from FastAPI (bypassing the LiteLLM proxy), because LiteLLM does not reliably propagate connection closes to Ollama — the inference kept running at 1000%+ CPU even after the client disconnected; the direct httpx connection plus a pump-task pattern (background task feeds a queue; main generator drains with a 0.4 s timeout and calls `is_disconnected()` on each timeout) ensures that cancelling the pump task raises `CancelledError` inside httpx recv(), which closes the TCP socket directly to Ollama's Go HTTP server, stopping inference within under a second; non-Ollama models continue to route through LiteLLM unchanged; gateway streaming paths (Anthropic, PS OpenAI) retain simpler per-line disconnect checks — @pj.norris
- Ollama wildcard routing: replaced individual per-model entries in `litellm/config.yaml` with a single `ollama/*` wildcard; any model installed via `ollama pull` now appears in the chat dropdown automatically — no config edit or LiteLLM restart required; `refresh_model_cache()` queries the Ollama API directly on every refresh and injects discovered models (using the `ollama/<name>` prefix that matches the wildcard); display names in the dropdown strip the prefix so users still see plain model names — @pj.norris
- Mandatory disclaimer step added to both the open-mode first-run wizard and the user-mode first-login wizard; after initial setup is complete, only the disclaimer is shown on each subsequent login (full wizard steps are skipped); disclaimer acceptance is tracked per session via sessionStorage and cleared on logout so it always reappears on the next login; the disclaimer covers work-only use and chat data retention; users must tick a checkbox before the Continue button enables, and the step indicator / progress bar are hidden until accepted — @pj.norris
- Ollama and LLM provider keys can now be used simultaneously; LLM Keys nav is always visible regardless of Ollama state; the LLM Keys nav dot and field highlight now clear when either Ollama is running with a selected model OR at least one provider key is set — @pj.norris
- Unified model selector in chat UI: when both Ollama models and LLM provider models are available, a single dropdown shows all options grouped as "Local Models (Ollama)", "Free Models", and "Paid Models"; works in both user mode and open mode; selected model is persisted per-user in localStorage; the static Ollama badge is retired in favour of the full picker — @pj.norris

### Fixed
- File scan now uses the correct PS API: a single synchronous `POST /api/sanitizeFile` with `APP-ID` header and `file` form field, replacing the previous submit+poll pattern that was incorrect; `PromptSecurityClient.sanitize_file()` replaces the old `sanitize_file_submit` + `sanitize_file_poll` methods in `prompt_security.py`; both the authenticated and guest endpoints updated to use the new method — @pj.norris
- File scan (Demo tab) in open mode was calling the authenticated `/upload/sanitize` endpoint, which returned 401 and redirected to the login page; added a `/guest/upload/sanitize` endpoint that accepts PS config as form fields (`ps_base_url`, `ps_app_id`) instead of reading from a user account; the frontend now routes to the guest endpoint in open mode (reading PS config from `hgapp_open_ps_config` localStorage) and shows a clear error if PS is not yet configured — @pj.norris
- Fix Model (lock model) setting was hidden in Settings → General whenever Ollama was enabled; since Ollama and LLM provider models now coexist in the same unified picker, the restriction is removed — Fix Model is always visible and Ollama no longer force-disables it when toggled on — @pj.norris
- Open mode (guest) chat was routing discovered provider-prefixed models (e.g. `openai/gpt-5.2`) through LiteLLM, which has no config entry for them, causing a 400 error; same bypass logic applied to guest requests via a new `_guest_llm_client()` helper: if the model ID has a provider prefix and a shared key exists, the request goes direct to the provider and `extra_body` (LiteLLM-specific) is omitted — @pj.norris
- Admin entry from the chat UI was storing the admin JWT in `hgapp_token` (the chat session key), wiping the logged-in user's token; this caused a second password prompt on the admin page (which found no `hgapp_admin_token` and re-prompted), and meant exit from admin either landed as the wrong user or logged the real user out; fixed by writing the admin token to `hgapp_admin_token` in the chat UI's admin-auth modal (line 3217 in `index.html`), so the chat user's `hgapp_token` is never touched; admin page picks up the pre-supplied token silently and `exitAdmin()` finds the original `hgapp_token` intact and returns to `/`; `showAdminOverlay()` guard switched from a DOM-based check to a synchronous `_overlayShowing` boolean for robustness — @pj.norris
- Returning to user mode after exiting admin triggered the full setup wizard instead of just the disclaimer; root cause: the account used to access admin had never gone through the user wizard, so its wizard-complete marker was never set in localStorage; fixed by auto-marking wizard complete for any user who already has existing chat sessions on the server (established users are not new and don't need onboarding) — @pj.norris
- Removed all `role === 'admin'` checks from the chat UI (`index.html`): there is no admin user concept in the user-facing app — the admin section is a separate password-protected area; removed the hidden "⚙️ Admin → Prompt Security Settings" link that was only shown to admin-role users, removed the role check from `canUseCompareMode()`, and removed it from the wizard-done auto-mark logic — @pj.norris
- Compare mode right pane (raw LLM, no PS) was incorrectly showing PS results for regular users; root cause was `canUseCompareMode()` returning false for non-admin users, which forced `effectiveSkipPs = false` in `streamIntoBubble` regardless of the `skipPs` argument; fixed by extending `canUseCompareMode()` to return true for any user who has PS configured and enabled, and removing the backend admin-only gate on `skip_ps` (any authenticated user may now call the right compare pane without PS processing) — @pj.norris
- Exit Admin was permanently blocked in User Mode when no PS Regions were configured; PS Regions is optional (Prompt Security may not be in use) so it now shows a nav dot but no longer counts toward the exit gate — @pj.norris
- Exit Admin link could never be clicked when security settings (encryption key, LiteLLM master key, etc.) were not yet saved via the Settings panel, leaving admins permanently stranded; the hard CSS block (`pointer-events: none`) is removed — exit is now always possible, and any incomplete security items surface as a soft confirmation dialog ("Exit Anyway" or "Go to Security") rather than a hard lock — @pj.norris
- Exit Admin soft warning was triggering on every exit because Encryption key and LiteLLM master key were always unset (both use safe ephemeral fallbacks); these are now advisory-only nav dots on the Security pane and no longer trigger the exit dialog — only Admin password and JWT secret (the items that leave the app insecure without them) prompt the warning — @pj.norris
- Admins were trapped in a login→admin redirect loop on exit: `_setup_complete()` required `encryption_key_overridden()` and at least one LLM provider key, so installs using only Ollama (or without the encryption override file) always returned `needs_setup=true`, causing `/login` to immediately bounce back to `/admin`; fixed by removing the encryption key requirement (ephemeral fallback is safe) and recognising Ollama-enabled-with-model as a valid LLM source — @pj.norris
- Saving the JWT secret before setting an admin password caused an immediate 401 / login redirect, locking the admin out (no password to log back in with); `POST /admin/jwt-secret` now hot-swaps the key then issues a fresh token signed with it and returns `{"ok": true, "token": "…"}` — the frontend immediately stores the new token so the session continues seamlessly — @pj.norris

## [2026-05-26]
### Fixed
- Ollama Start button no longer fails with "network not found" on fresh installs or after `docker compose down -v`; when an existing container has a stale network reference it is automatically removed and recreated — @pj.norris
- Ollama container created via the admin Start button was not joined to the Compose project network (`homegrown-ai-app-demo_default`), causing `http://ollama:11434` to be unreachable from the app container; added `network=network_name` to the Docker SDK `containers.run()` call — @pj.norris

### Changed
- Ollama Settings: "Detect Models" and "Pull a Model" controls (input, Pull button, Browse Models button) are now disabled — and a warning banner is shown — when the Ollama toggle has been turned on but not yet saved; once saved (which auto-starts the service), the controls unlock; `_savedOllamaEnabled` tracks the persisted DB state separately from the in-flight toggle — @pj.norris
- Ollama Settings: Active Model row is now hidden until the service is confirmed running; model detection runs automatically whenever service status resolves to "running" (covers both the manual Start button and the ↺ refresh button), so the model picker populates without needing a manual Detect click — @pj.norris

### Removed
- Admin Setup Wizard removed entirely — all configuration now lives directly in the Settings panel; the 🛠 Setup Wizard topbar button, wizard overlay HTML, all `.wz-*` CSS, and ~925 lines of wizard JS (state, step rendering, per-step save logic, navigation functions) are deleted; on first-run (`needs_setup=true`) the admin lands directly on the Settings panel instead of being routed through the wizard — @pj.norris

### Changed
- Config Status panel now mirrors the Settings sidebar structure exactly — sections are ordered and named General, Application, Security, Email (Open Mode only), PS Regions, Ollama, LLM Keys; each section header is a clickable link that switches directly to the corresponding settings pane; PS Regions count is now fetched and displayed; General shows access mode and fix-model state; nav dots updated to include `sp-general` (fix model fail) and `sp-psregions` (no regions configured) — @pj.norris
- Admin wizard "Admin" links now call `openAdminAuthModal()` instead of navigating directly to `/admin`, ensuring the password prompt always appears; `adminAuthBackdrop` z-index raised to 400 so it renders above wizard modals (z-index 300) — @pj.norris

## [2026-05-25]
### Added
- Ollama Model Browser modal in Admin → Settings: a "Browse Models" button next to the Pull input opens a searchable, filterable grid of 120+ models from ollama.com/library with capability badges (Tools, Vision, Thinking, Embedding), size chips, pull counts, and a one-click Pull button per card that streams progress inline — @pj.norris
- Model Browser size chips are now clickable: selecting a size tag (e.g. `7b`, `1.5b`) pre-fills the pull command with that variant, updates the Pull button label to `Pull :size`, and displays a colour-coded size warning (blue info → orange caution → red danger) based on estimated download size; clicking the chip again deselects it — @pj.norris
- Model Browser chips now show two visual states: green border + dot (●) marks the `latest` (default) tag for each model; green shaded background + ✓ icon marks already-downloaded variants (matched against the detected Ollama model list, including `:latest` alias resolution); chips update live after a pull completes — @pj.norris
- Hovering a downloaded chip reveals a red ✕ delete button; clicking it calls `DELETE /admin/ollama/model` to remove the model from Ollama, then refreshes the chip states immediately; success/error feedback appears in the pull status bar — @pj.norris
- A ✕ Cancel button appears in the pull status bar during an active download and aborts the stream via `AbortController`; cancellation shows a brief "⊘ Download cancelled" message then dismisses the bar — @pj.norris

### Changed
- Moved Prompt Security Regions out of the main sidebar navigation and into the Settings page as a new "PS Regions" pane (between LLM Keys and Ollama); the standalone nav item is removed, and the pane loads region data automatically when activated via `switchSettingsPane` — @pj.norris
- Settings pane fields that are not yet configured now get a red border and faint red background highlight via `.field-unset` CSS class, applied automatically by `applyFieldHighlights()` whenever `loadConfigChecklist()` runs; covers Admin Password, JWT Secret, Encryption Key, LiteLLM Key (Security pane), SMTP Host and Allowed Domains (Email pane, Open Mode only), the provider key table (LLM Keys pane when no key is set), the Fix Model selector (Application pane when Fix Model is enabled but blank), and the Active Model row (Ollama pane when no model is selected) — @pj.norris
- Ollama model picker in Admin → Settings now shows three distinct states: green (✓) = currently active model saved in DB, orange (◎) = selected but not yet saved/pending restart, neutral = not selected — @pj.norris
- Added an inline orange warning banner below the model picker when a selection hasn't been saved yet, reminding admins to click Save and that the change requires a LiteLLM restart — @pj.norris
- Chat UI model badge (index.html) now always displays a single admin-selected model badge (no per-user dropdown in Ollama mode); model switching is admin-only via Settings — @pj.norris

## [2026-05-24]
### Added
- Ollama service management UI in Admin → Settings: admins can start/stop the Ollama Docker container, view live service status (Running / Stopped / Docker N/A), detect available models with a clickable model picker, and pull new models with a streaming progress bar — @pj.norris
- New backend endpoints: `GET /admin/ollama/service`, `POST /admin/ollama/service/start`, `POST /admin/ollama/service/stop`, `POST /admin/ollama/pull` (streaming SSE) — @pj.norris
- Docker socket (`/var/run/docker.sock`) mounted into the `app` container and `docker>=7.0.0` added to `requirements.txt` to enable container management from the API — @pj.norris

### Changed
- Postgres password hardcoded to `hgapp_dev` in `docker-compose.yml` for both the `db` and `litellm` services; removed `${POSTGRES_PASSWORD:-hgapp_dev}` variable substitution so both services always agree on the password without a `.env` file — @pj.norris
- Removed DB Password panel from Admin → Settings and the corresponding "DB Password" step from the Setup Wizard; the password is fixed in `docker-compose.yml` so runtime changes were causing LiteLLM authentication failures (Prisma P1000) — @pj.norris
- Removed `POST /admin/db-password` API endpoint and the `db_password_set` field from `GET /app/settings` response — @pj.norris

## [2026-05-20]
### Added
- Ollama mode: when Ollama is enabled, Fix Model is automatically disabled and hidden in both App Settings and the setup wizard (Ollama controls model selection) — @pj.norris
- Ollama mode: when Ollama is enabled in App Settings, the model selector is hidden and replaced with a toolbar badge showing the active Ollama model; model defaults to the first available local model — @pj.norris
- Ollama service added to `docker-compose.yml` as an opt-in profile (`--profile ollama`) with a persistent `ollama_data` volume; does not start with the main stack — @pj.norris
- Ollama: "Test Connection" button in Admin → App Settings next to the Base URL field; calls new `POST /admin/ollama/test` backend endpoint which probes the Ollama instance's `/api/tags` API, reports reachable models, and auto-populates the Model IDs field on success — @pj.norris

### Changed
- Compare mode now automatically exits to single view when the user turns off Prompt Security via the toolbar toggle, for both open mode and authenticated mode — @pj.norris

### Fixed
- Ollama mode: compare and chat bubbles showed wrong model (e.g. `gpt-5-nano`) because all send paths read `modelSelect.value` directly — if a cloud model was saved in localStorage it would win over the Ollama override; replaced all four model-read sites with `getActiveModel()` which returns the Ollama badge text when `OLLAMA_ENABLED`, bypassing the selector entirely — @pj.norris
- Compare mode: PS insights (ALLOWED chip, violation cards, "No Prompt Security" badge) were rendered using the raw `skipPs` argument instead of the gated `effectiveSkipPs` variable — causing both panels to show identical PS output even when the right panel had PS skipped. Fixed `streamIntoBubble` at the three `evt.type === 'done'`, `'blocked'`, and `'revoke'` render branches to use `effectiveSkipPs` — @pj.norris

### Added
- Dynamic build version: `VERSION` file at repo root, `GET /version` endpoint, version displayed near the logo in the chat UI; GitHub Actions workflow (`version-bump.yml`) writes `build.<run_number>` to `VERSION` and commits it back on every merge to `main` — @pj.norris


### Fixed
- Guest activity not logged after DB password change via Setup Wizard: `_persist_guest` used a stale `AsyncSessionLocal` reference (copied at import time) instead of the module-level reference updated by `rebuild_engine`; fixed to use `_db_module.AsyncSessionLocal()` so it always reflects the live engine — @pj.norris

### Fixed
- 17 failing CI tests fixed: restored `skip_ps` 403 for non-admin users; added `_LOCAL_OPENAI_MODEL_IDS` set and `local_openai` provider detection; free models now return `requires_key: "openrouter"` instead of `None`; `_validate_security_bootstrap_config` now raises `RuntimeError` in production for insecure defaults; added `_validate_external_https_url`, `_normalize_legacy_public_http_url`, and `_migrate_legacy_ps_tenant_urls` security helpers; `_build_ps_api_client` now returns `None` for invalid/private tenant URLs; chart.js pinned to `@4.4.9` with SHA-384 SRI in `admin.html`; model selector now uses `if (m.requires_key && !m.key_set)` pattern; added `function canUseCompareMode()` gating compare mode to admin role with `effectiveSkipPs` in `streamIntoBubble` — @pj.norris

### Added
- API / Gateway mode selector added to Step 2 of both the open-mode wizard and the user-mode first-login wizard; selecting Gateway shows the tenant's gateway URL (or a warning if none is configured); selected mode is saved when the wizard finishes — open mode writes to localStorage, user mode sends `ps_mode` in the `PATCH /users/me/ps-config` call — @pj.norris


### Changed
- User-mode first-login wizard now enforces Prompt Security setup: Skip button removed from Step 2, both region and API key are required before Next is enabled, Next is disabled entirely when no regions are configured (user must contact admin), and error messages clarify that PS is mandatory — @pj.norris


### Added
- "Require password reset on next login" checkbox on the New User / Edit User modal; checked by default for new users (preserving previous behaviour), reflects current value when editing; `must_change_password` added to `UserCreate` and `UserUpdate` schemas and handled in both `POST /admin/users` and `PATCH /admin/users/{id}` endpoints — @pj.norris


### Fixed
- In user mode with PS disabled and compare mode not active, prompts returned no response: removed artificial `max_tokens=150` cap that was applied when `ps_client=None` (it could cause empty streams on some models); added `try/except` around the assistant message DB log so a DB error never silently swallows the `done` SSE event; `updatePsStatus()` now calls `exitCompare()` when PS is disabled so compare mode is properly exited and the regular chat is shown — @pj.norris

## [2026-05-19]
### Changed
- Demo scenario seed data moved from hardcoded Python list in `main.py` to `app/data/scenarios.json`; on startup the seed function reads from that file, so the built-in scenarios can be updated by editing or overwriting the JSON without touching Python code — @pj.norris

### Added
- "💾 Save to Master" button on the admin Demo Scenarios page writes the current database scenarios back to `app/data/scenarios.json` via `POST /admin/demo-scenarios/save-master`; any new database will then be seeded from the updated file; action is confirmed before saving and logged to the audit trail — @pj.norris


### Added
- Recent Messages section on the user detail overview now renders with the same ga-bubble/ga-chip format as the chat history modal — PS scanned/action chips, model chip, token chip, full message content; stats endpoint extended to return full content, ps_scanned, and token fields — @pj.norris
- Clicking a user row on the admin Users page in user management mode now opens the same chat history modal used for open-mode guests, showing all sessions and messages with date/PS filters; added `GET /admin/users/{user_id}/chat-history` backend endpoint returning the identical schema as `/admin/guest-activity` — @pj.norris
- Demo scenario changes (create, update, delete, duplicate) in user management mode are now recorded in the activity monitor; added `POST /users/log-event` endpoint for authenticated users and a unified `logActivity()` frontend helper that routes to the guest or auth endpoint depending on mode — @pj.norris


### Fixed
- `se` role users in user management mode received a 403 Forbidden error when sending any chat message; compare mode sends `skip_ps: true` for the right-side panel regardless of role, which the backend was rejecting — removed the restriction so all authenticated users can use `skip_ps` — @pj.norris

### Changed
- User management first-login wizard expanded from 2 to 3 steps to match the open-mode wizard content: Step 2 (Connect Prompt Security) now includes the full setup callout with numbered instructions and the HGA creation video; Step 3 (Your Preferences) added with history retention selector, SE Coaching Notes toggle, and Explanation Notes toggle; Skip on Step 2 now advances to preferences rather than finishing — @pj.norris


### Added
- New users created via the admin panel now have `must_change_password=True` set automatically; on first login they are shown the existing password-change panel on the login page with live complexity validation — @pj.norris

### Changed
- Admin button in the user menu is now always visible to all users in all modes; clicking it opens the password-protected admin auth modal regardless of the user's role or access mode — @pj.norris

### Added
- Setup wizard now includes a Prompt Security Regions step (both modes): add regions with name, base URL (validated), and optional gateway URL; regions are listed with a remove button; step requires at least one region to advance; pre-marked complete if regions already exist; Import button accepts a JSON backup and skips duplicates — @pj.norris
- In user management mode, exiting the admin panel when no users have been created shows a warning dialog offering to go to the Users tab to add one before leaving — @pj.norris
- New User modal now shows a live complexity panel and confirm password field with match indicator; Edit User modal keeps the "leave blank to keep" behaviour with no confirm field — @pj.norris
- Removed PS tenant assignment from user creation/edit modal and users table; admins no longer manually assign users to Prompt Security regions — @pj.norris
- First-login setup wizard for user management mode: shown once per user (localStorage flag) after initial login; Step 1 is a welcome screen, Step 2 offers Prompt Security API key entry (pre-filled with the admin-assigned region, skippable); no name, email, or verification fields — @pj.norris

### Fixed
- Navigating to admin after authenticating via the chat-page Admin modal prompted for the password a second time; admin page now reuses the token from localStorage and only shows the overlay if the token is absent or expired — @pj.norris

### Fixed
- Wizard "Admin Password" step: Save & Continue was silently failing because the save handler still referenced `wzAdminEmail` (removed in a prior fix), causing the email validation to block every submission; removed email field references and the unnecessary re-login attempt from the handler — @pj.norris


### Changed
- Setup Wizard now uses an explicit `wizard_completed` flag (stored in `app_settings`) instead of inferring completion from individual required items; the wizard opens on every admin page load until the user clicks "Go to Dashboard" on the final step, at which point the flag is saved and the wizard stops auto-opening — @pj.norris

### Added
- `GET /setup/status` — unauthenticated endpoint that returns `{ needs_setup: bool }` based on whether the admin password has been set via the wizard (checks for `admin_password_hash` in `app_settings`) — @pj.norris
- `POST /setup/bootstrap-token` — unauthenticated endpoint that issues a 2-hour admin JWT so the Setup Wizard can make authenticated API calls on first run; returns 403 once initial setup is complete, preventing reuse — @pj.norris
- First-run detection on admin page load: checks `/setup/status` before showing the login overlay; if setup is needed, automatically obtains a bootstrap token and opens the Setup Wizard without requiring a password — @pj.norris

### Changed
- `auth.py` no longer raises `RuntimeError` when `SECRET_KEY` is absent from the environment in production — the app starts with an ephemeral JWT key that the lifespan immediately replaces with the DB-stored secret; the `.env` entry is optional — @pj.norris
- `_validate_security_bootstrap_config()` in `main.py` downgrades from hard failures to log warnings for missing `SECRET_KEY` / `ADMIN_PASSWORD` env vars; all these secrets are now managed via the Setup Wizard and persisted in the database, making `.env` entirely optional (only `DATABASE_URL` is still needed at container start) — @pj.norris
- Setup Wizard Welcome, Encryption Key, JWT Secret, and Admin Password steps updated to explicitly explain which `.env` variable each step replaces — @pj.norris
- Removed all references to env var names (`SECRET_KEY`, `ENCRYPTION_KEY`, `ADMIN_PASSWORD`, `.env`) from the admin UI; wizard and confirm dialogs now describe each secret in plain terms without referencing the environment — @pj.norris
- Access Mode wizard step now shows the Next button like all other steps (previously hid it, forcing card-click-only navigation) — @pj.norris

### Fixed
- Setup Wizard Email step: username and from-address inputs were not pre-populated because the wizard used wrong field names (`smtp_username`, `smtp_from`) that don't match the API response keys (`email_username`, `from_email`); also fixed the save call which was POSTing to a non-existent `/admin/email-settings` endpoint instead of `/admin/app-settings` with correct field names; password placeholder now shows "(saved — leave blank to keep)" when a password is already stored — @pj.norris
- Setup Wizard App Settings step: was calling the generic `/admin/app-settings` PATCH (which stores raw strings and does not update in-memory globals) instead of the typed `/admin/application-settings` endpoint; `daily_limit: 0` now sent as integer 0 (unlimited) rather than `null`, which previously caused a `"none"` string in the DB and a ValueError on read-back — @pj.norris
- Setup Wizard: App Settings and DB Password steps now pre-marked as completed on wizard open (App Settings always has sensible defaults; DB Password when `db_password_set` is true) so they show "Next →" instead of "Save & Continue →" — @pj.norris

### Added
- Setup Wizard: full-screen multi-step overlay (`id="wizardOverlay"`) accessible via a new "🛠 Setup Wizard" button in the admin topbar; guides admins through 12 steps (Welcome → Encryption Key → JWT Secret → Admin Password → Access Mode → Email Server → Allowed Domains → LLM Provider Keys → LiteLLM Key → App Settings → DB Password → Complete); left sidebar shows numbered step list with green checkmarks for completed steps and greyed-out entries for mode-skipped steps; wizard auto-opens on first page load when fewer than 4 of 7 config items are passing (once per session via sessionStorage); all saves use the existing `api()` function and established endpoints — @pj.norris

### Changed
- Email Settings and Allowed Email Domains panels consolidated inside the Access Mode section; both are hidden in User Management mode and shown in Open Mode — @pj.norris; it is hidden when User Management mode is active and shown when Open Mode is selected, with an info banner explaining that magic link authentication requires an email server — @pj.norris
- Config Status checklist now only includes the Email group (and counts it against the total) when Open Mode is active; switching to User Management mode removes the Email entry and recalculates the badge — @pj.norris

## [2026-05-18]
### Fixed
- JWT Secret, Fernet Encryption Key, and LiteLLM Master Key fields in Admin → Settings → Security each have a red Clear button; clicking it shows a confirmation modal, then calls a new `DELETE` endpoint (`/admin/jwt-secret`, `/admin/encryption-key`, `/admin/litellm-key`) which removes the stored value from the DB (or override file), reverts the in-memory state to the env-var default, hides the "Saved" chip, and refreshes the Config Status checklist — @pj.norris
- Fix Model toggle now requires a model to be selected before the setting is persisted as enabled; Config Status checklist gains a "Fix Model" entry under Application whenever the toggle is on — green with the model ID if a model is selected, red warning if none is selected; the entry (and its contribution to the pass/fail count) is hidden when Fix Model is off — @pj.norris
- Fix Model toggle now requires a model to be selected before the setting is persisted as enabled; toggling on shows the picker and an inline error "A model must be selected to enable Fix Model" — the backend is not updated to `enabled=true` until a valid model is chosen from the dropdown — @pj.norris
- "Exit Admin" nav link is disabled (greyed out, unclickable) when any Config Status checklist item is incomplete; a tooltip explains how many items remain; the link re-enables automatically once all 7 items pass — @pj.norris
- Config Status sidebar checklist now treats LLM Provider Keys as a single pass/fail item: the group passes (and counts as 1 of 7) if at least one provider key is set, rather than counting each of the 5 providers separately — @pj.norris

### Changed
- First-login forced password change: bootstrap admin is created with `must_change_password=True`; `POST /auth/login` returns this flag in the user payload; `login.html` intercepts it, hides the login form, and shows a mandatory change-password panel that cannot be skipped; `POST /auth/change-password` verifies the current password, enforces min-8-char and no-reuse rules, clears the flag, then re-issues a fresh login — @pj.norris
- Default admin credentials changed to `admin@sentinelone.com` / `ChangeMe!`; Admin → Settings → Access Mode now shows an info box with these credentials and a note about the first-login prompt when User Management mode is active — @pj.norris
- LLM Provider Keys panel added to Admin → Settings: admins can store encrypted API keys for OpenAI, Anthropic, Google, Perplexity, and OpenRouter; keys are persisted in `app_settings` (Fernet-encrypted), loaded into `_SHARED_LLM_KEYS` at startup, and injected into every LiteLLM request via `extra_body` — no container restart needed; per-provider Set/Update/Clear actions with masked key preview — @pj.norris
- Application Settings panel added to Admin → Settings: Daily Message Limit (0 = unlimited), Max File Size (MB), Ollama Base URL, and Ollama Model IDs are now configurable at runtime via `PATCH /admin/application-settings`; values are persisted in `app_settings` and hot-swapped in-process on save (Ollama Base URL requires a LiteLLM restart to propagate) — @pj.norris
- LiteLLM container no longer requires a `LITELLM_MASTER_KEY` env var; removed `master_key` from `litellm/config.yaml` so the proxy runs without auth on the internal Docker network. The app still manages a LiteLLM key in the admin Security panel and sends it as a Bearer token (silently accepted). No `.env` entry needed — @pj.norris
- Admin access is now password-only — no email required. `POST /auth/admin-login` accepts a password, checks it against the hashed value in `app_settings` (falling back to `ADMIN_PASSWORD` env var), and issues a JWT for the admin user — @pj.norris
- `admin.html` no longer redirects to `/login` when unauthenticated; instead shows an in-page password overlay. Logout also returns to the overlay. Footer shows "Administrator" instead of the admin email — @pj.norris
- Admin modal in `index.html` (both open mode and user mode) is now password-only; both the open-mode button and the user-mode Admin menu item open the same modal — @pj.norris

### Added
- Admin Password field in Admin → Settings → Security: same live complexity rules and confirm-tick as Postgres Password; saves a bcrypt hash to `app_settings` via `POST /admin/admin-password` — @pj.norris
- Database settings panel in Admin → Settings: Postgres Password field with confirmation input, match validation, and encrypted storage via `db_password_enc` in `app_settings`; `POST /admin/db-password` issues `ALTER USER` on the live connection, writes a `db_config_override.json` file in a persistent Docker volume (`app_data:/app/data`), and hot-swaps the SQLAlchemy engine in-process so the new credentials take effect immediately without a restart — @pj.norris
- `database.py` now checks for an override file at startup (`app/data/db_config_override.json`) and uses its `database_url` if present, so password changes survive container restarts — @pj.norris

## [2026-05-15] — v2.2.0
### Added
- Email Settings panel in Admin → Settings: SMTP host, port, username, encrypted password (Fernet via `email_password_enc`), and from address are now configurable from the UI and stored in the `app_settings` table; env vars (`SMTP_HOST`, `SMTP_PORT`, `EMAIL_USERNAME`, `EMAIL_PASSWORD`, `FROM_EMAIL`) remain as fallback for existing deployments — @pj.norris
- `POST /admin/test-email` endpoint: sends a test message to a given address using the current SMTP config and writes an `email_settings_tested` audit event — @pj.norris
- Test Email row in the Email Settings panel with a recipient input and Send Test button showing inline success/error feedback — @pj.norris

## [2026-05-15] — v2.1.0
### Changed
- Registration and first sign-in now recorded in activity log: auth-mode login fires `user_login` on every sign-in and `user_first_login` (🌟) on the first ever login (detected by absence of prior login events); open-mode wizard completion fires `guest_registered` (🌟) via `/guest/log-event`; all three event types added to admin activity log icons and labels — @pj.norris
- All guest audit log entries now store `email (name)` as the identifier (e.g. `pj.norris@sentinelone.com (PJ)`) instead of name-only; `GuestChatRequest` and `/guest/log-event` both accept `guest_email`; frontend passes `getGuestEmail()` on every chat stream and log-event call — @pj.norris
- Prompt Security `user=` field now sends the guest's email address (falls back to name, then IP) so PS user-level policy and reporting is tied to the verified identity — @pj.norris

### Added
- Allowed Email Domains setting in Admin → Settings: admins can add/remove domain entries (e.g. `sentinelone.com`) stored as a JSON array in the `app_settings` table under key `allowed_email_domains`; input validates domain format, renders as removable chips, persists immediately via `PATCH /admin/app-settings`; empty list means any domain is allowed — @pj.norris
- Startup wizard step 1 now asks for email after name: when allowed domains are configured a split `localpart @ domain-dropdown` input is shown (first domain selected by default, required); when no domains are configured a plain optional email input is shown; email stored in `hgapp_guest_email` localStorage — @pj.norris
- Email verification in wizard step 1: clicking "Send code →" calls `POST /guest/request-email-code` which generates a 4-digit OTP (10-min TTL, stored in-memory), sends a branded HTML email via SMTP (`SMTP_HOST`/`SMTP_PORT`/`EMAIL_USERNAME`/`EMAIL_PASSWORD`/`FROM_EMAIL`); four large digit boxes appear with auto-advance, paste support, and shake-on-error animation; `POST /guest/verify-email-code` validates the code before allowing progression to step 2; Resend and Edit email actions supported — @pj.norris

### Changed
- "Prompt Security Instance/Instances" renamed to "Prompt Security Region/Regions" throughout `index.html`, `admin.html`, help docs, wizard, PS settings panel, user table headers, audit log labels, and import/export messages — @pj.norris

### Added
- Explanation Notes toggle next to SE Notes in toolbar and demo panel; stored in `hga_exp_notes_on` localStorage (default on); when off, hides "Why PS Caught This" panel (`ps-why-section` class) on both single and compare mode bubbles via `body.hide-exp-notes` CSS class; added to startup wizard step 3 defaulting on — @pj.norris

## [2026-05-14]
### Added
- PS redaction tokens (`[UPPERCASE_TOKEN]`) in bot messages are now rendered as amber inline chips with a ✂ prefix and "Redacted by Prompt Security" tooltip — applied via `highlightRedacted()` post-processor in `renderMd()`, covering normal mode, compare mode, and history replay — @pj.norris
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
- Live token estimation and usage badges
- Demo scenarios: PII, topic policy, token DoS, prompt injection

## [2026-03-15] — Initial release
### Added
- Multi-user chat with streaming responses, session history, daily limits
- LiteLLM proxy: OpenAI, Anthropic, Google, OpenRouter
- Prompt Security integration (API mode) with violation detail cards
- Admin dashboard: stats, charts, user/tenant management, activity log
- PostgreSQL persistence + Docker Compose setup
