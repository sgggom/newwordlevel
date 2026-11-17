(function () {
  const nativeFetch = window.fetch.bind(window);
  let backendMode = null; // "server" | "pyodide"
  let initPromise = null;
  let pyodide = null;
  let dispatchPy = null;
  let pyQueue = Promise.resolve();

  function setBanner(text) {
    const el = document.getElementById("dictSummary");
    if (el) el.textContent = text;
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = () => reject(new Error("无法加载 Pyodide 脚本: " + src));
      document.head.appendChild(s);
    });
  }

  async function ensureBackend() {
    if (backendMode) return backendMode;
    if (initPromise) return initPromise;

    initPromise = (async () => {
      const isLocal =
        location.hostname === "127.0.0.1" ||
        location.hostname === "localhost";

      if (isLocal && location.protocol !== "file:") {
        try {
          const ctrl = new AbortController();
          const timer = setTimeout(() => ctrl.abort(), 800);
          const probe = await nativeFetch("/api/meta", { signal: ctrl.signal });
          clearTimeout(timer);
          if (probe.ok) {
            backendMode = "server";
            return "server";
          }
        } catch (_) {
          // Fall back to Pyodide
        }
      }

      backendMode = "pyodide";
      setBanner("⏳ 首次加载浏览器 Python 引擎 (约10MB，有缓存)...");
      await loadScript("https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js");

      setBanner("⏳ 初始化 Pyodide 运行时...");
      pyodide = await window.loadPyodide({
        indexURL: "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/",
      });

      setBanner("⏳ 加载核心引擎与词典 (3.7MB)...");
      const [coreResp, dictResp] = await Promise.all([
        nativeFetch("./level_core.py"),
        nativeFetch("./game_dictionary.json"),
      ]);
      if (!coreResp.ok || !dictResp.ok) {
        throw new Error("无法加载 level_core.py 或 game_dictionary.json");
      }
      const [coreText, dictBytes] = await Promise.all([
        coreResp.text(),
        dictResp.arrayBuffer(),
      ]);

      pyodide.FS.writeFile("/home/pyodide/level_core.py", coreText);
      pyodide.FS.writeFile("/home/pyodide/game_dictionary.json", new Uint8Array(dictBytes));

      setBanner("⏳ 构建内存词库索引...");
      await pyodide.runPythonAsync(`
import sys, json, uuid
sys.path.insert(0, "/home/pyodide")
from level_core import GameDictionary, GameSession, LevelGenerator, LevelSolver

GDICT = GameDictionary()
SOLVER = LevelSolver(GDICT)
GENERATOR = LevelGenerator(GDICT)
SESSIONS = {}

def _handle_generate(body):
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

def dispatch_api(path, body_str):
    try:
        body = json.loads(body_str) if body_str else {}
        if path == "/api/meta":
            return 200, json.dumps({
                "meta": GDICT.meta,
                "bands": ["1k", "2k", "3k", "4k", "5k", "6k", "7k", "8k", "9k"],
                "spellings": ["通用", "英式 (UK)", "美式 (US)"],
                "tutorial_2_words": sorted(GDICT.tutorial_by_word.keys()),
            }, ensure_ascii=False)

        if path == "/api/generate":
            lvl = _handle_generate(body)
            return 200, json.dumps({"level": lvl}, ensure_ascii=False)

        if path == "/api/generate_batch":
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
            levels = []
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
                    levels.append(_handle_generate(cfg))
                    continue

                cfg["strictness"] = "path_unique"
                try:
                    base_lvl = _handle_generate(cfg)
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
                removed_words = set()
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
                peeled_lvl = {
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

                if avoid_hot:
                    for w in final_targets:
                        GENERATOR.target_usage[w] += 1
                    for ex in peeled_lvl.get("extra_words", []):
                        GENERATOR.extra_usage[ex["word"] if isinstance(ex, dict) else ex] += 1
                levels.append(peeled_lvl)

            return 200, json.dumps({"levels": levels}, ensure_ascii=False)

        if path == "/api/solve":
            level_data = body.get("level") or body
            max_rank = int(body.get("max_rank", 6000))
            extra_cats = body.get("extra_categories") or ["inflection", "obscure_base"]
            SOLVER.partition_candidates_and_solve(
                level_data,
                max_rank=max_rank,
                extra_categories=extra_cats,
            )
            return 200, json.dumps({
                "solver_report": level_data["solver_report"],
                "extra_words": level_data["extra_words"],
                "other_candidates": level_data.get("other_candidates", []),
            }, ensure_ascii=False)

        if path == "/api/toggle_candidate":
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
            return 200, json.dumps({"level": updated}, ensure_ascii=False)

        if path == "/api/play/start":
            level_data = body["level"]
            sid = str(uuid.uuid4())
            sess = GameSession(level_data)
            if len(SESSIONS) > 200:
                oldest = next(iter(SESSIONS))
                SESSIONS.pop(oldest, None)
            SESSIONS[sid] = sess
            return 200, json.dumps({"session_id": sid, "state": sess.snapshot()}, ensure_ascii=False)

        if path == "/api/play/submit":
            sid = body.get("session_id")
            sess = SESSIONS.get(sid) if sid else None
            if sess is None:
                if "level" not in body:
                    return 404, json.dumps({"error": "试玩会话已过期，请重置关卡"}, ensure_ascii=False)
                sess = GameSession(body["level"])
                sid = str(uuid.uuid4())
                SESSIONS[sid] = sess
            path_pts = [tuple(pt) for pt in body.get("path", [])]
            res = sess.submit_path(path_pts)
            res["session_id"] = sid
            return 200, json.dumps(res, ensure_ascii=False)

        return 404, json.dumps({"error": "Unknown API endpoint"}, ensure_ascii=False)
    except Exception as exc:
        return 400, json.dumps({"error": str(exc)}, ensure_ascii=False)
      `);
      dispatchPy = pyodide.globals.get("dispatch_api");
      return "pyodide";
    })();

    return initPromise;
  }

  const workerPool = [];
  let workerMsgSeq = 0;

  function ensureWorkers(n) {
    while (workerPool.length < n) {
      workerPool.push(new Worker("./pyodide_worker.js"));
    }
    return workerPool.slice(0, n);
  }

  function runOnWorker(worker, cfg) {
    return new Promise((resolve, reject) => {
      const id = ++workerMsgSeq;
      const onMsg = (ev) => {
        if (ev.data && ev.data.id === id) {
          worker.removeEventListener("message", onMsg);
          worker.removeEventListener("error", onErr);
          if (ev.data.ok) {
            resolve(JSON.parse(ev.data.jsonStr));
          } else {
            reject(new Error(ev.data.error || "Worker failed"));
          }
        }
      };
      const onErr = (err) => {
        worker.removeEventListener("message", onMsg);
        worker.removeEventListener("error", onErr);
        reject(new Error(err.message || "Worker error"));
      };
      worker.addEventListener("message", onMsg);
      worker.addEventListener("error", onErr);
      worker.postMessage({ id, bodyStr: JSON.stringify(cfg) });
    });
  }

  async function runParallelBatch(bodyStr) {
    const body = bodyStr ? JSON.parse(bodyStr) : {};
    const count = Math.max(1, parseInt(body.count || "5", 10));
    const reqWorkers = Math.max(1, Math.min(32, parseInt(body.workers || (navigator.hardwareConcurrency || 4), 10)));
    const nWorkers = Math.min(reqWorkers, count);
    if (nWorkers <= 1) return null;

    const workers = ensureWorkers(nWorkers);
    const baseCount = Math.floor(count / nWorkers);
    const rem = count % nWorkers;
    const startNum = parseInt(body.start_level_num || "1", 10);
    const baseSeed = body.rng_seed !== null && body.rng_seed !== undefined && body.rng_seed !== ""
      ? Number(body.rng_seed)
      : Math.floor(Math.random() * 10000000);

    let offset = 0;
    const promises = [];
    for (let i = 0; i < nWorkers; i++) {
      const wCount = baseCount + (i < rem ? 1 : 0);
      const subCfg = {
        ...body,
        count: wCount,
        start_level_num: startNum + offset,
        rng_seed: baseSeed + i * 500000,
      };
      offset += wCount;
      promises.push(runOnWorker(workers[i], subCfg));
    }

    const results = await Promise.all(promises);
    const merged = [];
    for (const r of results) {
      if (r && Array.isArray(r.levels)) {
        merged.push(...r.levels);
      }
    }
    return new Response(JSON.stringify({ levels: merged }), {
      status: 200,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  }

  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input.url;
    if (!url || !url.startsWith("/api/")) {
      return nativeFetch(input, init);
    }

    const mode = await ensureBackend();
    if (mode === "server") {
      return nativeFetch(input, init);
    }

    const cleanPath = url.split("?")[0];
    const bodyStr = init && init.body ? String(init.body) : "";

    if (cleanPath === "/api/generate_batch") {
      try {
        const parallelResp = await runParallelBatch(bodyStr);
        if (parallelResp) return parallelResp;
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message }), {
          status: 400,
          headers: { "Content-Type": "application/json; charset=utf-8" },
        });
      }
    }

    const runTask = () => {
      const resTuple = dispatchPy(cleanPath, bodyStr);
      const status = resTuple.get(0);
      const jsonStr = resTuple.get(1);
      resTuple.destroy();
      return new Response(jsonStr, {
        status,
        headers: { "Content-Type": "application/json; charset=utf-8" },
      });
    };

    const p = pyQueue.then(runTask, runTask);
    pyQueue = p.catch(() => {});
    return p;
  };

  // Continuous work-stealing multi-worker runner (zero barrier wait, saturates all CPU cores)
  window.runContinuousBatch = async function ({
    reqWorkers,
    chunkPerWorker = 20,
    getRemaining,
    makeTaskCfg,
    onLevels,
    shouldStop,
  }) {
    const mode = await ensureBackend();
    if (mode !== "pyodide") return false;

    const nWorkers = Math.max(1, Math.min(32, reqWorkers || (navigator.hardwareConcurrency || 4)));
    const workers = ensureWorkers(nWorkers);
    let inFlight = 0;
    let taskSeq = 0;

    async function workerLoop(workerIdx) {
      const worker = workers[workerIdx];
      while (!shouldStop()) {
        const rem = getRemaining(inFlight);
        if (rem <= 0) break;
        const take = Math.max(1, Math.min(chunkPerWorker, rem));
        inFlight += take;
        const seq = ++taskSeq;
        try {
          const cfg = makeTaskCfg(take, workerIdx, seq);
          const res = await runOnWorker(worker, cfg);
          const lvls = (res && Array.isArray(res.levels)) ? res.levels : [];
          onLevels(lvls);
        } finally {
          inFlight -= take;
        }
      }
    }

    await Promise.all(workers.map((_, idx) => workerLoop(idx)));
    return true;
  };
})();
