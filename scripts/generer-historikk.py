"""
VM 2026 — Kampreferat-generator
Kjøres av GitHub Actions etter poengregning.py.

Prinsipp:
1. Finn gårsdagens ferdigspilte kamper (norsk tid) fra data/data.js
2. Sjekk om kamppost.json allerede er generert for i går
3. Hent kampkandidater fra Serper med streng kvotekontroll og cache
4. Score kandidater basert på kilde/tittel/snippet/resultat/kampord
5. Bruk kun snippets som rågrunnlag/debug — publisert recap_tekst skal være norsk
6. Slå opp historikk og fakta fra data/kamp-referanser.json
7. Ikke legg tipping-oppramsing inn i recap_tekst; tippinger lagres strukturert per kamp
8. Skriv data/kamppost.json
"""

import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

# ── KONFIG ────────────────────────────────────────────────────────────────────
REPO_ROOT          = Path(__file__).parent.parent
DATA_DIR           = REPO_ROOT / "data"
DATA_JS            = DATA_DIR / "data.js"
KAMPPOST_JSON      = DATA_DIR / "kamppost.json"
REFERANSER_JSON    = DATA_DIR / "kamp-referanser.json"
SERPER_CACHE_JSON  = DATA_DIR / "serper-cache.json"

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
NORSK_TZ       = timezone(timedelta(hours=2))  # CEST sommertid under VM

# Kvotekontroll. Halvtime-triggeren kan kjøre ofte, men Serper skal ikke gjøre det.
MAX_SERPER_SOK_PER_KAMP        = int(os.environ.get("MAX_SERPER_SOK_PER_KAMP", "2"))
MIN_MINUTTER_MELLOM_RETRY      = int(os.environ.get("MIN_MINUTTER_MELLOM_RETRY", "90"))
MIN_KVALITETSSCORE             = int(os.environ.get("MIN_KVALITETSSCORE", "8"))
SERPER_NUM_RESULTS             = int(os.environ.get("SERPER_NUM_RESULTS", "20"))
MAX_SERPER_SOK_TOTALT_KJORING  = int(os.environ.get("MAX_SERPER_SOK_TOTALT_KJORING", "20"))
SERPER_GL                      = os.environ.get("SERPER_GL", "us")
SERPER_HL                      = os.environ.get("SERPER_HL", "en")

NORSKE_DAGER    = ["Mandag","Tirsdag","Onsdag","Torsdag","Fredag","Lørdag","Søndag"]
NORSKE_MAANEDER = ["januar","februar","mars","april","mai","juni",
                   "juli","august","september","oktober","november","desember"]

# Lagnavn som ofte skrives ulikt i engelske kampreferater.
ALIASES = {
    "Czech Republic": ["Czech Republic", "Czechia"],
    "Bosnia & Herzegovina": ["Bosnia & Herzegovina", "Bosnia-Herzegovina", "Bosnia"],
    "USA": ["USA", "United States", "USMNT"],
    "DR Congo": ["DR Congo", "Congo DR", "Democratic Republic of Congo", "D.R. Congo"],
    "Ivory Coast": ["Ivory Coast", "Côte d'Ivoire", "Cote d'Ivoire"],
    "South Korea": ["South Korea", "Korea Republic"],
    "Cape Verde": ["Cape Verde", "Cabo Verde"],
    "Qatar": ["Qatar"],
    "Netherlands": ["Netherlands", "Dutch"],
}

# Sterke kilder får pluss i score. Dette er ikke en fasit, kun et signal.
GODE_DOMENER = [
    "fifa.com", "espn.com", "bbc.com", "skysports.com", "reuters.com", "apnews.com",
    "theguardian.com", "nytimes.com", "cbssports.com", "foxsports.com", "sports.yahoo.com",
    "nbcsports.com", "goal.com", "worldsoccertalk.com", "sportingnews.com",
]

# Kilder/innhold som typisk gir dårlig publiseringstekst.
BLOKKERTE_DOMENER = [
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com", "facebook.com", "x.com",
    "twitter.com", "reddit.com", "pinterest.", "threads.net",
]

NEGATIVE_ORD = [
    "highlights", "watch", "stream", "announced by", "click here", "subscribe", "follow",
    "tune in", "broadcast", "tv channel", "how to watch", "where to watch", "kick-off",
    "kickoff", "preview", "prediction", "odds", "betting", "under pressure", "crucial",
    "will face", "set to", "ahead of", "check out", "find out", "read more",
    "full coverage", "live updates", "live blog", "as it happened", "tickets", "lineups",
    "line-ups", "am i the only one", "i think", "follow for daily", "watchalong", "reaction",
]

FAKTA_ORD = [
    "scored", "goal", "goals", "minute", "penalty", "header", "assist", "red card",
    "yellow card", "own goal", "equaliser", "equalizer", "equalised", "equalized",
    "winner", "substitute", "substitution", "brace", "hat-trick", "stoppage time",
    "full-time", "full time", "defeated", "beat", "beats", "drew", "draw",
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


def iso_utc_na():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── LES/SKRIV FILER ───────────────────────────────────────────────────────────

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


def les_serper_cache():
    if not SERPER_CACHE_JSON.exists():
        return {}
    try:
        return json.loads(SERPER_CACHE_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ADVARSEL: Klarte ikke lese serper-cache.json: {e}")
        return {}


def skriv_serper_cache(cache):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SERPER_CACHE_JSON.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


# ── KAMPER ────────────────────────────────────────────────────────────────────

def kampreferat_noekkel(hjemme, borte):
    lag = sorted([hjemme, borte])
    return f"{lag[0]}|||{lag[1]}"


def cache_noekkel(kamp_id, h_score, b_score):
    # Inkluder resultat slik at korrigert sluttresultat gir nytt lovlig søk.
    return f"{kamp_id}|{h_score}-{b_score}"


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

        # Hopp over placeholder-kamper.
        if re.match(r"^[W1-9]", hjemme) or re.match(r"^[W1-9]", borte):
            continue
        if not kamp.get("ferdig"):
            continue

        fd_utc = kamp.get("fd_utcDate", "")
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


# ── SERPER: QUERY, CACHE, SCORING ─────────────────────────────────────────────

def aliases_for(lag):
    verdier = ALIASES.get(lag, [lag])
    # Behold rekkefølge, fjern dubletter.
    sett = set()
    resultat = []
    for v in verdier:
        if v and v.lower() not in sett:
            resultat.append(v)
            sett.add(v.lower())
    return resultat


def quote_or_alias(lag):
    aliaser = aliases_for(lag)
    if len(aliaser) == 1:
        return f'"{aliaser[0]}"'
    return "(" + " OR ".join(f'"{a}"' for a in aliaser) + ")"


def bygg_serper_query(hjemme, borte, h_score, b_score):
    resultat = f'"{h_score}-{b_score}"'
    # Negative fraser bygges eksplisitt slik at vi ikke bruker ekstra Serper-søk på dårlige treff.
    negative_terms = [
        "-watch", "-highlights", "-stream", "-preview", "-prediction", "-odds", "-betting",
        "-youtube", "-tiktok", "-instagram", "-reddit", "-tickets", "-lineups",
        "-\"how to watch\"", "-\"live blog\"", "-\"live updates\"",
    ]
    return (
        f"{quote_or_alias(hjemme)} {quote_or_alias(borte)} {resultat} "
        f'"World Cup" ("match report" OR recap OR goals OR scorers OR "full-time") '
        + " ".join(negative_terms)
    )


def domene_fra_url(url):
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc.replace("www.", "")
    except Exception:
        return ""


def tekst_for_kandidat(k):
    return " ".join([
        k.get("title", ""),
        k.get("source", ""),
        k.get("snippet", ""),
        k.get("link", ""),
    ]).strip()


def normaliser_tekst(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def inneholder_alias(text_lower, lag):
    return any(alias.lower() in text_lower for alias in aliases_for(lag))


def score_kandidat(k, hjemme, borte, h_score, b_score):
    tekst = normaliser_tekst(tekst_for_kandidat(k))
    title = normaliser_tekst(k.get("title", ""))
    snippet = normaliser_tekst(k.get("snippet", ""))
    link = k.get("link", "")
    domene = domene_fra_url(link)
    score = 0
    grunner = []

    if not snippet or len(snippet) < 40:
        return -100, ["for kort snippet"]

    if any(d in domene for d in BLOKKERTE_DOMENER):
        score -= 12
        grunner.append("blokkert domene")

    if any(d in domene for d in GODE_DOMENER):
        score += 5
        grunner.append("god kilde")

    if inneholder_alias(tekst, hjemme):
        score += 3
        grunner.append("hjemmelag nevnt")
    if inneholder_alias(tekst, borte):
        score += 3
        grunner.append("bortelag nevnt")

    resultat_varianter = [
        f"{h_score}-{b_score}", f"{h_score}–{b_score}", f"{h_score} - {b_score}", f"{h_score} to {b_score}",
        f"{h_score} {b_score}",
    ]
    if any(r in tekst for r in resultat_varianter):
        score += 4
        grunner.append("resultat nevnt")

    if "match report" in title or "recap" in title or "full-time" in title or "full time" in title:
        score += 3
        grunner.append("kampreferat-tittel")

    for ord_ in FAKTA_ORD:
        if ord_ in tekst:
            score += 2

    for ord_ in NEGATIVE_ORD:
        if ord_ in tekst:
            score -= 5
            grunner.append(f"negativt ord: {ord_}")

    if snippet.endswith("...") or " ..." in snippet:
        score -= 2
        grunner.append("avkortet snippet")

    # Førsteperson/opinion skal ikke inn i recapgrunnlag.
    if re.search(r"\b(i|we)\b.*\b(think|feel|guess)\b", tekst):
        score -= 8
        grunner.append("opinion/førsteperson")

    k["score"] = score
    k["score_grunner"] = grunner[:8]
    return score, grunner


def dedupliser_kandidater(kandidater):
    sett = set()
    unike = []
    for k in kandidater:
        link_key = normaliser_tekst(k.get("link", ""))
        title_key = normaliser_tekst(k.get("title", ""))[:120]
        snippet_key = normaliser_tekst(k.get("snippet", ""))[:140]
        key = link_key or f"{title_key}|{snippet_key}"
        if key in sett:
            continue
        sett.add(key)
        unike.append(k)
    return unike


def soek_serper_api(query):
    """Ett Serper-kall. Returnerer kandidater med title/link/source/date/snippet."""
    if not SERPER_API_KEY:
        print("  ADVARSEL: SERPER_API_KEY ikke satt")
        return []

    payload = json.dumps({
        "q": query,
        "num": SERPER_NUM_RESULTS,
        "gl": SERPER_GL,
        "hl": SERPER_HL,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=payload,
        headers={
            "X-API-KEY": SERPER_API_KEY,
            "Content-Type": "application/json",
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  ADVARSEL: Serper-søk feilet: {e}")
        return []

    kandidater = []
    for item in data.get("organic", []):
        snippet = item.get("snippet", "").strip()
        if not snippet:
            continue
        kandidater.append({
            "title":   item.get("title", "").strip(),
            "link":    item.get("link", "").strip(),
            "source":  item.get("source", "").strip(),
            "date":    item.get("date", "").strip(),
            "snippet": re.sub(r"^[A-Za-z]{3}\s+\d{1,2},\s+\d{4}\s*[·\-–]\s*", "", snippet).strip(),
            "query":   query,
        })

    print(f"  → Hentet {len(kandidater)} kandidater fra Serper")
    return kandidater


def minutter_siden_iso(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except Exception:
        return None


def hent_kandidater_med_cache(kamp, cache, serper_teller):
    hjemme  = kamp["hjemmelag"]
    borte   = kamp["bortelag"]
    h_score = kamp["hjemme"]
    b_score = kamp["borte"]
    kamp_id = kamp["kamp_id"]
    key     = cache_noekkel(kamp_id, h_score, b_score)

    entry = cache.get(key)
    if entry:
        kandidater = entry.get("kandidater", [])
        beste_score = entry.get("beste_score", -100)
        antall_sok = entry.get("antall_sok", 0)
        status = entry.get("status", "ukjent")

        if status == "ok" and beste_score >= MIN_KVALITETSSCORE:
            print(f"  → Serper cache hit: score {beste_score} OK")
            return kandidater, entry, serper_teller

        if antall_sok >= MAX_SERPER_SOK_PER_KAMP:
            print(f"  → Serper maks søk brukt ({antall_sok}/{MAX_SERPER_SOK_PER_KAMP}). Bruker cache/fallback.")
            return kandidater, entry, serper_teller

        minutter = minutter_siden_iso(entry.get("sist_sokt"))
        if minutter is not None and minutter < MIN_MINUTTER_MELLOM_RETRY:
            print(f"  → Serper retry sperret ({int(minutter)} min siden sist). Bruker cache/fallback.")
            return kandidater, entry, serper_teller

    if serper_teller >= MAX_SERPER_SOK_TOTALT_KJORING:
        print(f"  → Serper totalgrense for kjøring nådd ({MAX_SERPER_SOK_TOTALT_KJORING}). Bruker fallback.")
        fallback_entry = entry or {"antall_sok": 0, "kandidater": [], "beste_score": -100, "status": "ikke_sokt"}
        return fallback_entry.get("kandidater", []), fallback_entry, serper_teller

    query = bygg_serper_query(hjemme, borte, h_score, b_score)
    print(f"  → Serper nytt søk {(entry or {}).get('antall_sok', 0) + 1}/{MAX_SERPER_SOK_PER_KAMP}")
    print(f"    Query: {query}")

    kandidater = soek_serper_api(query)
    serper_teller += 1

    kandidater = dedupliser_kandidater(kandidater)
    for k in kandidater:
        score_kandidat(k, hjemme, borte, h_score, b_score)
    kandidater.sort(key=lambda x: x.get("score", -999), reverse=True)

    beste_score = kandidater[0].get("score", -100) if kandidater else -100
    status = "ok" if beste_score >= MIN_KVALITETSSCORE else "lav_score"

    antall_sok = (entry or {}).get("antall_sok", 0) + 1
    ny_entry = {
        "kamp_id": kamp_id,
        "resultat": f"{h_score}-{b_score}",
        "sist_sokt": iso_utc_na(),
        "antall_sok": antall_sok,
        "beste_score": beste_score,
        "status": status,
        "query": query,
        "kandidater": kandidater[:10],
    }
    cache[key] = ny_entry
    skriv_serper_cache(cache)

    if status == "ok":
        print(f"  → Serper score {beste_score} OK")
    else:
        print(f"  → Serper score {beste_score} lav. Bruker norsk fallback hvis ingen trygge fakta.")

    return kandidater, ny_entry, serper_teller


# ── NORSK RECAP-TEKST ─────────────────────────────────────────────────────────

def resultatsetning(hjemme, borte, h, b):
    if h == b:
        return f"{hjemme} og {borte} delte poengene {h}–{b}."
    if h > b:
        return f"{hjemme} slo {borte} {h}–{b}."
    return f"{borte} slo {hjemme} {b}–{h}."


def fallback_kamptekst(hjemme, borte, h, b):
    if h == b:
        return f"Begge lag fikk med seg ett poeng etter en jevn kamp i gruppespillet."
    vinner = hjemme if h > b else borte
    taper = borte if h > b else hjemme
    return f"{vinner} fikk en sterk start på kampenes poengfangst, mens {taper} må jakte svar i neste runde."


def finn_mulig_scorer_og_hendelse(kandidater):
    """
    Forsiktig, regelbasert uthenting av enkle fakta fra engelsk snippet.
    Brukes kun til å lage norsk maltekst. Rå snippet publiseres ikke.
    """
    if not kandidater:
        return None

    gode = [k for k in kandidater if k.get("score", -100) >= MIN_KVALITETSSCORE]
    if not gode:
        return None

    tekst = " ".join((gode[0].get("title", ""), gode[0].get("snippet", "")))
    ren = re.sub(r"\s+", " ", tekst).strip()

    # Eksempler som fanges:
    # "Caleb Yirenkyi scored a 95th-minute winner"
    # "A late penalty from Teboho Mokoena secured ..."
    # "Michal Sadilek fired the Czechs into the lead in the sixth minute"
    patterns = [
        (r"([A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+scored\s+(?:a\s+)?(?:\d+(?:st|nd|rd|th)?[- ]minute\s+)?(winner|equaliser|equalizer|goal|penalty)?", "scored"),
        (r"(?:penalty|goal)\s+from\s+([A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})", "from_goal"),
        (r"([A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?:fired|headed|converted|netted|levelled|leveled|equalised|equalized)", "action"),
    ]

    spiller = None
    hendelse = "mål"
    for pattern, typ in patterns:
        m = re.search(pattern, ren)
        if m:
            spiller = m.group(1).strip()
            lower = ren.lower()
            if "stoppage time" in lower or "95th-minute" in lower or "94" in lower or "late" in lower:
                hendelse = "sen scoring"
            if "penalty" in lower:
                hendelse = "straffescoring"
            if "winner" in lower:
                hendelse = "vinnermål"
            if "equaliser" in lower or "equalizer" in lower or "levelled" in lower or "leveled" in lower:
                hendelse = "utligning"
            break

    if not spiller:
        return None

    # Unngå å bruke lagnavn eller generiske ord som spiller.
    if spiller.lower() in ["world cup", "full time", "fifa world", "match report"]:
        return None

    return {"spiller": spiller, "hendelse": hendelse, "kilde_score": gode[0].get("score", 0)}


def bygg_norsk_kampavsnitt(kamp, kandidater):
    hjemme = kamp["hjemmelag"]
    borte  = kamp["bortelag"]
    h      = kamp["hjemme"]
    b      = kamp["borte"]

    linjer = [resultatsetning(hjemme, borte, h, b)]
    fakta = finn_mulig_scorer_og_hendelse(kandidater)

    if fakta:
        spiller = fakta["spiller"]
        hendelse = fakta["hendelse"]
        if hendelse == "vinnermål":
            linjer.append(f"{spiller} ble kampens avgjørende navn med vinnermålet.")
        elif hendelse == "utligning":
            linjer.append(f"{spiller} sørget for utligningen som sikret poengdeling.")
        elif hendelse == "straffescoring":
            linjer.append(f"{spiller} kom på scoringslisten fra straffemerket.")
        elif hendelse == "sen scoring":
            linjer.append(f"{spiller} kom på scoringslisten sent i kampen.")
        else:
            linjer.append(f"{spiller} var blant spillerne som kom på scoringslisten.")
    else:
        linjer.append(fallback_kamptekst(hjemme, borte, h, b))

    return " ".join(linjer)


def bygg_recap_tekst(kamp, kandidater, ref):
    """
    Bygger recap-tekst i separate avsnitt:
    1. Norsk kampavsnitt basert på resultat + trygt uthentede fakta/fallback
    2. Historikk/fakta fra kamp-referanser.json

    Tipping skal ikke inn i recap_tekst. Den ligger strukturert i kamp.tippinger.
    """
    historikk = ref.get("historikk", "")
    fakta     = ref.get("fakta", [])

    avsnitt = [bygg_norsk_kampavsnitt(kamp, kandidater)]

    hist_linjer = []
    if historikk:
        hist_linjer.append(historikk)
    if fakta:
        hist_linjer.append(fakta[0])
    if hist_linjer:
        avsnitt.append(" ".join(hist_linjer))

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

    # Sjekk om allerede generert.
    eksisterende = les_eksisterende_kamppost()
    if eksisterende and eksisterende.get("dato") == igaar_str:
        print(f"  → kamppost.json allerede generert for {igaar_str}. Avslutter.")
        return

    print("\nLeser data/data.js...")
    vm_data = les_data_js()

    print("Leser data/kamp-referanser.json...")
    referanser = les_referanser()

    cache = les_serper_cache()
    serper_teller = 0

    print(f"\nFinner kamper fra {igaar_str} (norsk tid)...")
    gaarsdagens = finn_gaarsdagens_kamper(vm_data)

    if not gaarsdagens:
        print(f"  → Ingen ferdigspilte kamper for {igaar_str}. Avslutter.")
        return

    print(f"  → {len(gaarsdagens)} kamper funnet")

    kamposter = []
    for i, kamp in enumerate(gaarsdagens, 1):
        hjemme  = kamp["hjemmelag"]
        borte   = kamp["bortelag"]
        h_score = kamp["hjemme"]
        b_score = kamp["borte"]
        kamp_id = kamp["kamp_id"]

        print(f"\n[{i}/{len(gaarsdagens)}] {hjemme} {h_score}-{b_score} {borte}")

        ref       = referanser.get(kampreferat_noekkel(hjemme, borte), {})
        tippinger = hent_tippinger_for_kamp(vm_data, kamp_id)

        kandidater, cache_entry, serper_teller = hent_kandidater_med_cache(kamp, cache, serper_teller)
        recap_tekst = bygg_recap_tekst(kamp, kandidater, ref)

        beste_score = cache_entry.get("beste_score", -100) if cache_entry else -100
        fallback = beste_score < MIN_KVALITETSSCORE

        print(f"  Eksakt: {len(tippinger['eksakt'])} | Riktig: {len(tippinger['riktig'])} | Bom: {len(tippinger['bom'])}")
        print(f"  Recap-kvalitet: score={beste_score}, fallback={fallback}")

        kamposter.append({
            "kamp_id":       kamp_id,
            "hjemmelag":     hjemme,
            "bortelag":      borte,
            "hjemme_score":  h_score,
            "borte_score":   b_score,
            "gruppe":        ref.get("gruppe", kamp.get("gruppe", "")),
            "recap_tekst":   recap_tekst,
            # Beholder rådata for debugging, men frontend bør ikke publisere disse direkte.
            "snippets_raa":  [k.get("snippet", "") for k in kandidater[:5]],
            "serper_kandidater": kandidater[:5],
            "recap_kvalitet": {
                "score":       beste_score,
                "status":      cache_entry.get("status", "ukjent") if cache_entry else "ukjent",
                "antall_sok":  cache_entry.get("antall_sok", 0) if cache_entry else 0,
                "fallback":    fallback,
                "cache_key":   cache_noekkel(kamp_id, h_score, b_score),
            },
            "tippinger":     tippinger,
        })

        # Lite pusterom hvis flere nye Serper-søk skjer i samme kjøring.
        time.sleep(0.25)

    alle_kamp_ids = {k["kamp_id"] for k in gaarsdagens}
    stilling      = bygg_stilling(vm_data, alle_kamp_ids)

    kamppost = {
        "dato":          igaar_str,
        "dato_norsk":    formater_norsk_dato(igaar_str),
        "generert":      iso_utc_na(),
        "antall_kamper": len(kamposter),
        "kamper":        kamposter,
        "stilling":      stilling,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    KAMPPOST_JSON.write_text(
        json.dumps(kamppost, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    skriv_serper_cache(cache)
    print(f"\n✓ Skrev kamppost.json med {len(kamposter)} kamper for {igaar_str}")
    print(f"✓ Serper-søk brukt i denne kjøringen: {serper_teller}")


if __name__ == "__main__":
    main()
