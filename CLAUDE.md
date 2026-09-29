# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`stockagent` (CLI `stock`) is a research system for a watchlist of HK / A-share / US stocks. It collects prices, announcements and news; an LLM grades each event; tool-using research agents write deep dossiers ending in machine-checkable kill criteria; and it produces daily briefs and push alerts. It is a research aid only: it places no orders and gives no buy/sell advice. README.md (Chinese) is the user manual. Prompts, CLI output, comments and reports are all Simplified Chinese, so keep new user-facing strings in Chinese.

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
```

- There is no linter config or linter dependency. The `.ruff_cache/` shows ruff has only been run ad hoc (`uvx ruff check src tests`).
- Run the CLI from the repo root. It reads `./config.yaml`, `./watchlist.yaml` and `./.env` (`-c` overrides the config path).
- `company` args accept a watchlist id, name, alias or code; omit them to mean all companies.
- Production runs from cron (README 「定时运行」): `stock run --push` twice on weekdays, `stock alert` every 20 min in market hours, and a weekly `stock dossier`.
- **Adding a company:** add an entry to `watchlist.yaml` (listings with `market`/`code`/`benchmark`; `notes`, `watch_items`, `news_queries` become LLM context). Then run `stock fetch <id>` → `stock docs fetch <id>` → `stock triage <id>` → `stock profile <id>` → `stock dossier <id>`.

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
- **Also:** brief.py (daily brief, alerts); notify.py (Feishu/WeCom/Telegram, configured via `.env`); evaluate.py (`stock eval`: forward excess returns of graded events, measured from the first tradable close after publication).

**String contracts between modules.** Keep these prefixes if you edit the messages:
- Tool errors start with `错误`; the Anthropic agent loop marks those results `is_error`.
- A `web_search` result starting with `没有可用的搜索结果` counts as a search outage (`web.search_failed`), which makes a later `--reuse-notes` rerun gap-fill.
- Failed research notes start with `FAILED_MARK`.

## Rules that are easy to break

- **No shell or generic exec tool for agents.** The research agent reads untrusted web pages, so injected text must never be able to run commands. New capabilities go in tools.py as fixed `Tool`s. External CLIs (mcporter → Exa) are called with a fixed argv, never through a shell. `fetch_text` refuses loopback/private hosts, and `calculate` only evaluates a whitelisted subset of Python syntax (`safe_calc`).
- **Numbers come from code or tools, not the model.** Technicals, AH premium, price reactions, EV/FCF yield (`fcf_snapshot`) and valuation maths are all computed in Python, and prompts demand cited, tool-sourced figures. `ref/价值投资新经济-投资思路蒸馏.md` is the user's valuation framework (EV, FCF yield, Negative EV, SOTP), which `fcf_yield` implements.
- **Stay model-agnostic.** Model-specific knobs (`max_tokens`, `max_input_chars`, `tool_mode`, `json_mode`, `fallback`) belong in `config.yaml` profiles and are uncapped by default. Don't hardcode limits or tailor prompts or code to one model.
- **One `stock dossier` process at a time.** Its 4 parallel agents already exhaust the free search sources (360 captchas last 30+ min) and the local LLM gateway's account pool. Check `pgrep -fl "bin/stock dossier"` before starting one.
- **Tests stay offline.** conftest's autouse fixture disables mcporter/Exa and stubs `emdata.fcf_data`. Use the `app` fixture (temp dir, mock LLM), and monkeypatch any network call a new test reaches.
