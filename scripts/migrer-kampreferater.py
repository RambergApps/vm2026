#!/usr/bin/env python3
"""Samler tidligere publiserte data/kamppost.json fra Git-historikken.

Kjøres én gang med full Git-historikk (checkout fetch-depth: 0). Eksisterende
kampreferater.json beholdes og utvides. Nyeste publiserte versjon av et referat
vinner, men et endelig referat nedgraderes aldri til foreløpig.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_JS = REPO_ROOT / "data" / "data.js"
KAMPPOST_JSON = REPO_ROOT / "data" / "kamppost.json"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "kampreferater.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_data_js() -> dict[str, Any]:
    text = DATA_JS.read_text(encoding="utf-8")
    text = re.sub(r"^.*?const VM_DATA\s*=\s*", "", text, flags=re.DOTALL).strip().rstrip(";")
    return json.loads(text)


def canonical_for(kamp: dict[str, Any], fallback: str = "") -> str:
    return str(kamp.get("canonical_kamp_id") or kamp.get("alias_for") or kamp.get("kamp_id") or fallback)


def build_current_lookups(vm_data: dict[str, Any]) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    id_map: dict[str, str] = {}
    by_canonical: dict[str, dict[str, Any]] = {}
    for key, kamp in (vm_data.get("resultater") or {}).items():
        canonical = canonical_for(kamp, key)
        by_canonical[canonical] = kamp
        for candidate in (key, kamp.get("kamp_id"), kamp.get("canonical_kamp_id"), kamp.get("alias_for")):
            if candidate:
                id_map[str(candidate)] = canonical
    for _ in range(3):
        for key, value in list(id_map.items()):
            id_map[key] = id_map.get(value, value)
    return id_map, by_canonical


def rank(status: str) -> int:
    return {"midlertidig": 0, "forelopig": 1, "endelig": 2}.get(status, -1)


def merge(entries: dict[str, dict[str, Any]], entry: dict[str, Any]) -> None:
    key = str(entry.get("canonical_kamp_id") or entry.get("kamp_id") or entry.get("fd_match_id") or "")
    if not key:
        return
    old = entries.get(key)
    if old and rank(old.get("referatstatus", "")) > rank(entry.get("referatstatus", "")):
        return
    entries[key] = entry


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, text=True, capture_output=True, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Git-kommando feilet")
    return result.stdout


def revisions() -> list[str]:
    output = git("log", "--format=%H", "--all", "--", "data/kamppost.json", check=False)
    # Eldste først, slik at nyere versjoner kan overstyre.
    return list(reversed([line.strip() for line in output.splitlines() if line.strip()]))


def file_at_revision(revision: str) -> dict[str, Any] | None:
    raw = git("show", f"{revision}:data/kamppost.json", check=False)
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def revision_time(revision: str) -> str:
    return git("show", "-s", "--format=%cI", revision, check=False).strip() or utc_now()


def convert_post(
    post: dict[str, Any], published_at: str, id_map: dict[str, str], by_canonical: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    source_id = str(post.get("kamp_id") or "")
    canonical = id_map.get(source_id, source_id)
    current = by_canonical.get(canonical, {})
    quality = post.get("recap_kvalitet") if isinstance(post.get("recap_kvalitet"), dict) else {}
    is_final = quality.get("status") == "ok" and not quality.get("fallback", False)
    return {
        "kamp_id": source_id or canonical,
        "canonical_kamp_id": canonical,
        "fd_match_id": current.get("fd_match_id") or post.get("fd_match_id"),
        "fd_utcDate": current.get("fd_utcDate") or post.get("fd_utcDate", ""),
        "hjemmelag": post.get("hjemmelag", current.get("hjemmelag", "")),
        "bortelag": post.get("bortelag", current.get("bortelag", "")),
        "hjemme_score": post.get("hjemme_score", current.get("hjemme")),
        "borte_score": post.get("borte_score", current.get("borte")),
        "gruppe": post.get("gruppe", current.get("gruppe", "")),
        "runde": current.get("runde", post.get("runde", "gruppe")),
        "kampstatus": current.get("status", "FINISHED"),
        "referatstatus": "endelig" if is_final else "forelopig",
        "recap_tekst": post.get("recap_tekst", ""),
        "recap_kvalitet": quality,
        "tippinger": post.get("tippinger", {}),
        "kilde": quality.get("fulltekst_kilde") or quality.get("grunnlag") or "",
        "oppdatert": published_at,
    }


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp = Path(handle.name)
    temp.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrer historiske kampposter til kampreferater.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vm_data = load_data_js()
    id_map, by_canonical = build_current_lookups(vm_data)
    existing = load_json(args.output, {})
    entries = existing.get("kamper", {}) if isinstance(existing, dict) else {}
    if isinstance(entries, list):
        entries = {str(item.get("canonical_kamp_id") or item.get("kamp_id") or i): item for i, item in enumerate(entries)}
    if not isinstance(entries, dict):
        entries = {}

    revs = revisions()
    print(f"Fant {len(revs)} historiske versjoner av data/kamppost.json")
    for revision in revs:
        payload = file_at_revision(revision)
        if not payload:
            continue
        published_at = payload.get("generert") or revision_time(revision)
        for post in payload.get("kamper", []) if isinstance(payload.get("kamper"), list) else []:
            merge(entries, convert_post(post, published_at, id_map, by_canonical))

    # Ta også med arbeidskopien dersom den ikke er commitet ennå.
    current = load_json(KAMPPOST_JSON, {})
    for post in current.get("kamper", []) if isinstance(current.get("kamper"), list) else []:
        merge(entries, convert_post(post, current.get("generert") or utc_now(), id_map, by_canonical))

    report = {
        "sist_oppdatert": utc_now(),
        "antall_kamper": len(entries),
        "kamper": dict(sorted(entries.items())),
    }
    atomic_write(args.output, report)
    print(f"✓ Skrev {args.output} med {len(entries)} kampreferater")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
