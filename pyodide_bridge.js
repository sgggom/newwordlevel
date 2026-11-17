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
            count = max(1, min(30, int(body.get("count", 5))))
            start_num = int(body.get("start_level_num", 1))
            levels = []
            for i in range(count):
                cfg = dict(body)
                cfg["level_num"] = start_num + i
                if cfg.get("rng_seed") is not None:
                    cfg["rng_seed"] = int(cfg["rng_seed"]) + i
                if cfg["level_num"] == 1 and not cfg.get("word_lengths"):
                    cfg["rows"] = min(4, int(cfg.get("rows", 3)))
                    cfg["cols"] = min(4, int(cfg.get("cols", 3)))
                    cfg["fill_mode"] = "auto_void"
                elif cfg["level_num"] >= 2 and cfg.get("word_lengths"):
                    cfg["word_lengths"] = [max(3, int(x)) for x in cfg["word_lengths"]]
                levels.append(_handle_generate(cfg))
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

  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input.url;
    if (!url || !url.startsWith("/api/")) {
      return nativeFetch(input, init);
    }

    const mode = await ensureBackend();
    if (mode === "server") {
      return nativeFetch(input, init);
    }

    const bodyStr = init && init.body ? String(init.body) : "";
    const runTask = () => {
      const resTuple = dispatchPy(url.split("?")[0], bodyStr);
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
})();
