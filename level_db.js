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

    return {
      sig: computeSignature(lvl),
      rows,
      cols,
      size: `${rows}x${cols}`,
      max_cell_count: maxCell,
      target_count: targets.length,
      extra_count: extras.length,
      data: {
        level_num: Number(lvl.level_num || 2),
        rows,
        cols,
        template: lvl.template || "full",
        fill_mode: lvl.fill_mode || "full_cover",
        board: slimBoard,
        target_words: targets,
        extra_words: extras,
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

  function matchesFilter(rec, f) {
    if (f.size && rec.size !== f.size) return false;
    if (f.minMaxCount != null && rec.max_cell_count < f.minMaxCount) return false;
    if (f.maxMaxCount != null && rec.max_cell_count > f.maxMaxCount) return false;
    if (f.minTargets != null && rec.target_count < f.minTargets) return false;
    if (f.maxTargets != null && rec.target_count > f.maxTargets) return false;
    if (f.minExtras != null && rec.extra_count < f.minExtras) return false;
    if (f.maxExtras != null && rec.extra_count > f.maxExtras) return false;
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

  window.LevelDB = {
    openDB,
    count,
    clear,
    insertBatch,
    queryLevels,
    collectAllMatching,
    importText,
    computeSignature,
  };
})();
