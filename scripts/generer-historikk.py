"""
VM 2026 — Kampreferat-generator
Kjøres av GitHub Actions etter poengregning.py.

Flyt:
1. Finn gårsdagens ferdigspilte kamper (norsk tid) fra data/data.js
2. Sjekk om kamppost.json allerede er generert for i går
3. Søk etter kampreferat via Serper.dev (Google snippets)
4. Filtrer snippets — behold bare faktasetninger med scorere/minutter
5. Slå opp historikk og fakta fra data/kamp-referanser.json
6. Kombiner til ferdig recap-tekst per kamp
7. Skriv data/kamppost.json
"""

import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── KONFIG ────────────────────────────────────────────────────────────────────
REPO_ROOT       = Path(__file__).parent.parent
DATA_DIR        = REPO_ROOT / "data"
DATA_JS         = DATA_DIR / "data.js"
KAMPPOST_JSON   = DATA_DIR / "kamppost.json"
REFERANSER_JSON = DATA_DIR / "kamp-referanser.json"

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
NORSK_TZ       = timezone(timedelta(hours=2))  # CEST sommertid

NORSKE_DAGER    = ["Mandag","Tirsdag","Onsdag","Torsdag","Fredag","Lørdag","Søndag"]
NORSKE_MAANEDER = ["januar","februar","mars","april","mai","juni",
                   "juli","august","september","oktober","november","desember"]

# Ord som indikerer promo/preview/reklame — disse snippetene kastes
PROMO_ORD = [
    "highlights", "watch", "stream", "announced by", "click here",
    "subscribe", "follow", "tune in", "broadcast", "tv channel",
    "how to watch", "where to watch", "kick-off", "kickoff",
    "preview", "prediction", "odds", "betting", "under pressure",
    "crucial", "face ", "will face", "set to", "ahead of",
    "check out", "find out", "read more", "full coverage",
    "live updates", "live blog", "as it happened",
]

# Ord som indikerer at snippeten inneholder faktisk kampinfo
FAKTA_ORD = [
    "scored", "goal", "minute", "penalty", "header", "assist",
    "red card", "yellow card", "own goal", "equaliser", "equalizer",
    "winner", "substitute", "substitut", "brace", "hat-trick",
]

# ── DATO ──────────────────────────────────────────────────────────────────────

def norsk_dato_igaar():
    return (datetime.now(NORSK_TZ) - timedelta(days=1)).date()

def utc_til_norsk_dato(utc_str):
    """Konverter UTC-datostreng (ISO 8601) til norsk dato-streng YYYY-MM-DD."""
    if not utc_str:
        return None
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return dt.astimezone(NORSK_TZ).strftime("%Y-%m-%d")
    except Exception:
        return utc_str[:10]

def formater_norsk_dato(dato_str):
    dato = datetime.strptime(dato_str, "%Y-%m-%d")
    return f"{NORSKE_DAGER[dato.weekday()]} {dato.day}. {NORSKE_MAANEDER[dato.month-1]} {dato.year}"

# ── LES FILER ─────────────────────────────────────────────────────────────────

def les_data_js():
    tekst = DATA_JS.read_text(encoding="utf-8")
    tekst = re.sub(r"^.*?const VM_DATA\s*=\s*", "", tekst, flags=re.DOTALL)
    tekst = tekst.strip().rstrip(";")
    return json.loads(tekst)

def les_referanser():
    if not REFERANSER_JSON.exists():
        print("  ADVARSEL: kamp-referanser.json ikke funnet")
        return {}
    return json.loads(REFERANSER_JSON.read_text(encoding="utf-8"))

def les_eksisterende_kamppost():
    if not KAMPPOST_JSON.exists():
        return None
    try:
        return json.loads(KAMPPOST_JSON.read_text(encoding="utf-8"))
    except Exception:
        return None

# ── KAMPER ────────────────────────────────────────────────────────────────────

def kampreferat_noekkel(hjemme, borte):
    lag = sorted([hjemme, borte])
    return f"{lag[0]}|||{lag[1]}"

def finn_gaarsdagens_kamper(vm_data):
    """
    Finn ferdigspilte kamper fra i går — bruker norsk tid konsekvent.
    fd_utcDate konverteres til norsk dato før sammenligning.
    """
    igaar = str(norsk_dato_igaar())
    kamper = []

    for kid, kamp in vm_data.get("resultater", {}).items():
        hjemme = kamp.get("hjemmelag", "")
        borte  = kamp.get("bortelag", "")

        # Hopp over placeholder-kamper
        if re.match(r"^[W1-9]", hjemme) or re.match(r"^[W1-9]", borte):
            continue
        if not kamp.get("ferdig"):
            continue

        # Konverter til norsk dato — prioriter fd_utcDate siden den er mest presis
        fd_utc  = kamp.get("fd_utcDate", "")
        norsk_dato = utc_til_norsk_dato(fd_utc) if fd_utc else kamp.get("dato_openfootball", "")

        if norsk_dato == igaar:
            kamper.append(kamp)

    return sorted(kamper, key=lambda k: k.get("fd_utcDate", k.get("dato_openfootball", "")))

def hent_tippinger_for_kamp(vm_data, kamp_id):
    eksakt, riktig, bom = [], [], []
    for d in vm_data.get("stilling", []):
        for t in d.get("tippinger", []):
            if t.get("kamp_id") == kamp_id:
                info = {
                    "navn":    d["navn"],
                    "tippa_h": t.get("tippa_h"),
                    "tippa_b": t.get("tippa_b"),
                    "poeng":   t.get("poeng", 0),
                }
                if t.get("eksakt"):
                    eksakt.append(info)
                elif t.get("riktig_utfall"):
                    riktig.append(info)
                else:
                    bom.append(info)
    return {"eksakt": eksakt, "riktig": riktig, "bom": bom}

# ── SØK VIA SERPER ────────────────────────────────────────────────────────────

def soek_serper(hjemme, borte, h_score, b_score):
    """Søk via Serper.dev og returner rå snippets."""
    if not SERPER_API_KEY:
        print("  ADVARSEL: SERPER_API_KEY ikke satt")
        return []

    spørring = f"{hjemme} {borte} {h_score}-{b_score} World Cup 2026 match report goals scorers"
    payload  = json.dumps({"q": spørring, "num": 8}).encode("utf-8")

    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data    = payload,
        headers = {
            "X-API-KEY":    SERPER_API_KEY,
            "Content-Type": "application/json",
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  ADVARSEL: Serper-søk feilet: {e}")
        return []

    snippets = []
    for item in data.get("organic", []):
        snippet = item.get("snippet", "").strip()
        if snippet and len(snippet) > 40:
            snippets.append(snippet)

    print(f"  → Hentet {len(snippets)} rå snippets fra Serper")
    return snippets

def filtrer_snippets(snippets):
    """
    Behold bare faktasetninger med kampinfo.
    Kast promo, preview og reklame.
    """
    gode = []
    for snippet in snippets:
        lav = snippet.lower()

        # Kast hvis inneholder promo-ord
        if any(p in lav for p in PROMO_ORD):
            continue

        # Behold hvis inneholder faktaord
        if any(f in lav for f in FAKTA_ORD):
            # Rens dato-prefixer som "Jun 18, 2026 ·"
            snippet = re.sub(r"^[A-Za-z]{3}\s+\d{1,2},\s+\d{4}\s*[·\-–]\s*", "", snippet).strip()
            if snippet:
                gode.append(snippet)

    print(f"  → {len(gode)} snippets etter filtrering")
    return gode

# ── BYGG RECAP-TEKST ──────────────────────────────────────────────────────────

def bygg_recap_tekst(kamp, snippets_raa, ref, tippinger):
    """
    Bygger recap-tekst i tre separate avsnitt:
    1. Kampreferat (fra filtrerte snippets)
    2. Historikk/fakta (fra kamp-referanser.json)
    3. Tippingsoppsummering
    """
    hjemme = kamp["hjemmelag"]
    borte  = kamp["bortelag"]
    h      = kamp["hjemme"]
    b      = kamp["borte"]

    historikk = ref.get("historikk", "")
    fakta     = ref.get("fakta", [])

    snippets_filtrert = filtrer_snippets(snippets_raa)

    avsnitt = []

    # ── Avsnitt 1: Kampreferat ──
    kamp_linjer = []

    # Kampresultat
    if h == b:
        kamp_linjer.append(f"{hjemme} og {borte} delte poengene {h}-{b}.")
    elif h > b:
        kamp_linjer.append(f"{hjemme} slo {borte} {h}-{b}.")
    else:
        kamp_linjer.append(f"{borte} slo {hjemme} {b}-{h}.")

    # Legg til filtrerte snippets
    for s in snippets_filtrert[:2]:
        if not s.endswith("."):
            s += "."
        kamp_linjer.append(s)

    avsnitt.append(" ".join(kamp_linjer))

    # ── Avsnitt 2: Historikk og fakta ──
    hist_linjer = []
    if historikk:
        hist_linjer.append(historikk)
    if fakta:
        hist_linjer.append(fakta[0])
    if hist_linjer:
        avsnitt.append(" ".join(hist_linjer))

    # ── Avsnitt 3: Tippingsoppsummering ──
    eksakt = tippinger["eksakt"]
    riktig = tippinger["riktig"]
    bom    = tippinger["bom"]

    tips_linjer = []

    if eksakt:
        navn_liste = ", ".join(
            f"{d['navn']} ({d['tippa_h']}-{d['tippa_b']})" for d in eksakt
        )
        if len(eksakt) == 1:
            tips_linjer.append(f"{navn_liste} traff eksakt og får 6 poeng!")
        else:
            tips_linjer.append(
                f"{len(eksakt)} tippere traff eksakt: {navn_liste}. 6 poeng hver!"
            )

    if riktig:
        navn_liste = ", ".join(
            f"{d['navn']} ({d['tippa_h']}-{d['tippa_b']})" for d in riktig
        )
        if len(riktig) == 1:
            tips_linjer.append(
                f"{navn_liste} tippa riktig utfall og får 2 poeng."
            )
        else:
            tips_linjer.append(f"Riktig utfall (2p): {navn_liste}.")

    if bom:
        if len(bom) <= 4:
            navn_liste = ", ".join(
                f"{d['navn']} ({d['tippa_h']}-{d['tippa_b']})" for d in bom
            )
            tips_linjer.append(f"Bom (0p): {navn_liste}.")
        else:
            utvalg = ", ".join(
                f"{d['navn']} ({d['tippa_h']}-{d['tippa_b']})" for d in bom[:3]
            )
            tips_linjer.append(
                f"{len(bom)} tippere bommet — blant dem {utvalg} og {len(bom)-3} andre."
            )

    if tips_linjer:
        avsnitt.append(" ".join(tips_linjer))

    # Returner avsnitt separert med dobbelt linjeskift
    return "\n\n".join(avsnitt)

# ── STILLING ──────────────────────────────────────────────────────────────────

def bygg_stilling(vm_data, alle_kamp_ids):
    stilling = []
    for d in vm_data.get("stilling", []):
        poeng_i_dag = sum(
            t.get("poeng", 0)
            for t in d.get("tippinger", [])
            if t.get("kamp_id") in alle_kamp_ids and t.get("ferdig")
        )
        stilling.append({
            "navn":         d["navn"],
            "plass":        d.get("plass", 0),
            "poeng_totalt": d.get("poeng_totalt", 0),
            "poeng_i_dag":  poeng_i_dag,
        })
    stilling.sort(key=lambda x: x["poeng_totalt"], reverse=True)
    return stilling

# ── HOVEDFUNKSJON ─────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("VM 2026 Kampreferat-generator")
    print("=" * 60)

    igaar     = norsk_dato_igaar()
    igaar_str = str(igaar)
    print(f"\nGårsdagens dato (norsk tid): {igaar_str}")

    # Sjekk om allerede generert
    eksisterende = les_eksisterende_kamppost()
    if eksisterende and eksisterende.get("dato") == igaar_str:
        print(f"  → kamppost.json allerede generert for {igaar_str}. Avslutter.")
        return

    # Les data
    print("\nLeser data/data.js...")
    vm_data = les_data_js()

    print("Leser data/kamp-referanser.json...")
    referanser = les_referanser()

    # Finn gårsdagens kamper (norsk tid)
    print(f"\nFinner kamper fra {igaar_str} (norsk tid)...")
    gaarsdagens = finn_gaarsdagens_kamper(vm_data)

    if not gaarsdagens:
        print(f"  → Ingen ferdigspilte kamper for {igaar_str}. Avslutter.")
        return

    print(f"  → {len(gaarsdagens)} kamper funnet")

    # Bygg kamposter
    kamposter = []
    for i, kamp in enumerate(gaarsdagens, 1):
        hjemme  = kamp["hjemmelag"]
        borte   = kamp["bortelag"]
        h_score = kamp["hjemme"]
        b_score = kamp["borte"]
        kamp_id = kamp["kamp_id"]

        print(f"\n[{i}/{len(gaarsdagens)}] {hjemme} {h_score}-{b_score} {borte}")

        ref         = referanser.get(kampreferat_noekkel(hjemme, borte), {})
        tippinger   = hent_tippinger_for_kamp(vm_data, kamp_id)
        snippets_raa = soek_serper(hjemme, borte, h_score, b_score)
        recap_tekst = bygg_recap_tekst(kamp, snippets_raa, ref, tippinger)

        print(f"  Eksakt: {len(tippinger['eksakt'])} | Riktig: {len(tippinger['riktig'])} | Bom: {len(tippinger['bom'])}")

        kamposter.append({
            "kamp_id":       kamp_id,
            "hjemmelag":     hjemme,
            "bortelag":      borte,
            "hjemme_score":  h_score,
            "borte_score":   b_score,
            "gruppe":        ref.get("gruppe", kamp.get("gruppe", "")),
            "recap_tekst":   recap_tekst,
            "snippets_raa":  snippets_raa,
            "tippinger":     tippinger,
        })

        time.sleep(1)

    # Stilling
    alle_kamp_ids = {k["kamp_id"] for k in gaarsdagens}
    stilling      = bygg_stilling(vm_data, alle_kamp_ids)

    # Skriv kamppost.json
    kamppost = {
        "dato":          igaar_str,
        "dato_norsk":    formater_norsk_dato(igaar_str),
        "generert":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "antall_kamper": len(kamposter),
        "kamper":        kamposter,
        "stilling":      stilling,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    KAMPPOST_JSON.write_text(
        json.dumps(kamppost, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"\n✓ Skrev kamppost.json med {len(kamposter)} kamper for {igaar_str}")

if __name__ == "__main__":
    main()
