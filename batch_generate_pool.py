#!/usr/bin/env python3
"""Portable multi-process level pool generator using unique-path full scan + peeling."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE))

from level_core import GameDictionary, LevelGenerator, LevelSolver, orient_level_forward


def worker_run(args: tuple[int, int, int, int, int, str]) -> dict:
    wid, target_n, max_cell_count, max_rank, seed_base, tmp_dir = args
    chunk_path = os.path.join(tmp_dir, f".chunk_w{wid}_{os.getpid()}.jsonl")

    gdict = GameDictionary()
    gen = LevelGenerator(gdict)
    solver = LevelSolver(gdict)
    gen._record_usage = lambda *a, **kw: None

    target_usage: Counter[str] = Counter()
    extra_usage: Counter[str] = Counter()
    combined_usage: Counter[str] = Counter()
    gen.target_usage = target_usage
    gen.extra_usage = extra_usage

    presets = [
        {"rows": 2, "cols": 4},
        {"rows": 3, "cols": 3},
        {"rows": 2, "cols": 5},
        {"rows": 3, "cols": 4},
        {"rows": 3, "cols": 5},
    ]

    def refresh_hot_avoid() -> set[str]:
        if not combined_usage:
            return set()
        avg = sum(combined_usage.values()) / len(combined_usage)
        thresh = max(10.0, avg * 1.3)
        return {w for w, cnt in combined_usage.most_common(150) if cnt >= thresh}

    hot_avoid: set[str] = set()
    success = 0
    attempts = 0

    with open(chunk_path, "w", encoding="utf-8") as f_out:
        while success < target_n:
            cfg = presets[(attempts + wid) % len(presets)]
            rows, cols = cfg["rows"], cfg["cols"]
            attempts += 1
            gen.generated_count = success

            if attempts % 50 == 0:
                hot_avoid = refresh_hot_avoid()

            try:
                lvl = gen.generate_level(
                    level_num=2,
                    rows=rows,
                    cols=cols,
                    template="full",
                    fill_mode="full_cover",
                    max_cell_count=max_cell_count,
                    strictness="path_unique",
                    avoid_words=hot_avoid,
                    rng_seed=seed_base + wid * 500000 + attempts,
                    timeout_sec=2.0,
                )
            except Exception:
                continue

            all_cands = solver.scan_all_candidates(lvl, max_rank=max_rank)
            unique_elig = [c for c in all_cands if c["eligible_for_target"] and c["path_count"] == 1]

            counts = [[0] * cols for _ in range(rows)]
            for c in unique_elig:
                for r, col in c["sample_paths"][0]:
                    counts[r][col] += 1

            if any(counts[r][col] == 0 for r in range(rows) for col in range(cols)):
                continue

            selected = list(unique_elig)
            rem_order = sorted(
                range(len(selected)),
                key=lambda idx: (-target_usage[selected[idx]["word"]], selected[idx]["length"], selected[idx]["word"]),
            )
            removed_words: set[str] = set()
            for idx in rem_order:
                cur_max = max(counts[r][col] for r in range(rows) for col in range(cols))
                if cur_max <= max_cell_count:
                    break
                path = selected[idx]["sample_paths"][0]
                if all(counts[r][col] >= 2 for r, col in path):
                    for r, col in path:
                        counts[r][col] -= 1
                    removed_words.add(selected[idx]["word"])

            if max(counts[r][col] for r in range(rows) for col in range(cols)) > max_cell_count:
                continue

            final_targets = [c["word"] for c in selected if c["word"] not in removed_words]
            final_extras = [c["word"] for c in all_cands if c["word"] not in set(final_targets)]

            final_board = [
                [
                    {
                        "r": r,
                        "c": col,
                        "void": False,
                        "letter": lvl["board"][r][col]["letter"],
                        "count": counts[r][col],
                    }
                    for col in range(cols)
                ]
                for r in range(rows)
            ]
            final_lvl = {
                "level_num": 2,
                "rows": rows,
                "cols": cols,
                "board": final_board,
                "target_words": final_targets,
                "extra_words": final_extras,
            }

            rep = solver.analyze_solvability(final_lvl, max_states=10000)
            if not (rep.get("is_solvable") and rep.get("zero_deadlock")):
                continue
            orient_level_forward(final_lvl)

            for w in final_targets:
                target_usage[w] += 1
                combined_usage[w] += 1
            for w in final_extras:
                extra_usage[w] += 1
                combined_usage[w] += 1

            success += 1
            f_out.write(json.dumps(final_lvl, ensure_ascii=False) + "\n")

    return {"wid": wid, "chunk_path": chunk_path, "success": success, "attempts": attempts}


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch generate zero-deadlock word grid levels")
    parser.add_argument("--count", type=int, default=10000, help="Number of levels to generate")
    parser.add_argument("--max-cell-count", type=int, default=10, help="Max count per cell")
    parser.add_argument("--max-rank", type=int, default=6000, help="Dictionary frequency rank limit")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4, help="Parallel worker processes")
    parser.add_argument("--seed", type=int, default=1000000, help="Base RNG seed")
    parser.add_argument("--out", type=str, default="pool_levels.jsonl", help="Output JSONL path")
    args = parser.parse_args()

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = WORKSPACE / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    n_workers = max(1, min(args.workers, args.count))
    per_worker = args.count // n_workers
    rem = args.count % n_workers
    tasks = [
        (wid, per_worker + (1 if wid < rem else 0), args.max_cell_count, args.max_rank, args.seed, str(out_path.parent))
        for wid in range(n_workers)
    ]

    print(f"Generating {args.count} levels (max_cell_count={args.max_cell_count}, workers={n_workers})...")
    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        results = list(ex.map(worker_run, tasks))

    t_u: Counter[str] = Counter()
    e_u: Counter[str] = Counter()
    size_counts: Counter[str] = Counter()
    total_levels = 0

    with open(out_path, "w", encoding="utf-8") as f_out:
        for res in results:
            cpath = res["chunk_path"]
            with open(cpath, "r", encoding="utf-8") as f_in:
                for line in f_in:
                    f_out.write(line)
                    d = json.loads(line)
                    total_levels += 1
                    size_counts[f"{d['rows']}x{d['cols']}"] += 1
                    for w in d["target_words"]:
                        t_u[w] += 1
                    for w in d["extra_words"]:
                        e_u[w] += 1
            os.remove(cpath)

    elapsed = time.perf_counter() - t0
    print(f"Done in {elapsed:.2f}s: {total_levels} levels -> {out_path}")
    print(f"Sizes: {dict(size_counts)}")
    print(f"Unique targets: {len(t_u)}, Unique extras: {len(e_u)}")


if __name__ == "__main__":
    main()
