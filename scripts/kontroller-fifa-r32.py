#!/usr/bin/env python3
"""
Kontrollerer FIFA sin R32-brakett mot football-data.org uten å endre produksjonsdata.

Hovedprinsipp
-------------
- FIFA sitt kalenderendepunkt brukes som primærkilde for kampnummer M73-M88,
  FIFA event-ID, plassholdere og avspark.
- Jina-uttrekket brukes bare som tilleggskontroll. Det er ikke komplett nok til å
  være primærkilde for hele R32.
- football-data.org brukes for eksisterende fd_match_id og som uavhengig
  kobling på eksakt UTC-avspark.
- Lagnavn kan hentes fra den kilden som først har et komplett, faktisk lagpar.
  FIFA-navn brukes som fallback når football-data.org ennå har tomme lagfelt.
- Endelig kamp-ID godkjennes først når et komplett lagpar er kjent og ingen
  kjente lagopplysninger mellom kildene er i konflikt.

Scriptet skriver kun:
    data/fifa-r32-kontroll.json

Det endrer ikke manuelle-kamper.json, status.json, data.js, tippinger eller HTML.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests


# ── KONFIG ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DEFAULT_OUTPUT = DATA_DIR / "fifa-r32-kontroll.json"
MANUELLE_KAMPER_JSON = DATA_DIR / "manuelle-kamper.json"
STATUS_JSON = DATA_DIR / "status.json"

FIFA_API_URL = "https://api.fifa.com/api/v3/calendar/matches"
FIFA_API_PARAMS = {
    "idCompetition": 17,
    "idSeason": 285023,
    "count": 500,
    "language": "en",
}
JINA_URL = (
    "https://r.jina.ai/https://www.fifa.com/en/tournaments/"
    "mens/worldcup/canadamexicousa2026/standings"
)
FOOTBALL_DATA_URL = "https://api.football-data.org/v4/competitions/WC/matches"

R32_MATCH_NUMBERS = set(range(73, 89))
HTTP_TIMEOUT = 30

# Samme mapping som dagens bygg-r32-flyt skal bruke ved bygging av kamp-ID.
FD_NAVN_TIL_OF_FOR_ID = {
    "Bosnia-Herzegovina": "Bosnia & Herzegovina",
    "Cape Verde Islands": "Cape Verde",
    "Congo DR": "DR Congo",
    "Czechia": "Czech Republic",
}

# Bredere mapping brukes kun til kontroll på tvers av kilder.
SAMMENLIGNINGSNAVN = {
    "Bosnia-Herzegovina": "Bosnia & Herzegovina",
    "Bosnia and Herzegovina": "Bosnia & Herzegovina",
    "Cape Verde Islands": "Cape Verde",
    "Cabo Verde": "Cape Verde",
    "Congo DR": "DR Congo",
    "Czechia": "Czech Republic",
    "Côte d'Ivoire": "Ivory Coast",
    "Cote d'Ivoire": "Ivory Coast",
    "IR Iran": "Iran",
    "Korea Republic": "South Korea",
    "Türkiye": "Turkey",
    "Turkiye": "Turkey",
    "United States": "USA",
    "Curacao": "Curaçao",
}

GENERIC_PLACEHOLDERS = (
    "winner",
    "loser",
    "path",
    "tbd",
    "place",
    "runner",
    "qualified",
    "best third",
)


# ── GENERELLE HJELPEFUNKSJONER ────────────────────────────────────────────────
def iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rens(value: Any) -> str:
    """Samme prinsipp som dagens bygg-r32.yml."""
    return "".join(char if char.isalnum() else "_" for char in str(value or ""))


def kamp_id(hjemme: str, borte: str, dato: str) -> str:
    return f"{rens(hjemme)}_{rens(borte)}_{rens(dato)}"


def sammenligningsnoekkel(navn: str) -> str:
    normalisert = SAMMENLIGNINGSNAVN.get((navn or "").strip(), (navn or "").strip())
    ascii_navn = unicodedata.normalize("NFKD", normalisert).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_navn.lower())


def samme_lag(a: str, b: str) -> bool:
    return bool(a and b and sammenligningsnoekkel(a) == sammenligningsnoekkel(b))


def produksjonsnavn(navn: str) -> str:
    """Normaliserer kildenavn til lagnavnene som brukes i tippeappen."""
    navn = str(navn or "").strip()
    return SAMMENLIGNINGSNAVN.get(navn, navn)


def parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # FIFA-feltet er normalt ISO med offset/Z. Dersom offset mangler, antar vi
        # UTC kun for kontrollrapporten og merker dette på kampen.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normaliser_utc(value: Any) -> str:
    parsed = parse_iso_datetime(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else ""


def parse_iso_date(value: str) -> date | None:
    try:
        return datetime.strptime((value or "")[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def datoavvik_dager(a: str, b: str) -> int | None:
    da = parse_iso_date(a)
    db = parse_iso_date(b)
    if not da or not db:
        return None
    return abs((da - db).days)


def les_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def skriv_json_atomisk(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def hent_tekst(url: str, headers: dict[str, str] | None = None) -> str:
    response = requests.get(url, headers=headers or {}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text


def hent_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = requests.get(url, params=params, headers=headers or {}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"Forventet JSON-objekt fra {url}")
    return data


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
    if any(word in low for word in GENERIC_PLACEHOLDERS):
        return True
    patterns = (
        r"^[123][A-L]$",       # 1A, 2B, 3C
        r"^3[A-L]{2,}$",      # 3ABCDF
        r"^[WL]\d+$",         # W95 / L95
        r"^RU\d+$",           # RU101
        r"^M\d+$",            # M73
    )
    return any(re.fullmatch(pattern, value, flags=re.I) for pattern in patterns)


def recursive_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from recursive_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from recursive_strings(nested)


# ── FIFA KALENDER-API ─────────────────────────────────────────────────────────
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
        if isinstance(candidate, int) and candidate > 0:
            return candidate
        match = re.search(r"(?:^|\b)M?(\d{1,3})(?:\b|$)", str(candidate or ""), flags=re.I)
        if match:
            number = int(match.group(1))
            if 1 <= number <= 104:
                return number

    # Robust reserve dersom feltet er flyttet i FIFA-responsen.
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

    # Enkelte FIFA-svar kan legge placeholderen i ShortClubName.
    if team_name and er_placeholder(team_name):
        placeholder = placeholder or team_name
        team_name = ""
        code = ""

    value = team_name or placeholder
    known = bool(team_name and not er_placeholder(team_name))
    return {
        "verdi": value,
        "fifa_kode": code if known else "",
        "kjent_lag": known,
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


def parse_fifa_api_r32(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_results = first_nonempty(data.get("Results"), data.get("results"), data.get("Matches"), data.get("matches"))
    if not isinstance(raw_results, list):
        raise ValueError("FIFA API-svaret mangler en gyldig Results-liste")

    parsed: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []

    for index, raw_item in enumerate(raw_results):
        if not isinstance(raw_item, dict):
            continue
        match_no = extract_match_number(raw_item)
        if match_no not in R32_MATCH_NUMBERS:
            continue

        kickoff_utc, kickoff_had_timezone = extract_fifa_kickoff(raw_item)
        home = extract_fifa_side(raw_item, "Home")
        away = extract_fifa_side(raw_item, "Away")
        event_id = first_nonempty(raw_item.get("IdMatch"), raw_item.get("MatchId"), raw_item.get("id"))

        if not kickoff_utc:
            notes.append({"match_no": match_no, "advarsel": "FIFA API-kampen mangler gyldig avsparkstid"})
        if not home["verdi"]:
            notes.append({"match_no": match_no, "advarsel": "FIFA API-kampen mangler hjemmeplass"})
        if not away["verdi"]:
            notes.append({"match_no": match_no, "advarsel": "FIFA API-kampen mangler borteplass"})

        parsed.append(
            {
                "fifa_match_no": match_no,
                "fifa_event_id": event_id,
                "fifa_utcDate": kickoff_utc,
                "fifa_dato": kickoff_utc[:10] if kickoff_utc else "",
                "fifa_tid": kickoff_utc[11:16] if kickoff_utc else "",
                "fifa_tid_hadde_tidssone": kickoff_had_timezone,
                "fifa_hjemme": home["verdi"],
                "fifa_borte": away["verdi"],
                "fifa_hjemme_kode": home["fifa_kode"],
                "fifa_borte_kode": away["fifa_kode"],
                "fifa_hjemme_kjent": home["kjent_lag"],
                "fifa_borte_kjent": away["kjent_lag"],
                "fifa_hjemme_placeholder": home["placeholder"],
                "fifa_borte_placeholder": away["placeholder"],
                "fifa_api_result_index": index,
            }
        )

    by_number: dict[int, dict[str, Any]] = {}
    for item in parsed:
        number = item["fifa_match_no"]
        if number not in by_number:
            by_number[number] = item
            continue
        previous = by_number[number]
        fields = ("fifa_event_id", "fifa_utcDate", "fifa_hjemme", "fifa_borte")
        identical = all(previous.get(field) == item.get(field) for field in fields)
        notes.append(
            {
                "match_no": number,
                "type": "fifa_api_duplikat",
                "identisk": identical,
                "forste": {field: previous.get(field) for field in fields},
                "duplikat": {field: item.get(field) for field in fields},
            }
        )

    return [by_number[number] for number in sorted(by_number)], notes


# ── JINA-TILLEGGSKONTROLL ─────────────────────────────────────────────────────
MATCH_HEADING_RE = re.compile(r"^\[M(\d+)\]\((https?://[^)]+)\)\s*$")
DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def fifa_dato_til_iso(value: str) -> str:
    match = DATE_RE.fullmatch((value or "").strip())
    if not match:
        return ""
    mm, dd, yyyy = match.groups()
    return f"{yyyy}-{mm}-{dd}"


def parse_jina_r32(markdown: str) -> list[dict[str, Any]]:
    if not markdown:
        return []
    lines = markdown.splitlines()
    out: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        heading = MATCH_HEADING_RE.fullmatch(lines[index].strip())
        if not heading:
            index += 1
            continue
        match_no = int(heading.group(1))
        url = heading.group(2)
        index += 1
        block: list[str] = []
        while index < len(lines) and not MATCH_HEADING_RE.fullmatch(lines[index].strip()):
            if lines[index].strip().startswith("###") or lines[index].strip().startswith("####"):
                break
            if lines[index].strip():
                block.append(lines[index].strip())
            index += 1
        if match_no not in R32_MATCH_NUMBERS:
            continue
        date_value = next((line for line in block if DATE_RE.fullmatch(line)), "")
        date_index = block.index(date_value) if date_value else -1
        time_value = next((line for line in block[date_index + 1 :] if TIME_RE.fullmatch(line)), "") if date_index >= 0 else ""
        event_match = re.search(r"/(\d+)(?:\?.*)?$", url)
        out.append(
            {
                "fifa_match_no": match_no,
                "fifa_event_id": int(event_match.group(1)) if event_match else None,
                "fifa_dato": fifa_dato_til_iso(date_value),
                "fifa_tid": time_value,
            }
        )
    deduped = {item["fifa_match_no"]: item for item in out}
    return [deduped[number] for number in sorted(deduped)]


# ── FOOTBALL-DATA ─────────────────────────────────────────────────────────────
def er_r32_stage(stage: str) -> bool:
    stage = (stage or "").upper()
    return "ROUND_OF_32" in stage or "LAST_32" in stage


def er_kjent_fd_lag(navn: str) -> bool:
    if not navn:
        return False
    low = navn.lower()
    return not any(word in low for word in GENERIC_PLACEHOLDERS + ("w7", "w8", "w9"))


def parse_football_data_r32(data: dict[str, Any]) -> list[dict[str, Any]]:
    matches = data.get("matches", [])
    if not isinstance(matches, list):
        raise ValueError("football-data.org-svaret mangler en gyldig matches-liste")

    out: list[dict[str, Any]] = []
    for match in matches:
        if not isinstance(match, dict) or not er_r32_stage(str(match.get("stage", ""))):
            continue
        home_raw = str(dict_get(match, "homeTeam", "name") or "").strip()
        away_raw = str(dict_get(match, "awayTeam", "name") or "").strip()
        home_for_id = FD_NAVN_TIL_OF_FOR_ID.get(home_raw, home_raw)
        away_for_id = FD_NAVN_TIL_OF_FOR_ID.get(away_raw, away_raw)
        utc_date = normaliser_utc(match.get("utcDate"))
        date_part = utc_date[:10] if utc_date else ""
        known = er_kjent_fd_lag(home_for_id) and er_kjent_fd_lag(away_for_id) and bool(date_part)
        out.append(
            {
                "fd_match_id": match.get("id"),
                "fd_stage": match.get("stage", ""),
                "fd_status": match.get("status", ""),
                "fd_utcDate": utc_date,
                "fd_dato": date_part,
                "fd_hjemmelag_raw": home_raw,
                "fd_bortelag_raw": away_raw,
                "fd_hjemmelag_for_id": home_for_id,
                "fd_bortelag_for_id": away_for_id,
                "fd_hjemmelag_kjent": er_kjent_fd_lag(home_for_id),
                "fd_bortelag_kjent": er_kjent_fd_lag(away_for_id),
                "kamp_id": kamp_id(home_for_id, away_for_id, date_part) if known else None,
            }
        )
    return sorted(out, key=lambda item: item.get("fd_utcDate", ""))


def kompakt_fd(fd: dict[str, Any]) -> dict[str, Any]:
    return {
        "fd_match_id": fd.get("fd_match_id"),
        "fd_utcDate": fd.get("fd_utcDate", ""),
        "fd_dato": fd.get("fd_dato", ""),
        "fd_hjemmelag": fd.get("fd_hjemmelag_for_id", ""),
        "fd_bortelag": fd.get("fd_bortelag_for_id", ""),
        "kamp_id": fd.get("kamp_id"),
    }


# ── FIFA ↔ FOOTBALL-DATA MATCHING ─────────────────────────────────────────────
def match_fifa_mot_fd(fifa: dict[str, Any], fd_matches: list[dict[str, Any]]) -> dict[str, Any]:
    result = dict(fifa)
    exact_time = [fd for fd in fd_matches if fifa.get("fifa_utcDate") and fd.get("fd_utcDate") == fifa.get("fifa_utcDate")]

    if len(exact_time) == 0:
        result.update(
            {
                "status": "conflict",
                "forklaring": "Fant ingen football-data.org-kamp med identisk UTC-avspark.",
                "kamp_id": None,
                "fd_kandidater": [],
            }
        )
        return result
    if len(exact_time) > 1:
        result.update(
            {
                "status": "conflict",
                "forklaring": "Flere football-data.org-kamper har samme UTC-avspark.",
                "kamp_id": None,
                "fd_kandidater": [kompakt_fd(fd) for fd in exact_time],
            }
        )
        return result

    fd = exact_time[0]
    result.update(kompakt_fd(fd))
    result["koblet_pa"] = "eksakt_utc_avspark"

    fifa_home_known = bool(fifa.get("fifa_hjemme_kjent"))
    fifa_away_known = bool(fifa.get("fifa_borte_kjent"))
    fd_home_known = bool(fd.get("fd_hjemmelag_kjent"))
    fd_away_known = bool(fd.get("fd_bortelag_kjent"))

    # Kontroller faktiske lag straks begge kilder kjenner dem.
    if fifa_home_known and fd_home_known and not samme_lag(fifa.get("fifa_hjemme", ""), fd.get("fd_hjemmelag_for_id", "")):
        result.update({"status": "conflict", "forklaring": "Hjemmelaget avviker mellom FIFA og football-data.org.", "kamp_id": None})
        return result
    if fifa_away_known and fd_away_known and not samme_lag(fifa.get("fifa_borte", ""), fd.get("fd_bortelag_for_id", "")):
        result.update({"status": "conflict", "forklaring": "Bortelaget avviker mellom FIFA og football-data.org.", "kamp_id": None})
        return result

    # Bruk et komplett lagpar fra den kilden som har det først. FD beholdes som
    # førstevalg når begge lag finnes der, fordi dagens kamp-ID-er allerede er
    # normalisert mot FD/OpenFootball. FIFA er trygg fallback når FD ennå bare
    # har kamp-ID og avspark, slik som ved M73 South Africa–Canada.
    selected_home = ""
    selected_away = ""
    team_source = ""
    if fd_home_known and fd_away_known:
        selected_home = produksjonsnavn(fd.get("fd_hjemmelag_for_id", ""))
        selected_away = produksjonsnavn(fd.get("fd_bortelag_for_id", ""))
        team_source = "football_data_org"
    elif fifa_home_known and fifa_away_known:
        selected_home = produksjonsnavn(fifa.get("fifa_hjemme", ""))
        selected_away = produksjonsnavn(fifa.get("fifa_borte", ""))
        team_source = "fifa_api"

    date_part = str(fd.get("fd_dato") or fifa.get("fifa_dato") or "").strip()
    utc_date = str(fd.get("fd_utcDate") or fifa.get("fifa_utcDate") or "").strip()

    if selected_home and selected_away and date_part and utc_date:
        result.update(
            {
                "status": "matched",
                "forklaring": (
                    "FIFA-kampnummer og football-data.org-kamp er entydig koblet på eksakt UTC-avspark; "
                    + (
                        "begge lag er bekreftet hos football-data.org."
                        if team_source == "football_data_org"
                        else "begge lag er bekreftet hos FIFA mens football-data.org ennå mangler komplett lagpar."
                    )
                ),
                "kamp_hjemmelag": selected_home,
                "kamp_bortelag": selected_away,
                "kamp_dato": date_part,
                "kamp_utcDate": utc_date,
                "lagkilde": team_source,
                "kamp_id": kamp_id(selected_home, selected_away, date_part),
            }
        )
        return result

    # Matchnummer og fd_match_id kan kobles nå, men kamp-ID må vente til én av
    # kildene har et komplett faktisk lagpar.
    result.update(
        {
            "status": "pending",
            "forklaring": "FIFA-kampnummer er koblet til fd_match_id på eksakt UTC-avspark; ingen kilde har ennå et komplett faktisk lagpar.",
            "kamp_id": None,
        }
    )
    return result


# ── KONTROLL MOT EKSISTERENDE PRODUKSJONSDATA ─────────────────────────────────
def les_eksisterende_r32() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    manual = les_json(MANUELLE_KAMPER_JSON, {})
    for item in manual.get("kamper", []) if isinstance(manual, dict) else []:
        if item.get("runde") == "r32":
            entries.append(
                {
                    "kilde": "manuelle-kamper.json",
                    "match_no": item.get("match_no"),
                    "kamp_id": item.get("kamp_id"),
                    "hjemme": item.get("hjemmelag", ""),
                    "borte": item.get("bortelag", ""),
                    "dato": item.get("dato", ""),
                }
            )
    status = les_json(STATUS_JSON, {})
    r32_status = status.get("r32", {}) if isinstance(status, dict) else {}
    for item in r32_status.get("kamper", []) if isinstance(r32_status, dict) else []:
        entries.append(
            {
                "kilde": "status.json",
                "match_no": item.get("match_no"),
                "kamp_id": item.get("id") or item.get("kamp_id"),
                "hjemme": item.get("hjemme", ""),
                "borte": item.get("borte", ""),
                "dato": item.get("dato", ""),
            }
        )
    return entries


def legg_til_eksisterende_kontroll(item: dict[str, Any], existing: list[dict[str, Any]]) -> None:
    same_number = []
    for entry in existing:
        try:
            if entry.get("match_no") is not None and int(entry["match_no"]) == int(item["fifa_match_no"]):
                same_number.append(entry)
        except (TypeError, ValueError):
            continue
    if same_number:
        item["eksisterende_koblinger"] = same_number
        if item.get("kamp_id"):
            # Statusfilen inneholder opprinnelige plassholder-ID-er som 2A_2B.
            # De er ikke reelle tidligere kamp-ID-er og skal derfor ikke gi et
            # falskt ID-avvik når kampen for første gang får faktiske lag.
            comparable = [
                entry
                for entry in same_number
                if entry.get("kamp_id")
                and not er_placeholder(str(entry.get("hjemme") or ""))
                and not er_placeholder(str(entry.get("borte") or ""))
            ]
            item["kamp_id_samsvarer_med_eksisterende"] = all(
                entry.get("kamp_id") == item.get("kamp_id")
                for entry in comparable
            )


# ── HOVEDPROGRAM ──────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kontroller FIFA R32 mot dagens kamp-ID-regel.")
    parser.add_argument("--fifa-api-file", type=Path, help="Les lagret FIFA API-JSON i stedet for nett.")
    parser.add_argument("--jina-file", type=Path, help="Les lagret Jina-markdown i stedet for nett.")
    parser.add_argument("--skip-jina", action="store_true", help="Ikke hent Jina-tilleggskontrollen.")
    parser.add_argument("--football-data-file", type=Path, help="Les lagret football-data JSON i stedet for API.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Målfil for kontrollrapport.")
    parser.add_argument(
        "--strict",
        action="store_true",
        default=os.environ.get("STRICT", "0") == "1",
        help="Returner exit code 2 dersom 16 entydige matchnummerkoblinger ikke oppnås.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print("=" * 72)
    print("FIFA R32-kontroll — ingen produksjonsdata endres")
    print("=" * 72)

    try:
        if args.fifa_api_file:
            print(f"Leser FIFA kalender-API fra {args.fifa_api_file}")
            fifa_api_data = json.loads(args.fifa_api_file.read_text(encoding="utf-8"))
        else:
            print("Henter FIFA kalender-API...")
            fifa_api_data = hent_json(
                FIFA_API_URL,
                params=FIFA_API_PARAMS,
                headers={"User-Agent": "Mozilla/5.0 (compatible; RambergVMBot/2.0)", "Accept": "application/json"},
            )
        fifa_matches, fifa_api_notes = parse_fifa_api_r32(fifa_api_data)
        print(f"  → Fant {len(fifa_matches)} unike R32-kamper i FIFA API")

        jina_matches: list[dict[str, Any]] = []
        jina_error = ""
        if not args.skip_jina:
            try:
                if args.jina_file:
                    print(f"Leser Jina-kontroll fra {args.jina_file}")
                    jina_text = args.jina_file.read_text(encoding="utf-8")
                else:
                    print("Henter Jina som tilleggskontroll...")
                    jina_text = hent_tekst(JINA_URL, headers={"User-Agent": "RambergVMBot/2.0"})
                jina_matches = parse_jina_r32(jina_text)
                print(f"  → Fant {len(jina_matches)} R32-kamper i Jina-uttrekket")
            except Exception as exc:
                jina_error = str(exc)
                print(f"  → Jina-kontroll feilet, fortsetter med FIFA API: {exc}")

        if args.football_data_file:
            print(f"Leser football-data.org fra {args.football_data_file}")
            fd_data = json.loads(args.football_data_file.read_text(encoding="utf-8"))
        else:
            token = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()
            if not token:
                raise RuntimeError("FOOTBALL_DATA_TOKEN er ikke satt")
            print("Henter R32-kamper fra football-data.org...")
            fd_data = hent_json(FOOTBALL_DATA_URL, headers={"X-Auth-Token": token})
        fd_matches = parse_football_data_r32(fd_data)
        print(f"  → Fant {len(fd_matches)} R32-kamper hos football-data.org")

        existing = les_eksisterende_r32()
        mapped: list[dict[str, Any]] = []
        for fifa in fifa_matches:
            item = match_fifa_mot_fd(fifa, fd_matches)
            legg_til_eksisterende_kontroll(item, existing)
            mapped.append(item)

        counts = Counter(item.get("status", "ukjent") for item in mapped)
        fifa_numbers = {item["fifa_match_no"] for item in fifa_matches}
        missing_numbers = sorted(R32_MATCH_NUMBERS - fifa_numbers)
        exact_utc_links = sum(1 for item in mapped if item.get("koblet_pa") == "eksakt_utc_avspark")
        id_mismatches = [
            item["fifa_match_no"]
            for item in mapped
            if item.get("kamp_id_samsvarer_med_eksisterende") is False
        ]
        nonidentical_api_duplicates = [
            note for note in fifa_api_notes
            if note.get("type") == "fifa_api_duplikat" and note.get("identisk") is False
        ]

        # Sammenlign event-ID/dato for de kampene Jina faktisk eksponerer.
        api_by_no = {item["fifa_match_no"]: item for item in fifa_matches}
        jina_comparison: list[dict[str, Any]] = []
        for jina in jina_matches:
            api_item = api_by_no.get(jina["fifa_match_no"])
            if not api_item:
                jina_comparison.append({"match_no": jina["fifa_match_no"], "status": "mangler_i_api"})
                continue
            jina_comparison.append(
                {
                    "match_no": jina["fifa_match_no"],
                    "event_id_samsvar": not jina.get("fifa_event_id") or str(jina.get("fifa_event_id")) == str(api_item.get("fifa_event_id")),
                    "dato_samsvar": not jina.get("fifa_dato") or jina.get("fifa_dato") == api_item.get("fifa_dato"),
                    "tid_samsvar": not jina.get("fifa_tid") or jina.get("fifa_tid") == api_item.get("fifa_tid"),
                }
            )

        ready_for_mapping = bool(
            len(fifa_matches) == 16
            and len(fd_matches) == 16
            and exact_utc_links == 16
            and counts.get("conflict", 0) == 0
            and not missing_numbers
            and not nonidentical_api_duplicates
        )
        ready_for_production = bool(
            ready_for_mapping
            and counts.get("matched", 0) == 16
            and not id_mismatches
        )

        report = {
            "generert": iso_utc_now(),
            "formaal": "Kontroll av FIFA R32 mot dagens kamp-ID-regel",
            "produksjonsdata_endret": False,
            "kilder": {
                "fifa_api": str(args.fifa_api_file) if args.fifa_api_file else FIFA_API_URL,
                "fifa_api_params": FIFA_API_PARAMS,
                "fifa_jina": None if args.skip_jina else (str(args.jina_file) if args.jina_file else JINA_URL),
                "football_data": str(args.football_data_file) if args.football_data_file else FOOTBALL_DATA_URL,
            },
            "kamp_id_regel": "rens(komplett lagpar fra FD, ellers FIFA)_rens(football-data utcDate[:10])",
            "oppsummering": {
                "forventet_antall_r32": 16,
                "fifa_api_r32_funnet": len(fifa_matches),
                "fifa_api_match_no_mangler": missing_numbers,
                "fifa_jina_r32_funnet": len(jina_matches),
                "football_data_r32_funnet": len(fd_matches),
                "koblet_entydig_pa_utc_avspark": exact_utc_links,
                "matched": counts.get("matched", 0),
                "pending": counts.get("pending", 0),
                "conflict": counts.get("conflict", 0),
                "kamp_id_avvik_mot_eksisterende": id_mismatches,
                "motstridende_fifa_api_duplikater": [note.get("match_no") for note in nonidentical_api_duplicates],
                "klar_for_matchnummer_mapping": ready_for_mapping,
                "klar_for_produksjonsbruk": ready_for_production,
            },
            "fifa_api_merknader": fifa_api_notes,
            "jina_kontroll": {
                "feil": jina_error,
                "sammenligning": jina_comparison,
            },
            "kamper": mapped,
        }

        skriv_json_atomisk(args.output, report)
        print(f"\nSkrev kontrollrapport: {args.output}")
        print(
            "Status: "
            f"api={len(fifa_matches)}/16, "
            f"utc-koblet={exact_utc_links}/16, "
            f"matched={counts.get('matched', 0)}, "
            f"pending={counts.get('pending', 0)}, "
            f"conflict={counts.get('conflict', 0)}"
        )

        if args.strict and not ready_for_mapping:
            return 2
        return 0

    except Exception as exc:
        print(f"FEIL: {exc}", file=sys.stderr)
        print("Eksisterende kontrollfil ble ikke overskrevet.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
