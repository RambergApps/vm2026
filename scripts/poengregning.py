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
  1. OpenFootball (hvis score.ft finnes)
  2. football-data.org (hvis OF mangler score og kampen er FINISHED)
  3. Manuelle fallback-kamper (data/manuelle-kamper.json)
"""

import json
import os
import re
import sys
import requests
from pathlib import Path
from datetime import datetime, timezone

# ── KONFIG ────────────────────────────────────────────────────────────────────
API_URL               = "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json"
FOOTBALL_DATA_API_URL = "https://api.football-data.org/v4/competitions/WC/matches"
FOOTBALL_DATA_TOKEN   = os.environ.get("FOOTBALL_DATA_TOKEN", "")

# Lagnavn-mapping: football-data.org → OpenFootball
# OpenFootball-navn er master siden tipping-appen og kamp-ID-er er basert på disse.
FD_NAVN_TIL_OF = {
    "Bosnia-Herzegovina": "Bosnia & Herzegovina",
    "Cape Verde Islands":  "Cape Verde",
    "Congo DR":            "DR Congo",
    "Czechia":             "Czech Republic",
}

REPO_ROOT      = Path(__file__).parent.parent
TIPPINGER      = REPO_ROOT / "tippinger"
DATA_DIR       = REPO_ROOT / "data"
DATA_JS        = DATA_DIR / "data.js"
STATUS_JSON    = DATA_DIR / "status.json"
DELTAKERE_JSON = DATA_DIR / "deltakere.json"          # ← NY
MANUELLE_KAMPER_JSON = DATA_DIR / "manuelle-kamper.json"
MANGLER_RESULTATER_JSON = DATA_DIR / "mangler-resultater.json"

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
BONUS_SPORSMAL = {
    "r32":   {"id": "antall_uavgjort",      "tekst": "Antall kamper som ender uavgjort etter 90 min"},
    "r16":   {"id": "antall_nullen",        "tekst": "Antall lag som holder nullen etter 90 min"},
    "qf":    {"id": "antall_ettmaalsseier", "tekst": "Antall kamper avgjort med ett mål etter 90 min"},
    "sf":    {"id": "totale_maal",          "tekst": "Totalt antall mål i semifinalene etter 90 min"},
    "final": {"id": "begge_lag_scorer",     "tekst": "Scorer begge lag i finalen etter 90 min"},
}

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


def hent_football_data_org():
    """
    Henter kampresultater fra football-data.org.

    Returnerer en dict med kamp_id → {hjemme, borte, ferdig} for alle
    kamper med status FINISHED. Lagnavn normaliseres til OpenFootball-format
    via FD_NAVN_TIL_OF-mappingen slik at kamp-ID-er matcher.

    Returnerer tom dict hvis token mangler eller kall feiler.
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
    for kamp in data.get("matches", []):
        if kamp.get("status") != "FINISHED":
            continue

        # Normaliser lagnavn til OpenFootball-format
        team1 = FD_NAVN_TIL_OF.get(kamp["homeTeam"]["name"], kamp["homeTeam"]["name"])
        team2 = FD_NAVN_TIL_OF.get(kamp["awayTeam"]["name"], kamp["awayTeam"]["name"])

        # Dato fra utcDate (format: 2026-06-11T19:00:00Z -> 2026-06-11)
        dato = kamp.get("utcDate", "")[:10]

        score = kamp.get("score", {})
        ft    = score.get("fullTime", {})
        hjemme = ft.get("home")
        borte  = ft.get("away")

        if hjemme is None or borte is None:
            continue

        # Hent winner og duration for utslagskamper
        winner   = score.get("winner")    # HOME_TEAM / AWAY_TEAM / DRAW
        duration = score.get("duration")  # REGULAR / EXTRA_TIME / PENALTY_SHOOTOUT

        kid = kamp_id(team1, team2, dato)
        fd_lookup[kid] = {
            "hjemme":   hjemme,
            "borte":    borte,
            "ferdig":   True,
            "kilde":    "football_data_org",
            "winner":   winner,
            "duration": duration,
        }

    print(f"  -> {len(fd_lookup)} ferdigspilte kamper fra football-data.org")
    return fd_lookup


def bygg_resultat_lookup(api_data, fd_lookup):
    """
    Bygger en dict med kamp_id -> resultat for alle kamper.

    Prioritet for score:
      1. OpenFootball (hvis score.ft finnes)
      2. football-data.org (hvis OF mangler score og kampen er FINISHED hos fd.org)

    Format: { "Mexico_South_Africa_2026_06_11": { hjemme: 2, borte: 1, ferdig: True, runde: "gruppe", kilde: "..." } }
    """
    lookup = {}

    for kamp in api_data.get("matches", []):
        dato  = kamp.get("date", "")
        team1 = kamp.get("team1", "")
        team2 = kamp.get("team2", "")
        score = kamp.get("score", {})
        ht    = score.get("ft", [None, None])  # full time score

        # Bestem runde
        if kamp.get("group"):
            runde = "gruppe"
        elif kamp.get("round"):
            r = kamp["round"].lower()
            if "round of 32" in r or "r32" in r:
                runde = "r32"
            elif "round of 16" in r or "r16" in r:
                runde = "r16"
            elif "quarter" in r:
                runde = "qf"
            elif "semi" in r:
                runde = "sf"
            elif "final" in r:
                runde = "final"
            else:
                runde = "ukjent"
        else:
            runde = "ukjent"

        # Sjekk om OpenFootball har ferdig score
        of_ferdig = bool(ht and ht[0] is not None and ht[1] is not None)

        kid = kamp_id(team1, team2, dato)

        if of_ferdig:
            # OpenFootball har score -- bruk den
            lookup[kid] = {
                "hjemmelag": team1,
                "bortelag":  team2,
                "hjemme":    ht[0],
                "borte":     ht[1],
                "ferdig":    True,
                "runde":     runde,
                "dato":      dato,
                "kilde":     "openfootball",
            }
        else:
            # OpenFootball mangler score -- bygg oppforing og sjekk fd.org
            base = {
                "hjemmelag": team1,
                "bortelag":  team2,
                "hjemme":    None,
                "borte":     None,
                "ferdig":    False,
                "runde":     runde,
                "dato":      dato,
                "kilde":     "openfootball",
            }
            fd = fd_lookup.get(kid)
            if fd:
                base["hjemme"]  = fd["hjemme"]
                base["borte"]   = fd["borte"]
                base["ferdig"]  = True
                base["kilde"]   = "football_data_org"
                # Sett avanserer automatisk fra fd.org winner-felt (gjelder utslagskamper)
                if fd.get("winner") == "HOME_TEAM":
                    base["avanserer"] = team1
                elif fd.get("winner") == "AWAY_TEAM":
                    base["avanserer"] = team2
                duration = fd.get("duration", "REGULAR")
                print(f"    fd.org fallback: {team1} {fd['hjemme']}-{fd['borte']} {team2} ({dato}) [{duration}]")
            lookup[kid] = base

    of_ferdig_antall = sum(1 for v in lookup.values() if v["ferdig"] and v.get("kilde") == "openfootball")
    fd_ferdig_antall = sum(1 for v in lookup.values() if v["ferdig"] and v.get("kilde") == "football_data_org")
    print(f"  -> {len(lookup)} kamper totalt | OpenFootball: {of_ferdig_antall} ferdigspilte | football-data.org fallback: {fd_ferdig_antall}")
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
    """Normaliserer én manuell kamp til samme format som resultat_lookup."""
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
    return kid, item


def flett_inn_manuelle_kamper(resultat_lookup, manuelle_kamper):
    """
    Fletter manuelle kamper/resultater inn i OpenFootball-lookup.

    Regel:
      1. OpenFootball-resultat vinner hvis OpenFootball har ferdig score.
      2. Manuelt resultat brukes hvis OpenFootball mangler score.
      3. Manuell kamp brukes hvis OpenFootball mangler hele kampen.
    """
    brukt = 0
    lagt_til = 0
    ignorert = 0

    for raw in manuelle_kamper:
        kid, manuell = normaliser_manuell_kamp(raw)
        if not kid:
            print(f"  ADVARSEL: Hopper over manuell kamp uten kamp_id/lag/dato: {raw}")
            continue

        eksisterende = resultat_lookup.get(kid)
        if not eksisterende:
            resultat_lookup[kid] = manuell
            lagt_til += 1
            continue

        # OpenFootball er master når den har ferdig resultat.
        # Men manuell "avanserer" kan fortsatt brukes til å bygge neste runde
        # hvis kampen står uavgjort etter 90 minutter.
        if eksisterende.get("ferdig"):
            if manuell.get("avanserer") and not eksisterende.get("avanserer"):
                eksisterende["avanserer"] = manuell.get("avanserer")
            if manuell.get("match_no") and not eksisterende.get("match_no"):
                eksisterende["match_no"] = manuell.get("match_no")
            ignorert += 1
            continue

        # Hvis OpenFootball har kamp, men ikke resultat, kan manuell fallback brukes.
        if manuell.get("ferdig"):
            resultat_lookup[kid] = {
                **eksisterende,
                "hjemme": manuell["hjemme"],
                "borte": manuell["borte"],
                "ferdig": True,
                "kilde": "manuell_fallback",
            }
            for felt in ("avanserer", "match_no", "slot_hjemme", "slot_borte"):
                if manuell.get(felt) is not None:
                    resultat_lookup[kid][felt] = manuell.get(felt)
            brukt += 1
        else:
            # Behold OpenFootball-kampen, men fyll eventuelt inn manglende metadata fra manuell kamp.
            resultat_lookup[kid] = {
                **manuell,
                **eksisterende,
                "kilde": eksisterende.get("kilde", "openfootball"),
            }

    print(f"  → Manuell fallback: {lagt_til} kamper lagt til, {brukt} resultater brukt, {ignorert} ignorert fordi OpenFootball har resultat")
    return resultat_lookup


def skriv_mangler_resultater(resultat_lookup):
    """
    Skriver data/mangler-resultater.json med kamper som er datert før i dag,
    men fortsatt mangler resultat etter 90 minutter.
    """
    today = datetime.now(timezone.utc).date()
    mangler = []

    for kid, kamp in resultat_lookup.items():
        dato_txt = kamp.get("dato", "")
        try:
            kamp_dato = datetime.fromisoformat(dato_txt[:10]).date()
        except Exception:
            continue

        if kamp_dato < today and not kamp.get("ferdig"):
            mangler.append({
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
    print(f"  → Skrev mangler-resultater.json med {len(mangler)} kamper")

# ── LES TIPPINGER ─────────────────────────────────────────────────────────────
def les_alle_tippinger():
    """
    Leser alle JSON-filer fra tippinger/-mappene.
    Returnerer dict med deltaker_id → samlet tipping-data.

    Kobling på tvers av runder:
    - Gruppespill: deltaker_id genereres fra navn (slug)
    - Utslagsrunder: bruker meta.deltaker_id hvis tilgjengelig,
      faller tilbake på navn-slug for bakoverkompatibilitet.
    """
    deltakere = {}  # { deltaker_id: { navn, deltaker_id, tippinger, ... } }

    runder = ["gruppespill", "r32", "r16", "qf", "sf", "final"]

    for runde in runder:
        mappe = TIPPINGER / runde
        if not mappe.exists():
            continue

        for fil in sorted(mappe.glob("*.json")):  # sorter for deterministisk rekkefølge
            try:
                with open(fil, encoding="utf-8") as f:
                    data = json.load(f)

                navn = data.get("meta", {}).get("navn", "").strip()
                if not navn:
                    print(f"  ADVARSEL: Ingen navn i {fil.name} — hopper over")
                    continue

                # ── Finn riktig deltaker_id ──────────────────────────────────
                if runde == "gruppespill":
                    # Gruppespill etablerer deltaker_id fra navn
                    did = lag_deltaker_id(navn)
                else:
                    # Utslagsrunder: bruk meta.deltaker_id hvis det finnes og er kjent
                    did_fra_meta = data.get("meta", {}).get("deltaker_id", "").strip()
                    if did_fra_meta and did_fra_meta in deltakere:
                        did = did_fra_meta
                    else:
                        # Bakoverkompatibilitet: slug av navn
                        did = lag_deltaker_id(navn)
                        if did_fra_meta and did_fra_meta not in deltakere:
                            # Logg avvik for enklere feilsøking
                            print(f"  ADVARSEL: Ukjent deltaker_id '{did_fra_meta}' i {fil.name} — bruker navn-slug '{did}'")

                # ── Opprett deltaker hvis ny ─────────────────────────────────
                if did not in deltakere:
                    deltakere[did] = {
                        "navn":             navn,
                        "deltaker_id":      did,
                        "turneringsvinner": "",
                        "gruppespill":      [],
                        "utslagsrunder":    [],
                        "bonus":            {},
                    }

                # ── Legg til tippinger ───────────────────────────────────────
                if runde == "gruppespill":
                    # Nyeste fil vinner (siste innlevering gjelder)
                    deltakere[did]["turneringsvinner"] = data.get("turneringsvinner", "")
                    deltakere[did]["gruppespill"]      = data.get("gruppespill", [])
                else:
                    for t in data.get("tippinger", []):
                        t["runde"] = runde
                        deltakere[did]["utslagsrunder"].append(t)
                    if data.get("bonus"):
                        deltakere[did].setdefault("bonus", {})[runde] = data.get("bonus")

            except Exception as e:
                print(f"  FEIL ved lesing av {fil}: {e}")

    print(f"  → {len(deltakere)} deltakere funnet")
    return deltakere

# ── SKRIV DELTAKERE.JSON ──────────────────────────────────────────────────────
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
        [{"id": d["deltaker_id"], "navn": d["navn"]} for d in deltakere.values()],
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


def regn_poeng_deltaker(deltaker, resultat_lookup, faktisk_turneringsvinner=None, ascii_lookup=None):
    """Regner totale poeng for én deltaker."""
    navn                 = deltaker["navn"]
    poeng_totalt         = 0
    poeng_gruppespill    = 0
    poeng_utslagsrunder  = 0
    poeng_bonus          = 0
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
            "runde":         "gruppe",
        })

    # ── Utslagsrunder ──
    for t in deltaker.get("utslagsrunder", []):
        kid   = t.get("kamp_id", "")
        runde = t.get("runde", "r32")
        res   = resultat_lookup.get(kid)
        if res is None and ascii_lookup:
            res = ascii_lookup.get(kid)

        if not res or not res["ferdig"]:
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

    # ── Turneringsvinner ──
    if faktisk_turneringsvinner and deltaker.get("turneringsvinner"):
        if deltaker["turneringsvinner"] == faktisk_turneringsvinner:
            poeng_turneringsvinner = POENG_TURNERINGSVINNER

    poeng_totalt = poeng_gruppespill + poeng_utslagsrunder + poeng_bonus + poeng_turneringsvinner

    return {
        "navn":                    navn,
        "deltaker_id":             deltaker.get("deltaker_id", ""),   # ← NY
        "poeng_totalt":            poeng_totalt,
        "poeng_gruppespill":       poeng_gruppespill,
        "poeng_utslagsrunder":     poeng_utslagsrunder,
        "poeng_bonus":            poeng_bonus,
        "poeng_turneringsvinner":  poeng_turneringsvinner,
        "turneringsvinner":        deltaker.get("turneringsvinner", ""),
        "turneringsvinner_riktig": poeng_turneringsvinner > 0,
        "tippinger":               tipping_detaljer,
    }

# ── SKRIV DATA.JS ─────────────────────────────────────────────────────────────
def skriv_data_js(stilling, sist_oppdatert):
    """Skriver ferdig data.js som leaderboard-siden leser."""

    # Sorter etter poeng totalt
    stilling_sortert = sorted(stilling, key=lambda x: x["poeng_totalt"], reverse=True)

    # Legg til plassering
    for i, d in enumerate(stilling_sortert):
        d["plass"] = i + 1

    js_innhold = f"""// Denne filen genereres automatisk av GitHub Actions
// Ikke rediger manuelt — endringer overskrives ved neste kjøring
// Sist oppdatert: {sist_oppdatert}

const VM_DATA = {json.dumps({
    "sist_oppdatert": sist_oppdatert,
    "stilling": stilling_sortert,
}, ensure_ascii=False, indent=2)};
"""

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_JS.write_text(js_innhold, encoding="utf-8")
    print(f"  → Skrev data.js med {len(stilling_sortert)} deltakere")

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
    OpenFootball er master når den har komplett sett med kjente kamper for en runde.
    Hvis ikke bevares eksisterende status, og manuelle fallback-kamper kan fylle hull.
    """
    for runde in RUNDE_REKKEFOLGE:
        forventet = FORVENTET_ANTALL[runde]
        api_kjente = [
            (kid, v) for kid, v in resultat_lookup.items()
            if v.get("runde") == runde
            and v.get("kilde") in ("openfootball", "football_data_org")
            and er_kjent_lag(v.get("hjemmelag"))
            and er_kjent_lag(v.get("bortelag"))
        ]
        if len(api_kjente) >= forventet:
            status[runde]["kamper"] = [
                {
                    "id": kid,
                    "hjemme": v.get("hjemmelag", ""),
                    "borte": v.get("bortelag", ""),
                    "dato": v.get("dato", ""),
                    "info": "OpenFootball",
                    "match_no": match_no_for(runde, i),
                    "slot_hjemme": v.get("hjemmelag", ""),
                    "slot_borte": v.get("bortelag", ""),
                }
                for i, (kid, v) in enumerate(sorted(api_kjente, key=lambda x: x[1].get("dato", "")))
            ]
            print(f"  → {runde}: OpenFootball har komplett kampoppsett ({len(api_kjente)})")
            continue

        # Manuelle utslagskamper kan fylle kjent lag på korrekt match_no uten å miste øvrige placeholders.
        manual_by_no = {
            int(v["match_no"]): (kid, v)
            for kid, v in resultat_lookup.items()
            if v.get("runde") == runde
            and v.get("kilde") == "manuell_fallback"
            and v.get("match_no") is not None
            and er_kjent_lag(v.get("hjemmelag"))
            and er_kjent_lag(v.get("bortelag"))
        }
        for kamp in status.get(runde, {}).get("kamper", []):
            mn = kamp.get("match_no")
            if mn in manual_by_no:
                kid, v = manual_by_no[mn]
                kamp.update({
                    "id": kid,
                    "hjemme": v.get("hjemmelag", ""),
                    "borte": v.get("bortelag", ""),
                    "dato": v.get("dato", kamp.get("dato", "")),
                    "info": "Manuell fallback",
                    "avanserer": v.get("avanserer", kamp.get("avanserer", "")),
                })

def reset_status_til_originale_slots(status):
    """
    Bygger statusvisningen opp igjen fra opprinnelige bracket-slots før API/manuell fallback legges på.

    Dette hindrer at gamle manuelle testdata blir liggende igjen i status.json etter at
    fallback-kampen er slettet fra data/manuelle-kamper.json.
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
            kamp.pop("avanserer", None)

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

def oppdater_status(resultat_lookup):
    """
    Oppdaterer status.json.

    OpenFootball er master for kampoppsett når datakilden har komplett runde.
    Manuelle fallback-kamper/resultater brukes midlertidig når OpenFootball mangler data.
    Feltet `avanserer` brukes bare til å fylle neste utslagsrunde, ikke til poeng.
    """
    try:
        with open(STATUS_JSON, encoding="utf-8") as f:
            status = json.load(f)
    except Exception:
        print("  ADVARSEL: Kunne ikke lese status.json — hopper over oppdatering")
        return

    sikre_status_metadata(status)
    reset_status_til_originale_slots(status)
    legg_match_no_fra_status(resultat_lookup, status)
    oppdater_status_med_api_kamper(status, resultat_lookup)
    sikre_status_metadata(status)
    legg_match_no_fra_status(resultat_lookup, status)
    autofyll_neste_runder(status, resultat_lookup)

    for runde in RUNDE_REKKEFOLGE:
        if status[runde].get("aapen"):
            continue
        forrige = FORRIGE_RUNDE[runde]
        forrige_ferdig = runde_ferdig_i_status(status, resultat_lookup, forrige)
        alle_kjente = alle_lag_kjente_i_status(status, runde)
        if forrige_ferdig and alle_kjente:
            status[runde]["aapen"] = True
            print(f"  → ÅPNER {runde.upper()}! Forrige runde ferdig og alle lag kjente.")
        else:
            print(f"  → {runde}: venter (forrige ferdig={forrige_ferdig}, alle lag kjente={alle_kjente})")

    with open(STATUS_JSON, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

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
        resultat_lookup = bygg_resultat_lookup(api_data, fd_lookup)

    # Les manuelle kamper/resultater som midlertidig fallback
    print("\nLeser manuelle kamp-/resultat-fallbacks...")
    manuelle_kamper = les_manuelle_kamper()
    if manuelle_kamper:
        resultat_lookup = flett_inn_manuelle_kamper(resultat_lookup, manuelle_kamper)
    else:
        print("  → Ingen manuelle kamper/resultater funnet")

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
                "turneringsvinner": d.get("turneringsvinner", ""),
                "gruppespill":      d.get("gruppespill", []),
                "utslagsrunder":    [],
                "bonus":            {},
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
    stilling = []
    for did, deltaker in deltakere.items():
        resultat = regn_poeng_deltaker(deltaker, resultat_lookup, faktisk_turneringsvinner, ascii_lookup)
        stilling.append(resultat)
        print(f"  {deltaker['navn']} ({did}): {resultat['poeng_totalt']}p "
              f"(gruppe: {resultat['poeng_gruppespill']}p, "
              f"utslagsrunder: {resultat['poeng_utslagsrunder']}p, "
              f"bonus: {resultat.get('poeng_bonus', 0)}p, "
              f"turneringsvinner: {resultat['poeng_turneringsvinner']}p)")

    # Skriv data.js
    print("\nSkriver data.js...")
    sist_oppdatert = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    skriv_data_js(stilling, sist_oppdatert)

    # Oppdater status.json (kun produksjon)
    if not TEST_MODE:
        print("\nOppdaterer status.json...")
        oppdater_status(resultat_lookup)

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
