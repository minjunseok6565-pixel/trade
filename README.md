# Nba
Make nba simulator

## State module layout
- `state.py` is a facade that re-exports symbols for backwards-compatible imports.
- Implementation lives in focused `state_*.py` modules.
- Global state/constants are centralized in `state_store.py`.
