#!/usr/bin/env python3
"""Local HTTP server for the Letter Grid Word Puzzle Generator, Solver & Playtest Workbench."""

from __future__ import annotations

import argparse
import json
import mimetypes
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from level_core import GameDictionary, GameSession, LevelGenerator, LevelSolver, orient_level_forward

WORKSPACE = Path(__file__).resolve().parent
INDEX_HTML = WORKSPACE / "index.html"

GDICT = GameDictionary()
SOLVER = LevelSolver(GDICT)
GENERATOR = LevelGenerator(GDICT)
SESSIONS: dict[str, GameSession] = {}


class WorkbenchHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Concise server logging
        print(f"[HTTP] {self.address_string()} - {format % args}")

    def _send_json(self, code: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, path: Path) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        data = path.read_bytes()
        ctype, _ = mimetypes.guess_type(str(path))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{ctype or 'text/html'}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_file(INDEX_HTML)
            return
        if parsed.path == "/api/meta":
            self._send_json(
                HTTPStatus.OK,
                {
                    "meta": GDICT.meta,
                    "bands": ["1k", "2k", "3k", "4k", "5k", "6k", "7k", "8k", "9k"],
                    "spellings": ["通用", "英式 (UK)", "美式 (US)"],
                    "tutorial_2_words": sorted(GDICT.tutorial_by_word.keys()),
                },
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid JSON: {exc}"})
            return

        try:
            if parsed.path == "/api/generate":
                lvl = self._handle_generate(body)
                self._send_json(HTTPStatus.OK, {"level": lvl})
                return

            if parsed.path == "/api/generate_batch":
                levels = self._handle_generate_batch(body)
                self._send_json(HTTPStatus.OK, {"levels": levels})
                return

            if parsed.path == "/api/solve":
                level_data = body.get("level") or body
                max_rank = int(body.get("max_rank", 6000))
                extra_cats = body.get("extra_categories") or ["inflection", "obscure_base"]
                SOLVER.partition_candidates_and_solve(
                    level_data,
                    max_rank=max_rank,
                    extra_categories=extra_cats,
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "solver_report": level_data["solver_report"],
                        "extra_words": level_data["extra_words"],
                        "other_candidates": level_data.get("other_candidates", []),
                    },
                )
                return

            if parsed.path == "/api/toggle_candidate":
                level_data = body["level"]
                word = str(body["word"]).strip().upper()
                path_idx = int(body.get("path_idx", 0))
                max_rank = int(body.get("max_rank", 6000))
                extra_cats = body.get("extra_categories") or ["inflection", "obscure_base"]
                updated = SOLVER.toggle_candidate(
                    level_data,
                    word=word,
                    path_idx=path_idx,
                    max_rank=max_rank,
                    extra_categories=extra_cats,
                )
                self._send_json(HTTPStatus.OK, {"level": updated})
                return

            if parsed.path == "/api/play/start":
                level_data = body["level"]
                sid = str(uuid.uuid4())
                sess = GameSession(level_data)
                if len(SESSIONS) > 200:
                    # Evict oldest session
                    oldest = next(iter(SESSIONS))
                    SESSIONS.pop(oldest, None)
                SESSIONS[sid] = sess
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "session_id": sid,
                        "state": sess.snapshot(),
                    },
                )
                return

            if parsed.path == "/api/play/submit":
                sid = body.get("session_id")
                sess = SESSIONS.get(sid) if sid else None
                if sess is None:
                    # Recreate if level provided
                    if "level" not in body:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "试玩会话已过期，请重置关卡"})
                        return
                    sess = GameSession(body["level"])
                    sid = str(uuid.uuid4())
                    SESSIONS[sid] = sess
                path = [tuple(pt) for pt in body.get("path", [])]
                res = sess.submit_path(path)
                res["session_id"] = sid
                self._send_json(HTTPStatus.OK, res)
                return

            self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")
        except Exception as exc:
            traceback.print_exc()
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    @staticmethod
    def _handle_generate(body: dict[str, Any]) -> dict[str, Any]:
        raw_lengths = body.get("word_lengths")
        word_lengths = [int(x) for x in raw_lengths] if raw_lengths else None
        raw_seeds = body.get("seed_words")
        if isinstance(raw_seeds, str):
            seed_words = [s.strip() for s in raw_seeds.replace("，", ",").split(",") if s.strip()]
        else:
            seed_words = raw_seeds or []
        return GENERATOR.generate_level(
            level_num=int(body.get("level_num", 2)),
            rows=int(body.get("rows", 4)),
            cols=int(body.get("cols", 4)),
            template=str(body.get("template", "full")),
            void_mask=body.get("void_mask"),
            fill_mode=str(body.get("fill_mode", "auto_void")),
            word_count=int(body["word_count"]) if body.get("word_count") else None,
            word_lengths=word_lengths,
            max_rank=int(body.get("max_rank", 6000)),
            allowed_bands=body.get("allowed_bands") or None,
            allowed_spellings=body.get("allowed_spellings") or None,
            seed_words=seed_words,
            max_cell_count=int(body.get("max_cell_count", 10)),
            strictness=str(body.get("strictness", "deadlock_free")),
            extra_categories=body.get("extra_categories") or ["inflection", "obscure_base"],
            rng_seed=int(body["rng_seed"]) if body.get("rng_seed") not in (None, "") else None,
            timeout_sec=float(body.get("timeout_sec", 4.5)),
        )

    @classmethod
    def _handle_generate_batch(cls, body: dict[str, Any]) -> list[dict[str, Any]]:
        count = max(1, min(100, int(body.get("count", 5))))
        start_num = int(body.get("start_level_num", 1))
        strategy = str(body.get("strategy", "standard"))
        size_mode = str(body.get("size_mode", "current"))
        avoid_hot = bool(body.get("avoid_hot", True))
        max_cell_count = int(body.get("max_cell_count", 10))
        max_rank = int(body.get("max_rank", 6000))
        extra_cats = body.get("extra_categories") or ["inflection", "obscure_base"]
        allowed_bands = body.get("allowed_bands") or None
        allowed_spellings = body.get("allowed_spellings") or None

        presets = [(2, 4), (3, 3), (2, 5), (3, 4), (3, 5)]
        levels: list[dict[str, Any]] = []
        attempts = 0
        max_attempts = count * 8

        while len(levels) < count and attempts < max_attempts:
            idx = len(levels)
            attempts += 1
            cfg = dict(body)
            cfg["level_num"] = start_num + idx
            if size_mode == "presets":
                pr, pc = presets[(start_num - 1 + idx) % len(presets)]
                cfg["rows"] = pr
                cfg["cols"] = pc
                cfg["template"] = "full"
                cfg["fill_mode"] = "full_cover"
            if cfg.get("rng_seed") not in (None, ""):
                cfg["rng_seed"] = int(cfg["rng_seed"]) + attempts - 1

            if cfg["level_num"] == 1 and not cfg.get("word_lengths"):
                cfg["rows"] = min(4, int(cfg.get("rows", 3)))
                cfg["cols"] = min(4, int(cfg.get("cols", 3)))
                cfg["fill_mode"] = "auto_void"
            elif cfg["level_num"] >= 2 and cfg.get("word_lengths"):
                cfg["word_lengths"] = [max(3, int(x)) for x in cfg["word_lengths"]]

            if not avoid_hot:
                GENERATOR.reset_usage_stats()

            if strategy != "full_scan_peel":
                levels.append(cls._handle_generate(cfg))
                continue

            # full_scan_peel: generate base board -> scan unique-path candidates -> peel to max_cell_count
            cfg["strictness"] = "path_unique"
            try:
                base_lvl = cls._handle_generate(cfg)
            except Exception:
                continue

            rows = int(base_lvl["rows"])
            cols = int(base_lvl["cols"])
            all_cands = SOLVER.scan_all_candidates(
                base_lvl,
                max_rank=max_rank,
                allowed_bands=allowed_bands,
                allowed_spellings=allowed_spellings,
            )
            unique_elig = [c for c in all_cands if c["eligible_for_target"] and c["path_count"] == 1]

            counts = [[0] * cols for _ in range(rows)]
            void_mask = [[bool(base_lvl["board"][r][col].get("void", False)) for col in range(cols)] for r in range(rows)]
            for c in unique_elig:
                for r, col in c["sample_paths"][0]:
                    counts[r][col] += 1

            if any(not void_mask[r][col] and counts[r][col] == 0 for r in range(rows) for col in range(cols)):
                continue

            rem_order = sorted(
                range(len(unique_elig)),
                key=lambda i: (-GENERATOR.target_usage[unique_elig[i]["word"]], unique_elig[i]["length"], unique_elig[i]["word"]),
            )
            removed_words: set[str] = set()
            for i in rem_order:
                cur_max = max(counts[r][col] for r in range(rows) for col in range(cols))
                if cur_max <= max_cell_count:
                    break
                path = unique_elig[i]["sample_paths"][0]
                if all(counts[r][col] >= 2 for r, col in path):
                    for r, col in path:
                        counts[r][col] -= 1
                    removed_words.add(unique_elig[i]["word"])

            if max(counts[r][col] for r in range(rows) for col in range(cols)) > max_cell_count:
                continue

            final_targets = [c["word"] for c in unique_elig if c["word"] not in removed_words]
            final_board = [
                [
                    {
                        "r": r,
                        "c": col,
                        "void": void_mask[r][col],
                        "letter": "" if void_mask[r][col] else base_lvl["board"][r][col]["letter"],
                        "count": 0 if void_mask[r][col] else counts[r][col],
                    }
                    for col in range(cols)
                ]
                for r in range(rows)
            ]
            peeled_lvl: dict[str, Any] = {
                "level_num": int(base_lvl["level_num"]),
                "rows": rows,
                "cols": cols,
                "template": base_lvl.get("template", "full"),
                "fill_mode": base_lvl.get("fill_mode", "full_cover"),
                "board": final_board,
                "initial_words": list(final_targets),
                "initial_word_paths": {
                    c["word"]: c["sample_paths"][0]
                    for c in unique_elig
                    if c["word"] not in removed_words
                },
                "selected_candidates": [],
                "selected_candidate_paths": {},
                "target_words": final_targets,
                "generation_ms": base_lvl.get("generation_ms", 0),
            }
            SOLVER.partition_candidates_and_solve(
                peeled_lvl,
                max_rank=max_rank,
                extra_categories=extra_cats,
                allowed_bands=allowed_bands,
                allowed_spellings=allowed_spellings,
            )
            rep = peeled_lvl.get("solver_report", {})
            if not (rep.get("is_solvable") and rep.get("zero_deadlock")):
                continue
            if body.get("auto_forward", True):
                orient_level_forward(peeled_lvl)

            if avoid_hot:
                for w in final_targets:
                    GENERATOR.target_usage[w] += 1
                for ex in peeled_lvl.get("extra_words", []):
                    GENERATOR.extra_usage[ex["word"] if isinstance(ex, dict) else ex] += 1
            levels.append(peeled_lvl)

        return levels


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Letter Grid Word Puzzle Workbench Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), WorkbenchHandler)
    print(f"Workbench server running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
