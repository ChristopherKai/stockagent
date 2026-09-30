# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`stockagent` (CLI `stock`) is a research system for a watchlist of HK / A-share / US stocks. It collects prices, announcements and news; an LLM grades each event; tool-using research agents write deep dossiers ending in machine-checkable kill criteria; a tracker revises each dossier's valuation as new events arrive (target price = scenario-weighted valuation computed in code); and it produces daily briefs, push alerts and a local WebUI (`stock web`) with a built-in scheduler. It is a research aid only: it places no orders and gives no buy/sell ratings; the target price is labelled 「目标价（情景加权估值）」 with a disclaimer everywhere it appears. README.md (Chinese) is the user manual. Prompts, CLI output, comments and reports are all Simplified Chinese, so keep new user-facing strings in Chinese.

## Commands

```bash
uv sync                                                # Python 3.12, managed by uv
uv run pytest -q                                       # whole suite, ~10 s, fully offline
uv run pytest tests/test_agent.py::test_safe_calc -q   # one test
uv run pytest -k search_filings -q
uv run stock llm-test [--profile NAME]                 # probe each LLM profile (text + JSON)
uv run stock status                                    # DB counts, task→model routing, push channels
uv run stock -v run [company ...] [--no-llm]           # daily pipeline; -v (before the subcommand) = INFO logs
uv run stock dossier <company> [--reuse-notes] [--refill]   # deep report, several minutes per company
uv run stock target add|list|show|run|rebase|remove <company>  # tracker: code-built baseline, event-driven revisions
uv run stock web [--port 8765] [--no-scheduler] [--open]       # WebUI on 127.0.0.1 + job runner + scheduler
uv run stock screen refresh|run|show|promote|themes|eval        # screener: HK Connect + A-share universe → candidates
```

- There is no linter config or linter dependency. The `.ruff_cache/` shows ruff has only been run ad hoc (`uvx ruff check src tests`).
- Run the CLI from the repo root. It reads `./config.yaml`, `./watchlist.yaml` and `./.env` (`-c` overrides the config path).
- `company` args accept a watchlist id, name, alias or code; omit them to mean all companies.
- Production runs from the built-in scheduler inside `stock web` (config `scheduler.jobs`, cron syntax in Asia/Shanghai; README 「定时运行」 has a launchd plist): `stock run --push` twice on weekdays (includes the tracker step), `stock alert` every 20 min in market hours. System cron is the alternative; never enable both.
- **Adding a company:** add an entry to `watchlist.yaml` (listings with `market`/`code`/`benchmark`; `notes`, `watch_items`, `news_queries` become LLM context), or use the WebUI's 观察清单 page. Then run `stock fetch <id>` → `stock docs fetch <id>` → `stock triage <id>` → `stock profile <id>` → `stock dossier <id>` → `stock target add <id>` (the WebUI's 「新公司初始化」 job runs the same chain).

## Architecture

**Context object.** `App.load()` (app.py) builds `App(settings, watchlist, db, llm)` from config.yaml, watchlist.yaml and .env. Nearly every module function takes `app` first. Config and watchlist are pydantic models (config.py).

**Storage.** One SQLite file, `data/stock.db` (db.py: WAL mode, shared across threads behind an RLock).
- Timestamps are stored as UTC ISO strings; `utils.iso()` rejects naive datetimes. Display is in `CST` (Asia/Shanghai).
- **No migrations:** the schema is `CREATE TABLE IF NOT EXISTS`, so a column added to `SCHEMA` never reaches an existing DB. Write an explicit migration.
- The `kv` table holds cross-run state:
  - filing watermarks `fetched:filings:<HK:00100>`. The first fetch only looks back `announcements_lookback_days`, and raising it later does not backfill.
  - per-source circuit breakers `down:<source>`: 60 min, shared by every process.
  - cached external IDs: `hkex_stock_id:`, `cninfo_org_id:`, `sec_cik:`, `em_secid:`, `us_secid:`.
  - once-a-day markers: `reports_checked:`, `monitor_checked:`.
  - scheduler fire markers `sched_last:<schedule name>` (last minute fired, CST ISO).
- Tracker tables: `trackers` (one row per tracked company; points at the current baseline dossier and latest revision), `revisions` (baseline / revision / review snapshots with the full model JSON, applied and rejected changes, assessed evidence, trigger-status snapshot), `tracker_refs` (primary key `(company_id, ref)`: every piece of evidence the tracker has shown to the model, so nothing is processed twice). `jobs` holds WebUI / scheduler / tracker-queued background jobs.
- Process locks live in `data/tmp/locks/*.lock` (fcntl.flock, released when the process dies): `dossier`, `pipeline` (run / alert / target run), `web` (only one scheduler + job runner). `PRAGMA user_version` is set (currently 1); new columns on existing tables need an ALTER keyed on it.
- `data/` is gitignored, generated output: the DB; `docs/` (originals plus extracted `.txt`); `dossiers/` (html/md/pdf plus `notes/*.json`); `reports/`; `inbox/`; `tmp/`.

**Instrument keys.** The Eastmoney `secid` keys all price data:
- `116.00100` HK; `1.` SH; `0.` SZ/BJ; `105/106/107.TICKER` US.
- `100.HSI` / `124.HSTECH` indices; `133.HKDCNH` FX.

`bars`/`quotes` rows use it, and the Tencent and Frankfurter fallbacks translate from it. Watchlist listings use `Listing.key`, e.g. `HK:00100`.

**Data layer** (`sources/`, `app.fetch`, docs.py).
- Adapters return plain dicts. `app._try` applies the circuit breaker; `app._guard` stops one failing source from aborting a run. `sources.price_sources` sets the fallback order.
- Documents are copied to `data/docs/<company>/`, deduped by SHA-256, and text-extracted with a `[第N页]` marker per page. `tools.split_pages`, `search_filings` and the citation format depend on those markers.
- Filing search converts both query and text to Simplified Chinese (`utils.to_simplified`). Never convert the other way.
- `docs.fetch_reports` only downloads documents in HKEX/cninfo's annual and interim *report* categories, skipping any `(doc_type, period)` that already exists. When adding something like a results announcement by hand, give it `--type other`; typing it `interim_report` would stop the real report from ever downloading.
- Users add material via `data/inbox/<company>/` (imported by `stock run`), `stock docs add`, `stock data add` and `stock note`.

**LLM layer** (llm.py).
- Tasks (`triage`, `stock_brief`, `market_brief`, `profile`, `research`, `dossier`, `monitor`) route to profiles via `llm.tasks`. Protocols: `anthropic`, `openai` (any compatible endpoint) and `mock`.
- `complete()`: JSON-schema output plus a pydantic `validate` hook, retried once with the parse error appended. Truncated text is auto-continued up to `max_continuations` times.
- `run_agent()`: the tool loop, using native tool calls or a `<tool_call>` text protocol (`tool_mode: text`).
- Fallback: retryable failures (connection errors, 402/403/408/409/429/5xx) move down the profile's `fallback` chain. A failed profile is tried last for the next 120 s.
- Every call is logged to `llm_calls`.
- `mock` fabricates schema-valid JSON and echoes any `id=` values in the prompt; the tests rely on this.

**Pipelines.**
- **Triage** (triage.py): a conservative title dedupe runs in code, then batches go to the LLM along with the last 3 days of graded events for cross-batch dedupe. Items the model drops get `triage_attempts` bumped; items stop being retried after 3 attempts.
- **Profile** (profile.py): uses the latest annual and interim reports plus up to 3 other docs. Input is budgeted by `report.profile_max_doc_chars` (and the profile's optional `max_input_chars`), excerpting around key section headings.
- **Dossier** (dossier.py), the core pipeline, in order:
  1. 4 research sub-agents (`SECTIONS`) run in parallel, each with a fresh `ToolBox`.
  2. Notes are saved to `data/dossiers/notes/` straight away, so `--reuse-notes` can skip research and redo only failed sections.
  3. A gap-fill agent searches for missing data; whatever it can't find becomes `data_requests` for the user.
  4. The report is written in two parts.
  5. 「未披露」 claims are audited against the notes.
  6. A key-facts coverage check adds anything missed.
  7. The tracking card is extracted (pydantic `Card`).
  8. `db.save_dossier` deactivates old triggers and replaces catalysts and the plan.
  9. Output is rendered to HTML/MD, and to PDF via headless Chrome with a throwaway profile in `data/tmp/`. `verify.py` lists report numbers not traceable to notes or tool output.
- **Feedback loop:**
  - The card's plan (`related_tickers`, `news_keywords`) widens what `fetch_news` and `fetch_market` collect.
  - monitor.py checks kill criteria on each run: price metrics in code, fundamental/event ones by LLM. Status only escalates (ok → warning → triggered).
  - Non-ok criteria are fed into the next dossier (`previous_alerts`).
- **Tracker** (valuation.py, tracker.py; runs as step 5/6 of `stock run`, after monitor and before the brief):
  1. Baseline (no LLM): `valuation.build_baseline` finds the dossier's `valuation_matrix` tool call that explains the most card scenario endpoints (±1.5%), skips calls whose shares disagree with `quote.market_cap / price / 1e8` by >30%, and solves each scenario's multiple so its price equals the card range midpoint. No usable matrix → `method="range"` model (drivers are prices, only scalable). Price = `tools.matrix_price` (same function as the matrix tool), clamped at 0; target = probability-weighted (`tracker.target`). fx is recomputed in code from `133.HKDCNH` / `133.USDCNH` bars; the report currency is inferred by matching the matrix fx against those rates.
  2. Candidates: items triaged after (baseline dossier − 2 h) and published ≤ 30 days before it, materiality ≥ `tracker.min_materiality`, not yet in `tracker_refs`; trigger status escalations vs the last revision's snapshot; documents and manual data added after the dossier. No candidates → no LLM call.
  3. Stage A: `run_agent("tracker")` with a `ToolBox(allow=...)` whitelist (web_search removed while the dossier lock is held). Context = valuation model, tracking card (thesis, kill criteria, catalysts, watch metrics, data gaps, related tickers), the dossier's valuation/scenario/insight sections, recent revisions.
  4. Stage B: `complete("tracker")` returns changes as `{path, op: set|add|mul, operand, reason, evidence[], derivation}`; code applies them (`apply_changes`). Rejected: invalid path/op, no valid evidence code, out-of-bounds values, fact paths (`g.shares`, `g.net_cash`, `set` on drivers) whose operand is not found in cited evidence or tool results. Fact-path moves > `tracker.flag_change` are not applied but stored as `confirm: true` entries for the user (`tracker.confirm_change`, WebUI 「采纳」). Methodology issues go to `questions` → flags. No `validate=` hook on this call (one bad change must not fail the whole revision).
  5. Materiality ≥ `tracker.redo_materiality` judged material, or thesis broken → `queue_redo` inserts a `dossier` job (cooldown `redo_cooldown_days`); `cmd_dossier` re-baselines tracked companies after saving the new dossier. `dossier.previous_revisions()` feeds revisions since the last dossier into the next one.
- **Jobs & scheduler** (jobs.py, jobrun.py, scheduler.py, cron.py): the WebUI and scheduler may only run tasks registered in `jobs.TASKS` (fixed argv, validated company ids and flags, no shell). `JobRunner` (thread in `stock web`) launches `python -m stockagent.jobrun <id>`, which runs the CLI in-process and writes its own exit status; groups `pipeline` and `dossier` are mutually exclusive (also checked against the file locks). Free-text input (notes, manual data, uploads) is handled in the web process, never passed as argv.
- **WebUI** (web/): FastAPI + Jinja2, server-rendered, vanilla JS/CSS, SVG chart built in Python. Pages read the DB only (no network). Config edits go through `web/yamledit.py` (ruamel round-trip, comments kept, codes written as double-quoted strings, full validation, backup, etag conflict check); `.env` edits only accept names ending `_KEY/_TOKEN/_SECRET/_WEBHOOK`.
- **Screener** (`screen/`, `stock screen`, WebUI 选股): depends on the core; the core never imports it. Own tables (`screen_*`, created by `screen.store.ensure`), own `screen` lock and job group.
  - L0 `universe.py`: 港股通 list + A-share valuation snapshot + statements, all via `sources/emscreen.py` bulk datacenter queries (never push2 `clist`: it rate-limits hard and then breaks single quotes too). `emscreen.rows_all` requires a unique sort key; unsorted datacenter paging drops and duplicates rows. A-share statements are cached per report period in `screen_bulk`.
  - Statement shapes come from `emdata.hk_fcf_from_rows` / `a_fcf_from_rows` (same as `fcf_data`); metrics from `fcf.metrics` (shared with the `fcf_yield` tool). `fcf.capex_checked` replaces near-zero HK capex with net investing outflow and flags it.
  - `events.py`: HKEX whole-market daily titles + cninfo keyword search + A-share forecasts, classified by regex `RULES` (no LLM); negatives veto. Watermarks `screen:events:<source>` in kv.
  - `buckets.py`: L1 filters, the five buckets (floor / quality / theme → long pool; event / revision → event pool), rough odds, trap flags, tiers per pool: A 高胜率+高赔率, B 高胜率, C 高赔率, D 观察, X 快筛淘汰 (`tier_for`). Odds are for ranking only.
  - `calendar.py`: forward catalysts from cached bulk data (announced / pattern-predicted dividends, A-share report appointments and HK statutory deadlines, A-share lock-up expiries as risk flags, dated catalysts from existing dossiers). Model-predicted catalysts never count toward tiers.
  - Liquidity (`run.liquidity`): 20-day median turnover (fallbacks: turnover × mcap, volume × close with A-share lots of 100), zero-volume days, capacity estimate.
  - `quick.py`: one `complete("screen")` per candidate; it can only downgrade to X, never upgrade (the model invents catalysts). `themes.py`: `themes.yaml` + `run_agent("screen_theme")` with `ToolBox(app, None, allow=...)`; suggestions are validated against the universe and written to `themes.yaml` only when the user accepts. `promote.py` writes watchlist entries (A+H both listings). `evaluate.py`: forward excess returns from `price_date`.
- **Also:** brief.py (daily brief, alerts; renders the tracker line in code, never passes target numbers to the brief LLM); notify.py (Feishu/WeCom/Telegram, configured via `.env`); evaluate.py (`stock eval`: forward excess returns of graded events, measured from the first tradable close after publication).

**String contracts between modules.** Keep these prefixes if you edit the messages:
- Tool errors start with `错误`; the Anthropic agent loop marks those results `is_error`.
- A `web_search` result starting with `没有可用的搜索结果` counts as a search outage (`web.search_failed`), which makes a later `--reuse-notes` rerun gap-fill.
- Failed research notes start with `FAILED_MARK`.
- Tracker evidence codes shown to the model: events `id=<short_id>` (12 hex chars, in the user message so the mock can echo them), kill criteria `K<id>`, documents `D<id>`, manual data `M<id>`, catalysts `C<n>`, stage-A tool results `T<n>`. `tracker_refs.ref` prefixes: `item:`, `trig:<id>:<status>`, `doc:`, `man:<id>:<imported_at>`, `cat:<dossier>:<hash>`. Only the Stage B `events` array uses an `id` field (the mock expands arrays with an `id` property per hex id).

## Rules that are easy to break

- **No shell or generic exec tool for agents.** The research agent reads untrusted web pages, so injected text must never be able to run commands. New capabilities go in tools.py as fixed `Tool`s. External CLIs (mcporter → Exa) are called with a fixed argv, never through a shell. `fetch_text` refuses loopback/private hosts, and `calculate` only evaluates a whitelisted subset of Python syntax (`safe_calc`).
- **Numbers come from code or tools, not the model.** Technicals, AH premium, price reactions, EV/FCF yield (`fcf_snapshot`) and valuation maths are all computed in Python, and prompts demand cited, tool-sourced figures. `ref/价值投资新经济-投资思路蒸馏.md` is the user's valuation framework (EV, FCF yield, Negative EV, SOTP), which `fcf_yield` implements.
- **Stay model-agnostic.** Model-specific knobs (`max_tokens`, `max_input_chars`, `tool_mode`, `json_mode`, `fallback`) belong in `config.yaml` profiles and are uncapped by default. Don't hardcode limits or tailor prompts or code to one model.
- **One `stock dossier` process at a time.** Its 4 parallel agents already exhaust the free search sources (360 captchas last 30+ min) and the local LLM gateway's account pool. `cmd_dossier` enforces this with the `dossier` file lock (a second one exits with a message); `pgrep` misses jobs started as `python -m stockagent.jobrun`, so check `stock web`'s 状态 page or the lock instead.
- **Target prices come from code.** The tracker model never states a new price: it proposes op/operand changes with evidence codes; code validates provenance and recomputes. Keep the scenario-weighted-valuation label and disclaimer wherever a target price is shown.
- **WebUI safety.** Bind only 127.0.0.1; keep TrustedHost, the Origin check and the CSRF dependency on every router; sanitize any LLM/web-derived markdown with `render.md_html` (nh3) and links with `safe_url`; serve stored report HTML with the sandbox CSP; pages must not call networked helpers (`ensure_bars`, `latest_quote`, US `resolve_ticker`).
- **Tests stay offline.** conftest's autouse fixture disables mcporter/Exa and stubs `emdata.fcf_data`. Use the `app` fixture (temp dir, mock LLM), and monkeypatch any network call a new test reaches.
