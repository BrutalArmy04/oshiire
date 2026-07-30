// ==UserScript==
// @name         Oshiire manifest-aware bulk unsave
// @namespace    oshiire
// @version      1.1.0
// @description  Count (default) or unsave ONLY the Reddit posts Oshiire has provably captured, slowly and politely, from your own logged-in old.reddit saved page. Dry run by default; the live run is behind an explicit toggle. Loads the whitelist built by export_unsave_list.py.
// @match        *://old.reddit.com/user/*/saved*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

/*
 * SAFETY: this script runs in YOUR authenticated browser and clicks the same
 * "unsave" links you would by hand. No API calls, no credentials, no network of
 * its own -- Reddit's own click handler + modhash do the work. It NEVER unsaves
 * anything that isn't in the whitelist you load; the default action for every
 * post is to skip it and leave it untouched. Comments (t1_) are never touched.
 *
 * TWO MODES. The script starts in DRY RUN and stays there until you say
 * otherwise, matching the rest of the project (archive.py is dry-run by default
 * too -- a destructive step should have to be asked for):
 *
 *   Dry run (count only)  -- DEFAULT. Walks the same listing, page by page,
 *       matches every post against the whitelist and reports how many WOULD be
 *       unsaved. It never calls .click(), so nothing on your account changes.
 *       Run this first, every time, and sanity-check the number.
 *   Live run (unsaves)    -- only after you pick it in the panel and confirm
 *       the warning. Does the actual clicking, with the same throttles.
 *
 * UNSAVING IS NOT REVERSIBLE FROM THIS TOOL. There is no re-save button here
 * and no undo: the feed is read-only to Oshiire, so a post unsaved by mistake
 * can only be recovered by finding it again on Reddit by hand. Drain in small
 * batches and re-check the count between runs.
 *
 * Load `unsave_list.json` (from export_unsave_list.py) via the panel's file
 * picker (or paste it). State lives in localStorage so the script survives the
 * automatic page-to-page navigation and can be paused/resumed. Re-running is
 * naturally idempotent: unsaved posts drop out of the saved listing.
 */
(function () {
    "use strict";

    // Only run on a real saved listing page (the @match already scopes this, but
    // guard against the account overview / other tabs).
    if (!/\/saved\/?/.test(location.pathname)) return;

    // ------------------------------------------------------------------ config
    const NS = "oshiire_unsave_";
    const K_LIST = NS + "list";
    const K_RUNNING = NS + "running";
    const K_STATS = NS + "stats";
    const K_LOG = NS + "log";
    const K_MODE = NS + "mode";

    // Dry run is the default, and an unrecognised/absent stored value falls back
    // to it -- so a corrupted localStorage can only ever fail SAFE.
    const MODE_COUNT = "count";
    const MODE_LIVE = "live";

    const CLICK_DELAY_MIN = 2000;   // ms between successful unsave clicks
    const CLICK_DELAY_MAX = 3000;
    const PAGE_DELAY = 5000;        // ms to wait before advancing to next page
    const SETTLE_DELAY = 1500;      // ms after (re)load before auto-resuming
    const VERIFY_TIMEOUT = 2500;    // ms to wait for the unsave toggle to flip
    const VERIFY_POLL = 150;        // ms between toggle-flip checks
    const BACKOFF_STEPS = [30000, 60000, 120000, 240000]; // rate-limit backoff
    const MAX_RETRIES = BACKOFF_STEPS.length;
    const LOG_CAP = 200;            // recent skipped/uncertain ids kept for display

    // ------------------------------------------------------------------ state
    let idSet = new Set();
    let stopFlag = false;           // set by Pause; halts the loop after current item
    let processing = false;         // guards against double-starting the loop

    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const jitter = (a, b) => a + Math.random() * (b - a);

    const ZERO_STATS = { unsaved: 0, skipped: 0, uncertain: 0, would: 0, scanned: 0 };

    function loadStats() {
        try {
            return Object.assign({}, ZERO_STATS,
                JSON.parse(localStorage.getItem(K_STATS) || "{}"));
        } catch (_) {
            return Object.assign({}, ZERO_STATS);
        }
    }
    function saveStats(s) { localStorage.setItem(K_STATS, JSON.stringify(s)); }
    let stats = loadStats();

    function resetStats() {
        stats = Object.assign({}, ZERO_STATS);
        saveStats(stats);
    }

    // Anything other than an explicit stored MODE_LIVE means dry run.
    function currentMode() {
        return localStorage.getItem(K_MODE) === MODE_LIVE ? MODE_LIVE : MODE_COUNT;
    }
    function isDryRun() { return currentMode() === MODE_COUNT; }

    function pushLog(msg) {
        let arr;
        try { arr = JSON.parse(localStorage.getItem(K_LOG) || "[]"); } catch (_) { arr = []; }
        arr.push(msg);
        if (arr.length > LOG_CAP) arr = arr.slice(arr.length - LOG_CAP);
        localStorage.setItem(K_LOG, JSON.stringify(arr));
        renderLog();
    }

    function isRunning() { return localStorage.getItem(K_RUNNING) === "1"; }
    function setRunning(v) { localStorage.setItem(K_RUNNING, v ? "1" : "0"); }

    function loadWhitelist() {
        try {
            const arr = JSON.parse(localStorage.getItem(K_LIST) || "[]");
            idSet = new Set(arr.map(normalizeId).filter(Boolean));
        } catch (_) {
            idSet = new Set();
        }
    }

    // Accept whatever form an id was stored/read in and reduce to bare base-36:
    // strips a leading `thing_`, then a leading `t3_`/`t1_`. The whitelist is
    // bare, and old.reddit's data-fullname is `t3_...`; both collapse here.
    function normalizeId(raw) {
        if (typeof raw !== "string") return "";
        let s = raw.trim();
        if (s.startsWith("thing_")) s = s.slice(6);
        if (s.startsWith("t3_") || s.startsWith("t1_")) s = s.slice(3);
        return s;
    }

    // ------------------------------------------------------------------ panel UI
    let elLoaded, elStartBtn, elCounters, elLogBox, elLoaderRow, elStatus, elModeNote;
    let elModeCount, elModeLive;

    function buildPanel() {
        const panel = document.createElement("div");
        panel.id = "oshiire-unsave-panel";
        panel.innerHTML = `
            <style>
              #oshiire-unsave-panel{position:fixed;top:12px;right:12px;z-index:2147483647;
                width:290px;font:12px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;
                background:#111;color:#eee;border:1px solid #444;border-radius:8px;
                box-shadow:0 4px 18px rgba(0,0,0,.5);padding:10px;}
              #oshiire-unsave-panel h4{margin:0 0 6px;font-size:13px;color:#fff;}
              #oshiire-unsave-panel button{cursor:pointer;border:1px solid #555;
                background:#222;color:#eee;border-radius:5px;padding:5px 9px;font-size:12px;}
              #oshiire-unsave-panel button:hover{background:#2c2c2c;}
              #oshiire-unsave-panel button.go{background:#1f6f43;border-color:#2c9c5f;}
              #oshiire-unsave-panel button.stop{background:#7a2222;border-color:#a83232;}
              #oshiire-unsave-panel .row{margin:6px 0;display:flex;gap:6px;align-items:center;flex-wrap:wrap;}
              #oshiire-unsave-panel textarea{width:100%;height:44px;background:#000;color:#9f9;
                border:1px solid #444;border-radius:4px;font-family:monospace;font-size:11px;}
              #oshiire-unsave-panel .counters{font-family:monospace;font-size:11px;color:#bbb;
                white-space:pre;background:#000;border:1px solid #333;border-radius:4px;padding:6px;}
              #oshiire-unsave-panel .status{font-size:11px;color:#f6c453;min-height:14px;}
              #oshiire-unsave-panel .logbox{margin-top:6px;max-height:110px;overflow:auto;
                background:#000;border:1px solid #333;border-radius:4px;padding:5px;
                font-family:monospace;font-size:10px;color:#999;}
              #oshiire-unsave-panel .muted{color:#888;font-size:11px;}
              #oshiire-unsave-panel .mode{border:1px solid #444;border-radius:5px;
                padding:6px;margin:6px 0;background:#181818;}
              #oshiire-unsave-panel .mode label{display:block;margin:2px 0;cursor:pointer;}
              #oshiire-unsave-panel .modenote{font-size:10px;margin-top:4px;line-height:1.35;}
              #oshiire-unsave-panel .modenote.safe{color:#7ec98f;}
              #oshiire-unsave-panel .modenote.danger{color:#ff8a8a;}
            </style>
            <h4>Oshiire &middot; bulk unsave</h4>
            <div class="row" id="ou-loader-row">
              <input type="file" id="ou-file" accept=".json,application/json" class="muted">
            </div>
            <div class="row">
              <textarea id="ou-paste" placeholder="...or paste unsave_list.json here"></textarea>
            </div>
            <div class="row">
              <button id="ou-load">Load list</button>
              <button id="ou-clear">Clear</button>
              <span id="ou-loaded" class="muted">no list</span>
            </div>
            <div class="mode">
              <label><input type="radio" name="ou-mode" id="ou-mode-count" value="count" checked>
                Dry run &mdash; count only</label>
              <label><input type="radio" name="ou-mode" id="ou-mode-live" value="live">
                Live run &mdash; actually unsave</label>
              <div class="modenote safe" id="ou-mode-note"></div>
            </div>
            <div class="row">
              <button id="ou-start" class="go">Start</button>
            </div>
            <div class="status" id="ou-status"></div>
            <div class="counters" id="ou-counters"></div>
            <div class="logbox" id="ou-log"></div>
        `;
        document.body.appendChild(panel);

        elLoaderRow = panel.querySelector("#ou-loader-row");
        elLoaded = panel.querySelector("#ou-loaded");
        elStartBtn = panel.querySelector("#ou-start");
        elCounters = panel.querySelector("#ou-counters");
        elLogBox = panel.querySelector("#ou-log");
        elStatus = panel.querySelector("#ou-status");
        elModeNote = panel.querySelector("#ou-mode-note");
        elModeCount = panel.querySelector("#ou-mode-count");
        elModeLive = panel.querySelector("#ou-mode-live");

        panel.querySelector("#ou-file").addEventListener("change", onFilePick);
        panel.querySelector("#ou-load").addEventListener("click", onPasteLoad);
        panel.querySelector("#ou-clear").addEventListener("click", onClear);
        elModeCount.addEventListener("change", () => onModeChange(MODE_COUNT));
        elModeLive.addEventListener("change", () => onModeChange(MODE_LIVE));
        elStartBtn.addEventListener("click", onStartPause);
    }

    // Switching modes is the explicit toggle that arms the destructive path, so
    // going live asks once, in words, and refuses silently-on. Switching also
    // resets the counters: a dry-run total and a live total mean different
    // things and must never be added together in one display.
    function onModeChange(requested) {
        if (requested === MODE_LIVE) {
            const ok = window.confirm(
                "LIVE RUN\n\n" +
                "This will actually unsave every whitelisted post it finds, in your " +
                "account, for real.\n\n" +
                "Unsaving cannot be undone from this tool -- there is no re-save " +
                "button and no history. A post removed by mistake can only be found " +
                "again by hand on Reddit.\n\n" +
                "Run the dry run first and check the count. Continue?");
            if (!ok) {
                elModeCount.checked = true;
                localStorage.setItem(K_MODE, MODE_COUNT);
                renderMode();
                setStatus("Stayed in dry run.");
                return;
            }
        }
        localStorage.setItem(K_MODE, requested);
        resetStats();
        renderMode();
        renderCounters(pageThings().length);
        setStatus(requested === MODE_LIVE
            ? "LIVE mode armed. Counters reset."
            : "Dry run. Nothing will be changed.");
    }

    function renderMode() {
        const live = !isDryRun();
        elModeCount.checked = !live;
        elModeLive.checked = live;
        // Mode is locked mid-run: flipping it between pages would splice two
        // different kinds of pass into one set of counters.
        const running = isRunning();
        elModeCount.disabled = running;
        elModeLive.disabled = running;
        elModeNote.className = "modenote " + (live ? "danger" : "safe");
        elModeNote.textContent = live
            ? "Clicks unsave for real. Not reversible from this tool."
            : "Reads the listing only. No clicks, nothing changes.";
    }

    function setStatus(msg) { if (elStatus) elStatus.textContent = msg || ""; }

    function renderLoaded() {
        elLoaded.textContent = idSet.size ? `loaded ${idSet.size} ids` : "no list";
    }

    function renderCounters(remainingOnPage) {
        const page = currentPageNumber();
        const tail =
            `remaining on page: ${remainingOnPage == null ? "-" : remainingOnPage}\n` +
            `page:              ${page == null ? "?" : page}`;
        elCounters.textContent = isDryRun()
            ? `DRY RUN -- no changes\n` +
              `would unsave:      ${stats.would}\n` +
              `would skip:        ${stats.skipped}\n` +
              `scanned:           ${stats.scanned}\n` + tail
            : `LIVE\n` +
              `unsaved (session): ${stats.unsaved}\n` +
              `skipped:           ${stats.skipped}\n` +
              `uncertain:         ${stats.uncertain}\n` +
              `scanned:           ${stats.scanned}\n` + tail;
    }

    function renderLog() {
        if (!elLogBox) return;
        let arr;
        try { arr = JSON.parse(localStorage.getItem(K_LOG) || "[]"); } catch (_) { arr = []; }
        elLogBox.textContent = arr.slice(-40).join("\n");
        elLogBox.scrollTop = elLogBox.scrollHeight;
    }

    function renderRunning() {
        const running = isRunning();
        elStartBtn.textContent = running
            ? "Pause"
            : (isDryRun() ? "Start dry run" : "Start LIVE run");
        elStartBtn.className = running ? "stop" : "go";
        renderMode();  // keep the mode radios locked/unlocked in step with it
    }

    // Rough page number from the ?count= query param (25 per page on old.reddit).
    function currentPageNumber() {
        const m = location.search.match(/[?&]count=(\d+)/);
        if (!m) return 1;
        return Math.floor(parseInt(m[1], 10) / 25) + 1;
    }

    // ------------------------------------------------------------------ list load
    function acceptList(text, sourceLabel) {
        let arr;
        try {
            arr = JSON.parse(text);
        } catch (e) {
            setStatus("Could not parse JSON: " + e.message);
            return;
        }
        if (!Array.isArray(arr)) {
            setStatus("Expected a JSON array of ids.");
            return;
        }
        const norm = arr.map(normalizeId).filter(Boolean);
        localStorage.setItem(K_LIST, JSON.stringify(norm));
        loadWhitelist();
        renderLoaded();
        setStatus(`Loaded ${idSet.size} ids from ${sourceLabel}.`);
    }

    function onFilePick(ev) {
        const file = ev.target.files && ev.target.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => acceptList(String(reader.result), file.name);
        reader.onerror = () => setStatus("File read error.");
        reader.readAsText(file);
    }

    function onPasteLoad() {
        const ta = document.getElementById("ou-paste");
        if (ta && ta.value.trim()) acceptList(ta.value, "pasted text");
        else setStatus("Paste the JSON array first, or use the file picker.");
    }

    function onClear() {
        localStorage.removeItem(K_LIST);
        localStorage.removeItem(K_STATS);
        localStorage.removeItem(K_LOG);
        // Clearing also disarms: a full reset that left LIVE armed would be a
        // trap for whoever presses Start next.
        localStorage.setItem(K_MODE, MODE_COUNT);
        setRunning(false);
        stopFlag = true;
        idSet = new Set();
        stats = loadStats();
        renderLoaded();
        renderCounters(null);
        renderLog();
        renderRunning();
        setStatus("Cleared list, counters and log. Back to dry run.");
    }

    // ------------------------------------------------------------------ unsave core
    // Find the clickable "unsave" anchor inside a thing (robust to old.reddit
    // markup variants: match by trimmed link text).
    function findUnsaveLink(thing) {
        const links = thing.querySelectorAll("a, span.option a, .link-save-button a");
        for (const a of links) {
            if (/^unsave$/i.test((a.textContent || "").trim())) return a;
        }
        return null;
    }

    function hasSaveAffordance(thing) {
        // Success = a "save" affordance is now present (toggle flipped back).
        const links = thing.querySelectorAll("a");
        for (const a of links) {
            if (/^save$/i.test((a.textContent || "").trim())) return true;
        }
        return false;
    }

    async function verifyFlipped(thing) {
        const deadline = Date.now() + VERIFY_TIMEOUT;
        while (Date.now() < deadline) {
            // Success if the unsave link is gone OR a save link has appeared.
            if (!findUnsaveLink(thing) || hasSaveAffordance(thing)) return true;
            await sleep(VERIFY_POLL);
        }
        return false;
    }

    // Attempt one unsave with rate-limit backoff. Returns "ok" | "uncertain".
    async function unsaveOne(thing, id) {
        for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
            if (stopFlag) return "uncertain";
            const link = findUnsaveLink(thing);
            if (!link) {
                // Already unsaved (e.g. a re-run) -> treat as done, don't count.
                return "ok";
            }
            link.click();
            if (await verifyFlipped(thing)) return "ok";

            // Didn't flip: likely rate-limited or a transient error. Back off.
            if (attempt < MAX_RETRIES) {
                const wait = BACKOFF_STEPS[attempt];
                setStatus(`Toggle didn't flip for ${id}; backing off ${Math.round(wait / 1000)}s (attempt ${attempt + 1}/${MAX_RETRIES})...`);
                await sleep(wait);
            }
        }
        return "uncertain";
    }

    // ------------------------------------------------------------------ page loop
    function pageThings() {
        // Posts only -- t1_ comments are never considered.
        return Array.from(document.querySelectorAll('.thing[data-fullname^="t3_"]'));
    }

    function nextPageHref() {
        const a = document.querySelector(".next-button a")
            || document.querySelector("span.next-button a");
        return a ? a.href : null;
    }

    async function runLoop() {
        if (processing) return;
        processing = true;
        stopFlag = false;

        const dryRun = isDryRun();
        const things = pageThings();
        let remaining = things.length;
        renderCounters(remaining);

        for (const thing of things) {
            if (stopFlag || !isRunning()) break;
            remaining--;

            const id = normalizeId(thing.getAttribute("data-fullname") || "");
            if (!id) { renderCounters(remaining); continue; }

            stats.scanned++;

            if (!idSet.has(id)) {
                // DEFAULT: skip. Only whitelisted posts are ever unsaved.
                stats.skipped++;
                saveStats(stats);
                renderCounters(remaining);
                continue;
            }

            if (dryRun) {
                // The whole dry run is this branch: match, tally, move on. No
                // .click(), no verify, no backoff -- nothing here can change
                // anything on the account, which is the guarantee being made.
                // Reading the DOM costs nothing, so the click throttle is not
                // needed either; only page advances are still paced.
                stats.would++;
                saveStats(stats);
                pushLog(`would unsave: ${id}`);
                renderCounters(remaining);
                setStatus(`Dry run: would unsave ${id}.`);
                continue;
            }

            setStatus(`Unsaving ${id} ...`);
            const result = await unsaveOne(thing, id);
            if (result === "ok") {
                stats.unsaved++;
                saveStats(stats);
                renderCounters(remaining);
                setStatus(`Unsaved ${id}.`);
                await sleep(jitter(CLICK_DELAY_MIN, CLICK_DELAY_MAX));
            } else {
                stats.uncertain++;
                saveStats(stats);
                pushLog(`uncertain: ${id} (left saved)`);
                renderCounters(remaining);
                // Several consecutive uncertainties => stop and let the user look.
                setStatus(`Uncertain on ${id}; pausing so nothing hammers Reddit.`);
                setRunning(false);
                renderRunning();
                break;
            }
        }

        processing = false;

        if (!isRunning() || stopFlag) {
            setStatus("Paused.");
            return;
        }

        // Page done -> advance politely, or finish.
        const href = nextPageHref();
        if (href) {
            setStatus("Page done. Advancing to next page...");
            await sleep(PAGE_DELAY);
            if (isRunning() && !stopFlag) location.href = href;
        } else {
            setRunning(false);
            renderRunning();
            setStatus(dryRun
                ? `Dry run complete -- would unsave ${stats.would} of ${stats.scanned} `
                  + `post(s) scanned. Nothing was changed.`
                : "Complete -- no more pages in this saved listing.");
        }
    }

    // ------------------------------------------------------------------ controls
    function onStartPause() {
        if (isRunning()) {
            setRunning(false);
            stopFlag = true;
            renderRunning();
            setStatus("Pausing after current item...");
            return;
        }
        if (idSet.size === 0) {
            setStatus("Load a whitelist first.");
            return;
        }
        setRunning(true);
        renderRunning();
        setStatus(isDryRun() ? "Dry run: counting, changing nothing." : "Running LIVE.");
        runLoop();
    }

    // ------------------------------------------------------------------ init
    function init() {
        buildPanel();
        loadWhitelist();
        renderLoaded();
        renderMode();
        renderCounters(pageThings().length);
        renderLog();
        renderRunning();

        // If we auto-advanced here mid-run, resume after a short settle delay.
        if (isRunning() && idSet.size > 0) {
            setStatus("Resuming after page load...");
            setTimeout(runLoop, SETTLE_DELAY);
        } else if (isRunning()) {
            // Running flag set but no list loaded (e.g. list cleared) -> stop.
            setRunning(false);
            renderRunning();
        }
    }

    if (document.body) init();
    else window.addEventListener("DOMContentLoaded", init);
})();
