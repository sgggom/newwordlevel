# newwordlevel

Letter Grid Word Puzzle Engine, Solver, Generator & Workbench.

## Requirements
- Python 3.10+ (standard library only, no third-party packages required)

## Quick Start on Any Device

### 1. Run the Local Web Workbench
```bash
python3 server.py
```
Then open `http://127.0.0.1:8000` in your browser.
*(Note: GitHub Pages only serves static files, so `/api/*` endpoints require running `server.py`.)*

### 2. Batch Generate Level Pool
```bash
python3 batch_generate_pool.py --count 10000 --max-cell-count 10 --workers 8 --out pool_10k.jsonl
```
