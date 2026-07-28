"""Fold a second site's store into the one the single gateway will keep.

Both sites ran their own gateway before the store became shared, so each has
a store the other has never seen. server.py deliberately refuses to adopt two
of them on its own: turns carry no timestamps, so no order between the two
histories exists to be discovered, and inventing one writes a false past into
the only record the robot treats as true. That choice belongs to a person,
which is what this tool is for.

Nothing is written without --write. Run it once without, read what it says it
will do, then run it again.

    python -m gateway.merge_memory memory  home.json office.json --turns home-first
    python -m gateway.merge_memory people  home-people.json office-people.json --fold-same-name

Run from the firmware directory, the same place run_gateway.ps1 runs from.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Any

try:
    from . import people
except ImportError:  # invoked as a plain script rather than a module
    import people  # type: ignore[no-redef]

# Matches server.MEMORY_MAX_TURNS. The loader keeps only the last N on read,
# so a merge that produces more is not preserving what it appears to.
MAX_TURNS = 20
MAX_PROFILE_ITEMS = 40
MAX_EMBEDDINGS = 8  # people.PeopleStore.add_embedding's max_samples


def _load(path: str) -> dict[str, Any]:
    # utf-8-sig for the same reason the gateway uses it: these files get
    # hand-corrected in a Windows editor, and a BOM must not make the whole
    # store look unreadable.
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected an object at the top level")
    return data


def _write(path: str, data: dict[str, Any]) -> None:
    if os.path.exists(path):
        backup = f"{path}.before-merge"
        shutil.copyfile(path, backup)
        print(f"  kept a copy of the previous file at {backup}")
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    print(f"  wrote {path}")


def _visibility_rank(visibility: str) -> int:
    return people._VISIBILITY_MIN_RANK.get(visibility, people._ROLE_RANK[people.ROLE_MASTER])


def _normalise_facts(raw: Any) -> list[dict[str, str]]:
    """Accept both shapes the store has had: bare strings and tagged facts."""
    facts: list[dict[str, str]] = []
    for item in raw or []:
        if isinstance(item, str):
            facts.append({"text": item, "visibility": people.VISIBILITY_MASTER})
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            visibility = item.get("visibility")
            facts.append({
                "text": item["text"],
                "visibility": visibility if visibility in people._VISIBILITY_MIN_RANK
                else people.VISIBILITY_MASTER,
            })
    return facts


def merge_memory(args: argparse.Namespace) -> int:
    primary = _load(args.primary)
    secondary = _load(args.secondary)

    p_facts = _normalise_facts(primary.get("profile"))
    s_facts = _normalise_facts(secondary.get("profile"))
    p_turns = [t for t in primary.get("turns", []) if isinstance(t, dict)]
    s_turns = [t for t in secondary.get("turns", []) if isinstance(t, dict)]

    print(f"primary   {args.primary}: {len(p_facts)} facts, {len(p_turns)} turns")
    print(f"secondary {args.secondary}: {len(s_facts)} facts, {len(s_turns)} turns")

    # Facts are individually true regardless of which site heard them, so they
    # union safely. Where the same fact carries two visibilities, the tighter
    # one wins: widening someone's access by accident is the failure that
    # cannot be taken back.
    merged_facts: dict[str, dict[str, str]] = {}
    tightened = 0
    for fact in p_facts + s_facts:
        key = fact["text"].strip()
        existing = merged_facts.get(key)
        if existing is None:
            merged_facts[key] = dict(fact, text=key)
            continue
        if _visibility_rank(fact["visibility"]) > _visibility_rank(existing["visibility"]):
            existing["visibility"] = fact["visibility"]
            tightened += 1
    facts = list(merged_facts.values())[:MAX_PROFILE_ITEMS]
    print(f"\nfacts: {len(p_facts) + len(s_facts)} in, {len(facts)} kept "
          f"({len(p_facts) + len(s_facts) - len(merged_facts)} duplicates, "
          f"{tightened} tightened to the stricter visibility)")

    if p_turns and s_turns and not args.turns:
        print("\nBoth stores hold conversation. Turns carry no timestamps, so the two\n"
              "histories cannot be interleaved -- pick which comes first, or keep one:\n"
              "  --turns home-first | office-first | primary-only | secondary-only\n"
              "('home' means primary, 'office' means secondary.)", file=sys.stderr)
        return 2

    choice = args.turns or ("primary-only" if p_turns else "secondary-only")
    if choice in ("home-first", "primary-first"):
        turns = p_turns + s_turns
    elif choice in ("office-first", "secondary-first"):
        turns = s_turns + p_turns
    elif choice == "primary-only":
        turns = list(p_turns)
    else:
        turns = list(s_turns)

    dropped = max(0, len(turns) - MAX_TURNS)
    turns = turns[-MAX_TURNS:]
    print(f"turns: {choice}, {len(turns)} kept" +
          (f", {dropped} oldest dropped -- the gateway only ever reads the last "
           f"{MAX_TURNS}" if dropped else ""))

    result = {"profile": facts, "turns": turns}
    out = args.out or args.primary
    if not args.write:
        print(f"\ndry run. add --write to save this to {out}")
        return 0
    _write(out, result)
    return 0


def merge_people(args: argparse.Namespace) -> int:
    primary = _load(args.primary).get("people", {})
    secondary = _load(args.secondary).get("people", {})
    if not isinstance(primary, dict) or not isinstance(secondary, dict):
        print("both files must hold a 'people' object", file=sys.stderr)
        return 2

    print(f"primary   {args.primary}: {len(primary)} enrolled")
    print(f"secondary {args.secondary}: {len(secondary)} enrolled")

    merged: dict[str, dict[str, Any]] = {pid: dict(rec) for pid, rec in primary.items()}
    for pid, rec in secondary.items():
        if pid in merged:
            # Ids are random uuids, so a collision means the same record
            # travelled, not two people colliding.
            print(f"  {pid}: present in both, keeping the primary copy")
            continue
        merged[pid] = dict(rec)

    # The same human enrolled at both sites got two random ids and is now two
    # people. Left alone, recognition splits between them: half the samples
    # under each, each matching less often than one record would, and facts
    # filed under whichever id happened to win.
    by_name: dict[str, list[str]] = {}
    for pid, rec in merged.items():
        by_name.setdefault(str(rec.get("name", "")).strip().casefold(), []).append(pid)
    duplicates = {name: pids for name, pids in by_name.items() if name and len(pids) > 1}

    if duplicates and not args.fold_same_name:
        print("\nSame name enrolled more than once:")
        for name, pids in duplicates.items():
            print(f"  {name!r}: {', '.join(pids)}")
        print("Recognition will split between these records. Re-run with --fold-same-name\n"
              "to combine them, or rename one if they really are different people.",
              file=sys.stderr)
        return 2

    folded = 0
    if duplicates and args.fold_same_name:
        for name, pids in duplicates.items():
            keep, *rest = sorted(pids, key=lambda p: merged[p].get("first_seen") or 0)
            target = merged[keep]
            for pid in rest:
                other = merged.pop(pid)
                for modality in ("voice", "face"):
                    samples = list(target.get(modality) or []) + list(other.get(modality) or [])
                    # Samples carry no timestamps, so "keep the most recent"
                    # cannot be honoured exactly; keep the tail, matching what
                    # add_embedding does as samples accumulate.
                    target[modality] = samples[-MAX_EMBEDDINGS:]
                target["encounters"] = (target.get("encounters") or 0) + (other.get("encounters") or 0)
                first = [v for v in (target.get("first_seen"), other.get("first_seen")) if v]
                last = [v for v in (target.get("last_seen"), other.get("last_seen")) if v]
                if first:
                    target["first_seen"] = min(first)
                if last:
                    target["last_seen"] = max(last)
                # Roles disagreeing means one site trusted this person less.
                # Keep that, and let a person raise it deliberately.
                ranks = people._ROLE_RANK
                if ranks.get(other.get("role"), 0) < ranks.get(target.get("role"), 0):
                    target["role"] = other.get("role")
                folded += 1
                print(f"  folded {pid} into {keep} ({name!r})")

    print(f"\npeople: {len(merged)} enrolled after merge" +
          (f", {folded} duplicate record(s) folded" if folded else ""))
    out = args.out or args.primary
    if not args.write:
        print(f"\ndry run. add --write to save this to {out}")
        return 0
    _write(out, {"people": merged})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="what", required=True)

    m = sub.add_parser("memory", help="merge two conversation stores")
    m.add_argument("primary", help="the store to keep (usually the home one)")
    m.add_argument("secondary", help="the store brought from the other site")
    m.add_argument("--turns", choices=["home-first", "office-first", "primary-first",
                                       "secondary-first", "primary-only", "secondary-only"])
    m.add_argument("--out", help="write here instead of over the primary")
    m.add_argument("--write", action="store_true", help="actually write; otherwise dry run")
    m.set_defaults(func=merge_memory)

    p = sub.add_parser("people", help="merge two enrolment registries")
    p.add_argument("primary", help="the registry to keep (usually the home one)")
    p.add_argument("secondary", help="the registry brought from the other site")
    p.add_argument("--fold-same-name", action="store_true",
                   help="combine records that share a name into one person")
    p.add_argument("--out", help="write here instead of over the primary")
    p.add_argument("--write", action="store_true", help="actually write; otherwise dry run")
    p.set_defaults(func=merge_people)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
