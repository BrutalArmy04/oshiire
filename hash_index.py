"""Backfill Stage 0: perceptual-hash index of the local archive.

Standalone, purely local, strictly READ-ONLY over ARCHIVE_DIR. It walks every
image under the archive, computes a 64-bit DCT perceptual hash (pHash), and
stores (rel_path, phash, size, mtime, dimensions) in a local SQLite index so a
later stage can ask: "is this re-encoded/resized Reddit copy the same artwork as
something I already have filed?" -- answered by nearest-neighbour Hamming lookup.

pHash is chosen because it is invariant to scaling and resilient to JPEG
re-encode/compression -- exactly the transforms Reddit applies -- so a Reddit
copy and the higher-res pixiv original hash to a small Hamming distance.

This tool NEVER moves, renames, writes to, or deletes anything under
ARCHIVE_DIR. Its only output is the index file (default `data/archive_index.db`),
which lives under the project dir, never inside ARCHIVE_DIR, and is gitignored.

Usage:
    python hash_index.py build [--hash-size 8] [--db data/archive_index.db]
    python hash_index.py lookup <image_path> [-n 5] [--db data/archive_index.db]
"""
import argparse
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from dotenv import load_dotenv

# Pillow + imagehash are only needed at runtime; import lazily in the functions
# that use them so `--help` and arg parsing work without them installed.

DEFAULT_DB = Path("data/archive_index.db")
DEFAULT_HASH_SIZE = 8  # 8 -> 64-bit hash

# Extensions we attempt to open. Anything else under the archive is ignored.
IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
)

# Compare stored vs current mtime with a small tolerance -- st_mtime is a float
# and can wobble slightly across filesystems/round-trips.
MTIME_EPSILON = 1e-4

COMMIT_EVERY = 500  # flush a batch of inserts this often, so a crash resumes cleanly


@dataclass
class Match:
    """One nearest-neighbour result from the index."""
    distance: int          # Hamming distance, 0..hash_bits
    hash_bits: int         # denominator for readability (e.g. 64)
    rel_path: str          # POSIX path relative to ARCHIVE_DIR
    size: int
    width: Optional[int]
    height: Optional[int]

    @property
    def similarity_pct(self) -> float:
        return 100.0 * (1.0 - self.distance / self.hash_bits)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def get_archive_dir() -> Path:
    """Read ARCHIVE_DIR from .env. Replicated (not imported from archive.py) to
    keep this backfill tool standalone from the pipeline code."""
    load_dotenv()
    value = os.environ.get("ARCHIVE_DIR")
    if not value:
        print(
            "ARCHIVE_DIR is not set in .env. Add ARCHIVE_DIR=<path to your archive root> and retry.",
            file=sys.stderr,
        )
        sys.exit(1)
    archive_dir = Path(value)
    if not archive_dir.exists():
        print(f"ARCHIVE_DIR does not exist: {archive_dir}", file=sys.stderr)
        sys.exit(1)
    return archive_dir


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #
def _configure_pillow() -> None:
    """Make Pillow forgiving: hash slightly-truncated files instead of erroring,
    and raise (but keep finite) the decompression-bomb ceiling for big art."""
    from PIL import Image, ImageFile

    ImageFile.LOAD_TRUNCATED_IMAGES = True
    # Default is ~178M px; large legit artwork can exceed it. Keep a finite cap
    # so a genuinely pathological file still raises (caught + skipped) rather
    # than exhausting memory.
    Image.MAX_IMAGE_PIXELS = 500_000_000


def compute_phash(path: Path, hash_size: int) -> tuple[str, int, int]:
    """Return (hex_phash, width, height) for one image. Raises on unreadable
    images; callers are expected to catch and skip."""
    import imagehash
    from PIL import Image

    with Image.open(path) as img:
        width, height = img.size
        # phash internally grayscales/resizes; force load within the context.
        h = imagehash.phash(img, hash_size=hash_size)
    # imagehash's __str__ is the hex encoding of the flattened bit array.
    return str(h), width, height


def hamming(hex_a: str, hex_b: str) -> int:
    """Hamming distance between two equal-length hex hash strings."""
    return (int(hex_a, 16) ^ int(hex_b, 16)).bit_count()


# --------------------------------------------------------------------------- #
# Index storage (SQLite)
# --------------------------------------------------------------------------- #
def open_index(db_path: Path, hash_size: int) -> sqlite3.Connection:
    """Open/create the index db and verify its hash params match this run."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS images (
               rel_path   TEXT PRIMARY KEY,
               phash      TEXT NOT NULL,
               size       INTEGER NOT NULL,
               mtime      REAL NOT NULL,
               width      INTEGER,
               height     INTEGER,
               indexed_at TEXT
           )"""
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()

    hash_bits = hash_size * hash_size
    stored_bits = conn.execute(
        "SELECT value FROM meta WHERE key = 'hash_bits'"
    ).fetchone()
    if stored_bits is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('algo', 'phash')")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('hash_bits', ?)", (str(hash_bits),)
        )
        conn.commit()
    elif int(stored_bits[0]) != hash_bits:
        conn.close()
        print(
            f"Index {db_path} was built with {stored_bits[0]}-bit hashes but this run "
            f"requests {hash_bits}-bit. Use a different --db or delete the old index.",
            file=sys.stderr,
        )
        sys.exit(1)
    return conn


def read_hash_bits(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'hash_bits'").fetchone()
    return int(row[0]) if row else DEFAULT_HASH_SIZE * DEFAULT_HASH_SIZE


# One definition of the row shape, shared by the bulk build below and the
# single-row insert above it, so the two can never drift apart.
INSERT_SQL = (
    "INSERT OR REPLACE INTO images "
    "(rel_path, phash, size, mtime, width, height, indexed_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def record_indexed_file(
    db_path: Path,
    rel_path: str,
    phash: str,
    size: int,
    mtime: float,
    width: Optional[int],
    height: Optional[int],
) -> bool:
    """Add/refresh ONE row in an EXISTING index. Returns True when written.

    For callers that already hold the file's hash: archive.py files an image
    whose pHash imagemeta.py cached back at review time, so the index can be
    kept current as files land instead of only at the next `build` sweep. No
    image is opened here.

    Deliberately never CREATES the index. An absent db means this user isn't
    using duplicate detection, and conjuring a one-row index would be worse
    than none -- it would look built while covering almost nothing. Likewise
    refuses a hash whose depth differs from the index's: distances across
    depths are meaningless, so a wrong-depth row would silently poison every
    later comparison. Both refusals return False; `build` remains the way to
    (re)create an index.
    """
    if not db_path.exists() or not phash or not rel_path:
        return False
    conn = sqlite3.connect(db_path)
    try:
        # 4 bits per hex character -- the hash's own length is the authority on
        # its depth, not whatever a caller believes it cached.
        if read_hash_bits(conn) != len(phash) * 4:
            return False
        conn.execute(
            INSERT_SQL,
            (rel_path, phash, size, mtime, width, height,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return True


def load_existing(conn: sqlite3.Connection) -> dict[str, tuple[int, float]]:
    """Preload {rel_path: (size, mtime)} for O(1) resume checks during the walk."""
    return {
        rel_path: (size, mtime)
        for rel_path, size, mtime in conn.execute(
            "SELECT rel_path, size, mtime FROM images"
        )
    }


# --------------------------------------------------------------------------- #
# Walk
# --------------------------------------------------------------------------- #
def iter_image_files(root: Path) -> Iterator[Path]:
    """Yield every candidate image file under root, recursively. Read-only: this
    only stats/reads directory entries, never modifies them."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if Path(name).suffix.lower() in IMAGE_EXTENSIONS:
                yield Path(dirpath) / name


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #
def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _print_progress(done: int, total: int, started: float, *, final: bool = False) -> None:
    elapsed = time.monotonic() - started
    rate = done / elapsed if elapsed > 0 else 0.0
    pct = (100.0 * done / total) if total else 100.0
    eta = ((total - done) / rate) if rate > 0 and not final else 0.0
    eta_str = "" if final or eta <= 0 else f"  ETA {_fmt_duration(eta)}"
    line = (
        f"  {done:,} of {total:,} ({pct:5.1f}%)  "
        f"{rate:5.1f} img/s  elapsed {_fmt_duration(elapsed)}{eta_str}"
    )
    end = "\n" if final else ""
    # Pad to clear any leftover characters from a longer previous line.
    print(f"\r{line:<78}", end=end, flush=True)


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build_index(archive_dir: Path, db_path: Path, hash_size: int) -> None:
    if db_path.resolve() == archive_dir.resolve() or db_path.resolve().is_relative_to(
        archive_dir.resolve()
    ):
        # Belt-and-suspenders: the index must never live inside the archive.
        print(
            f"Refusing to write the index inside ARCHIVE_DIR ({archive_dir}). "
            f"Choose a --db path outside the archive.",
            file=sys.stderr,
        )
        sys.exit(1)

    _configure_pillow()
    conn = open_index(db_path, hash_size)
    existing = load_existing(conn)

    print(f"Scanning {archive_dir} for images ...")
    all_files = list(iter_image_files(archive_dir))
    total = len(all_files)
    print(f"Found {total:,} candidate image files. {len(existing):,} already indexed.\n")

    indexed = skipped = failed = 0
    pending = 0
    started = time.monotonic()
    insert = INSERT_SQL

    try:
        for i, path in enumerate(all_files, start=1):
            try:
                stat = path.stat()
                rel_path = path.relative_to(archive_dir).as_posix()
                prev = existing.get(rel_path)
                if prev is not None and prev[0] == stat.st_size and abs(
                    prev[1] - stat.st_mtime
                ) <= MTIME_EPSILON:
                    skipped += 1
                else:
                    phash, width, height = compute_phash(path, hash_size)
                    conn.execute(
                        insert,
                        (
                            rel_path,
                            phash,
                            stat.st_size,
                            stat.st_mtime,
                            width,
                            height,
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    existing[rel_path] = (stat.st_size, stat.st_mtime)
                    indexed += 1
                    pending += 1
                    if pending >= COMMIT_EVERY:
                        conn.commit()
                        pending = 0
            except Exception as exc:  # corrupt/unreadable image -> log & skip
                failed += 1
                # Newline first so the message doesn't collide with the \r line.
                print(
                    f"\n  ! skipped (unreadable): {path} -- {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

            if i % 25 == 0 or i == total:
                _print_progress(i, total, started)

        conn.commit()
        _print_progress(total, total, started, final=True)
    except KeyboardInterrupt:
        conn.commit()  # keep the partial batch so a re-run resumes here
        _print_progress(indexed + skipped + failed, total, started, final=True)
        print("\nInterrupted -- progress saved. Re-run `build` to resume.", file=sys.stderr)
        conn.close()
        sys.exit(130)

    conn.close()
    print(
        f"\nDone. indexed {indexed:,}  |  skipped (already current) {skipped:,}  "
        f"|  failed {failed:,}  |  total rows now {len(existing):,}"
    )
    print(f"Index written to {db_path.resolve()}")


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #
def query_image(
    image_path: Path, top_n: int = 5, db_path: Path = DEFAULT_DB
) -> list[Match]:
    """Return the top-N nearest archive images to `image_path`, ascending by
    Hamming distance. This is the entry point Stage 2 and the calibration run
    call. Distance is 0..hash_bits (see Match.hash_bits) -- small means "same
    image."""
    if not db_path.exists():
        raise FileNotFoundError(
            f"No index at {db_path}. Run `python hash_index.py build` first."
        )
    _configure_pillow()
    conn = sqlite3.connect(db_path)
    try:
        hash_bits = read_hash_bits(conn)
        hash_size = int(round(hash_bits**0.5))
        query_hash, _w, _h = compute_phash(image_path, hash_size)
        rows = conn.execute(
            "SELECT rel_path, phash, size, width, height FROM images"
        ).fetchall()
    finally:
        conn.close()

    q_int = int(query_hash, 16)
    matches = [
        Match(
            distance=(q_int ^ int(phash, 16)).bit_count(),
            hash_bits=hash_bits,
            rel_path=rel_path,
            size=size,
            width=width,
            height=height,
        )
        for rel_path, phash, size, width, height in rows
    ]
    matches.sort(key=lambda m: m.distance)
    return matches[:top_n]


def _print_matches(image_path: Path, matches: list[Match]) -> None:
    if not matches:
        print("Index is empty -- nothing to compare against.")
        return
    bits = matches[0].hash_bits
    print(f"\nNearest {len(matches)} archive image(s) to: {image_path}")
    print(
        f"(pHash Hamming distance out of {bits}; same-image copies are typically "
        f"<= ~{max(6, bits // 8)}.)\n"
    )
    print(f"  {'#':>2}  {'dist':>7}  {'sim':>6}  {'dims':>11}  {'size':>9}  path")
    print(f"  {'-'*2}  {'-'*7}  {'-'*6}  {'-'*11}  {'-'*9}  {'-'*4}")
    for rank, m in enumerate(matches, start=1):
        dims = f"{m.width}x{m.height}" if m.width and m.height else "?"
        size_kb = f"{m.size / 1024:,.0f} KB"
        print(
            f"  {rank:>2}  {m.distance:>3}/{m.hash_bits:<3}  "
            f"{m.similarity_pct:5.1f}%  {dims:>11}  {size_kb:>9}  {m.rel_path}"
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build/query a perceptual-hash index of the local archive (read-only)."
    )
    sub = parser.add_subparsers(dest="command")

    p_build = sub.add_parser("build", help="Walk ARCHIVE_DIR and (re)build the index. Resumable.")
    p_build.add_argument(
        "--hash-size", type=int, default=DEFAULT_HASH_SIZE,
        help=f"pHash size; NxN bits (default {DEFAULT_HASH_SIZE} -> {DEFAULT_HASH_SIZE**2}-bit).",
    )
    p_build.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"Index path (default {DEFAULT_DB}).")

    p_lookup = sub.add_parser("lookup", help="Find the nearest archive images to a given image.")
    p_lookup.add_argument("image", type=Path, help="Image to look up.")
    p_lookup.add_argument("-n", "--top", type=int, default=5, help="How many matches (default 5).")
    p_lookup.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"Index path (default {DEFAULT_DB}).")

    args = parser.parse_args()
    command = args.command or "build"  # build is the default action

    if command == "build":
        archive_dir = get_archive_dir()
        build_index(archive_dir, args.db, args.hash_size)
    elif command == "lookup":
        if not args.image.exists():
            print(f"Image not found: {args.image}", file=sys.stderr)
            sys.exit(1)
        matches = query_image(args.image, top_n=args.top, db_path=args.db)
        _print_matches(args.image, matches)


if __name__ == "__main__":
    main()
