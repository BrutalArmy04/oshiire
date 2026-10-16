"""First-run starter files: create the configs the app cannot start without.

ensure_starters() writes ONLY files that are missing, each atomically, and never
overwrites anything. The starters are EMPTY, never copies of the *.example files
-- those carry invented demo franchises and fake manifest entries that would end
up in a real archive. Paths are relative to the cwd, i.e. the data root (see
datahome.py), so in a git checkout that already has its configs this is a no-op.

app.py calls it after the single-instance lock is held (so no second Oshiire is
writing alongside it) and before `import review`, whose import reads layout.json
and exits when it is missing.
"""
import json
import os
import tempfile
from pathlib import Path

LAYOUT_PATH = Path("layout.json")

# The smallest layout every reader accepts: archive.route_entry indexes all
# seven special_folders keys, load_shortname_map indexes shortname_file, and
# resolve/archive read group_subfolder. The names are the documented defaults
# from layout.example.json.
STARTER_LAYOUT = {
    "group_subfolder": "Others_Group",
    "special_folders": {
        "crossover": "Crossover",
        "wallpaper_root": "Wallpaper",
        "wallpaper_pc": "PC",
        "wallpaper_phone": "Phone",
        "others_oc": "Others/Artist's Original",
        "others_unknown_source": "Others/Unknown Sauce",
        "others_known_series": "Others/Known Series",
    },
    "shortname_file": "known_series_names.txt",
    "franchise_aliases": {},
    "character_aliases": {},
    "franchises": {},
}


def _json_text(data):
    # newline="" on write + an explicit "\n": LF on disk everywhere (see
    # .gitattributes for why the config files keep LF).
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _write_if_missing(path, text):
    """Atomically create `path` with `text` unless it already exists. Returns
    True when the file was written."""
    path = Path(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".starter-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        if path.exists():  # appeared while we wrote -- keep it, drop ours
            os.unlink(tmp)
            return False
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def _shortname_path():
    """The shortname file the layout points at, or None when it should not be
    created here: the layout can't be read, or names an ABSOLUTE path (normally
    a synced archive folder that may simply not be mounted right now -- creating
    an empty file there would be worse than leaving it missing, which every
    reader already treats as 'no shortnames')."""
    try:
        with LAYOUT_PATH.open("r", encoding="utf-8") as fh:
            value = json.load(fh).get("shortname_file")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return None if path.is_absolute() else path


def ensure_starters():
    """Create each missing starter file; return the list of paths written."""
    written = []
    starters = [
        (Path("manifest.json"), _json_text({})),
        (Path("subreddit_map.json"), _json_text({})),
        (Path("data") / "series_aliases.json", _json_text({"aliases": {}})),
        (LAYOUT_PATH, _json_text(STARTER_LAYOUT)),
    ]
    for path, text in starters:
        if _write_if_missing(path, text):
            written.append(path)
    # After layout.json, so a fresh layout's "known_series_names.txt" and an
    # existing layout's own shortname_file are both honoured.
    shortname = _shortname_path()
    if shortname is not None and _write_if_missing(shortname, ""):
        written.append(shortname)
    return written
