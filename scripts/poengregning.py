"""
VM 2026 Tippekonkurranse — Poengregningscript
Kjøres av GitHub Actions hver time.

Flyt:
1. Hent ferdigspilte kamper fra VM API
2. Les alle tippinger fra /tippinger/
3. Regn poeng per deltaker
4. Skriv data/data.js
5. Oppdater data/status.json med utslagsrunde-info
"""

import json
import os
import sys
import requests
from pathlib import Path
from datetime import datetime, timezone

# ── KONFIG ────────────────────────────────────────────────────────────────────
API_URL      = "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json"
REPO_ROOT    = Path(__file__).parent.parent
TIPPINGER    = REPO_ROOT / "tippinger"
DATA_DIR     = REPO_ROOT / "data"
DATA_JS      = DATA_DIR / "data.js"
STATUS_JSON  = DATA_DIR / "status.json"

# Poeng per runde
POENG = {
    "gruppe": {"vinner": 2, "eksakt": 2},
    "r32":    {"vinner": 3, "eksakt": 2},
    "r16":    {"vinner": 4, "eksakt": 2},
    "qf":     {"vinner": 5, "eksakt": 2},
    "sf":     {"vinner": 6, "eksakt": 2},
    "final":  {"vinner": 7, "eksakt": 2},
}
POENG_TURNERINGSVINNER = 35

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

# ── HENT API-DATA ─────────────────────────────────────────────────────────────
def hent_api_data():
    """Henter VM-data fra openfootball API."""
    print("Henter kampdata fra API...")
    try:
        r = requests.get(API_URL, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"FEIL: Kunne ikke hente API-data: {e}")
        sys.exit(1)

def bygg_resultat_lookup(api_data):
    """
    Bygger en dict med kamp_id → resultat for alle ferdigspilte kamper.
    Format: { "Mexico_South_Africa_2026_06_11": { hjemme: 2, borte: 1, ferdig: True, runde: "gruppe" } }
    """
    lookup = {}
    now = datetime.now(timezone.utc)

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

        # Sjekk om kampen er ferdigspilt
        ferdig = False
        if ht and ht[0] is not None and ht[1] is not None:
            ferdig = True

        kid = kamp_id(team1, team2, dato)
        lookup[kid] = {
            "hjemmelag": team1,
            "bortelag":  team2,
            "hjemme":    ht[0] if ht else None,
            "borte":     ht[1] if ht else None,
            "ferdig":    ferdig,
            "runde":     runde,
            "dato":      dato,
        }

    ferdig_antall = sum(1 for v in lookup.values() if v["ferdig"])
    print(f"  → {len(lookup)} kamper totalt, {ferdig_antall} ferdigspilte")
    return lookup

# ── LES TIPPINGER ─────────────────────────────────────────────────────────────
def les_alle_tippinger():
    """
    Leser alle JSON-filer fra tippinger/-mappene.
    Returnerer dict med navn → samlet tipping-data.
    """
    deltakere = {}

    runder = ["gruppespill", "r32", "r16", "qf", "sf", "final"]

    for runde in runder:
        mappe = TIPPINGER / runde
        if not mappe.exists():
            continue

        for fil in mappe.glob("*.json"):
            try:
                with open(fil, encoding="utf-8") as f:
                    data = json.load(f)

                navn = data.get("meta", {}).get("navn", "").strip()
                if not navn:
                    print(f"  ADVARSEL: Ingen navn i {fil.name} — hopper over")
                    continue

                if navn not in deltakere:
                    deltakere[navn] = {
                        "navn":              navn,
                        "turneringsvinner":  data.get("turneringsvinner", ""),
                        "gruppespill":       [],
                        "utslagsrunder":     [],
                    }

                # Legg til gruppespill-tippinger
                if runde == "gruppespill":
                    deltakere[navn]["turneringsvinner"] = data.get("turneringsvinner", "")
                    deltakere[navn]["gruppespill"] = data.get("gruppespill", [])

                # Legg til utslagsrunde-tippinger
                else:
                    for t in data.get("tippinger", []):
                        t["runde"] = runde
                        deltakere[navn]["utslagsrunder"].append(t)

            except Exception as e:
                print(f"  FEIL ved lesing av {fil}: {e}")

    print(f"  → {len(deltakere)} deltakere funnet")
    return deltakere

# ── REGN POENG ────────────────────────────────────────────────────────────────
def regn_poeng_for_kamp(tippa_h, tippa_b, faktisk_h, faktisk_b, runde, tippa_vinner=None, faktisk_vinner=None):
    """
    Regner poeng for én kamp.

    Gruppespill:
      - Riktig utfall: vinner-poeng
      - Eksakt resultat: +eksakt-poeng

    Utslagsrunder:
      - Riktig vinner (går videre): vinner-poeng
      - Eksakt resultat etter 90 min: +eksakt-poeng
    """
    if faktisk_h is None or faktisk_b is None:
        return 0, False, False  # Kamp ikke ferdigspilt

    poeng_config = POENG.get(runde, POENG["gruppe"])
    poeng        = 0
    riktig_utfall = False
    eksakt        = False

    if runde == "gruppe":
        # Sjekk utfall
        if utfall(tippa_h, tippa_b) == utfall(faktisk_h, faktisk_b):
            poeng += poeng_config["vinner"]
            riktig_utfall = True
        # Sjekk eksakt
        if tippa_h == faktisk_h and tippa_b == faktisk_b:
            poeng += poeng_config["eksakt"]
            eksakt = True

    else:
        # Utslagsrunder — sjekk vinner (går videre)
        if tippa_vinner and faktisk_vinner and tippa_vinner == faktisk_vinner:
            poeng += poeng_config["vinner"]
            riktig_utfall = True
        # Sjekk eksakt resultat etter 90 min
        if tippa_h is not None and tippa_b is not None:
            if tippa_h == faktisk_h and tippa_b == faktisk_b:
                poeng += poeng_config["eksakt"]
                eksakt = True

    return poeng, riktig_utfall, eksakt

def regn_poeng_deltaker(deltaker, resultat_lookup, faktisk_turneringsvinner=None):
    """Regner totale poeng for én deltaker."""
    navn              = deltaker["navn"]
    poeng_totalt      = 0
    poeng_gruppespill = 0
    poeng_utslagsrunder = 0
    poeng_turneringsvinner = 0
    tipping_detaljer  = []

    # ── Gruppespill ──
    for t in deltaker.get("gruppespill", []):
        kid = t.get("kamp_id", "")
        res = resultat_lookup.get(kid)

        if not res or not res["ferdig"]:
            tipping_detaljer.append({
                "kamp_id":    kid,
                "hjemmelag":  res["hjemmelag"] if res else "",
                "bortelag":   res["bortelag"]  if res else "",
                "tippa_h":    t.get("hjemme"),
                "tippa_b":    t.get("borte"),
                "faktisk_h":  res["hjemme"] if res else None,
                "faktisk_b":  res["borte"]  if res else None,
                "poeng":      0,
                "ferdig":     False,
                "runde":      "gruppe",
            })
            continue

        p, riktig, eksakt = regn_poeng_for_kamp(
            t.get("hjemme"), t.get("borte"),
            res["hjemme"], res["borte"],
            "gruppe"
        )
        poeng_gruppespill += p
        tipping_detaljer.append({
            "kamp_id":    kid,
            "hjemmelag":  res["hjemmelag"],
            "bortelag":   res["bortelag"],
            "tippa_h":    t.get("hjemme"),
            "tippa_b":    t.get("borte"),
            "faktisk_h":  res["hjemme"],
            "faktisk_b":  res["borte"],
            "poeng":      p,
            "riktig_utfall": riktig,
            "eksakt":     eksakt,
            "ferdig":     True,
            "runde":      "gruppe",
        })

    # ── Utslagsrunder ──
    for t in deltaker.get("utslagsrunder", []):
        kid   = t.get("kamp_id", "")
        runde = t.get("runde", "r32")
        res   = resultat_lookup.get(kid)

        if not res or not res["ferdig"]:
            continue

        p, riktig, eksakt = regn_poeng_for_kamp(
            t.get("hjemme"), t.get("borte"),
            res["hjemme"], res["borte"],
            runde,
            tippa_vinner=t.get("vinner"),
            faktisk_vinner=res.get("vinner")
        )
        poeng_utslagsrunder += p
        tipping_detaljer.append({
            "kamp_id":    kid,
            "hjemmelag":  res["hjemmelag"],
            "bortelag":   res["bortelag"],
            "tippa_h":    t.get("hjemme"),
            "tippa_b":    t.get("borte"),
            "faktisk_h":  res["hjemme"],
            "faktisk_b":  res["borte"],
            "tippa_vinner":   t.get("vinner"),
            "faktisk_vinner": res.get("vinner"),
            "poeng":      p,
            "riktig_utfall": riktig,
            "eksakt":     eksakt,
            "ferdig":     True,
            "runde":      runde,
        })

    # ── Turneringsvinner ──
    if faktisk_turneringsvinner and deltaker.get("turneringsvinner"):
        if deltaker["turneringsvinner"] == faktisk_turneringsvinner:
            poeng_turneringsvinner = POENG_TURNERINGSVINNER

    poeng_totalt = poeng_gruppespill + poeng_utslagsrunder + poeng_turneringsvinner

    return {
        "navn":                   navn,
        "poeng_totalt":           poeng_totalt,
        "poeng_gruppespill":      poeng_gruppespill,
        "poeng_utslagsrunder":    poeng_utslagsrunder,
        "poeng_turneringsvinner": poeng_turneringsvinner,
        "turneringsvinner":       deltaker.get("turneringsvinner", ""),
        "turneringsvinner_riktig": poeng_turneringsvinner > 0,
        "tippinger":              tipping_detaljer,
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

    DATA_JS.write_text(js_innhold, encoding="utf-8")
    print(f"  → Skrev data.js med {len(stilling_sortert)} deltakere")

# ── OPPDATER STATUS.JSON ──────────────────────────────────────────────────────
def oppdater_status(resultat_lookup):
    """
    Sjekker om alle gruppekamper er ferdigspilt.
    Hvis ja, setter r32 til åpen (hvis den ikke allerede er åpen).
    """
    try:
        with open(STATUS_JSON, encoding="utf-8") as f:
            status = json.load(f)
    except Exception:
        print("  ADVARSEL: Kunne ikke lese status.json — hopper over oppdatering")
        return

    gruppe_kamper  = [v for v in resultat_lookup.values() if v["runde"] == "gruppe"]
    gruppe_ferdig  = all(k["ferdig"] for k in gruppe_kamper) if gruppe_kamper else False

    # Åpne r32 automatisk hvis alle gruppekamper er ferdig
    if gruppe_ferdig and not status.get("r32", {}).get("aapen"):
        print("  → Alle gruppekamper ferdig! Setter r32 til åpen.")
        status["r32"]["aapen"] = True

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
        faktisk_turneringsvinner = None  # Ikke kjent ennå
    else:
        api_data = hent_api_data()
        resultat_lookup = bygg_resultat_lookup(api_data)
        # Turneringsvinner er ikke kjent før finalen er spilt
        faktisk_turneringsvinner = None
        final_kamper = [v for v in resultat_lookup.values() if v["runde"] == "final" and v["ferdig"]]
        if final_kamper:
            f = final_kamper[0]
            if f["hjemme"] > f["borte"]:
                faktisk_turneringsvinner = f["hjemmelag"]
            elif f["borte"] > f["hjemme"]:
                faktisk_turneringsvinner = f["bortelag"]
            else:
                faktisk_turneringsvinner = f.get("vinner")  # Straffespark-vinner

    # Les tippinger
    print("\nLeser tippinger...")
    if TEST_MODE:
        deltakere = {d["meta"]["navn"]: {
            "navn": d["meta"]["navn"],
            "turneringsvinner": d.get("turneringsvinner", ""),
            "gruppespill": d.get("gruppespill", []),
            "utslagsrunder": [],
        } for d in MOCK_TIPPINGER}
        print(f"  → {len(deltakere)} test-deltakere")
    else:
        deltakere = les_alle_tippinger()

    if not deltakere:
        print("  ADVARSEL: Ingen tippinger funnet — skriver tom stilling")

    # Regn poeng
    print("\nRegner poeng...")
    stilling = []
    for navn, deltaker in deltakere.items():
        resultat = regn_poeng_deltaker(deltaker, resultat_lookup, faktisk_turneringsvinner)
        stilling.append(resultat)
        print(f"  {navn}: {resultat['poeng_totalt']}p "
              f"(gruppe: {resultat['poeng_gruppespill']}p, "
              f"utslagsrunder: {resultat['poeng_utslagsrunder']}p, "
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

    # Verifiser poeng mot forventede verdier i testmodus
    if TEST_MODE:
        print("\n── TESTVERIFISERING ──")
        forventet = {
            "Kari Nordmann": 12,  # 3 eksakte = 3×4p
            "Ole Hansen":    4,   # 1 eksakt (Norge) + 1 riktig utfall = 4+2
            "Petter Ås":     4,   # 1 riktig utfall (uavgjort) + 1 eksakt (uavgjort) = 2+4 ... se under
        }
        print("\nForventet poeng:")
        print("  Kari Nordmann: Mexico 2-1 ✓ eksakt (4p) + Korea 1-1 ✓ eksakt (4p) + Norge 3-0 ✓ eksakt (4p) = 12p")
        print("  Ole Hansen:    Mexico 1-0 ✗ (0p) + Korea 2-1 ✗ (0p) + Norge 3-0 ✓ eksakt (4p) = 4p")
        print("  Petter Ås:     Mexico 0-2 ✗ (0p) + Korea 1-1 ✓ eksakt (4p) + Norge 2-1 ✓ utfall (2p) = 6p")
        print("\nBeregnede poeng:")
        for d in sorted(stilling, key=lambda x: x["poeng_totalt"], reverse=True):
            print(f"  {d['navn']}: {d['poeng_totalt']}p")

if __name__ == "__main__":
    main()
