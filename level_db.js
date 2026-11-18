(function () {
  const DB_NAME = "NewWordLevelDB";
  const DB_VERSION = 1;
  const STORE_NAME = "levels";

  let dbPromise = null;

  function openDB() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (ev) => {
        const db = ev.target.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) {
          const store = db.createObjectStore(STORE_NAME, { keyPath: "id", autoIncrement: true });
          store.createIndex("sig", "sig", { unique: true });
          store.createIndex("size", "size", { unique: false });
          store.createIndex("max_cell_count", "max_cell_count", { unique: false });
          store.createIndex("target_count", "target_count", { unique: false });
          store.createIndex("extra_count", "extra_count", { unique: false });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error || new Error("无法打开 IndexedDB"));
    });
    return dbPromise;
  }

  function computeSignature(lvl) {
    const boardKey = (lvl.board || [])
      .map((row) => row.map((c) => (c.void ? "#" : `${c.letter}${c.count}`)).join(","))
      .join("/");
    const targetsKey = [...(lvl.target_words || [])].map((w) => String(w).toUpperCase()).sort().join(",");
    return `${lvl.rows}x${lvl.cols}|${boardKey}|${targetsKey}`;
  }

  function findWordPathOnBoard(board, word) {
    if (!board || !board.length || !word || word.length < 2) return null;
    const rows = board.length;
    const cols = board[0].length;
    const w = String(word).toUpperCase();
    const dirs = [[-1, 0], [1, 0], [0, -1], [0, 1]];
    const used = Array.from({ length: rows }, () => Array(cols).fill(false));
    const path = [];

    function dfs(r, c, idx) {
      if (idx === w.length - 1) return true;
      const nextCh = w[idx + 1];
      for (const [dr, dc] of dirs) {
        const nr = r + dr;
        const nc = c + dc;
        if (nr >= 0 && nr < rows && nc >= 0 && nc < cols && !used[nr][nc]) {
          const cell = board[nr][nc];
          if (cell && !cell.void && Number(cell.count || 0) > 0 && String(cell.letter || "").toUpperCase() === nextCh) {
            used[nr][nc] = true;
            path.push([nr, nc]);
            if (dfs(nr, nc, idx + 1)) return true;
            path.pop();
            used[nr][nc] = false;
          }
        }
      }
      return false;
    }

    const ch0 = w[0];
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const cell = board[r][c];
        if (cell && !cell.void && Number(cell.count || 0) > 0 && String(cell.letter || "").toUpperCase() === ch0) {
          used[r][c] = true;
          path.push([r, c]);
          if (dfs(r, c, 0)) return path.map(([pr, pc]) => [pr, pc]);
          path.pop();
          used[r][c] = false;
        }
      }
    }
    return null;
  }

  function scorePathDirection(path) {
    if (!path || path.length < 2) return 0;
    let total = 0;
    for (let k = 0; k < path.length - 1; k++) {
      const dr = path[k + 1][0] - path[k][0];
      const dc = path[k + 1][1] - path[k][1];
      const stepScore = dr + dc; // right(+1), down(+1), left(-1), up(-1)
      total += (k === 0 ? 2 : 1) * stepScore;
    }
    return total;
  }

  function scoreWordPath(path) {
    if (!path || path.length < 2) return 0;
    const dr0 = path[1][0] - path[0][0];
    const dc0 = path[1][1] - path[0][1];
    // 起步方向：右/下=0，左/上=1
    const startScore = (dr0 === -1 || dc0 === -1) ? 1 : 0;
    let turns = 0;
    let reversals = 0;
    let prevDr = dr0;
    let prevDc = dc0;
    let lastHoriz = dc0 !== 0 ? dc0 : 0;
    let lastVert = dr0 !== 0 ? dr0 : 0;

    for (let k = 1; k < path.length - 1; k++) {
      const dr = path[k + 1][0] - path[k][0];
      const dc = path[k + 1][1] - path[k][1];
      if (dr !== prevDr || dc !== prevDc) {
        turns++;
      }
      if (dc !== 0) {
        if (lastHoriz !== 0 && dc === -lastHoriz) reversals++;
        lastHoriz = dc;
      }
      if (dr !== 0) {
        if (lastVert !== 0 && dr === -lastVert) reversals++;
        lastVert = dr;
      }
      prevDr = dr;
      prevDc = dc;
    }
    return startScore + turns + reversals;
  }

  function computeLevelMetrics(lvl) {
    const targets = (lvl.target_words || []).map((w) => String(w).toUpperCase());
    const initPaths = lvl.initial_word_paths || {};
    const selPaths = lvl.selected_candidate_paths || {};
    const tdMap = {};
    if (lvl.solver_report && Array.isArray(lvl.solver_report.target_details)) {
      for (const td of lvl.solver_report.target_details) {
        if (td && td.word && td.sample_paths && td.sample_paths.length) {
          tdMap[String(td.word).toUpperCase()] = td.sample_paths[0];
        }
      }
    }

    let charSum = 0;
    let dirTotal = 0;
    let pathTotal = 0;
    let fwdCount = 0;
    let revCount = 0;
    const wordDirScores = {};
    const wordPathScores = {};

    for (const w of targets) {
      charSum += w.length;
      const p = initPaths[w] || selPaths[w] || tdMap[w] || findWordPathOnBoard(lvl.board, w);
      const score = p ? scorePathDirection(p) : 0;
      const pScore = p ? scoreWordPath(p) : 0;
      wordDirScores[w] = score;
      wordPathScores[w] = pScore;
      dirTotal += score;
      pathTotal += pScore;
      if (score > 0) fwdCount++;
      else if (score < 0) revCount++;
    }

    const n = targets.length;
    return {
      avg_len: n ? Math.round((charSum / n) * 100) / 100 : 0,
      dir_score_total: dirTotal,
      dir_score_avg: n ? Math.round((dirTotal / n) * 100) / 100 : 0,
      path_score_total: pathTotal,
      path_score_avg: n ? Math.round((pathTotal / n) * 100) / 100 : 0,
      fwd_word_count: fwdCount,
      rev_word_count: revCount,
      word_dir_scores: wordDirScores,
      word_path_scores: wordPathScores,
    };
  }

  function slimLevel(lvl) {
    const rows = Number(lvl.rows || 0);
    const cols = Number(lvl.cols || 0);
    let maxCell = 0;
    const slimBoard = (lvl.board || []).map((row) =>
      row.map((c) => {
        const cnt = c.void ? 0 : Number(c.count || 0);
        if (cnt > maxCell) maxCell = cnt;
        return c.void
          ? { r: c.r, c: c.c, void: true, letter: "", count: 0 }
          : { r: c.r, c: c.c, void: false, letter: String(c.letter || "").toUpperCase(), count: cnt };
      })
    );
    const targets = (lvl.target_words || []).map((w) => String(w).toUpperCase());
    const extras = (lvl.extra_words || []).map((ex) =>
      typeof ex === "string" ? ex.toUpperCase() : String(ex.word || "").toUpperCase()
    ).filter(Boolean);
    const metrics = computeLevelMetrics(lvl);

    return {
      sig: computeSignature(lvl),
      rows,
      cols,
      size: `${rows}x${cols}`,
      max_cell_count: maxCell,
      target_count: targets.length,
      extra_count: extras.length,
      avg_len: metrics.avg_len,
      dir_score_total: metrics.dir_score_total,
      dir_score_avg: metrics.dir_score_avg,
      path_score_total: metrics.path_score_total,
      path_score_avg: metrics.path_score_avg,
      fwd_word_count: metrics.fwd_word_count,
      rev_word_count: metrics.rev_word_count,
      data: {
        level_num: Number(lvl.level_num || 2),
        rows,
        cols,
        template: lvl.template || "full",
        fill_mode: lvl.fill_mode || "full_cover",
        board: slimBoard,
        target_words: targets,
        extra_words: extras,
        word_dir_scores: metrics.word_dir_scores,
        word_path_scores: metrics.word_path_scores,
      },
    };
  }

  async function count() {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readonly");
      const req = tx.objectStore(STORE_NAME).count();
      req.onsuccess = () => resolve(req.result || 0);
      req.onerror = () => reject(req.error);
    });
  }

  async function clear() {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      const req = tx.objectStore(STORE_NAME).clear();
      req.onsuccess = () => resolve();
      req.onerror = () => reject(req.error);
    });
  }

  async function insertBatch(rawLevels) {
    if (!rawLevels || !rawLevels.length) return { inserted: 0, skipped: 0 };
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      const store = tx.objectStore(STORE_NAME);
      let inserted = 0;
      let skipped = 0;

      for (const raw of rawLevels) {
        const rec = slimLevel(raw);
        const req = store.add(rec);
        req.onsuccess = () => {
          inserted++;
        };
        req.onerror = (ev) => {
          // Unique constraint violation on sig -> duplicate level, skip cleanly
          ev.preventDefault();
          ev.stopPropagation();
          skipped++;
        };
      }

      tx.oncomplete = () => resolve({ inserted, skipped });
      tx.onerror = () => reject(tx.error || new Error("批量写入 IndexedDB 失败"));
    });
  }

  function ensureMetrics(rec) {
    if (
      rec.avg_len == null ||
      rec.dir_score_avg == null ||
      rec.path_score_avg == null ||
      rec.rev_word_count == null ||
      !rec.data.word_dir_scores ||
      !rec.data.word_path_scores
    ) {
      const m = computeLevelMetrics(rec.data);
      rec.avg_len = m.avg_len;
      rec.dir_score_total = m.dir_score_total;
      rec.dir_score_avg = m.dir_score_avg;
      rec.path_score_total = m.path_score_total;
      rec.path_score_avg = m.path_score_avg;
      rec.fwd_word_count = m.fwd_word_count;
      rec.rev_word_count = m.rev_word_count;
      rec.data.word_dir_scores = m.word_dir_scores;
      rec.data.word_path_scores = m.word_path_scores;
    }
    return rec;
  }

  function matchesFilter(rec, f) {
    ensureMetrics(rec);
    if (f.size && rec.size !== f.size) return false;
    if (f.minMaxCount != null && rec.max_cell_count < f.minMaxCount) return false;
    if (f.maxMaxCount != null && rec.max_cell_count > f.maxMaxCount) return false;
    if (f.minTargets != null && rec.target_count < f.minTargets) return false;
    if (f.maxTargets != null && rec.target_count > f.maxTargets) return false;
    if (f.minExtras != null && rec.extra_count < f.minExtras) return false;
    if (f.maxExtras != null && rec.extra_count > f.maxExtras) return false;
    if (f.minAvgLen != null && rec.avg_len < f.minAvgLen) return false;
    if (f.maxAvgLen != null && rec.avg_len > f.maxAvgLen) return false;
    if (f.minDirAvg != null && rec.dir_score_avg < f.minDirAvg) return false;
    if (f.maxDirAvg != null && rec.dir_score_avg > f.maxDirAvg) return false;
    if (f.minPathAvg != null && rec.path_score_avg < f.minPathAvg) return false;
    if (f.maxPathAvg != null && rec.path_score_avg > f.maxPathAvg) return false;
    if (f.minRevWords != null && rec.rev_word_count < f.minRevWords) return false;
    if (f.maxRevWords != null && rec.rev_word_count > f.maxRevWords) return false;
    if (f.dirTendency && f.dirTendency !== "all") {
      if (f.dirTendency === "forward" && !(rec.dir_score_avg > 0)) return false;
      if (f.dirTendency === "reverse" && !(rec.dir_score_avg < 0)) return false;
      if (f.dirTendency === "all_forward" && rec.rev_word_count !== 0) return false;
      if (f.dirTendency === "has_reverse" && !(rec.rev_word_count > 0)) return false;
    }
    if (f.wordContains) {
      const targetUpper = f.wordContains;
      if (!rec.data.target_words.some((w) => w.includes(targetUpper))) return false;
    }
    return true;
  }

  async function queryLevels(filters = {}, limit = 50, offset = 0) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readonly");
      const store = tx.objectStore(STORE_NAME);
      const source = filters.size
        ? store.index("size").openCursor(IDBKeyRange.only(filters.size))
        : store.openCursor();

      const items = [];
      let totalMatched = 0;

      source.onsuccess = (ev) => {
        const cursor = ev.target.result;
        if (!cursor) {
          resolve({ totalMatched, items });
          return;
        }
        const rec = cursor.value;
        if (matchesFilter(rec, filters)) {
          if (totalMatched >= offset && items.length < limit) {
            items.push(rec);
          }
          totalMatched++;
        }
        cursor.continue();
      };
      source.onerror = () => reject(source.error);
    });
  }

  async function collectAllMatching(filters = {}, onProgress) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readonly");
      const store = tx.objectStore(STORE_NAME);
      const source = filters.size
        ? store.index("size").openCursor(IDBKeyRange.only(filters.size))
        : store.openCursor();

      const lines = [];
      let scanned = 0;

      source.onsuccess = (ev) => {
        const cursor = ev.target.result;
        if (!cursor) {
          resolve(lines);
          return;
        }
        scanned++;
        const rec = cursor.value;
        if (matchesFilter(rec, filters)) {
          lines.push(JSON.stringify(rec.data));
        }
        if (onProgress && scanned % 5000 === 0) {
          onProgress(scanned, lines.length);
        }
        cursor.continue();
      };
      source.onerror = () => reject(source.error);
    });
  }

  async function importText(text, isJsonl, onProgress) {
    let items = [];
    if (isJsonl) {
      const lines = text.split(/\r?\n/);
      for (const line of lines) {
        const s = line.trim();
        if (!s) continue;
        try {
          items.push(JSON.parse(s));
        } catch (_) {}
      }
    } else {
      const parsed = JSON.parse(text);
      items = Array.isArray(parsed) ? parsed : (parsed.levels || [parsed]);
    }

    let totalInserted = 0;
    let totalSkipped = 0;
    const chunkSize = 1000;
    for (let i = 0; i < items.length; i += chunkSize) {
      const chunk = items.slice(i, i + chunkSize);
      const res = await insertBatch(chunk);
      totalInserted += res.inserted;
      totalSkipped += res.skipped;
      if (onProgress) onProgress(Math.min(i + chunk.length, items.length), items.length, totalInserted, totalSkipped);
    }
    return { total: items.length, inserted: totalInserted, skipped: totalSkipped };
  }

  function applyBoardSymmetry(lvl, mode) {
    if (!mode || mode === "id") return lvl;
    const rows = Number(lvl.rows || (lvl.board ? lvl.board.length : 0));
    const cols = Number(lvl.cols || (lvl.board && lvl.board[0] ? lvl.board[0].length : 0));
    const fnMap = {
      flip_h: (r, c) => [r, cols - 1 - c],
      flip_v: (r, c) => [rows - 1 - r, c],
      rot180: (r, c) => [rows - 1 - r, cols - 1 - c],
    };
    const fn = fnMap[mode];
    if (!fn) return lvl;

    const nextBoard = Array.from({ length: rows }, () => Array(cols).fill(null));
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const [nr, nc] = fn(r, c);
        nextBoard[nr][nc] = { ...lvl.board[r][c], r: nr, c: nc };
      }
    }
    const cloned = { ...lvl, board: nextBoard };
    delete cloned.initial_word_paths;
    delete cloned.selected_candidate_paths;
    delete cloned.word_dir_scores;
    delete cloned.word_path_scores;
    return cloned;
  }

  function orientLevelForward(lvl) {
    const rows = Number(lvl.rows || (lvl.board ? lvl.board.length : 0));
    const cols = Number(lvl.cols || (lvl.board && lvl.board[0] ? lvl.board[0].length : 0));
    if (!rows || !cols || !lvl.board) {
      return { changed: false, mode: "id", lvl, metrics: computeLevelMetrics(lvl) };
    }
    const fnMap = {
      id: (r, c) => [r, c],
      flip_h: (r, c) => [r, cols - 1 - c],
      flip_v: (r, c) => [rows - 1 - r, c],
      rot180: (r, c) => [rows - 1 - r, cols - 1 - c],
    };

    const validModes = ["id"];
    for (const mode of ["flip_h", "flip_v", "rot180"]) {
      const fn = fnMap[mode];
      let ok = true;
      for (let r = 0; r < rows && ok; r++) {
        for (let c = 0; c < cols; c++) {
          const [nr, nc] = fn(r, c);
          if (Boolean(lvl.board[r][c].void) !== Boolean(lvl.board[nr][nc].void)) {
            ok = false;
            break;
          }
        }
      }
      if (ok) validModes.push(mode);
    }

    let bestMode = "id";
    let bestMetrics = null;
    let bestKey = null;
    let bestLvl = lvl;

    for (const mode of validModes) {
      const cand = mode === "id" ? lvl : applyBoardSymmetry(lvl, mode);
      const m = computeLevelMetrics(cand);
      const key = [m.dir_score_total, -(m.path_score_total || 0), mode === "id" ? 1 : 0];
      if (
        !bestKey ||
        key[0] > bestKey[0] ||
        (key[0] === bestKey[0] && key[1] > bestKey[1]) ||
        (key[0] === bestKey[0] && key[1] === bestKey[1] && key[2] > bestKey[2])
      ) {
        bestKey = key;
        bestMode = mode;
        bestMetrics = m;
        bestLvl = cand;
      }
    }

    return {
      changed: bestMode !== "id",
      mode: bestMode,
      lvl: bestLvl,
      metrics: bestMetrics,
    };
  }

  async function orientAllLevels(onProgress) {
    const db = await openDB();
    const total = await count();
    if (total === 0) return { total: 0, updated: 0, unchanged: 0, deduped: 0 };

    // Read all keys/records in chunks via cursor, then write updates in batches
    const allRecs = await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readonly");
      const req = tx.objectStore(STORE_NAME).getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });

    let updated = 0;
    let unchanged = 0;
    let deduped = 0;
    const chunkSize = 500;

    for (let i = 0; i < allRecs.length; i += chunkSize) {
      const chunk = allRecs.slice(i, i + chunkSize);
      await new Promise((resolve, reject) => {
        const tx = db.transaction(STORE_NAME, "readwrite");
        const store = tx.objectStore(STORE_NAME);

        for (const rec of chunk) {
          const res = orientLevelForward(rec.data);
          const m = res.metrics;
          const needUpdate =
            res.changed ||
            rec.avg_len == null ||
            rec.dir_score_avg == null ||
            rec.path_score_avg == null ||
            !rec.data.word_path_scores;

          if (!needUpdate) {
            unchanged++;
            continue;
          }

          const newData = {
            ...res.lvl,
            word_dir_scores: m.word_dir_scores,
            word_path_scores: m.word_path_scores,
          };
          const newRec = {
            ...rec,
            sig: computeSignature(newData),
            avg_len: m.avg_len,
            dir_score_total: m.dir_score_total,
            dir_score_avg: m.dir_score_avg,
            path_score_total: m.path_score_total,
            path_score_avg: m.path_score_avg,
            fwd_word_count: m.fwd_word_count,
            rev_word_count: m.rev_word_count,
            data: newData,
          };

          const putReq = store.put(newRec);
          putReq.onsuccess = () => {
            if (res.changed) updated++;
            else unchanged++;
          };
          putReq.onerror = (ev) => {
            // If flipped signature collides with an existing record, delete this redundant one
            ev.preventDefault();
            ev.stopPropagation();
            store.delete(rec.id);
            deduped++;
          };
        }

        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });

      if (onProgress) {
        onProgress(Math.min(i + chunk.length, allRecs.length), allRecs.length, updated, unchanged, deduped);
      }
      // Yield to browser UI thread between chunks
      await new Promise((r) => setTimeout(r, 0));
    }

    return { total: allRecs.length, updated, unchanged, deduped };
  }

  window.LevelDB = {
    openDB,
    count,
    clear,
    insertBatch,
    queryLevels,
    collectAllMatching,
    importText,
    computeSignature,
    computeLevelMetrics,
    scorePathDirection,
    scoreWordPath,
    orientLevelForward,
    orientAllLevels,
  };
})();
