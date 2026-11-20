importScripts("https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js");

let pyodideReady = null;

async function initPyodide() {
  if (pyodideReady) return pyodideReady;
  pyodideReady = (async () => {
    const pyodide = await loadPyodide({
      indexURL: "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/",
    });
    const [coreResp, dictResp] = await Promise.all([
      fetch("./level_core.py"),
      fetch("./game_dictionary.json"),
    ]);
    const [coreText, dictBytes] = await Promise.all([
      coreResp.text(),
      dictResp.arrayBuffer(),
    ]);
    pyodide.FS.writeFile("/home/pyodide/level_core.py", coreText);
    pyodide.FS.writeFile("/home/pyodide/game_dictionary.json", new Uint8Array(dictBytes));

    await pyodide.runPythonAsync(`
import sys, json
sys.path.insert(0, "/home/pyodide")
from level_core import GameDictionary, LevelGenerator, LevelSolver, orient_level_forward

GDICT = GameDictionary()
SOLVER = LevelSolver(GDICT)
GENERATOR = LevelGenerator(GDICT)

def run_batch(body_str):
    body = json.loads(body_str)
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
    avoid_words = set(body.get("avoid_words") or [])

    raw_lengths = body.get("word_lengths")
    base_word_lengths = [int(x) for x in raw_lengths] if raw_lengths else None
    raw_seeds = body.get("seed_words")
    if isinstance(raw_seeds, str):
        seed_words = [s.strip() for s in raw_seeds.replace("，", ",").split(",") if s.strip()]
    else:
        seed_words = raw_seeds or []

    presets = [(2, 4), (3, 3), (2, 5), (3, 4), (3, 5)]
    levels = []
    attempts = 0
    max_attempts = count * 8

    while len(levels) < count and attempts < max_attempts:
        idx = len(levels)
        attempts += 1
        lvl_num = start_num + idx
        rows = int(body.get("rows", 4))
        cols = int(body.get("cols", 4))
        template = str(body.get("template", "full"))
        fill_mode = str(body.get("fill_mode", "auto_void"))
        word_lengths = list(base_word_lengths) if base_word_lengths else None

        if size_mode == "presets":
            rows, cols = presets[(start_num - 1 + idx) % len(presets)]
            template = "full"
            fill_mode = "full_cover"

        if lvl_num == 1 and not word_lengths:
            rows = min(4, rows)
            cols = min(4, cols)
            fill_mode = "auto_void"
        elif lvl_num >= 2 and word_lengths:
            word_lengths = [max(3, int(x)) for x in word_lengths]

        rng_seed = None
        if body.get("rng_seed") not in (None, ""):
            rng_seed = int(body["rng_seed"]) + attempts - 1

        if not avoid_hot:
            GENERATOR.reset_usage_stats()

        gen_kwargs = dict(
            level_num=lvl_num,
            rows=rows,
            cols=cols,
            template=template,
            void_mask=body.get("void_mask"),
            fill_mode=fill_mode,
            word_count=int(body["word_count"]) if body.get("word_count") else None,
            word_lengths=word_lengths,
            max_rank=max_rank,
            allowed_bands=allowed_bands,
            allowed_spellings=allowed_spellings,
            seed_words=seed_words,
            max_cell_count=max_cell_count,
            strictness="path_unique" if strategy == "full_scan_peel" else str(body.get("strictness", "deadlock_free")),
            extra_categories=extra_cats,
            avoid_words=avoid_words if avoid_hot and avoid_words else None,
            rng_seed=rng_seed,
            timeout_sec=float(body.get("timeout_sec", 4.5)),
        )

        if strategy != "full_scan_peel":
            levels.append(GENERATOR.generate_level(**gen_kwargs))
            continue

        try:
            base_lvl = GENERATOR.generate_level(**gen_kwargs)
        except Exception:
            continue

        r_n = int(base_lvl["rows"])
        c_n = int(base_lvl["cols"])
        all_cands = SOLVER.scan_all_candidates(
            base_lvl,
            max_rank=max_rank,
            allowed_bands=allowed_bands,
            allowed_spellings=allowed_spellings,
        )
        unique_elig = [c for c in all_cands if c["eligible_for_target"] and c["path_count"] == 1]

        counts = [[0] * c_n for _ in range(r_n)]
        void_mask = [[bool(base_lvl["board"][r][col].get("void", False)) for col in range(c_n)] for r in range(r_n)]
        for c in unique_elig:
            for r, col in c["sample_paths"][0]:
                counts[r][col] += 1

        if any(not void_mask[r][col] and counts[r][col] == 0 for r in range(r_n) for col in range(c_n)):
            continue

        target_wc = int(body["word_count"]) if body.get("word_count") else None
        rem_c3 = sum(1 for c in unique_elig if c["length"] == 3)
        rem_c4 = sum(1 for c in unique_elig if c["length"] == 4)
        rem_c5p = sum(1 for c in unique_elig if c["length"] >= 5)
        keep_len3 = ((start_num + idx + attempts) % 5) != 4
        rem_order = sorted(
            range(len(unique_elig)),
            key=lambda i: (-GENERATOR.target_usage[unique_elig[i]["word"]], unique_elig[i]["length"], unique_elig[i]["word"]),
        )
        removed_words = set()
        for i in rem_order:
            cur_max = max(counts[r][col] for r in range(r_n) for col in range(c_n))
            cur_wc = len(unique_elig) - len(removed_words)
            if cur_max <= max_cell_count and (target_wc is None or cur_wc <= target_wc):
                break
            w_len = unique_elig[i]["length"]
            if w_len >= 5 and rem_c5p <= 1:
                continue
            if w_len == 4 and rem_c4 <= 1:
                continue
            if keep_len3 and w_len == 3 and rem_c3 <= 1:
                continue
            path = unique_elig[i]["sample_paths"][0]
            if all(counts[r][col] >= 2 for r, col in path):
                for r, col in path:
                    counts[r][col] -= 1
                removed_words.add(unique_elig[i]["word"])
                if w_len >= 5:
                    rem_c5p -= 1
                elif w_len == 4:
                    rem_c4 -= 1
                elif w_len == 3:
                    rem_c3 -= 1

        if max(counts[r][col] for r in range(r_n) for col in range(c_n)) > max_cell_count:
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
                for col in range(c_n)
            ]
            for r in range(r_n)
        ]
        peeled_lvl = {
            "level_num": int(base_lvl["level_num"]),
            "rows": r_n,
            "cols": c_n,
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

    return json.dumps({"levels": levels}, ensure_ascii=False)
    `);
    return pyodide;
  })();
  return pyodideReady;
}

self.onmessage = async (ev) => {
  const { id, bodyStr } = ev.data;
  try {
    const pyodide = await initPyodide();
    const runBatch = pyodide.globals.get("run_batch");
    const jsonStr = runBatch(bodyStr);
    self.postMessage({ id, ok: true, jsonStr });
  } catch (err) {
    self.postMessage({ id, ok: false, error: err && err.message ? err.message : String(err) });
  }
};
