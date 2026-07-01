#!/usr/bin/env python3
"""
Uavhengig FIFA-kontroll for R32.

Formål
------
Sammenligner FIFA Calendar API direkte mot våre lagrede R32-kamper i:
  - data/status.json
  - data/manuelle-kamper.json

Bruker IKKE football-data.org.
Endrer IKKE produksjonsdata.
Skriver kun:
  - data/fifa-only-kontroll.json

Prinsipp
--------
- FIFA er kontrollkilde/master for avsparkstid.
- Våre lagrede kamper kontrolleres på match_no 73-88.
- Klokkeslett-avvik rapporteres tydelig, men er ikke automatisk en feil kampkobling.
- Lag/event-avvik er konflikt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


# ── KONFIG ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
STATUS_JSON = DATA_DIR / "status.json"
MANUELLE_KAMPER_JSON = DATA_DIR / "manuelle-kamper.json"
DEFAULT_OUTPUT = DATA_DIR / "fifa-only-kontroll.json"

FIFA_API_URL = "https://api.fifa.com/api/v3/calendar/matches"
FIFA_API_PARAMS = {
    "idCompetition": 17,
    "idSeason": 285023,
    "count": 500,
    "language": "en",
}
HTTP_TIMEOUT = 30
EXPECTED_R32_MATCH_NUMBERS = set(range(73, 89))

SAMMENLIGNINGSNAVN = {
    "Bosnia-Herzegovina": "Bosnia & Herzegovina",
    "Bosnia and Herzegovina": "Bosnia & Herzegovina",
    "Cape Verde Islands": "Cape Verde",
    "Cabo Verde": "Cape Verde",
    "Congo DR": "DR Congo",
    "Côte d'Ivoire": "Ivory Coast",
    "Cote d'Ivoire": "Ivory Coast",
    "IR Iran": "Iran",
    "Korea Republic": "South Korea",
    "Türkiye": "Turkey",
    "Turkiye": "Turkey",
    "United States": "USA",
    "Curacao": "Curaçao",
}

PLACEHOLDER_WORDS = (
    "winner",
    "loser",
    "path",
    "tbd",
    "place",
    "runner",
    "qualified",
    "best third",
)


# ── HJELPEFUNKSJONER ──────────────────────────────────────────────────────────
def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def les_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Kunne ikke lese JSON: {path}: {exc}") from exc


def skriv_json_atomisk(path: Path, data: Any) -> None:
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


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def dict_get(data: Any, *path: str) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def localized_text(value: Any) -> str:
    """Henter tekst fra FIFA sine lokaliserte tekstobjekter/lister."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        for item in value:
            text = localized_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("Description", "Text", "Name", "Value", "Label"):
            text = localized_text(value.get(key))
            if text:
                return text
        for nested in value.values():
            text = localized_text(nested)
            if text:
                return text
    return ""


def er_placeholder(value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return True
    low = value.lower()
    if any(word in low for word in PLACEHOLDER_WORDS):
        return True
    patterns = (
        r"^[123][A-L]$",       # 1A, 2B, 3C
        r"^3[A-L]{2,}$",      # 3ABCDF
        r"^[WL]\d+$",         # W79 / L101
        r"^RU\d+$",           # RU101
        r"^M\d+$",            # M73
    )
    return any(re.fullmatch(pattern, value, flags=re.I) for pattern in patterns)


def normaliser_lag(navn: str) -> str:
    normalisert = SAMMENLIGNINGSNAVN.get((navn or "").strip(), (navn or "").strip())
    ascii_navn = unicodedata.normalize("NFKD", normalisert).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_navn.lower())


def samme_lag(a: str, b: str) -> bool:
    return bool(a and b and normaliser_lag(a) == normaliser_lag(b))


def parse_iso_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normaliser_utc(value: Any) -> str:
    parsed = parse_iso_utc(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else ""


def avvik_minutter(a: str, b: str) -> int | None:
    da = parse_iso_utc(a)
    db = parse_iso_utc(b)
    if not da or not db:
        return None
    return int(round((db - da).total_seconds() / 60))


def recursive_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from recursive_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from recursive_strings(nested)


# ── FIFA-PARSING ──────────────────────────────────────────────────────────────
def extract_match_number(item: dict[str, Any]) -> int | None:
    direct_candidates = [
        item.get("MatchNumber"),
        item.get("MatchNo"),
        item.get("MatchNumberDisplay"),
        item.get("MatchNumberText"),
        dict_get(item, "Properties", "MatchNumber"),
        dict_get(item, "Properties", "MatchNo"),
    ]
    for candidate in direct_candidates:
        if isinstance(candidate, int) and 1 <= candidate <= 104:
            return candidate
        match = re.search(r"(?:^|\b)M?(\d{1,3})(?:\b|$)", str(candidate or ""), flags=re.I)
        if match:
            number = int(match.group(1))
            if 1 <= number <= 104:
                return number

    for text in recursive_strings(item):
        match = re.fullmatch(r"\s*M(\d{1,3})\s*", text, flags=re.I)
        if match:
            number = int(match.group(1))
            if 1 <= number <= 104:
                return number
    return None


def extract_fifa_side(item: dict[str, Any], side: str) -> dict[str, Any]:
    side_obj = item.get(side)
    side_obj = side_obj if isinstance(side_obj, dict) else {}
    letter = "A" if side.lower() == "home" else "B"

    team_name = localized_text(
        first_nonempty(
            side_obj.get("ShortClubName"),
            side_obj.get("ClubName"),
            side_obj.get("TeamName"),
            side_obj.get("Name"),
            side_obj.get("ShortName"),
        )
    )
    code = localized_text(
        first_nonempty(
            side_obj.get("Abbreviation"),
            side_obj.get("IdCountry"),
            side_obj.get("CountryCode"),
        )
    )
    placeholder = localized_text(
        first_nonempty(
            item.get(f"PlaceHolder{letter}"),
            item.get(f"Placeholder{letter}"),
            item.get(f"Team{letter}Placeholder"),
            dict_get(item, "Properties", f"PlaceHolder{letter}"),
            dict_get(item, "Properties", f"Placeholder{letter}"),
            side_obj.get("PlaceHolder"),
            side_obj.get("Placeholder"),
        )
    )

    if team_name and er_placeholder(team_name):
        placeholder = placeholder or team_name
        team_name = ""
        code = ""

    value = team_name or placeholder
    known = bool(team_name and not er_placeholder(team_name))
    return {
        "lag": SAMMENLIGNINGSNAVN.get(value, value) if known else value,
        "raw": value,
        "kode": code if known else "",
        "kjent": known,
        "placeholder": "" if known else value,
    }


def extract_fifa_kickoff(item: dict[str, Any]) -> tuple[str, bool]:
    candidates = [
        item.get("UtcDate"),
        item.get("UTCDate"),
        item.get("Date"),
        item.get("MatchDate"),
        item.get("LocalDate"),
        dict_get(item, "Properties", "UtcDate"),
        dict_get(item, "Properties", "LocalDate"),
    ]
    raw = first_nonempty(*candidates)
    raw_text = str(raw or "").strip()
    had_timezone = bool(re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", raw_text))
    return normaliser_utc(raw_text), had_timezone


def hent_fifa_r32() -> dict[int, dict[str, Any]]:
    response = requests.get(
        FIFA_API_URL,
        params=FIFA_API_PARAMS,
        headers={"User-Agent": "RambergVMBot/1.0", "Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    raw_results = data.get("Results") if isinstance(data, dict) else None
    if not isinstance(raw_results, list):
        raise RuntimeError("FIFA API-svaret mangler Results-liste")

    out: dict[int, dict[str, Any]] = {}
    duplikater: list[int] = []

    for index, item in enumerate(raw_results):
        if not isinstance(item, dict):
            continue
        match_no = extract_match_number(item)
        if match_no not in EXPECTED_R32_MATCH_NUMBERS:
            continue
        home = extract_fifa_side(item, "Home")
        away = extract_fifa_side(item, "Away")
        utc_date, had_timezone = extract_fifa_kickoff(item)
        event_id = str(first_nonempty(item.get("IdMatch"), item.get("MatchId"), item.get("id")) or "")

        parsed = {
            "match_no": match_no,
            "fifa_event_id": event_id,
            "fifa_utcDate": utc_date,
            "fifa_dato": utc_date[:10] if utc_date else "",
            "fifa_tid": utc_date[11:16] if utc_date else "",
            "fifa_tid_hadde_tidssone": had_timezone,
            "fifa_hjemme": home["lag"],
            "fifa_borte": away["lag"],
            "fifa_hjemme_raw": home["raw"],
            "fifa_borte_raw": away["raw"],
            "fifa_hjemme_kode": home["kode"],
            "fifa_borte_kode": away["kode"],
            "fifa_hjemme_kjent": home["kjent"],
            "fifa_borte_kjent": away["kjent"],
            "fifa_hjemme_placeholder": home["placeholder"],
            "fifa_borte_placeholder": away["placeholder"],
            "fifa_api_result_index": index,
        }

        if match_no in out:
            duplikater.append(match_no)
        out[match_no] = parsed

    if duplikater:
        print(f"ADVARSEL: FIFA API hadde duplikate R32-kampnumre: {sorted(set(duplikater))}")
    return out


# ── LAGREDE KAMPER ────────────────────────────────────────────────────────────
def compact_saved(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    return {
        "kamp_id": item.get("kamp_id") or item.get("id"),
        "match_no": item.get("match_no"),
        "hjemme": item.get("hjemmelag") or item.get("hjemme"),
        "borte": item.get("bortelag") or item.get("borte"),
        "dato": item.get("dato"),
        "utcDate": item.get("utcDate"),
        "fd_match_id": item.get("fd_match_id"),
        "fifa_event_id": item.get("fifa_event_id"),
        "kilde": item.get("kilde"),
        "lagkilde": item.get("lagkilde"),
    }


def saved_by_match_no_from_status() -> dict[int, dict[str, Any]]:
    status = les_json(STATUS_JSON, {})
    out: dict[int, dict[str, Any]] = {}
    for item in ((status.get("r32") or {}).get("kamper") or []):
        if not isinstance(item, dict):
            continue
        try:
            match_no = int(item.get("match_no"))
        except (TypeError, ValueError):
            continue
        out[match_no] = item
    return out


def saved_by_match_no_from_manual() -> dict[int, dict[str, Any]]:
    manual = les_json(MANUELLE_KAMPER_JSON, {"kamper": []})
    out: dict[int, dict[str, Any]] = {}
    for item in manual.get("kamper", []):
        if not isinstance(item, dict) or item.get("runde") != "r32":
            continue
        try:
            match_no = int(item.get("match_no"))
        except (TypeError, ValueError):
            continue
        out[match_no] = item
    return out


# ── SAMMENLIGNING ─────────────────────────────────────────────────────────────
def kontroller_lagret_kilde(kilde_navn: str, lagret: dict[str, Any] | None, fifa: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kilde": kilde_navn,
        "lagret": compact_saved(lagret),
        "funn": [],
        "klokkeslett_avvik": False,
        "conflict": False,
        "ok": False,
    }

    if not fifa:
        result["conflict"] = True
        result["funn"].append("Mangler FIFA-kamp for match_no")
        return result

    if not lagret:
        result["conflict"] = True
        result["funn"].append(f"Mangler lagret R32-kamp i {kilde_navn}")
        return result

    lagret_event = str(lagret.get("fifa_event_id") or "")
    fifa_event = str(fifa.get("fifa_event_id") or "")
    if lagret_event and fifa_event and lagret_event != fifa_event:
        result["conflict"] = True
        result["funn"].append(f"fifa_event_id avviker: lagret={lagret_event}, fifa={fifa_event}")

    lagret_hjemme = str(lagret.get("hjemmelag") or lagret.get("hjemme") or "")
    lagret_borte = str(lagret.get("bortelag") or lagret.get("borte") or "")
    if lagret_hjemme and fifa.get("fifa_hjemme") and not samme_lag(lagret_hjemme, str(fifa.get("fifa_hjemme"))):
        result["conflict"] = True
        result["funn"].append(f"hjemmelag avviker: lagret={lagret_hjemme}, fifa={fifa.get('fifa_hjemme')}")
    if lagret_borte and fifa.get("fifa_borte") and not samme_lag(lagret_borte, str(fifa.get("fifa_borte"))):
        result["conflict"] = True
        result["funn"].append(f"bortelag avviker: lagret={lagret_borte}, fifa={fifa.get('fifa_borte')}")

    lagret_utc = normaliser_utc(lagret.get("utcDate"))
    fifa_utc = str(fifa.get("fifa_utcDate") or "")
    minutes = avvik_minutter(fifa_utc, lagret_utc) if fifa_utc and lagret_utc else None
    result["fifa_utcDate"] = fifa_utc
    result["lagret_utcDate"] = lagret_utc
    result["avvik_minutter"] = minutes
    result["master_tid"] = "fifa"

    if minutes is None:
        result["funn"].append("Kunne ikke sammenligne utcDate")
    elif minutes != 0:
        result["klokkeslett_avvik"] = True
        result["funn"].append(f"klokkeslett avviker med {minutes} minutter: FIFA={fifa_utc}, lagret={lagret_utc}")

    result["ok"] = not result["conflict"] and not result["klokkeslett_avvik"]
    if not result["funn"]:
        result["funn"].append("OK")
    return result


def bygg_rapport() -> dict[str, Any]:
    fifa_by_no = hent_fifa_r32()
    status_by_no = saved_by_match_no_from_status()
    manual_by_no = saved_by_match_no_from_manual()

    kamper: list[dict[str, Any]] = []
    for match_no in sorted(EXPECTED_R32_MATCH_NUMBERS):
        fifa = fifa_by_no.get(match_no)
        status_check = kontroller_lagret_kilde("status.json", status_by_no.get(match_no), fifa)
        manual_check = kontroller_lagret_kilde("manuelle-kamper.json", manual_by_no.get(match_no), fifa)

        checks = [status_check, manual_check]
        has_conflict = any(c.get("conflict") for c in checks)
        has_time_drift = any(c.get("klokkeslett_avvik") for c in checks)

        if has_conflict:
            samlet_status = "conflict"
        elif has_time_drift:
            samlet_status = "klokkeslett_avvik"
        else:
            samlet_status = "ok"

        kamper.append({
            "match_no": match_no,
            "status": samlet_status,
            "fifa": fifa,
            "status_json": status_check,
            "manuelle_kamper_json": manual_check,
        })

    summary = {
        "forventet_antall_r32": 16,
        "fifa_api_r32_funnet": len(fifa_by_no),
        "fifa_api_match_no_mangler": sorted(EXPECTED_R32_MATCH_NUMBERS - set(fifa_by_no)),
        "lagret_status_r32_funnet": len(status_by_no),
        "lagret_manuelle_r32_funnet": len(manual_by_no),
        "ok": sum(1 for k in kamper if k["status"] == "ok"),
        "klokkeslett_avvik": sum(1 for k in kamper if k["status"] == "klokkeslett_avvik"),
        "conflict": sum(1 for k in kamper if k["status"] == "conflict"),
        "master_tid": "fifa",
        "bruker_football_data": False,
        "produksjonsdata_endret": False,
    }

    return {
        "generert": utc_now(),
        "formaal": "Uavhengig FIFA-kontroll av lagrede R32-kamper",
        "produksjonsdata_endret": False,
        "master_tid": "fifa",
        "kilder": {
            "fifa_api": FIFA_API_URL,
            "fifa_api_params": FIFA_API_PARAMS,
            "status_json": str(STATUS_JSON.relative_to(REPO_ROOT)),
            "manuelle_kamper_json": str(MANUELLE_KAMPER_JSON.relative_to(REPO_ROOT)),
            "football_data": None,
        },
        "oppsummering": summary,
        "kamper": kamper,
    }


def print_rapport_kort(rapport: dict[str, Any]) -> None:
    summary = rapport.get("oppsummering", {})
    print("=" * 70)
    print("FIFA-only kontroll")
    print("=" * 70)
    print(
        "R32: "
        f"fifa={summary.get('fifa_api_r32_funnet')}/16, "
        f"ok={summary.get('ok')}/16, "
        f"klokkeslett_avvik={summary.get('klokkeslett_avvik')}, "
        f"conflict={summary.get('conflict')}"
    )
    print("Master for avsparkstid: FIFA")
    print("")

    avvik = [k for k in rapport.get("kamper", []) if k.get("status") != "ok"]
    if not avvik:
        print("Ingen avvik funnet.")
        return

    print("Avvik:")
    for kamp in avvik:
        fifa = kamp.get("fifa") or {}
        print(
            f"M{kamp.get('match_no')} {fifa.get('fifa_hjemme', '?')}–{fifa.get('fifa_borte', '?')} "
            f"[{kamp.get('status')}]"
        )
        for check_key in ("status_json", "manuelle_kamper_json"):
            check = kamp.get(check_key) or {}
            funn = check.get("funn") or []
            if funn and funn != ["OK"]:
                print(f"  {check.get('kilde')}: " + " | ".join(funn))


def main() -> int:
    parser = argparse.ArgumentParser(description="Uavhengig FIFA-kontroll av lagrede R32-kamper")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Hvor rapporten skal skrives")
    parser.add_argument("--strict", action="store_true", help="Returner exit code 2 ved conflict. Klokkeslett-avvik alene feiler ikke.")
    parser.add_argument("--fail-on-time-drift", action="store_true", help="Returner exit code 2 også ved klokkeslett-avvik")
    args = parser.parse_args()

    try:
        rapport = bygg_rapport()
        output = Path(args.output)
        if not output.is_absolute():
            output = REPO_ROOT / output
        skriv_json_atomisk(output, rapport)
        print_rapport_kort(rapport)
        print(f"\nSkrev rapport: {output.relative_to(REPO_ROOT) if output.is_relative_to(REPO_ROOT) else output}")

        summary = rapport.get("oppsummering", {})
        conflict = int(summary.get("conflict") or 0)
        time_drift = int(summary.get("klokkeslett_avvik") or 0)
        if args.strict and conflict:
            return 2
        if args.fail_on_time_drift and time_drift:
            return 2
        return 0
    except Exception as exc:
        print(f"FEIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
