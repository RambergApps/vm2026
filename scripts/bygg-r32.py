#!/usr/bin/env python3
"""
Oppdaterer R32-kampene fortløpende fra den verifiserte FIFA-kontrollen.

Forutsetning:
  scripts/kontroller-fifa-r32.py har nettopp skrevet
  data/fifa-r32-kontroll.json.

Scriptet:
- bruker FIFA match_no 73–88 som stabil brakettkobling
- oppretter kamper når kontrollen har et komplett, konfliktfritt lagpar
- bruker football-data.org sin entydige kampkobling/UTC-tid og FIFA-lagnavn som
  fallback når football-data.org ennå har tomme lagfelt
- skriver genererte R32-kamper til data/manuelle-kamper.json
- bevarer andre manuelle kamper og eventuelle ferdige resultater
- endrer ikke status.json direkte; poengregning.py oppdaterer visningen etterpå
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
CONTROL_PATH = DATA_DIR / "fifa-r32-kontroll.json"
MANUAL_PATH = DATA_DIR / "manuelle-kamper.json"
STATUS_PATH = DATA_DIR / "status.json"
GENERATED_SOURCE = "football_data_org_r32"
EXPECTED_MATCH_NUMBERS = set(range(73, 89))


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


def status_slots_by_match_no() -> dict[int, dict[str, str]]:
    status = read_json(STATUS_PATH, {})
    out: dict[int, dict[str, str]] = {}
    for kamp in ((status.get("r32") or {}).get("kamper") or []):
        try:
            match_no = int(kamp.get("match_no"))
        except (TypeError, ValueError):
            continue
        out[match_no] = {
            "slot_hjemme": str(kamp.get("slot_hjemme") or kamp.get("hjemme") or ""),
            "slot_borte": str(kamp.get("slot_borte") or kamp.get("borte") or ""),
        }
    return out


def validate_control(report: dict[str, Any]) -> list[dict[str, Any]]:
    summary = report.get("oppsummering") or {}
    if summary.get("fifa_api_r32_funnet") != 16:
        raise RuntimeError("FIFA-kontrollen inneholder ikke alle 16 R32-kampene")
    if summary.get("koblet_entydig_pa_utc_avspark") != 16:
        raise RuntimeError("Ikke alle 16 FIFA-kampnumre er entydig koblet til football-data.org")
    if summary.get("conflict", 0) != 0:
        raise RuntimeError("FIFA-kontrollen inneholder konflikt(er); ingen data skrives")
    if not summary.get("klar_for_matchnummer_mapping"):
        raise RuntimeError("FIFA-kontrollen er ikke klar for matchnummer-mapping")

    matches = report.get("kamper")
    if not isinstance(matches, list):
        raise RuntimeError("Kontrollfilen mangler en gyldig kampliste")

    numbers = set()
    for item in matches:
        try:
            numbers.add(int(item.get("fifa_match_no")))
        except (TypeError, ValueError):
            pass
    missing = sorted(EXPECTED_MATCH_NUMBERS - numbers)
    if missing:
        raise RuntimeError(f"Kontrollfilen mangler FIFA-kampnumre: {missing}")
    return matches


def build_generated_entry(
    item: dict[str, Any],
    previous: dict[str, Any] | None,
    slots: dict[str, str],
) -> dict[str, Any]:
    match_no = int(item["fifa_match_no"])
    home = str(
        item.get("kamp_hjemmelag")
        or item.get("fd_hjemmelag")
        or item.get("fifa_hjemme")
        or ""
    ).strip()
    away = str(
        item.get("kamp_bortelag")
        or item.get("fd_bortelag")
        or item.get("fifa_borte")
        or ""
    ).strip()
    date = str(item.get("kamp_dato") or item.get("fd_dato") or item.get("fifa_dato") or "").strip()
    utc_date = str(
        item.get("kamp_utcDate")
        or item.get("fd_utcDate")
        or item.get("fifa_utcDate")
        or ""
    ).strip()
    match_id = str(item.get("kamp_id") or "").strip()

    if not all((home, away, date, utc_date, match_id, item.get("fd_match_id"))):
        raise RuntimeError(f"M{match_no} er markert matched, men mangler nødvendig kampdata")

    previous = previous or {}
    preserve_result = bool(previous.get("ferdig")) or (
        previous.get("hjemme") is not None and previous.get("borte") is not None
    )

    entry = {
        "kamp_id": match_id,
        "runde": "r32",
        "gruppe": "",
        "match_no": match_no,
        "dato": date,
        "utcDate": utc_date,
        "fd_match_id": item.get("fd_match_id"),
        "fifa_event_id": item.get("fifa_event_id"),
        "hjemmelag": home,
        "bortelag": away,
        "hjemme": previous.get("hjemme") if preserve_result else None,
        "borte": previous.get("borte") if preserve_result else None,
        "ferdig": bool(previous.get("ferdig")) if preserve_result else False,
        "kilde": GENERATED_SOURCE,
        "lagkilde": item.get("lagkilde", previous.get("lagkilde", "")),
        "slot_hjemme": slots.get("slot_hjemme", previous.get("slot_hjemme", "")),
        "slot_borte": slots.get("slot_borte", previous.get("slot_borte", "")),
    }
    if previous.get("avanserer"):
        entry["avanserer"] = previous["avanserer"]
    return entry


def main() -> int:
    print("=" * 70)
    print("Bygg R32 fortløpende fra verifisert FIFA-kontroll")
    print("=" * 70)

    try:
        report = read_json(CONTROL_PATH, None)
        if not isinstance(report, dict):
            raise RuntimeError(f"Mangler gyldig kontrollfil: {CONTROL_PATH}")
        control_matches = validate_control(report)

        manual = read_json(MANUAL_PATH, {"sist_oppdatert": None, "kamper": []})
        if not isinstance(manual, dict):
            manual = {"sist_oppdatert": None, "kamper": []}
        existing = manual.get("kamper")
        if not isinstance(existing, list):
            existing = []

        preserved = [item for item in existing if item.get("kilde") != GENERATED_SOURCE]
        generated_by_no: dict[int, dict[str, Any]] = {}
        for item in existing:
            if item.get("kilde") != GENERATED_SOURCE:
                continue
            try:
                generated_by_no[int(item.get("match_no"))] = item
            except (TypeError, ValueError):
                continue

        slots_by_no = status_slots_by_match_no()
        newly_ready: list[int] = []
        updated: list[int] = []

        for item in control_matches:
            if item.get("status") != "matched":
                continue
            match_no = int(item["fifa_match_no"])
            previous = generated_by_no.get(match_no)
            new_entry = build_generated_entry(item, previous, slots_by_no.get(match_no, {}))
            if previous is None:
                newly_ready.append(match_no)
            elif previous != new_entry:
                updated.append(match_no)
            generated_by_no[match_no] = new_entry

        new_matches = preserved + [generated_by_no[n] for n in sorted(generated_by_no)]
        old_normalized = json.dumps(existing, ensure_ascii=False, sort_keys=True)
        new_normalized = json.dumps(new_matches, ensure_ascii=False, sort_keys=True)

        if old_normalized == new_normalized:
            print(f"Ingen nye R32-kamper klare. Genererte kamper beholdt: {len(generated_by_no)}")
            return 0

        manual["kamper"] = new_matches
        manual["sist_oppdatert"] = now_utc()
        write_json_atomic(MANUAL_PATH, manual)

        print(f"Skrev {MANUAL_PATH}")
        print(f"Genererte R32-kamper totalt: {len(generated_by_no)}/16")
        if newly_ready:
            print("Nye tippeklare kampkoblinger: " + ", ".join(f"M{n}" for n in newly_ready))
        if updated:
            print("Oppdaterte kampkoblinger: " + ", ".join(f"M{n}" for n in updated))
        return 0
    except Exception as exc:
        print(f"FEIL: {exc}")
        print("Eksisterende data/manuelle-kamper.json ble ikke endret.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
