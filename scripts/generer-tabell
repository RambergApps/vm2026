#!/usr/bin/env python3
"""Genererer gruppetabeller og komplett utslagsoppsett for VM 2026.

Datakilder:
- Gruppespill: FIFA standings-siden via Jina Reader.
- Utslagskamper M73–M104: FIFA Calendar API direkte.

De to datadelene oppdateres uavhengig. Hvis én kilde midlertidig feiler,
beholdes siste gyldige del fra eksisterende ``data/tabell.json``. Filen
overskrives aldri med en ufullstendig gruppetabell eller utslagsbrakett.
"""
from __future__ import annotations

import argparse
import json
import re
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "data" / "tabell.json"
JINA_URL = (
    "https://r.jina.ai/https://www.fifa.com/en/tournaments/"
    "mens/worldcup/canadamexicousa2026/standings"
)
FIFA_API_URL = "https://api.fifa.com/api/v3/calendar/matches"
FIFA_API_PARAMS = {
    "idCompetition": 17,
    "idSeason": 285023,
    "count": 500,
    "language": "en",
}
HTTP_TIMEOUT = 30
EXPECTED_GROUPS = [chr(code) for code in range(ord("A"), ord("L") + 1)]
EXPECTED_KNOCKOUT_MATCHES = list(range(73, 105))
ROUND_RANGES = {
    "r32": range(73, 89),
    "r16": range(89, 97),
    "qf": range(97, 101),
    "sf": range(101, 103),
    "bronze": range(103, 104),
    "final": range(104, 105),
}
GENERIC_PLACEHOLDERS = (
    "tbd",
    "to be determined",
    "winner",
    "runner-up",
    "runner up",
    "third place",
    "3rd place",
)

# FIFA-navn normaliseres til navnene som ellers brukes i appen.
NAME_MAP = {
    "Korea Republic": "South Korea",
    "Czechia": "Czech Republic",
    "Bosnia and Herzegovina": "Bosnia & Herzegovina",
    "Côte d'Ivoire": "Ivory Coast",
    "Türkiye": "Turkey",
    "IR Iran": "Iran",
    "Cabo Verde": "Cape Verde",
    "Congo DR": "DR Congo",
    "United States": "USA",
}

TEAM_ROW_RE = re.compile(
    r"\|\s*\|\s*(?P<place>\d+)\s*\|\s*"
    r"\[!\[[^\]]*\]\((?P<flag>[^)]+)\)\s*"
    r"(?P<name>.*?)\s+(?P<code>[A-Z]{3})\]\([^)]+\)\s*\|\s*"
    r"(?P<played>\d+)\s*\|\s*(?P<wins>\d+)\s*\|\s*"
    r"(?P<draws>\d+)\s*\|\s*(?P<losses>\d+)\s*\|\s*"
    r"(?P<gf>\d+)\s*\|\s*(?P<ga>\d+)\s*\|\s*"
    r"(?P<gd>-?\d+)\s*\|\s*(?P<tcs>-?\d+)\s*\|\s*"
    r"(?P<points>\d+)\s*\|",
    flags=re.DOTALL,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "RambergVMBot/1.0",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def fetch_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    full_url = url + "?" + urlencode(params)
    request = urllib.request.Request(
        full_url,
        headers={
            "User-Agent": "RambergVMBot/1.0",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        data = json.loads(response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError("FIFA API returnerte ikke et JSON-objekt")
    return data


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False
    ) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


# ── GRUPPETABELLER ────────────────────────────────────────────────────────────
def parse_standings(markdown: str) -> dict[str, list[dict[str, Any]]]:
    if "Standings and Group Tables" not in markdown:
        raise ValueError("FIFA/Jina-svaret mangler gruppetabellene")

    groups: dict[str, list[dict[str, Any]]] = {}
    marker_re = re.compile(r"Standings and Group Tables\s*-\s*Group\s+([A-L])")
    markers = list(marker_re.finditer(markdown))

    for index, marker in enumerate(markers):
        group = marker.group(1)
        if group in groups:
            continue
        end = markers[index + 1].start() if index + 1 < len(markers) else len(markdown)
        section = markdown[marker.end():end]
        rows: list[dict[str, Any]] = []
        for match in TEAM_ROW_RE.finditer(section):
            raw_name = re.sub(r"\s+", " ", match.group("name")).strip()
            rows.append(
                {
                    "plass": int(match.group("place")),
                    "lag": NAME_MAP.get(raw_name, raw_name),
                    "fifa_navn": raw_name,
                    "kode": match.group("code"),
                    "flagg_url": match.group("flag").replace(" ", "%20"),
                    "spilt": int(match.group("played")),
                    "seier": int(match.group("wins")),
                    "uavgjort": int(match.group("draws")),
                    "tap": int(match.group("losses")),
                    "maal_for": int(match.group("gf")),
                    "maal_mot": int(match.group("ga")),
                    "maal_forskjell": int(match.group("gd")),
                    "lagdisiplin": int(match.group("tcs")),
                    "poeng": int(match.group("points")),
                }
            )
        if len(rows) == 4:
            groups[group] = sorted(rows, key=lambda row: row["plass"])

    return groups


def validate_groups(groups: dict[str, list[dict[str, Any]]]) -> None:
    missing = [group for group in EXPECTED_GROUPS if len(groups.get(group, [])) != 4]
    if missing:
        raise ValueError(f"Ufullstendig FIFA-tabell. Mangler komplett gruppe: {', '.join(missing)}")
    codes = [row["kode"] for group in EXPECTED_GROUPS for row in groups[group]]
    if len(codes) != 48 or len(set(codes)) != 48:
        raise ValueError("Forventet 48 unike lag i gruppetabellene")


# ── FIFA CALENDAR API / UTSLAGSKAMPER ─────────────────────────────────────────
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


def recursive_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from recursive_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from recursive_strings(nested)


def normalize_team_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return NAME_MAP.get(value, value)


def is_placeholder(value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return True
    low = value.lower()
    if any(word in low for word in GENERIC_PLACEHOLDERS):
        return True
    patterns = (
        r"^[123][A-L]$",       # 1A, 2B, 3C
        r"^3[A-L]{2,}$",       # 3ABCDF
        r"^[WL]\d+$",         # W95 / L95
        r"^RU\d+$",           # RU101
        r"^M\d+$",            # M73
    )
    return any(re.fullmatch(pattern, value, flags=re.I) for pattern in patterns)


def extract_match_number(item: dict[str, Any]) -> int | None:
    candidates = [
        item.get("MatchNumber"),
        item.get("MatchNo"),
        item.get("MatchNumberDisplay"),
        item.get("MatchNumberText"),
        dict_get(item, "Properties", "MatchNumber"),
        dict_get(item, "Properties", "MatchNo"),
    ]
    for candidate in candidates:
        if isinstance(candidate, int) and candidate > 0:
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


def extract_side(item: dict[str, Any], side: str) -> dict[str, Any]:
    side_obj = item.get(side)
    side_obj = side_obj if isinstance(side_obj, dict) else {}
    is_home = side.lower() == "home"
    letter = "A" if is_home else "B"

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

    if team_name and is_placeholder(team_name):
        placeholder = placeholder or team_name
        team_name = ""
        code = ""

    team_name = normalize_team_name(team_name)
    value = team_name or placeholder
    known = bool(team_name and not is_placeholder(team_name))
    return {
        "value": value,
        "code": code if known else "",
        "known": known,
        "placeholder": "" if known else value,
        "object": side_obj,
    }


def normalize_utc(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def extract_kickoff(item: dict[str, Any]) -> str:
    raw = first_nonempty(
        item.get("UtcDate"),
        item.get("UTCDate"),
        item.get("Date"),
        item.get("MatchDate"),
        item.get("LocalDate"),
        dict_get(item, "Properties", "UtcDate"),
        dict_get(item, "Properties", "LocalDate"),
    )
    return normalize_utc(str(raw or ""))


def parse_score(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    match = re.fullmatch(r"\s*(\d+)\s*", str(value))
    return int(match.group(1)) if match else None


def extract_score(item: dict[str, Any], side: str, side_obj: dict[str, Any]) -> int | None:
    is_home = side.lower() == "home"
    prefix = "Home" if is_home else "Away"
    candidates = [
        side_obj.get("Score"),
        side_obj.get("Goals"),
        side_obj.get("GoalsFor"),
        item.get(f"{prefix}Score"),
        item.get(f"Score{prefix}"),
        dict_get(item, "Properties", f"{prefix}Score"),
    ]
    for candidate in candidates:
        score = parse_score(localized_text(candidate) if isinstance(candidate, (dict, list)) else candidate)
        if score is not None:
            return score
    return None


def extract_status(item: dict[str, Any]) -> str:
    return localized_text(
        first_nonempty(
            item.get("MatchStatus"),
            item.get("Status"),
            item.get("MatchStatusName"),
            dict_get(item, "Properties", "MatchStatus"),
            dict_get(item, "Properties", "Status"),
        )
    )


def round_for_match(match_no: int) -> str:
    for round_id, numbers in ROUND_RANGES.items():
        if match_no in numbers:
            return round_id
    raise ValueError(f"Ukjent utslagskamp M{match_no}")


def parse_knockout(api_data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_results = first_nonempty(
        api_data.get("Results"),
        api_data.get("results"),
        api_data.get("Matches"),
        api_data.get("matches"),
    )
    if not isinstance(raw_results, list):
        raise ValueError("FIFA API-svaret mangler en gyldig Results-liste")

    by_number: dict[int, dict[str, Any]] = {}
    quality: dict[int, int] = {}

    for raw_item in raw_results:
        if not isinstance(raw_item, dict):
            continue
        match_no = extract_match_number(raw_item)
        if match_no not in EXPECTED_KNOCKOUT_MATCHES:
            continue

        home = extract_side(raw_item, "Home")
        away = extract_side(raw_item, "Away")
        kickoff = extract_kickoff(raw_item)
        event_id = first_nonempty(raw_item.get("IdMatch"), raw_item.get("MatchId"), raw_item.get("id"))
        entry = {
            "match_no": match_no,
            "fifa_event_id": str(event_id) if event_id is not None else "",
            "runde": round_for_match(match_no),
            "utcDate": kickoff,
            "dato": kickoff[:10] if kickoff else "",
            "hjemme": home["value"],
            "borte": away["value"],
            "hjemme_kode": home["code"],
            "borte_kode": away["code"],
            "hjemme_kjent": home["known"],
            "borte_kjent": away["known"],
            "hjemme_placeholder": home["placeholder"],
            "borte_placeholder": away["placeholder"],
            "status": extract_status(raw_item),
            "hjemme_score": extract_score(raw_item, "Home", home["object"]),
            "borte_score": extract_score(raw_item, "Away", away["object"]),
        }
        entry_quality = (
            int(bool(entry["fifa_event_id"]))
            + int(bool(entry["utcDate"]))
            + int(bool(entry["hjemme"]))
            + int(bool(entry["borte"]))
            + int(entry["hjemme_kjent"])
            + int(entry["borte_kjent"])
            + int(entry["hjemme_score"] is not None)
            + int(entry["borte_score"] is not None)
        )
        if match_no not in by_number or entry_quality > quality[match_no]:
            by_number[match_no] = entry
            quality[match_no] = entry_quality

    return [by_number[number] for number in sorted(by_number)]


def validate_knockout(matches: list[dict[str, Any]]) -> None:
    numbers = [int(match["match_no"]) for match in matches]
    missing = sorted(set(EXPECTED_KNOCKOUT_MATCHES) - set(numbers))
    extras = sorted(set(numbers) - set(EXPECTED_KNOCKOUT_MATCHES))
    duplicates = sorted({number for number in numbers if numbers.count(number) > 1})
    if missing or extras or duplicates:
        details = []
        if missing:
            details.append("mangler " + ", ".join(f"M{number}" for number in missing))
        if extras:
            details.append("uventede " + ", ".join(f"M{number}" for number in extras))
        if duplicates:
            details.append("duplikater " + ", ".join(f"M{number}" for number in duplicates))
        raise ValueError("Ufullstendig FIFA-utslagsbrakett: " + "; ".join(details))

    for round_id, expected_range in ROUND_RANGES.items():
        actual = [match for match in matches if match.get("runde") == round_id]
        if len(actual) != len(list(expected_range)):
            raise ValueError(f"Feil antall kamper i {round_id}: {len(actual)}")


# ── HOVEDPROGRAM ──────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generer data/tabell.json fra FIFA standings og Calendar API.")
    parser.add_argument("--input-file", type=Path, help="Bruk lagret Jina-tekst i stedet for nett")
    parser.add_argument("--fifa-api-file", type=Path, help="Bruk lagret FIFA API-JSON i stedet for nett")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def existing_groups_valid(existing: dict[str, Any]) -> bool:
    try:
        validate_groups(existing.get("grupper", {}))
        return True
    except Exception:
        return False


def existing_knockout_valid(existing: dict[str, Any]) -> bool:
    try:
        validate_knockout(existing.get("utslagskamper", []))
        return True
    except Exception:
        return False


def main() -> int:
    args = parse_args()
    existing = read_json(args.output, {}) if args.output.exists() else {}
    now = utc_now()
    warnings: list[str] = []

    groups: dict[str, list[dict[str, Any]]] | None = None
    group_updated = False
    try:
        text = args.input_file.read_text(encoding="utf-8") if args.input_file else fetch_text(JINA_URL)
        parsed_groups = parse_standings(text)
        validate_groups(parsed_groups)
        groups = {group: parsed_groups[group] for group in EXPECTED_GROUPS}
        group_updated = True
    except Exception as exc:
        if existing_groups_valid(existing):
            groups = existing["grupper"]
            warnings.append(f"Gruppetabell beholdt fra forrige gyldige fil: {exc}")
        else:
            warnings.append(f"Gruppetabell kunne ikke bygges: {exc}")

    knockout: list[dict[str, Any]] | None = None
    knockout_updated = False
    try:
        api_data = (
            read_json(args.fifa_api_file, {})
            if args.fifa_api_file
            else fetch_json(FIFA_API_URL, FIFA_API_PARAMS)
        )
        parsed_knockout = parse_knockout(api_data)
        validate_knockout(parsed_knockout)
        knockout = parsed_knockout
        knockout_updated = True
    except Exception as exc:
        if existing_knockout_valid(existing):
            knockout = existing["utslagskamper"]
            warnings.append(f"Utslagsbrakett beholdt fra forrige gyldige fil: {exc}")
        else:
            warnings.append(f"Utslagsbrakett kunne ikke bygges: {exc}")

    if groups is None or knockout is None:
        print("FEIL: Kan ikke skrive komplett tabell.json")
        for warning in warnings:
            print("  - " + warning)
        print("Eksisterende tabell.json ble ikke overskrevet.")
        return 1

    report = {
        "sist_oppdatert": now,
        "sist_oppdatert_grupper": now if group_updated else existing.get("sist_oppdatert_grupper", existing.get("sist_oppdatert")),
        "sist_oppdatert_utslag": now if knockout_updated else existing.get("sist_oppdatert_utslag", existing.get("sist_oppdatert")),
        "kilde": JINA_URL,
        "kilder": {
            "grupper": JINA_URL,
            "utslag": FIFA_API_URL,
            "utslag_parametre": FIFA_API_PARAMS,
        },
        "oppdatert_i_denne_kjoringen": {
            "grupper": group_updated,
            "utslag": knockout_updated,
        },
        "advarsler": warnings,
        "antall_grupper": 12,
        "antall_lag": 48,
        "antall_utslagskamper": len(knockout),
        "grupper": groups,
        "utslagskamper": knockout,
    }
    atomic_write_json(args.output, report)
    print(
        f"✓ Skrev {args.output} med 12 grupper, 48 lag og "
        f"{len(knockout)} FIFA-utslagskamper (M73–M104)"
    )
    for warning in warnings:
        print("  ADVARSEL: " + warning)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
