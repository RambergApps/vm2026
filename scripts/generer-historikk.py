"""
VM 2026 — Kampreferat-generator
Kjøres av GitHub Actions etter poengregning.py.

Prinsipp:
1. Finn gårsdagens ferdigspilte kamper (norsk tid) fra data/data.js
2. Hvis kamppost.json allerede finnes for datoen: regenerer rapporten, men seed Serper-cache fra eksisterende data
3. Hent kampkandidater fra Serper med streng kvotekontroll og cache
4. Score kandidater basert på kilde/tittel/snippet/resultat/kampord
5. Forsøk å hente full kamptekst/fakta fra utvalgte kilder funnet av Serper
   - Reuters og ESPN brukes ikke til fulltekst, men kan fortsatt brukes som snippet-kandidater
   - Jina Reader kan brukes for kilder som tillater reader-henting, f.eks. CONCACAF
6. Bruk snippets som fallback/debug — publisert recap_tekst skal være norsk
7. Slå opp historikk og fakta fra data/kamp-referanser.json
8. Ikke legg tipping-oppramsing inn i recap_tekst; tippinger lagres strukturert per kamp
9. Skriv data/kamppost.json
"""

import json
import os
import re
import time
import urllib.request
import urllib.error
import html
import unicodedata
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

# Fulltekst/faktahenting bruker ikke Serper-kvote, men caches slik at vi ikke
# henter samme side hver halvtime. Vi lagrer bare ekstraherte fakta, ikke
# artikkeltekst, for å unngå at repoet fylles med tredjepartsinnhold.
FULLTEKST_AKTIVERT             = os.environ.get("FULLTEKST_AKTIVERT", "1") != "0"
MAX_FULLTEKST_PER_KJORING      = int(os.environ.get("MAX_FULLTEKST_PER_KJORING", "8"))
FULLTEKST_TIMEOUT              = int(os.environ.get("FULLTEKST_TIMEOUT", "12"))
FULLTEKST_MIN_SCORE            = int(os.environ.get("FULLTEKST_MIN_SCORE", "8"))
FULLTEKST_MAX_BYTES            = int(os.environ.get("FULLTEKST_MAX_BYTES", "450000"))
JINA_READER_AKTIVERT           = os.environ.get("JINA_READER_AKTIVERT", "1") != "0"
FULLTEKST_CACHE_VERSION        = "v14-fifa-artikkel-fulltekst"
OFFISIELL_KILDE_SOK_AKTIVERT   = os.environ.get("OFFISIELL_KILDE_SOK_AKTIVERT", "1") != "0"
MAX_OFFISIELLE_KILDESOK_KAMP   = int(os.environ.get("MAX_OFFISIELLE_KILDESOK_KAMP", "2"))
OFFISIELL_KILDE_SOK_VERSION    = "v10-fifa-forst-artikkeltype"

# Historikk/referanser skal bare brukes som fallback når kampdataene er for tynne.
# Poenget med kamppost er ferskt kampreferat, ikke daglig manuelt vedlikehold av referanser.
HISTORIKK_SOM_FALLBACK         = os.environ.get("HISTORIKK_SOM_FALLBACK", "1") != "0"
MIN_RECAP_SETNINGER            = int(os.environ.get("MIN_RECAP_SETNINGER", "3"))
BRUK_SNIPPETS_I_RECAP          = os.environ.get("BRUK_SNIPPETS_I_RECAP", "0") == "1"

# Ferske kamper kan få bedre kilder etter første kjøring.
# Ikke la en tidlig OK-snippet låse kampen hvis fulltekst/fakta fortsatt er tynn.
FERSK_KAMP_REFRESH_TIMER       = int(os.environ.get("FERSK_KAMP_REFRESH_TIMER", "24"))
MIN_MINUTTER_MELLOM_OK_REFRESH = int(os.environ.get("MIN_MINUTTER_MELLOM_OK_REFRESH", "30"))
MAX_SERPER_REFRESH_PER_KAMP    = int(os.environ.get("MAX_SERPER_REFRESH_PER_KAMP", "3"))


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
    "nbcsports.com", "goal.com", "worldsoccertalk.com", "sportingnews.com", "concacaf.com",
]

# Fulltekst-kilder prioriteres. Serper finner kandidatene; dette steget henter
# kun fra selve kilden/reader og teller ikke mot Serper-kvoten.
# Reuters og ESPN er bevisst utelatt: de kan fortsatt gi gode snippets via Serper,
# men er for ustabile/blokkerte som fulltekst-kilder i GitHub Actions.
FULLTEKST_PRIORITET = [
    # FIFA-artiklene har vist seg å gi best fulltekstgrunnlag for gode referater.
    ("fifa.com", 120),
    ("concacaf.com", 90),
    ("apnews.com", 85),
    ("bbc.com", 80),
    ("skysports.com", 70),
    ("theguardian.com", 65),
]

FULLTEKST_UTELUKKEDE_DOMENER = [
    "reuters.com",
    "espn.com", "espn.co.uk", "espn.com.sg", "espn.com.au", "espn.",
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


def seed_cache_fra_eksisterende_kamppost(eksisterende, cache):
    """
    Hvis kamppost.json finnes fra før, kan den inneholde gode Serper-kandidater.
    Bruk disse til å fylle serper-cache.json når cache mangler, slik at en ren
    regenerering av teksten ikke brenner nye Serper-søk.
    """
    if not eksisterende or not isinstance(eksisterende, dict):
        return 0

    antall = 0
    for kamp in eksisterende.get("kamper", []) or []:
        kvalitet = kamp.get("recap_kvalitet", {}) or {}
        key = kvalitet.get("cache_key")
        if not key:
            kamp_id = kamp.get("kamp_id")
            h_score = kamp.get("hjemme_score")
            b_score = kamp.get("borte_score")
            if kamp_id is None or h_score is None or b_score is None:
                continue
            key = cache_noekkel(kamp_id, h_score, b_score)

        kandidater = kamp.get("serper_kandidater", []) or []
        # Ikke overskriv eksisterende cache med data fra kamppost, med mindre cache mangler kandidater.
        if key in cache and cache.get(key, {}).get("kandidater"):
            continue

        cache[key] = {
            "kamp_id": kamp.get("kamp_id", ""),
            "resultat": f"{kamp.get('hjemme_score', '')}-{kamp.get('borte_score', '')}",
            "sist_sokt": eksisterende.get("generert") or iso_utc_na(),
            "antall_sok": int(kvalitet.get("antall_sok", 0) or 0),
            "beste_score": int(kvalitet.get("score", -100) or -100),
            "status": kvalitet.get("status", "ok" if int(kvalitet.get("score", -100) or -100) >= MIN_KVALITETSSCORE else "lav_score"),
            "query": (kandidater[0].get("query", "") if kandidater else ""),
            "kandidater": kandidater[:15],
        }
        antall += 1

    return antall


def uten_generert(obj):
    """Returner kopi brukt til diff-sjekk uten feltet som alltid endrer seg."""
    if not isinstance(obj, dict):
        return obj
    kopi = dict(obj)
    kopi.pop("generert", None)
    return kopi


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


def sokelag_navn(lag):
    """
    Returnerer ett søkevennlig lagnavn.
    Vi unngår avansert Google-syntaks med OR/parenteser fordi Serper kan avvise
    for komplekse query-strenger med HTTP 400.
    """
    aliaser = aliases_for(lag)
    foretrukket = {
        "Czech Republic": "Czechia",
        "Bosnia & Herzegovina": "Bosnia Herzegovina",
        "USA": "United States",
        "DR Congo": "Congo DR",
        "Ivory Coast": "Ivory Coast",
        "South Korea": "South Korea",
        "Cape Verde": "Cape Verde",
    }
    return foretrukket.get(lag, aliaser[0] if aliaser else lag)


def bygg_serper_query(hjemme, borte, h_score, b_score):
    """
    Lager én robust Serper-query per kamp.
    Bevisst enkel syntaks: ingen OR, ingen parenteser og ingen negative quoted phrases.
    Scoringen i etterkant avgjør hvilke treff som er gode nok.
    """
    negative_terms = [
        "-watch", "-highlights", "-stream", "-preview", "-prediction", "-odds", "-betting",
        "-youtube", "-tiktok", "-instagram", "-reddit", "-tickets", "-lineups",
        "-liveblog", "-liveupdates",
    ]
    return " ".join([
        sokelag_navn(hjemme),
        sokelag_navn(borte),
        f"{h_score}-{b_score}",
        "World Cup 2026",
        "match report recap goals scorers full-time",
        *negative_terms,
    ])

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
            # FIFA bruker ofte tittelen "Match report and highlights" for ordinære kampreferater.
            # Det skal ikke straffes som video-/highlight-støy.
            if ord_ == "highlights" and "match report" in title and "fifa.com" in domene:
                continue
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
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        print(f"  ADVARSEL: Serper-søk feilet: HTTP {e.code}: {e.reason}")
        if body:
            print(f"  Serper-respons: {body[:500]}")
        return []
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


def cache_trenger_ok_refresh(entry):
    """
    True når Serper-cache er OK, men referatgrunnlaget fortsatt er svakt.
    Dette hindrer at en tidlig snippet låser kampen før fullere kampreferater er indeksert.
    """
    if not isinstance(entry, dict):
        return False

    minutter = minutter_siden_iso(entry.get("sist_sokt"))
    if minutter is None:
        return False
    if minutter > FERSK_KAMP_REFRESH_TIMER * 60:
        return False
    if minutter < MIN_MINUTTER_MELLOM_OK_REFRESH:
        return False

    refresh_antall = int(entry.get("ok_refresh_antall", 0) or 0)
    if refresh_antall >= MAX_SERPER_REFRESH_PER_KAMP:
        return False

    fulltekst = entry.get("fulltekst_fakta") or {}
    fulltekst_ok = isinstance(fulltekst, dict) and fulltekst.get("status") == "ok"

    # Hvis vi allerede har god fulltekst og strukturerte fakta, trenger vi ikke nytt Serper-søk.
    if fulltekst_ok and har_rikt_fulltekstgrunnlag(fulltekst):
        return False

    # Prøv igjen når fulltekst mangler/ikke ble funnet, eller når kandidatene bare har tynne snippets.
    if not fulltekst_ok:
        return True

    return False


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
            if cache_trenger_ok_refresh(entry):
                minutter = minutter_siden_iso(entry.get("sist_sokt"))
                print(f"  → Serper cache OK, men referatgrunnlag er tynt. Tillater refresh ({int(minutter or 0)} min siden sist).")
            else:
                print(f"  → Serper cache hit: score {beste_score} OK")
                return kandidater, entry, serper_teller

        maks_sok = MAX_SERPER_SOK_PER_KAMP
        if status == "ok" and beste_score >= MIN_KVALITETSSCORE:
            maks_sok = max(MAX_SERPER_SOK_PER_KAMP, MAX_SERPER_REFRESH_PER_KAMP)

        if antall_sok >= maks_sok:
            print(f"  → Serper maks søk brukt ({antall_sok}/{maks_sok}). Bruker cache/fallback.")
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
    ok_refresh_antall = int((entry or {}).get("ok_refresh_antall", 0) or 0)
    if entry and entry.get("status") == "ok" and int((entry or {}).get("beste_score", -100) or -100) >= MIN_KVALITETSSCORE:
        ok_refresh_antall += 1
    ny_entry = {
        "kamp_id": kamp_id,
        "resultat": f"{h_score}-{b_score}",
        "sist_sokt": iso_utc_na(),
        "antall_sok": antall_sok,
        "ok_refresh_antall": ok_refresh_antall,
        "beste_score": beste_score,
        "status": status,
        "query": query,
        "kandidater": kandidater[:15],
    }
    if entry and entry.get("fulltekst_fakta"):
        # Behold tidligere fulltekst-fakta inntil fulltekst-steget eventuelt oppdaterer dem.
        ny_entry["fulltekst_fakta"] = entry.get("fulltekst_fakta")
    cache[key] = ny_entry
    skriv_serper_cache(cache)

    if status == "ok":
        print(f"  → Serper score {beste_score} OK")
    else:
        print(f"  → Serper score {beste_score} lav. Bruker norsk fallback hvis ingen trygge fakta.")

    return kandidater, ny_entry, serper_teller


# ── NORSK RECAP-TEKST ─────────────────────────────────────────────────────────

# Bruk norske navn i publisert tekst. Dette påvirker ikke datafeltene/kamp_id.
NORSKE_LAGNAVN = {
    "Czech Republic": "Tsjekkia",
    "Czechia": "Tsjekkia",
    "South Africa": "Sør-Afrika",
    "South Korea": "Sør-Korea",
    "Switzerland": "Sveits",
    "Bosnia & Herzegovina": "Bosnia-Hercegovina",
    "Uzbekistan": "Usbekistan",
    "Ivory Coast": "Elfenbenskysten",
    "DR Congo": "DR Kongo",
    "Germany": "Tyskland",
    "France": "Frankrike",
    "Norway": "Norge",
    "Sweden": "Sverige",
    "Netherlands": "Nederland",
    "Egypt": "Egypt",
    "New Zealand": "New Zealand",
    "Saudi Arabia": "Saudi-Arabia",
    "Cape Verde": "Kapp Verde",
    "Spain": "Spania",
    "Brazil": "Brasil",
    "Morocco": "Marokko",
    "Scotland": "Skottland",
    "Turkey": "Tyrkia",
    "Austria": "Østerrike",
    "Algeria": "Algerie",
}

NAVNEORD_STOPP = {
    "World", "Cup", "Fifa", "FIFA", "Full", "Time", "Final", "Score", "Group",
    "Round", "Match", "Report", "Game", "Analysis", "Expert", "Recap", "Wednesday",
    "Thursday", "Friday", "Saturday", "Sunday", "Monday", "Tuesday", "June", "Jun",
    "Second", "Half", "First", "Late", "Goal", "Goals", "Penalty", "Substitutes",
}


def visningsnavn_lag(lag):
    return NORSKE_LAGNAVN.get(lag, lag)


def resultatsetning(hjemme, borte, h, b):
    hnavn = visningsnavn_lag(hjemme)
    bnavn = visningsnavn_lag(borte)
    if h == b:
        return f"{hnavn} og {bnavn} delte poengene {h}–{b}."
    if h > b:
        return f"{hnavn} slo {bnavn} {h}–{b}."
    return f"{bnavn} slo {hnavn} {b}–{h}."


def fallback_kamptekst(hjemme, borte, h, b):
    if h == b:
        return "Begge lag fikk med seg ett poeng etter en jevn kamp i gruppespillet."
    vinner = visningsnavn_lag(hjemme if h > b else borte)
    taper = visningsnavn_lag(borte if h > b else hjemme)
    return f"{vinner} fikk en sterk start på gruppespillet, mens {taper} må jakte svar i neste runde."


def kandidattekst_liste(kandidater, maks=4):
    gode = [k for k in kandidater if k.get("score", -100) >= MIN_KVALITETSSCORE]
    if not gode:
        return []
    return [re.sub(r"\s+", " ", f"{k.get('title','')} {k.get('snippet','')}").strip() for k in gode[:maks]]


def rens_spillernavn(navn, hjemme=None, borte=None):
    if not navn:
        return ""
    navn = re.sub(r"\s+", " ", navn).strip(" .,:;–—-•⚽️⏰")

    # Fjern lagnavn/alias som noen snippets limer foran spillernavnet, f.eks. "Panama Caleb Yirenkyi".
    lag_aliaser = []
    for lag in [hjemme, borte]:
        if lag:
            lag_aliaser.extend(aliases_for(lag))
            lag_aliaser.append(sokelag_navn(lag))
            lag_aliaser.append(visningsnavn_lag(lag))
    lag_aliaser = sorted({a for a in lag_aliaser if a}, key=len, reverse=True)
    endret = True
    while endret:
        endret = False
        for alias in lag_aliaser:
            if navn.lower().startswith(alias.lower() + " "):
                navn = navn[len(alias):].strip()
                endret = True

    deler = [d for d in navn.split() if d not in NAVNEORD_STOPP]
    navn = " ".join(deler).strip()

    # Ikke returner åpenbare lagnavn/generiske treff.
    lav = navn.lower()
    if not navn or len(navn) < 3:
        return ""
    for alias in lag_aliaser:
        if lav == alias.lower():
            return ""
    if lav in ["world cup", "fifa world", "match report", "final score", "game analysis"]:
        return ""
    return navn


def finn_navn_i_tekst(pattern, tekst, hjemme=None, borte=None):
    m = re.search(pattern, tekst)
    if not m:
        return ""
    return rens_spillernavn(m.group(1), hjemme, borte)


def lag_aliaser_for_match(lag):
    """Aliasliste brukt ved enkel laggjenkjenning i kamptekst."""
    aliaser = []
    for a in aliases_for(lag):
        if a:
            aliaser.append(a)
    norsk = visningsnavn_lag(lag)
    if norsk and norsk not in aliaser:
        aliaser.append(norsk)
    # Noen vanlige skrivemåter i kilder.
    ekstra = {
        "Bosnia & Herzegovina": ["Bosnia and Herzegovina", "Bosnia-Herzegovina", "Bosnia", "Bosnia Herzegovina", "Bosnia and Herzgovina", "Bosnia Herzgovina"],
        "Czech Republic": ["Czechia", "Czech Republic", "Czechs"],
        "South Africa": ["South Africa", "Bafana Bafana"],
        "Uzbekistan": ["Uzbekistan", "Uzbeks"],
        "Colombia": ["Colombia", "Colombians", "South Americans"],
        "Switzerland": ["Switzerland", "Swiss"],
        "Ghana": ["Ghana", "Black Stars"],
        "Panama": ["Panama", "Panamanians"],
    }
    aliaser.extend(ekstra.get(lag, []))
    # Lengste først gjør regex-/kontekstsjekk mer presis.
    return sorted({a for a in aliaser if a}, key=len, reverse=True)


def lag_regex(lag):
    aliaser = lag_aliaser_for_match(lag)
    return r"(?:" + "|".join(re.escape(a) for a in aliaser) + r")"


def finn_lag_i_kontekst(kontekst, kamp):
    """Returner hjemme-/bortelag hvis konteksten tydelig peker på ett av dem."""
    if not kontekst:
        return ""
    lav = kontekst.lower()
    treff = []
    for lag in [kamp.get("hjemmelag", ""), kamp.get("bortelag", "")]:
        for alias in lag_aliaser_for_match(lag):
            if alias and re.search(r"\b" + re.escape(alias.lower()) + r"\b", lav):
                treff.append(lag)
                break
    if len(treff) == 1:
        return treff[0]
    return ""


def gjett_scorer_lag_fra_kontekst(navn, kontekst, kamp, maaltype=""):
    """
    Forsøk å knytte målscorer til lag. Returnerer tom streng hvis vi ikke er trygge.
    Dette er bevisst konservativt for å unngå feil som at en motspiller blir kreditert vinnerlaget.
    """
    if not navn:
        return ""
    hjemme = kamp.get("hjemmelag", "")
    borte = kamp.get("bortelag", "")
    h = int(kamp.get("hjemme", 0) or 0)
    b = int(kamp.get("borte", 0) or 0)
    vinner = hjemme if h > b else borte if b > h else ""
    taper = borte if h > b else hjemme if b > h else ""
    ctx = re.sub(r"\s+", " ", kontekst or " ").strip()
    lav = ctx.lower()

    # Direkte "for TEAM" / "TEAM goal" / "gave TEAM" / "earned TEAM"-mønstre.
    for lag in [hjemme, borte]:
        lr = lag_regex(lag)
        safe_patterns = [
            rf"(?i)\bfor\s+{lr}\b",
            rf"(?i)\b{lr}\s+(?:goal|goals|scorer|scorers)\b",
            rf"(?i)\b(?:gave|gives|give|earned|earns|earn|secured|secures|seal(?:ed)?|helped)\s+{lr}\b",
            rf"(?i)\b{lr}\s+(?:a|an|the)?\s*(?:victory|win|draw|point)\b",
        ]
        if any(re.search(pat, ctx) for pat in safe_patterns):
            return lag

    # Hvis det står "X scored ... to earn Ghana a 1-0 victory", peker det på vinnerlaget.
    if vinner:
        vr = lag_regex(vinner)
        if re.search(rf"(?i)\b(?:earn|earned|give|gave|secure|secured|seal|sealed)\s+{vr}\b", ctx):
            return vinner

    # Eneste mål / winner i en kamp med én scoring er normalt vinnerlaget.
    if vinner and h + b == 1 and any(x in lav for x in ["winner", "only goal", "lone goal", "match's only goal", "match’s only goal", "last-gasp", "stoppage time"]):
        return vinner

    # Uavgjort: equaliser/penalty + tydelig team i samme setning.
    if h == b and maaltype in {"penalty", "equaliser", "equalizer"}:
        lag = finn_lag_i_kontekst(ctx, kamp)
        if lag:
            return lag

    return ""


def samme_spillernavn(a, b):
    """True når to navn trolig er samme spiller, f.eks. Manzambi/Johan Manzambi."""
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return False
    return a == b or a.endswith(" " + b) or b.endswith(" " + a)


def legg_til_goal_event(events, spiller, lag, minutt="", maaltype=""):
    spiller = rens_spillernavn(spiller)
    lag = lag or ""
    minutt = str(minutt or "").strip()
    maaltype = maaltype or "goal"
    if not spiller:
        return

    # Fuzzy de-dupe: samme lag + samme minutt + kortnavn/fullt navn skal ikke telle dobbelt.
    for e in events:
        if (e.get("lag", "").lower() == lag.lower()
                and str(e.get("minutt", "")).lower() == minutt.lower()
                and e.get("type", "").lower() == maaltype.lower()
                and samme_spillernavn(e.get("spiller", ""), spiller)):
            if len(spiller) > len(e.get("spiller", "")):
                e["spiller"] = spiller
            return

    events.append({
        "spiller": spiller,
        "lag": lag,
        "minutt": minutt,
        "type": maaltype,
    })


def splitt_minutter(minutttekst):
    """Splitt '71, 90' til ['71', '90'], men behold uttrykk som '90+7'."""
    tekst = str(minutttekst or "").strip()
    if not tekst:
        return [""]
    deler = [d.strip() for d in re.split(r"\s*,\s*|\s+and\s+", tekst) if d.strip()]
    return deler or [tekst]


def navn_ascii(s):
    """Normaliser navn for sammenligning på tvers av aksenter: Díaz/Diaz."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s or "")
        if not unicodedata.combining(c)
    ).lower().strip()


def fullfor_spillernavn(kortnavn, kjente_navn):
    """Utvid f.eks. Manzambi -> Johan Manzambi hvis vi allerede kjenner fullt navn."""
    if not kortnavn:
        return kortnavn
    lav = kortnavn.lower()
    lav_ascii = navn_ascii(kortnavn)
    for kjent in kjente_navn or []:
        if not kjent:
            continue
        kjent_lav = kjent.lower()
        kjent_ascii = navn_ascii(kjent)
        if kjent_lav.endswith(" " + lav) or kjent_ascii.endswith(" " + lav_ascii) or kjent_ascii == lav_ascii:
            return kjent
    return kortnavn


def parse_goal_section(segment, lag, kamp, kjente_navn=None):
    """Parse 'Switzerland goals: Manzambi (71, 90) Vargas (84), Xhaka pen (90+7)'."""
    events = []
    if not segment:
        return events
    # Begrens segmentet slik at Jina/FIFA-navigasjon ikke spiser enorme mengder tekst.
    segment = re.split(r"(?i)\b(?:match facts|line-ups|standings|next match|fixtures|also read|related)\b", segment[:450])[0]
    # Stopp ved setningsslutt hvis det kommer mye annet innhold.
    segment = segment.split(". ")[0]
    pat = re.compile(r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})(?:\s+(pen|penalty|og|own goal))?\s*\(([^)]*?\d[^)]*)\)")
    for m in pat.finditer(segment):
        spiller = rens_spillernavn(m.group(1), kamp.get("hjemmelag"), kamp.get("bortelag"))
        spiller = fullfor_spillernavn(spiller, kjente_navn or [])
        typ = (m.group(2) or "goal").lower()
        if typ == "pen":
            typ = "penalty"
        for minutt in splitt_minutter(m.group(3).strip()):
            legg_til_goal_event(events, spiller, lag, minutt, typ)
    return events


def ekstraher_goal_events_fra_tekst(tekst, kamp, kjente_navn=None):
    """
    Trekk ut mål som strukturerte hendelser med spiller + lag når teksten gjør det trygt.
    Returnerer kun hendelser hvor lag er kjent eller sterkt utledet.
    """
    events = []
    if not tekst:
        return events
    tekst = re.sub(r"\s+", " ", tekst)
    hjemme = kamp.get("hjemmelag", "")
    borte = kamp.get("bortelag", "")

    # 1) Team-linjene fra FIFA/CONCACAF: "Switzerland goals: ... Bosnia goal: ..."
    section_hits = []
    for lag in [hjemme, borte]:
        for alias in lag_aliaser_for_match(lag):
            pattern = re.compile(rf"(?i)\b{re.escape(alias)}\s+goals?\s*:\s*")
            for m in pattern.finditer(tekst):
                section_hits.append((m.start(), m.end(), lag, alias))
    section_hits.sort(key=lambda x: x[0])
    for i, (start, end, lag, alias) in enumerate(section_hits):
        neste = section_hits[i + 1][0] if i + 1 < len(section_hits) else min(len(tekst), end + 450)
        segment = tekst[end:neste]
        for ev in parse_goal_section(segment, lag, kamp, kjente_navn=kjente_navn):
            legg_til_goal_event(events, ev["spiller"], ev["lag"], ev.get("minutt", ""), ev.get("type", "goal"))

    # 2) "Second-half goals from A and B gave Colombia ..."
    m = re.search(
        r"(?i:second-half goals from)\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,2})\s+and\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,2})\s+(?:gave|give|helped|secured|sealed)\s+([^.,;]{0,80})",
        tekst,
    )
    if m:
        # I setninger som "goals from A and B gave Colombia a 3-1 victory over Uzbekistan"
        # skal laget etter "gave" prioriteres, ikke begge lagene i konteksten.
        ctx_after = m.group(3).strip()
        lag = ""
        for kandidat_lag in [hjemme, borte]:
            for alias in lag_aliaser_for_match(kandidat_lag):
                if re.match(r"(?i)^" + re.escape(alias) + r"\b", ctx_after):
                    lag = kandidat_lag
                    break
            if lag:
                break
        if not lag:
            lag = finn_lag_i_kontekst(ctx_after, kamp)
        if lag:
            for g in [m.group(1), m.group(2)]:
                spiller = fullfor_spillernavn(rens_spillernavn(g, hjemme, borte), kjente_navn or [])
                legg_til_goal_event(events, spiller, lag, "second half", "goal")

    # 3) "A late penalty from Teboho Mokoena secured South Africa a 1-1 draw"
    for m in re.finditer(r"(?i)(?:penalty|spot-kick)\s+from\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?:secured|gave|earned|rescued|helped)\s+([^.,;]{0,80})", tekst):
        lag = finn_lag_i_kontekst(m.group(2), kamp)
        spiller = fullfor_spillernavn(rens_spillernavn(m.group(1), hjemme, borte), kjente_navn or [])
        if lag:
            legg_til_goal_event(events, spiller, lag, "", "penalty")

    # 4) "Daniel Muñoz scored the opening goal for Colombia ..."
    for m in re.finditer(r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:(?:scored|netted|struck|headed|fired|converted))\s+([^.;]{0,160})", tekst):
        spiller = fullfor_spillernavn(rens_spillernavn(m.group(1), hjemme, borte), kjente_navn or [])
        ctx = m.group(0) + " " + m.group(2)
        typ = "penalty" if re.search(r"(?i)penalty|spot-kick|from the spot", ctx) else "goal"
        lag = gjett_scorer_lag_fra_kontekst(spiller, ctx, kamp, typ)
        minutt = ""
        mm = re.search(r"(90\s*[’']?\s*\+\s*\d+|\d{1,2}(?:st|nd|rd|th)?\s+minute|\d{1,2}[’'])", ctx, flags=re.I)
        if mm:
            minutt = mm.group(1)
        if lag:
            legg_til_goal_event(events, spiller, lag, minutt, typ)

    # 5) "Luis Diaz scored one and set up another against..." + team i nærheten.
    for m in re.finditer(r"(?:brilliant\s+)?([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:scored one and set up another)", tekst):
        start = max(0, m.start() - 180)
        end = min(len(tekst), m.end() + 180)
        ctx = tekst[start:end]
        lag = finn_lag_i_kontekst(ctx, kamp)
        spiller = fullfor_spillernavn(rens_spillernavn(m.group(1), hjemme, borte), kjente_navn or [])
        if lag:
            legg_til_goal_event(events, spiller, lag, "", "goal")

    # Rydd: behold bare hendelser med sikkert lag og unngå dubletter på spiller/lag/minutt.
    ryddet = []
    sett = set()
    for ev in events:
        spiller = ev.get("spiller", "")
        lag = ev.get("lag", "")
        if not spiller or not lag:
            continue
        key = (spiller.lower(), lag.lower(), ev.get("minutt", "").lower())
        if key in sett:
            continue
        sett.add(key)
        ryddet.append(ev)
    return ryddet[:8]


def spillere_for_lag(goal_events, lag):
    navn = []
    for ev in goal_events or []:
        spiller = ev.get("spiller")
        if ev.get("lag") != lag or not spiller:
            continue
        # Unngå både "Johan Manzambi" og "Manzambi" i samme liste.
        if any(n.lower() == spiller.lower() or n.lower().endswith(" " + spiller.lower()) or spiller.lower().endswith(" " + n.lower()) for n in navn):
            # Behold det lengste navnet.
            for i, n in enumerate(list(navn)):
                if spiller.lower().endswith(" " + n.lower()) and len(spiller) > len(n):
                    navn[i] = spiller
            continue
        navn.append(spiller)
    return navn


def event_minutt_til_norsk(ev):
    minutt = (ev or {}).get("minutt", "") or ""
    ml = minutt.lower()
    if "90" in ml and "+" in ml:
        return "på overtid"
    if "second half" in ml:
        return "etter pause"
    if re.search(r"\b(6|6th|sixth)\b", ml):
        return "tidlig i kampen"
    if minutt:
        return f"i {minutt}" if "minute" not in ml else minutt
    return ""


def join_navn(navn):
    navn = [n for n in navn if n]
    if not navn:
        return ""
    if len(navn) == 1:
        return navn[0]
    if len(navn) == 2:
        return f"{navn[0]} og {navn[1]}"
    return f"{', '.join(navn[:-1])} og {navn[-1]}"


def antall_maal_per_spiller(goal_events, lag=""):
    """Tell mål per spiller, med enkel sammenslåing av kortnavn/fullt navn."""
    teller = []
    for ev in goal_events or []:
        if lag and ev.get("lag") != lag:
            continue
        spiller = ev.get("spiller", "")
        if not spiller:
            continue
        funnet = False
        for item in teller:
            navn = item["spiller"]
            samme = (
                navn.lower() == spiller.lower()
                or navn.lower().endswith(" " + spiller.lower())
                or spiller.lower().endswith(" " + navn.lower())
            )
            if samme:
                item["antall"] += 1
                if len(spiller) > len(navn):
                    item["spiller"] = spiller
                funnet = True
                break
        if not funnet:
            teller.append({"spiller": spiller, "antall": 1})
    return teller


def referat_fakta_score(detaljer):
    """
    Poengscore for hvor rikt fulltekstgrunnlaget er.
    Serper/snippets skal ikke avgjøre om referatet er OK; dette måles på ekstraherte fakta.
    """
    if not isinstance(detaljer, dict):
        return 0

    score = 0
    source_type = detaljer.get("source_type") or detaljer.get("fulltekst_source_type") or "unknown"

    if source_type == "article_report":
        score += 2

    if detaljer.get("goal_events"):
        score += 4

    if detaljer.get("scorere"):
        score += 2

    if detaljer.get("hat_trick_player") or detaljer.get("brace_scorer"):
        score += 2

    if int(detaljer.get("red_cards", 0) or 0) >= 1:
        score += 2

    for felt in [
        "first_world_cup_win", "knockout_secured", "keeper_error", "group_top",
        "home_crowd", "goalless_first_half", "second_half_goals", "late", "stoppage",
        "winner", "penalty", "equaliser", "nine_men", "early_lead",
    ]:
        if detaljer.get(felt):
            score += 1

    return score


def referat_fakta_mangler(detaljer):
    """Kort debugliste for hvorfor fulltekstgrunnlaget ikke regnes som rikt nok."""
    mangler = []
    if not isinstance(detaljer, dict):
        return ["fulltekst_fakta"]

    source_type = detaljer.get("source_type") or detaljer.get("fulltekst_source_type") or "unknown"
    if detaljer.get("status") != "ok" and not detaljer.get("fulltekst_status") == "ok":
        mangler.append("fulltekst_status")
    if source_type != "article_report":
        mangler.append("article_report")
    if not detaljer.get("goal_events"):
        mangler.append("goal_events")
    if not any(detaljer.get(f) for f in ["knockout_secured", "first_world_cup_win", "keeper_error", "group_top", "red_cards", "nine_men", "goalless_first_half", "second_half_goals", "late", "stoppage"]):
        mangler.append("kampforlop")
    if referat_fakta_score(detaljer) < 5:
        mangler.append("referat_fakta_score")
    return mangler[:6]


def har_rikt_fulltekstgrunnlag(detaljer):
    """
    True bare når fulltekstfakta er bredt nok til et ordentlig kampreferat.
    Brace/scorer alene skal ikke stoppe refresh.
    """
    if not isinstance(detaljer, dict) or detaljer.get("status") != "ok":
        return False

    if detaljer.get("fulltekst_riktig_kamp") is False:
        return False

    source_type = detaljer.get("source_type") or detaljer.get("fulltekst_source_type") or "unknown"
    if source_type in {"preview", "wrong_match"}:
        return False

    score = referat_fakta_score(detaljer)

    # Ekte kampartikkel trenger minst middels faktabredde.
    if source_type == "article_report":
        return score >= 5

    # Match-details/andre sider kan brukes som nødløsning i teksten, men skal ikke
    # regnes som ferdig beriket med mindre faktaene er klart rikere enn kun scorer/brace.
    return bool(detaljer.get("goal_events")) and score >= 7


def har_kampnaere_detaljer(detaljer):
    """Bakoverkompatibel wrapper. Bruk den strammere fulltekstvurderingen."""
    return har_rikt_fulltekstgrunnlag(detaljer)


def skal_legge_til_historikk(ref, detaljer):
    """Historikk brukes bare når kampdataene er for tynne, ikke som standard avsnitt."""
    if not HISTORIKK_SOM_FALLBACK:
        return False
    if not ref:
        return False
    return not har_kampnaere_detaljer(detaljer)


# ── FULLTEKST / KILDEFAKTA ───────────────────────────────────────────────────

def domene_matcher(domene, needle):
    """True for exact domain or subdomain, e.g. www.concacaf.com -> concacaf.com."""
    domene = (domene or "").lower().strip()
    needle = (needle or "").lower().strip()
    return domene == needle or domene.endswith("." + needle)


def domene_i_liste(domene, liste):
    return any(domene_matcher(domene, d) or d in domene for d in liste)


def fulltekst_kandidat_matcher_kamp(k, kamp):
    """Fulltekst-kandidater må peke på riktig kamp: begge lag må være representert."""
    tekst = normaliser_tekst(tekst_for_kandidat(k))
    hjemme = kamp.get("hjemmelag", "")
    borte = kamp.get("bortelag", "")
    return inneholder_alias(tekst, hjemme) and inneholder_alias(tekst, borte)


def klassifiser_fulltekst_kandidat(k, kamp=None):
    """
    Klassifiser URL-en før fulltekstforsøk.
    Dette gjør at ekte kampartikler prioriteres over match-details og preview-stoff.
    """
    tekst = normaliser_tekst(tekst_for_kandidat(k))
    link = (k.get("link", "") or "").lower()
    domene = domene_fra_url(link)

    if kamp is not None and not fulltekst_kandidat_matcher_kamp(k, kamp):
        return "wrong_match"

    preview_ord = [
        "preview", "seek to", "will face", "set to", "ahead of", "how to watch",
        "where to watch", "winner of", "will clinch", "will secure", "look to",
        "bid to", "aim to", "kick-off", "kickoff",
    ]
    if any(x in tekst for x in preview_ord):
        return "preview"

    if "/matches/" in link or "full match details" in tekst or "lineups" in tekst or "match updates" in tekst:
        return "match_details"

    postmatch_ord = [
        "match report", "game analysis", "report and highlights", "full-time",
        "full time", "defeated", "beat", "beats", "won", "rout", "crush",
        "secure", "secured", "confirm", "confirmed", "advanced", "advance",
    ]
    if "/articles/" in link and domene_matcher(domene, "fifa.com"):
        return "article_report"
    if "/news/" in link and any(x in tekst for x in postmatch_ord):
        return "article_report"
    if any(x in tekst for x in postmatch_ord):
        return "article_report"

    return "other"


def fulltekst_kildeprioritet(k, kamp=None):
    """Ranger kandidater etter domene + artikkeltype. FIFA-artikkel skal vinne."""
    domene = domene_fra_url(k.get("link", ""))
    if not domene:
        return -999
    if domene_i_liste(domene, BLOKKERTE_DOMENER):
        return -999
    if domene_i_liste(domene, FULLTEKST_UTELUKKEDE_DOMENER):
        return -999

    source_type = klassifiser_fulltekst_kandidat(k, kamp) if kamp is not None else "unknown"
    if source_type in {"wrong_match", "preview"}:
        return -999

    domene_score = -999
    for needle, score in FULLTEKST_PRIORITET:
        if domene_matcher(domene, needle):
            domene_score = score
            break
    if domene_score <= 0:
        return -999

    type_bonus = {
        "article_report": 60,
        "other": 0,
        "match_details": -35,
    }.get(source_type, 0)
    kandidat_score = max(-20, min(20, int(k.get("score", 0))))
    return domene_score + type_bonus + kandidat_score


def fulltekst_egnet_kandidater(kandidater, kamp=None):
    valgte = []
    debug = []
    for k in kandidater or []:
        domene = domene_fra_url(k.get("link", "")) or "ukjent"
        score = int(k.get("score", -100))
        source_type = klassifiser_fulltekst_kandidat(k, kamp) if kamp is not None else "unknown"
        prio = fulltekst_kildeprioritet(k, kamp=kamp)
        if prio <= 0:
            debug.append(f"{domene}:{score}:{source_type}:droppet")
            continue

        # Fulltekst-score etter henting avgjør endelig kvalitet, men kandidat må være riktig kamp.
        mild_offisiell = prio >= 50 and kamp is not None and fulltekst_kandidat_matcher_kamp(k, kamp)
        if score < MIN_KVALITETSSCORE and not mild_offisiell:
            debug.append(f"{domene}:{score}:{source_type}:lav_score")
            continue

        kk = dict(k)
        kk["fulltekst_prioritet"] = prio
        kk["source_type"] = source_type
        valgte.append(kk)
        debug.append(f"{domene}:{score}:{source_type}:egnet")

    valgte.sort(key=lambda x: (x.get("fulltekst_prioritet", 0), x.get("score", 0)), reverse=True)
    return valgte[:8], debug[:12]


# ── OFFISIELL FULLTEKST-KILDEDISCOVERY ───────────────────────────────────────

CONCACAF_LAG = {
    "Panama", "USA", "United States", "Mexico", "Canada", "Haiti", "Curaçao", "Curacao",
    "Qatar"  # beholdes ikke som CONCACAF, men skader ikke om FIFA/CONCACAF ikke treffer
}


def rens_soketerm_lag(lag):
    """Gjør lagnavn trygge i Serper-query uten anførselstegn/OR."""
    lag = (lag or "").replace("&", " ").replace("-", " ")
    lag = re.sub(r"[^A-Za-z0-9À-ÖØ-öø-ÿ ]+", " ", lag)
    return re.sub(r"\s+", " ", lag).strip()


def fulltekst_lookup_domener_for_kamp(kamp):
    """
    Domener som kan være verdt et separat kildesøk når vanlig Serper-cache
    ikke inneholder fulltekst-egnede kilder. CONCACAF prioriteres kun når ett
    av lagene realistisk kan dekkes der.
    """
    hjemme = kamp.get("hjemmelag", "")
    borte = kamp.get("bortelag", "")
    # FIFA først: de vellykkede referatene kom fra FIFA-artikler.
    domener = ["fifa.com"]
    if hjemme in CONCACAF_LAG or borte in CONCACAF_LAG:
        domener.append("concacaf.com")
    # Noen kilder fungerer bedre som fulltekst enn Reuters/ESPN.
    domener.extend(["apnews.com", "bbc.com", "skysports.com", "theguardian.com"])

    # Behold rekkefølge, fjern dubletter.
    unike = []
    for d in domener:
        if d not in unike:
            unike.append(d)
    return unike


KILDE_QUERY_NAVN = {
    "concacaf.com": "CONCACAF",
    "fifa.com": "FIFA",
    "apnews.com": "AP News",
    "bbc.com": "BBC Sport",
    "skysports.com": "Sky Sports",
    "theguardian.com": "Guardian",
}


def bygg_offisiell_kilde_query(kamp, domene):
    """
    Serper free-kontoer kan avvise avanserte operatorer som site:.
    Derfor brukes domenet/kilden som vanlig søkeord, og resultatene filtreres
    etterpå på faktisk domene.
    """
    hjemme = rens_soketerm_lag(kamp.get("hjemmelag", ""))
    borte = rens_soketerm_lag(kamp.get("bortelag", ""))
    h = kamp.get("hjemme")
    b = kamp.get("borte")
    kilde = KILDE_QUERY_NAVN.get(domene, domene.replace(".com", ""))
    return (
        f"{kilde} {domene} {hjemme} {borte} {h}-{b} "
        f"World Cup 2026 match report recap goal scorer full time"
    )


def suppler_med_offisielle_fulltekst_kilder(kamp, kandidater, cache, cache_entry, serper_teller):
    """
    Hvis vanlig Serper-cache ikke inneholder en egnet fulltekst-kilde, gjør et
    svært begrenset kildesøk mot prioriterte domener, f.eks. CONCACAF først
    for kamper med CONCACAF-lag. Dette er eneste måten scriptet kan finne
    CONCACAF når den ikke ligger i cached kandidatlisten.
    """
    if not OFFISIELL_KILDE_SOK_AKTIVERT:
        return kandidater, cache_entry, serper_teller

    egnet, _ = fulltekst_egnet_kandidater(kandidater, kamp=kamp)
    if any(k.get("source_type") == "article_report" for k in egnet):
        return kandidater, cache_entry, serper_teller
    if egnet:
        print("  → Fulltekst-egnet kilde finnes, men ingen kampartikkel. Søker prioritert artikkelkilde først.")

    key = cache_noekkel(kamp["kamp_id"], kamp["hjemme"], kamp["borte"])
    entry = cache_entry or cache.get(key) or {}
    if entry.get("offisiell_kilde_sok_versjon") == OFFISIELL_KILDE_SOK_VERSION:
        return kandidater, entry, serper_teller

    if serper_teller >= MAX_SERPER_SOK_TOTALT_KJORING:
        print(f"  → Offisiell kilde-søk hoppet over: Serper totalgrense nådd ({MAX_SERPER_SOK_TOTALT_KJORING}).")
        return kandidater, entry, serper_teller

    domener = fulltekst_lookup_domener_for_kamp(kamp)
    print("  → Ingen fulltekst-egnet kilde i cache. Søker prioriterte kilder: " + ", ".join(domener[:MAX_OFFISIELLE_KILDESOK_KAMP]))

    nye = []
    sokt = []
    for domene in domener[:max(0, MAX_OFFISIELLE_KILDESOK_KAMP)]:
        if serper_teller >= MAX_SERPER_SOK_TOTALT_KJORING:
            break
        query = bygg_offisiell_kilde_query(kamp, domene)
        print(f"    Offisiell kilde-query: {query}")
        funn = soek_serper_api(query)
        serper_teller += 1
        sokt.append(domene)
        # Uten site:-operator må vi filtrere hardt på faktisk domene etterpå.
        domene_funn = []
        andre_funn = 0
        for k in funn:
            faktisk_domene = domene_fra_url(k.get("link", ""))
            if not domene_matcher(faktisk_domene, domene):
                andre_funn += 1
                continue
            score_kandidat(k, kamp["hjemmelag"], kamp["bortelag"], kamp["hjemme"], kamp["borte"])
            k["offisiell_kilde_sok"] = domene
            domene_funn.append(k)
        if andre_funn:
            print(f"    → Filtrerte bort {andre_funn} treff utenfor {domene}")
        if domene_funn:
            print(f"    → Beholder {len(domene_funn)} treff fra {domene}")
        nye.extend(domene_funn)

        # Stopp tidlig bare hvis vi fant en ekte kampartikkel.
        # Match-details er nyttig nødgrunnlag, men vi søker videre etter artikkel innenfor kvoten.
        kombi_tmp = dedupliser_kandidater((kandidater or []) + nye)
        egnet_tmp, _ = fulltekst_egnet_kandidater(kombi_tmp, kamp=kamp)
        if any(k.get("source_type") == "article_report" for k in egnet_tmp):
            print(f"    → Fant kampartikkel via {domene}")
            break
        elif egnet_tmp:
            print(f"    → Fant bare match/details-kilde via {domene}. Søker videre etter kampartikkel hvis kvoten tillater det.")

    kombi = dedupliser_kandidater((kandidater or []) + nye)
    for k in kombi:
        if "score" not in k:
            score_kandidat(k, kamp["hjemmelag"], kamp["bortelag"], kamp["hjemme"], kamp["borte"])
    kombi.sort(key=lambda x: x.get("score", -999), reverse=True)

    entry["kandidater"] = kombi[:15]
    entry["offisiell_kilde_sok_versjon"] = OFFISIELL_KILDE_SOK_VERSION
    entry["offisiell_kilde_sokt"] = iso_utc_na()
    entry["offisiell_kilde_domener"] = sokt
    cache[key] = entry
    skriv_serper_cache(cache)
    return kombi, entry, serper_teller


def hent_url_tekst_direkte(url):
    """Hent HTML/tekst direkte fra en kilde. Returnerer tekst, eller tom streng ved feil."""
    if not url:
        return ""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; RambergVMBot/1.0; +https://rambergapps.github.io/vm2026/)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/markdown;q=0.8,*/*;q=0.7",
            "Accept-Language": "en-US,en;q=0.9,nb;q=0.7",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=FULLTEKST_TIMEOUT) as resp:
            raw = resp.read(FULLTEKST_MAX_BYTES)
            charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except Exception as e:
        print(f"    Fulltekst: klarte ikke hente {domene_fra_url(url)} direkte ({e})")
        return ""


def jina_reader_url(url):
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return "https://r.jina.ai/" + url
    return ""


def hent_url_tekst(url):
    """
    Hent fulltekstgrunnlag. For tillatte kilder prøver vi Jina Reader først,
    siden den ofte gir ren Markdown/tekst fra sider som ellers er tunge å parse.
    Direkte henting brukes som fallback. Reuters/ESPN filtreres før denne funksjonen.
    """
    if not url:
        return ""

    if JINA_READER_AKTIVERT:
        reader = jina_reader_url(url)
        if reader:
            tekst = hent_url_tekst_direkte(reader)
            if tekst and len(tekst) > 250:
                print(f"    Fulltekst: Jina Reader OK for {domene_fra_url(url)} ({len(tekst)} tegn)")
                return tekst
            print(f"    Fulltekst: Jina Reader ga ikke nok tekst for {domene_fra_url(url)}")

    return hent_url_tekst_direkte(url)


def hent_jsonld_verdier(obj):
    """Finn headline/description/articleBody i JSON-LD uten å anta eksakt struktur."""
    verdier = []
    if isinstance(obj, dict):
        for key in ["headline", "description", "articleBody"]:
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                verdier.append(val.strip())
        for val in obj.values():
            verdier.extend(hent_jsonld_verdier(val))
    elif isinstance(obj, list):
        for item in obj:
            verdier.extend(hent_jsonld_verdier(item))
    return verdier


def ekstraher_artikkeltekst_fra_html(html_text):
    """Trekk ut mest mulig ren tekst fra HTML/JSON-LD/meta/Markdown. Lagrer ikke råtekst permanent."""
    if not html_text:
        return ""

    # Jina Reader returnerer ofte ren Markdown/tekst, ikke HTML. Da bruker vi den direkte.
    if "<html" not in html_text.lower() and "<script" not in html_text.lower() and len(html_text) > 250:
        plain = html.unescape(html_text)
        plain = re.sub(r'\s+', ' ', plain).strip()
        return plain[:50000]

    deler = []

    # JSON-LD gir ofte ren artikkeltekst for Reuters/AP/BBC-lignende sider.
    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html_text, flags=re.I | re.S):
        try:
            data = json.loads(html.unescape(block.strip()))
            deler.extend(hent_jsonld_verdier(data))
        except Exception:
            continue

    # Meta description er ofte en kort, presis oppsummering.
    meta_patterns = [
        r'<meta[^>]+(?:name|property)=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pat in meta_patterns:
        for m in re.findall(pat, html_text, flags=re.I | re.S):
            deler.append(html.unescape(m.strip()))

    # Grov HTML→tekst fallback. Vi bruker dette kun til faktaekstraksjon, ikke publisering.
    cleaned = re.sub(r'<script\b[^>]*>.*?</script>', ' ', html_text, flags=re.I | re.S)
    cleaned = re.sub(r'<style\b[^>]*>.*?</style>', ' ', cleaned, flags=re.I | re.S)
    cleaned = re.sub(r'<noscript\b[^>]*>.*?</noscript>', ' ', cleaned, flags=re.I | re.S)
    cleaned = re.sub(r'<[^>]+>', ' ', cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if cleaned:
        deler.append(cleaned[:30000])

    tekst = re.sub(r'\s+', ' ', ' '.join(deler)).strip()
    return tekst[:50000]


def resultat_i_tekst(tekst_lower, h, b):
    varianter = [
        f"{h}-{b}", f"{h}–{b}", f"{h} - {b}", f"{h} to {b}",
        f"{b}-{h}", f"{b}–{h}", f"{b} - {h}", f"{b} to {h}",
    ]
    return any(v in tekst_lower for v in varianter)




def fulltekst_matcher_riktig_kamp(tekst, kamp):
    """Sjekk etter henting: fullteksten må faktisk omtale begge lagene."""
    lav = (tekst or "").lower()
    return inneholder_alias(lav, kamp.get("hjemmelag", "")) and inneholder_alias(lav, kamp.get("bortelag", ""))

def score_fulltekst(tekst, kamp):
    if not tekst or len(tekst) < 250:
        return -100
    lav = tekst.lower()
    if not fulltekst_matcher_riktig_kamp(tekst, kamp):
        return -100
    score = 0
    if inneholder_alias(lav, kamp["hjemmelag"]):
        score += 4
    if inneholder_alias(lav, kamp["bortelag"]):
        score += 4
    if resultat_i_tekst(lav, kamp["hjemme"], kamp["borte"]):
        score += 4
    for ord_ in FAKTA_ORD:
        if ord_ in lav:
            score += 1
    # Lange tekster som faktisk nevner kampen gir bedre grunnlag, men ikke la lengde dominere.
    if len(tekst) > 1200:
        score += 2
    if len(tekst) > 2500:
        score += 2
    for ord_ in NEGATIVE_ORD:
        if ord_ in lav:
            score -= 1
    return score


def ekstraher_fakta_fra_fulltekst(tekst, kamp, kilde_url, source_type="unknown", kilde_tittel=""):
    """Ekstraher korte, strukturerte fakta fra fullside. Ikke lagre full artikkeltekst."""
    hjemme = kamp["hjemmelag"]
    borte = kamp["bortelag"]
    lav = tekst.lower()

    fakta = {
        "status": "ok",
        "versjon": FULLTEKST_CACHE_VERSION,
        "kilde_url": kilde_url,
        "kilde_tittel": kilde_tittel,
        "domene": domene_fra_url(kilde_url),
        "source_type": source_type,
        "fulltekst_riktig_kamp": fulltekst_matcher_riktig_kamp(tekst, kamp),
        "score": score_fulltekst(tekst, kamp),
        "hentet": iso_utc_na(),
        "late": any(x in lav for x in ["stoppage time", "last-gasp", "deep into stoppage", "late", "90+", "90'+", "seven minutes from fulltime", "seven minutes from full-time"]),
        "stoppage": any(x in lav for x in ["stoppage time", "last-gasp", "deep into stoppage", "90+", "90'+", "95th-minute", "90’+5", "90'+5"]),
        "winner": "winner" in lav or "lone goal" in lav or "match's only goal" in lav or "match’s only goal" in lav,
        "penalty": "penalty" in lav or "from the spot" in lav or "spot-kick" in lav,
        "equaliser": any(x in lav for x in ["equaliser", "equalizer", "equalised", "equalized", "salvaged a point", "salvage draw", "rescues a draw", "fought back to draw"]),
        "goalless_first_half": "goalless first half" in lav or "goalless at halftime" in lav or "goalless at half-time" in lav,
        "second_half_goals": "second-half goals" in lav or "second half goals" in lav,
        "second_half_scorers": [],
        "substitutes": "substitutes" in lav or "substitute" in lav or "came off the bench" in lav,
        "debutants": "debutants" in lav or "debutant" in lav,
        "scorere": [],
        "goal_events": [],
        "penalty_scorer": "",
        "lead_scorer": "",
        "brace_scorer": "",
        "sub_names": [],
        "minute_text": "",
        "lead_minute_text": "",
        "early_lead": False,
        "record_pass_player": "",
        "record_passes": "",
        "star_player": "",
        "scored_and_assisted": False,
        "hat_trick_player": "",
        "red_cards": 0,
        "nine_men": False,
        "first_world_cup_win": False,
        "knockout_secured": False,
        "group_top": False,
        "home_crowd": False,
        "keeper_error": False,
    }

    # Strukturerte mål med spiller + lag. Dette brukes primært i norsk recap,
    # fordi rene scorerlister kan blande spillere fra begge lag.
    fakta["goal_events"] = ekstraher_goal_events_fra_tekst(tekst, kamp)


    # Minutt/tilleggstid.
    if re.search(r"90\s*[’']?\s*\+\s*5|90\+5|95th-minute|fifth minute of second-half stoppage", lav):
        fakta["minute_text"] = "på overtid"
    elif "seven minutes from fulltime" in lav or "seven minutes from full-time" in lav:
        fakta["minute_text"] = "sju minutter før slutt"
    elif re.search(r"83(?:rd)? minute|83[’']", lav):
        fakta["minute_text"] = "mot slutten av kampen"
    elif re.search(r"sixth minute|6th minute|after six minutes|in the sixth", lav):
        fakta["lead_minute_text"] = "tidlig i kampen"
        fakta["early_lead"] = True

    if re.search(r"opening goal|early lead|took early control|took early lead", lav):
        fakta["early_lead"] = True

    m_record = re.search(r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:completed)\s+(\d{2,3})\s+(?i:passes)", tekst)
    if m_record:
        fakta["record_pass_player"] = rens_spillernavn(m_record.group(1), hjemme, borte)
        fakta["record_passes"] = m_record.group(2)

    m_star = re.search(r"(?i:brilliant)\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:scored one and set up another)", tekst)
    if m_star:
        fakta["star_player"] = rens_spillernavn(m_star.group(1), hjemme, borte)
        fakta["scored_and_assisted"] = True

    m_hat = re.search(r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:scored\s+(?:a\s+)?hat[- ]trick|hat[- ]trick)", tekst)
    if not m_hat:
        m_hat = re.search(r"(?i:hat[- ]trick\s+(?:from|by|for)\s+)([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})", tekst)
    if m_hat:
        fakta["hat_trick_player"] = rens_spillernavn(m_hat.group(1), hjemme, borte)

    red_card_count = len(re.findall(r"(?i)red cards?|sent off|down to nine men|nine-man", tekst))
    fakta["red_cards"] = red_card_count
    fakta["nine_men"] = "nine-man" in lav or "nine men" in lav or "down to nine" in lav
    fakta["first_world_cup_win"] = any(x in lav for x in ["first world cup win", "first-ever world cup win", "first world cup finals win", "first world cup match"] )
    fakta["knockout_secured"] = any(x in lav for x in ["secure a spot in the knockout", "secured a spot in the knockout", "confirm their qualification", "qualification into the round", "first spot in the round", "advance to the knockout", "first team to reach"])
    fakta["group_top"] = any(x in lav for x in ["top of group", "lead group", "top world cup group", "lead group b", "lead group a"])
    fakta["home_crowd"] = any(x in lav for x in ["home crowd", "raucous home crowd", "in vancouver", "co-hosts"])
    fakta["keeper_error"] = any(x in lav for x in ["keeper error", "goalkeeper error", "serious korean goalkeeper error"])

    # Penalty scorer.
    p_patterns = [
        r"(?i:(?:penalty|spot-kick)\s+from)\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})",
        r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:(?:converted|scored|netted|levelled|leveled).*?(?:penalty|spot))",
    ]
    for pat in p_patterns:
        navn = finn_navn_i_tekst(pat, tekst, hjemme, borte)
        if navn:
            fakta["penalty_scorer"] = navn
            break

    # Ledelsesmål / tidlig mål.
    lead_patterns = [
        r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:(?:sent|fired|put|gave).*?(?:lead|ahead))",
        r"(?i:(?:early lead through|lead through))\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})",
        r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:scored the opening goal)",
    ]
    for pat in lead_patterns:
        navn = finn_navn_i_tekst(pat, tekst, hjemme, borte)
        if navn:
            fakta["lead_scorer"] = navn
            break

    # Scored twice / brace.
    brace_patterns = [
        r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:(?:scored twice|netted twice|bagged a brace|scored a brace))",
        r"(?i:brace from)\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})",
    ]
    for pat in brace_patterns:
        navn = finn_navn_i_tekst(pat, tekst, hjemme, borte)
        if navn:
            fakta["brace_scorer"] = navn
            fakta["scorere"].append(navn)
            break

    # Second-half goals from A and B gave/sealed/helped TEAM...
    # Lagre disse separat slik at vi ikke senere bruker åpningmålscorer
    # (f.eks. Daniel Muñoz) som en av "etter pause"-målscorerne.
    m = re.search(
        r"(?i:second-half goals from)\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,2})\s+and\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,2})\s+(?:gave|give|helped|secured|sealed)\s+([^.,;]{0,80})",
        tekst,
    )
    if m:
        ctx_after = m.group(3).strip()
        lag = ""
        for kandidat_lag in [hjemme, borte]:
            for alias in lag_aliaser_for_match(kandidat_lag):
                if re.match(r"(?i)^" + re.escape(alias) + r"\b", ctx_after):
                    lag = kandidat_lag
                    break
            if lag:
                break
        if not lag:
            lag = finn_lag_i_kontekst(ctx_after, kamp)
        n1 = rens_spillernavn(m.group(1), hjemme, borte)
        n2 = rens_spillernavn(m.group(2), hjemme, borte)
        navnene = [n for n in [n1, n2] if n]
        fakta["scorere"].extend(navnene)
        if lag and (not (hjemme and borte) or lag in [hjemme, borte]):
            fakta["second_half_scorers"] = navnene
        fakta["second_half_goals"] = True

    # Generelle scorer-mønstre.
    score_patterns = [
        r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:(?:scored|netted|struck).*?(?:winner|lone goal|only goal|goal))",
        r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:(?:levelled|leveled|equalised|equalized|converted|fired|headed))",
    ]
    for pat in score_patterns:
        navn = finn_navn_i_tekst(pat, tekst, hjemme, borte)
        if navn:
            fakta["scorere"].append(navn)

    # Substitutes A and B.
    m = re.search(
        r"(?i:substitutes?)\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,2})\s+and\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,2})",
        tekst,
    )
    if m:
        fakta["sub_names"] = [
            rens_spillernavn(m.group(1), hjemme, borte),
            rens_spillernavn(m.group(2), hjemme, borte),
        ]
        fakta["sub_names"] = [n for n in fakta["sub_names"] if n]

    # Rydd navnelister.
    for felt in ["scorere", "sub_names", "second_half_scorers"]:
        unike = []
        for n in fakta.get(felt, []):
            if n and n not in unike:
                unike.append(n)
        fakta[felt] = unike[:4]

    if fakta["penalty_scorer"] and fakta["penalty_scorer"] not in fakta["scorere"]:
        fakta["scorere"].insert(0, fakta["penalty_scorer"])
    if fakta["lead_scorer"] and fakta["lead_scorer"] not in fakta["scorere"]:
        fakta["scorere"].append(fakta["lead_scorer"])

    # Kjør mål-event-ekstraksjon én gang til med kjente fulle navn, slik at
    # korte FIFA-linjer som "Manzambi (71, 90)" kan bli "Johan Manzambi".
    kjente_navn = (
        list(fakta.get("sub_names", []) or [])
        + list(fakta.get("scorere", []) or [])
        + list(fakta.get("second_half_scorers", []) or [])
    )
    ekstra_events = ekstraher_goal_events_fra_tekst(tekst, kamp, kjente_navn=kjente_navn)
    for ev in ekstra_events:
        legg_til_goal_event(
            fakta["goal_events"],
            ev.get("spiller", ""),
            ev.get("lag", ""),
            ev.get("minutt", ""),
            ev.get("type", "goal"),
        )

    fakta["referat_fakta_score"] = referat_fakta_score(fakta)
    fakta["mangler"] = referat_fakta_mangler(fakta)
    fakta["rikt_fulltekstgrunnlag"] = har_rikt_fulltekstgrunnlag(fakta)

    return fakta


def hent_fulltekst_fakta_for_kamp(kamp, kandidater, maks_forsok=5):
    """Prøv prioriterte kilder og returner ekstraherte fakta, ikke rå artikkeltekst."""
    egnet, debug = fulltekst_egnet_kandidater(kandidater, kamp=kamp)
    if debug:
        print("  → Fulltekst-kandidater: " + "; ".join(debug))
    if not egnet:
        print("  → Fulltekst: ingen egnede kilder etter blokkering/klassifisering")
        return {"status": "ikke_funnet", "versjon": FULLTEKST_CACHE_VERSION, "hentet": iso_utc_na(), "score": -100, "referat_fakta_score": 0, "mangler": ["fulltekst_kilde"], "_forsok_url": 0}

    forsok_url = 0
    beste_tynne_fakta = None

    for k in egnet[:max(0, maks_forsok)]:
        url = k.get("link", "")
        domene = domene_fra_url(url)
        source_type = k.get("source_type") or klassifiser_fulltekst_kandidat(k, kamp)
        forsok_url += 1
        print(f"  → Fulltekst: prøver {domene} ({source_type})")
        html_text = hent_url_tekst(url)
        tekst = ekstraher_artikkeltekst_fra_html(html_text)
        score = score_fulltekst(tekst, kamp)
        if score >= FULLTEKST_MIN_SCORE:
            fakta = ekstraher_fakta_fra_fulltekst(
                tekst,
                kamp,
                url,
                source_type=source_type,
                kilde_tittel=k.get("title", ""),
            )
            fakta["score"] = score
            fakta["versjon"] = FULLTEKST_CACHE_VERSION
            fakta["_forsok_url"] = forsok_url
            fakta["referat_fakta_score"] = referat_fakta_score(fakta)
            fakta["mangler"] = referat_fakta_mangler(fakta)
            fakta["rikt_fulltekstgrunnlag"] = har_rikt_fulltekstgrunnlag(fakta)
            if har_rikt_fulltekstgrunnlag(fakta):
                print(f"  → Fulltekst: {domene} OK, score {score}, fakta_score {fakta['referat_fakta_score']}")
                return fakta
            print(f"    Fulltekst: {domene} relevant, men tynt grunnlag (score {score}, fakta_score {fakta['referat_fakta_score']}, mangler: {', '.join(fakta.get('mangler', []))})")
            if beste_tynne_fakta is None or referat_fakta_score(fakta) > referat_fakta_score(beste_tynne_fakta):
                beste_tynne_fakta = fakta
        else:
            print(f"    Fulltekst: {domene} lav score ({score})")

    if beste_tynne_fakta:
        beste_tynne_fakta["status"] = "ok"
        beste_tynne_fakta["_forsok_url"] = forsok_url
        return beste_tynne_fakta

    return {"status": "ikke_funnet", "versjon": FULLTEKST_CACHE_VERSION, "hentet": iso_utc_na(), "score": -100, "referat_fakta_score": 0, "mangler": ["fulltekst_kilde"], "_forsok_url": forsok_url}


def oppdater_fulltekst_i_cache(kamp, kandidater, cache, cache_entry, fulltekst_teller):
    """
    Sørg for at cache har fulltekst_fakta hvis mulig. Henter bare én gang per kamp/resultat
    og lagrer kun ekstraherte fakta, ikke artikkeltekst.
    """
    if not FULLTEKST_AKTIVERT:
        return cache_entry, fulltekst_teller

    key = cache_noekkel(kamp["kamp_id"], kamp["hjemme"], kamp["borte"])
    entry = cache_entry or cache.get(key) or {}
    eksisterende = entry.get("fulltekst_fakta")
    if (
        isinstance(eksisterende, dict)
        and eksisterende.get("versjon") == FULLTEKST_CACHE_VERSION
        and eksisterende.get("status") in ["ok", "ikke_funnet", "feilet"]
    ):
        if eksisterende.get("status") == "ok" and har_rikt_fulltekstgrunnlag(eksisterende):
            print(f"  → Fulltekst cache hit: {eksisterende.get('domene', 'ukjent')} {eksisterende.get('source_type', 'ukjent')} score {eksisterende.get('score', -100)} fakta_score {eksisterende.get('referat_fakta_score', referat_fakta_score(eksisterende))}")
            return entry, fulltekst_teller
        elif eksisterende.get("status") == "ok":
            print(f"  → Fulltekst cache OK, men grunnlaget er tynt ({eksisterende.get('domene', 'ukjent')} {eksisterende.get('source_type', 'ukjent')}, fakta_score {eksisterende.get('referat_fakta_score', referat_fakta_score(eksisterende))}). Prøver bedre kilde.")

        # Ikke lås negative/tynne fulltekst-resultater for ferske kamper.
        minutter = minutter_siden_iso(eksisterende.get("hentet"))
        retry_antall = int(eksisterende.get("retry_antall", 0) or 0)
        if (
            minutter is not None
            and minutter <= FERSK_KAMP_REFRESH_TIMER * 60
            and minutter >= MIN_MINUTTER_MELLOM_OK_REFRESH
            and retry_antall < MAX_SERPER_REFRESH_PER_KAMP
        ):
            print(f"  → Fulltekst var {eksisterende.get('status')}, men kampen er fersk. Prøver fulltekst på nytt.")
        else:
            return entry, fulltekst_teller

    if fulltekst_teller >= MAX_FULLTEKST_PER_KJORING:
        print(f"  → Fulltekst totalgrense nådd ({MAX_FULLTEKST_PER_KJORING}). Hopper over.")
        return entry, fulltekst_teller

    try:
        fakta = hent_fulltekst_fakta_for_kamp(kamp, kandidater, MAX_FULLTEKST_PER_KJORING - fulltekst_teller)
    except Exception as e:
        print(f"  ADVARSEL: Fulltekst-henting feilet: {e}")
        fakta = {"status": "feilet", "versjon": FULLTEKST_CACHE_VERSION, "hentet": iso_utc_na(), "score": -100, "feil": str(e)[:200]}

    forsok_url = int(fakta.pop("_forsok_url", 0)) if isinstance(fakta, dict) else 0
    fulltekst_teller += forsok_url
    if isinstance(fakta, dict):
        prev_retry = 0
        if isinstance(eksisterende, dict):
            prev_retry = int(eksisterende.get("retry_antall", 0) or 0)
        if fakta.get("status") != "ok" or not har_rikt_fulltekstgrunnlag(fakta):
            fakta["retry_antall"] = prev_retry + 1
    entry["fulltekst_fakta"] = fakta
    cache[key] = entry
    skriv_serper_cache(cache)
    return entry, fulltekst_teller


def merge_fulltekst_fakta(detaljer, fakta):
    """La fulltekstfakta berike snippet-detaljene uten å overskrive gode verdier unødig."""
    if not isinstance(fakta, dict) or fakta.get("status") != "ok":
        return detaljer

    detaljer["fulltekst_source_type"] = fakta.get("source_type", "unknown")
    detaljer["source_type"] = fakta.get("source_type", "unknown")
    detaljer["fulltekst_riktig_kamp"] = fakta.get("fulltekst_riktig_kamp", True)
    detaljer["referat_fakta_score"] = fakta.get("referat_fakta_score", referat_fakta_score(fakta))
    detaljer["mangler"] = fakta.get("mangler", referat_fakta_mangler(fakta))

    for felt in ["late", "stoppage", "winner", "penalty", "equaliser", "goalless_first_half", "second_half_goals", "substitutes", "debutants", "nine_men", "first_world_cup_win", "knockout_secured", "group_top", "home_crowd", "keeper_error"]:
        detaljer[felt] = bool(detaljer.get(felt)) or bool(fakta.get(felt))
    detaljer["red_cards"] = max(int(detaljer.get("red_cards", 0) or 0), int(fakta.get("red_cards", 0) or 0))

    for felt in ["minute_text", "lead_minute_text", "penalty_scorer", "lead_scorer", "brace_scorer", "record_pass_player", "record_passes", "star_player", "hat_trick_player"]:
        if fakta.get(felt):
            detaljer[felt] = fakta.get(felt)
    for felt in ["early_lead", "scored_and_assisted"]:
        detaljer[felt] = bool(detaljer.get(felt)) or bool(fakta.get(felt))

    # Strukturerte mål fra fulltekst skal ha høyere prioritet enn rå scorerlister.
    goal_events = []
    for ev in list(fakta.get("goal_events", []) or []) + list(detaljer.get("goal_events", []) or []):
        if not isinstance(ev, dict):
            continue
        spiller = ev.get("spiller", "")
        lag = ev.get("lag", "")
        if not spiller or not lag:
            continue
        minutt = str(ev.get("minutt", "")).lower()
        # Unngå både "Johan Manzambi" og "Manzambi" som separate events for samme lag/minutt.
        duplikat = False
        for existing in goal_events:
            ex_spiller = existing.get("spiller", "")
            ex_lag = existing.get("lag", "")
            ex_minutt = str(existing.get("minutt", "")).lower()
            if ex_lag.lower() == lag.lower() and ex_minutt == minutt:
                if ex_spiller.lower() == spiller.lower() or ex_spiller.lower().endswith(" " + spiller.lower()) or spiller.lower().endswith(" " + ex_spiller.lower()):
                    if len(spiller) > len(ex_spiller):
                        existing["spiller"] = spiller
                    duplikat = True
                    break
        if not duplikat:
            goal_events.append(dict(ev))
    detaljer["goal_events"] = goal_events[:8]

    for felt in ["scorere", "sub_names", "second_half_scorers"]:
        unike = []
        for n in list(fakta.get(felt, []) or []) + list(detaljer.get(felt, []) or []):
            if n and n not in unike:
                unike.append(n)
        detaljer[felt] = unike[:4]

    detaljer["fulltekst_kilde"] = fakta.get("domene", "")
    detaljer["fulltekst_score"] = fakta.get("score", -100)
    return detaljer

def utled_kampdetaljer(kamp, kandidater, fulltekst_fakta=None):
    """
    Trekker ut noen trygge kampdetaljer fra de beste kandidatene.
    Målet er ikke å forstå alt, men å få med mer av verdien i snippets uten å publisere rå engelsk tekst.
    """
    hjemme = kamp["hjemmelag"]
    borte = kamp["bortelag"]
    tekster = kandidattekst_liste(kandidater, maks=5) if BRUK_SNIPPETS_I_RECAP else []
    samlet = " ".join(tekster)
    lav = samlet.lower()

    detaljer = {
        "tekster": tekster,
        "late": any(x in lav for x in ["stoppage time", "last-gasp", "deep into stoppage", "late", "90+", "90'+", "seven minutes from fulltime"]),
        "stoppage": any(x in lav for x in ["stoppage time", "last-gasp", "deep into stoppage", "90+", "90'+", "95th-minute", "94"]),
        "winner": "winner" in lav or "lone goal" in lav,
        "penalty": "penalty" in lav or "from the spot" in lav,
        "equaliser": any(x in lav for x in ["equaliser", "equalizer", "equalised", "equalized", "salvaged a point", "salvage draw", "rescues a draw"]),
        "goalless_first_half": "goalless first half" in lav,
        "second_half_goals": "second-half goals" in lav or "second half goals" in lav,
        "second_half_scorers": [],
        "substitutes": "substitutes" in lav or "substitute" in lav,
        "debutants": "debutants" in lav or "debutant" in lav,
        "scorere": [],
        "goal_events": ekstraher_goal_events_fra_tekst(samlet, kamp),
        "penalty_scorer": "",
        "sub_names": [],
        "minute_text": "",
        "lead_minute_text": "",
        "lead_scorer": "",
        "early_lead": False,
        "record_pass_player": "",
        "record_passes": "",
        "star_player": "",
        "scored_and_assisted": False,
    }

    # Minutt/tilleggstid.
    if re.search(r"90\s*'\s*\+\s*5|90\+5|95th-minute|fifth minute of second-half stoppage", lav):
        detaljer["minute_text"] = "på overtid"
    elif "seven minutes from fulltime" in lav:
        detaljer["minute_text"] = "sju minutter før slutt"
    elif re.search(r"83(?:rd)? minute|83'", lav):
        detaljer["minute_text"] = "mot slutten av kampen"

    if re.search(r"opening goal|early lead|took early control|took early lead", lav):
        detaljer["early_lead"] = True
    if re.search(r"sixth minute|6th minute|after six minutes|in the sixth", lav):
        detaljer["lead_minute_text"] = "tidlig i kampen"
        detaljer["early_lead"] = True

    m_record = re.search(r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:completed)\s+(\d{2,3})\s+(?i:passes)", samlet)
    if m_record:
        detaljer["record_pass_player"] = rens_spillernavn(m_record.group(1), hjemme, borte)
        detaljer["record_passes"] = m_record.group(2)

    m_star = re.search(r"(?i:brilliant)\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:scored one and set up another)", samlet)
    if m_star:
        detaljer["star_player"] = rens_spillernavn(m_star.group(1), hjemme, borte)
        detaljer["scored_and_assisted"] = True

    m_hat = re.search(r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:scored\s+(?:a\s+)?hat[- ]trick|hat[- ]trick)", samlet)
    if not m_hat:
        m_hat = re.search(r"(?i:hat[- ]trick\s+(?:from|by|for)\s+)([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})", samlet)
    if m_hat:
        detaljer["hat_trick_player"] = rens_spillernavn(m_hat.group(1), hjemme, borte)

    red_card_count = len(re.findall(r"(?i)red cards?|sent off|down to nine men|nine-man", samlet))
    detaljer["red_cards"] = red_card_count
    detaljer["nine_men"] = "nine-man" in lav or "nine men" in lav or "down to nine" in lav
    detaljer["first_world_cup_win"] = any(x in lav for x in ["first world cup win", "first-ever world cup win", "first world cup finals win", "first world cup match"] )
    detaljer["knockout_secured"] = any(x in lav for x in ["secure a spot in the knockout", "secured a spot in the knockout", "confirm their qualification", "qualification into the round", "first spot in the round", "advance to the knockout", "first team to reach"])
    detaljer["group_top"] = any(x in lav for x in ["top of group", "lead group", "top world cup group", "lead group b", "lead group a"])
    detaljer["home_crowd"] = any(x in lav for x in ["home crowd", "raucous home crowd", "in vancouver", "co-hosts"])
    detaljer["keeper_error"] = any(x in lav for x in ["keeper error", "goalkeeper error", "serious korean goalkeeper error"])

    # Spiller som scoret / avgjorde.
    score_patterns = [
        r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+scored\s+(?:deep into\s+)?(?:in\s+)?(?:the\s+)?(?:\w+[- ]minute\s+)?(?:of\s+second-half\s+stoppage\s+time\s+)?(?:a\s+)?(?:winner|goal|penalty)?",
        r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+struck\s+the\s+lone\s+goal",
        r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?:levelled|leveled|equalised|equalized|converted|netted|fired|headed)",
    ]
    for pattern in score_patterns:
        navn = finn_navn_i_tekst(pattern, samlet, hjemme, borte)
        if navn:
            detaljer["scorere"].append(navn)
            break

    lead_patterns = [
        r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:scored the opening goal)",
        r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:(?:sent|fired|put|gave).*?(?:lead|ahead))",
    ]
    for pattern in lead_patterns:
        navn = finn_navn_i_tekst(pattern, samlet, hjemme, borte)
        if navn:
            detaljer["lead_scorer"] = navn
            if navn not in detaljer["scorere"]:
                detaljer["scorere"].append(navn)
            break

    # Penalty from Teboho Mokoena / late penalty from ...
    pnavn = finn_navn_i_tekst(r"(?i:(?:penalty|spot-kick)\s+from)\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})", samlet, hjemme, borte)
    if not pnavn:
        pnavn = finn_navn_i_tekst(r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,3})\s+(?i:(?:levelled|leveled|equalised|equalized|converted).*?(?:penalty|spot))", samlet, hjemme, borte)
    if pnavn:
        detaljer["penalty_scorer"] = pnavn
        if pnavn not in detaljer["scorere"]:
            detaljer["scorere"].append(pnavn)

    # Second-half goals from Luis Díaz and Jáminton Campaz gave Colombia...
    m = re.search(
        r"(?i:second-half goals from)\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,2})\s+and\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,2})\s+(?:gave|give|helped|secured|sealed)\s+([^.,;]{0,80})",
        samlet,
    )
    if m:
        n1 = rens_spillernavn(m.group(1), hjemme, borte)
        n2 = rens_spillernavn(m.group(2), hjemme, borte)
        navnene = [n for n in [n1, n2] if n]
        detaljer["scorere"] = navnene
        detaljer["second_half_scorers"] = navnene
        detaljer["second_half_goals"] = True

    # Substitutes Johan Manzambi and Ruben Vargas inspire...
    m = re.search(
        r"(?i:substitutes?)\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,2})\s+and\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'\-]+){0,2})",
        samlet,
    )
    if m:
        detaljer["sub_names"] = [
            rens_spillernavn(m.group(1), hjemme, borte),
            rens_spillernavn(m.group(2), hjemme, borte),
        ]
        detaljer["sub_names"] = [n for n in detaljer["sub_names"] if n]

    # Kjør mål-event-ekstraksjon én gang til med kjente fulle navn fra snippets.
    # Dette gjør at korte scorerlinjer som "Manzambi (71, 90)" kan bli
    # "Johan Manzambi" når et annet snippet har fullt navn.
    kjente_navn = (
        list(detaljer.get("sub_names", []) or [])
        + list(detaljer.get("scorere", []) or [])
        + list(detaljer.get("second_half_scorers", []) or [])
    )
    if kjente_navn:
        for ev in ekstraher_goal_events_fra_tekst(samlet, kamp, kjente_navn=kjente_navn):
            legg_til_goal_event(
                detaljer["goal_events"],
                ev.get("spiller", ""),
                ev.get("lag", ""),
                ev.get("minutt", ""),
                ev.get("type", "goal"),
            )

    # Rydd dubletter i scorerlisten.
    unike = []
    for n in detaljer["scorere"]:
        if n and n not in unike:
            unike.append(n)
    detaljer["scorere"] = unike[:3]

    if detaljer.get("star_player"):
        detaljer["star_player"] = fullfor_spillernavn(
            detaljer["star_player"],
            list(detaljer.get("scorere", []) or [])
            + list(detaljer.get("second_half_scorers", []) or [])
            + list(detaljer.get("sub_names", []) or [])
        )
    if detaljer.get("hat_trick_player"):
        detaljer["hat_trick_player"] = fullfor_spillernavn(
            detaljer["hat_trick_player"],
            list(detaljer.get("scorere", []) or [])
            + list(detaljer.get("second_half_scorers", []) or [])
            + list(detaljer.get("sub_names", []) or [])
        )

    detaljer = merge_fulltekst_fakta(detaljer, fulltekst_fakta or {})
    return detaljer


def lag_detaljsetning(kamp, detaljer):
    hjemme = kamp["hjemmelag"]
    borte = kamp["bortelag"]
    h = kamp["hjemme"]
    b = kamp["borte"]
    hnavn = visningsnavn_lag(hjemme)
    bnavn = visningsnavn_lag(borte)
    vinner_lag = hjemme if h > b else borte if b > h else ""
    taper_lag = borte if h > b else hjemme if b > h else ""
    vinner = visningsnavn_lag(vinner_lag) if vinner_lag else ""
    taper = visningsnavn_lag(taper_lag) if taper_lag else ""

    goal_events = detaljer.get("goal_events", []) or []
    winner_events = [ev for ev in goal_events if ev.get("lag") == vinner_lag]
    loser_events = [ev for ev in goal_events if ev.get("lag") == taper_lag]
    hjemme_events = [ev for ev in goal_events if ev.get("lag") == hjemme]
    borte_events = [ev for ev in goal_events if ev.get("lag") == borte]

    scorere = detaljer.get("scorere", [])
    scorer_tekst = join_navn(scorere[:2]) if scorere else ""

    if detaljer.get("hat_trick_player") and h != b:
        return f"{detaljer['hat_trick_player']} ble kampens store profil for {vinner} med hattrick."

    # Spiller med to scoringer/brace fra fulltekst.
    if detaljer.get("brace_scorer") and h != b:
        navn = detaljer.get("brace_scorer")
        if detaljer.get("substitutes"):
            return f"{navn} ble den store profilen for {vinner} etter å ha kommet inn fra benken og scoret to ganger."
        return f"{navn} ble den store profilen for {vinner} med to scoringer."

    # Uavgjort med sen straffe/utligning. Bruk kun lag-kontekst hvis den finnes.
    if h == b and detaljer.get("penalty"):
        penalty_events = [ev for ev in goal_events if ev.get("type") == "penalty"]
        pnavn = detaljer.get("penalty_scorer") or (penalty_events[0].get("spiller") if penalty_events else "")
        plagg = penalty_events[0].get("lag") if penalty_events else ""
        lead = detaljer.get("lead_scorer", "")
        tid = detaljer.get("minute_text") or "mot slutten"
        if pnavn and lead and lead != pnavn:
            # Hvis straffelaget er kjent, bruk det. Ellers behold tidligere trygge formulering.
            redningslag = visningsnavn_lag(plagg) if plagg else bnavn
            return f"{lead} sendte {hnavn} i ledelsen, men {pnavn} reddet uavgjort for {redningslag} med en straffe {tid}."
        if pnavn:
            redningslag = visningsnavn_lag(plagg) if plagg else bnavn
            return f"{redningslag} reddet uavgjort med en straffescoring av {pnavn} {tid}."
        return f"{bnavn} reddet uavgjort med en sen straffescoring."

    # Trygg sen scoring/vinnermål: krever mål-event knyttet til vinnerlaget.
    # Brukes kun på tette kamper, ellers kan sene 4–1-scoringer feilaktig bli "matchvinner".
    if h != b and winner_events and abs(h - b) <= 1 and (detaljer.get("winner") or detaljer.get("stoppage") or detaljer.get("late")):
        ev = winner_events[-1]
        navn = ev.get("spiller", "")
        tid = event_minutt_til_norsk(ev) or detaljer.get("minute_text") or "sent i kampen"
        if h + b == 1:
            return f"{navn} avgjorde for {vinner} med kampens eneste scoring {tid}."
        return f"{navn} ble matchvinner for {vinner} med en scoring {tid}."

    # Mål etter pause fra vinnerlaget. Bruk eksplisitte second_half_scorers først.
    # Dette hindrer at åpningmålscorer (f.eks. Daniel Muñoz før pause) blir blandet
    # inn i formuleringen "Etter pause sørget X og Y ...".
    if h != b and detaljer.get("second_half_goals"):
        second_half_names = detaljer.get("second_half_scorers", []) or []
        if len(second_half_names) >= 2:
            navn = join_navn(second_half_names[:2])
            other_winner_names = [n for n in spillere_for_lag(goal_events, vinner_lag) if n not in second_half_names]
            if other_winner_names:
                return f"{other_winner_names[0]} sendte {vinner} i ledelsen før pause, og etter hvilen sørget {navn} for at {vinner} dro fra."
            return f"Etter pause sørget {navn} for at {vinner} dro fra og sikret seieren."

    # Full måloversikt bør overstyre generell innbytter-/historikktekst.
    # Dette gir mer meningsfulle referater når FIFA/CONCACAF leverer scorerliste med minutter.
    if h != b and winner_events:
        winner_names = spillere_for_lag(goal_events, vinner_lag)
        loser_names = spillere_for_lag(goal_events, taper_lag)
        maal_teller = antall_maal_per_spiller(goal_events, vinner_lag)
        dobbel = next((m["spiller"] for m in maal_teller if m.get("antall", 0) >= 2), "")

        # Bruk denne grenen når vi har mer enn ett mål / flere scorere.
        # Tette 1-0-kamper er håndtert av matchvinner-grenen over.
        if len(winner_events) >= 2 or len(winner_names) >= 2 or dobbel:
            start = "Etter en målløs første omgang " if detaljer.get("goalless_first_half") else ""
            if dobbel:
                andre = [n for n in winner_names if n.lower() != dobbel.lower()]
                if start:
                    hoved = f"{start}scoret {dobbel} to ganger"
                else:
                    hoved = f"{dobbel} scoret to ganger"
                if andre:
                    hoved += f", og {join_navn(andre[:2])} kom også på scoringslisten for {vinner}"
                else:
                    hoved += f" for {vinner}"
            else:
                if start:
                    hoved = f"{start}scoret {join_navn(winner_names[:3])} for {vinner}"
                else:
                    hoved = f"{join_navn(winner_names[:3])} scoret for {vinner}"

            if loser_names:
                return f"{hoved}, mens {join_navn(loser_names[:2])} reduserte for {taper}."
            return hoved + "."

    # Innbyttere + målløs første omgang. Dette brukes først når vi ikke har bedre måloversikt.
    sub_names = detaljer.get("sub_names", [])
    if sub_names and h != b:
        navn = join_navn(sub_names[:2])
        if detaljer.get("goalless_first_half"):
            return f"Etter en målløs første omgang ble innbytterne {navn} sentrale da {vinner} tok over kampen."
        return f"Innbytterne {navn} ble viktige da {vinner} avgjorde kampen."

    # Hvis vi har full måloversikt med lag, bruk en nøktern og trygg formulering.
    if h != b and winner_events:
        winner_names = spillere_for_lag(goal_events, vinner_lag)
        loser_names = spillere_for_lag(goal_events, taper_lag)
        if winner_names and loser_names:
            return f"{join_navn(winner_names[:3])} scoret for {vinner}, mens {join_navn(loser_names[:2])} scoret for {taper}."
        if winner_names:
            return f"{join_navn(winner_names[:3])} var blant målscorerne for {vinner}."

    # Målløs første omgang og klar seier.
    if detaljer.get("goalless_first_half") and h != b:
        return f"Kampen var målløs til pause, før {vinner} tok over etter hvilen."

    # Debutant-vinkel.
    if detaljer.get("debutants") and h != b:
        return f"{taper} fikk kjenne nivået i VM-debuten, mens {vinner} åpnet gruppespillet med tre poeng."

    # En navngitt målscorer uten nok lag-kontekst: ikke koble spilleren til vinnerlaget.
    if scorere:
        return f"{scorer_tekst} var blant spillerne som kom på scoringslisten."

    return fallback_kamptekst(hjemme, borte, h, b)

def setning_normalform(s):
    return re.sub(r"[^a-z0-9æøå]+", "", (s or "").lower())


def legg_til_setning(setninger, setning):
    setning = re.sub(r"\s+", " ", (setning or "").strip())
    if not setning:
        return
    if not setning.endswith((".", "!", "?")):
        setning += "."
    norm = setning_normalform(setning)
    for eksisterende in setninger:
        en = setning_normalform(eksisterende)
        if not norm or norm == en or norm in en or en in norm:
            return
    setninger.append(setning)


def minutt_sort_key(ev):
    txt = str((ev or {}).get("minutt", ""))
    m = re.search(r"\d+", txt)
    return int(m.group(0)) if m else 999


def kort_minutt(minutt):
    minutt = str(minutt or "").strip()
    if not minutt:
        return ""
    if "second half" in minutt.lower():
        return "etter pause"
    return minutt.replace("'", "")


def spillere_med_minutter(events):
    grupper = []
    for ev in sorted(events or [], key=minutt_sort_key):
        spiller = ev.get("spiller", "")
        if not spiller:
            continue
        funnet = None
        for g in grupper:
            if samme_spillernavn(g["spiller"], spiller):
                funnet = g
                if len(spiller) > len(g["spiller"]):
                    g["spiller"] = spiller
                break
        if not funnet:
            funnet = {"spiller": spiller, "minutter": [], "typer": []}
            grupper.append(funnet)
        minutt = kort_minutt(ev.get("minutt", ""))
        if minutt and minutt not in funnet["minutter"]:
            funnet["minutter"].append(minutt)
        typ = ev.get("type", "goal")
        if typ and typ not in funnet["typer"]:
            funnet["typer"].append(typ)
    return grupper


def spillergruppe_tekst(gruppe):
    navn = gruppe.get("spiller", "")
    minutter = gruppe.get("minutter", [])
    typer = gruppe.get("typer", [])
    if not navn:
        return ""
    suffix = ""
    if "penalty" in typer:
        suffix = " på straffe"
    if len(minutter) >= 2:
        return f"{navn}{suffix} ({' og '.join(minutter)})"
    if len(minutter) == 1 and minutter[0] != "etter pause":
        return f"{navn}{suffix} ({minutter[0]})"
    return f"{navn}{suffix}"


def bygg_maaloversikt_setning(kamp, detaljer):
    hjemme = kamp["hjemmelag"]
    borte = kamp["bortelag"]
    h = kamp["hjemme"]
    b = kamp["borte"]
    vinner_lag = hjemme if h > b else borte if b > h else ""
    taper_lag = borte if h > b else hjemme if b > h else ""
    vinner = visningsnavn_lag(vinner_lag) if vinner_lag else ""
    taper = visningsnavn_lag(taper_lag) if taper_lag else ""
    events = detaljer.get("goal_events", []) or []
    if not events:
        return ""

    if vinner_lag:
        winner_events = [ev for ev in events if ev.get("lag") == vinner_lag]
        loser_events = [ev for ev in events if ev.get("lag") == taper_lag]
        if len(winner_events) >= 2 or len(events) >= 3:
            vinnere = [spillergruppe_tekst(g) for g in spillere_med_minutter(winner_events)]
            tapere = [spillergruppe_tekst(g) for g in spillere_med_minutter(loser_events)]
            vinnere = [v for v in vinnere if v]
            tapere = [t for t in tapere if t]
            if vinnere and tapere:
                return f"Målene til {vinner} kom ved {join_navn(vinnere)}, mens {join_navn(tapere)} reduserte for {taper}."
            if vinnere:
                return f"Målene til {vinner} kom ved {join_navn(vinnere)}."

    if h == b and len(events) >= 2:
        hjemme_events = [ev for ev in events if ev.get("lag") == hjemme]
        borte_events = [ev for ev in events if ev.get("lag") == borte]
        htxt = join_navn([spillergruppe_tekst(g) for g in spillere_med_minutter(hjemme_events) if spillergruppe_tekst(g)])
        btxt = join_navn([spillergruppe_tekst(g) for g in spillere_med_minutter(borte_events) if spillergruppe_tekst(g)])
        if htxt and btxt:
            return f"{htxt} scoret for {visningsnavn_lag(hjemme)}, mens {btxt} svarte for {visningsnavn_lag(borte)}."
    return ""


def bygg_utdypende_setninger(kamp, detaljer):
    hjemme = kamp["hjemmelag"]
    borte = kamp["bortelag"]
    h = kamp["hjemme"]
    b = kamp["borte"]
    hnavn = visningsnavn_lag(hjemme)
    bnavn = visningsnavn_lag(borte)
    vinner_lag = hjemme if h > b else borte if b > h else ""
    taper_lag = borte if h > b else hjemme if b > h else ""
    vinner = visningsnavn_lag(vinner_lag) if vinner_lag else ""
    taper = visningsnavn_lag(taper_lag) if taper_lag else ""
    setninger = []

    maaloversikt = bygg_maaloversikt_setning(kamp, detaljer)
    if maaloversikt:
        legg_til_setning(setninger, maaloversikt)

    if h != b and h + b == 1 and (detaljer.get("late") or detaljer.get("stoppage")):
        legg_til_setning(setninger, f"Dermed ble kampen avgjort på små marginer, etter at {taper} lenge hadde holdt unna")

    if h != b and detaljer.get("goalless_first_half") and h + b >= 3:
        legg_til_setning(setninger, f"Alle scoringene kom etter pause, og {vinner} fikk kampen over i sitt spor i sluttfasen")

    if h != b and detaljer.get("second_half_goals"):
        if detaljer.get("star_player") and detaljer.get("scored_and_assisted"):
            legg_til_setning(setninger, f"{detaljer['star_player']} satte tydelig preg på kampen med både scoring og målgivende bidrag")
        elif detaljer.get("second_half_scorers"):
            legg_til_setning(setninger, f"Avgjørelsen kom først etter hvilen, da {join_navn(detaljer.get('second_half_scorers', [])[:2])} fant veien til mål")

    if h != b and detaljer.get("debutants"):
        legg_til_setning(setninger, f"For {taper} ble det en tøff VM-debut, selv om kampen lenge levde")

    if h == b and detaljer.get("penalty") and detaljer.get("equaliser"):
        if detaljer.get("early_lead"):
            legg_til_setning(setninger, f"{hnavn} hadde fått kampen inn i sitt spor med en tidlig ledelse, men klarte ikke å holde helt inn")
        legg_til_setning(setninger, f"Straffen gjorde at {bnavn} fikk med seg ett poeng fra en kamp som lenge så ut til å vippe motsatt vei")

    if detaljer.get("record_pass_player") and detaljer.get("record_passes"):
        legg_til_setning(setninger, f"{detaljer['record_pass_player']} ble trukket fram med {detaljer['record_passes']} pasninger, men det var ikke nok til å gi {bnavn if bnavn != vinner else hnavn} uttelling")

    if h != b and detaljer.get("hat_trick_player"):
        legg_til_setning(setninger, f"{detaljer['hat_trick_player']} markerte seg med hattrick for {vinner}")

    if h != b and (detaljer.get("nine_men") or int(detaljer.get("red_cards", 0) or 0) >= 2):
        legg_til_setning(setninger, f"{taper} avsluttet kampen med ni mann etter to røde kort")
    elif h != b and int(detaljer.get("red_cards", 0) or 0) == 1:
        legg_til_setning(setninger, f"{taper} fikk også en utvisning som preget kampbildet")

    if h != b and detaljer.get("first_world_cup_win"):
        legg_til_setning(setninger, f"For {vinner} var dette en historisk første VM-seier")

    if h != b and detaljer.get("keeper_error"):
        legg_til_setning(setninger, f"Avgjørelsen kom etter en keeperfeil som {vinner} utnyttet")

    if h != b and detaljer.get("knockout_secured"):
        legg_til_setning(setninger, f"Seieren sikret samtidig {vinner} plass i utslagsrunden")
    elif h != b and detaljer.get("group_top"):
        legg_til_setning(setninger, f"Resultatet sender {vinner} opp i en sterk posisjon i gruppen")

    if h != b and detaljer.get("home_crowd") and not detaljer.get("first_world_cup_win"):
        legg_til_setning(setninger, f"Vertsnasjonen fikk dermed en opptur foran eget publikum")

    if not setninger:
        if h == b:
            legg_til_setning(setninger, "Begge lag fikk perioder med overtak, men ingen klarte å vippe kampen definitivt i sin favør")
        elif abs(h - b) >= 3:
            legg_til_setning(setninger, f"Seieren ble etter hvert klar, selv om kampen ikke nødvendigvis var avgjort fra start")
        else:
            legg_til_setning(setninger, f"{vinner} fikk dermed full uttelling i en kamp der marginene var viktige")

    return setninger


def bygg_norsk_kampavsnitt(kamp, kandidater, fulltekst_fakta=None, detaljer=None):
    detaljer = detaljer or utled_kampdetaljer(kamp, kandidater, fulltekst_fakta)
    setninger = []
    legg_til_setning(setninger, resultatsetning(kamp["hjemmelag"], kamp["bortelag"], kamp["hjemme"], kamp["borte"]))
    legg_til_setning(setninger, lag_detaljsetning(kamp, detaljer))
    for s in bygg_utdypende_setninger(kamp, detaljer):
        legg_til_setning(setninger, s)

    # Sikre at publisert kampreferat ikke ender som bare resultat + én kort detalj.
    if len(setninger) < MIN_RECAP_SETNINGER:
        h = kamp["hjemme"]
        b = kamp["borte"]
        hjemme = visningsnavn_lag(kamp["hjemmelag"])
        borte = visningsnavn_lag(kamp["bortelag"])
        if h == b:
            legg_til_setning(setninger, f"Poengdelingen gjør at både {hjemme} og {borte} står igjen med noe, men uten full uttelling")
        else:
            vinner = visningsnavn_lag(kamp["hjemmelag"] if h > b else kamp["bortelag"])
            legg_til_setning(setninger, f"Resultatet gir {vinner} tre viktige poeng i gruppespillet")

    return "\n".join(setninger)


def tokeniser(s):
    return {w for w in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+", (s or "").lower()) if len(w) > 3}


def er_duplikat_fakta(faktatekst, eksisterende_tekst):
    ft = tokeniser(faktatekst)
    et = tokeniser(eksisterende_tekst)
    if not ft or not et:
        return False
    overlap = len(ft & et) / max(1, min(len(ft), len(et)))
    return overlap >= 0.65


def bygg_historikkavsnitt(ref):
    historikk = ref.get("historikk", "")
    fakta = ref.get("fakta", []) or []
    linjer = []
    if historikk:
        linjer.append(historikk.rstrip("."))
    samlet = " ".join(linjer)
    for f in fakta:
        if not f:
            continue
        if er_duplikat_fakta(f, samlet):
            continue
        linjer.append(f.rstrip("."))
        break  # Hold historikkdelen kort.
    if not linjer:
        return ""
    return ". ".join(linjer) + "."


def bygg_recap_tekst(kamp, kandidater, ref, fulltekst_fakta=None):
    """
    Bygger publisert recap-tekst.

    Prioritet:
    1. Ferske kampdetaljer fra fulltekst/snippets: mål, minutter, straffe, sen utligning, kampforløp.
    2. Historikk/fakta fra kamp-referanser.json kun som fallback når kampdataene er for tynne.

    Tipping skal ikke inn i recap_tekst. Den ligger strukturert i kamp.tippinger.
    """
    detaljer = utled_kampdetaljer(kamp, kandidater, fulltekst_fakta)
    avsnitt = [bygg_norsk_kampavsnitt(kamp, kandidater, fulltekst_fakta, detaljer=detaljer)]

    if skal_legge_til_historikk(ref, detaljer):
        hist = bygg_historikkavsnitt(ref)
        if hist:
            avsnitt.append(hist)

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

    eksisterende = les_eksisterende_kamppost()
    eksisterende_for_dato = eksisterende if eksisterende and eksisterende.get("dato") == igaar_str else None
    if eksisterende_for_dato:
        print(f"  → kamppost.json finnes allerede for {igaar_str}. Regenererer rapporten; Serper styres av cache.")

    print("\nLeser data/data.js...")
    vm_data = les_data_js()

    print("Leser data/kamp-referanser.json...")
    referanser = les_referanser()

    cache = les_serper_cache()
    seedet = seed_cache_fra_eksisterende_kamppost(eksisterende_for_dato, cache)
    if seedet:
        skriv_serper_cache(cache)
        print(f"  → Seedet Serper-cache fra eksisterende kamppost: {seedet} kamper")
    serper_teller = 0
    fulltekst_teller = 0

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
        kandidater, cache_entry, serper_teller = suppler_med_offisielle_fulltekst_kilder(kamp, kandidater, cache, cache_entry, serper_teller)
        cache_entry, fulltekst_teller = oppdater_fulltekst_i_cache(kamp, kandidater, cache, cache_entry, fulltekst_teller)
        fulltekst_fakta = (cache_entry or {}).get("fulltekst_fakta", {})
        recap_tekst = bygg_recap_tekst(kamp, kandidater, ref, fulltekst_fakta)

        beste_score = cache_entry.get("beste_score", -100) if cache_entry else -100
        referat_score = referat_fakta_score(fulltekst_fakta) if isinstance(fulltekst_fakta, dict) else 0
        rikt_fulltekstgrunnlag = har_rikt_fulltekstgrunnlag(fulltekst_fakta) if isinstance(fulltekst_fakta, dict) else False
        fallback = not rikt_fulltekstgrunnlag
        if isinstance(fulltekst_fakta, dict) and fulltekst_fakta.get("status") == "ok":
            recap_status = "ok" if rikt_fulltekstgrunnlag else "tynt_fulltekstgrunnlag"
        else:
            recap_status = fulltekst_fakta.get("status", "ikke_sokt") if isinstance(fulltekst_fakta, dict) else "ikke_sokt"

        print(f"  Eksakt: {len(tippinger['eksakt'])} | Riktig: {len(tippinger['riktig'])} | Bom: {len(tippinger['bom'])}")
        print(f"  Recap-kvalitet: status={recap_status}, fakta_score={referat_score}, fallback={fallback}")

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
            "serper_kandidater": kandidater[:10],
            "recap_kvalitet": {
                "score":       referat_score,
                "status":      recap_status,
                "grunnlag":    "fulltekst" if isinstance(fulltekst_fakta, dict) and fulltekst_fakta.get("status") == "ok" else "fallback",
                "source_type": fulltekst_fakta.get("source_type", "") if isinstance(fulltekst_fakta, dict) else "",
                "antall_sok":  cache_entry.get("antall_sok", 0) if cache_entry else 0,
                "fallback":    fallback,
                "cache_key":   cache_noekkel(kamp_id, h_score, b_score),
                "serper_score": beste_score,
                "fulltekst_status": fulltekst_fakta.get("status", "ikke_sokt") if isinstance(fulltekst_fakta, dict) else "ikke_sokt",
                "fulltekst_kilde":  fulltekst_fakta.get("domene", "") if isinstance(fulltekst_fakta, dict) else "",
                "fulltekst_score":  fulltekst_fakta.get("score", -100) if isinstance(fulltekst_fakta, dict) else -100,
                "referat_fakta_score": referat_score,
                "mangler": fulltekst_fakta.get("mangler", referat_fakta_mangler(fulltekst_fakta)) if isinstance(fulltekst_fakta, dict) else ["fulltekst_fakta"],
                "offisiell_kilde_sokt": cache_entry.get("offisiell_kilde_domener", []) if cache_entry else [],
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

    if eksisterende_for_dato and uten_generert(eksisterende_for_dato) == uten_generert(kamppost):
        # Unngå ny commit/diff hver halvtime bare fordi timestamp endrer seg.
        kamppost["generert"] = eksisterende_for_dato.get("generert", kamppost["generert"])
        print("\n✓ Kamppost regenerert, men innholdet er uendret. Beholder eksisterende generert-tidspunkt.")
    else:
        print(f"\n✓ Kamppost regenerert med endringer for {igaar_str}")

    KAMPPOST_JSON.write_text(
        json.dumps(kamppost, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    skriv_serper_cache(cache)
    print(f"✓ Skrev kamppost.json med {len(kamposter)} kamper for {igaar_str}")
    print(f"✓ Serper-søk brukt i denne kjøringen: {serper_teller}")
    print(f"✓ Fulltekstforsøk brukt i denne kjøringen: {fulltekst_teller}")


if __name__ == "__main__":
    main()
