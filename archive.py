"""Slice 3a: archive routing engine. Given approved manifest entries, compute
where each one belongs per layout.json and (only with --apply) move it out of
staging/ into ARCHIVE_DIR. Dry-run (default) prints the planned table and
touches nothing on disk -- no file moves, no manifest writes. Never creates a
destination directory; anything that doesn't resolve to an existing folder is
flagged for Slice 3b's review UI instead.
"""
import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from manifest import load_manifest, save_manifest
from shortname import load_layout, load_shortname_map, match_shortname

ORIGINAL_RE = re.compile(r"\[\s*original\s*\]", re.IGNORECASE)


@dataclass
class RouteResult:
    action: str  # "move" or "flag"
    dest_dir: Optional[str] = None       # relative to ARCHIVE_DIR, POSIX-style
    filename_suffix: Optional[str] = None  # e.g. "BtR" for shortname style
    note: Optional[str] = None           # e.g. "alias", "OC" -- shown in table
    flag_reason: Optional[str] = None
    flag_detail: Optional[str] = None


def is_original(title: str) -> bool:
    return bool(ORIGINAL_RE.search(title or ""))


def resolve_franchise(tag_name: str, layout: dict):
    """Returns (folder_name_or_None, status) where status is one of
    "aliased", "identity", "unmapped". A None folder_name with status
    "aliased" means an explicit null alias (no folder exists yet)."""
    aliases = layout.get("franchise_aliases", {})
    if tag_name in aliases:
        return aliases[tag_name], "aliased"
    if tag_name in layout.get("franchises", {}):
        return tag_name, "identity"
    return None, "unmapped"


def resolve_character(folder_name: str, franchise_def: dict, character_name: str, layout: dict):
    if not character_name:
        return None
    aliases = layout.get("character_aliases", {}).get(folder_name, {})
    candidate = aliases.get(character_name, character_name)
    if candidate in franchise_def.get("characters", []):
        return candidate
    return None


def route_entry(entry: dict, layout: dict, shortname_entries: list) -> RouteResult:
    franchise_list = entry.get("franchise") or []
    character_list = entry.get("character_guess") or []
    crossover = entry.get("crossover", False)
    title = entry.get("title", "")
    special = layout["special_folders"]
    primary_franchise = franchise_list[0] if franchise_list else None

    if crossover:
        return RouteResult("move", special["crossover"], note="crossover")

    if entry.get("archive_override") == "unknown_source":
        return RouteResult("move", special["others_unknown_source"], note="manual override")

    if entry.get("archive_override") == "known_series":
        code = match_shortname(primary_franchise, shortname_entries) if primary_franchise else None
        if code:
            return RouteResult(
                "move",
                special["others_known_series"],
                filename_suffix=code,
                note="manual override: known series",
            )
        return RouteResult(
            "flag",
            flag_reason="known_series_override_unresolved",
            flag_detail=f"archive_override='known_series' but no shortname entry found for '{primary_franchise}'",
        )

    if primary_franchise:
        folder_name, status = resolve_franchise(primary_franchise, layout)

        if status == "aliased" and folder_name is None:
            return RouteResult(
                "flag",
                flag_reason="no_folder_alias",
                flag_detail=f"franchise_aliases maps '{primary_franchise}' to null -- no folder exists yet",
            )

        if folder_name and folder_name in layout.get("franchises", {}):
            franchise_def = layout["franchises"][folder_name]
            style = franchise_def["style"]
            note = "alias" if status == "aliased" else None

            if style == "flat":
                return RouteResult("move", folder_name, note=note)

            if style == "nested":
                same_series_group = entry.get("same_series_group", False)
                if same_series_group or len(character_list) >= 2:
                    group_dir = f"{folder_name}/{layout['group_subfolder']}"
                    return RouteResult("move", group_dir, note=note)

                char_name = character_list[0] if character_list else None
                matched = resolve_character(folder_name, franchise_def, char_name, layout)
                if matched:
                    return RouteResult("move", f"{folder_name}/{matched}", note=note)

                if franchise_def.get("fallback") == "root":
                    return RouteResult("move", folder_name, note=note)

                return RouteResult(
                    "flag",
                    flag_reason="needs_folder",
                    flag_detail=f"No subfolder for character '{char_name}' in {folder_name}",
                )

        # Unmapped (or aliased/identity to a folder that isn't defined) -> try
        # the shortname file before giving up.
        shortname = match_shortname(primary_franchise, shortname_entries)
        if shortname:
            return RouteResult(
                "move",
                special["others_known_series"],
                filename_suffix=shortname,
                note="shortname",
            )

        if status == "unmapped":
            return RouteResult(
                "flag",
                flag_reason="unresolved_franchise",
                flag_detail=f"Franchise '{primary_franchise}' has no folder or alias configured in layout.json",
            )

        return RouteResult(
            "flag",
            flag_reason="needs_shortname",
            flag_detail=f"No shortname entry for franchise '{primary_franchise}'; propose one",
        )

    if is_original(title):
        return RouteResult("move", special["others_oc"], note="OC")

    return RouteResult("move", special["others_unknown_source"], note="unidentified")


def _dest_filename(local_path: Path, suffix: Optional[str]) -> str:
    if not suffix:
        return local_path.name
    return f"{local_path.stem}_{suffix}{local_path.suffix}"


def _avoid_collision(dest_dir: Path, filename: str):
    """Returns (final_filename, collided). Never overwrites an existing file
    at the destination -- if one is already there, appends _2, _3, ... before
    the extension until landing on a name that doesn't exist yet."""
    dest_path = dest_dir / filename
    if not dest_path.exists():
        return filename, False

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    n = 2
    while True:
        candidate = f"{stem}_{n}{suffix}"
        if not (dest_dir / candidate).exists():
            return candidate, True
        n += 1


def plan_moves(manifest: dict, layout: dict, shortname_entries: list, archive_dir: Path):
    """Returns (rows, skipped_status_counts). Each row is a dict describing
    one approved entry's planned outcome; nothing here touches disk."""
    rows = []
    skipped_status_counts = {}
    special = layout["special_folders"]

    for post_id, entry in manifest.items():
        status = entry.get("status")
        if status != "approved":
            skipped_status_counts[status] = skipped_status_counts.get(status, 0) + 1
            continue

        local_path = Path(entry["local_path"])
        if not local_path.exists():
            rows.append({
                "post_id": post_id,
                "entry": entry,
                "outcome": "missing_file",
                "franchise": entry.get("franchise") or [],
                "characters": entry.get("character_guess") or [],
                "dest_display": "-",
                "note": f"staging file not found: {local_path}",
            })
            continue

        result = route_entry(entry, layout, shortname_entries)

        if result.action == "flag":
            rows.append({
                "post_id": post_id,
                "entry": entry,
                "outcome": "flag",
                "franchise": entry.get("franchise") or [],
                "characters": entry.get("character_guess") or [],
                "dest_display": "-",
                "note": f"{result.flag_reason}: {result.flag_detail}",
                "flag_reason": result.flag_reason,
                "flag_detail": result.flag_detail,
            })
            continue

        dest_dir = archive_dir / result.dest_dir

        if not dest_dir.exists():
            rows.append({
                "post_id": post_id,
                "entry": entry,
                "outcome": "flag",
                "franchise": entry.get("franchise") or [],
                "characters": entry.get("character_guess") or [],
                "dest_display": "-",
                "note": f"missing_directory: {result.dest_dir}/ does not exist under ARCHIVE_DIR",
                "flag_reason": "missing_directory",
                "flag_detail": f"Destination directory '{result.dest_dir}' does not exist under ARCHIVE_DIR",
            })
            continue

        wanted_filename = _dest_filename(local_path, result.filename_suffix)
        dest_filename, collided = _avoid_collision(dest_dir, wanted_filename)
        dest_path = dest_dir / dest_filename

        note_parts = [result.note] if result.note else []
        if collided:
            note_parts.append(f"renamed to avoid overwriting existing '{wanted_filename}'")
        note = f"({', '.join(note_parts)})" if note_parts else ""

        rows.append({
            "post_id": post_id,
            "entry": entry,
            "outcome": "move",
            "franchise": entry.get("franchise") or [],
            "characters": entry.get("character_guess") or [],
            "dest_display": f"{result.dest_dir}/{dest_filename}",
            "note": note,
            "local_path": local_path,
            "dest_path": dest_path,
            "dest_rel": f"{result.dest_dir}/{dest_filename}",
        })

        # Wallpaper copies ride alongside a successful move only -- a flagged
        # or missing-file entry has no fixed archive location yet to copy
        # from, so wallpaper copying waits for a future run once it's routed.
        wallpaper = entry.get("wallpaper", "none")
        wallpaper_targets = []
        if wallpaper in ("pc", "both"):
            wallpaper_targets.append(("pc", special["wallpaper_pc"]))
        if wallpaper in ("phone", "both"):
            wallpaper_targets.append(("phone", special["wallpaper_phone"]))

        for target_name, target_folder in wallpaper_targets:
            wp_dir = archive_dir / special["wallpaper_root"] / target_folder
            wp_dir_rel = f"{special['wallpaper_root']}/{target_folder}"

            if not wp_dir.exists():
                rows.append({
                    "post_id": post_id,
                    "entry": entry,
                    "outcome": "copy_missing_directory",
                    "franchise": entry.get("franchise") or [],
                    "characters": entry.get("character_guess") or [],
                    "dest_display": "-",
                    "note": f"wallpaper dir missing: {wp_dir_rel}/",
                })
                continue

            wp_filename, wp_collided = _avoid_collision(wp_dir, dest_filename)
            wp_note_parts = [f"wallpaper copy ({target_name})"]
            if wp_collided:
                wp_note_parts.append(f"renamed to avoid overwriting existing '{dest_filename}'")

            rows.append({
                "post_id": post_id,
                "entry": entry,
                "outcome": "copy",
                "copy_target": target_name,
                "franchise": entry.get("franchise") or [],
                "characters": entry.get("character_guess") or [],
                "dest_display": f"{wp_dir_rel}/{wp_filename}",
                "note": f"({', '.join(wp_note_parts)})",
                # The copy source is the primary move's destination, which only
                # exists on disk once that move row has actually been applied.
                # plan_moves always appends an entry's copy rows immediately
                # after its move row, and apply_moves processes rows in list
                # order, so the source is guaranteed to exist by the time this
                # copy runs.
                "source_path": dest_path,
                "dest_path": wp_dir / wp_filename,
                "dest_rel": f"{wp_dir_rel}/{wp_filename}",
            })

    return rows, skipped_status_counts


def print_table(rows) -> None:
    if not rows:
        print("(no approved entries to route)")
        return

    columns = ["post_id", "franchise", "character(s)", "action", "destination", "note"]
    table_rows = [columns]
    for row in rows:
        action = {
            "move": "MOVE",
            "flag": "FLAG",
            "missing_file": "MISSING_FILE",
            "copy": "COPY",
            "copy_missing_directory": "COPY_MISSING_DIR",
        }[row["outcome"]]
        table_rows.append([
            row["post_id"],
            ", ".join(row["franchise"]) or "-",
            ", ".join(row["characters"]) or "-",
            action,
            row["dest_display"],
            row["note"],
        ])

    widths = [max(len(r[i]) for r in table_rows) for i in range(len(columns))]
    for r in table_rows:
        print(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)))


def print_summary(rows, skipped_status_counts) -> None:
    would_move = sum(1 for r in rows if r["outcome"] == "move")
    flagged = [r for r in rows if r["outcome"] == "flag"]
    missing_file = sum(1 for r in rows if r["outcome"] == "missing_file")
    copies = [r for r in rows if r["outcome"] == "copy"]
    copy_missing_dir = sum(1 for r in rows if r["outcome"] == "copy_missing_directory")

    flag_breakdown = {}
    for r in flagged:
        flag_breakdown[r["flag_reason"]] = flag_breakdown.get(r["flag_reason"], 0) + 1
    flag_str = ", ".join(f"{k}={v}" for k, v in sorted(flag_breakdown.items())) or "none"

    copy_breakdown = {}
    for r in copies:
        copy_breakdown[r["copy_target"]] = copy_breakdown.get(r["copy_target"], 0) + 1
    copy_str = ", ".join(f"{k}={v}" for k, v in sorted(copy_breakdown.items())) or "none"

    skipped_total = sum(skipped_status_counts.values())
    skipped_str = ", ".join(f"{k}={v}" for k, v in sorted(skipped_status_counts.items())) or "none"

    print()
    print(f"would_move: {would_move}")
    print(f"flagged_for_review: {len(flagged)}  ({flag_str})")
    print(f"missing_staging_file: {missing_file}")
    print(f"wallpaper_copies: {len(copies)}  ({copy_str})")
    print(f"wallpaper_dir_missing: {copy_missing_dir}")
    print(f"skipped (not approved): {skipped_total}  ({skipped_str})")


def apply_moves(manifest: dict, rows) -> None:
    now = datetime.now(timezone.utc).isoformat()

    for row in rows:
        entry = row["entry"]

        if row["outcome"] == "move":
            if row["dest_path"].exists():
                raise RuntimeError(
                    f"Refusing to overwrite existing file at {row['dest_path']} "
                    f"(post_id={row['post_id']}) -- this should have been caught during planning."
                )
            shutil.move(str(row["local_path"]), str(row["dest_path"]))
            entry["status"] = "archived"
            entry["archive_path"] = row["dest_rel"]
            entry["archived_at"] = now
            entry.pop("archive_flag", None)
            entry.pop("archive_flag_detail", None)
            entry.pop("archive_flag_at", None)
        elif row["outcome"] == "flag":
            entry["archive_flag"] = row["flag_reason"]
            entry["archive_flag_detail"] = row["flag_detail"]
            entry["archive_flag_at"] = now
        elif row["outcome"] == "copy":
            if row["dest_path"].exists():
                raise RuntimeError(
                    f"Refusing to overwrite existing wallpaper copy at {row['dest_path']} "
                    f"(post_id={row['post_id']}) -- this should have been caught during planning."
                )
            shutil.copy2(str(row["source_path"]), str(row["dest_path"]))
            entry.setdefault("wallpaper_paths", []).append(row["dest_rel"])
        # "missing_file"/"copy_missing_directory" rows: nothing to write,
        # entry stays approved (or archived, for a copy still pending its
        # wallpaper directory) for a future run to pick up.

    save_manifest(manifest)


def get_archive_dir() -> Path:
    load_dotenv()
    archive_dir_value = os.environ.get("ARCHIVE_DIR")
    if not archive_dir_value:
        print("ARCHIVE_DIR is not set in .env. Add ARCHIVE_DIR=<path to your archive root> and retry.", file=sys.stderr)
        sys.exit(1)

    archive_dir = Path(archive_dir_value)
    if not archive_dir.exists():
        print(f"ARCHIVE_DIR does not exist: {archive_dir}", file=sys.stderr)
        sys.exit(1)

    return archive_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Route approved manifest entries into the archive.")
    parser.add_argument("--apply", "--execute", dest="apply", action="store_true",
                         help="Actually move files and update the manifest. Default is dry-run.")
    args = parser.parse_args()

    archive_dir = get_archive_dir()
    layout = load_layout()
    shortname_entries = load_shortname_map(layout)
    manifest = load_manifest()

    rows, skipped_status_counts = plan_moves(manifest, layout, shortname_entries, archive_dir)
    print_table(rows)
    print_summary(rows, skipped_status_counts)

    if args.apply:
        actionable = [r for r in rows if r["outcome"] in ("move", "flag", "copy")]
        apply_moves(manifest, actionable)
        moved = sum(1 for r in actionable if r["outcome"] == "move")
        flagged = sum(1 for r in actionable if r["outcome"] == "flag")
        copied = sum(1 for r in actionable if r["outcome"] == "copy")
        print()
        print(f"Applied: moved {moved}, flagged {flagged}, wallpaper-copied {copied}.")
    else:
        print()
        print("Dry-run only -- nothing was moved. Re-run with --apply to perform these moves.")


if __name__ == "__main__":
    main()
