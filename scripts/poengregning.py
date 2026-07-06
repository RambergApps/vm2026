"""
VM 2026 Tippekonkurranse — Poengregningscript
Kjøres av GitHub Actions hvert 30. minutt.

Flyt:
1. Hent kampoppsett fra OpenFootball API (primærkilde for struktur)
2. For kamper uten score: hent fra football-data.org (fallback for resultater)
3. Les manuelle fallback-kamper fra data/manuelle-kamper.json
4. Les alle tippinger fra /tippinger/
5. Regn poeng per deltaker
6. Skriv data/data.js
7. Skriv data/deltakere.json  (kobler navn til stabil ID)
8. Oppdater data/status.json med utslagsrunde-info

Kilder og prioritet for score:
  1. football-data.org (rask resultat-/statuskilde, matchet tilbake til OpenFootball-ID)
  2. OpenFootball (kampoppsett og stabil kamp-ID)
  3. Manuelle fallback-kamper (data/manuelle-kamper.json)
"""

import json
import os
import re
import sys
import requests
import unicodedata
from pathlib import Path
from datetime import datetime, timezone, timedelta

from sen_pamelding import apply_late_signup_points

# ── KONFIG ────────────────────────────────────────────────────────────────────
API_URL               = "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json"
FOOTBALL_DATA_API_URL = "https://api.football-data.org/v4/competitions/WC/matches"
FOOTBALL_DATA_TOKEN   = os.environ.get("FOOTBALL_DATA_TOKEN", "")
FIFA_PLAYER_STATS_URL = "https://r.jina.ai/https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/statistics/player-statistics"
FIFA_CALENDAR_API_URL = "https://api.fifa.com/api/v3/calendar/matches"
FIFA_CALENDAR_PARAMS = {
    "idCompetition": 17,
    "idSeason": 285023,
    "count": 500,
    "language": "en",
}
FIFA_STATUS_MAP = {
    0: "FINISHED",
    1: "TIMED",
    3: "IN_PLAY",
}

# Lagnavn-mapping: football-data.org → OpenFootball
# OpenFootball-navn er master siden tipping-appen og kamp-ID-er er basert på disse.
FD_NAVN_TIL_OF = {
    "Bosnia-Herzegovina":      "Bosnia & Herzegovina",
    "Bosnia and Herzegovina":  "Bosnia & Herzegovina",
    "Cape Verde Islands":      "Cape Verde",
    "Cabo Verde":              "Cape Verde",
    "Congo DR":                "DR Congo",
    "Czechia":                 "Czech Republic",
    "Korea Republic":          "South Korea",
    "Côte d'Ivoire":           "Ivory Coast",
    "Türkiye":                 "Turkey",
    "United States":           "USA",
    "IR Iran":                 "Iran",
    "Curacao":                 "Curaçao",
}

REPO_ROOT      = Path(__file__).parent.parent
TIPPINGER      = REPO_ROOT / "tippinger"
DATA_DIR       = REPO_ROOT / "data"
DATA_JS        = DATA_DIR / "data.js"
STATUS_JSON    = DATA_DIR / "status.json"
DELTAKERE_JSON = DATA_DIR / "deltakere.json"          # ← NY
MANUELLE_KAMPER_JSON = DATA_DIR / "manuelle-kamper.json"
DEBUG_JSON           = DATA_DIR / "fd_debug.json"
FIFA_DEBUG_JSON      = DATA_DIR / "fifa_debug.json"
MANGLER_RESULTATER_JSON = DATA_DIR / "mangler-resultater.json"
KAMP_MAPPING_JSON       = DATA_DIR / "kamp-mapping.json"

# Poeng per runde
# Alle kamper poengberegnes på resultat etter ordinær tid / 90 minutter.
# "utfall" betyr H/U/B etter 90 min, ikke hvem som går videre etter ekstraomganger/straffer.
POENG = {
    "gruppe": {"utfall": 2, "eksakt": 4},
    "r32":    {"utfall": 3, "eksakt": 4},
    "r16":    {"utfall": 4, "eksakt": 4},
    "qf":     {"utfall": 5, "eksakt": 4},
    "sf":     {"utfall": 6, "eksakt": 4},
    "final":  {"utfall": 7, "eksakt": 4},
}
POENG_TURNERINGSVINNER = 70
POENG_BONUS = 10
POENG_HELHETSBONUS = 20
BONUS_SPORSMAL = {
    "r32":   {"id": "antall_uavgjort",      "tekst": "Antall kamper som ender uavgjort i løpet av de første 90 minuttene, inkludert tilleggstid"},
    "r16":   {"id": "antall_nullen",        "tekst": "Antall lag som holder nullen i løpet av de første 90 minuttene, inkludert tilleggstid"},
    "qf":    {"id": "antall_ettmaalsseier", "tekst": "Antall kamper som avgjøres med ett mål i løpet av de første 90 minuttene, inkludert tilleggstid"},
    "sf":    {"id": "totale_maal",          "tekst": "Totalt antall mål i semifinalene i løpet av de første 90 minuttene, inkludert tilleggstid"},
    "final": {"id": "begge_lag_scorer",     "tekst": "Scorer begge lag i finalen i løpet av de første 90 minuttene, inkludert tilleggstid?"},
}
HELHETSBONUS_SPORSMAL = {
    "flest_maal_lag": {
        "tekst": "Hvilket lag scorer flest mål fra 32-delsfinalene til og med finalen i løpet av de første 90 minuttene, inkludert tilleggstid?"
    },
    "totale_maal_utslag": {
        "tekst": "Hvor mange mål scores totalt fra 32-delsfinalene til og med finalen i løpet av de første 90 minuttene, inkludert tilleggstid?"
    },
    "golden_boot": {
        "tekst": "Hvem vinner FIFA Golden Boot?"
    },
}
UTSLAGSRUNDER = ("r32", "r16", "qf", "sf", "final")

# FIFA Calendar gir avsparkstid per offisielt matchnummer.
# Brukes kun til frist/tippebar for utslagskamper som er bygget fra Wxx-slotter.
# Endrer ikke kamp_id, fd_match_id, fifa_event_id eller match_no.
FIFA_MATCH_NO_LOOKUP = {}

# ── TESTMODUS ─────────────────────────────────────────────────────────────────
TEST_MODE = not os.environ.get("GITHUB_ACTIONS")

MOCK_RESULTATER = [
    {"kamp_id": "Mexico_South_Africa_2026-06-11", "hjemmelag": "Mexico", "bortelag": "South Africa", "hjemme": 2, "borte": 1, "ferdig": True, "runde": "gruppe"},
    {"kamp_id": "South_Korea_Czech_Republic_2026-06-11", "hjemmelag": "South Korea", "bortelag": "Czech Republic", "hjemme": 1, "borte": 1, "ferdig": True, "runde": "gruppe"},
    {"kamp_id": "Norway_IC_Path_2_winner_2026-06-11", "hjemmelag": "Norway", "bortelag": "Iraq", "hjemme": 3, "borte": 0, "ferdig": True, "runde": "gruppe"},
]

MOCK_TIPPINGER = [
    {
        "meta": {"navn": "Kari Nordmann", "token": "vm2026-ramberg"},
        "turneringsvinner": "Norway",
        "gruppespill": [
            {"kamp_id": "Mexico_South_Africa_2026-06-11",        "hjemme": 2, "borte": 1},
            {"kamp_id": "South_Korea_Czech_Republic_2026-06-11", "hjemme": 1, "borte": 1},
            {"kamp_id": "Norway_IC_Path_2_winner_2026-06-11",    "hjemme": 3, "borte": 0},
        ]
    },
    {
        "meta": {"navn": "Ole Hansen", "token": "vm2026-ramberg"},
        "turneringsvinner": "Brazil",
        "gruppespill": [
            {"kamp_id": "Mexico_South_Africa_2026-06-11",        "hjemme": 1, "borte": 0},
            {"kamp_id": "South_Korea_Czech_Republic_2026-06-11", "hjemme": 2, "borte": 1},
            {"kamp_id": "Norway_IC_Path_2_winner_2026-06-11",    "hjemme": 3, "borte": 0},
        ]
    },
    {
        "meta": {"navn": "Petter Ås", "token": "vm2026-ramberg"},
        "turneringsvinner": "Spain",
        "gruppespill": [
            {"kamp_id": "Mexico_South_Africa_2026-06-11",        "hjemme": 0, "borte": 2},
            {"kamp_id": "South_Korea_Czech_Republic_2026-06-11", "hjemme": 1, "borte": 1},
            {"kamp_id": "Norway_IC_Path_2_winner_2026-06-11",    "hjemme": 2, "borte": 1},
        ]
    },
]

# ── HJELPEFUNKSJONER ──────────────────────────────────────────────────────────
def utfall(hjemme, borte):
    """Returnerer 'H', 'U' eller 'B' basert på målscore."""
    if hjemme is None or borte is None:
        return None
    if hjemme > borte:
        return "H"
    elif hjemme < borte:
        return "B"
    return "U"

def kamp_id(team1, team2, dato):
    """Genererer kamp-ID fra lagnavn og dato — matcher tipping-appens format."""
    def rens(s):
        return "".join(c if c.isalnum() else "_" for c in s)
    return f"{rens(team1)}_{rens(team2)}_{rens(dato)}"

def dato_minus_en_dag(dato):
    """Returnerer YYYY-MM-DD minus én dag. Brukes for UTC/lokal-dato-avvik mellom kilder."""
    try:
        return (datetime.fromisoformat(dato[:10]) - timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        return ""

def kamp_id_til_ascii(kid):
    """
    Konverterer en kamp-ID til ASCII-only format.
    Brukes for å matche tippinger generert av JavaScript der
    spesialtegn (f.eks. ç i Curaçao) erstattes med _.
    """
    return "".join(c if (c.isascii() and c.isalnum()) or c == "_" else "_" for c in kid)

def bygg_ascii_lookup(resultat_lookup):
    """
    Bygger en sekundær lookup med ASCII-normaliserte kamp-ID-er.
    Fanger opp tippinger der JavaScript har erstattet f.eks. ç med _.
    Returnerer kun oppføringer der ASCII-ID avviker fra original-ID.
    """
    ascii_lookup = {}
    for kid, val in resultat_lookup.items():
        ascii_kid = kamp_id_til_ascii(kid)
        if ascii_kid != kid:
            ascii_lookup[ascii_kid] = val
    if ascii_lookup:
        print(f"  -> ASCII-fallback lookup: {len(ascii_lookup)} kamp(er) med spesialtegn")
        for k in ascii_lookup:
            print(f"     {k}")
    return ascii_lookup

def lag_deltaker_id(navn):
    """
    Genererer en stabil, URL-sikker deltaker_id fra navn.
    Eksempel: "Petter Ås" → "petter_aas"
    Brukes som primærnøkkel på tvers av runder.
    """
    s = navn.lower().strip()
    # Normaliser norske og andre spesialtegn
    s = s.replace("æ", "ae").replace("ø", "oe").replace("å", "aa")
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    # Erstatt alt som ikke er a–z eller 0–9 med underscore
    s = re.sub(r"[^a-z0-9]", "_", s)
    # Fjern doble underscores og ledende/etterfølgende
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "ukjent"



def parse_int(v):
    if v in (None, ""):
        return None
    try:
        return int(v)
    except Exception:
        return None

def finn_avanserer(kamp):
    """
    Returnerer laget som faktisk går videre fra en utslagskamp.
    Dette brukes kun til bracket/neste runde, ikke til poeng.
    """
    explicit = (kamp.get("avanserer") or "").strip()
    if explicit:
        return explicit
    if not kamp.get("ferdig"):
        return None
    h = kamp.get("hjemme")
    b = kamp.get("borte")
    if h is None or b is None:
        return None
    if h > b:
        return kamp.get("hjemmelag")
    if b > h:
        return kamp.get("bortelag")
    return None

# ── HENT API-DATA ─────────────────────────────────────────────────────────────
def hent_api_data():
    """Henter VM-data fra openfootball API."""
    print("Henter kampdata fra OpenFootball...")
    try:
        r = requests.get(API_URL, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"FEIL: Kunne ikke hente API-data: {e}")
        sys.exit(1)


def parse_dato(dato):
    """Parser YYYY-MM-DD til date. Returnerer None ved ugyldig verdi."""
    try:
        return datetime.fromisoformat(str(dato or "")[:10]).date()
    except Exception:
        return None


def dato_avvik_dager(dato_a, dato_b):
    """Returnerer absolutt datoavvik i dager, eller None hvis en dato ikke kan parses."""
    a = parse_dato(dato_a)
    b = parse_dato(dato_b)
    if not a or not b:
        return None
    return abs((a - b).days)


def normaliser_lagnavn_for_match(navn):
    """
    Normaliserer lagnavn for trygg matching mellom OpenFootball og football-data.org.
    Beholder ikke visningsnavn — dette er kun en teknisk sammenligningsnøkkel.
    """
    s = str(navn or "").strip()
    s = FD_NAVN_TIL_OF.get(s, s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")


def runde_fra_openfootball_kamp(kamp):
    """Bestemmer intern runde fra OpenFootball-kamp."""
    if kamp.get("group"):
        return "gruppe"
    if kamp.get("round"):
        r = kamp["round"].lower()
        if "round of 32" in r or "r32" in r:
            return "r32"
        if "round of 16" in r or "r16" in r:
            return "r16"
        if "quarter" in r:
            return "qf"
        if "semi" in r:
            return "sf"
        if "final" in r:
            return "final"
    return "ukjent"


def fd_runde_fra_stage(stage):
    """Mapper football-data.org sitt stage-felt til intern runde."""
    stage = str(stage or "").strip().upper()
    mapping = {
        "GROUP_STAGE": "gruppe",
        "LAST_32": "r32",
        "ROUND_OF_32": "r32",
        "LAST_16": "r16",
        "ROUND_OF_16": "r16",
        "QUARTER_FINALS": "qf",
        "SEMI_FINALS": "sf",
        "FINAL": "final",
    }
    return mapping.get(stage, "ukjent")


def les_tidligere_spillerstatistikk():
    """Leser siste gyldige FIFA-uttrekk fra eksisterende data.js."""
    if not DATA_JS.exists():
        return []
    try:
        tekst = DATA_JS.read_text(encoding="utf-8")
        match = re.search(r"const\s+VM_DATA\s*=\s*(\{.*\})\s*;\s*$", tekst, re.S)
        if not match:
            return []
        data = json.loads(match.group(1))
        spillere = data.get("spillerstatistikk", [])
        return spillere if isinstance(spillere, list) else []
    except Exception:
        return []


def hent_fifa_spillerstatistikk():
    """
    Henter FIFAs rangerte spillerstatistikk via Jina.

    Returnerer en liste med rank, spiller, mål, assists og minutter. Førsteplass
    brukes som Golden Boot-fasit først når hele utslagsfasen er ferdig. Ved en
    midlertidig feil beholdes siste gyldige uttrekk fra data.js.
    """
    print("Henter FIFA spillerstatistikk...")
    tidligere = les_tidligere_spillerstatistikk()
    try:
        response = requests.get(FIFA_PLAYER_STATS_URL, timeout=20)
        response.raise_for_status()
        tekst = response.text
    except Exception as e:
        print(f"  ADVARSEL: Kunne ikke hente FIFA spillerstatistikk: {e}")
        if tidligere:
            print(f"  → Bruker siste gyldige FIFA-uttrekk med {len(tidligere)} spillere")
        return tidligere

    spillere = []
    rad_pattern = re.compile(
        r"\|\s*(\d+)\s*\|\s*(.*?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|"
    )
    for match in rad_pattern.finditer(tekst):
        rank = int(match.group(1))
        player_cell = match.group(2)
        spiller = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", player_cell).strip()
        spiller = re.sub(r"\s+", " ", spiller)
        if not spiller:
            continue
        spillere.append({
            "rank": rank,
            "spiller": spiller,
            "maal": int(match.group(3)),
            "assists": int(match.group(4)),
            "minutter": int(match.group(5)),
        })

    # Unngå duplikater dersom Jina-svaret inneholder samme tabell flere ganger.
    unike = {}
    for spiller in spillere:
        unike[(spiller["rank"], spiller["spiller"])] = spiller
    spillere = sorted(unike.values(), key=lambda x: (x["rank"], x["spiller"]))
    if not spillere:
        print("  ADVARSEL: FIFA-siden ble hentet, men ingen spillerrader kunne parses")
        if tidligere:
            print(f"  → Bruker siste gyldige FIFA-uttrekk med {len(tidligere)} spillere")
        return tidligere
    print(f"  → {len(spillere)} rangerte spillere hentet fra FIFA")
    return spillere



def lokaliser_fifa_tekst(value):
    """Henter tekst fra FIFAs lokaliserte tekststrukturer."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        for item in value:
            text = lokaliser_fifa_tekst(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("Description", "Text", "Name", "Value", "Label"):
            text = lokaliser_fifa_tekst(value.get(key))
            if text:
                return text
        for nested in value.values():
            text = lokaliser_fifa_tekst(nested)
            if text:
                return text
    return ""


def fifa_lagnavn(side_obj):
    """Normaliserer FIFA-lagnavn til OpenFootball-navnene appen bruker."""
    side_obj = side_obj if isinstance(side_obj, dict) else {}
    raw = lokaliser_fifa_tekst(
        side_obj.get("ShortClubName")
        or side_obj.get("ClubName")
        or side_obj.get("TeamName")
        or side_obj.get("Name")
        or side_obj.get("ShortName")
    )
    return FD_NAVN_TIL_OF.get(raw, raw)


def fifa_parse_score(*values):
    """Parser score fra FIFA. 0 er gyldig score og må ikke tolkes som False."""
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        match = re.fullmatch(r"\s*(\d+)\s*", str(value))
        if match:
            return int(match.group(1))
    return None


def fifa_status_til_fd_status(match_status):
    try:
        status_int = int(match_status)
    except Exception:
        return "TIMED"
    return FIFA_STATUS_MAP.get(status_int, f"FIFA_STATUS_{status_int}")


def fifa_kamp_til_resultat(kamp):
    """Konverterer én FIFA Calendar-kamp til samme basisformat som resultat_lookup."""
    home_obj = kamp.get("Home") if isinstance(kamp.get("Home"), dict) else {}
    away_obj = kamp.get("Away") if isinstance(kamp.get("Away"), dict) else {}
    team1 = fifa_lagnavn(home_obj)
    team2 = fifa_lagnavn(away_obj)
    utc_date = str(kamp.get("Date") or kamp.get("UtcDate") or kamp.get("UTCDate") or "").strip()
    dato = utc_date[:10]
    hjemme = fifa_parse_score(kamp.get("HomeTeamScore"), home_obj.get("Score"))
    borte = fifa_parse_score(kamp.get("AwayTeamScore"), away_obj.get("Score"))
    har_score = hjemme is not None and borte is not None
    raw_status = kamp.get("MatchStatus")
    status = fifa_status_til_fd_status(raw_status)
    try:
        status_int = int(raw_status)
    except Exception:
        status_int = None

    return {
        "fifa_event_id": str(kamp.get("IdMatch") or kamp.get("MatchId") or ""),
        "fifa_match_no": parse_int(kamp.get("MatchNumber")),
        "hjemmelag": team1,
        "bortelag": team2,
        "fifa_hjemmelag": fifa_lagnavn(home_obj),
        "fifa_bortelag": fifa_lagnavn(away_obj),
        "hjemme": hjemme,
        "borte": borte,
        "har_score": har_score,
        "ferdig": status_int == 0 and har_score,
        "kamp_ferdig_api": status_int == 0,
        "kilde": "fifa_calendar",
        "status": status,
        "fifa_status_raw": raw_status,
        "fifa_match_time": kamp.get("MatchTime"),
        "fifa_dato": dato,
        "fifa_utcDate": utc_date,
        "winner": kamp.get("Winner"),
        "fifa_kamp_id_basert_paa_dato": kamp_id(team1, team2, dato) if team1 and team2 and dato else "",
    }


def hent_fifa_calendar():
    """
    Henter resultater/status fra FIFA Calendar API.

    Brukes som sekundær resultatkilde etter football-data bulk. FIFA-kallet er
    åpent og gir live/final score, men OpenFootball eier fortsatt kamp-ID-ene.
    I tillegg lagres avsparkstid per offisielt matchnummer, slik at senere
    utslagsrunder kan åpnes fortløpende når lagene er klare.
    """
    global FIFA_MATCH_NO_LOOKUP
    FIFA_MATCH_NO_LOOKUP = {}
    print("Henter kampresultater fra FIFA Calendar API...")
    try:
        r = requests.get(
            FIFA_CALENDAR_API_URL,
            params=FIFA_CALENDAR_PARAMS,
            headers={"User-Agent": "RambergVMBot/1.0", "Accept": "application/json"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ADVARSEL: Kunne ikke hente FIFA Calendar API: {e}")
        return {}

    raw_results = data.get("Results") if isinstance(data, dict) else None
    if not isinstance(raw_results, list):
        print("  ADVARSEL: FIFA Calendar API mangler Results-liste")
        return {}

    fifa_lookup = {}
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        item = fifa_kamp_til_resultat(raw)

        # Lagre FIFA-avspark per match_no også for kamper der lagene fortsatt
        # er placeholders. Dette brukes kun til utcDate/tippefrist i status.json.
        match_no = parse_int(item.get("fifa_match_no"))
        if match_no is not None and item.get("fifa_utcDate"):
            FIFA_MATCH_NO_LOOKUP[match_no] = item

        if not item.get("hjemmelag") or not item.get("bortelag") or not item.get("fifa_dato"):
            continue
        key = item.get("fifa_event_id") or item.get("fifa_kamp_id_basert_paa_dato")
        fifa_lookup[str(key)] = item

    ferdig_antall = sum(1 for v in fifa_lookup.values() if v.get("ferdig"))
    paagaar_antall = sum(1 for v in fifa_lookup.values() if v.get("status") in ("IN_PLAY", "PAUSED"))
    med_score = sum(1 for v in fifa_lookup.values() if v.get("har_score"))
    print(f"  -> {ferdig_antall} ferdigspilte, {paagaar_antall} pågående, {med_score} med score fra FIFA Calendar")
    print(f"  -> {len(FIFA_MATCH_NO_LOOKUP)} kamper med FIFA-avsparkstid per match_no")

    debug_data = {
        "tidspunkt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kamper": {
            key: {
                "fifa_event_id": v.get("fifa_event_id"),
                "hjemmelag": v.get("hjemmelag"),
                "bortelag": v.get("bortelag"),
                "status": v.get("status"),
                "fifa_status_raw": v.get("fifa_status_raw"),
                "ferdig": v.get("ferdig"),
                "hjemme": v.get("hjemme"),
                "borte": v.get("borte"),
                "fifa_dato": v.get("fifa_dato"),
                "fifa_utcDate": v.get("fifa_utcDate"),
                "fifa_match_time": v.get("fifa_match_time"),
            }
            for key, v in sorted(fifa_lookup.items(), key=lambda x: x[0])
        }
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FIFA_DEBUG_JSON.write_text(json.dumps(debug_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  -> Skrev fifa_debug.json med {len(fifa_lookup)} kamper")

    return fifa_lookup


def finn_fifa_match_for_of(kid, team1, team2, dato, fifa_lookup):
    """Finner FIFA Calendar-kamp for én OpenFootball-kamp basert på lag og dato."""
    if not fifa_lookup:
        return None
    of_home_key = normaliser_lagnavn_for_match(team1)
    of_away_key = normaliser_lagnavn_for_match(team2)
    kandidater = []

    for fifa_key, fifa in fifa_lookup.items():
        if normaliser_lagnavn_for_match(fifa.get("hjemmelag")) != of_home_key:
            continue
        if normaliser_lagnavn_for_match(fifa.get("bortelag")) != of_away_key:
            continue
        avvik = dato_avvik_dager(dato, fifa.get("fifa_dato"))
        if avvik is None or avvik > 1:
            continue
        kandidater.append((avvik, 0 if fifa.get("har_score") else 1, fifa_key, fifa))

    if not kandidater:
        return None
    kandidater.sort(key=lambda x: (x[0], x[1], x[2]))
    beste_avvik = kandidater[0][0]
    beste = [k for k in kandidater if k[0] == beste_avvik]
    if len(beste) > 1:
        print(f"    ADVARSEL: Flere mulige FIFA-treff for {kid}: {[x[2] for x in beste]} — hopper over FIFA-match")
        return None
    return beste[0][3]


def bruk_fifa_paa_base(base, fifa, team1, team2):
    """Legger FIFA Calendar-status og score inn på en OpenFootball-kamp."""
    if not fifa:
        return base

    base["status"] = fifa.get("status", base.get("status", "TIMED"))
    base["fifa_event_id"] = fifa.get("fifa_event_id")
    base["fifa_match_no"] = fifa.get("fifa_match_no")
    base["fifa_utcDate"] = fifa.get("fifa_utcDate")
    base["fifa_dato"] = fifa.get("fifa_dato")
    base["fifa_status_raw"] = fifa.get("fifa_status_raw")
    base["fifa_match_time"] = fifa.get("fifa_match_time")
    base["kamp_ferdig_api"] = bool(fifa.get("kamp_ferdig_api"))

    if fifa.get("har_score"):
        base["hjemme"] = fifa.get("hjemme")
        base["borte"] = fifa.get("borte")
        base["ferdig"] = bool(fifa.get("ferdig"))
        base["kilde"] = "fifa_calendar"
        base["kilde_score"] = "fifa_calendar"

    # FIFA Winner er IdTeam, så vi bruker ikke dette til avansement her.
    return base


def kamp_burde_ha_status_na(base, margin_minutter=15):
    """True bare for kamper som har eller snart har avspark. Hindrer enkeltkall på fremtidige kamper."""
    tidspunkt = parse_iso_utc(base.get("fd_utcDate") or base.get("utcDate") or base.get("dato"))
    if not tidspunkt:
        return False
    return tidspunkt <= datetime.now(timezone.utc) + timedelta(minutes=margin_minutter)

def les_kamp_mapping():
    """
    Leser data/kamp-mapping.json.

    Format:
    {
      "sist_oppdatert": "...",
      "kamper": {
        "OpenFootball_kamp_id": {
          "fd_match_id": 123,
          ...
        }
      }
    }
    """
    if not KAMP_MAPPING_JSON.exists():
        return {"sist_oppdatert": None, "kamper": {}}

    try:
        data = json.loads(KAMP_MAPPING_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ADVARSEL: Kunne ikke lese kamp-mapping.json: {e}")
        return {"sist_oppdatert": None, "kamper": {}}

    # Bakoverkompatibilitet hvis filen senere skulle ha blitt lagret som ren dict.
    if isinstance(data, dict) and "kamper" not in data:
        data = {"sist_oppdatert": None, "kamper": data}

    if not isinstance(data.get("kamper"), dict):
        data["kamper"] = {}

    return data


def skriv_kamp_mapping(mapping_data, force=False):
    """Skriver data/kamp-mapping.json bare hvis innholdet faktisk er endret."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    mapping_data = {
        "sist_oppdatert": mapping_data.get("sist_oppdatert"),
        "kamper": dict(sorted(mapping_data.get("kamper", {}).items())),
    }
    ny = json.dumps(mapping_data, ensure_ascii=False, indent=2) + "\n"
    gammel = KAMP_MAPPING_JSON.read_text(encoding="utf-8") if KAMP_MAPPING_JSON.exists() else None

    if not force and gammel == ny:
        print("  → kamp-mapping.json uendret")
        return

    KAMP_MAPPING_JSON.write_text(ny, encoding="utf-8")
    print(f"  → Skrev kamp-mapping.json med {len(mapping_data.get('kamper', {}))} koblinger")


def hent_football_data_org():
    """
    Henter kampresultater/status fra football-data.org.

    Returnerer en dict med fd_match_id → data. Disse ID-ene brukes bare til
    kildekobling. Poeng og frontend skal fortsatt bruke OpenFootball-kamp_id.
    """
    if not FOOTBALL_DATA_TOKEN:
        print("  ADVARSEL: FOOTBALL_DATA_TOKEN ikke satt — hopper over football-data.org")
        return {}

    print("Henter kampresultater fra football-data.org...")
    try:
        r = requests.get(
            FOOTBALL_DATA_API_URL,
            headers={"X-Auth-Token": FOOTBALL_DATA_TOKEN},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ADVARSEL: Kunne ikke hente fra football-data.org: {e}")
        return {}

    fd_lookup = {}
    tillatte_status = {"SCHEDULED", "TIMED", "IN_PLAY", "PAUSED", "FINISHED"}

    for kamp in data.get("matches", []):
        status_fd = kamp.get("status") or "TIMED"
        if status_fd not in tillatte_status:
            continue

        # football-data.org kan returnere null for lag/dato på enkelte fremtidige/ikke-fastlagte kamper.
        # Disse kan ikke matches trygt mot OpenFootball-ID og skal derfor ikke inn i fd_lookup.
        home_raw = ((kamp.get("homeTeam") or {}).get("name") or "").strip()
        away_raw = ((kamp.get("awayTeam") or {}).get("name") or "").strip()

        # Normaliser lagnavn til OpenFootball-format
        team1 = (FD_NAVN_TIL_OF.get(home_raw, home_raw) or "").strip()
        team2 = (FD_NAVN_TIL_OF.get(away_raw, away_raw) or "").strip()

        # Dato fra utcDate (format: 2026-06-14T04:00:00Z -> 2026-06-14)
        utc_date = (kamp.get("utcDate") or "").strip()
        dato = utc_date[:10]

        if not team1 or not team2 or not dato:
            print(
                "  -> Hopper over fd.org-kamp uten komplett lag/dato: "
                f"id={kamp.get('id')} home='{home_raw}' away='{away_raw}' utcDate='{utc_date}'"
            )
            continue

        score = kamp.get("score", {}) or {}
        ft = score.get("fullTime") or {}
        rt = score.get("regularTime") or {}
        et = score.get("extraTime") or {}
        pens = score.get("penalties") or {}

        # Hent winner og duration for utslagskamper.
        winner   = score.get("winner")    # HOME_TEAM / AWAY_TEAM / DRAW
        duration = score.get("duration") or "REGULAR"
        fd_runde = fd_runde_fra_stage(kamp.get("stage", ""))
        er_utslag = fd_runde in UTSLAGSRUNDER

        rt_hjemme = rt.get("home")
        rt_borte = rt.get("away")
        ft_hjemme = ft.get("home")
        ft_borte = ft.get("away")

        mangler_regulartime = False
        if er_utslag:
            # Alle tips og bonusspørsmål bruker de første 90 minuttene.
            # For kamper avgjort innen 90 minutter kan fullTime brukes som trygg fallback.
            hjemme = rt_hjemme
            borte = rt_borte
            if hjemme is None or borte is None:
                if duration == "REGULAR":
                    hjemme = ft_hjemme
                    borte = ft_borte
                elif status_fd == "FINISHED":
                    mangler_regulartime = True
                    hjemme = None
                    borte = None
        else:
            # Gruppespill har ikke ekstraomganger; fullTime og regularTime er samme grunnlag.
            hjemme = ft_hjemme
            borte = ft_borte
            if hjemme is None or borte is None:
                hjemme = rt_hjemme
                borte = rt_borte

        fd_match_id = kamp.get("id")
        fd_key = str(fd_match_id or kamp_id(team1, team2, dato))

        fd_lookup[fd_key] = {
            "fd_match_id": fd_match_id,
            "hjemmelag": team1,
            "bortelag": team2,
            "fd_hjemmelag": home_raw,
            "fd_bortelag": away_raw,
            "hjemme": hjemme,
            "borte": borte,
            "har_score": hjemme is not None and borte is not None,
            # ferdig betyr her at et gyldig 90-minuttersresultat kan poengberegnes.
            "ferdig": status_fd == "FINISHED" and hjemme is not None and borte is not None,
            "kamp_ferdig_api": status_fd == "FINISHED",
            "mangler_regulartime": mangler_regulartime,
            "fulltime_hjemme": ft_hjemme,
            "fulltime_borte": ft_borte,
            "extratime_hjemme": et.get("home"),
            "extratime_borte": et.get("away"),
            "straffer_hjemme": pens.get("home"),
            "straffer_borte": pens.get("away"),
            "kilde": "football_data_org",
            "winner": winner,
            "duration": duration,
            "status": status_fd,
            "fd_dato": dato,
            "fd_utcDate": utc_date,
            "fd_stage": kamp.get("stage", ""),
            "fd_runde": fd_runde,
            "fd_group": kamp.get("group", ""),
            "fd_kamp_id_basert_paa_dato": kamp_id(team1, team2, dato),
        }

    ferdig_antall  = sum(1 for v in fd_lookup.values() if v["ferdig"])
    paagaar_antall = sum(1 for v in fd_lookup.values() if v["status"] in ("IN_PLAY", "PAUSED"))
    med_score      = sum(1 for v in fd_lookup.values() if v.get("har_score"))
    print(f"  -> {ferdig_antall} ferdigspilte, {paagaar_antall} pågående/pause, {med_score} med score fra football-data.org")

    # Skriv debug-fil så vi kan inspisere hva fd.org faktisk returnerte
    debug_data = {
        "tidspunkt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kamper": {
            fd_key: {
                "fd_match_id": v.get("fd_match_id"),
                "fd_hjemmelag": v.get("fd_hjemmelag"),
                "fd_bortelag": v.get("fd_bortelag"),
                "hjemmelag_normalisert": v.get("hjemmelag"),
                "bortelag_normalisert": v.get("bortelag"),
                "status": v.get("status"),
                "ferdig": v.get("ferdig"),
                "hjemme": v.get("hjemme"),
                "borte": v.get("borte"),
                "fd_dato": v.get("fd_dato"),
                "fd_utcDate": v.get("fd_utcDate"),
                "fd_kamp_id_basert_paa_dato": v.get("fd_kamp_id_basert_paa_dato"),
            }
            for fd_key, v in sorted(fd_lookup.items(), key=lambda x: x[0])
        }
    }
    DEBUG_JSON.write_text(json.dumps(debug_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  -> Skrev fd_debug.json med {len(fd_lookup)} kamper")

    return fd_lookup


def finn_fd_match_for_of(kid, team1, team2, dato, runde, fd_lookup, mapping_data, now_iso):
    """
    Finner riktig football-data.org-kamp for én OpenFootball-kamp.

    Prioritet:
      1. Eksisterende data/kamp-mapping.json
      2. Auto-match på hjemmelag/bortelag + datoavvik maks ±1 dag

    Returnerer (fd, mapping_endret).
    """
    mapping = mapping_data.setdefault("kamper", {})
    eksisterende = mapping.get(kid)

    if eksisterende:
        fd_id = str(eksisterende.get("fd_match_id") or "")
        if fd_id and fd_id in fd_lookup:
            return fd_lookup[fd_id], False
        # Hvis fd_match_id mangler eller ikke finnes i dagens fd.org-payload,
        # prøver vi auto-match på nytt i stedet for å låse oss til en død kobling.
        print(f"    ADVARSEL: Lagret fd_match_id mangler for {kid} — prøver auto-match på nytt")

    of_home_key = normaliser_lagnavn_for_match(team1)
    of_away_key = normaliser_lagnavn_for_match(team2)
    kandidater = []

    for fd_key, fd in fd_lookup.items():
        if normaliser_lagnavn_for_match(fd.get("hjemmelag")) != of_home_key:
            continue
        if normaliser_lagnavn_for_match(fd.get("bortelag")) != of_away_key:
            continue

        avvik = dato_avvik_dager(dato, fd.get("fd_dato"))
        if avvik is None or avvik > 1:
            continue

        kandidater.append((avvik, fd_key, fd))

    if not kandidater:
        return None, False

    kandidater.sort(key=lambda x: (x[0], 0 if x[2].get("har_score") else 1, x[1]))
    beste_avvik = kandidater[0][0]
    beste = [k for k in kandidater if k[0] == beste_avvik]

    # Hvis det finnes flere like gode treff, ikke gjett.
    if len(beste) > 1:
        print(f"    ADVARSEL: Flere mulige fd.org-treff for {kid}: {[x[1] for x in beste]} — hopper over auto-match")
        return None, False

    _, fd_key, fd = beste[0]
    fd_match_id = fd.get("fd_match_id")

    mapping[kid] = {
        "of_kamp_id": kid,
        "of_dato": dato,
        "of_hjemmelag": team1,
        "of_bortelag": team2,
        "runde": runde,
        "fd_match_id": fd_match_id,
        "fd_dato": fd.get("fd_dato"),
        "fd_utcDate": fd.get("fd_utcDate"),
        "fd_hjemmelag": fd.get("fd_hjemmelag"),
        "fd_bortelag": fd.get("fd_bortelag"),
        "fd_hjemmelag_normalisert": fd.get("hjemmelag"),
        "fd_bortelag_normalisert": fd.get("bortelag"),
        "dato_avvik_dager": beste_avvik,
        "match_type": "auto_lag_dato",
        "confidence": "high",
        "sist_matchet": now_iso,
    }
    mapping_data["sist_oppdatert"] = now_iso

    if beste_avvik:
        print(f"    fd.org mapping datoavvik: {kid} -> fd_match_id={fd_match_id} ({team1}–{team2}, OF {dato}, fd {fd.get('fd_dato')})")

    return fd, True


def bruk_fd_paa_base(base, fd, team1, team2):
    """Legger fd.org-status og 90-minuttersresultat inn på en OpenFootball-kamp."""
    if not fd:
        return base

    base["status"] = fd.get("status", base.get("status", "TIMED"))
    base["fd_match_id"] = fd.get("fd_match_id")
    base["dato_fd_org"] = fd.get("fd_dato")
    base["fd_utcDate"] = fd.get("fd_utcDate")
    base["fd_hjemmelag"] = fd.get("fd_hjemmelag")
    base["fd_bortelag"] = fd.get("fd_bortelag")
    base["duration"] = fd.get("duration")
    base["kamp_ferdig_api"] = bool(fd.get("kamp_ferdig_api"))
    base["fd_mangler_regulartime"] = bool(fd.get("mangler_regulartime"))
    for felt in (
        "fulltime_hjemme", "fulltime_borte", "extratime_hjemme", "extratime_borte",
        "straffer_hjemme", "straffer_borte"
    ):
        if fd.get(felt) is not None:
            base[felt] = fd.get(felt)

    if fd.get("har_score"):
        base["hjemme"] = fd.get("hjemme")
        base["borte"] = fd.get("borte")
        base["ferdig"] = fd.get("ferdig", False)
        base["kilde"] = "football_data_org"
        base["kilde_score"] = "football_data_org"
    elif fd.get("mangler_regulartime"):
        # OpenFootball score.ft er resultatet etter 90 minutter og kan brukes
        # som trygg fallback. Varsle bare dersom heller ikke den kilden har score.
        if base.get("ferdig") and base.get("hjemme") is not None and base.get("borte") is not None:
            base["mangler_regulartime"] = False
        else:
            base["hjemme"] = None
            base["borte"] = None
            base["ferdig"] = False
            base["kilde_score"] = None
            base["mangler_regulartime"] = True
    else:
        base["mangler_regulartime"] = False

    # Avansement kan brukes selv om 90-minuttersresultatet mangler.
    if fd.get("kamp_ferdig_api") and fd.get("winner") == "HOME_TEAM":
        base["avanserer"] = team1
    elif fd.get("kamp_ferdig_api") and fd.get("winner") == "AWAY_TEAM":
        base["avanserer"] = team2

    return base


def bygg_resultat_lookup(api_data, fd_lookup, fifa_lookup=None):
    """
    Bygger en dict med OpenFootball kamp_id -> resultat for alle kamper.

    Viktig prinsipp:
      - OpenFootball eier kamp-ID og kampoppsett.
      - football-data.org eier rask score/status.
      - data/kamp-mapping.json kobler fd_match_id tilbake til OpenFootball-ID.

    Format:
      {
        "Australia_Turkey_2026_06_13": {
          hjemme: 2,
          borte: 0,
          ferdig: True,
          runde: "gruppe",
          kilde_score: "football_data_org"
        }
      }
    """
    lookup = {}
    fifa_lookup = fifa_lookup or {}
    mapping_data = les_kamp_mapping()
    mapping_endret = False
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for kamp in api_data.get("matches", []):
        dato  = kamp.get("date", "")
        team1 = kamp.get("team1", "")
        team2 = kamp.get("team2", "")
        score = kamp.get("score", {}) or {}
        ht    = score.get("ft", [None, None])  # OpenFootball: resultat etter 90 minutter
        runde = runde_fra_openfootball_kamp(kamp)

        kid = kamp_id(team1, team2, dato)

        # OpenFootball brukes kun som kampoppsett/canonical kamp-ID.
        # Resultater skal komme fra football-data.org, FIFA Calendar eller manuell fallback.
        base = {
            "hjemmelag": team1,
            "bortelag":  team2,
            "hjemme":    None,
            "borte":     None,
            "ferdig":    False,
            "runde":     runde,
            "dato":      dato,
            "dato_openfootball": dato,
            "status":    "TIMED",
            "kilde":     "openfootball",
            "kilde_score": None,
        }

        fd, endret = finn_fd_match_for_of(kid, team1, team2, dato, runde, fd_lookup, mapping_data, now_iso)
        mapping_endret = mapping_endret or endret
        if fd:
            base = bruk_fd_paa_base(base, fd, team1, team2)
            duration = fd.get("duration", "REGULAR")
            if fd.get("har_score"):
                if fd.get("ferdig"):
                    print(f"    fd.org score: {team1} {fd['hjemme']}-{fd['borte']} {team2} (OF {dato}, fd {fd.get('fd_dato')}) [{duration}]")
                else:
                    print(f"    fd.org pågående: {team1} {fd['hjemme']}-{fd['borte']} {team2} (OF {dato}, fd {fd.get('fd_dato')}) [{fd.get('status')}]")
            else:
                print(f"    fd.org status: {team1}–{team2} (OF {dato}, fd {fd.get('fd_dato')}) [{fd.get('status')}]")

        # FIFA Calendar er sekundær resultatkilde når football-data bulk ikke har score.
        # 0–0 håndteres korrekt via is not None i fifa_kamp_til_resultat().
        if not base.get("kilde_score"):
            fifa = finn_fifa_match_for_of(kid, team1, team2, dato, fifa_lookup)
            if fifa and fifa.get("har_score"):
                base = bruk_fifa_paa_base(base, fifa, team1, team2)
                if fifa.get("ferdig"):
                    print(f"    FIFA score: {team1} {fifa['hjemme']}-{fifa['borte']} {team2} (OF {dato}, FIFA {fifa.get('fifa_dato')}) [FINISHED]")
                else:
                    print(f"    FIFA pågående: {team1} {fifa['hjemme']}-{fifa['borte']} {team2} (OF {dato}, FIFA {fifa.get('fifa_dato')}) [{fifa.get('status')}, {fifa.get('fifa_match_time')}]")

        lookup[kid] = base

    if fd_lookup:
        skriv_kamp_mapping(mapping_data, force=mapping_endret or not KAMP_MAPPING_JSON.exists())

    fd_ferdig_antall = sum(1 for v in lookup.values() if v["ferdig"] and v.get("kilde_score") == "football_data_org")
    fd_paagaar_antall = sum(1 for v in lookup.values() if not v["ferdig"] and v.get("kilde_score") == "football_data_org")
    fifa_ferdig_antall = sum(1 for v in lookup.values() if v["ferdig"] and v.get("kilde_score") == "fifa_calendar")
    fifa_paagaar_antall = sum(1 for v in lookup.values() if not v["ferdig"] and v.get("kilde_score") == "fifa_calendar")
    print(f"  -> {len(lookup)} kamper totalt | football-data.org ferdig: {fd_ferdig_antall} | football-data.org pågående: {fd_paagaar_antall} | FIFA ferdig: {fifa_ferdig_antall} | FIFA pågående: {fifa_paagaar_antall}")
    return lookup



# ── MANUELLE KAMPER / FALLBACK ───────────────────────────────────────────────
def les_manuelle_kamper():
    """
    Leser data/manuelle-kamper.json hvis den finnes.

    Brukes som midlertidig fallback:
      - OpenFootball er master hvis OpenFootball har ferdig resultat.
      - Manuell kamp/resultat brukes hvis OpenFootball mangler kampen eller score.
      - Når OpenFootball senere får resultat for samme kamp_id, vinner OpenFootball automatisk.
    """
    if not MANUELLE_KAMPER_JSON.exists():
        return []

    try:
        data = json.loads(MANUELLE_KAMPER_JSON.read_text(encoding="utf-8"))
        kamper = data.get("kamper", [])
        if not isinstance(kamper, list):
            print("  ADVARSEL: data/manuelle-kamper.json har ikke listefeltet 'kamper'")
            return []
        return kamper
    except Exception as e:
        print(f"  ADVARSEL: Kunne ikke lese manuelle kamper: {e}")
        return []


def normaliser_manuell_kamp(kamp):
    """Normaliserer én manuell/generert kamp til samme format som resultat_lookup."""
    hjemmelag = (kamp.get("hjemmelag") or kamp.get("hjemme_lag") or kamp.get("hjemmeNavn") or "").strip()
    bortelag = (kamp.get("bortelag") or kamp.get("borte_lag") or kamp.get("borteNavn") or "").strip()
    dato = (kamp.get("dato") or kamp.get("date") or "").strip()

    kid = (kamp.get("kamp_id") or kamp.get("id") or "").strip()
    if not kid and hjemmelag and bortelag and dato:
        kid = kamp_id(hjemmelag, bortelag, dato)

    try:
        hjemme_score = kamp.get("hjemme")
        borte_score = kamp.get("borte")
        hjemme_score = int(hjemme_score) if hjemme_score not in (None, "") else None
        borte_score = int(borte_score) if borte_score not in (None, "") else None
    except Exception:
        hjemme_score = None
        borte_score = None

    ferdig = bool(kamp.get("ferdig")) and hjemme_score is not None and borte_score is not None

    item = {
        "hjemmelag": hjemmelag,
        "bortelag": bortelag,
        "hjemme": hjemme_score,
        "borte": borte_score,
        "ferdig": ferdig,
        "runde": kamp.get("runde", "gruppe"),
        "gruppe": kamp.get("gruppe", ""),
        "dato": dato,
        "kilde": "manuell_fallback",
        "kilde_opprinnelig": kamp.get("kilde", "manuell"),
    }
    match_no = parse_int(kamp.get("match_no"))
    if match_no is not None:
        item["match_no"] = match_no
    if kamp.get("slot_hjemme"):
        item["slot_hjemme"] = str(kamp.get("slot_hjemme"))
    if kamp.get("slot_borte"):
        item["slot_borte"] = str(kamp.get("slot_borte"))
    if kamp.get("avanserer"):
        item["avanserer"] = str(kamp.get("avanserer")).strip()
    if kamp.get("utcDate"):
        item["utcDate"] = str(kamp.get("utcDate")).strip()
        item["fd_utcDate"] = str(kamp.get("utcDate")).strip()
    if kamp.get("fd_match_id") is not None:
        item["fd_match_id"] = kamp.get("fd_match_id")
    if kamp.get("fifa_event_id") is not None:
        item["fifa_event_id"] = kamp.get("fifa_event_id")
    return kid, item

def flett_inn_manuelle_kamper(resultat_lookup, manuelle_kamper, fd_lookup=None):
    """
    Fletter manuelle/genererte kamper inn i resultat_lookup.

    Genererte utslagskamper kan ha en annen kamp-ID-dato enn OpenFootball fordi
    den endelige ID-en bruker football-data.org sin utcDate. Derfor kobles de
    også direkte på fd_match_id, slik at tips på den publiserte kamp-ID-en får
    score selv ved datoavvik mellom kildene.
    """
    brukt = 0
    lagt_til = 0
    ignorert = 0
    fd_lookup = fd_lookup or {}

    for raw in manuelle_kamper:
        kid, manuell = normaliser_manuell_kamp(raw)
        if not kid:
            print(f"  ADVARSEL: Hopper over manuell kamp uten kamp_id/lag/dato: {raw}")
            continue

        fd_match = None
        duplicate_fd_keys = []
        if manuell.get("fd_match_id") is not None:
            fd_id = str(manuell.get("fd_match_id"))
            fd_match = fd_lookup.get(fd_id)
            duplicate_fd_keys = [
                other_kid
                for other_kid, other in resultat_lookup.items()
                if other_kid != kid
                and str(other.get("fd_match_id") or "") == fd_id
                and other.get("runde") == manuell.get("runde")
            ]

        # Berik den publiserte kamp-ID-en direkte fra football-data.org.
        if fd_match:
            manuell["status"] = fd_match.get("status", "TIMED")
            manuell["fd_utcDate"] = fd_match.get("fd_utcDate") or manuell.get("fd_utcDate")
            manuell["dato_fd_org"] = fd_match.get("fd_dato") or manuell.get("dato")
            manuell["duration"] = fd_match.get("duration")
            manuell["kamp_ferdig_api"] = bool(fd_match.get("kamp_ferdig_api"))
            manuell["mangler_regulartime"] = bool(fd_match.get("mangler_regulartime"))
            for felt in (
                "fulltime_hjemme", "fulltime_borte", "extratime_hjemme", "extratime_borte",
                "straffer_hjemme", "straffer_borte"
            ):
                if fd_match.get(felt) is not None:
                    manuell[felt] = fd_match.get(felt)
            if fd_match.get("har_score"):
                manuell["hjemme"] = fd_match.get("hjemme")
                manuell["borte"] = fd_match.get("borte")
                manuell["ferdig"] = bool(fd_match.get("ferdig"))
                manuell["kilde_score"] = "football_data_org"
            if fd_match.get("kamp_ferdig_api") and fd_match.get("winner") == "HOME_TEAM":
                manuell["avanserer"] = manuell.get("hjemmelag")
            elif fd_match.get("kamp_ferdig_api") and fd_match.get("winner") == "AWAY_TEAM":
                manuell["avanserer"] = manuell.get("bortelag")

        # Et gyldig manuelt 90-minuttersresultat løser API-varselet.
        if manuell.get("ferdig") and manuell.get("hjemme") is not None and manuell.get("borte") is not None:
            manuell["mangler_regulartime"] = False

        # Den publiserte FD-baserte kamp-ID-en er canonical for genererte R32-kamper.
        # Fjern en eventuell OpenFootball-dublett med samme fd_match_id og annen dato-ID.
        if manuell.get("kilde_opprinnelig") == "football_data_org_r32":
            for duplicate_key in duplicate_fd_keys:
                resultat_lookup.pop(duplicate_key, None)

        eksisterende = resultat_lookup.get(kid)
        if not eksisterende:
            resultat_lookup[kid] = manuell
            lagt_til += 1
            continue

        if eksisterende.get("ferdig"):
            if manuell.get("avanserer") and not eksisterende.get("avanserer"):
                eksisterende["avanserer"] = manuell.get("avanserer")
            if manuell.get("match_no") and not eksisterende.get("match_no"):
                eksisterende["match_no"] = manuell.get("match_no")
            for felt in ("fd_match_id", "fd_utcDate", "fifa_event_id"):
                if manuell.get(felt) is not None and eksisterende.get(felt) is None:
                    eksisterende[felt] = manuell.get(felt)
            ignorert += 1
            continue

        if manuell.get("ferdig"):
            resultat_lookup[kid] = {
                **eksisterende,
                "hjemme": manuell["hjemme"],
                "borte": manuell["borte"],
                "ferdig": True,
                "status": manuell.get("status", "FINISHED"),
                "kilde": "manuell_fallback",
                "kilde_score": manuell.get("kilde_score", "manuell_fallback"),
                # Et manuelt, ferdig 90-minuttersresultat løser API-avviket.
                "mangler_regulartime": False,
            }
            for felt in (
                "avanserer", "match_no", "slot_hjemme", "slot_borte",
                "fd_match_id", "fd_utcDate", "fifa_event_id", "duration",
                "fulltime_hjemme", "fulltime_borte", "extratime_hjemme",
                "extratime_borte", "straffer_hjemme", "straffer_borte"
            ):
                if manuell.get(felt) is not None:
                    resultat_lookup[kid][felt] = manuell.get(felt)
            brukt += 1
        else:
            resultat_lookup[kid] = {
                **manuell,
                **eksisterende,
                "kilde": eksisterende.get("kilde", "openfootball"),
            }

    print(f"  → Manuell fallback: {lagt_til} kamper lagt til, {brukt} resultater brukt, {ignorert} ignorert fordi API har resultat")
    return resultat_lookup

def skriv_mangler_resultater(resultat_lookup):
    """
    Skriver data/mangler-resultater.json.

    Filen brukes av admin-siden og inneholder både kamper som mangler et vanlig
    resultat, og ferdige utslagskamper der API-et mangler resultatet etter de
    første 90 minuttene.
    """
    today = datetime.now(timezone.utc).date()
    mangler = []

    for kid, kamp in resultat_lookup.items():
        dato_txt = kamp.get("dato", "")
        try:
            kamp_dato = datetime.fromisoformat(dato_txt[:10]).date()
        except Exception:
            kamp_dato = None

        if kamp.get("mangler_regulartime"):
            mangler.append({
                "type": "mangler_regulartime",
                "kamp_id": kid,
                "hjemmelag": kamp.get("hjemmelag", ""),
                "bortelag": kamp.get("bortelag", ""),
                "dato": dato_txt,
                "runde": kamp.get("runde", "ukjent"),
                "gruppe": kamp.get("gruppe", ""),
                "status": "mangler_regulartime",
                "status_api": kamp.get("status", "FINISHED"),
                "duration": kamp.get("duration"),
                "fulltime_hjemme": kamp.get("fulltime_hjemme"),
                "fulltime_borte": kamp.get("fulltime_borte"),
                "straffer_hjemme": kamp.get("straffer_hjemme"),
                "straffer_borte": kamp.get("straffer_borte"),
                "avanserer": kamp.get("avanserer", ""),
                "melding": "Kampen er ferdig, men resultatet etter de første 90 minuttene mangler.",
            })
            continue

        if kamp_dato and kamp_dato < today and not kamp.get("ferdig"):
            mangler.append({
                "type": "mangler_resultat",
                "kamp_id": kid,
                "hjemmelag": kamp.get("hjemmelag", ""),
                "bortelag": kamp.get("bortelag", ""),
                "dato": dato_txt,
                "runde": kamp.get("runde", "gruppe"),
                "gruppe": kamp.get("gruppe", ""),
                "status": "mangler_resultat",
            })

    data = {
        "sist_sjekket": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kamper": sorted(mangler, key=lambda x: (x.get("dato", ""), x.get("runde", ""), x.get("hjemmelag", "")))
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MANGLER_RESULTATER_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  → Skrev mangler-resultater.json med {len(mangler)} varsler")

# ── BONUSFRISTER ──────────────────────────────────────────────────────────────
def parse_iso_utc(value):
    try:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def kamp_har_startet(kamp):
    """True når avspark er passert. Brukes for å offentliggjøre låste utslagstips uten å beregne poeng."""
    kickoff = parse_iso_utc(
        (kamp or {}).get("fd_utcDate")
        or (kamp or {}).get("utcDate")
        or (kamp or {}).get("fifa_utcDate")
    )
    return bool(kickoff and datetime.now(timezone.utc) >= kickoff)


def les_bonusfrister():
    """Henter første bekreftede avspark per utslagsrunde fra status.json.

    Bonus skal aldri baseres på bare kalenderdato. Vi bruker tidligste utcDate på
    kampene. Rundens lagrede ``frist`` brukes bare som reserve dersom kampene
    midlertidig mangler tider.
    """
    try:
        status = json.loads(STATUS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    frister = {}
    for runde in UTSLAGSRUNDER:
        round_data = status.get(runde, {}) or {}
        tider = []
        for kamp in round_data.get("kamper", []) or []:
            dt = parse_iso_utc(kamp.get("utcDate") or kamp.get("avspark") or kamp.get("kickoff"))
            if dt:
                tider.append(dt)
        if tider:
            frister[runde] = min(tider)
            continue
        fallback = parse_iso_utc(round_data.get("frist"))
        if fallback:
            frister[runde] = fallback
    return frister


def innlevering_foer_bonusfrist(innlevert, runde, frister):
    """Godtar bonus bare når både innleveringstid og bekreftet frist finnes."""
    frist = frister.get(runde)
    tidspunkt = parse_iso_utc(innlevert)
    return bool(frist and tidspunkt and tidspunkt < frist)


# ── LES TIPPINGER ─────────────────────────────────────────────────────────────
def les_alle_tippinger():
    """
    Leser alle JSON-filer fra tippinger/-mappene.

    For utslagsrunder dedupliseres tips på:
        deltaker_id + runde + kamp_id
    Nyeste meta.innlevert vinner. Dermed kan samme deltaker levere flere ganger
    etter hvert som nye kamper åpnes, og også oppdatere et tidligere tips før avspark.
    """
    deltakere = {}
    bonusfrister = les_bonusfrister()
    runder = ["gruppespill", "r32", "r16", "qf", "sf", "final"]

    for runde in runder:
        mappe = TIPPINGER / runde
        if not mappe.exists():
            continue

        for fil in sorted(mappe.glob("*.json")):
            try:
                with open(fil, encoding="utf-8") as f:
                    data = json.load(f)

                meta = data.get("meta", {}) or {}
                navn = str(meta.get("navn", "")).strip()
                if not navn:
                    print(f"  ADVARSEL: Ingen navn i {fil.name} — hopper over")
                    continue

                requested_type = str(meta.get("deltaker_type", "") or "").strip().lower()
                navn_slug = lag_deltaker_id(navn)
                if runde == "gruppespill":
                    did = navn_slug
                    participant_type = "ordinaer"
                else:
                    did_fra_meta = str(meta.get("deltaker_id", "") or "").strip()
                    if did_fra_meta and did_fra_meta in deltakere:
                        did = did_fra_meta
                        participant_type = deltakere[did].get("deltaker_type", "ordinaer")
                    elif requested_type == "sen_pamelding":
                        did = navn_slug
                        participant_type = "sen_pamelding"
                        if did_fra_meta and did_fra_meta != did:
                            print(
                                f"  ADVARSEL: deltaker_id '{did_fra_meta}' matcher ikke navnet i {fil.name} "
                                f"— bruker '{did}'"
                            )
                    else:
                        did = navn_slug
                        participant_type = "ukjent"
                        if did_fra_meta and did_fra_meta not in deltakere:
                            print(f"  ADVARSEL: Ukjent deltaker_id '{did_fra_meta}' i {fil.name} — bruker navn-slug '{did}'")

                if did not in deltakere:
                    deltakere[did] = {
                        "navn": navn,
                        "deltaker_id": did,
                        "deltaker_type": participant_type,
                        "turneringsvinner": "",
                        "gruppespill": [],
                        "utslagsrunder": [],
                        "bonus": {},
                        "helhetsbonus": {},
                        "_nyeste_utslag": {},
                        "_nyeste_bonus": {},
                        "_nyeste_helhetsbonus": {},
                        "_gruppespill_sort": ("", ""),
                    }
                elif runde == "gruppespill":
                    # En faktisk gruppespillkupong gjør alltid deltakeren ordinær.
                    deltakere[did]["deltaker_type"] = "ordinaer"
                elif (
                    deltakere[did].get("deltaker_type") in (None, "", "ukjent")
                    and participant_type == "sen_pamelding"
                ):
                    deltakere[did]["deltaker_type"] = "sen_pamelding"

                innlevert = str(meta.get("innlevert", "") or "")
                sort_key = (innlevert, fil.name)

                if runde == "gruppespill":
                    if sort_key >= deltakere[did].get("_gruppespill_sort", ("", "")):
                        deltakere[did]["turneringsvinner"] = data.get("turneringsvinner", "")
                        deltakere[did]["gruppespill"] = data.get("gruppespill", [])
                        deltakere[did]["_gruppespill_sort"] = sort_key
                    continue

                latest = deltakere[did].setdefault("_nyeste_utslag", {})
                for raw_tip in data.get("tippinger", []) or []:
                    if not isinstance(raw_tip, dict):
                        continue
                    kid = str(raw_tip.get("kamp_id", "") or "").strip()
                    if not kid:
                        continue
                    dedupe_key = f"{runde}:{kid}"
                    current = latest.get(dedupe_key)
                    if current is None or sort_key >= current["sort"]:
                        tip = dict(raw_tip)
                        tip["runde"] = runde
                        latest[dedupe_key] = {"sort": sort_key, "tip": tip, "fil": fil.name}

                if data.get("bonus"):
                    if innlevering_foer_bonusfrist(innlevert, runde, bonusfrister):
                        bonus_latest = deltakere[did].setdefault("_nyeste_bonus", {})
                        current = bonus_latest.get(runde)
                        if current is None or sort_key >= current["sort"]:
                            bonus_latest[runde] = {"sort": sort_key, "bonus": data.get("bonus")}
                    else:
                        print(f"  ADVARSEL: Sen bonusinnlevering ignorert i {fil.name} ({runde})")

                if runde == "r32" and isinstance(data.get("helhetsbonus"), dict):
                    if innlevering_foer_bonusfrist(innlevert, "r32", bonusfrister):
                        helhet_latest = deltakere[did].setdefault("_nyeste_helhetsbonus", {})
                        for bonus_id, svar in data.get("helhetsbonus", {}).items():
                            if bonus_id not in HELHETSBONUS_SPORSMAL or svar in (None, ""):
                                continue
                            current = helhet_latest.get(bonus_id)
                            if current is None or sort_key >= current["sort"]:
                                helhet_latest[bonus_id] = {"sort": sort_key, "svar": svar}
                    else:
                        print(f"  ADVARSEL: Sen helhetsbonus ignorert i {fil.name}")

            except Exception as e:
                print(f"  FEIL ved lesing av {fil}: {e}")

    for deltaker in deltakere.values():
        latest = deltaker.pop("_nyeste_utslag", {})
        deltaker["utslagsrunder"] = [
            entry["tip"]
            for _, entry in sorted(
                latest.items(),
                key=lambda item: (
                    item[1]["tip"].get("runde", ""),
                    item[1]["tip"].get("kamp_id", ""),
                ),
            )
        ]
        bonus_latest = deltaker.pop("_nyeste_bonus", {})
        deltaker["bonus"] = {runde: entry["bonus"] for runde, entry in bonus_latest.items()}
        helhet_latest = deltaker.pop("_nyeste_helhetsbonus", {})
        deltaker["helhetsbonus"] = {bonus_id: entry["svar"] for bonus_id, entry in helhet_latest.items()}
        deltaker.pop("_gruppespill_sort", None)

    print(f"  → {len(deltakere)} deltakere funnet")
    return deltakere

def skriv_deltakere_json(deltakere):
    """
    Skriver data/deltakere.json — en liste over alle kjente deltakere.
    Brukes av utslagsrunde-appen til å bygge 'Hvem er du?'-dropdown.

    Format:
    [
      { "id": "kari_nordmann", "navn": "Kari Nordmann" },
      ...
    ]
    """
    liste = sorted(
        [
            {
                "id": d["deltaker_id"],
                "navn": d["navn"],
                "deltaker_type": d.get("deltaker_type", "ordinaer"),
            }
            for d in deltakere.values()
        ],
        key=lambda x: x["navn"].lower()
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DELTAKERE_JSON.write_text(
        json.dumps(liste, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"  → Skrev deltakere.json med {len(liste)} deltakere")

# ── REGN POENG ────────────────────────────────────────────────────────────────
def regn_poeng_for_kamp(tippa_h, tippa_b, faktisk_h, faktisk_b, runde, tippa_vinner=None, faktisk_vinner=None):
    """
    Regner poeng for én kamp.

    Felles regel for gruppespill og utslagsrunder:
      - Riktig utfall etter ordinær tid / 90 minutter: runde-poeng
      - Eksakt resultat etter ordinær tid / 90 minutter: +eksakt-poeng

    Ekstraomganger, straffer og hvilket lag som går videre brukes ikke i
    kamp-poengberegningen. Parametrene tippa_vinner/faktisk_vinner beholdes
    kun for bakoverkompatibilitet med eldre innleveringer, men ignoreres.
    """
    if faktisk_h is None or faktisk_b is None:
        return 0, False, False  # Kamp ikke ferdigspilt

    if tippa_h is None or tippa_b is None:
        return 0, False, False  # Mangler tips

    poeng_config  = POENG.get(runde, POENG["gruppe"])
    poeng         = 0
    riktig_utfall = False
    eksakt        = False

    # Riktig H/U/B etter 90 minutter
    if utfall(tippa_h, tippa_b) == utfall(faktisk_h, faktisk_b):
        poeng += poeng_config["utfall"]
        riktig_utfall = True

    # Eksakt resultat etter 90 minutter
    if tippa_h == faktisk_h and tippa_b == faktisk_b:
        poeng += poeng_config["eksakt"]
        eksakt = True

    return poeng, riktig_utfall, eksakt

def bonus_fasit_for_runde(runde, resultat_lookup):
    """
    Returnerer (fasit, ferdig) for bonusspørsmålet i en utslagsrunde.
    Bonus regnes først når forventet antall kamper i runden har ferdig 90-minuttersresultat.
    """
    kamper = [
        k for k in resultat_lookup.values()
        if k.get("runde") == runde and k.get("ferdig") and k.get("hjemme") is not None and k.get("borte") is not None
    ]
    forventet = FORVENTET_ANTALL.get(runde)
    if not forventet or len(kamper) < forventet:
        return None, False

    if runde == "r32":
        return sum(1 for k in kamper if k["hjemme"] == k["borte"]), True
    if runde == "r16":
        return sum((1 if k["hjemme"] == 0 else 0) + (1 if k["borte"] == 0 else 0) for k in kamper), True
    if runde == "qf":
        return sum(1 for k in kamper if abs(k["hjemme"] - k["borte"]) == 1), True
    if runde == "sf":
        return sum(k["hjemme"] + k["borte"] for k in kamper), True
    if runde == "final":
        k = kamper[0]
        return "ja" if k["hjemme"] > 0 and k["borte"] > 0 else "nei", True
    return None, False


def normaliser_bonus_svar(svar):
    if svar is None:
        return None
    if isinstance(svar, str):
        s = svar.strip().lower()
        if s == "":
            return None
        if s in ("ja", "nei"):
            return s
        try:
            return int(s)
        except Exception:
            return s
    return svar


def normaliser_tekst_svar(value):
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def kategoriser_totale_maal(totale_maal):
    if totale_maal <= 46:
        return "0_46"
    if totale_maal <= 77:
        return "47_77"
    return "78_pluss"


def visningsverdi_maalintervall(value):
    return {
        "0_46": "0–46 mål",
        "47_77": "47–77 mål",
        "78_pluss": "78 mål eller flere",
    }.get(value, value)


def bygg_match_no_lookup(resultat_lookup):
    """Lager en ekstra oppslagsnøkkel for utslagskamper: (runde, match_no) -> kamp.

    Dette gjør poengregningen robust dersom kamp_id inneholder en gammel dato,
    men match_no fortsatt er den samme offisielle FIFA-kampen.
    """
    lookup = {}
    for kamp in resultat_lookup.values():
        runde = kamp.get("runde")
        if runde not in UTSLAGSRUNDER:
            continue
        match_no = parse_int(kamp.get("match_no") or kamp.get("fifa_match_no"))
        if match_no is None:
            continue
        key = (runde, match_no)
        eksisterende = lookup.get(key)
        if eksisterende is None:
            lookup[key] = kamp
            continue
        # Foretrekk kamp som faktisk kan poengberegnes.
        ny_score = kamp.get("ferdig") and kamp.get("hjemme") is not None and kamp.get("borte") is not None
        gammel_score = eksisterende.get("ferdig") and eksisterende.get("hjemme") is not None and eksisterende.get("borte") is not None
        if ny_score and not gammel_score:
            lookup[key] = kamp
    return lookup


def unike_utslagskamper(resultat_lookup):
    """Returnerer én canonical kamp per utslagskamp, uten alias-/kildedubletter."""
    valgte = {}
    for kid, kamp in resultat_lookup.items():
        runde = kamp.get("runde")
        if runde not in UTSLAGSRUNDER:
            continue
        if kamp.get("match_no") is not None:
            nokkel = (runde, "match_no", str(kamp.get("match_no")))
        elif kamp.get("fd_match_id") is not None:
            nokkel = (runde, "fd", str(kamp.get("fd_match_id")))
        else:
            nokkel = (runde, "kamp", kid)
        eksisterende = valgte.get(nokkel)
        if not eksisterende:
            valgte[nokkel] = kamp
            continue
        # Foretrekk oppføringen som har gyldig 90-minuttersresultat.
        score_ny = kamp.get("ferdig") and kamp.get("hjemme") is not None and kamp.get("borte") is not None
        score_gammel = eksisterende.get("ferdig") and eksisterende.get("hjemme") is not None and eksisterende.get("borte") is not None
        if score_ny and not score_gammel:
            valgte[nokkel] = kamp
    return list(valgte.values())


def bygg_bonusstatus(resultat_lookup, spillerstatistikk):
    """Bygger offentlig status og fasitgrunnlag for alle bonusspørsmål."""
    utslagskamper = unike_utslagskamper(resultat_lookup)
    ferdige = [
        kamp for kamp in utslagskamper
        if kamp.get("ferdig") and kamp.get("hjemme") is not None and kamp.get("borte") is not None
    ]
    totalt_forventet = sum(FORVENTET_ANTALL.values())
    alle_ferdige = len(ferdige) >= totalt_forventet

    maal_per_lag = {}
    totale_maal = 0
    for kamp in ferdige:
        h = int(kamp.get("hjemme", 0))
        b = int(kamp.get("borte", 0))
        totale_maal += h + b
        hjemmelag = kamp.get("hjemmelag", "")
        bortelag = kamp.get("bortelag", "")
        if hjemmelag:
            maal_per_lag[hjemmelag] = maal_per_lag.get(hjemmelag, 0) + h
        if bortelag:
            maal_per_lag[bortelag] = maal_per_lag.get(bortelag, 0) + b

    maks_maal = max(maal_per_lag.values(), default=0)
    ledere = sorted([lag for lag, antall in maal_per_lag.items() if antall == maks_maal and antall > 0])
    maal_kategori = kategoriser_totale_maal(totale_maal)
    toppscorer = spillerstatistikk[0] if spillerstatistikk else None

    utslagsfasen_startet = any(kamp_har_startet(k) for k in utslagskamper)

    runder = {}
    for runde in UTSLAGSRUNDER:
        r_kamper = [k for k in ferdige if k.get("runde") == runde]
        runde_startet = any(kamp_har_startet(k) for k in utslagskamper if k.get("runde") == runde)
        forventet = FORVENTET_ANTALL.get(runde, 0)
        ferdig_runde = forventet > 0 and len(r_kamper) >= forventet
        runde_status = "ferdig" if ferdig_runde else ("paagaar" if (runde_startet or r_kamper) else "ikke_startet")
        if runde == "r32":
            verdi = sum(1 for k in r_kamper if k["hjemme"] == k["borte"])
        elif runde == "r16":
            verdi = sum((1 if k["hjemme"] == 0 else 0) + (1 if k["borte"] == 0 else 0) for k in r_kamper)
        elif runde == "qf":
            verdi = sum(1 for k in r_kamper if abs(k["hjemme"] - k["borte"]) == 1)
        elif runde == "sf":
            verdi = sum(k["hjemme"] + k["borte"] for k in r_kamper)
        elif runde == "final":
            verdi = None if not r_kamper else ("ja" if r_kamper[0]["hjemme"] > 0 and r_kamper[0]["borte"] > 0 else "nei")
        else:
            verdi = None
        runder[runde] = {
            "status": runde_status,
            "ferdige_kamper": len(r_kamper),
            "totalt_kamper": forventet,
            "verdi": verdi,
            "fasit": verdi if ferdig_runde else None,
            "sporsmal": BONUS_SPORSMAL[runde]["tekst"],
        }

    return {
        "helhet": {
            "flest_maal_lag": {
                "status": "ferdig" if alle_ferdige else ("paagaar" if (utslagsfasen_startet or ferdige) else "ikke_startet"),
                "ledere": ledere,
                "antall_maal": maks_maal,
                "ferdige_kamper": len(ferdige),
                "totalt_kamper": totalt_forventet,
                "fasit": ledere if alle_ferdige else None,
                "sporsmal": HELHETSBONUS_SPORSMAL["flest_maal_lag"]["tekst"],
            },
            "totale_maal_utslag": {
                "status": "ferdig" if alle_ferdige else ("paagaar" if (utslagsfasen_startet or ferdige) else "ikke_startet"),
                "antall": totale_maal,
                "forelopig_kategori": maal_kategori,
                "forelopig_kategori_tekst": visningsverdi_maalintervall(maal_kategori),
                "ferdige_kamper": len(ferdige),
                "totalt_kamper": totalt_forventet,
                "fasit": maal_kategori if alle_ferdige else None,
                "fasit_tekst": visningsverdi_maalintervall(maal_kategori) if alle_ferdige else None,
                "sporsmal": HELHETSBONUS_SPORSMAL["totale_maal_utslag"]["tekst"],
            },
            "golden_boot": {
                "status": "ferdig" if alle_ferdige and toppscorer else ("forelopig" if toppscorer else "avventer_fifa"),
                "spiller": toppscorer.get("spiller") if toppscorer else None,
                "maal": toppscorer.get("maal") if toppscorer else None,
                "assists": toppscorer.get("assists") if toppscorer else None,
                "minutter": toppscorer.get("minutter") if toppscorer else None,
                "fasit": toppscorer.get("spiller") if alle_ferdige and toppscorer else None,
                "kilde": "FIFA",
                "sporsmal": HELHETSBONUS_SPORSMAL["golden_boot"]["tekst"],
            },
        },
        "runder": runder,
    }


def bonus_svar_er_offentlig(runde, bonusfrister, bonusstatus):
    """Svar blir offentlige når fristen er passert, aldri mens spørsmålet er åpent."""
    now = datetime.now(timezone.utc)
    frist = (bonusfrister or {}).get("r32" if runde == "helhet" else runde)
    if frist:
        return now >= frist
    # Fallback hvis status.json mangler frist: vis først når minst én relevant kamp er ferdig.
    if runde == "helhet":
        return ((bonusstatus or {}).get("helhet", {}).get("totale_maal_utslag", {}).get("ferdige_kamper", 0) or 0) > 0
    return ((bonusstatus or {}).get("runder", {}).get(runde, {}).get("ferdige_kamper", 0) or 0) > 0


def regn_poeng_helhetsbonus(deltaker, bonusstatus, vis_svar=False):
    svarene = deltaker.get("helhetsbonus", {}) or {}
    detaljer = []
    poeng = 0
    helhet = (bonusstatus or {}).get("helhet", {})

    for bonus_id, config in HELHETSBONUS_SPORSMAL.items():
        svar = svarene.get(bonus_id)
        status = helhet.get(bonus_id, {})
        fasit = status.get("fasit")
        ferdig = status.get("status") == "ferdig" and fasit is not None
        riktig = False
        fasit_visning = fasit

        if bonus_id == "flest_maal_lag":
            riktige_lag = fasit if isinstance(fasit, list) else []
            riktig = ferdig and any(normaliser_tekst_svar(svar) == normaliser_tekst_svar(lag) for lag in riktige_lag)
            fasit_visning = ", ".join(riktige_lag) if riktige_lag else None
        elif bonus_id == "totale_maal_utslag":
            riktig = ferdig and str(svar or "") == str(fasit or "")
            fasit_visning = visningsverdi_maalintervall(fasit) if fasit else None
            svar = visningsverdi_maalintervall(svar) if svar else svar
        elif bonus_id == "golden_boot":
            riktig = ferdig and normaliser_tekst_svar(svar) == normaliser_tekst_svar(fasit)

        p = POENG_HELHETSBONUS if riktig else 0
        poeng += p
        if svar not in (None, "") and (vis_svar or ferdig):
            detaljer.append({
                "type": "helhetsbonus",
                "id": bonus_id,
                "runde": "helhet",
                "sporsmal": config["tekst"],
                "svar": svar,
                "fasit": fasit_visning,
                "poeng": p,
                "riktig": riktig,
                "ferdig": ferdig,
            })
    return poeng, detaljer


def regn_poeng_deltaker(
    deltaker, resultat_lookup, faktisk_turneringsvinner=None, ascii_lookup=None,
    bonusstatus=None, bonusfrister=None, match_no_lookup=None
):
    """Regner totale poeng for én deltaker."""
    navn                 = deltaker["navn"]
    poeng_totalt         = 0
    poeng_gruppespill    = 0
    poeng_utslagsrunder  = 0
    poeng_bonus          = 0
    poeng_helhetsbonus   = 0
    poeng_turneringsvinner = 0
    tipping_detaljer     = []

    # ── Gruppespill ──
    for t in deltaker.get("gruppespill", []):
        kid = t.get("kamp_id", "")
        res = resultat_lookup.get(kid)
        if res is None and ascii_lookup:
            res = ascii_lookup.get(kid)

        if not res or not res["ferdig"]:
            tipping_detaljer.append({
                "kamp_id":   kid,
                "hjemmelag": res["hjemmelag"] if res else "",
                "bortelag":  res["bortelag"]  if res else "",
                "tippa_h":   t.get("hjemme"),
                "tippa_b":   t.get("borte"),
                "faktisk_h": res["hjemme"] if res else None,
                "faktisk_b": res["borte"]  if res else None,
                "poeng":     0,
                "ferdig":    False,
                "status":    res.get("status", "TIMED") if res else "TIMED",
                "runde":     "gruppe",
            })
            continue

        p, riktig, eksakt = regn_poeng_for_kamp(
            t.get("hjemme"), t.get("borte"),
            res["hjemme"], res["borte"],
            "gruppe"
        )
        poeng_gruppespill += p
        tipping_detaljer.append({
            "kamp_id":       kid,
            "hjemmelag":     res["hjemmelag"],
            "bortelag":      res["bortelag"],
            "tippa_h":       t.get("hjemme"),
            "tippa_b":       t.get("borte"),
            "faktisk_h":     res["hjemme"],
            "faktisk_b":     res["borte"],
            "poeng":         p,
            "riktig_utfall": riktig,
            "eksakt":        eksakt,
            "ferdig":        True,
            "status":        res.get("status", "FINISHED"),
            "runde":         "gruppe",
        })

    # ── Utslagsrunder ──
    for t in deltaker.get("utslagsrunder", []):
        kid   = t.get("kamp_id", "")
        runde = t.get("runde", "r32")
        res   = resultat_lookup.get(kid)
        if res is None and ascii_lookup:
            res = ascii_lookup.get(kid)
        if res is None and match_no_lookup:
            match_no = parse_int(t.get("match_no"))
            if match_no is not None:
                res = match_no_lookup.get((runde, match_no))

        if not res or not res["ferdig"]:
            # Utslagstips skal ikke være synlige før avspark.
            # Når kampen har startet er tipset låst, og kan trygt vises med 0 poeng
            # frem til 90-minuttersresultatet foreligger.
            if res and kamp_har_startet(res):
                tipping_detaljer.append({
                    "kamp_id":   kid,
                    "hjemmelag": res.get("hjemmelag", ""),
                    "bortelag":  res.get("bortelag", ""),
                    "tippa_h":   t.get("hjemme"),
                    "tippa_b":   t.get("borte"),
                    "faktisk_h": res.get("hjemme"),
                    "faktisk_b": res.get("borte"),
                    "poeng":     0,
                    "ferdig":    False,
                    "status":    res.get("status", "TIMED"),
                    "runde":     runde,
                })
            continue

        p, riktig, eksakt = regn_poeng_for_kamp(
            t.get("hjemme"), t.get("borte"),
            res["hjemme"], res["borte"],
            runde
        )
        poeng_utslagsrunder += p
        tipping_detaljer.append({
            "kamp_id":        kid,
            "hjemmelag":      res["hjemmelag"],
            "bortelag":       res["bortelag"],
            "tippa_h":        t.get("hjemme"),
            "tippa_b":        t.get("borte"),
            "faktisk_h":      res["hjemme"],
            "faktisk_b":      res["borte"],
            "poeng":          p,
            "riktig_utfall":  riktig,
            "eksakt":         eksakt,
            "ferdig":         True,
            "runde":          runde,
        })

    # ── Bonusspørsmål i utslagsrunder ──
    for runde, bonus in deltaker.get("bonus", {}).items():
        if runde not in BONUS_SPORSMAL:
            continue
        fasit, ferdig_bonus = bonus_fasit_for_runde(runde, resultat_lookup)
        svar = normaliser_bonus_svar(bonus.get("svar") if isinstance(bonus, dict) else bonus)
        fasit_norm = normaliser_bonus_svar(fasit)
        riktig_bonus = ferdig_bonus and svar is not None and fasit_norm is not None and svar == fasit_norm
        p_bonus = POENG_BONUS if riktig_bonus else 0
        poeng_bonus += p_bonus
        if bonus_svar_er_offentlig(runde, bonusfrister, bonusstatus) or ferdig_bonus:
            tipping_detaljer.append({
                "type": "bonus",
                "runde": runde,
                "sporsmal": BONUS_SPORSMAL[runde]["tekst"],
                "svar": svar,
                "fasit": fasit,
                "poeng": p_bonus,
                "riktig": riktig_bonus,
                "ferdig": ferdig_bonus,
            })

    # ── Bonusspørsmål for hele utslagsfasen ──
    vis_helhet = bonus_svar_er_offentlig("helhet", bonusfrister, bonusstatus)
    poeng_helhetsbonus, helhetsdetaljer = regn_poeng_helhetsbonus(
        deltaker, bonusstatus or {}, vis_svar=vis_helhet
    )
    tipping_detaljer.extend(helhetsdetaljer)

    # ── Turneringsvinner ──
    if faktisk_turneringsvinner and deltaker.get("turneringsvinner"):
        if deltaker["turneringsvinner"] == faktisk_turneringsvinner:
            poeng_turneringsvinner = POENG_TURNERINGSVINNER

    poeng_totalt = poeng_gruppespill + poeng_utslagsrunder + poeng_bonus + poeng_helhetsbonus + poeng_turneringsvinner

    return {
        "navn":                    navn,
        "deltaker_id":             deltaker.get("deltaker_id", ""),   # ← NY
        "deltaker_type":           deltaker.get("deltaker_type", "ordinaer"),
        "sen_pamelding":           deltaker.get("deltaker_type") == "sen_pamelding",
        "poeng_totalt":            poeng_totalt,
        "poeng_start":             0,
        "startpoeng_status":       "venter" if deltaker.get("deltaker_type") == "sen_pamelding" else "ikke_aktuelt",
        "poeng_gruppespill":       poeng_gruppespill,
        "poeng_utslagsrunder":     poeng_utslagsrunder,
        "poeng_bonus":            poeng_bonus,
        "poeng_helhetsbonus":     poeng_helhetsbonus,
        "poeng_turneringsvinner":  poeng_turneringsvinner,
        "turneringsvinner":        deltaker.get("turneringsvinner", ""),
        "turneringsvinner_riktig": poeng_turneringsvinner > 0,
        "tippinger":               tipping_detaljer,
    }

# ── SKRIV DATA.JS ─────────────────────────────────────────────────────────────
def bygg_public_resultater(resultat_lookup):
    """
    Bygger canonical resultatliste til frontend.

    Nøkkel er OpenFootball-kamp_id. Hvis en kamp_id inneholder spesialtegn
    som kan ha blitt ASCII-normalisert av gammel JavaScript, legges det også
    inn en alias-nøkkel som peker på samme resultat.
    """
    resultater = {}

    for kid, kamp in sorted(resultat_lookup.items(), key=lambda x: (x[1].get("dato", ""), x[0])):
        item = {
            "kamp_id": kid,
            "canonical_kamp_id": kid,
            "hjemmelag": kamp.get("hjemmelag", ""),
            "bortelag": kamp.get("bortelag", ""),
            "hjemme": kamp.get("hjemme"),
            "borte": kamp.get("borte"),
            "ferdig": bool(kamp.get("ferdig")),
            "status": kamp.get("status", "FINISHED" if kamp.get("ferdig") else "TIMED"),
            "runde": kamp.get("runde", "gruppe"),
            "dato_openfootball": kamp.get("dato_openfootball") or kamp.get("dato", ""),
            "kilde_score": kamp.get("kilde_score") or kamp.get("kilde", ""),
        }

        for felt in ("dato_fd_org", "fd_match_id", "fd_utcDate", "fd_hjemmelag", "fd_bortelag", "fifa_event_id", "fifa_match_no", "fifa_utcDate", "fifa_dato", "avanserer", "match_no"):
            if kamp.get(felt) is not None:
                item[felt] = kamp.get(felt)

        resultater[kid] = item

        ascii_kid = kamp_id_til_ascii(kid)
        if ascii_kid != kid and ascii_kid not in resultater:
            resultater[ascii_kid] = {
                **item,
                "kamp_id": ascii_kid,
                "canonical_kamp_id": kid,
                "alias_for": kid,
            }

    return resultater


def skriv_data_js(stilling, sist_oppdatert, resultat_lookup=None, bonusstatus=None, spillerstatistikk=None, startpoengstatus=None):
    """Skriver ferdig data.js som leaderboard-siden leser."""

    # Sorter etter poeng totalt
    stilling_sortert = sorted(stilling, key=lambda x: x["poeng_totalt"], reverse=True)

    # Legg til plassering
    for i, d in enumerate(stilling_sortert):
        d["plass"] = i + 1

    payload = {
        "sist_oppdatert": sist_oppdatert,
        "resultater": bygg_public_resultater(resultat_lookup or {}),
        "bonusstatus": bonusstatus or {"helhet": {}, "runder": {}},
        "spillerstatistikk": spillerstatistikk or [],
        "startpoengstatus": startpoengstatus or {"status": "ikke_beregnet"},
        "stilling": stilling_sortert,
    }

    js_innhold = f"""// Denne filen genereres automatisk av GitHub Actions
// Ikke rediger manuelt — endringer overskrives ved neste kjøring
// Sist oppdatert: {sist_oppdatert}

const VM_DATA = {json.dumps(payload, ensure_ascii=False, indent=2)};
"""

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_JS.write_text(js_innhold, encoding="utf-8")
    print(f"  → Skrev data.js med {len(stilling_sortert)} deltakere og {len(payload['resultater'])} resultatoppføringer")

# ── OPPDATER STATUS.JSON ──────────────────────────────────────────────────────

# Hvilken runde som må være ferdig før neste åpnes
FORRIGE_RUNDE = {
    "r32":   "gruppe",
    "r16":   "r32",
    "qf":    "r16",
    "sf":    "qf",
    "final": "sf",
}

RUNDE_REKKEFOLGE = ["r32", "r16", "qf", "sf", "final"]
FORVENTET_ANTALL = {"r32": 16, "r16": 8, "qf": 4, "sf": 2, "final": 1}
KAMP_NR_START = {"r32": 73, "r16": 89, "qf": 97, "sf": 101, "final": 104}
NESTE_RUNDE = {"r32": "r16", "r16": "qf", "qf": "sf", "sf": "final"}

def er_kjent_lag(navn):
    """Returnerer False for placeholders som W74, 1A, 3A/B/C, TBD osv."""
    if not navn or not str(navn).strip():
        return False
    n = str(navn).strip()
    low = n.lower()
    if "/" in n:
        return False
    if re.fullmatch(r"[123][A-L]", n, re.I):
        return False
    if re.fullmatch(r"W\d+", n, re.I):
        return False
    PLASSHOLDERE = ["winner", "loser", "path", "tbd", "place", "runner"]
    return not any(p in low for p in PLASSHOLDERE)

def match_no_for(runde, index, kamp=None):
    if kamp and kamp.get("match_no") is not None:
        try:
            return int(kamp.get("match_no"))
        except Exception:
            pass
    base = KAMP_NR_START.get(runde)
    return base + index if base is not None else None

def sikre_status_metadata(status):
    """Legger på match_no og originale slots uten å endre synlig kampoppsett."""
    for runde in RUNDE_REKKEFOLGE:
        kamper = status.get(runde, {}).get("kamper", [])
        for index, kamp in enumerate(kamper):
            kamp.setdefault("match_no", match_no_for(runde, index, kamp))
            kamp.setdefault("slot_hjemme", kamp.get("slot_hjemme") or kamp.get("hjemme", ""))
            kamp.setdefault("slot_borte", kamp.get("slot_borte") or kamp.get("borte", ""))

def status_id_til_match_no(status):
    out = {}
    for runde in RUNDE_REKKEFOLGE:
        for index, kamp in enumerate(status.get(runde, {}).get("kamper", [])):
            mn = match_no_for(runde, index, kamp)
            if kamp.get("id"):
                out[kamp["id"]] = mn
    return out

def legg_match_no_fra_status(resultat_lookup, status):
    idmap = status_id_til_match_no(status)
    for kid, kamp in resultat_lookup.items():
        if kamp.get("runde") in RUNDE_REKKEFOLGE and not kamp.get("match_no") and kid in idmap:
            kamp["match_no"] = idmap[kid]

def runde_ferdig_i_status(status, resultat_lookup, runde):
    if runde == "gruppe":
        gruppekamper = [v for v in resultat_lookup.values() if v.get("runde") == "gruppe"]
        return bool(gruppekamper) and all(k.get("ferdig") for k in gruppekamper)
    kamper = status.get(runde, {}).get("kamper", [])
    if not kamper:
        return False
    ferdige = 0
    for kamp in kamper:
        kid = kamp.get("id")
        if kid and resultat_lookup.get(kid, {}).get("ferdig"):
            ferdige += 1
    return ferdige == len(kamper)

def alle_lag_kjente_i_status(status, runde):
    kamper = status.get(runde, {}).get("kamper", [])
    return bool(kamper) and all(er_kjent_lag(k.get("hjemme")) and er_kjent_lag(k.get("borte")) for k in kamper)

def bygg_avanserer_by_match_no(resultat_lookup):
    out = {}
    for kamp in resultat_lookup.values():
        runde = kamp.get("runde")
        if runde not in RUNDE_REKKEFOLGE:
            continue
        mn = kamp.get("match_no")
        if mn is None:
            continue
        avanserer = finn_avanserer(kamp)
        if avanserer:
            out[int(mn)] = avanserer
    return out

def slot_vinner(slot, avanserer_by_no):
    slot = str(slot or "").strip()
    m = re.fullmatch(r"W(\d+)", slot, re.I)
    if not m:
        return slot if er_kjent_lag(slot) else None
    return avanserer_by_no.get(int(m.group(1)))

def oppdater_status_med_api_kamper(status, resultat_lookup):
    """
    Oppdaterer hver utslagskamp på stabilt match_no.

    Dette er bevisst per kamp, ikke per komplett runde. Dermed kan M74 bli
    tippebar straks begge lag er kjent, selv om andre R32-kamper fortsatt har
    placeholders.
    """
    source_priority = {
        "manuell_fallback": 1,
        "openfootball": 2,
        "football_data_org": 3,
    }

    by_round_and_no = {}
    for kid, value in resultat_lookup.items():
        runde = value.get("runde")
        if runde not in RUNDE_REKKEFOLGE:
            continue
        match_no = parse_int(value.get("match_no"))
        if match_no is None:
            continue
        if not (er_kjent_lag(value.get("hjemmelag")) and er_kjent_lag(value.get("bortelag"))):
            continue

        key = (runde, match_no)
        current = by_round_and_no.get(key)
        priority = source_priority.get(value.get("kilde"), 0)
        if current is None or priority >= current[2]:
            by_round_and_no[key] = (kid, value, priority)

    for runde in RUNDE_REKKEFOLGE:
        for index, kamp in enumerate(status.get(runde, {}).get("kamper", [])):
            match_no = match_no_for(runde, index, kamp)
            selected = by_round_and_no.get((runde, match_no))
            if not selected:
                continue
            kid, value, _ = selected
            dato = value.get("dato_fd_org") or value.get("dato") or kamp.get("dato", "")
            utc_date = value.get("fd_utcDate") or value.get("utcDate") or ""
            kamp.update({
                "id": kid,
                "hjemme": value.get("hjemmelag", ""),
                "borte": value.get("bortelag", ""),
                "dato": dato,
                "info": "Kampoppsett bekreftet",
                "match_no": match_no,
            })
            if utc_date:
                kamp["utcDate"] = utc_date
            if value.get("fd_match_id") is not None:
                kamp["fd_match_id"] = value.get("fd_match_id")
            if value.get("fifa_event_id") is not None:
                kamp["fifa_event_id"] = value.get("fifa_event_id")
            if value.get("avanserer"):
                kamp["avanserer"] = value.get("avanserer")

def reset_status_til_originale_slots(status):
    """
    Bygger statusvisningen opp igjen fra opprinnelige bracket-slots før
    bekreftede kampdata legges på. Dynamiske felter beregnes på nytt hver kjøring.
    """
    for runde in RUNDE_REKKEFOLGE:
        for index, kamp in enumerate(status.get(runde, {}).get("kamper", [])):
            match_no = match_no_for(runde, index, kamp)
            slot_h = kamp.get("slot_hjemme") or kamp.get("hjemme", "")
            slot_b = kamp.get("slot_borte") or kamp.get("borte", "")
            dato = kamp.get("dato", "")

            kamp["match_no"] = match_no
            kamp["slot_hjemme"] = slot_h
            kamp["slot_borte"] = slot_b
            kamp["hjemme"] = slot_h
            kamp["borte"] = slot_b
            kamp["id"] = kamp_id(slot_h, slot_b, dato)
            kamp["info"] = ""
            # Behold utcDate. Tidspunktet er knyttet til offisielt match_no/slot,
            # ikke til lagene, og må ikke slettes ved reset. Hvis FIFA senere
            # endrer avsparkstid, oppdateres utcDate av oppdater_status_med_fifa_tidspunkter().
            for felt in (
                "avanserer", "fd_match_id", "fifa_event_id",
                "tippebar", "tippe_status"
            ):
                kamp.pop(felt, None)

def autofyll_neste_runder(status, resultat_lookup):
    """
    Fyller neste utslagsrunde basert på feltet `avanserer`.

    Poeng beregnes fortsatt kun fra 90-minuttersresultatet. `avanserer` brukes bare
    for bracket-bygging. Hvis bare én side av neste kamp er kjent, fylles bare den
    siden, mens den andre siden beholder Wxx-placeholder.
    """
    avanserer_by_no = bygg_avanserer_by_match_no(resultat_lookup)
    for runde, neste in NESTE_RUNDE.items():
        for kamp in status.get(neste, {}).get("kamper", []):
            slot_h = kamp.get("slot_hjemme") or kamp.get("hjemme", "")
            slot_b = kamp.get("slot_borte") or kamp.get("borte", "")
            lag_h = slot_vinner(slot_h, avanserer_by_no)
            lag_b = slot_vinner(slot_b, avanserer_by_no)

            changed = False
            if lag_h:
                kamp["hjemme"] = lag_h
                changed = True
            if lag_b:
                kamp["borte"] = lag_b
                changed = True

            if changed:
                kamp["id"] = kamp_id(kamp.get("hjemme", slot_h), kamp.get("borte", slot_b), kamp.get("dato", ""))
                kamp["info"] = "Autofyll fra avansement"


def oppdater_status_med_fifa_tidspunkter(status):
    """
    Legger FIFA-avsparkstid på status-kamper basert på match_no.

    Dette gjør at R16/QF/SF/finale kan bli tippebare straks begge lag er klare
    via Wxx-avansement. Funksjonen endrer kun utcDate for eksisterende kamp-slotter;
    den endrer ikke kamp_id, match_no, fd_match_id, fifa_event_id, lag eller score.
    """
    if not FIFA_MATCH_NO_LOOKUP:
        print("  → FIFA-avspark per match_no ikke tilgjengelig — hopper over tidssynk")
        return

    oppdatert = 0
    for runde in RUNDE_REKKEFOLGE:
        for index, kamp in enumerate(status.get(runde, {}).get("kamper", [])):
            match_no = match_no_for(runde, index, kamp)
            fifa = FIFA_MATCH_NO_LOOKUP.get(match_no)
            fifa_utc = fifa.get("fifa_utcDate") if fifa else None
            if not fifa_utc:
                continue
            if kamp.get("utcDate") != fifa_utc:
                kamp["utcDate"] = fifa_utc
                oppdatert += 1

    print(f"  → Synket FIFA-avsparkstid på {oppdatert} status-kamper")

def parse_utc_datetime(value):
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def oppdater_tippebar_status(status):
    """Setter tippebar per kamp og holder runden synlig etter første åpning."""
    now = datetime.now(timezone.utc)

    for runde in RUNDE_REKKEFOLGE:
        round_data = status.setdefault(runde, {"aapen": False, "frist": None, "kamper": []})
        any_known_pair = False
        upcoming_deadlines = []
        open_count = 0

        for kamp in round_data.get("kamper", []):
            known_pair = er_kjent_lag(kamp.get("hjemme")) and er_kjent_lag(kamp.get("borte"))
            kickoff = parse_utc_datetime(kamp.get("utcDate"))
            any_known_pair = any_known_pair or known_pair

            if not known_pair:
                kamp["tippebar"] = False
                kamp["tippe_status"] = "venter_pa_lag"
            elif kickoff is None:
                kamp["tippebar"] = False
                kamp["tippe_status"] = "mangler_avsparkstid"
            elif now >= kickoff:
                kamp["tippebar"] = False
                kamp["tippe_status"] = "startet"
            else:
                kamp["tippebar"] = True
                kamp["tippe_status"] = "aapen"
                upcoming_deadlines.append(kickoff)
                open_count += 1

        # Når runden først har fått en ferdig definert kamp, skal fanen forbli synlig.
        round_data["aapen"] = bool(round_data.get("aapen") or any_known_pair)
        round_data["antall_tippebare"] = open_count
        round_data["neste_frist"] = (
            min(upcoming_deadlines).strftime("%Y-%m-%dT%H:%M:%SZ")
            if upcoming_deadlines else None
        )

        # Felles bonusfrist er første bekreftede avspark i runden. Alle kampene
        # tas med, også de som allerede er startet, slik at fristen ikke flyttes.
        alle_tider = [
            parse_utc_datetime(kamp.get("utcDate"))
            for kamp in round_data.get("kamper", [])
        ]
        alle_tider = [dt for dt in alle_tider if dt]
        if alle_tider:
            round_data["frist"] = min(alle_tider).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            # Behold sist bekreftede frist ved en midlertidig kildefeil.
            round_data.setdefault("frist", None)
        print(
            f"  → {runde}: synlig={round_data['aapen']}, "
            f"tippebare={open_count}/{len(round_data.get('kamper', []))}"
        )


def oppdater_status(resultat_lookup):
    """
    Oppdaterer status.json per kamp.

    Runder blir synlige så snart minst én kamp har begge lag klare. Selve
    innleveringen styres av kampfeltet tippebar, som lukkes ved UTC-avspark.
    """
    try:
        with open(STATUS_JSON, encoding="utf-8") as f:
            status = json.load(f)
    except Exception:
        print("  ADVARSEL: Kunne ikke lese status.json — hopper over oppdatering")
        return

    sikre_status_metadata(status)

    # Viktig: fang opp match_no fra status FØR vi resetter synlige lag tilbake
    # til Wxx-slotter. Når en R16-kamp allerede står som f.eks.
    # France_Morocco_2026_07_04 i status.json, men OpenFootball/FD ikke har
    # match_no i payloaden, er dette koblingen som gjør at W89/W90 kan fylles
    # videre til QF. Tidligere ble denne informasjonen slettet av reset først.
    legg_match_no_fra_status(resultat_lookup, status)

    reset_status_til_originale_slots(status)
    legg_match_no_fra_status(resultat_lookup, status)
    oppdater_status_med_api_kamper(status, resultat_lookup)
    sikre_status_metadata(status)
    legg_match_no_fra_status(resultat_lookup, status)
    # Kjør én gang til slik at API-oppføringer som nå fikk match_no kan berike metadata.
    oppdater_status_med_api_kamper(status, resultat_lookup)
    autofyll_neste_runder(status, resultat_lookup)

    # Etter autofyll kan neste runde ha fått konkrete lag og ny kamp_id.
    # Kjør mapping/API-beriking én gang til, slik at samme kjøring også kan
    # hente fd_match_id/fifa_event_id/utcDate for QF/SF/finale når kilden har det.
    sikre_status_metadata(status)
    legg_match_no_fra_status(resultat_lookup, status)
    oppdater_status_med_api_kamper(status, resultat_lookup)

    oppdater_status_med_fifa_tidspunkter(status)
    oppdater_tippebar_status(status)

    with open(STATUS_JSON, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
        f.write("\n")

# ── HOVEDFUNKSJON ─────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("VM 2026 Poengregning")
    print(f"Modus: {'TEST (mock-data)' if TEST_MODE else 'PRODUKSJON (live API)'}")
    print("=" * 60)

    # Hent resultater
    if TEST_MODE:
        print("\n[TEST] Bruker mock-resultater")
        resultat_lookup = {r["kamp_id"]: r for r in MOCK_RESULTATER}
    else:
        api_data = hent_api_data()
        fd_lookup = hent_football_data_org()
        fifa_lookup = hent_fifa_calendar()
        resultat_lookup = bygg_resultat_lookup(api_data, fd_lookup, fifa_lookup)

    spillerstatistikk = [] if TEST_MODE else hent_fifa_spillerstatistikk()

    # Les manuelle kamper/resultater som midlertidig fallback
    print("\nLeser manuelle kamp-/resultat-fallbacks...")
    manuelle_kamper = les_manuelle_kamper()
    if manuelle_kamper:
        resultat_lookup = flett_inn_manuelle_kamper(resultat_lookup, manuelle_kamper, fd_lookup if not TEST_MODE else {})
    else:
        print("  → Ingen manuelle kamper/resultater funnet")

    # Oppdater status før bonusfrister leses, slik at samme kjøring bruker de
    # nyeste bekreftede avsparkstidene.
    if not TEST_MODE:
        print("\nOppdaterer status.json...")
        oppdater_status(resultat_lookup)

    bonusstatus = bygg_bonusstatus(resultat_lookup, spillerstatistikk)
    bonusfrister = les_bonusfrister()

    faktisk_turneringsvinner = None
    final_kamper = [v for v in resultat_lookup.values() if v["runde"] == "final" and v["ferdig"]]
    if final_kamper:
        faktisk_turneringsvinner = finn_avanserer(final_kamper[0])

    # Skriv liste over kamper som burde vært ferdig, men mangler resultat
    print("\nSkriver mangler-resultater.json...")
    skriv_mangler_resultater(resultat_lookup)

    # Les tippinger
    print("\nLeser tippinger...")
    if TEST_MODE:
        # Mock-data inkluderer nå deltaker_id
        deltakere = {}
        for d in MOCK_TIPPINGER:
            navn = d["meta"]["navn"]
            did  = lag_deltaker_id(navn)
            deltakere[did] = {
                "navn":             navn,
                "deltaker_id":      did,
                "deltaker_type":    "ordinaer",
                "turneringsvinner": d.get("turneringsvinner", ""),
                "gruppespill":      d.get("gruppespill", []),
                "utslagsrunder":    [],
                "bonus":            {},
                "helhetsbonus":     {},
            }
        print(f"  → {len(deltakere)} test-deltakere")
    else:
        deltakere = les_alle_tippinger()

    if not deltakere:
        print("  ADVARSEL: Ingen tippinger funnet — skriver tom stilling")

    # Skriv deltakere.json (gjøres alltid, også i testmodus)
    print("\nSkriver deltakere.json...")
    skriv_deltakere_json(deltakere)

    # Bygg ASCII-fallback lookup for tippinger med spesialtegn (f.eks. Curaçao → Cura_ao)
    ascii_lookup = bygg_ascii_lookup(resultat_lookup)

    # Regn poeng
    print("\nRegner poeng...")
    match_no_lookup = bygg_match_no_lookup(resultat_lookup)
    stilling = []
    for did, deltaker in deltakere.items():
        resultat = regn_poeng_deltaker(
            deltaker, resultat_lookup, faktisk_turneringsvinner, ascii_lookup,
            bonusstatus, bonusfrister, match_no_lookup
        )
        stilling.append(resultat)
        print(f"  {deltaker['navn']} ({did}): {resultat['poeng_totalt']}p "
              f"(gruppe: {resultat['poeng_gruppespill']}p, "
              f"utslagsrunder: {resultat['poeng_utslagsrunder']}p, "
              f"rundebonus: {resultat.get('poeng_bonus', 0)}p, "
              f"helhetsbonus: {resultat.get('poeng_helhetsbonus', 0)}p, "
              f"turneringsvinner: {resultat['poeng_turneringsvinner']}p)")

    # Legg på gjennomsnittlig gruppespillscore for eksplisitt senpåmeldte.
    # Før gruppespillet er ferdig beholdes poeng_start=0 og status='venter'.
    stilling, startpoengstatus = apply_late_signup_points(stilling, deltakere, resultat_lookup)
    print(
        "  → Startpoengstatus: "
        f"{startpoengstatus.get('status')} "
        f"(startpoeng={startpoengstatus.get('startpoeng')}, "
        f"senpåmeldte={startpoengstatus.get('antall_senpameldte', 0)})"
    )

    # Skriv data.js
    print("\nSkriver data.js...")
    sist_oppdatert = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    skriv_data_js(
        stilling, sist_oppdatert, resultat_lookup, bonusstatus,
        spillerstatistikk, startpoengstatus
    )

    print("\n" + "=" * 60)
    print("Ferdig!")
    print("=" * 60)

    # Verifiser poeng i testmodus
    if TEST_MODE:
        print("\n── TESTVERIFISERING ──")
        print("\nForventet poeng:")
        print("  Kari Nordmann: Mexico 2-1 ✓ eksakt (6p) + Korea 1-1 ✓ eksakt (6p) + Norge 3-0 ✓ eksakt (6p) = 18p")
        print("  Ole Hansen:    Mexico 1-0 ✓ utfall (2p) + Korea 2-1 ✗ (0p) + Norge 3-0 ✓ eksakt (6p) = 8p")
        print("  Petter Ås:     Mexico 0-2 ✗ (0p) + Korea 1-1 ✓ eksakt (6p) + Norge 2-1 ✓ utfall (2p) = 8p")
        print("\nBeregnede poeng:")
        for d in sorted(stilling, key=lambda x: x["poeng_totalt"], reverse=True):
            print(f"  {d['navn']} (id: {d['deltaker_id']}): {d['poeng_totalt']}p")
        print("\nGenerert deltakere.json:")
        for d in deltakere.values():
            print(f"  {d['deltaker_id']} → {d['navn']}")

if __name__ == "__main__":
    main()
