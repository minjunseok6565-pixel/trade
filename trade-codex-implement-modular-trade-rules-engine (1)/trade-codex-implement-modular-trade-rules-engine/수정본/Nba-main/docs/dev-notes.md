# Dev Notes: Feature Verification Checklist

## Master Schedule (1230 games)
- `_build_master_schedule` now enforces 30-team, 82-game-per-team coverage by assigning fixed 4/3/2 game counts per matchup with deterministic 4-game rotations across divisions.
- Added `get_schedule_summary` helper and `/api/debug/schedule-summary` endpoint for quick JSON verification of totals/home-away splits.
- Pytest `tests/test_schedule.py` asserts 1230 total games, 82 per team, and tight home/away balance (skips automatically if `pandas` is unavailable in the environment).

## League Progress (auto-sim for other teams)
- `advance_league_until` still simulates all non-user games between `current_date` and `target_date`, updates state/boxscores, and calls weekly AI GM ticks after updating `league["current_date"]`.
- `/api/advance-league` forwards the target date and user team ID unchanged; `simulateGameProgress` on the front-end triggers this before the user’s game sim (no changes required).

## Stats / Standings / Teams / News Tabs
- Back-end stats pipeline: `update_state_with_game` accumulates player season totals from boxscores; `compute_league_leaders` surfaces per-game leaders for the four tracked categories.
- Standings use `_compute_team_records` plus `get_conference_standings` (rank/GB sorting) and are exposed via `/api/standings`.
- Team card/detail APIs (`/api/teams`, `/api/team-detail/{id}`) include meta, record, payroll/cap, and per-player season averages; front-end tabs consume them directly.
- Weekly news flow: `refresh_weekly_news` caches Gemini-generated summaries; front-end `loadWeeklyNewsIfNeeded` renders them when an API key is present.

## AI Trades / Deadline
- `_run_ai_gm_tick_if_needed` keeps weekly cadence and respects the Feb 5 trade deadline.
- Trades mutate `ROSTER_DF`-backed state and append both transaction logs and news feed items.

## Observations / Potential Follow-ups
- Home/away balancing now stays within ±2 games; deeper parity or travel clustering could be explored later.
- If CI lacks `pandas`, install or vendor the dependency to run the new schedule tests instead of skipping.
- Additional smoke tests for standings/leaders after multi-day sims would provide extra coverage once a seeded sim harness is available.
