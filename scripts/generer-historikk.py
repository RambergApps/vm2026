"""
VM 2026 — Kampreferat-generator
Kjøres av GitHub Actions etter poengregning.py.

Flyt:
1. Finn gårsdagens ferdigspilte kamper (norsk tid) fra data/data.js
2. Sjekk om kamppost.json allerede er generert for i går
3. Søk etter kampreferat-snippets for hver kamp (scorere, minutter, hendelser)
4. Slå opp historikk og fakta fra data/kamp-referanser.json
5. Kombiner med tippinger og poengresultater fra data/data.js
6. Skriv data/kamppost.json
"""

import json
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── KONFIG ────────────────────────────────────────────────────────────────────
REPO_ROOT        = Path(__file__).parent.parent
DATA_DIR         = REPO_ROOT / "data"
DATA_JS          = DATA_DIR / "data.js"
KAMPPOST_JSON    = DATA_DIR / "kamppost.json"
REFERANSER_JSON  = DATA_DIR / "kamp-referanser.json"

NORSK_TZ = timezone(timedelta(hours=2))  # CEST (sommertid)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; VM2026-recap/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "no,nb;q=0.9,en;q=0.8",
}

# ── HJELPEFUNKSJONER ──────────────────────────────────────────────────────────

def norsk_dato_igaar():
    return (datetime.now(NORSK_TZ) - timedelta(days=1)).date()

def les_data_js():
    """Les og parse data/data.js — returnerer VM_DATA-dict."""
    tekst = DATA_JS.read_text(encoding="utf-8")
    tekst = re.sub(r"^.*?const VM_DATA\s*=\s*", "", tekst, flags=re.DOTALL)
    tekst = tekst.strip().rstrip(";")
    return json.loads(tekst)

def les_referanser():
    """Les data/kamp-referanser.json."""
    if not REFERANSER_JSON.exists():
        print("  ADVARSEL: kamp-referanser.json ikke funnet")
        return {}
    return json.loads(REFERANSER_JSON.read_text(encoding="utf-8"))

def kampreferat_noekkel(hjemme, borte):
    """Lager alfabetisk sortert nøkkel for kamp-referanser.json."""
    lag = sorted([hjemme, borte])
    return f"{lag[0]}|||{lag[1]}"

def les_eksisterende_kamppost():
    """Les eksisterende kamppost.json hvis den finnes."""
    if not KAMPPOST_JSON.exists():
        return None
    try:
        return json.loads(KAMPPOST_JSON.read_text(encoding="utf-8"))
    except Exception:
        return None

def finn_gaarsdagens_kamper(vm_data):
    """Finn ferdigspilte kamper fra i går (norsk dato)."""
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
    """Hent alle deltakeres tippinger for en gitt kamp."""
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

def soek_kampreferat(hjemme, borte, hjemme_score, borte_score):
    """
    Søk etter kampreferat-snippets via DuckDuckGo HTML-søk.
    Returnerer liste med relevante snippets.
    """
    spørring = f"{hjemme} {borte} {hjemme_score}-{borte_score} World Cup 2026 goals scorers"
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(spørring)

    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ADVARSEL: Søk feilet for {hjemme} vs {borte}: {e}")
        return []

    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
    snippets = [re.sub(r"<[^>]+>", " ", s).strip() for s in snippets]
    snippets = [re.sub(r"\s+", " ", s) for s in snippets if len(s) > 40]

    relevante = [s for s in snippets if any(
        ord in s.lower() for ord in ["goal", "score", "minute", "scored", "pen", "header", "assist"]
    )]

    print(f"  → Fant {len(relevante)} relevante snippets")
    return relevante[:5]

def trekk_ut_scorere(snippets, hjemme, borte):
    """
    Forsøk å trekke ut scorere og minutter fra snippets.
    Returnerer dict med hjemme/borte scorerlister.
    """
    scorere = {"hjemme": [], "borte": []}
    maal_pattern = re.compile(
        r"([A-ZÁÉÍÓÚÀÈÌÒÙÄËÏÖÜÆØÅ][a-záéíóúàèìòùäëïöüæøå]+"
        r"(?:\s+[A-ZÁÉÍÓÚÀÈÌÒÙÄËÏÖÜÆØÅ][a-záéíóúàèìòùäëïöüæøå]+)*)"
        r"\s*\(?\s*(\d{1,3}(?:\+\d+)?)'?\s*(?:pen|pen\.|\(pen\))?\s*\)?",
        re.IGNORECASE
    )

    for snippet in snippets:
        for linje in snippet.split("."):
            linje_lower = linje.lower()
            if hjemme.lower().split()[0] in linje_lower and "goal" in linje_lower:
                for navn, min in maal_pattern.findall(linje):
                    if navn not in [h["navn"] for h in scorere["hjemme"]]:
                        scorere["hjemme"].append({"navn": navn, "minutt": min.strip("'")})
            if borte.lower().split()[0] in linje_lower and "goal" in linje_lower:
                for navn, min in maal_pattern.findall(linje):
                    if navn not in [b["navn"] for b in scorere["borte"]]:
                        scorere["borte"].append({"navn": navn, "minutt": min.strip("'")})

    return scorere

def formater_norsk_dato(dato_str):
    """Formater dato til norsk format: Torsdag 18. juni 2026"""
    dager    = ["Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag"]
    maaneder = ["januar", "februar", "mars", "april", "mai", "juni",
                "juli", "august", "september", "oktober", "november", "desember"]
    dato = datetime.strptime(dato_str, "%Y-%m-%d")
    return f"{dager[dato.weekday()]} {dato.day}. {maaneder[dato.month-1]} {dato.year}"

# ── HOVEDFUNKSJON ─────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("VM 2026 Kampreferat-generator")
    print("=" * 60)

    igaar     = norsk_dato_igaar()
    igaar_str = str(igaar)
    print(f"\nGårsdagens dato (norsk tid): {igaar_str}")

    # Sjekk om kamppost allerede er generert for i går
    eksisterende = les_eksisterende_kamppost()
    if eksisterende and eksisterende.get("dato") == igaar_str:
        print(f"  → kamppost.json er allerede generert for {igaar_str}. Avslutter.")
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
        print(f"  → Ingen ferdigspilte kamper funnet for {igaar_str}. Avslutter.")
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

        # Hent referanse
        ref_noekkel = kampreferat_noekkel(hjemme, borte)
        ref = referanser.get(ref_noekkel, {})
        if ref:
            print(f"  → Fant referanse: gruppe {ref.get('gruppe', '?')}")
        else:
            print(f"  → Ingen referanse funnet for nøkkel: {ref_noekkel}")

        # Hent tippinger
        tippinger = hent_tippinger_for_kamp(vm_data, kamp_id)
        print(f"  Eksakt: {len(tippinger['eksakt'])} | Riktig: {len(tippinger['riktig'])} | Bom: {len(tippinger['bom'])}")

        # Søk etter kampreferat
        snippets = soek_kampreferat(hjemme, borte, h_score, b_score)
        time.sleep(1.5)

        # Trekk ut scorere
        scorere = trekk_ut_scorere(snippets, hjemme, borte)

        kamposter.append({
            "kamp_id":      kamp_id,
            "hjemmelag":    hjemme,
            "bortelag":     borte,
            "hjemme_score": h_score,
            "borte_score":  b_score,
            "gruppe":       ref.get("gruppe", kamp.get("gruppe", "")),
            "kallenavn":    ref.get("kallenavn", {}),
            "historikk":    ref.get("historikk", ""),
            "fakta":        ref.get("fakta", []),
            "scorere":      scorere,
            "snippets":     snippets,
            "tippinger":    tippinger,
        })

    # Beregn poeng i dag per deltaker
    alle_kamp_ids = {k["kamp_id"] for k in gaarsdagens}
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
