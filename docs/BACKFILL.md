# Backfill: getting at saves the RSS feed can't reach

## The symptom

You run the ingester and it says this:

```
downloaded=0
```

Nothing is broken. There is no error, no traceback, no warning. Run it again
tomorrow after saving twenty more posts and it will still say `downloaded=0`.

What has happened is that your Reddit **saved listing has hit its ceiling** —
roughly 1,000 items. Reddit keeps accepting new saves, but they stop appearing
in the listing, and the private RSS feed that `ingest.py` reads is a view onto
that same listing. So the feed keeps returning the same old posts it has always
returned, Oshiire correctly recognises every one of them as already handled, and
reports that it downloaded nothing. It is telling the truth. The problem is
upstream of it.

Two separate things are wrong once you are in this state, and they need
different fixes:

| Problem | Fix |
|---|---|
| Everything you saved *before* the window rolled past it is unreachable | **Backfill** — sweep your full history from Reddit's data export |
| Everything you save *from now on* is invisible, because the listing is full | **Drain** — bulk-unsave posts Oshiire has already captured, freeing headroom |

Do them in that order. Backfill first captures the history, and only then is it
safe to unsave anything, because "safe to unsave" means "provably captured".

This guide is the whole loop. It assumes you have Oshiire set up and working
(see [SETUP.md](SETUP.md)) and that you have your own archive — the numbers and
thresholds in here are examples, not settings to copy.

---

## Prerequisites: request your data export

Reddit will give you a machine-readable dump of your account, including every
post you have ever saved — far past the listing ceiling.

1. Go to <https://www.reddit.com/settings/data-request> (logged in).
2. Request a **GDPR** or **CCPA** export. Either works.
3. Wait. It typically arrives within a day or two, as an email with a download
   link to a zip.
4. Unzip it and find `saved_posts.csv`.
5. Copy that file to `data/saved_posts.csv` in your Oshiire folder.

The file is two columns:

```csv
id,permalink
exa1flat,https://www.reddit.com/r/neonwardgirls/comments/exa1flat/mika_on_the_overpass/
exb2nest,https://www.reddit.com/r/starfallchronicle/comments/exb2nest/seraphine_voss_commission/
```

Bare base-36 post ids (no `t3_` prefix) and a permalink. That is all — no title,
no image URL, no timestamp. Every one of those has to be fetched per post, which
is why the sweep is slow and batched.

> **This file is your personal account data.** It is a complete list of
> everything you have ever saved. `.gitignore` already excludes
> `data/saved_posts.csv`; do not move it somewhere that isn't covered, and do
> not attach it to a bug report.

**Want to try the mechanics before your export arrives?**
`data/saved_posts.example.csv` is a committed four-row file with invented ids.
Point the sweep at it with a copy:

```
copy data\saved_posts.example.csv data\saved_posts.csv
```

The rows are fictional, so every one will come back `dead (removed)` — but you
will see the batching, logging, cursor and summary work end to end. Delete it
before dropping in the real export, or the cursor will be pointing into the
wrong file.

---

## Step 1: build the hash index — do this FIRST

```
python hash_index.py build
```

**Order matters here, and getting it wrong wastes the whole sweep.**

The backfill sweep's entire job is to sort old saves into *"I already own this"*
and *"this is genuinely new"*. It cannot do that by post id: you saved the same
artwork from three different subreddits over the years, and most of what is in
your archive was filed by hand long before Oshiire existed and has no post id
attached to it at all. The only thing that can answer the question is the
picture itself.

So `hash_index.py` walks `ARCHIVE_DIR`, computes a 64-bit perceptual hash
(pHash) of every image, and stores it in a local SQLite index at
`data/archive_index.db`. pHash survives the transforms Reddit applies — rescaling
and JPEG re-encoding — so a Reddit copy of an image lands a small Hamming
distance from the higher-resolution original you already have filed.

Without the index, `backfill.py` refuses to start:

```
No index at data/archive_index.db. Run `python hash_index.py build` first.
```

That refusal is deliberate. If it ran anyway it would classify your entire
back catalogue as new and dump thousands of images you already own into
`staging/` for you to review by hand.

Notes on the build:

- It is **strictly read-only** over `ARCHIVE_DIR`. It never moves, renames,
  writes or deletes anything in your archive. Its only output is the index file,
  which lives under `data/` and is gitignored.
- It is **resumable and incremental**. Interrupt it with Ctrl-C and re-run; it
  keeps what it hashed. On later runs it only hashes files whose size or mtime
  changed.
- It is **slow the first time** — roughly 0.2s per high-resolution image, so a
  large archive is a coffee break. It stats every file under `ARCHIVE_DIR` on
  every run, so allow a few minutes even for a no-op refresh on a cloud-synced
  folder.

### Refresh it after archiving, or duplicates stop being caught

The index is a snapshot, and **a file missing from the index is compared against
nothing.** That is a silent failure — no error, just duplicates sailing through.
Three things keep it current, and all three are needed:

- `archive.py --apply` records each file it files (and each wallpaper copy) as
  it goes, reusing the hash review already computed. Free, no image re-read.
- `launchers/7_maintenance.bat` step 5 re-runs `hash_index.py build`. This is
  the **only** thing that catches images you added to the archive by hand,
  outside the pipeline — which for most people is most of the archive.
- Duplicate detection falls back to an entry's cached hash when the index
  doesn't have it yet. Correct, but narrower: it only covers files the pipeline
  itself handled.

Run `hash_index.py build` yourself after any large manual import.

---

## Step 2: calibrate your own thresholds

```
python calibrate.py run --count 200 --start-date 2023-05-01
```

The sweep sorts each fetched image into three buckets by its distance to the
nearest archived image:

| Distance | Bucket | What happens |
|---|---|---|
| `<= 8` | **owned** | Discarded. No manifest entry, no staging file. |
| `9..11` | **uncertain** | Kept, flagged for you to judge in review. |
| `>= 12` | **new** | Kept as an ordinary `pending_review` entry. |

> ### Do not copy those numbers blindly
>
> `8` and `11` are the constants currently compiled into `backfill.py`
> (`OWNED_MAX`, `UNCERTAIN_MAX`), and they were **measured against one specific,
> densely-populated archive**. They are not a property of pHash and they are not
> a property of your collection.
>
> The threshold is really asking "at what distance do unrelated images start
> colliding by chance?" — and that noise floor depends on how many images you
> have and how similar they are to each other. A large archive of one art style
> has *many* near-collisions and needs a tight threshold. A sparse archive of a
> hundred images across a dozen unrelated series has almost none, and can afford
> a looser one; using `8` there may throw away genuinely new art whose only crime
> was resembling something you own.
>
> Getting it wrong is asymmetric. Too loose and you re-import things you already
> have — annoying, and you catch it at review. **Too tight and the sweep
> silently discards new art without ever creating a manifest entry, so you never
> find out it existed.** Err loose.

`calibrate.py` measures the floor for *your* archive. It takes a slice of saved
posts from a period where you know your archive coverage is good, fetches each
one, and reports the distribution of distances between the Reddit copy and its
nearest archived neighbour. Pick a date range where you are confident you filed
nearly everything you saved.

It is a measurement tool and nothing else: read-only over the CSV and the index,
never writes `manifest.json`, never touches `ARCHIVE_DIR`, and writes only under
`calibration/` (gitignored).

What you are looking for in the output is a **gap** — a cluster of low distances
(the true matches), then empty space, then a rising tail (chance collisions).
Set `OWNED_MAX` at the top of the cluster and `UNCERTAIN_MAX` at the bottom of
the tail. If there is no clean gap, widen the uncertain band rather than
guessing; that band exists precisely to hold the cases the numbers can't settle.

If you have a folder of files you *know* came from that period, cross-check the
result:

```
python calibrate.py ground-truth --folder <that folder>
```

Then edit `OWNED_MAX` and `UNCERTAIN_MAX` near the top of `backfill.py`.

---

## Step 3: run the sweep

```
python backfill.py --limit 500
```

Batched and resumable. Run it, let it work through 500 rows, stop. Run it again
later and it picks up where it left off. On a ~40,000-row export at a couple of
seconds per post, the whole thing is a background chore spread over days — which
is fine, and is the intended way to use it.

### What it does per row

1. **Free dedup.** If the post id is already in `manifest.json` in any status,
   skip it. No fetch, no network.
2. **Fetch the post.** The CSV has no image URL, so it fetches the
   `old.reddit.com` HTML permalink. (Reddit's `.json` and API endpoints return
   403 for this project's User-Agent; the plain HTML page works.)
3. **Classify.** Single image, gallery (each image handled independently), or
   dead.
4. **Download and hash**, then route by distance into owned / uncertain / new.

### Three-way routing

- **owned** — the temp download is deleted immediately. No manifest entry is
  created, so the *only* record that this post was ever processed is the line in
  `backfill_log.jsonl`. That matters later: `export_unsave_list.py` reads the log
  to authorise unsaving these.
- **uncertain** — kept, moved into `staging/`, and the entry is marked
  `backfill_uncertain: true` along with the matched path and distance. Goes into
  the normal review queue with an amber banner.
- **new** — kept as an ordinary `pending_review` entry, metadata-tagged. No AI
  tagging: at this scale it would take days, so that runs later as its own pass.

### Rate limits, and why a 429 is never a dead post

A large fraction of old saves — often half or more, and rising the further back
you go — are genuinely dead: deleted posts, removed images, dead hosts. Dead-link
handling is the *common* path, not an edge case. Which makes one distinction
load-bearing:

**A non-200 response is not how Reddit signals deletion.** A removed post still
renders HTTP 200, with the post body simply absent from the page. So a non-200
is almost always throttling. If the sweep treated it as death it would burn
through hundreds of live, uncaptured posts marking them permanently gone — and
because a `dead` row never becomes a manifest entry, you would have no way to
notice.

The handling:

- **HTTP 429, and transient network errors** — back off `30s, 60s, 120s, 240s`
  and retry, honouring `Retry-After` if it is longer. If a 429 outlives the whole
  schedule, the sweep **stops cleanly**, rewinds the cursor to the row it was on,
  and tells you to try later. That row is retried next run; it is never marked
  dead.
- **A run of 15 consecutive fetch failures** trips a circuit breaker and aborts
  the batch. Blocks of failures like that are soft-throttling, not a coincidental
  cluster of dead links.
- **HTTP 403** is terminal — a restricted or quarantined subreddit, which does
  not change with time or backoff. Logged under its own reason so it stops being
  re-queued forever.
- **404 and 5xx** stay retryable. Those do flip back, and a 5xx is often
  throttling wearing a different hat.

Recover anything caught by the above:

```
python backfill.py --retry-failed
```

This is an orthogonal pass, not a resume: it re-reads the log for ids recorded
`dead`/`fetch_failed` that have not since resolved to any real outcome, and
re-fetches those. It never moves the positional cursor, and it is idempotent —
an id recovered on an earlier run is not re-fetched. It deliberately has *no*
consecutive-failure breaker, because its work list is made of things that already
failed once, so deterministic failures cluster at the head and a streak test
would abort every run in the same first few rows.

### The tombstone gate

Some image hosts serve a **placeholder card** for a deleted image — imgur's
"image does not exist" graphic — with HTTP 200 and an `image/*` content type. The
download succeeds. Nothing about the response says the image is gone. Left alone,
you would accumulate hundreds of identical copies of imgur's error graphic in
your archive.

`tombstones.py` holds perceptual-hash signatures of these placeholders in
`data/tombstones.json`. Every downloaded image is checked against them *before*
routing; a match is classified `tombstone` — dead, deleted, never entered into
the manifest, with its source link written to the log so you can go hunting
manually if it was something you cared about.

That file is committed, unlike everything else under `data/`, because host
placeholders are identical for everyone — it is generic reference data, not
anything derived from your account. To add a host:

```
python tombstones.py add <url-or-file> --label <name> --host <host>
```

Its `hash_bits` must match the index's, or the distances are meaningless; the
sweep warns and disables tombstone matching rather than comparing across depths.

---

## Step 4: review the uncertain band

**This does not resolve itself.** Nothing downstream promotes an uncertain entry
or discards it. It sits in `pending_review` until you look at it.

```
launchers\3_review.bat
```

An uncertain entry shows an **amber** duplicate banner naming the archived image
it resembles and the distance. Amber means the numbers could not decide — that
is the whole point of the band. Compare the two by eye:

- **Already have it** → Reject. The staging file is deleted; the manifest record
  stays forever so it is never re-downloaded.
- **Different image** → treat it like any other entry: tag and Accept.

A **red** banner is the certain band, and shows up in ordinary review too — it
means near-identical, with a one-click reject offered.

---

## Step 5: drain the listing

Now that history is captured, free up the listing so new saves become visible
again.

```
python export_unsave_list.py
```

This builds `data/unsave_list.json` — a flat array of bare post ids that are
**provably captured**. It is read-only: it reads `manifest.json` and
`backfill_log.jsonl` and writes that one file. No network, no `.env`, no secrets.

An id is emitted only if:

- it is in the manifest in **any** status (including `rejected` — you decided
  about it, which is the point), **or**
- the backfill log recorded it `owned` — the image is already in your archive,
  and since owned posts get no manifest entry, the log is the only record, **or**
- the log recorded it `dead` for a reason that means genuinely gone: `removed`,
  `no_image`, `gallery_no_images`.

An id is **withheld** if the log ever recorded it `failed`, or `dead` with reason
`fetch_failed`. Those are transient — the post may be perfectly alive and simply
not captured yet. A taint wins even if the post is otherwise in the manifest: a
gallery can have one image captured and a sibling that failed, and unsaving it
would lose the retry.

### Then run the userscript

Install `oshiire_unsave.user.js` in Tampermonkey/Violentmonkey and open your own
old.reddit saved page. It runs in your authenticated browser and clicks the same
"unsave" links you would by hand — no API calls, no credentials, no network of
its own. Load `unsave_list.json` into the panel with the file picker.

> ### Read this before the live run
>
> **Unsaving cannot be undone from this tool.** There is no re-save button, no
> history and no undo. Oshiire's access to Reddit is read-only — it *cannot* put
> a save back. A post removed by mistake can only be recovered by finding it
> again on Reddit by hand.
>
> **The script starts in dry run and stays there until you say otherwise.** Dry
> run walks the same listing, matches every post against the whitelist, and
> reports how many *would* be unsaved without clicking anything. The live run is
> behind a separate radio button and a confirmation prompt. This mirrors
> `archive.py`, which is dry-run-by-default for the same reason.
>
> **Run the dry run first, every time, and check the number.** If it says 900
> when you expected 40, something is wrong with the whitelist — stop and find out
> what before arming the live run.
>
> **The whitelist contains only provably-captured ids** (see the rules above),
> and the script's default action for any post *not* on that list is to skip it
> untouched. Comments (`t1_`) are never touched at all.
>
> **Drain in small batches and re-check the count between runs.** A few hundred
> at a time, not the whole list in one sitting. Pause is safe at any point;
> progress lives in localStorage and survives the page-to-page navigation.
> Re-running is naturally idempotent, since unsaved posts drop out of the
> listing.

The throttles are deliberately conservative — 2–3s between clicks, 5s between
pages, exponential backoff up to 4 minutes if a toggle doesn't flip — and the run
pauses itself on an uncertain result rather than pressing on.

### The loop

Draining is not a one-shot. It is a cycle, and each turn gives you back a bit
more reach:

```
   drain some captured posts
        ↓
   listing drops below the ceiling
        ↓
   older saves become visible in the listing again
        ↓
   the RSS feed starts returning things it couldn't before
        ↓
   ingest captures them  →  they become "provably captured"
        ↓
   next export adds them to the whitelist
        ↓
   drain some more  ──────┐
        ↑                 │
        └─────────────────┘
```

The `NEW since last export` line in the export summary is the number worth
watching — that is how many posts became drainable since last time.

---

## Steady state

Once the back catalogue is swept, the routine is one launcher:

```
launchers\7_maintenance.bat
```

Five steps, with summaries printed for each:

| Step | What | Why here |
|---|---|---|
| 1 | `backfill.py --limit 500` | Chews through the next slice of history |
| 2 | `export_unsave_list.py` | Refreshes the whitelist with everything newly captured |
| — | 120s cooldown | Both halves hit old.reddit; back-to-back arrives pre-throttled |
| 3 | `ingest.py` | Ordinary RSS ingest of recent saves |
| 4 | `imagemeta.py warm` | Hashes what ingest just downloaded, so review launches instantly |
| 5 | `hash_index.py build` | Re-indexes the archive, catching hand-filed images |

Then load the refreshed `data/unsave_list.json` into the browser userscript and
drain a batch.

**What to run when:**

| Situation | Run |
|---|---|
| Routine upkeep | `7_maintenance.bat` |
| After a big archiving run | Step 5 alone: `hash_index.py build` |
| After a large manual import into the archive | `hash_index.py build` |
| Sweep aborted on rate limits | Wait, then `backfill.py --retry-failed` |
| Just want new saves, nothing else | `1_ingest.bat` |

---

## Troubleshooting

### `downloaded=0` and I have definitely saved things

The listing is full. That is this whole document. Confirm by opening your saved
page and checking whether recent saves appear — if they don't, drain (step 5).

If you have *already* drained and it still says 0, check that the drain actually
removed anything: the userscript's counter distinguishes `unsaved` from
`uncertain`, and a run that ends with everything `uncertain` changed nothing.

### Repeated 429s

Reddit is throttling you. Nothing is broken and nothing is lost — the sweep stops
cleanly and rewinds so the interrupted row is retried.

- Wait. Hours, not minutes; the throttle is per-account and has a long memory.
- Increase `--sleep` (default 2.0s) on the next run.
- Increase `COOLDOWN` in `7_maintenance.bat` if the *ingest* step is what's
  getting throttled — that means the backfill step used up the budget.
- Then `python backfill.py --retry-failed` to recover anything mislabelled.

Do not lower `--sleep` to "get it over with". The sweep is designed to be
finishable over days; hammering gets the whole account throttled harder.

### Dead vs tombstoned vs failed

These look alike in the summary and mean different things:

| Bucket | Meaning | Recoverable? |
|---|---|---|
| `dead` / `removed` | Post deleted or removed. Page rendered, post body absent. | No |
| `dead` / `no_image` | Post exists but links nothing importable (text, video, dead host). | No |
| `dead` / `forbidden` | HTTP 403 — restricted or quarantined subreddit. Terminal. | No |
| `dead` / `fetch_failed` | Fetch never completed. **Says nothing about the post.** | **Yes — `--retry-failed`** |
| `tombstone` | Downloaded fine, but the image is a host's "removed" placeholder. | No, but the source link is in the log |
| `failed` | Download or hash failed after the fetch reached the post. | Sometimes; re-run the row with `--offset` |

Only `fetch_failed` is automatically re-queued. `failed` and `fetch_failed` are
also the two that keep their post ids *out* of the unsave whitelist.

To see what is actually failing and why:

```
python backfill.py --retry-failed --limit 50
```

It prints a `Still failing, by cause` tally — `ReadTimeout`, `HTTP 404`, and so
on. A 4xx there is a permanently gone post, not throttling.

### A stalled cursor

Read `data/backfill/state.json` (see `state.example.json` for an annotated copy):

```json
{ "next_row": 5000, "totals": { "processed": 5000, "...": 0 }, "hash_bits": 64,
  "updated_at": "2026-03-05T18:33:26.760497+00:00" }
```

- **`updated_at` not advancing while a sweep appears to be running** — it is
  wedged, not working. The cursor is written at every checkpoint (every 50 rows),
  always *after* the manifest is saved. Ctrl-C and re-run; you lose at most the
  current checkpoint's work.
- **`next_row` not moving between runs** — the sweep is aborting early every
  time, almost certainly on the rate-limit breaker. Check stderr for the abort
  message.
- **`next_row` past where you expect** — `--offset N` re-runs a range without
  moving the cursor. Re-runs are idempotent, so this is safe.
- **`next_row` equals the CSV row count** — the sweep is finished.

The append-only run log at `data/backfill/backfill_log.jsonl` is the per-post
detail behind those totals (annotated copy: `backfill_log.example.jsonl`). One
JSON object per line, never rewritten. To find out what happened to one post:

```
findstr "exa1flat" data\backfill\backfill_log.jsonl
```

Note that the `totals` buckets are counted **per image** while `processed` is
counted **per row**, so on an export containing galleries the buckets legitimately
sum to more than `processed`.

---

## Appendix: `data/archive_index.db` schema

Plain SQLite. Nothing else in the project opens this file — every write goes
through `hash_index.py`. Generated from the `CREATE TABLE` statements in
[`hash_index.py`](../hash_index.py) (`open_index`):

```sql
CREATE TABLE IF NOT EXISTS images (
    rel_path   TEXT PRIMARY KEY,
    phash      TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime      REAL NOT NULL,
    width      INTEGER,
    height     INTEGER,
    indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

**`images`** — one row per indexed file.

| Column | Notes |
|---|---|
| `rel_path` | POSIX path **relative to `ARCHIVE_DIR`**. This is what joins back to a manifest entry's `archive_path`; both are POSIX and both are archive-relative, so they match exactly. |
| `phash` | Hex-encoded perceptual hash. Its **length is the authority on its depth** (4 bits per hex char), not any caller's belief about it. |
| `size`, `mtime` | Resume keys. A file whose size and mtime are unchanged (within a small float tolerance) is skipped on re-build without being re-hashed. |
| `width`, `height` | Pixel dimensions, nullable. Also what feeds the review UI's wallpaper suggestion. |
| `indexed_at` | UTC ISO-8601 timestamp of the insert. |

**`meta`** — two rows, written once when the index is created:

| Key | Value |
|---|---|
| `algo` | `phash` |
| `hash_bits` | e.g. `64`. Compared on every open; a run requesting a different depth is **refused**, not silently converted. |

That refusal is the important one. Hamming distances between hashes computed at
different depths are meaningless — not merely imprecise — so a mixed-depth index
would poison every comparison made against it, invisibly. The same check guards
`record_indexed_file`, which refuses a wrong-depth row, and the tombstone loader,
which disables matching rather than compare across depths.

`record_indexed_file` also **never creates** the index. An absent database means
this user isn't using duplicate detection at all, and conjuring a one-row index
would be worse than having none — it would look built while covering almost
nothing. `hash_index.py build` stays the only way to create one.
