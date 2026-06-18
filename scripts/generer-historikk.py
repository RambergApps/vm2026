"""
VM 2026 — Kampreferat-generator
Kjøres av GitHub Actions etter poengregning.py.

Flyt:
1. Finn gårsdagens ferdigspilte kamper (norsk tid) fra data/data.js
2. Sjekk om kamppost.json allerede er generert for i går
3. Søk etter kampreferat via Serper.dev (Google snippets)
4. Slå opp historikk og fakta fra data/kamp-referanser.json
5. Kombiner til ferdig recap-tekst per kamp
6. Skriv data/kamppost.json
"""

import json
import os
import re
import time
import urllib.request
import urllib.parse
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

# ── DATO ──────────────────────────────────────────────────────────────────────

def norsk_dato_igaar():
    return (datetime.now(NORSK_TZ) - timedelta(days=1)).date()

def formater_norsk_dato(dato_str):
    dato = datetime.strptime(dato_str, "%Y-%m-%d")
    return f"{NORSKE_DAGER[dato.weekday()]} {dato.day}. {NORSKE_MAANEDER[dato.month-1]} {dato.year}"

# ── LES FILER ────────────────────────────────────────────────────────────────

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

# ── KAMPER ───────────────────────────────────────────────────────────────────

def kampreferat_noekkel(hjemme, borte):
    lag = sorted([hjemme, borte])
    return f"{lag[0]}|||{lag[1]}"

def finn_gaarsdagens_kamper(vm_data):
    igaar = str(norsk_dato_igaar())
    kamper = []
    for kid, kamp in vm_data.get("resultater", {}).items():
        hjemme = kamp.get("hjemmelag", "")
        borte  = kamp.get("bortelag", "")
        if re.match(r"^[W1-9]", hjemme) or re.match(r"^[W1-9]", borte):
            continue
        if not kamp.get("ferdig"):
            continue
        dato = kamp.get("dato_openfootball") or kamp.get("dato_fd_org", "")[:10]
        if dato == igaar:
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

# ── SØK VIA SERPER ───────────────────────────────────────────────────────────

def soek_serper(hjemme, borte, h_score, b_score):
    """Søk via Serper.dev og returner organiske snippets."""
    if not SERPER_API_KEY:
        print("  ADVARSEL: SERPER_API_KEY ikke satt — hopper over søk")
        return []

    spørring = f"{hjemme} {borte} {h_score}-{b_score} World Cup 2026 match report goals"
    payload  = json.dumps({"q": spørring, "num": 5}).encode("utf-8")
    url      = "https://google.serper.dev/search"

    req = urllib.request.Request(
        url,
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

    print(f"  → Fant {len(snippets)} snippets fra Serper")
    return snippets[:5]

# ── BYGG RECAP-TEKST ─────────────────────────────────────────────────────────

def bygg_recap_tekst(kamp, snippets, ref, tippinger):
    """
    Kombinerer snippets + referanser + tippinger til en sammenhengende
    norsk recap-tekst per kamp.
    """
    hjemme  = kamp["hjemmelag"]
    borte   = kamp["bortelag"]
    h       = kamp["hjemme"]
    b       = kamp["borte"]

    hjemme_kallenavn = ref.get("kallenavn", {}).get(hjemme, hjemme)
    borte_kallenavn  = ref.get("kallenavn", {}).get(borte,  borte)
    historikk        = ref.get("historikk", "")
    fakta            = ref.get("fakta", [])
    gruppe           = ref.get("gruppe", "")

    linjer = []

    # ── Kampoverskrift ──
    gruppe_tekst = f" — Gruppe {gruppe}" if gruppe else ""
    if h == b:
        utfall = f"{hjemme_kallenavn} og {borte_kallenavn} delte poengene {h}-{b}{gruppe_tekst}."
    elif h > b:
        utfall = f"{hjemme_kallenavn} slo {borte_kallenavn} {h}-{b}{gruppe_tekst}."
    else:
        utfall = f"{borte_kallenavn} slo {hjemme_kallenavn} {b}-{h}{gruppe_tekst}."
    linjer.append(utfall)

    # ── Snippet-innhold ──
    if snippets:
        # Bruk beste snippet — rens og trim
        beste = snippets[0]
        # Fjern dato-prefixer som "Jun 18, 2026 ·"
        beste = re.sub(r"^[A-Za-z]{3}\s+\d{1,2},\s+\d{4}\s*[·\-–]\s*", "", beste)
        beste = beste.strip()
        if beste and not beste.endswith("."):
            beste += "."
        if beste:
            linjer.append(beste)

        # Hvis det er flere snippets med ny info, trekk ut ekstra detaljer
        for s in snippets[1:3]:
            s = re.sub(r"^[A-Za-z]{3}\s+\d{1,2},\s+\d{4}\s*[·\-–]\s*", "", s).strip()
            # Bare legg til hvis den inneholder mål/scorere og ikke er for lik forrige
            if any(ord in s.lower() for ord in ["goal", "scored", "penalty", "header", "minute"]):
                if s and s not in linjer:
                    if not s.endswith("."):
                        s += "."
                    linjer.append(s)
                    break

    # ── Historikk ──
    if historikk:
        linjer.append(historikk)

    # ── Fakta ──
    if fakta:
        linjer.append(fakta[0])

    # ── Tippinger ──
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
            tips_linjer.append(f"{len(eksakt)} tippere traff eksakt: {navn_liste}. 6 poeng hver!")

    if riktig:
        navn_liste = ", ".join(
            f"{d['navn']} ({d['tippa_h']}-{d['tippa_b']})" for d in riktig
        )
        if len(riktig) == 1:
            tips_linjer.append(f"{navn_liste} tippa riktig utfall og får 2 poeng.")
        else:
            tips_linjer.append(f"Riktig utfall (2p): {navn_liste}.")

    if bom:
        if len(bom) == 1:
            d = bom[0]
            tips_linjer.append(
                f"{d['navn']} tippa {d['tippa_h']}-{d['tippa_b']} og bommer fullstendig."
            )
        elif len(bom) <= 5:
            navn_liste = ", ".join(
                f"{d['navn']} ({d['tippa_h']}-{d['tippa_b']})" for d in bom
            )
            tips_linjer.append(f"Bom (0p): {navn_liste}.")
        else:
            # Mange bom — nevn noen og antall
            utvalg = ", ".join(
                f"{d['navn']} ({d['tippa_h']}-{d['tippa_b']})" for d in bom[:3]
            )
            tips_linjer.append(
                f"{len(bom)} tippere bommet — blant dem {utvalg} og {len(bom)-3} andre."
            )

    if tips_linjer:
        linjer.append(" ".join(tips_linjer))

    return " ".join(linjer)

# ── STILLING ─────────────────────────────────────────────────────────────────

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

    # Finn gårsdagens kamper
    print(f"\nFinner kamper fra {igaar_str}...")
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
        snippets    = soek_serper(hjemme, borte, h_score, b_score)
        recap_tekst = bygg_recap_tekst(kamp, snippets, ref, tippinger)

        print(f"  Eksakt: {len(tippinger['eksakt'])} | Riktig: {len(tippinger['riktig'])} | Bom: {len(tippinger['bom'])}")

        kamposter.append({
            "kamp_id":      kamp_id,
            "hjemmelag":    hjemme,
            "bortelag":     borte,
            "hjemme_score": h_score,
            "borte_score":  b_score,
            "gruppe":       ref.get("gruppe", kamp.get("gruppe", "")),
            "kallenavn":    ref.get("kallenavn", {}),
            "recap_tekst":  recap_tekst,
            "tippinger":    tippinger,
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
