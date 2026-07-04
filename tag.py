"""Slice 1: run metadata-based character tagging over manifest.json and print
a summary table. Pure/deterministic; no network calls, no AI.
"""
from manifest import load_manifest, save_manifest
from tagger import guess_franchise_and_character

COLUMNS = ["post_id", "franchise", "character_guess", "crossover", "guess_confidence"]


def tag_entry(entry: dict) -> None:
    post_metadata = {"subreddit": entry.get("subreddit", ""), "title": entry.get("title", "")}
    franchise_list, crossover, guess = guess_franchise_and_character(post_metadata)

    entry["franchise"] = franchise_list
    entry["crossover"] = crossover
    entry["character_guess"] = guess.name
    entry["guess_confidence"] = guess.confidence
    entry["guess_source"] = guess.source


def print_table(entries) -> None:
    rows = [COLUMNS] + [
        [
            e["post_id"],
            "[" + ", ".join(e["franchise"]) + "]",
            "[" + ", ".join(e["character_guess"]) + "]",
            str(e["crossover"]).lower(),
            e["guess_confidence"],
        ]
        for e in entries
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(COLUMNS))]
    for row in rows:
        print(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def run_tagging(manifest: dict) -> list:
    tagged = []
    for entry in manifest.values():
        if entry.get("status") != "pending_review":
            continue
        tag_entry(entry)
        tagged.append(entry)
    return tagged


def main() -> None:
    manifest = load_manifest()
    tagged = run_tagging(manifest)
    save_manifest(manifest)
    print_table(tagged)
    print(f"tagged={len(tagged)}")


if __name__ == "__main__":
    main()
