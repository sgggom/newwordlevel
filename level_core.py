#!/usr/bin/env python3
"""Core game engine, solver (with deadlock detection & extra-word scan), and level generator.

Strict game rules implemented:
1. Fixed board with active cells (letter + remaining count) and optional void cells; letters never fall/move/refill.
2. Orthogonal-only adjacent path (4-neighbors), no diagonals, no jumps, no crossing void/cleared cells,
   no reusing the same cell within a single word; backtracking to the previous cell undoes the last step.
3. Completing an unfound target word decrements count by 1 on each cell along the player's actual path.
   Cells reaching count 0 have their letter cleared and become impassable.
4. Extra words are level-recognized words not in targets: recorded once, consume 0 cell counts,
   do not advance target progress, and only include lengths present in the current level's target words.
5. Submission validity is strictly determined by the level's target_words and extra_words lists.
   Already-found words neither score again nor consume counts; invalid words/paths change nothing.
   Level 2+ requires word length >= 3; Level 1 permits curated 2-letter tutorial words.
6. Level cleared iff ALL target words are found AND ALL non-void cells have remaining count == 0.
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parent
DICT_PATH = WORKSPACE / "game_dictionary.json"

DIRS = [(-1, 0), (1, 0), (0, -1), (0, 1)]


def _neighbors(r: int, c: int, rows: int, cols: int) -> list[tuple[int, int]]:
    out = []
    for dr, dc in DIRS:
        nr, nc = r + dr, c + dc
        if 0 <= nr < rows and 0 <= nc < cols:
            out.append((nr, nc))
    return out


class TrieNode:
    __slots__ = ("children", "word", "entry_info")

    def __init__(self) -> None:
        self.children: dict[str, TrieNode] = {}
        self.word: str | None = None
        self.entry_info: dict[str, Any] | None = None


class GameDictionary:
    """In-memory layered dictionary with bitmask pattern index and extra-word Trie."""

    def __init__(self, dict_path: Path = DICT_PATH) -> None:
        raw = json.loads(dict_path.read_text(encoding="utf-8"))
        self.meta: dict[str, Any] = raw["meta"]
        self.core_words: list[dict[str, Any]] = raw["core_words"]
        self.tutorial_2: dict[str, dict[str, str]] = raw["tutorial_2letter_words"]
        self.inflections: dict[str, dict[str, Any]] = raw["inflections"]
        self.obscure_words: dict[str, dict[str, Any]] = raw["obscure_words"]

        self.core_by_word: dict[str, dict[str, Any]] = {}
        for item in self.core_words:
            w_up = item["word"].upper()
            item_up = dict(item)
            item_up["word_upper"] = w_up
            item_up["rarity_tier"] = max(1, min(5, int(item_up.get("rarity_tier") or 1)))
            self.core_by_word[w_up] = item_up

        self.tutorial_by_word: dict[str, dict[str, Any]] = {
            w.upper(): {
                **info,
                "word_upper": w.upper(),
                "length": 2,
                "rarity_tier": max(1, min(5, int(info.get("rarity_tier") or 1))),
            }
            for w, info in self.tutorial_2.items()
        }

        # Bitmask index for core_words (index 0..len-1)
        # pos_masks[length][pos][ch_upper] -> int bitmask
        self.pos_masks: dict[int, list[dict[str, int]]] = {
            L: [defaultdict(int) for _ in range(L)] for L in range(3, 9)
        }
        self.length_masks: dict[int, int] = defaultdict(int)
        for idx, item in enumerate(self.core_words):
            w_up = item["word"].upper()
            L = len(w_up)
            if L not in self.pos_masks:
                continue
            bit = 1 << idx
            self.length_masks[L] |= bit
            for pos, ch in enumerate(w_up):
                self.pos_masks[L][pos][ch] |= bit

        # Bitmask index for tutorial 2-letter words
        self.tut_list: list[dict[str, Any]] = list(self.tutorial_by_word.values())
        self.tut_pos_masks: list[dict[str, int]] = [defaultdict(int), defaultdict(int)]
        self.tut_all_mask: int = 0
        for idx, item in enumerate(self.tut_list):
            bit = 1 << idx
            self.tut_all_mask |= bit
            for pos, ch in enumerate(item["word_upper"]):
                self.tut_pos_masks[pos][ch] |= bit

        self._filter_cache: dict[tuple[Any, ...], int] = {}

        # Build unified Trie for extra-word scanning
        self.extra_trie = TrieNode()
        for item in self.core_words:
            w_up = item["word"].upper()
            self._insert_trie(
                w_up,
                {
                    "word": w_up,
                    "source": "core",
                    "rank": item["id"],
                    "band": item["band"],
                    "ipa": item["ipa"],
                    "zh": item["zh"],
                    "spelling": item["spelling"],
                    "extra_only": bool(item.get("extra_only", False)),
                    "rarity_tier": max(1, min(5, int(item.get("rarity_tier") or 1))),
                },
            )
        for w, info in self.inflections.items():
            w_up = w.upper()
            self._insert_trie(
                w_up,
                {
                    "word": w_up,
                    "source": "inflection",
                    "lemma": info["lemma"].upper(),
                    "tag_desc": info["tag_desc"],
                    "zh": info["zh"],
                    "rarity_tier": max(1, min(5, int(info.get("rarity_tier") or 4))),
                },
            )
        for w, info in self.obscure_words.items():
            w_up = w.upper()
            self._insert_trie(
                w_up,
                {
                    "word": w_up,
                    "source": "obscure_ecdict",
                    "bnc": info["bnc"],
                    "frq": info["frq"],
                    "zh": info["zh"],
                    "rarity_tier": max(1, min(5, int(info.get("rarity_tier") or 5))),
                },
            )
        for w_up, info in self.tutorial_by_word.items():
            self._insert_trie(
                w_up,
                {
                    "word": w_up,
                    "source": "tutorial_2",
                    "ipa": info.get("ipa", ""),
                    "zh": info.get("zh", ""),
                    "rarity_tier": max(1, min(5, int(info.get("rarity_tier") or 1))),
                },
            )

    def _insert_trie(self, word_up: str, entry_info: dict[str, Any]) -> None:
        node = self.extra_trie
        for ch in word_up:
            nxt = node.children.get(ch)
            if nxt is None:
                nxt = TrieNode()
                node.children[ch] = nxt
            node = nxt
        node.word = word_up
        node.entry_info = entry_info

    def get_core_filter_mask(
        self,
        max_rank: int = 6000,
        allowed_bands: list[str] | None = None,
        allowed_spellings: list[str] | None = None,
    ) -> int:
        bands_key = tuple(sorted(allowed_bands)) if allowed_bands else None
        spell_key = tuple(sorted(allowed_spellings)) if allowed_spellings else None
        key = (max_rank, bands_key, spell_key)
        cached = self._filter_cache.get(key)
        if cached is not None:
            return cached
        mask = 0
        bands_set = set(allowed_bands) if allowed_bands else None
        spell_set = set(allowed_spellings) if allowed_spellings else None
        for idx, item in enumerate(self.core_words):
            if item.get("extra_only"):
                continue
            if item["id"] > max_rank:
                continue
            if bands_set is not None and item["band"] not in bands_set:
                continue
            if spell_set is not None and item["spelling"] not in spell_set:
                continue
            mask |= 1 << idx
        self._filter_cache[key] = mask
        return mask

    def count_matching_words(
        self,
        pattern: list[str],
        level_num: int = 2,
        max_rank: int = 6000,
        allowed_bands: list[str] | None = None,
        allowed_spellings: list[str] | None = None,
    ) -> int:
        """O(1) bitmask count of core words matching pattern."""
        L = len(pattern)
        if L == 2:
            if level_num != 1:
                return 0
            m = self.tut_all_mask
            for pos, ch in enumerate(pattern):
                if ch and ch != "?":
                    m &= self.tut_pos_masks[pos].get(ch.upper(), 0)
                    if not m:
                        return 0
            return m.bit_count()
        if L not in self.pos_masks:
            return 0
        m = self.get_core_filter_mask(max_rank, allowed_bands, allowed_spellings) & self.length_masks[L]
        if not m:
            return 0
        pm = self.pos_masks[L]
        for pos, ch in enumerate(pattern):
            if ch and ch != "?":
                m &= pm[pos].get(ch.upper(), 0)
                if not m:
                    return 0
        return m.bit_count()

    def match_pattern(
        self,
        pattern: list[str],
        level_num: int = 2,
        max_rank: int = 6000,
        allowed_bands: list[str] | None = None,
        allowed_spellings: list[str] | None = None,
        exclude_words: set[str] | None = None,
        rng: random.Random | None = None,
        sample_limit: int = 40,
        usage_counts: dict[str, int] | None = None,
    ) -> list[str]:
        """Return candidate uppercase words matching pattern ('' or '?' = wildcard)."""
        L = len(pattern)
        excl = exclude_words or set()
        if L == 2:
            if level_num != 1:
                return []
            m = self.tut_all_mask
            for pos, ch in enumerate(pattern):
                if ch and ch != "?":
                    m &= self.tut_pos_masks[pos].get(ch.upper(), 0)
                    if not m:
                        return []
            cands = []
            while m:
                lsb = m & -m
                idx = lsb.bit_length() - 1
                w = self.tut_list[idx]["word_upper"]
                if w not in excl:
                    cands.append(w)
                m ^= lsb
            if rng:
                rng.shuffle(cands)
            if usage_counts:
                cands.sort(key=lambda w: usage_counts.get(w, 0))
            return cands[:sample_limit]

        if L not in self.pos_masks:
            return []
        m = self.get_core_filter_mask(max_rank, allowed_bands, allowed_spellings) & self.length_masks[L]
        if not m:
            return []
        pm = self.pos_masks[L]
        for pos, ch in enumerate(pattern):
            if ch and ch != "?":
                m &= pm[pos].get(ch.upper(), 0)
                if not m:
                    return []
        # Extract matching indices efficiently
        indices: list[int] = []
        while m:
            lsb = m & -m
            indices.append(lsb.bit_length() - 1)
            m ^= lsb
        pool_cap = sample_limit * 4 if usage_counts else sample_limit * 2
        if rng and len(indices) > pool_cap:
            indices = rng.sample(indices, pool_cap)
        elif rng:
            rng.shuffle(indices)
        out: list[str] = []
        for idx in indices:
            w = self.core_words[idx]["word"].upper()
            if w not in excl:
                out.append(w)
        if usage_counts:
            out.sort(key=lambda w: usage_counts.get(w, 0))
        return out[:sample_limit]

    def get_word_rarity_tier(self, word_up: str) -> int:
        """Return integer rarity tier in [1, 5] (1 = easiest, 5 = rarest)."""
        w = word_up.upper()
        if w in self.core_by_word:
            return max(1, min(5, int(self.core_by_word[w].get("rarity_tier") or 1)))
        if w in self.tutorial_by_word:
            return max(1, min(5, int(self.tutorial_by_word[w].get("rarity_tier") or 1)))
        inf = self.inflections.get(w.lower())
        if inf:
            if inf.get("rarity_tier") is not None:
                return max(1, min(5, int(inf["rarity_tier"])))
            lemma_up = inf["lemma"].upper()
            if lemma_up in self.core_by_word:
                return min(5, self.get_word_rarity_tier(lemma_up) + 1)
            return 4
        obs = self.obscure_words.get(w.lower())
        if obs:
            return max(1, min(5, int(obs.get("rarity_tier") or 5)))
        return 5

    def get_word_rarity(self, word_up: str) -> int:
        """Compute word rarity score: max(0, len(w) - 3) * max(0, rarity_tier - 1)."""
        w = word_up.upper()
        tier = self.get_word_rarity_tier(w)
        return max(0, len(w) - 3) * max(0, tier - 1)

    def lookup_word_meta(self, word_up: str) -> dict[str, Any]:
        w = word_up.upper()
        rarity_tier = self.get_word_rarity_tier(w)
        rarity = self.get_word_rarity(w)
        if w in self.core_by_word:
            it = self.core_by_word[w]
            return {
                "word": w,
                "rank": it["id"],
                "band": it["band"],
                "tier": it["tier"],
                "spelling": it["spelling"],
                "ipa": it["ipa"],
                "zh": it["zh"],
                "rarity_tier": rarity_tier,
                "rarity": rarity,
                "source": "core",
            }
        if w in self.tutorial_by_word:
            it = self.tutorial_by_word[w]
            return {
                "word": w,
                "rank": 0,
                "band": "引导词",
                "tier": "第1关引导词",
                "spelling": "通用",
                "ipa": it.get("ipa", ""),
                "zh": it.get("zh", ""),
                "rarity_tier": rarity_tier,
                "rarity": rarity,
                "source": "tutorial_2",
            }
        inf = self.inflections.get(w.lower())
        if inf:
            return {
                "word": w,
                "source": "inflection",
                "lemma": inf["lemma"].upper(),
                "tag_desc": inf["tag_desc"],
                "zh": inf["zh"],
                "rarity_tier": rarity_tier,
                "rarity": rarity,
            }
        obs = self.obscure_words.get(w.lower())
        if obs:
            return {
                "word": w,
                "source": "obscure_ecdict",
                "bnc": obs["bnc"],
                "frq": obs["frq"],
                "zh": obs["zh"],
                "rarity_tier": rarity_tier,
                "rarity": rarity,
            }
        return {"word": w, "source": "custom", "zh": "", "rarity_tier": rarity_tier, "rarity": rarity}


# ==============================================================================
# Strict 5-Step Rule State Machine
# ==============================================================================


class GameSession:
    """Interactive and testable state machine enforcing all 6 game rules."""

    def __init__(self, level_data: dict[str, Any]) -> None:
        self.level_num: int = int(level_data.get("level_num", 2))
        self.rows: int = int(level_data["rows"])
        self.cols: int = int(level_data["cols"])
        self.cells: list[list[dict[str, Any]]] = []
        for r in range(self.rows):
            row_cells = []
            for c in range(self.cols):
                src = level_data["board"][r][c]
                is_void = bool(src.get("void", False))
                cnt = 0 if is_void else int(src.get("count", 0))
                letter = "" if (is_void or cnt <= 0) else str(src.get("letter", "")).upper()
                row_cells.append(
                    {
                        "r": r,
                        "c": c,
                        "void": is_void,
                        "letter": letter,
                        "initial_count": cnt,
                        "count": cnt,
                    }
                )
            self.cells.append(row_cells)

        self.target_words: list[str] = [str(w).upper() for w in level_data.get("target_words", [])]
        self.target_set: set[str] = set(self.target_words)
        target_lengths = {len(w) for w in self.target_words}

        # Extra words strictly restricted to lengths present in target_words (and not in targets)
        raw_extras = level_data.get("extra_words", [])
        self.extra_set: set[str] = set()
        for ew in raw_extras:
            w = ew["word"].upper() if isinstance(ew, dict) else str(ew).upper()
            if w not in self.target_set and len(w) in target_lengths:
                if self.level_num == 1 or len(w) >= 3:
                    self.extra_set.add(w)

        self.found_targets: list[str] = []
        self.found_target_set: set[str] = set()
        self.found_extras: list[str] = []
        self.found_extra_set: set[str] = set()
        self.cleared: bool = False
        self.history: list[dict[str, Any]] = []

    def step_drag_cell(self, current_path: list[tuple[int, int]], r: int, c: int) -> tuple[list[tuple[int, int]], str]:
        """Interactive drag helper supporting backtrack-to-undo-last-step."""
        if not (0 <= r < self.rows and 0 <= c < self.cols):
            return current_path, "OUT_OF_BOUNDS"
        cell = self.cells[r][c]
        if cell["void"] or cell["count"] <= 0 or not cell["letter"]:
            return current_path, "UNUSABLE_CELL"

        if not current_path:
            return [(r, c)], "STARTED"

        if len(current_path) >= 2 and (r, c) == current_path[-2]:
            return current_path[:-1], "UNDONE_LAST"

        if (r, c) in current_path:
            return current_path, "ALREADY_IN_PATH"

        pr, pc = current_path[-1]
        if abs(pr - r) + abs(pc - c) != 1:
            return current_path, "NOT_ORTHOGONAL"

        return current_path + [(r, c)], "EXTENDED"

    def validate_path(self, path: list[tuple[int, int]]) -> tuple[bool, str]:
        """Step 1: Verify path legality (bounds, active, orthogonal, no repeats)."""
        if not path or len(path) < 2:
            return False, "路径长度不足（至少需要2格）"
        seen: set[tuple[int, int]] = set()
        for idx, (r, c) in enumerate(path):
            if not (0 <= r < self.rows and 0 <= c < self.cols):
                return False, f"坐标越界: ({r}, {c})"
            cell = self.cells[r][c]
            if cell["void"]:
                return False, f"不能经过镂空格: ({r}, {c})"
            if cell["count"] <= 0 or not cell["letter"]:
                return False, f"格子 ({r}, {c}) 次数已耗尽并清空"
            if (r, c) in seen:
                return False, f"同一次连词不能重复使用同一格: ({r}, {c})"
            seen.add((r, c))
            if idx > 0:
                pr, pc = path[idx - 1]
                if abs(pr - r) + abs(pc - c) != 1:
                    return False, f"只能上下左右正交连线，禁止斜连或跳格: ({pr},{pc})->({r},{c})"
        return True, "路径合法"

    def check_level_cleared(self) -> bool:
        """Step 5: Both conditions must hold: all targets found AND all cells count == 0."""
        if len(self.found_target_set) != len(self.target_set):
            return False
        for row in self.cells:
            for cell in row:
                if cell["count"] != 0:
                    return False
        return True

    def submit_path(self, path: list[tuple[int, int]]) -> dict[str, Any]:
        """Execute strict 5-step submission pipeline:
        1. Check path legality
        2. Check already found
        3. Classify target / extra / invalid
        4. Update state
        5. Check level cleared
        """
        trace: list[dict[str, str]] = []

        # Step 1: validate path
        ok, msg = self.validate_path(path)
        trace.append({"step": "1. 路径合法性", "result": "PASS" if ok else "FAIL", "detail": msg})
        if not ok:
            return {
                "status": "INVALID_PATH",
                "word": "",
                "message": msg,
                "cleared": self.cleared,
                "trace": trace,
                "state": self.snapshot(),
            }

        word = "".join(self.cells[r][c]["letter"] for r, c in path)

        # Step 2: check already found
        if word in self.found_target_set or word in self.found_extra_set:
            msg2 = f"单词 {word} 已找到过，不重复计入且不扣减次数"
            trace.append({"step": "2. 重复提交检查", "result": "HIT", "detail": msg2})
            trace.append({"step": "3. 词表归类", "result": "SKIP", "detail": "已存在记录，跳过判定"})
            trace.append({"step": "4. 状态更新", "result": "NOOP", "detail": "棋盘与进度不变"})
            trace.append({"step": "5. 通关判定", "result": "CLEARED" if self.cleared else "PENDING", "detail": "未完成" if not self.cleared else "已通关"})
            return {
                "status": "ALREADY_FOUND",
                "word": word,
                "message": msg2,
                "cleared": self.cleared,
                "trace": trace,
                "state": self.snapshot(),
            }
        trace.append({"step": "2. 重复提交检查", "result": "PASS", "detail": f"{word} 尚未找到"})

        # Step 3: classify word
        if word in self.target_set:
            kind = "TARGET"
            trace.append({"step": "3. 词表归类", "result": "TARGET", "detail": f"{word} 是本关目标词"})
        elif word in self.extra_set:
            kind = "EXTRA"
            trace.append({"step": "3. 词表归类", "result": "EXTRA", "detail": f"{word} 是本关额外词"})
        else:
            msg3 = f"{word} 未收录在本关目标词表与额外词表中，视为无效词"
            trace.append({"step": "3. 词表归类", "result": "INVALID", "detail": msg3})
            trace.append({"step": "4. 状态更新", "result": "NOOP", "detail": "棋盘与进度不变"})
            trace.append({"step": "5. 通关判定", "result": "CLEARED" if self.cleared else "PENDING", "detail": "状态未变化"})
            return {
                "status": "INVALID_WORD",
                "word": word,
                "message": msg3,
                "cleared": self.cleared,
                "trace": trace,
                "state": self.snapshot(),
            }

        # Step 4: update state
        cleared_cells: list[tuple[int, int]] = []
        if kind == "TARGET":
            self.found_targets.append(word)
            self.found_target_set.add(word)
            for r, c in path:
                cell = self.cells[r][c]
                cell["count"] -= 1
                if cell["count"] == 0:
                    cell["letter"] = ""
                    cleared_cells.append((r, c))
            msg4 = f"目标词 {word} 完成！路径 {len(path)} 格次数各 -1" + (
                f"（清空格子: {cleared_cells}）" if cleared_cells else ""
            )
            trace.append({"step": "4. 状态更新", "result": "UPDATED", "detail": msg4})
        else:
            self.found_extras.append(word)
            self.found_extra_set.add(word)
            msg4 = f"额外词 {word} 已记录（不消耗格子次数，不增加目标词进度）"
            trace.append({"step": "4. 状态更新", "result": "UPDATED", "detail": msg4})

        # Step 5: check cleared
        self.cleared = self.check_level_cleared()
        remaining_cnt = sum(c["count"] for row in self.cells for c in row)
        if self.cleared:
            msg5 = "全部目标词已找到且所有格子次数已归零 —— 通关！"
        else:
            msg5 = f"尚余 {len(self.target_set) - len(self.found_target_set)} 个目标词，剩余总次数 {remaining_cnt}"
        trace.append({"step": "5. 通关判定", "result": "CLEARED" if self.cleared else "PENDING", "detail": msg5})

        self.history.append({"word": word, "status": kind, "path": [list(p) for p in path]})
        return {
            "status": kind,
            "word": word,
            "message": msg4,
            "cleared_cells": [list(p) for p in cleared_cells],
            "cleared": self.cleared,
            "trace": trace,
            "state": self.snapshot(),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "cols": self.cols,
            "board": copy.deepcopy(self.cells),
            "target_words": self.target_words,
            "found_targets": list(self.found_targets),
            "found_extras": list(self.found_extras),
            "cleared": self.cleared,
            "remaining_counts": sum(c["count"] for row in self.cells for c in row),
        }


# ==============================================================================
# Solver: Path Finding, State-Space Deadlock Detection, Extra-Word Scan
# ==============================================================================


class LevelSolver:
    """Analyzes solvability, multi-path deadlocks, and extra words for a level."""

    def __init__(self, gdict: GameDictionary) -> None:
        self.gdict = gdict

    @staticmethod
    def find_word_paths(
        letters: list[list[str]],
        counts: list[list[int]],
        void_mask: list[list[bool]],
        word: str,
        limit: int | None = None,
    ) -> list[list[tuple[int, int]]]:
        """Find all simple orthogonal paths spelling `word` on cells with count > 0."""
        rows = len(letters)
        cols = len(letters[0]) if rows else 0
        w_len = len(word)
        if w_len == 0:
            return []
        results: list[list[tuple[int, int]]] = []
        used = [[False] * cols for _ in range(rows)]
        path: list[tuple[int, int]] = []

        def dfs(r: int, c: int, idx: int) -> bool:
            if idx == w_len - 1:
                results.append(list(path))
                return limit is not None and len(results) >= limit
            nxt_ch = word[idx + 1]
            for nr, nc in _neighbors(r, c, rows, cols):
                if (
                    not used[nr][nc]
                    and not void_mask[nr][nc]
                    and counts[nr][nc] > 0
                    and letters[nr][nc] == nxt_ch
                ):
                    used[nr][nc] = True
                    path.append((nr, nc))
                    stop = dfs(nr, nc, idx + 1)
                    path.pop()
                    used[nr][nc] = False
                    if stop:
                        return True
            return False

        ch0 = word[0]
        for r in range(rows):
            for c in range(cols):
                if not void_mask[r][c] and counts[r][c] > 0 and letters[r][c] == ch0:
                    used[r][c] = True
                    path.append((r, c))
                    stop = dfs(r, c, 0)
                    path.pop()
                    used[r][c] = False
                    if stop:
                        return results
        return results

    def analyze_solvability(
        self,
        level_data: dict[str, Any],
        max_states: int = 40000,
    ) -> dict[str, Any]:
        """Verify rule compliance, conservation, multi-paths, and full-order deadlock freedom."""
        level_num = int(level_data.get("level_num", 2))
        rows = int(level_data["rows"])
        cols = int(level_data["cols"])
        board = level_data["board"]
        targets = [str(w).upper() for w in level_data.get("target_words", [])]

        letters = [[""] * cols for _ in range(rows)]
        init_counts = [[0] * cols for _ in range(rows)]
        void_mask = [[False] * cols for _ in range(rows)]
        active_cells = 0
        total_counts = 0

        for r in range(rows):
            for c in range(cols):
                cell = board[r][c]
                is_void = bool(cell.get("void", False))
                void_mask[r][c] = is_void
                if not is_void:
                    cnt = int(cell.get("count", 0))
                    ch = str(cell.get("letter", "")).upper()
                    init_counts[r][c] = cnt
                    letters[r][c] = ch if cnt > 0 else ""
                    if cnt > 0:
                        active_cells += 1
                        total_counts += cnt

        issues: list[str] = []
        if not targets:
            issues.append("目标词表为空")
        if len(set(targets)) != len(targets):
            issues.append("目标词表存在重复单词")
        if level_num >= 2:
            short_words = [w for w in targets if len(w) < 3]
            if short_words:
                issues.append(f"第 {level_num} 关存在少于3字母的目标词: {short_words}")

        target_char_sum = sum(len(w) for w in targets)
        if total_counts != target_char_sum:
            issues.append(
                f"次数守恒不匹配: 棋盘总次数={total_counts}，目标词总字母数={target_char_sum}"
            )

        # Find initial paths for every target word
        initial_paths: dict[str, list[list[tuple[int, int]]]] = {}
        zero_path_words: list[str] = []
        for w in targets:
            paths = self.find_word_paths(letters, init_counts, void_mask, w, limit=64)
            initial_paths[w] = paths
            if not paths:
                zero_path_words.append(w)

        if zero_path_words:
            issues.append(f"以下目标词在初始棋盘上无合法路径: {zero_path_words}")

        init_paths_map = level_data.get("initial_word_paths") or {}
        sel_paths_map = level_data.get("selected_candidate_paths") or {}
        target_details = []
        for w in targets:
            primary_p = init_paths_map.get(w) or sel_paths_map.get(w) or (initial_paths[w][0] if initial_paths[w] else [])
            dir_score, path_score = _score_path_dir_and_complexity(primary_p)
            meta = self.gdict.lookup_word_meta(w)
            target_details.append(
                {
                    "word": w,
                    "path_count": len(initial_paths[w]),
                    "sample_paths": [[list(p) for p in path] for path in initial_paths[w][:8]],
                    "dir_score": dir_score,
                    "path_score": path_score,
                    "diff_score": meta["rarity"] + path_score,
                    **meta,
                }
            )

        rarity_score_total = sum(int(td.get("rarity", 0)) for td in target_details)
        path_score_total = sum(int(td.get("path_score", 0)) for td in target_details)
        diff_score_total = sum(int(td.get("diff_score", 0)) for td in target_details)
        diff_score_avg = round(diff_score_total / len(target_details), 2) if target_details else 0.0
        diff_score_max = max((int(td.get("diff_score", 0)) for td in target_details), default=0)

        if issues or not targets:
            return {
                "safety_level": "UNSOLVABLE",
                "safety_label": "无解 / 配置异常",
                "is_solvable": False,
                "zero_deadlock": False,
                "path_unique": False,
                "issues": issues,
                "total_active_cells": active_cells,
                "total_counts": total_counts,
                "target_char_sum": target_char_sum,
                "avg_overlap": round(total_counts / active_cells, 2) if active_cells else 0.0,
                "rarity_score_total": rarity_score_total,
                "path_score_total": path_score_total,
                "diff_score_total": diff_score_total,
                "diff_score_avg": diff_score_avg,
                "diff_score_max": diff_score_max,
                "explored_states": 0,
                "truncated": False,
                "target_details": target_details,
                "winning_solution": [],
                "deadlock_witness": None,
            }

        # Fast-path mathematical check: if every word has exactly 1 path on initial board
        if all(len(initial_paths[w]) == 1 for w in targets):
            rem = [row[:] for row in init_counts]
            ok_sub = True
            for w in targets:
                for r, c in initial_paths[w][0]:
                    rem[r][c] -= 1
                    if rem[r][c] < 0:
                        ok_sub = False
                        break
            if ok_sub and all(rem[r][c] == 0 for r in range(rows) for c in range(cols)):
                winning_sol = [
                    {"word": w, "path": [list(p) for p in initial_paths[w][0]]} for w in targets
                ]
                return {
                    "safety_level": "PATH_UNIQUE_ZERO_DEADLOCK",
                    "safety_label": "路径唯一 · 绝对零死局",
                    "is_solvable": True,
                    "zero_deadlock": True,
                    "path_unique": True,
                    "issues": [],
                    "total_active_cells": active_cells,
                    "total_counts": total_counts,
                    "target_char_sum": target_char_sum,
                    "avg_overlap": round(total_counts / active_cells, 2) if active_cells else 0.0,
                    "rarity_score_total": rarity_score_total,
                    "path_score_total": path_score_total,
                    "diff_score_total": diff_score_total,
                    "diff_score_avg": diff_score_avg,
                    "diff_score_max": diff_score_max,
                    "explored_states": 1,
                    "truncated": False,
                    "target_details": target_details,
                    "winning_solution": winning_sol,
                    "deadlock_witness": None,
                }

        # Full state-space DFS with memoization over (found_mask, flat_counts_tuple)
        # First map active cells to a flat vector for compact state keys
        active_coords: list[tuple[int, int]] = []
        coord_to_idx: dict[tuple[int, int], int] = {}
        for r in range(rows):
            for c in range(cols):
                if init_counts[r][c] > 0:
                    coord_to_idx[(r, c)] = len(active_coords)
                    active_coords.append((r, c))

        n_targets = len(targets)
        all_mask = (1 << n_targets) - 1

        # Precompute flat path indices for all initial paths of each target word
        # Since cell counts only decrease, valid paths at any later state MUST be a subset of initial_paths!
        word_init_flat_paths: list[list[tuple[int, ...]]] = []
        for w in targets:
            fp_list = [tuple(coord_to_idx[pt] for pt in path) for path in initial_paths[w]]
            word_init_flat_paths.append(fp_list)

        init_vec = tuple(init_counts[r][c] for r, c in active_coords)
        memo_can_win: dict[tuple[int, tuple[int, ...]], bool] = {}
        winning_move: dict[tuple[int, tuple[int, ...]], tuple[int, int, tuple[int, tuple[int, ...]]]] = {}
        truncated = False
        deadlock_state_info: dict[str, Any] | None = None

        # Track parent move sequence via DFS stack for minimal deadlock witness reconstruction
        def solve_dfs(
            found_mask: int,
            vec: tuple[int, ...],
            history_steps: list[tuple[int, int]],
        ) -> bool:
            nonlocal truncated, deadlock_state_info
            state = (found_mask, vec)
            if state in memo_can_win:
                return memo_can_win[state]

            if found_mask == all_mask:
                won = all(v == 0 for v in vec)
                memo_can_win[state] = won
                return won

            if len(memo_can_win) >= max_states:
                truncated = True
                # Assume True when truncated to avoid false deadlock claim, track truncated flag
                return True

            # Quick prune: if ANY unfound word currently has 0 valid paths, state is definitely dead!
            avail_moves: list[tuple[int, int, tuple[int, ...]]] = []
            for wi in range(n_targets):
                if found_mask & (1 << wi):
                    continue
                word_has_move = False
                for pi, fp in enumerate(word_init_flat_paths[wi]):
                    if all(vec[ci] > 0 for ci in fp):
                        word_has_move = True
                        avail_moves.append((wi, pi, fp))
                if not word_has_move:
                    memo_can_win[state] = False
                    if deadlock_state_info is None:
                        deadlock_state_info = {
                            "stuck_word": targets[wi],
                            "steps": list(history_steps),
                            "reason": f"完成前序词后，目标词 {targets[wi]} 在棋盘上无合法路径可用",
                        }
                    return False

            any_win = False
            for wi, pi, fp in avail_moves:
                nxt_list = list(vec)
                for ci in fp:
                    nxt_list[ci] -= 1
                nxt_vec = tuple(nxt_list)
                nxt_mask = found_mask | (1 << wi)
                history_steps.append((wi, pi))
                child_wins = solve_dfs(nxt_mask, nxt_vec, history_steps)
                history_steps.pop()

                if child_wins:
                    if not any_win:
                        any_win = True
                        winning_move[state] = (wi, pi, (nxt_mask, nxt_vec))
                else:
                    # Wait: if we haven't recorded a specific branch where parent CAN win
                    # or reaches deadlock, record if needed
                    pass

            memo_can_win[state] = any_win
            return any_win

        is_solvable = solve_dfs(0, init_vec, [])

        # Check if any reachable state from init cannot win (deadlock risk!)
        deadlock_witness: dict[str, Any] | None = None
        if is_solvable and deadlock_state_info is not None:
            # Find shortest path from (0, init_vec) into ANY memo_can_win[state] == False
            q: deque[tuple[tuple[int, tuple[int, ...]], list[tuple[int, int]]]] = deque([((0, init_vec), [])])
            visited_bfs = {(0, init_vec)}
            found_witness = None
            while q:
                st, steps = q.popleft()
                fmask, vec = st
                if fmask == all_mask:
                    continue
                for wi in range(n_targets):
                    if fmask & (1 << wi):
                        continue
                    for pi, fp in enumerate(word_init_flat_paths[wi]):
                        if all(vec[ci] > 0 for ci in fp):
                            nxt_list = list(vec)
                            for ci in fp:
                                nxt_list[ci] -= 1
                            nxt_st = (fmask | (1 << wi), tuple(nxt_list))
                            nxt_steps = steps + [(wi, pi)]
                            if memo_can_win.get(nxt_st) is False:
                                # Identify which unfound word(s) became stuck or unsolvable
                                stuck = []
                                for wj in range(n_targets):
                                    if not (nxt_st[0] & (1 << wj)):
                                        if not any(all(nxt_st[1][ci] > 0 for ci in fp2) for fp2 in word_init_flat_paths[wj]):
                                            stuck.append(targets[wj])
                                reason = (
                                    f"按此路径操作后，目标词 {', '.join(stuck)} 在棋盘上再无合法路径"
                                    if stuck
                                    else "按此路径操作后进入死局状态，剩余路径组合无法耗尽全部格子"
                                )
                                found_witness = {
                                    "reason": reason,
                                    "stuck_word": stuck[0] if stuck else "",
                                    "steps": [
                                        {
                                            "word": targets[swi],
                                            "path": [list(p) for p in initial_paths[targets[swi]][spi]],
                                        }
                                        for swi, spi in nxt_steps
                                    ],
                                }
                                break
                            if nxt_st not in visited_bfs and nxt_st in memo_can_win:
                                visited_bfs.add(nxt_st)
                                q.append((nxt_st, nxt_steps))
                    if found_witness is not None:
                        break
                if found_witness is not None:
                    break
            deadlock_witness = found_witness or {
                "reason": deadlock_state_info["reason"],
                "stuck_word": deadlock_state_info["stuck_word"],
                "steps": [
                    {
                        "word": targets[wi],
                        "path": [list(p) for p in initial_paths[targets[wi]][pi]],
                    }
                    for wi, pi in deadlock_state_info["steps"]
                ],
            }

        winning_solution: list[dict[str, Any]] = []
        if is_solvable:
            cur = (0, init_vec)
            seen_walk = set()
            while cur in winning_move and cur not in seen_walk:
                seen_walk.add(cur)
                wi, pi, nxt = winning_move[cur]
                winning_solution.append(
                    {
                        "word": targets[wi],
                        "path": [list(p) for p in initial_paths[targets[wi]][pi]],
                    }
                )
                cur = nxt

        all_unique = all(len(initial_paths[w]) == 1 for w in targets)
        if not is_solvable:
            safety_level = "UNSOLVABLE"
            safety_label = "无解（所有路径组合均无法通关）"
            issues.append("无法在耗尽所有格子次数的情况下找到全部目标词")
        elif deadlock_witness is not None:
            safety_level = "HAS_DEADLOCK_RISK"
            safety_label = "有解 · 但存在误消死局风险"
        elif all_unique:
            safety_level = "PATH_UNIQUE_ZERO_DEADLOCK"
            safety_label = "路径唯一 · 绝对零死局"
        else:
            safety_level = "MULTI_PATH_ZERO_DEADLOCK"
            safety_label = "多路径兼容 · 全顺序零死局"

        return {
            "safety_level": safety_level,
            "safety_label": safety_label,
            "is_solvable": is_solvable,
            "zero_deadlock": is_solvable and deadlock_witness is None and not truncated,
            "path_unique": all_unique,
            "issues": issues,
            "total_active_cells": active_cells,
            "total_counts": total_counts,
            "target_char_sum": target_char_sum,
            "avg_overlap": round(total_counts / active_cells, 2) if active_cells else 0.0,
            "rarity_score_total": rarity_score_total,
            "path_score_total": path_score_total,
            "diff_score_total": diff_score_total,
            "diff_score_avg": diff_score_avg,
            "diff_score_max": diff_score_max,
            "explored_states": len(memo_can_win),
            "truncated": truncated,
            "target_details": target_details,
            "winning_solution": winning_solution,
            "deadlock_witness": deadlock_witness,
        }

    def scan_all_candidates(
        self,
        level_data: dict[str, Any],
        max_rank: int = 6000,
        allowed_bands: list[str] | None = None,
        allowed_spellings: list[str] | None = None,
        exclude_words: set[str] | None = None,
        allowed_lengths: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Stage 2: Traverse active board cells to discover all valid candidate words."""
        level_num = int(level_data.get("level_num", 2))
        rows = int(level_data["rows"])
        cols = int(level_data["cols"])
        board = level_data["board"]
        excl = exclude_words or set()

        if allowed_lengths is not None:
            valid_lengths = set(allowed_lengths)
        else:
            valid_lengths = set(range(2 if level_num == 1 else 3, 9))
        if not valid_lengths:
            return []
        max_len = max(valid_lengths)

        letters = [[""] * cols for _ in range(rows)]
        counts = [[0] * cols for _ in range(rows)]
        void_mask = [[False] * cols for _ in range(rows)]
        for r in range(rows):
            for c in range(cols):
                cell = board[r][c]
                void_mask[r][c] = bool(cell.get("void", False))
                if not void_mask[r][c]:
                    counts[r][c] = int(cell.get("count", 0))
                    letters[r][c] = str(cell.get("letter", "")).upper() if counts[r][c] > 0 else ""

        bands_set = set(allowed_bands) if allowed_bands else None
        spell_set = set(allowed_spellings) if allowed_spellings else None

        found_paths: dict[str, list[list[tuple[int, int]]]] = defaultdict(list)
        found_meta: dict[str, dict[str, Any]] = {}
        used = [[False] * cols for _ in range(rows)]
        cur_path: list[tuple[int, int]] = []

        def dfs(r: int, c: int, node: TrieNode, depth: int) -> None:
            if node.word is not None and depth in valid_lengths:
                w = node.word
                if w not in excl and (level_num == 1 or depth >= 3):
                    info = node.entry_info or {}
                    src = info.get("source", "")
                    eligible = False
                    if src == "inflection":
                        cat = "inflection"
                    elif src == "obscure_ecdict":
                        cat = "obscure_base"
                    elif src == "tutorial_2":
                        cat = "tutorial_2"
                        eligible = level_num == 1
                    elif src == "core":
                        if info.get("extra_only"):
                            cat = "inflection"
                            eligible = False
                        else:
                            rank = info.get("rank", 99999)
                            if rank <= max_rank:
                                cat = "unselected_common"
                                ok_band = bands_set is None or info.get("band") in bands_set
                                ok_sp = spell_set is None or info.get("spelling") in spell_set
                                eligible = ok_band and ok_sp
                            else:
                                cat = "obscure_base"
                    else:
                        cat = "obscure_base"

                    if w not in found_meta:
                        found_meta[w] = {**info, "category": cat, "eligible_for_target": eligible}
                    if len(found_paths[w]) < 12:
                        found_paths[w].append(list(cur_path))

            if depth >= max_len:
                return
            for nr, nc in _neighbors(r, c, rows, cols):
                if not used[nr][nc] and not void_mask[nr][nc] and counts[nr][nc] > 0:
                    ch = letters[nr][nc]
                    nxt = node.children.get(ch)
                    if nxt is not None:
                        used[nr][nc] = True
                        cur_path.append((nr, nc))
                        dfs(nr, nc, nxt, depth + 1)
                        cur_path.pop()
                        used[nr][nc] = False

        root = self.gdict.extra_trie
        for r in range(rows):
            for c in range(cols):
                if not void_mask[r][c] and counts[r][c] > 0:
                    ch = letters[r][c]
                    nxt = root.children.get(ch)
                    if nxt is not None:
                        used[r][c] = True
                        cur_path.append((r, c))
                        dfs(r, c, nxt, 1)
                        cur_path.pop()
                        used[r][c] = False

        cat_order = {"unselected_common": 0, "tutorial_2": 1, "inflection": 2, "obscure_base": 3}
        cat_labels = {
            "inflection": "词形变化（复数/时态等）",
            "obscure_base": "低频 / 生僻词",
            "unselected_common": "候选常用词",
            "tutorial_2": "两字母引导词",
        }

        out: list[dict[str, Any]] = []
        for w, paths in found_paths.items():
            meta = found_meta[w]
            out.append(
                {
                    "word": w,
                    "length": len(w),
                    "category": meta["category"],
                    "category_label": cat_labels.get(meta["category"], meta["category"]),
                    "eligible_for_target": meta["eligible_for_target"],
                    "zh": meta.get("zh", ""),
                    "tag_desc": meta.get("tag_desc", ""),
                    "rank": meta.get("rank"),
                    "band": meta.get("band"),
                    "rarity_tier": self.gdict.get_word_rarity_tier(w),
                    "rarity": self.gdict.get_word_rarity(w),
                    "path_count": len(paths),
                    "sample_paths": [[list(p) for p in path] for path in paths[:6]],
                }
            )
        out.sort(key=lambda x: (0 if x["eligible_for_target"] else 1, cat_order.get(x["category"], 9), -x["length"], x["word"]))
        return out

    def partition_candidates_and_solve(
        self,
        level_data: dict[str, Any],
        max_rank: int = 6000,
        extra_categories: list[str] | None = None,
        allowed_bands: list[str] | None = None,
        allowed_spellings: list[str] | None = None,
    ) -> dict[str, Any]:
        """Stage 4: Solve target_words and partition remaining candidates into extra_words & other_candidates."""
        level_num = int(level_data.get("level_num", 2))
        rows = int(level_data["rows"])
        cols = int(level_data["cols"])
        board = level_data["board"]

        init_words = [str(w).upper() for w in level_data.get("initial_words", [])]
        sel_cands = [str(w).upper() for w in level_data.get("selected_candidates", [])]
        if init_words or sel_cands:
            seen_t: set[str] = set()
            merged: list[str] = []
            for w in init_words + sel_cands:
                if w not in seen_t:
                    seen_t.add(w)
                    merged.append(w)
            level_data["target_words"] = merged
        else:
            merged = [str(w).upper() for w in level_data.get("target_words", [])]
            level_data["initial_words"] = list(merged)
            level_data["selected_candidates"] = []

        sol_report = self.analyze_solvability(level_data)
        target_set = set(merged)
        target_lengths = {len(w) for w in merged}

        # Tag each target_detail with origin ('initial' | 'candidate')
        init_set = set(level_data.get("initial_words", []))
        for td in sol_report.get("target_details", []):
            td["origin"] = "initial" if td["word"] in init_set else "candidate"

        all_cands = self.scan_all_candidates(
            level_data,
            max_rank=max_rank,
            allowed_bands=allowed_bands,
            allowed_spellings=allowed_spellings,
            exclude_words=target_set,
        )

        # Prepare counts & primary target cells for expiration trigger computation
        counts = [[0] * cols for _ in range(rows)]
        for r in range(rows):
            for c in range(cols):
                if not board[r][c].get("void", False):
                    counts[r][c] = int(board[r][c].get("count", 0))

        target_primary_cells: dict[str, set[tuple[int, int]]] = {}
        init_paths_map = level_data.get("initial_word_paths", {})
        sel_paths_map = level_data.get("selected_candidate_paths", {})
        for td in sol_report.get("target_details", []):
            w = td["word"]
            if w in init_paths_map:
                target_primary_cells[w] = {tuple(pt) for pt in init_paths_map[w]}
            elif w in sel_paths_map:
                target_primary_cells[w] = {tuple(pt) for pt in sel_paths_map[w]}
            elif td["sample_paths"]:
                target_primary_cells[w] = {tuple(pt) for pt in td["sample_paths"][0]}

        allowed_cats = set(extra_categories or ["inflection", "obscure_base", "tutorial_2"])
        if level_num == 1:
            allowed_cats.add("tutorial_2")

        extra_words: list[dict[str, Any]] = []
        other_candidates: list[dict[str, Any]] = []
        for cand in all_cands:
            w_len = cand["length"]
            paths = cand["sample_paths"]
            triggers: list[str] = []
            for tw, tw_cells in target_primary_cells.items():
                all_broken = True
                for path in paths:
                    if all((tuple(pt) not in tw_cells) or counts[pt[0]][pt[1]] > 1 for pt in path):
                        all_broken = False
                        break
                if all_broken:
                    triggers.append(tw)
            cand_item = {**cand, "expiration_triggers": triggers}
            if w_len in target_lengths and cand["category"] in allowed_cats and (level_num == 1 or w_len >= 3):
                extra_words.append(cand_item)
            else:
                other_candidates.append(cand_item)

        level_data["extra_words"] = extra_words
        level_data["other_candidates"] = other_candidates
        level_data["solver_report"] = sol_report
        return level_data

    def toggle_candidate(
        self,
        level_data: dict[str, Any],
        word: str,
        path_idx: int = 0,
        max_rank: int = 6000,
        extra_categories: list[str] | None = None,
    ) -> dict[str, Any]:
        """Promote a candidate word into target_words (+1 path counts) or demote it back (-1 path counts)."""
        lvl = copy.deepcopy(level_data)
        w = word.strip().upper()
        level_num = int(lvl.get("level_num", 2))
        if level_num >= 2 and len(w) < 3:
            raise ValueError(f"第 {level_num} 关不允许少于3个字母的目标词: {w}")

        init_words = [str(x).upper() for x in lvl.get("initial_words", [])]
        sel_cands = [str(x).upper() for x in lvl.get("selected_candidates", [])]
        sel_paths = dict(lvl.get("selected_candidate_paths", {}))
        board = lvl["board"]
        rows, cols = int(lvl["rows"]), int(lvl["cols"])

        if w in init_words:
            raise ValueError(f"{w} 是铺盘初始词，不可移出目标词")

        letters = [[""] * cols for _ in range(rows)]
        counts = [[0] * cols for _ in range(rows)]
        void_mask = [[False] * cols for _ in range(rows)]
        for r in range(rows):
            for c in range(cols):
                cell = board[r][c]
                void_mask[r][c] = bool(cell.get("void", False))
                if not void_mask[r][c]:
                    counts[r][c] = int(cell.get("count", 0))
                    letters[r][c] = str(cell.get("letter", "")).upper() if counts[r][c] > 0 else ""

        if w in sel_cands:
            # Demote back to candidate/extra
            chosen_path = sel_paths.get(w)
            if not chosen_path:
                ps = self.find_word_paths(letters, counts, void_mask, w, limit=1)
                if not ps:
                    raise ValueError(f"找不到 {w} 的已选路径")
                chosen_path = [list(pt) for pt in ps[0]]
            for r, c in chosen_path:
                if board[r][c]["count"] <= 1:
                    raise ValueError(f"移除 {w} 会使格子 ({r},{c}) 次数归零，无法移除")
            for r, c in chosen_path:
                board[r][c]["count"] -= 1
            sel_cands = [x for x in sel_cands if x != w]
            sel_paths.pop(w, None)
        else:
            # Promote candidate into target_words
            meta_w = self.gdict.core_by_word.get(w)
            if meta_w and meta_w.get("extra_only"):
                raise ValueError(f"{w} 标记为仅额外词（时态/词形/缩写词），不可晋升为目标词")
            ps = self.find_word_paths(letters, counts, void_mask, w, limit=12)
            if not ps:
                raise ValueError(f"棋盘上不存在 {w} 的合法路径")
            chosen_path = [list(pt) for pt in ps[path_idx % len(ps)]]
            for r, c in chosen_path:
                board[r][c]["count"] += 1
            sel_cands.append(w)
            sel_paths[w] = chosen_path

        lvl["selected_candidates"] = sel_cands
        lvl["selected_candidate_paths"] = sel_paths
        return self.partition_candidates_and_solve(
            lvl,
            max_rank=max_rank,
            extra_categories=extra_categories,
        )

    def scan_extra_words(
        self,
        level_data: dict[str, Any],
        max_rank: int = 6000,
        categories: list[str] | None = None,
        solvability_report: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper returning extra words (same lengths as target_words)."""
        targets = [str(w).upper() for w in level_data.get("target_words", [])]
        target_set = set(targets)
        target_lengths = {len(w) for w in targets}
        if not target_lengths:
            return []
        level_num = int(level_data.get("level_num", 2))
        allowed_cats = set(categories or ["inflection", "obscure_base", "tutorial_2"])
        if level_num == 1:
            allowed_cats.add("tutorial_2")

        cands = self.scan_all_candidates(
            level_data,
            max_rank=max_rank,
            exclude_words=target_set,
            allowed_lengths=target_lengths,
        )

        rows, cols = int(level_data["rows"]), int(level_data["cols"])
        board = level_data["board"]
        counts = [[0] * cols for _ in range(rows)]
        letters = [[""] * cols for _ in range(rows)]
        void_mask = [[False] * cols for _ in range(rows)]
        for r in range(rows):
            for c in range(cols):
                void_mask[r][c] = bool(board[r][c].get("void", False))
                if not void_mask[r][c]:
                    counts[r][c] = int(board[r][c].get("count", 0))
                    letters[r][c] = str(board[r][c].get("letter", "")).upper() if counts[r][c] > 0 else ""

        target_primary_cells: dict[str, set[tuple[int, int]]] = {}
        if solvability_report and solvability_report.get("target_details"):
            for td in solvability_report["target_details"]:
                if td["sample_paths"]:
                    target_primary_cells[td["word"]] = {tuple(pt) for pt in td["sample_paths"][0]}
        else:
            for tw in targets:
                ps = self.find_word_paths(letters, counts, void_mask, tw, limit=1)
                if ps:
                    target_primary_cells[tw] = set(ps[0])

        out: list[dict[str, Any]] = []
        for c in cands:
            if c["category"] not in allowed_cats:
                continue
            triggers: list[str] = []
            for tw, tw_cells in target_primary_cells.items():
                if all(any((tuple(pt) in tw_cells and counts[pt[0]][pt[1]] <= 1) for pt in path) for path in c["sample_paths"]):
                    triggers.append(tw)
            out.append({**c, "expiration_triggers": triggers})
        return out


# ==============================================================================
# Orientation Optimization (Forward Direction Alignment)
# ==============================================================================


def _score_path_dir_and_complexity(path: list[list[int]] | list[tuple[int, int]]) -> tuple[int, int]:
    """Return (dir_score, path_score) for a word path."""
    if not path or len(path) < 2:
        return 0, 0
    dr0 = path[1][0] - path[0][0]
    dc0 = path[1][1] - path[0][1]
    dir_total = 2 * (dr0 + dc0)
    start_score = 1 if (dr0 == -1 or dc0 == -1) else 0
    turns = 0
    reversals = 0
    prev_dr, prev_dc = dr0, dc0
    last_horiz = dc0 if dc0 != 0 else 0
    last_vert = dr0 if dr0 != 0 else 0

    for k in range(1, len(path) - 1):
        dr = path[k + 1][0] - path[k][0]
        dc = path[k + 1][1] - path[k][1]
        dir_total += dr + dc
        if dr != prev_dr or dc != prev_dc:
            turns += 1
        if dc != 0:
            if last_horiz != 0 and dc == -last_horiz:
                reversals += 1
            last_horiz = dc
        if dr != 0:
            if last_vert != 0 and dr == -last_vert:
                reversals += 1
            last_vert = dr
        prev_dr, prev_dc = dr, dc

    return dir_total, start_score + turns + reversals


def orient_level_forward(level_data: dict[str, Any], force_any_shape: bool = False) -> dict[str, Any]:
    """Pick the symmetry (id, flip_h, flip_v, rot180) preserving shape that maximizes forward direction score."""
    rows = int(level_data["rows"])
    cols = int(level_data["cols"])
    board = level_data["board"]
    targets = [str(w).upper() for w in level_data.get("target_words", [])]
    if not targets or rows <= 0 or cols <= 0:
        return level_data

    transforms = {
        "id": lambda r, c: (r, c),
        "flip_h": lambda r, c: (r, cols - 1 - c),
        "flip_v": lambda r, c: (rows - 1 - r, c),
        "rot180": lambda r, c: (rows - 1 - r, cols - 1 - c),
    }

    # Preserve void silhouette unless caller explicitly allows changing asymmetric shapes
    valid_names: list[str] = []
    for name, fn in transforms.items():
        if force_any_shape or name == "id":
            valid_names.append(name)
            continue
        ok = True
        for r in range(rows):
            for c in range(cols):
                nr, nc = fn(r, c)
                if bool(board[r][c].get("void", False)) != bool(board[nr][nc].get("void", False)):
                    ok = False
                    break
            if not ok:
                break
        if ok:
            valid_names.append(name)

    if len(valid_names) <= 1:
        return level_data

    # Collect primary path for each target word
    init_paths = level_data.get("initial_word_paths") or {}
    sel_paths = level_data.get("selected_candidate_paths") or {}
    td_paths: dict[str, list[list[int]]] = {}
    rep = level_data.get("solver_report") or {}
    for td in rep.get("target_details") or []:
        if td.get("word") and td.get("sample_paths"):
            td_paths[str(td["word"]).upper()] = td["sample_paths"][0]

    letters = [["" if board[r][c].get("void") else str(board[r][c].get("letter", "")).upper() for c in range(cols)] for r in range(rows)]
    counts = [[0 if board[r][c].get("void") else int(board[r][c].get("count", 0)) for c in range(cols)] for r in range(rows)]
    void_mask = [[bool(board[r][c].get("void", False)) for c in range(cols)] for r in range(rows)]

    primary_paths: list[list[list[int]]] = []
    for w in targets:
        p = init_paths.get(w) or sel_paths.get(w) or td_paths.get(w)
        if not p:
            found = LevelSolver.find_word_paths(letters, counts, void_mask, w, limit=1)
            if found:
                p = [[pt[0], pt[1]] for pt in found[0]]
        if p:
            primary_paths.append(p)

    if not primary_paths:
        return level_data

    best_name = "id"
    best_key: tuple[int, int, int] | None = None
    for name in valid_names:
        fn = transforms[name]
        dir_sum = 0
        path_sum = 0
        for p in primary_paths:
            tp = [fn(pt[0], pt[1]) for pt in p]
            ds, ps = _score_path_dir_and_complexity(tp)
            dir_sum += ds
            path_sum += ps
        key = (dir_sum, -path_sum, 1 if name == "id" else 0)
        if best_key is None or key > best_key:
            best_key = key
            best_name = name

    if best_name == "id":
        level_data["orientation_applied"] = "id"
        return level_data

    fn = transforms[best_name]

    def xform_path(p: list[Any]) -> list[list[int]]:
        return [[fn(pt[0], pt[1])[0], fn(pt[0], pt[1])[1]] for pt in p]

    new_board = [[None for _ in range(cols)] for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            nr, nc = fn(r, c)
            cell_copy = dict(board[r][c])
            cell_copy["r"] = nr
            cell_copy["c"] = nc
            new_board[nr][nc] = cell_copy
    level_data["board"] = new_board

    if "initial_word_paths" in level_data and level_data["initial_word_paths"]:
        level_data["initial_word_paths"] = {w: xform_path(p) for w, p in level_data["initial_word_paths"].items()}
    if "selected_candidate_paths" in level_data and level_data["selected_candidate_paths"]:
        level_data["selected_candidate_paths"] = {w: xform_path(p) for w, p in level_data["selected_candidate_paths"].items()}

    for key in ("extra_words", "other_candidates"):
        if key in level_data and isinstance(level_data[key], list):
            for item in level_data[key]:
                if isinstance(item, dict) and item.get("sample_paths"):
                    item["sample_paths"] = [xform_path(p) for p in item["sample_paths"]]

    if rep:
        new_init = level_data.get("initial_word_paths") or {}
        new_sel = level_data.get("selected_candidate_paths") or {}
        for td in rep.get("target_details") or []:
            if td.get("sample_paths"):
                td["sample_paths"] = [xform_path(p) for p in td["sample_paths"]]
            tw = str(td.get("word", "")).upper()
            tp = new_init.get(tw) or new_sel.get(tw) or (td["sample_paths"][0] if td.get("sample_paths") else [])
            ds, ps = _score_path_dir_and_complexity(tp)
            td["dir_score"] = ds
            td["path_score"] = ps
            if td.get("rarity") is not None:
                td["diff_score"] = td["rarity"] + ps
        if rep.get("winning_solution"):
            for step in rep["winning_solution"]:
                if step.get("path"):
                    step["path"] = xform_path(step["path"])
        if rep.get("deadlock_witness") and rep["deadlock_witness"].get("steps"):
            for step in rep["deadlock_witness"]["steps"]:
                if step.get("path"):
                    step["path"] = xform_path(step["path"])

    level_data["orientation_applied"] = best_name
    return level_data


# ==============================================================================
# Level Generator
# ==============================================================================


def build_void_template(
    rows: int,
    cols: int,
    template: str = "full",
    rng: random.Random | None = None,
) -> list[list[bool]]:
    """Create a 2D boolean void mask for common board silhouettes."""
    mask = [[False] * cols for _ in range(rows)]
    if template == "full":
        return mask
    if template == "corners" and rows >= 3 and cols >= 3:
        mask[0][0] = mask[0][cols - 1] = mask[rows - 1][0] = mask[rows - 1][cols - 1] = True
        return mask
    if template == "center_hole" and rows >= 3 and cols >= 3:
        mask[rows // 2][cols // 2] = True
        if rows % 2 == 0:
            mask[rows // 2 - 1][cols // 2] = True
        if cols % 2 == 0:
            mask[rows // 2][cols // 2 - 1] = True
        if rows % 2 == 0 and cols % 2 == 0:
            mask[rows // 2 - 1][cols // 2 - 1] = True
        return mask
    if template == "cross" and rows >= 3 and cols >= 3:
        # Four corner quadrants hollowed out to leave a cross/plus shape
        for r in range(rows):
            for c in range(cols):
                if (r in (0, rows - 1)) and (c in (0, cols - 1)):
                    mask[r][c] = True
        return mask
    if template == "ring" and rows >= 4 and cols >= 4:
        for r in range(1, rows - 1):
            for c in range(1, cols - 1):
                if rows >= 5 and cols >= 5 and 1 <= r <= rows - 2 and 1 <= c <= cols - 2:
                    if r == rows // 2 and c == cols // 2:
                        mask[r][c] = True
                else:
                    mask[r][c] = True
        # Ensure ring isn't too sparse on 4x4
        if rows == 4 and cols == 4:
            mask = [[False] * cols for _ in range(rows)]
            mask[1][1] = mask[2][2] = True
        return mask
    if template == "random":
        rgen = rng or random.Random()
        total = rows * cols
        holes = max(1, total // 6)
        coords = [(r, c) for r in range(rows) for c in range(cols)]
        rgen.shuffle(coords)
        for r, c in coords[:holes]:
            mask[r][c] = True
        return mask
    return mask


class LevelGenerator:
    """Backtracking path-embedding + bitmask-word-sampling generator with deadlock validation."""

    def __init__(self, gdict: GameDictionary) -> None:
        self.gdict = gdict
        self.solver = LevelSolver(gdict)
        self.target_usage: Counter[str] = Counter()
        self.extra_usage: Counter[str] = Counter()
        self.generated_count: int = 0

    def reset_usage_stats(self) -> None:
        """Reset rolling word usage counters used for frequency balancing and layout avoidance."""
        self.target_usage.clear()
        self.extra_usage.clear()
        self.generated_count = 0

    def _get_hot_extra_avoid_set(self, extra_avoid: set[str] | None = None) -> set[str]:
        avoid = set(extra_avoid or ())
        if self.generated_count >= 8 and self.extra_usage:
            most_common = self.extra_usage.most_common(30)
            avg = sum(self.extra_usage.values()) / max(1, len(self.extra_usage))
            thresh = max(2.0, avg * 1.5)
            for w, cnt in most_common:
                if cnt >= thresh:
                    avoid.add(w)
        return avoid

    @staticmethod
    def _sample_initial_lengths(
        level_num: int,
        non_void_total: int,
        fill_mode: str,
        max_cell_count: int,
        word_count: int | None,
        seed_lengths: list[int],
        rng: random.Random,
    ) -> list[int]:
        """Stage 1: Sample light-overlap initial words just enough to cover the board letters."""
        seed_sum = sum(seed_lengths)
        seed_n = len(seed_lengths)

        if level_num == 1:
            min_len, max_len = 2, min(4, non_void_total)
            wc = word_count if word_count is not None else rng.choice([2, 3, 3])
            min_chars = non_void_total if fill_mode == "full_cover" else 5
            max_chars = min(non_void_total * max_cell_count, max(min_chars + 2, int(non_void_total * 1.25)))
        else:
            min_len = 3
            if non_void_total <= 9:
                max_len = min(5, non_void_total)
                if non_void_total >= 6 and max_cell_count <= 2:
                    default_wc = 3
                elif non_void_total >= 6 and max_cell_count >= 5:
                    default_wc = rng.choice([3, 4, 5])
                elif non_void_total >= 6:
                    default_wc = rng.choice([3, 3, 4])
                else:
                    default_wc = 2
            elif non_void_total <= 16:
                max_len = min(6, non_void_total)
                default_wc = rng.choice([4, 4, 5]) if max_cell_count >= 5 else rng.choice([3, 4, 4])
            elif non_void_total <= 25:
                max_len = min(7, non_void_total)
                default_wc = rng.choice([4, 5, 5]) if max_cell_count >= 5 else rng.choice([4, 4, 5])
            else:
                max_len = min(8, non_void_total)
                default_wc = rng.choice([5, 5, 6])

            wc = word_count if word_count is not None else default_wc
            budget_floor = 12 if (non_void_total >= 6 and max_cell_count >= 2) else non_void_total
            if fill_mode == "full_cover":
                # Allow headroom so [3, 4, 5+] initial plans & higher word counts fit within char budget
                min_chars = non_void_total
                high_cap = max(budget_floor, non_void_total + 6, int(non_void_total * 1.75)) if max_cell_count >= 5 else max(budget_floor, non_void_total + 4, int(non_void_total * 1.55))
                max_chars = min(non_void_total * max_cell_count, high_cap)
            else:
                min_chars = max(5, int(non_void_total * 0.55))
                max_chars = min(non_void_total * max_cell_count, max(budget_floor, int(non_void_total * 1.35) + 1))

        wc = max(seed_n, wc)
        rem_n = wc - seed_n
        if rem_n == 0:
            return []

        while seed_sum + rem_n * max_len < min_chars and rem_n < 12:
            rem_n += 1
        while rem_n > 1 and seed_sum + rem_n * min_len > max_chars:
            rem_n -= 1

        chosen = [rng.randint(min_len, max_len) for _ in range(rem_n)]
        if level_num == 1:
            if 2 not in seed_lengths and 2 not in chosen:
                chosen[0] = 2
        else:
            # Ensure initial lengths cover >=5, ==4, and (80% of the time) ==3
            allow_no3 = non_void_total >= 9 and rng.random() < 0.20
            need_ge5 = non_void_total >= 5 and max_len >= 5 and not any(L >= 5 for L in seed_lengths)
            need_eq4 = non_void_total >= 4 and max_len >= 4 and not any(L == 4 for L in seed_lengths)
            need_eq3 = (not allow_no3) and not any(L == 3 for L in seed_lengths)

            fixed_indices: set[int] = set()
            free_slots = list(range(rem_n))
            rng.shuffle(free_slots)
            ge5_idx: int | None = None

            if need_ge5 and free_slots and seed_sum + 5 <= max_chars:
                idx = next((i for i in free_slots if chosen[i] >= 5), free_slots[0])
                free_slots.remove(idx)
                if chosen[idx] < 5:
                    chosen[idx] = 5
                ge5_idx = idx
                fixed_indices.add(idx)
            if need_eq4 and free_slots:
                idx = next((i for i in free_slots if chosen[i] == 4), free_slots[0])
                free_slots.remove(idx)
                chosen[idx] = 4
                fixed_indices.add(idx)
            if need_eq3 and free_slots:
                idx = next((i for i in free_slots if chosen[i] == 3), free_slots[0])
                free_slots.remove(idx)
                chosen[idx] = 3
                fixed_indices.add(idx)

            for _ in range(40):
                tot = seed_sum + sum(chosen)
                if min_chars <= tot <= max_chars:
                    break
                if tot < min_chars:
                    adjustable = [
                        i for i in range(rem_n)
                        if (i not in fixed_indices or i == ge5_idx) and chosen[i] < max_len
                    ]
                    if not adjustable:
                        adjustable = [i for i in range(rem_n) if chosen[i] < max_len]
                    if not adjustable:
                        break
                    chosen[rng.choice(adjustable)] += 1
                else:
                    adjustable = [i for i in range(rem_n) if i not in fixed_indices and chosen[i] > min_len]
                    if not adjustable:
                        adjustable = [
                            i for i in range(rem_n)
                            if (i != ge5_idx and chosen[i] > min_len) or (i == ge5_idx and chosen[i] > 5)
                        ]
                    if not adjustable:
                        adjustable = [i for i in range(rem_n) if chosen[i] > min_len]
                    if not adjustable:
                        break
                    chosen[rng.choice(adjustable)] -= 1

            while seed_sum + sum(chosen) < min_chars and len(chosen) + seed_n < 12:
                need = min_chars - (seed_sum + sum(chosen))
                chosen.append(min(max_len, max(min_len, need)))

            return chosen

        for _ in range(40):
            tot = seed_sum + sum(chosen)
            if min_chars <= tot <= max_chars:
                break
            idx = rng.randrange(rem_n)
            if tot < min_chars and chosen[idx] < max_len:
                chosen[idx] += 1
            elif tot > max_chars and chosen[idx] > min_len:
                chosen[idx] -= 1

        return chosen

    def _sample_candidate_paths(
        self,
        letters: list[list[str]],
        counts: list[list[int]],
        void_mask: list[list[bool]],
        length: int,
        max_cell_count: int,
        fixed_word: str | None,
        min_new_cov: int,
        prefer_uncovered: bool,
        rng: random.Random,
        max_paths: int = 36,
    ) -> list[tuple[int, int, list[tuple[int, int]]]]:
        """Sample scored candidate paths of exact `length` using Warnsdorff-guided backtracking."""
        rows = len(letters)
        cols = len(letters[0])
        active_cells = [
            (r, c)
            for r in range(rows)
            for c in range(cols)
            if not void_mask[r][c] and counts[r][c] < max_cell_count
        ]
        if len(active_cells) < length:
            return []

        has_existing = any(counts[r][c] > 0 for r in range(rows) for c in range(cols))
        candidates: list[tuple[int, int, list[tuple[int, int]]]] = []
        seen_paths: set[tuple[tuple[int, int], ...]] = set()

        # Precompute uncovered neighbor degree for Warnsdorff heuristic
        uncov_deg = [[0] * cols for _ in range(rows)]
        for r in range(rows):
            for c in range(cols):
                if not void_mask[r][c]:
                    uncov_deg[r][c] = sum(
                        1 for nr, nc in _neighbors(r, c, rows, cols)
                        if not void_mask[nr][nc] and counts[nr][nc] == 0
                    )

        starts = list(active_cells)
        rng.shuffle(starts)
        if fixed_word:
            starts = [
                (r, c)
                for (r, c) in starts
                if letters[r][c] == "" or letters[r][c] == fixed_word[0]
            ]
        if prefer_uncovered:
            # Prefer starting at uncovered corner/edge cells (low uncov_deg) or adjacent to covered cells
            starts.sort(
                key=lambda pt: (
                    0 if counts[pt[0]][pt[1]] == 0 else 1,
                    uncov_deg[pt[0]][pt[1]],
                )
            )

        attempts = 0
        max_attempts = 700 if prefer_uncovered else 300

        def walk(path: list[tuple[int, int]], used_set: set[tuple[int, int]], overlap: int, new_cov: int) -> None:
            nonlocal attempts
            if len(candidates) >= max_paths or attempts >= max_attempts:
                return
            idx = len(path)
            if new_cov + (length - idx) < min_new_cov:
                return
            if idx == length:
                tpath = tuple(path)
                if tpath not in seen_paths and new_cov >= min_new_cov:
                    seen_paths.add(tpath)
                    overlap_score = overlap * 3 if overlap < length else -10
                    if has_existing and overlap == 0:
                        overlap_score -= 4
                    score = overlap_score + new_cov * (4 if prefer_uncovered else 2) + rng.randint(0, 2)
                    candidates.append((score, new_cov, list(path)))
                return

            pr, pc = path[-1]
            nbrs = _neighbors(pr, pc, rows, cols)
            rng.shuffle(nbrs)
            if prefer_uncovered:
                nbrs.sort(
                    key=lambda pt: (
                        0 if counts[pt[0]][pt[1]] == 0 else 1,
                        uncov_deg[pt[0]][pt[1]],
                    )
                )
            for nr, nc in nbrs:
                if (nr, nc) in used_set or void_mask[nr][nc] or counts[nr][nc] >= max_cell_count:
                    continue
                ch = letters[nr][nc]
                if fixed_word and ch != "" and ch != fixed_word[idx]:
                    continue
                attempts += 1
                if attempts > max_attempts:
                    return
                used_set.add((nr, nc))
                path.append((nr, nc))
                is_ov = 1 if ch != "" else 0
                is_new = 1 if counts[nr][nc] == 0 else 0
                walk(path, used_set, overlap + is_ov, new_cov + is_new)
                path.pop()
                used_set.remove((nr, nc))
                if len(candidates) >= max_paths:
                    return

        for sr, sc in starts:
            if len(candidates) >= max_paths or attempts >= max_attempts:
                break
            ch0 = letters[sr][sc]
            is_ov0 = 1 if ch0 != "" else 0
            is_new0 = 1 if counts[sr][sc] == 0 else 0
            walk([(sr, sc)], {(sr, sc)}, is_ov0, is_new0)

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates

    def _record_usage(self, level: dict[str, Any]) -> None:
        self.generated_count += 1
        for w in level.get("target_words", []):
            self.target_usage[w] += 1
        for ex in level.get("extra_words", []):
            self.extra_usage[ex["word"]] += 1

    def generate_level(
        self,
        level_num: int = 2,
        rows: int = 4,
        cols: int = 4,
        template: str = "full",
        void_mask: list[list[bool]] | None = None,
        fill_mode: str = "auto_void",
        word_count: int | None = None,
        word_lengths: list[int] | None = None,
        max_rank: int = 6000,
        allowed_bands: list[str] | None = None,
        allowed_spellings: list[str] | None = None,
        seed_words: list[str] | None = None,
        max_cell_count: int = 10,
        strictness: str = "deadlock_free",
        extra_categories: list[str] | None = None,
        avoid_words: set[str] | None = None,
        rng_seed: int | None = None,
        timeout_sec: float = 4.0,
        auto_forward: bool = True,
    ) -> dict[str, Any]:
        """Generate a verified puzzle level satisfying strictness & coverage rules."""
        t0 = time.perf_counter()
        rng = random.Random(rng_seed)
        hot_avoid = self._get_hot_extra_avoid_set(avoid_words)

        # Clean seed words
        cleaned_seeds: list[str] = []
        for sw in seed_words or []:
            w = sw.strip().upper()
            if w and w.isalpha():
                if level_num >= 2 and len(w) < 3:
                    raise ValueError(f"第 {level_num} 关不允许少于3个字母的目标词: {w}")
                if level_num == 1 and len(w) == 2 and w not in self.gdict.tutorial_by_word:
                    raise ValueError(f"第 1 关两字母引导词必须在引导词表内: {w}")
                if w not in cleaned_seeds:
                    cleaned_seeds.append(w)

        explicit_lengths = [int(x) for x in word_lengths] if word_lengths else None
        if explicit_lengths and level_num >= 2:
            for L in explicit_lengths:
                if L < 3:
                    raise ValueError("第 2 关起目标词长度必须至少为 3 个字母")

        seed_lengths = [len(sw) for sw in cleaned_seeds]
        last_lengths_plan: list[int] = explicit_lengths or []
        best_fallback: tuple[tuple[int, int], dict[str, Any]] | None = None

        def check_strict(rep: dict[str, Any]) -> bool:
            if strictness == "path_unique":
                return rep["safety_level"] == "PATH_UNIQUE_ZERO_DEADLOCK"
            if strictness == "deadlock_free":
                return bool(rep["zero_deadlock"])
            return bool(rep["is_solvable"])

        for restart in range(28):
            if time.perf_counter() - t0 > timeout_sec:
                break
            base_void = (
                copy.deepcopy(void_mask)
                if void_mask is not None
                else build_void_template(rows, cols, template, rng)
            )
            non_void_total = sum(1 for r in range(rows) for c in range(cols) if not base_void[r][c])

            if explicit_lengths is not None:
                lengths_plan = list(explicit_lengths)
                slots: list[tuple[str | None, int]] = []
                remaining_lengths = list(lengths_plan)
                for sw in cleaned_seeds:
                    slots.append((sw, len(sw)))
                    if len(sw) in remaining_lengths:
                        remaining_lengths.remove(len(sw))
                for L in sorted(remaining_lengths, reverse=True):
                    slots.append((None, L))
                if word_count is not None and len(slots) > word_count and len(cleaned_seeds) <= word_count:
                    slots = slots[:word_count]
            else:
                extra_lengths = self._sample_initial_lengths(
                    level_num=level_num,
                    non_void_total=non_void_total,
                    fill_mode=fill_mode,
                    max_cell_count=max_cell_count,
                    word_count=None,
                    seed_lengths=seed_lengths,
                    rng=rng,
                )
                slots = [(sw, len(sw)) for sw in cleaned_seeds]
                for L in sorted(extra_lengths, reverse=True):
                    slots.append((None, L))
                lengths_plan = [L for _, L in slots]

            last_lengths_plan = lengths_plan
            total_init_chars = sum(L for _, L in slots)
            if fill_mode == "full_cover" and total_init_chars < non_void_total:
                raise ValueError(
                    f"满铺模式需要目标词总字母数 ({total_init_chars}) >= 非镂空有效格数 ({non_void_total})"
                )
            if total_init_chars > non_void_total * max_cell_count:
                raise ValueError(
                    f"目标词总字母数 ({total_init_chars}) 超过棋盘最大承载次数 ({non_void_total * max_cell_count})"
                )

            letters = [[""] * cols for _ in range(rows)]
            counts = [[0] * cols for _ in range(rows)]
            chosen_words: list[str] = []
            chosen_paths: list[list[tuple[int, int]]] = []
            used_word_set: set[str] = set()

            suffix_chars = [0] * (len(slots) + 1)
            for i in range(len(slots) - 1, -1, -1):
                suffix_chars[i] = suffix_chars[i + 1] + slots[i][1]

            node_budget = [0]

            def place_word(slot_idx: int, uncovered_left: int) -> bool:
                if time.perf_counter() - t0 > timeout_sec or node_budget[0] > 180:
                    return False
                node_budget[0] += 1

                if slot_idx == len(slots):
                    if fill_mode == "full_cover" and uncovered_left > 0:
                        return False
                    return True

                fixed_w, L = slots[slot_idx]
                rem_chars_after = suffix_chars[slot_idx + 1]
                min_new_cov = max(0, uncovered_left - rem_chars_after) if fill_mode == "full_cover" else 0
                prefer_uncovered = fill_mode == "full_cover" and uncovered_left > 0

                cand_paths = self._sample_candidate_paths(
                    letters,
                    counts,
                    base_void,
                    L,
                    max_cell_count,
                    fixed_w,
                    min_new_cov,
                    prefer_uncovered,
                    rng,
                    max_paths=32 if slot_idx > 0 else 16,
                )

                # Rank paths by word-pool richness to avoid 1-word bottleneck islands (like _ _ I -> SKI)
                if fixed_w is None and slot_idx > 0:
                    scored_paths: list[tuple[int, int, int, list[tuple[int, int]]]] = []
                    for base_sc, new_cov, path in cand_paths:
                        pat = [letters[r][c] for r, c in path]
                        if any(ch != "" for ch in pat):
                            mcnt = self.gdict.count_matching_words(
                                pat,
                                level_num=level_num,
                                max_rank=max_rank,
                                allowed_bands=allowed_bands,
                                allowed_spellings=allowed_spellings,
                            )
                            if mcnt == 0:
                                continue
                            rich_tier = 1 if mcnt >= 5 else 0
                        else:
                            rich_tier = 1
                        scored_paths.append((rich_tier, base_sc, new_cov, path))
                    scored_paths.sort(key=lambda x: (x[0], x[1]), reverse=True)
                    ordered_paths = [(nc, p) for _, _, nc, p in scored_paths]
                else:
                    ordered_paths = [(nc, p) for _, nc, p in cand_paths]

                for new_cov, path in ordered_paths:
                    if fixed_w is not None:
                        word_cands = [fixed_w] if fixed_w not in used_word_set else []
                    else:
                        pattern = [letters[r][c] for r, c in path]
                        word_cands = self.gdict.match_pattern(
                            pattern,
                            level_num=level_num,
                            max_rank=max_rank,
                            allowed_bands=allowed_bands,
                            allowed_spellings=allowed_spellings,
                            exclude_words=used_word_set,
                            rng=rng,
                            sample_limit=16,
                            usage_counts=self.target_usage,
                        )
                    if not word_cands:
                        continue

                    for w in word_cands[:8]:
                        saved: list[tuple[int, int, str, int]] = []
                        for pos_i, (r, c) in enumerate(path):
                            saved.append((r, c, letters[r][c], counts[r][c]))
                            letters[r][c] = w[pos_i]
                            counts[r][c] += 1

                        ok_inc = True
                        if strictness == "path_unique":
                            if len(self.solver.find_word_paths(letters, counts, base_void, w, limit=2)) != 1:
                                ok_inc = False
                            else:
                                for prev_w in chosen_words:
                                    if len(self.solver.find_word_paths(letters, counts, base_void, prev_w, limit=2)) != 1:
                                        ok_inc = False
                                        break

                        if ok_inc:
                            chosen_words.append(w)
                            chosen_paths.append(path)
                            used_word_set.add(w)
                            if place_word(slot_idx + 1, uncovered_left - new_cov):
                                return True
                            used_word_set.remove(w)
                            chosen_paths.pop()
                            chosen_words.pop()

                        for r, c, prev_ch, prev_cnt in saved:
                            letters[r][c] = prev_ch
                            counts[r][c] = prev_cnt

                return False

            if not place_word(0, non_void_total):
                continue

            # Stage 1 finalized board
            final_board: list[list[dict[str, Any]]] = []
            for r in range(rows):
                row_out = []
                for c in range(cols):
                    if base_void[r][c] or (fill_mode == "auto_void" and counts[r][c] == 0):
                        row_out.append({"r": r, "c": c, "void": True, "letter": "", "count": 0})
                    else:
                        row_out.append(
                            {
                                "r": r,
                                "c": c,
                                "void": False,
                                "letter": letters[r][c],
                                "count": counts[r][c],
                            }
                        )
                final_board.append(row_out)

            init_words = list(chosen_words)
            init_paths_map = {w: [list(pt) for pt in p] for w, p in zip(chosen_words, chosen_paths)}

            level_candidate = {
                "level_num": level_num,
                "rows": rows,
                "cols": cols,
                "template": template,
                "fill_mode": fill_mode,
                "board": final_board,
                "initial_words": init_words,
                "initial_word_paths": init_paths_map,
                "selected_candidates": [],
                "selected_candidate_paths": {},
                "target_words": list(init_words),
            }

            base_report = self.solver.analyze_solvability(level_candidate)
            if not check_strict(base_report):
                continue

            # Stage 2: Scan all candidate words on the fixed letter board
            all_cands = self.solver.scan_all_candidates(
                level_candidate,
                max_rank=max_rank,
                allowed_bands=allowed_bands,
                allowed_spellings=allowed_spellings,
                exclude_words=set(init_words),
            )

            # Stage 3: Select eligible candidates (preferring 4+ letter, lowest usage, unique-path)
            selected_cands: list[str] = []
            selected_paths_map: dict[str, list[list[int]]] = {}
            if explicit_lengths is None and max_cell_count >= 2:
                promotable = [c for c in all_cands if c["eligible_for_target"]]
                if strictness == "path_unique":
                    promotable = [c for c in promotable if c["path_count"] == 1]

                # Shuffle first for tie-breaking, then fill missing length buckets before general promotion
                rng.shuffle(promotable)
                base_sort_key = lambda c: (
                    0 if c["path_count"] == 1 else 1,
                    0 if c["length"] >= 4 else 1,
                    self.target_usage.get(c["word"], 0),
                    -c["length"],
                )
                promotable.sort(key=base_sort_key)

                has_init_5p = any(len(w) >= 5 for w in init_words)
                has_init_4 = any(len(w) == 4 for w in init_words)
                has_init_3 = any(len(w) == 3 for w in init_words)
                priority_cands: list[dict[str, Any]] = []
                used_in_prio: set[str] = set()
                if not has_init_5p:
                    c5 = next((c for c in promotable if c["length"] >= 5), None)
                    if c5:
                        priority_cands.append(c5)
                        used_in_prio.add(c5["word"])
                if not has_init_4:
                    c4 = next((c for c in promotable if c["length"] == 4 and c["word"] not in used_in_prio), None)
                    if c4:
                        priority_cands.append(c4)
                        used_in_prio.add(c4["word"])
                if not has_init_3 and not (non_void_total >= 9 and rng.random() < 0.20):
                    c3 = next((c for c in promotable if c["length"] == 3 and c["word"] not in used_in_prio), None)
                    if c3:
                        priority_cands.append(c3)
                        used_in_prio.add(c3["word"])
                ordered_promotable = priority_cands + [c for c in promotable if c["word"] not in used_in_prio]

                # Promote ALL eligible target-pool candidates (unless word_count explicitly caps total)
                desired_promote = len(ordered_promotable) if word_count is None else max(0, word_count - len(init_words))

                for cand in ordered_promotable:
                    if len(selected_cands) >= desired_promote:
                        break
                    cw = cand["word"]
                    if cw in init_words or cw in selected_cands:
                        continue
                    chosen_p = None
                    for p in cand["sample_paths"]:
                        if all(final_board[r][c]["count"] + 1 <= max_cell_count for r, c in p):
                            chosen_p = p
                            break
                    if chosen_p is None:
                        continue

                    for r, c in chosen_p:
                        final_board[r][c]["count"] += 1
                    selected_cands.append(cw)
                    selected_paths_map[cw] = chosen_p
                    level_candidate["selected_candidates"] = list(selected_cands)
                    level_candidate["selected_candidate_paths"] = dict(selected_paths_map)
                    level_candidate["target_words"] = init_words + selected_cands

                    rep_try = self.solver.analyze_solvability(level_candidate)
                    if not check_strict(rep_try):
                        for r, c in chosen_p:
                            final_board[r][c]["count"] -= 1
                        selected_cands.pop()
                        selected_paths_map.pop(cw, None)
                        level_candidate["selected_candidates"] = list(selected_cands)
                        level_candidate["selected_candidate_paths"] = dict(selected_paths_map)
                        level_candidate["target_words"] = init_words + selected_cands

            # Stage 4: Partition remaining candidates into extra_words & other_candidates + final solve
            self.solver.partition_candidates_and_solve(
                level_candidate,
                max_rank=max_rank,
                extra_categories=extra_categories,
                allowed_bands=allowed_bands,
                allowed_spellings=allowed_spellings,
            )
            level_candidate["generation_ms"] = round((time.perf_counter() - t0) * 1000, 1)

            # Layout avoidance check: avoid boards that form hot/over-repeated extra words
            collision_cnt = sum(1 for ex in level_candidate["extra_words"] if ex["word"] in hot_avoid)
            final_t = level_candidate["target_words"]
            missing_req_len = 0
            if level_num >= 2 and non_void_total >= 5:
                if not any(len(w) >= 5 for w in final_t):
                    missing_req_len += 1
                if non_void_total >= 4 and not any(len(w) == 4 for w in final_t):
                    missing_req_len += 1
            too_few_words_penalty = (
                1 if (word_count is None and explicit_lengths is None and max_cell_count >= 3 and len(final_t) <= 4) else 0
            )
            need_promote_penalty = (
                1 if (explicit_lengths is None and max_cell_count >= 2 and len(selected_cands) == 0) else 0
            )
            quality_key = (missing_req_len, too_few_words_penalty, collision_cnt, need_promote_penalty)

            if quality_key != (0, 0, 0, 0) and restart < 10:
                if best_fallback is None or quality_key < best_fallback[0]:
                    best_fallback = (quality_key, level_candidate)
                continue

            if auto_forward:
                orient_level_forward(level_candidate)
            self._record_usage(level_candidate)
            return level_candidate

        if best_fallback is not None:
            chosen_level = best_fallback[1]
            if auto_forward:
                orient_level_forward(chosen_level)
            self._record_usage(chosen_level)
            return chosen_level

        raise RuntimeError(
            f"在 {timeout_sec}s 内未能生成满足约束 ({strictness}, {rows}x{cols}, {fill_mode}, lengths={last_lengths_plan}) 的关卡，请尝试放宽模板或减少约束"
        )


# ==============================================================================
# Unit Tests & CLI
# ==============================================================================


def run_tests() -> None:
    print("Loading dictionary...")
    gdict = GameDictionary()
    solver = LevelSolver(gdict)
    gen = LevelGenerator(gdict)

    # 1. Test 5-step rule state machine strictly
    print("[Test 1] Strict 5-step rule engine & edge cases...")
    # Board 3x3:
    # B(1) O(1) O(1)
    # A(1) [VOID] K(1)
    # T(1) [C(0)=cleared] S(0)
    # Let's make a clean level: targets = ["BOOK", "CAT"], plus extra "AT" (level 1!) or "BOA" (level 2!)
    # Wait: level 2: targets = ["BOOK", "ACT"] (len 4, len 3)
    # Row 0: B(1) O(1) O(1) K(1)
    # Row 1: A(1) C(1) T(1) [VOID]
    # Note: B(0,0)->O(0,1)->O(0,2)->K(0,3) = BOOK
    #       A(1,0)->C(1,1)->T(1,2) = ACT
    # Notice B(0,0)-O(0,1)-A(1,0) = BOA (3 letters, len in {3,4}) -> valid extra word!
    test_level = {
        "level_num": 2,
        "rows": 2,
        "cols": 4,
        "board": [
            [
                {"r": 0, "c": 0, "void": False, "letter": "B", "count": 1},
                {"r": 0, "c": 1, "void": False, "letter": "O", "count": 1},
                {"r": 0, "c": 2, "void": False, "letter": "O", "count": 1},
                {"r": 0, "c": 3, "void": False, "letter": "K", "count": 1},
            ],
            [
                {"r": 1, "c": 0, "void": False, "letter": "A", "count": 1},
                {"r": 1, "c": 1, "void": False, "letter": "C", "count": 1},
                {"r": 1, "c": 2, "void": False, "letter": "T", "count": 1},
                {"r": 1, "c": 3, "void": True, "letter": "", "count": 0},
            ],
        ],
        "target_words": ["BOOK", "ACT"],
        "extra_words": ["CAB", "AT", "BOAT"],  # AT (len 2) excluded in level 2; CAB is valid orthogonal extra
    }
    sess = GameSession(test_level)
    assert "AT" not in sess.extra_set, "2-letter extra word should be excluded in level 2"
    assert "CAB" in sess.extra_set

    # Test backtrack-to-undo step
    p = []
    p, ev = sess.step_drag_cell(p, 0, 0)
    assert p == [(0, 0)] and ev == "STARTED"
    p, ev = sess.step_drag_cell(p, 0, 1)
    assert p == [(0, 0), (0, 1)] and ev == "EXTENDED"
    p, ev = sess.step_drag_cell(p, 0, 0)
    assert p == [(0, 0)] and ev == "UNDONE_LAST"

    # Diagonal forbidden: (0,0) -> (1,1)
    r_diag = sess.submit_path([(0, 0), (1, 1), (1, 2)])
    assert r_diag["status"] == "INVALID_PATH"

    # Void forbidden: (1,2) -> (1,3)
    r_void = sess.submit_path([(1, 1), (1, 2), (1, 3)])
    assert r_void["status"] == "INVALID_PATH"

    # Reuse same cell forbidden: B(0,0)->O(0,1)->B(0,0)
    r_reuse = sess.submit_path([(0, 0), (0, 1), (0, 0)])
    assert r_reuse["status"] == "INVALID_PATH"

    # Real word NOT in target/extra lists rejected without changing board: B(0,0)->A(1,0)->C(1,1) = BAC
    r_unlisted = sess.submit_path([(0, 0), (1, 0), (1, 1)])
    assert r_unlisted["status"] == "INVALID_WORD"
    assert sess.cells[0][0]["count"] == 1

    # Extra word CAB (C(1,1)->A(1,0)->B(0,0)) does NOT consume count!
    r_extra = sess.submit_path([(1, 1), (1, 0), (0, 0)])
    assert r_extra["status"] == "EXTRA"
    assert sess.cells[0][0]["count"] == 1
    assert len(sess.found_targets) == 0

    # Duplicate extra word CAB -> ALREADY_FOUND
    r_extra2 = sess.submit_path([(1, 1), (1, 0), (0, 0)])
    assert r_extra2["status"] == "ALREADY_FOUND"

    # Target word BOOK -> clears B, O, O, K!
    r_t1 = sess.submit_path([(0, 0), (0, 1), (0, 2), (0, 3)])
    assert r_t1["status"] == "TARGET"
    assert sess.cells[0][0]["count"] == 0 and sess.cells[0][0]["letter"] == ""
    assert not r_t1["cleared"]

    # Repeat BOOK fails on cleared cells (Step 1)
    r_rep = sess.submit_path([(0, 0), (0, 1), (0, 2), (0, 3)])
    assert r_rep["status"] == "INVALID_PATH"

    # Target word ACT -> clears A, C, T -> level cleared!
    r_t2 = sess.submit_path([(1, 0), (1, 1), (1, 2)])
    assert r_t2["status"] == "TARGET"
    assert r_t2["cleared"] is True
    print("  [PASS] 5-step rule engine verified.")

    # 2. Test Solver Multi-Path Zero-Deadlock AND Deadlock Witness detection!
    print("[Test 2] Solver multi-path zero-deadlock & deadlock risk detection...")
    multi_safe_level = {
        "level_num": 2,
        "rows": 3,
        "cols": 3,
        "board": [
            [
                {"r": 0, "c": 0, "void": False, "letter": "C", "count": 1},
                {"r": 0, "c": 1, "void": False, "letter": "A", "count": 1},
                {"r": 0, "c": 2, "void": False, "letter": "T", "count": 1},
            ],
            [
                {"r": 1, "c": 0, "void": True, "letter": "", "count": 0},
                {"r": 1, "c": 1, "void": False, "letter": "T", "count": 1},
                {"r": 1, "c": 2, "void": False, "letter": "A", "count": 1},
            ],
            [
                {"r": 2, "c": 0, "void": True, "letter": "", "count": 0},
                {"r": 2, "c": 1, "void": True, "letter": "", "count": 0},
                {"r": 2, "c": 2, "void": False, "letter": "R", "count": 1},
            ],
        ],
        "target_words": ["CAT", "RAT"],
    }
    rep_ms = solver.analyze_solvability(multi_safe_level)
    assert rep_ms["safety_level"] == "MULTI_PATH_ZERO_DEADLOCK", f"Got {rep_ms}"
    print("  [PASS] Multi-path zero-deadlock verified.")

    # Intentional deadlock-risk level:
    # CAT can go C(0,0)->A(0,1)->T(0,2) [leaves A(2,1)->T(1,1)->E(1,2) = ATE]
    # OR C(0,0)->A(0,1)->T(1,1) [DEADLOCK! ATE loses T(1,1) so A(2,1) is isolated]
    deadlock_level = {
        "level_num": 2,
        "rows": 3,
        "cols": 3,
        "board": [
            [
                {"r": 0, "c": 0, "void": False, "letter": "C", "count": 1},
                {"r": 0, "c": 1, "void": False, "letter": "A", "count": 1},
                {"r": 0, "c": 2, "void": False, "letter": "T", "count": 1},
            ],
            [
                {"r": 1, "c": 0, "void": True, "letter": "", "count": 0},
                {"r": 1, "c": 1, "void": False, "letter": "T", "count": 1},
                {"r": 1, "c": 2, "void": False, "letter": "E", "count": 1},
            ],
            [
                {"r": 2, "c": 0, "void": True, "letter": "", "count": 0},
                {"r": 2, "c": 1, "void": False, "letter": "A", "count": 1},
                {"r": 2, "c": 2, "void": True, "letter": "", "count": 0},
            ],
        ],
        "target_words": ["CAT", "ATE"],
    }
    rep_dl = solver.analyze_solvability(deadlock_level)
    assert rep_dl["is_solvable"] is True
    assert rep_dl["safety_level"] == "HAS_DEADLOCK_RISK", f"Got {rep_dl}"
    assert rep_dl["deadlock_witness"] is not None
    print("  [PASS] Detected deadlock witness:", rep_dl["deadlock_witness"]["reason"])

    # 3. Test Generator for Level 1 (2-letter guide words) and Levels 2..5 (3x3, 4x4, 5x5)
    print("[Test 3] Generator Level 1 (2-letter guide) & 3x3 / 4x4 / 5x5 zero-deadlock...")
    lvl1 = gen.generate_level(
        level_num=1, rows=3, cols=3, template="full", fill_mode="auto_void",
        word_lengths=[2, 2, 3], strictness="deadlock_free", rng_seed=42
    )
    assert lvl1["solver_report"]["zero_deadlock"] is True
    print(f"  [PASS] Level 1 generated in {lvl1['generation_ms']}ms: targets={lvl1['target_words']}, extras={len(lvl1['extra_words'])}")

    lvl_3x3 = gen.generate_level(
        level_num=2, rows=3, cols=3, template="full", fill_mode="full_cover",
        word_lengths=[3, 4, 4], strictness="path_unique", rng_seed=7
    )
    assert lvl_3x3["solver_report"]["safety_level"] == "PATH_UNIQUE_ZERO_DEADLOCK"
    print(f"  [PASS] Level 2 (3x3 full_cover path_unique) in {lvl_3x3['generation_ms']}ms: {lvl_3x3['target_words']}")

    lvl_4x4 = gen.generate_level(
        level_num=5, rows=4, cols=4, template="corners", fill_mode="full_cover",
        word_lengths=[4, 4, 5, 5], strictness="deadlock_free", rng_seed=99
    )
    assert lvl_4x4["solver_report"]["zero_deadlock"] is True
    # Verify extra word lengths strictly match target word lengths
    t_lens = {len(w) for w in lvl_4x4["target_words"]}
    assert all(ex["length"] in t_lens for ex in lvl_4x4["extra_words"])
    print(f"  [PASS] Level 5 (4x4 corners full_cover) in {lvl_4x4['generation_ms']}ms: targets={lvl_4x4['target_words']}, extras={len(lvl_4x4['extra_words'])}")

    lvl_5x5 = gen.generate_level(
        level_num=10, rows=5, cols=5, template="center_hole", fill_mode="auto_void",
        word_lengths=[4, 5, 5, 6, 6], strictness="deadlock_free", rng_seed=123
    )
    assert lvl_5x5["solver_report"]["zero_deadlock"] is True
    print(f"  [PASS] Level 10 (5x5 center_hole) in {lvl_5x5['generation_ms']}ms: targets={lvl_5x5['target_words']}, extras={len(lvl_5x5['extra_words'])}")

    # Verify winning_solution actually clears the generated level inside GameSession!
    for name, lvl in [("L1", lvl1), ("L2", lvl_3x3), ("L5", lvl_4x4), ("L10", lvl_5x5)]:
        s = GameSession(lvl)
        for step in lvl["solver_report"]["winning_solution"]:
            res = s.submit_path([tuple(p) for p in step["path"]])
            assert res["status"] == "TARGET", f"{name} step failed: {res}"
        assert s.cleared is True, f"{name} did not clear!"
    print("  [PASS] All generated levels verified end-to-end through GameSession.")

    # 4. Test dynamic word_lengths=None generation yields varied valid levels
    print("[Test 4] Dynamic word_lengths sampling (word_lengths=None)...")
    dyn_signatures = set()
    for seed in range(10, 16):
        dlvl = gen.generate_level(level_num=3, rows=4, cols=4, template="full", fill_mode="full_cover", word_lengths=None, rng_seed=seed)
        assert dlvl["solver_report"]["zero_deadlock"] is True
        sig = tuple(sorted(len(w) for w in dlvl["target_words"]))
        dyn_signatures.add(sig)
    assert len(dyn_signatures) >= 2, f"Expected varied length combinations, got {dyn_signatures}"
    print(f"  [PASS] Dynamic word lengths produced varied combinations: {sorted(dyn_signatures)}")

    # 5. Test 4-stage pipeline: initial_words + selected_candidates == target_words, remaining -> extra_words, toggle_candidate
    print("[Test 5] 4-stage candidate promotion & toggle_candidate...")
    p_lvl = gen.generate_level(level_num=4, rows=4, cols=4, template="full", fill_mode="full_cover", rng_seed=42)
    assert p_lvl["target_words"] == p_lvl["initial_words"] + p_lvl["selected_candidates"]
    assert len(p_lvl["selected_candidates"]) >= 1, f"Expected >=1 promoted candidate, got {p_lvl}"
    assert all("rarity" in td and "rarity_tier" in td and "path_score" in td and "diff_score" in td for td in p_lvl["solver_report"]["target_details"])
    assert all("rarity" in ex and "rarity_tier" in ex for ex in p_lvl["extra_words"])
    # Verify rarity tier & rarity score formula: max(0, len - 3) * max(0, tier - 1)
    assert gdict.get_word_rarity("ACT") == 0  # 3-letter word always 0
    assert gdict.get_word_rarity("ABLE") == 0  # tier 1 -> 0
    for sample_w in ["ACNE", "ACORN", "ALIAS"]:
        expected_r = max(0, len(sample_w) - 3) * max(0, gdict.get_word_rarity_tier(sample_w) - 1)
        assert gdict.get_word_rarity(sample_w) == expected_r, f"{sample_w} rarity mismatch"
    for td in p_lvl["solver_report"]["target_details"]:
        assert td["diff_score"] == td["rarity"] + td["path_score"]
    init_sum = sum(c["count"] for row in p_lvl["board"] for c in row)
    # Demote first selected candidate -> counts drop by its length, word moves out of target_words
    cand_to_demote = p_lvl["selected_candidates"][0]
    demoted = solver.toggle_candidate(p_lvl, cand_to_demote)
    assert cand_to_demote not in demoted["target_words"]
    assert sum(c["count"] for row in demoted["board"] for c in row) == init_sum - len(cand_to_demote)
    # Re-promote it back -> counts restore & level stays solvable
    re_promoted = solver.toggle_candidate(demoted, cand_to_demote)
    assert cand_to_demote in re_promoted["target_words"]
    assert sum(c["count"] for row in re_promoted["board"] for c in row) == init_sum
    assert re_promoted["solver_report"]["is_solvable"] is True
    print(f"  [PASS] 4-stage pipeline verified: initial={p_lvl['initial_words']}, selected={p_lvl['selected_candidates']}, extras={len(p_lvl['extra_words'])}, other_cands={len(p_lvl['other_candidates'])}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Letter Grid Word Puzzle Engine / Solver / Generator")
    parser.add_argument("--test", action="store_true", help="Run comprehensive rule, solver, and generator unit tests")
    parser.add_argument("--batch", type=int, default=0, help="Generate N sample levels and save to --out")
    parser.add_argument("--out", type=str, default="sample_levels.json", help="Output JSON path for batch generation")
    args = parser.parse_args()

    if args.test:
        run_tests()
        return

    if args.batch > 0:
        gdict = GameDictionary()
        gen = LevelGenerator(gdict)
        presets = [
            {"level_num": 1, "rows": 2, "cols": 4, "template": "full", "fill_mode": "full_cover"},
            {"level_num": 2, "rows": 3, "cols": 3, "template": "full", "fill_mode": "full_cover"},
            {"level_num": 3, "rows": 2, "cols": 5, "template": "full", "fill_mode": "full_cover"},
            {"level_num": 4, "rows": 3, "cols": 4, "template": "full", "fill_mode": "full_cover"},
            {"level_num": 5, "rows": 3, "cols": 5, "template": "full", "fill_mode": "full_cover"},
        ]
        levels = []
        for i in range(args.batch):
            cfg = dict(presets[i % len(presets)])
            cfg["level_num"] = i + 1 if i > 0 else 1
            lvl = gen.generate_level(**cfg, rng_seed=1000 + i)
            levels.append(lvl)
        out_path = Path(args.out)
        out_path.write_text(json.dumps(levels, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Generated {len(levels)} levels -> {out_path}")


if __name__ == "__main__":
    main()
