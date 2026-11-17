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

from level_core import GameDictionary, GameSession, LevelGenerator, LevelSolver

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
                count = max(1, min(30, int(body.get("count", 5))))
                start_num = int(body.get("start_level_num", 1))
                levels = []
                for i in range(count):
                    cfg = dict(body)
                    cfg["level_num"] = start_num + i
                    if cfg.get("rng_seed") is not None:
                        cfg["rng_seed"] = int(cfg["rng_seed"]) + i
                    # Level 1 automatically includes 2-letter guide lengths dynamically unless user specified word_lengths
                    if cfg["level_num"] == 1 and not cfg.get("word_lengths"):
                        cfg["rows"] = min(4, int(cfg.get("rows", 3)))
                        cfg["cols"] = min(4, int(cfg.get("cols", 3)))
                        cfg["fill_mode"] = "auto_void"
                    elif cfg["level_num"] >= 2 and cfg.get("word_lengths"):
                        cfg["word_lengths"] = [max(3, int(x)) for x in cfg["word_lengths"]]
                    levels.append(self._handle_generate(cfg))
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
