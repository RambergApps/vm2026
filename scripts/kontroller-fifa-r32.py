#!/usr/bin/env python3
"""
Kontrollerer FIFAs R32-brakett mot football-data.org uten å endre produksjonsdata.

Formål
------
1. Hent R32-plasseringene fra FIFA via Jina Reader.
2. Hent R32-kampene fra football-data.org.
3. Match kampene forsiktig.
4. Lag kamp-ID med nøyaktig samme regel som dagens bygg-r32.yml:
      rens(hjemmelag) + "_" + rens(bortelag) + "_" + rens(utcDate[:10])
5. Skriv kun data/fifa-r32-kontroll.json.

Scriptet endrer IKKE:
- data/manuelle-kamper.json
- data/status.json
- data/data.js
- tippinger eller HTML-filer

Normal bruk i repo:
    python scripts/kontroller-fifa-r32.py

Miljøvariabel:
    FOOTBALL_DATA_TOKEN  Påkrevd ved live-kjøring.

Lokal test med lagrede svar:
    python scripts/kontroller-fifa-r32.py \
      --jina-file fifa-standings-jina.txt \
      --football-data-file football-data.json \
      --output fifa-r32-kontroll.json
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
from typing import Any

import requests


# ── KONFIG ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DEFAULT_OUTPUT = DATA_DIR / "fifa-r32-kontroll.json"
MANUELLE_KAMPER_JSON = DATA_DIR / "manuelle-kamper.json"
STATUS_JSON = DATA_DIR / "status.json"

JINA_URL = (
    "https://r.jina.ai/https://www.fifa.com/en/tournaments/"
    "mens/worldcup/canadamexicousa2026/standings"
)
FOOTBALL_DATA_URL = "https://api.football-data.org/v4/competitions/WC/matches"

R32_MATCH_NUMBERS = set(range(73, 89))
HTTP_TIMEOUT = 25

# Dette er med vilje samme mapping som i nåværende bygg-r32.yml.
# Den påvirker selve kamp-ID-en og skal derfor ikke utvides i dette kontrollsteget.
FD_NAVN_TIL_OF_FOR_ID = {
    "Bosnia-Herzegovina": "Bosnia & Herzegovina",
    "Cape Verde Islands": "Cape Verde",
    "Congo DR": "DR Congo",
    "Czechia": "Czech Republic",
}

# Bredere mapping brukes KUN for sammenligning mellom kildene.
# Den endrer aldri kamp-ID-en som dagens bygg-r32.yml ville ha laget.
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
)


# ── GENERELLE HJELPEFUNKSJONER ────────────────────────────────────────────────
def iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rens(s: Any) -> str:
    """Identisk prinsipp som dagens bygg-r32.yml."""
    return "".join(c if c.isalnum() else "_" for c in str(s or ""))


def kamp_id(hjemme: str, borte: str, dato: str) -> str:
    """Kamp-ID etter nøyaktig samme regel som dagens bygg-r32.yml."""
    return f"{rens(hjemme)}_{rens(borte)}_{rens(dato)}"


def sammenligningsnoekkel(navn: str) -> str:
    normalisert = SAMMENLIGNINGSNAVN.get((navn or "").strip(), (navn or "").strip())
    ascii_navn = unicodedata.normalize("NFKD", normalisert).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_navn.lower())


def parse_iso_date(value: str) -> date | None:
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def hent_tekst(url: str, headers: dict[str, str] | None = None) -> str:
    r = requests.get(url, headers=headers or {}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text


def hent_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    r = requests.get(url, headers=headers or {}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError(f"Forventet JSON-objekt fra {url}")
    return data


# ── FIFA/JINA-PARSER ──────────────────────────────────────────────────────────
MATCH_HEADING_RE = re.compile(r"^\[M(\d+)\]\((https?://[^)]+)\)\s*$")
IMAGE_RE = re.compile(r"^!\[[^\]]*\]\((https?://[^)]+)\)\s*$")
DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


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
        r"^W\d+$",            # W95
        r"^L\d+$",            # L95
        r"^RU\d+$",           # RU101
    )
    return any(re.fullmatch(pattern, value, flags=re.I) for pattern in patterns)


def fifa_dato_til_iso(value: str) -> str:
    m = DATE_RE.fullmatch((value or "").strip())
    if not m:
        return ""
    mm, dd, yyyy = m.groups()
    return f"{yyyy}-{mm}-{dd}"


def rens_blokklinjer(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if line.strip()]


def parse_fifa_sider(rest_lines: list[str]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """
    Tolker de to sidene i én FIFA-kampblokk.

    Faktisk lag presenteres vanligvis slik i Jina:
        Germany
        ![Image ...](...)
        GER

    Placeholders presenteres som én linje, for eksempel 1I eller 3ABCDF.
    """
    lines = rens_blokklinjer(rest_lines)
    elements: list[tuple[int, dict[str, Any]]] = []
    consumed: set[int] = set()
    warnings: list[str] = []

    # Finn faktiske lag ved å bruke flaggbildet som et sikkert anker.
    for i, line in enumerate(lines):
        image_match = IMAGE_RE.fullmatch(line)
        if not image_match:
            continue

        prev_i = i - 1
        while prev_i >= 0 and prev_i in consumed:
            prev_i -= 1
        next_i = i + 1
        while next_i < len(lines) and next_i in consumed:
            next_i += 1

        if prev_i < 0 or next_i >= len(lines):
            warnings.append("Ufullstendig FIFA-lagblokk rundt flaggbilde")
            continue

        navn = lines[prev_i]
        kode = lines[next_i]
        if er_placeholder(navn) or not re.fullmatch(r"[A-Z0-9]{3}", kode):
            warnings.append(f"Kunne ikke tolke faktisk lag rundt flagg: navn='{navn}', kode='{kode}'")
            continue

        elements.append(
            (
                prev_i,
                {
                    "verdi": navn,
                    "fifa_kode": kode,
                    "flagg_url": image_match.group(1),
                    "kjent_lag": True,
                },
            )
        )
        consumed.update({prev_i, i, next_i})

    # Ubrukte linjer er normalt placeholders. Rundeetiketter ignoreres.
    ignored_labels = {"final", "play-off for third place", "third place play-off"}
    for i, line in enumerate(lines):
        if i in consumed or IMAGE_RE.fullmatch(line):
            continue
        if line.lower() in ignored_labels:
            continue
        elements.append(
            (
                i,
                {
                    "verdi": line,
                    "fifa_kode": "",
                    "flagg_url": "",
                    "kjent_lag": not er_placeholder(line),
                },
            )
        )

    elements.sort(key=lambda item: item[0])
    sides = [item[1] for item in elements]

    if len(sides) < 2:
        warnings.append(f"Fant bare {len(sides)} side(r) i FIFA-blokken")
        while len(sides) < 2:
            sides.append({"verdi": "", "fifa_kode": "", "flagg_url": "", "kjent_lag": False})
    elif len(sides) > 2:
        warnings.append(f"Fant {len(sides)} mulige sider; bruker de to første")

    return sides[0], sides[1], warnings


def parse_fifa_r32(markdown: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not markdown or "Knockout bracket" not in markdown:
        raise ValueError("Jina-svaret inneholder ikke 'Knockout bracket'")

    lines = markdown.splitlines()
    parsed: list[dict[str, Any]] = []
    parser_warnings: list[dict[str, Any]] = []

    i = 0
    while i < len(lines):
        heading = MATCH_HEADING_RE.fullmatch(lines[i].strip())
        if not heading:
            i += 1
            continue

        match_no = int(heading.group(1))
        url = heading.group(2)
        i += 1
        block: list[str] = []
        while i < len(lines) and not MATCH_HEADING_RE.fullmatch(lines[i].strip()):
            # Ny seksjon avslutter også blokken.
            if lines[i].strip().startswith("###") or lines[i].strip().startswith("####"):
                break
            block.append(lines[i])
            i += 1

        if match_no not in R32_MATCH_NUMBERS:
            continue

        clean = rens_blokklinjer(block)
        if len(clean) < 4:
            parser_warnings.append({"match_no": match_no, "advarsel": "For kort FIFA-kampblokk"})
            continue

        # Finn dato og tid, ikke anta at de alltid er på nøyaktig samme indeks.
        date_idx = next((idx for idx, line in enumerate(clean) if DATE_RE.fullmatch(line)), None)
        if date_idx is None:
            parser_warnings.append({"match_no": match_no, "advarsel": "Mangler FIFA-dato"})
            continue
        time_idx = next(
            (idx for idx in range(date_idx + 1, len(clean)) if TIME_RE.fullmatch(clean[idx])),
            None,
        )
        if time_idx is None:
            parser_warnings.append({"match_no": match_no, "advarsel": "Mangler FIFA-tid"})
            continue

        hjemme, borte, side_warnings = parse_fifa_sider(clean[time_idx + 1 :])
        for warning in side_warnings:
            parser_warnings.append({"match_no": match_no, "advarsel": warning})

        fifa_event_id_match = re.search(r"/(\d+)(?:\?.*)?$", url)
        parsed.append(
            {
                "fifa_match_no": match_no,
                "fifa_event_id": int(fifa_event_id_match.group(1)) if fifa_event_id_match else None,
                "fifa_url": url,
                "fifa_dato": fifa_dato_til_iso(clean[date_idx]),
                "fifa_tid": clean[time_idx],
                "fifa_hjemme": hjemme["verdi"],
                "fifa_borte": borte["verdi"],
                "fifa_hjemme_kode": hjemme["fifa_kode"],
                "fifa_borte_kode": borte["fifa_kode"],
                "fifa_hjemme_kjent": bool(hjemme["kjent_lag"]),
                "fifa_borte_kjent": bool(borte["kjent_lag"]),
            }
        )

    # FIFA-siden inneholder for tiden samme brakett flere ganger. Dedupliser på M-nummer.
    by_no: dict[int, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    for item in parsed:
        no = item["fifa_match_no"]
        if no not in by_no:
            by_no[no] = item
            continue
        previous = by_no[no]
        comparable_fields = (
            "fifa_event_id",
            "fifa_dato",
            "fifa_tid",
            "fifa_hjemme",
            "fifa_borte",
        )
        differs = any(previous.get(field) != item.get(field) for field in comparable_fields)
        duplicates.append(
            {
                "match_no": no,
                "identisk": not differs,
                "forste": {field: previous.get(field) for field in comparable_fields},
                "duplikat": {field: item.get(field) for field in comparable_fields},
            }
        )

    return [by_no[no] for no in sorted(by_no)], parser_warnings + duplicates


# ── FOOTBALL-DATA ─────────────────────────────────────────────────────────────
def er_r32_stage(stage: str) -> bool:
    stage = (stage or "").upper()
    return "ROUND_OF_32" in stage or "LAST_32" in stage


def er_kjent_fd_lag(navn: str) -> bool:
    if not navn:
        return False
    low = navn.lower()
    return not any(p in low for p in GENERIC_PLACEHOLDERS + ("w7", "w8", "w9"))


def parse_football_data_r32(data: dict[str, Any]) -> list[dict[str, Any]]:
    matches = data.get("matches", [])
    if not isinstance(matches, list):
        raise ValueError("football-data.org-svaret mangler en gyldig 'matches'-liste")

    out: list[dict[str, Any]] = []
    for match in matches:
        if not er_r32_stage(str(match.get("stage", ""))):
            continue

        home_raw = str(((match.get("homeTeam") or {}).get("name") or "")).strip()
        away_raw = str(((match.get("awayTeam") or {}).get("name") or "")).strip()
        home_for_id = FD_NAVN_TIL_OF_FOR_ID.get(home_raw, home_raw)
        away_for_id = FD_NAVN_TIL_OF_FOR_ID.get(away_raw, away_raw)
        utc_date = str(match.get("utcDate") or "").strip()
        dato = utc_date[:10]
        known = er_kjent_fd_lag(home_for_id) and er_kjent_fd_lag(away_for_id) and bool(dato)

        out.append(
            {
                "fd_match_id": match.get("id"),
                "fd_stage": match.get("stage", ""),
                "fd_status": match.get("status", ""),
                "fd_utcDate": utc_date,
                "fd_dato": dato,
                "fd_hjemmelag_raw": home_raw,
                "fd_bortelag_raw": away_raw,
                "fd_hjemmelag_for_id": home_for_id,
                "fd_bortelag_for_id": away_for_id,
                "fd_hjemmelag_kjent": er_kjent_fd_lag(home_for_id),
                "fd_bortelag_kjent": er_kjent_fd_lag(away_for_id),
                "kamp_id": kamp_id(home_for_id, away_for_id, dato) if known else None,
            }
        )

    return out


# ── MATCHING ──────────────────────────────────────────────────────────────────
def samme_lag(a: str, b: str) -> bool:
    return bool(a and b and sammenligningsnoekkel(a) == sammenligningsnoekkel(b))


def dato_kompatibel(fifa_dato: str, fd_dato: str) -> bool:
    avvik = datoavvik_dager(fifa_dato, fd_dato)
    return avvik is not None and avvik <= 1


def kompakt_fd(fd: dict[str, Any]) -> dict[str, Any]:
    return {
        "fd_match_id": fd.get("fd_match_id"),
        "fd_utcDate": fd.get("fd_utcDate", ""),
        "fd_dato": fd.get("fd_dato", ""),
        "fd_hjemmelag": fd.get("fd_hjemmelag_for_id", ""),
        "fd_bortelag": fd.get("fd_bortelag_for_id", ""),
        "kamp_id": fd.get("kamp_id"),
    }


def finn_delvise_kandidater(fifa: dict[str, Any], fd_matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for fd in fd_matches:
        if not dato_kompatibel(fifa.get("fifa_dato", ""), fd.get("fd_dato", "")):
            continue
        if fifa.get("fifa_hjemme_kjent") and not samme_lag(
            fifa.get("fifa_hjemme", ""), fd.get("fd_hjemmelag_for_id", "")
        ):
            continue
        if fifa.get("fifa_borte_kjent") and not samme_lag(
            fifa.get("fifa_borte", ""), fd.get("fd_bortelag_for_id", "")
        ):
            continue
        candidates.append(fd)
    return candidates


def match_fifa_mot_fd(fifa: dict[str, Any], fd_matches: list[dict[str, Any]]) -> dict[str, Any]:
    result = dict(fifa)
    home_known = bool(fifa.get("fifa_hjemme_kjent"))
    away_known = bool(fifa.get("fifa_borte_kjent"))

    if not (home_known and away_known):
        candidates = finn_delvise_kandidater(fifa, fd_matches)
        result.update(
            {
                "status": "pending",
                "forklaring": "FIFA har fortsatt minst én plassholder; endelig kamp-ID godkjennes ikke ennå.",
                "kamp_id": None,
                "delvise_fd_kandidater": [kompakt_fd(fd) for fd in candidates],
            }
        )
        if len(candidates) == 1:
            # Kun et forslag til kontroll. Status forblir pending og brukes ikke i produksjon.
            result["foreslaatt_fd_kandidat"] = kompakt_fd(candidates[0])
        return result

    oriented = [
        fd
        for fd in fd_matches
        if samme_lag(fifa["fifa_hjemme"], fd.get("fd_hjemmelag_for_id", ""))
        and samme_lag(fifa["fifa_borte"], fd.get("fd_bortelag_for_id", ""))
    ]
    compatible = [fd for fd in oriented if dato_kompatibel(fifa["fifa_dato"], fd.get("fd_dato", ""))]

    if len(compatible) == 1:
        fd = compatible[0]
        result.update(
            {
                "status": "matched",
                "forklaring": "Samme hjemme-/bortelag og kompatibel dato i FIFA og football-data.org.",
                **kompakt_fd(fd),
                "dato_avvik_dager": datoavvik_dager(fifa["fifa_dato"], fd.get("fd_dato", "")),
            }
        )
        return result

    if len(compatible) > 1:
        result.update(
            {
                "status": "conflict",
                "forklaring": "Flere football-data.org-kamper matcher samme FIFA-oppgjør.",
                "kamp_id": None,
                "fd_kandidater": [kompakt_fd(fd) for fd in compatible],
            }
        )
        return result

    reversed_candidates = [
        fd
        for fd in fd_matches
        if samme_lag(fifa["fifa_hjemme"], fd.get("fd_bortelag_for_id", ""))
        and samme_lag(fifa["fifa_borte"], fd.get("fd_hjemmelag_for_id", ""))
        and dato_kompatibel(fifa["fifa_dato"], fd.get("fd_dato", ""))
    ]
    if reversed_candidates:
        result.update(
            {
                "status": "conflict",
                "forklaring": "Lagene finnes i football-data.org, men hjemme- og bortelag er motsatt av FIFA.",
                "kamp_id": None,
                "fd_kandidater": [kompakt_fd(fd) for fd in reversed_candidates],
            }
        )
        return result

    if len(oriented) == 1:
        fd = oriented[0]
        result.update(
            {
                "status": "conflict",
                "forklaring": "Lagene matcher, men FIFA-dato og football-data.org-dato avviker med mer enn ett døgn.",
                "kamp_id": None,
                "fd_kandidater": [kompakt_fd(fd)],
                "dato_avvik_dager": datoavvik_dager(fifa["fifa_dato"], fd.get("fd_dato", "")),
            }
        )
        return result

    result.update(
        {
            "status": "conflict",
            "forklaring": "Fant ingen entydig football-data.org-kamp for det kjente FIFA-oppgjøret.",
            "kamp_id": None,
            "fd_kandidater": [kompakt_fd(fd) for fd in oriented],
        }
    )
    return result


# ── SAMMENLIGNING MOT EKSISTERENDE PRODUKSJONSDATA ───────────────────────────
def les_eksisterende_r32() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    manual = les_json(MANUELLE_KAMPER_JSON, {})
    for item in manual.get("kamper", []) if isinstance(manual, dict) else []:
        if item.get("runde") != "r32":
            continue
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
    if item.get("status") != "matched" or not item.get("kamp_id"):
        return

    same_no = [
        e for e in existing
        if e.get("match_no") is not None and int(e["match_no"]) == int(item["fifa_match_no"])
    ]
    same_teams = [
        e for e in existing
        if samme_lag(e.get("hjemme", ""), item.get("fd_hjemmelag", ""))
        and samme_lag(e.get("borte", ""), item.get("fd_bortelag", ""))
    ]
    candidates = same_no or same_teams
    item["eksisterende_koblinger"] = candidates
    if candidates:
        item["kamp_id_samsvarer_med_eksisterende"] = all(
            not e.get("kamp_id") or e.get("kamp_id") == item.get("kamp_id") for e in candidates
        )
    else:
        item["kamp_id_samsvarer_med_eksisterende"] = None


# ── HOVEDPROGRAM ──────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kontroller FIFA R32 mot dagens kamp-ID-regel.")
    parser.add_argument("--jina-file", type=Path, help="Les lagret Jina-markdown i stedet for nett.")
    parser.add_argument(
        "--football-data-file",
        type=Path,
        help="Les lagret football-data.org JSON i stedet for API.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Målfil for kontrollrapport.")
    parser.add_argument(
        "--strict",
        action="store_true",
        default=os.environ.get("STRICT", "0") == "1",
        help="Returner exit code 2 ved konflikt eller kamp-ID-avvik.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print("=" * 68)
    print("FIFA R32-kontroll — ingen produksjonsdata endres")
    print("=" * 68)

    try:
        if args.jina_file:
            print(f"Leser FIFA/Jina fra {args.jina_file}")
            fifa_markdown = args.jina_file.read_text(encoding="utf-8")
        else:
            print("Henter FIFA-brakett via Jina Reader...")
            fifa_markdown = hent_tekst(JINA_URL, headers={"User-Agent": "RambergVMBot/1.0"})

        fifa_matches, parser_notes = parse_fifa_r32(fifa_markdown)
        print(f"  → Fant {len(fifa_matches)} unike R32-kamper hos FIFA")

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
        missing_match_nos = sorted(R32_MATCH_NUMBERS - {m["fifa_match_no"] for m in fifa_matches})
        id_mismatches = [
            item["fifa_match_no"]
            for item in mapped
            if item.get("kamp_id_samsvarer_med_eksisterende") is False
        ]
        nonidentical_duplicates = [
            note for note in parser_notes
            if isinstance(note, dict) and note.get("identisk") is False
        ]

        report = {
            "generert": iso_utc_now(),
            "formaal": "Kontroll av FIFA R32 mot dagens kamp-ID-regel",
            "produksjonsdata_endret": False,
            "kilder": {
                "fifa_jina": str(args.jina_file) if args.jina_file else JINA_URL,
                "football_data": str(args.football_data_file) if args.football_data_file else FOOTBALL_DATA_URL,
            },
            "kamp_id_regel": "rens(hjemmelag)_rens(bortelag)_rens(football-data utcDate[:10])",
            "oppsummering": {
                "forventet_antall_r32": 16,
                "fifa_r32_funnet": len(fifa_matches),
                "fifa_match_no_mangler": missing_match_nos,
                "football_data_r32_funnet": len(fd_matches),
                "matched": counts.get("matched", 0),
                "pending": counts.get("pending", 0),
                "conflict": counts.get("conflict", 0),
                "kamp_id_avvik_mot_eksisterende": id_mismatches,
                "motstridende_fifa_duplikater": [n.get("match_no") for n in nonidentical_duplicates],
                "klar_for_produksjonsbruk": bool(
                    len(fifa_matches) == 16
                    and counts.get("matched", 0) == 16
                    and counts.get("conflict", 0) == 0
                    and not id_mismatches
                    and not nonidentical_duplicates
                ),
            },
            "parser_merknader": parser_notes,
            "kamper": mapped,
        }

        skriv_json_atomisk(args.output, report)
        print(f"\nSkrev kontrollrapport: {args.output}")
        print(
            "Status: "
            f"matched={counts.get('matched', 0)}, "
            f"pending={counts.get('pending', 0)}, "
            f"conflict={counts.get('conflict', 0)}"
        )
        if missing_match_nos:
            print(f"FIFA mangler foreløpig disse R32-numrene i Jina-uttrekket: {missing_match_nos}")
        if id_mismatches:
            print(f"ADVARSEL: Kamp-ID-avvik mot eksisterende data for: {id_mismatches}")

        if args.strict and (counts.get("conflict", 0) or id_mismatches or nonidentical_duplicates):
            return 2
        return 0

    except Exception as exc:
        print(f"FEIL: {exc}", file=sys.stderr)
        print("Eksisterende kontrollfil ble ikke overskrevet.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
