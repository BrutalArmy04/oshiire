"""Slice 4: run the WD14 AI fallback tagger over manifest.json for entries
the fast metadata path (tag.py) left unresolved. NOT wired into ingest.py --
must be invoked explicitly:  python ai_tag.py [--force]
"""
import argparse
from pathlib import Path

from ai_tagger import guess_franchise_and_character_ai
from manifest import load_manifest, save_manifest
from tag import print_table


def _is_candidate(entry: dict, force: bool) -> bool:
    if entry.get("status") != "pending_review":
        return False
    if entry.get("guess_source") == "manual":
        return False
    if entry.get("guess_confidence") in ("high", "medium"):
        return False           # metadata already resolved it well
    if entry.get("guess_source") == "ai" and not force:
        return False           # already tried; don't re-run the model every invocation
    return True


def run_ai_tagging(manifest: dict, force: bool = False) -> list:
    tagged = []
    for entry in manifest.values():
        if not _is_candidate(entry, force):
            continue
        local_path = entry.get("local_path")
        if not local_path or not Path(local_path).exists():
            continue  # can't run AI without the image; leave metadata result untouched

        franchise_list, guess = guess_franchise_and_character_ai(
            local_path, entry.get("franchise", [])
        )
        entry["franchise"] = franchise_list
        entry["character_guess"] = guess.name
        entry["guess_confidence"] = guess.confidence
        entry["guess_source"] = "ai"
        # status and crossover are deliberately never touched here.
        tagged.append(entry)
    return tagged


def main() -> None:
    parser = argparse.ArgumentParser(description="Slice 4: WD14 AI fallback tagging.")
    parser.add_argument("--force", action="store_true",
                         help="Re-run AI even on entries already guess_source == 'ai'")
    args = parser.parse_args()

    manifest = load_manifest()
    tagged = run_ai_tagging(manifest, force=args.force)
    save_manifest(manifest)
    print_table(tagged)
    print(f"ai_tagged={len(tagged)}")


if __name__ == "__main__":
    main()
