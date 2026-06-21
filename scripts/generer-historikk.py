"""
VM 2026 — Kampreferat-generator, FIFA-first

Enkel regel:
1. Ferdig kamp -> bygg forventede FIFA article-URL-er.
2. Hent Jina/fulltekst fra FIFA.
3. Parse FIFA sin kampblokk, mål-linjer og noen faste seksjoner.
4. Godkjenn først når harde kriterier er møtt.
5. Hvis kriterier ikke er møtt, publiser kort foreløpig rapport og prøv igjen neste kjøring.

Ingen Serper i normalflyten. Ingen snippet-basert publisering.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

# ── KONFIG ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "data"
DATA_JS = DATA_DIR / "data.js"
KAMPPOST_JSON = DATA_DIR / "kamppost.json"
REFERANSER_JSON = DATA_DIR / "kamp-referanser.json"
# Behold filnavn for kompatibilitet med eksisterende repo/workflow, selv om Serper ikke brukes.
SERPER_CACHE_JSON = DATA_DIR / "serper-cache.json"

NORSK_TZ = timezone(timedelta(hours=2))  # CEST under VM
RETRY_SISTE_TIMER = int(os.environ.get("RETRY_SISTE_TIMER", "36"))
FULLTEKST_TIMEOUT = int(os.environ.get("FULLTEKST_TIMEOUT", "15"))
FULLTEKST_MAX_BYTES = int(os.environ.get("FULLTEKST_MAX_BYTES", "500000"))
MAX_FULLTEKST_PER_KJORING = int(os.environ.get("MAX_FULLTEKST_PER_KJORING", "24"))
JINA_READER_AKTIVERT = os.environ.get("JINA_READER_AKTIVERT", "1") != "0"
FULLTEKST_CACHE_VERSION = "v21-fifa-rich-recap"
FIFA_ARTICLE_BASE = os.environ.get(
    "FIFA_ARTICLE_BASE",
    "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/articles",
)

NORSKE_DAGER = ["Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag"]
NORSKE_MAANEDER = [
    "januar", "februar", "mars", "april", "mai", "juni",
    "juli", "august", "september", "oktober", "november", "desember",
]

LAG_VISNING = {
    "Brazil": "Brasil",
    "Germany": "Tyskland",
    "Ivory Coast": "Elfenbenskysten",
    "Côte d'Ivoire": "Elfenbenskysten",
    "Cote d'Ivoire": "Elfenbenskysten",
    "Netherlands": "Nederland",
    "Sweden": "Sverige",
    "Scotland": "Skottland",
    "Morocco": "Marokko",
    "Haiti": "Haiti",
    "Turkey": "Tyrkia",
    "Türkiye": "Tyrkia",
    "Turkiye": "Tyrkia",
    "Paraguay": "Paraguay",
    "United States": "USA",
    "USA": "USA",
    "Australia": "Australia",
    "Czech Republic": "Tsjekkia",
    "Czechia": "Tsjekkia",
    "South Africa": "Sør-Afrika",
    "South Korea": "Sør-Korea",
    "Bosnia & Herzegovina": "Bosnia-Hercegovina",
    "Bosnia and Herzegovina": "Bosnia-Hercegovina",
    "Qatar": "Qatar",
    "Switzerland": "Sveits",
    "Canada": "Canada",
    "Mexico": "Mexico",
    "Ghana": "Ghana",
    "Panama": "Panama",
    "Uzbekistan": "Usbekistan",
    "Colombia": "Colombia",
}

ALIASES = {
    "Brazil": ["Brazil", "Brasil"],
    "Germany": ["Germany", "Deutschland"],
    "Ivory Coast": ["Ivory Coast", "Côte d'Ivoire", "Cote d'Ivoire", "Côte d’Ivoire", "Les Éléphants"],
    "Netherlands": ["Netherlands", "Dutch", "Oranje"],
    "Sweden": ["Sweden", "Swedes"],
    "Scotland": ["Scotland", "Scots", "Tartan Army"],
    "Morocco": ["Morocco", "Atlas Lions"],
    "Haiti": ["Haiti"],
    "Turkey": ["Turkey", "Türkiye", "Turkiye"],
    "Paraguay": ["Paraguay"],
    "United States": ["United States", "USA", "USMNT"],
    "USA": ["United States", "USA", "USMNT"],
    "Australia": ["Australia", "Socceroos"],
    "Czech Republic": ["Czech Republic", "Czechia"],
    "Czechia": ["Czechia", "Czech Republic"],
    "South Africa": ["South Africa"],
    "South Korea": ["South Korea", "Korea Republic"],
    "Bosnia & Herzegovina": ["Bosnia & Herzegovina", "Bosnia and Herzegovina", "Bosnia-Herzegovina", "Bosnia"],
    "Bosnia and Herzegovina": ["Bosnia and Herzegovina", "Bosnia & Herzegovina", "Bosnia-Herzegovina", "Bosnia"],
    "Qatar": ["Qatar"],
    "Switzerland": ["Switzerland", "Swiss"],
    "Canada": ["Canada"],
    "Mexico": ["Mexico"],
    "Ghana": ["Ghana"],
    "Panama": ["Panama"],
    "Uzbekistan": ["Uzbekistan"],
    "Colombia": ["Colombia"],
}

SLUG_OVERRIDES = {
    "USA": ["united-states", "usa"],
    "United States": ["united-states", "usa"],
    "Ivory Coast": ["cote-divoire", "ivory-coast"],
    "Côte d'Ivoire": ["cote-divoire", "ivory-coast"],
    "Cote d'Ivoire": ["cote-divoire", "ivory-coast"],
    "Bosnia & Herzegovina": ["bosnia-herzegovina", "bosnia-and-herzegovina"],
    "Bosnia and Herzegovina": ["bosnia-herzegovina", "bosnia-and-herzegovina"],
    "Czech Republic": ["czechia", "czech-republic"],
    "South Korea": ["south-korea", "korea-republic"],
    "Turkey": ["turkey", "turkiye"],
}

# ── DATO/FIL ──────────────────────────────────────────────────────────────────

def norsk_dato_igaar():
    return (datetime.now(NORSK_TZ) - timedelta(days=1)).date()


def utc_til_norsk_dato(utc_str):
    if not utc_str:
        return None
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return dt.astimezone(NORSK_TZ).strftime("%Y-%m-%d")
    except Exception:
        return utc_str[:10]


def formater_norsk_dato(dato_str):
    dato = datetime.strptime(dato_str, "%Y-%m-%d")
    return f"{NORSKE_DAGER[dato.weekday()]} {dato.day}. {NORSKE_MAANEDER[dato.month - 1]} {dato.year}"


def iso_utc_na():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_til_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def les_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def skriv_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def les_data_js():
    tekst = DATA_JS.read_text(encoding="utf-8")
    tekst = re.sub(r"^.*?const VM_DATA\s*=\s*", "", tekst, flags=re.DOTALL).strip().rstrip(";")
    return json.loads(tekst)


def les_referanser():
    return les_json(REFERANSER_JSON, {})


def les_cache():
    return les_json(SERPER_CACHE_JSON, {})


def les_eksisterende_kamppost():
    return les_json(KAMPPOST_JSON, None)


def uten_generert(obj):
    if not isinstance(obj, dict):
        return obj
    kopi = dict(obj)
    kopi.pop("generert", None)
    return kopi

# ── KAMPER/TIPPING ────────────────────────────────────────────────────────────

def kampreferat_noekkel(hjemme, borte):
    lag = sorted([hjemme, borte])
    return f"{lag[0]}|||{lag[1]}"


def cache_noekkel(kamp_id, h_score, b_score):
    return f"{kamp_id}|{h_score}-{b_score}"


def kamp_norsk_dato(kamp):
    fd_utc = kamp.get("fd_utcDate", "")
    return utc_til_norsk_dato(fd_utc) if fd_utc else kamp.get("dato_openfootball", "")


def kamp_utc_dt(kamp):
    return iso_til_dt(kamp.get("fd_utcDate", ""))


def kamp_innenfor_siste_timer(kamp, timer):
    dt = kamp_utc_dt(kamp)
    if dt is None:
        return False
    alder_timer = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return 0 <= alder_timer <= timer


def er_placeholder_kamp(kamp):
    hjemme = kamp.get("hjemmelag", "")
    borte = kamp.get("bortelag", "")
    return bool(re.match(r"^[W1-9]", hjemme) or re.match(r"^[W1-9]", borte))


def cache_har_ferdig_fifa(cache_entry, kamp):
    fakta = (cache_entry or {}).get("fulltekst_fakta") or {}
    return fulltekst_kriterier_mott(fakta, kamp)


def finn_kamper_for_rapport_og_retry(vm_data, cache, rapport_dato_str):
    rapport = []
    retry = []
    rapport_ids = set()
    for kid, kamp in vm_data.get("resultater", {}).items():
        if er_placeholder_kamp(kamp) or not kamp.get("ferdig"):
            continue
        kamp_id = kamp.get("kamp_id", kid)
        if kamp_norsk_dato(kamp) == rapport_dato_str:
            rapport.append(kamp)
            rapport_ids.add(kamp_id)
            continue
        key = cache_noekkel(kamp_id, kamp.get("hjemme"), kamp.get("borte"))
        if kamp_innenfor_siste_timer(kamp, RETRY_SISTE_TIMER) and not cache_har_ferdig_fifa(cache.get(key), kamp):
            retry.append(kamp)

    def sortkey(k):
        return k.get("fd_utcDate", k.get("dato_openfootball", ""))

    alle = []
    seen = set()
    for kamp in sorted(rapport, key=sortkey) + sorted(retry, key=sortkey):
        kid = kamp.get("kamp_id")
        if kid not in seen:
            seen.add(kid)
            alle.append(kamp)
    return alle, rapport_ids, len([k for k in retry if k.get("kamp_id") not in rapport_ids])


def hent_tippinger_for_kamp(vm_data, kamp_id):
    eksakt, riktig, bom = [], [], []
    for deltaker in vm_data.get("stilling", []):
        for tips in deltaker.get("tippinger", []):
            if tips.get("kamp_id") != kamp_id:
                continue
            info = {
                "navn": deltaker.get("navn", ""),
                "tippa_h": tips.get("tippa_h"),
                "tippa_b": tips.get("tippa_b"),
                "poeng": tips.get("poeng", 0),
            }
            if tips.get("eksakt"):
                eksakt.append(info)
            elif tips.get("riktig_utfall"):
                riktig.append(info)
            else:
                bom.append(info)
    return {"eksakt": eksakt, "riktig": riktig, "bom": bom}


def bygg_stilling(vm_data, alle_kamp_ids):
    stilling = []
    for d in vm_data.get("stilling", []):
        dag_poeng = 0
        for t in d.get("tippinger", []):
            if t.get("kamp_id") in alle_kamp_ids:
                dag_poeng += int(t.get("poeng", 0) or 0)
        stilling.append({
            "navn": d.get("navn", ""),
            "plass": d.get("plass"),
            "poeng_totalt": d.get("poeng_totalt", d.get("poeng", 0)),
            "poeng_i_dag": dag_poeng,
        })
    return stilling

# ── URL/HENTING ───────────────────────────────────────────────────────────────

def domene_fra_url(url):
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def ascii_slug(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def fifa_slug_varianter(lag):
    varianter = []
    for v in SLUG_OVERRIDES.get(lag, []):
        varianter.append(v)
    varianter.append(ascii_slug(lag))
    for alias in ALIASES.get(lag, []):
        varianter.append(ascii_slug(alias))
    out = []
    for v in varianter:
        if v and v not in out:
            out.append(v)
    return out[:4]


def bygg_fifa_article_urls(kamp):
    hjemme = kamp["hjemmelag"]
    borte = kamp["bortelag"]
    urls = []
    for a, b in [(hjemme, borte), (borte, hjemme)]:
        for sa in fifa_slug_varianter(a):
            for sb in fifa_slug_varianter(b):
                for suffix in ["match-report-highlights", "highlights-match-report"]:
                    urls.append(f"{FIFA_ARTICLE_BASE}/{sa}-{sb}-{suffix}")
    out = []
    for u in urls:
        if u not in out:
            out.append(u)
    return out[:8]


def hent_url_tekst_direkte(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; RambergVMBot/1.0)",
            "Accept-Language": "en-US,en;q=0.9,nb;q=0.7",
        },
    )
    with urllib.request.urlopen(req, timeout=FULLTEKST_TIMEOUT) as resp:
        raw = resp.read(FULLTEKST_MAX_BYTES)
    charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def jina_reader_url(url):
    return "https://r.jina.ai/" + url


def hent_url_tekst(url):
    if JINA_READER_AKTIVERT:
        try:
            tekst = hent_url_tekst_direkte(jina_reader_url(url))
            if tekst and len(tekst) > 1200:
                print(f"    Fulltekst: Jina Reader OK for fifa.com ({len(tekst)} tegn)")
                return tekst
        except Exception as e:
            print(f"    Fulltekst: klarte ikke hente r.jina.ai direkte ({e})")
    try:
        html_tekst = hent_url_tekst_direkte(url)
        tekst = ekstraher_tekst_fra_html(html_tekst)
        if tekst and len(tekst) > 1000:
            print(f"    Fulltekst: direkte HTML OK for fifa.com ({len(tekst)} tegn)")
            return tekst
    except Exception as e:
        print(f"    Fulltekst: direkte FIFA feilet ({e})")
    return ""


def ekstraher_tekst_fra_html(raw_html):
    # Enkel fallback hvis Jina feiler. Jina er normalvei.
    if not raw_html:
        return ""
    text = re.sub(r"(?is)<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", " ", raw_html)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()

# ── TEKSTNORMALISERING ────────────────────────────────────────────────────────

def normaliser_dash(s):
    return (s or "").replace("–", "-").replace("—", "-").replace("−", "-")


def normaliser_rom(s):
    return re.sub(r"\s+", " ", s or "").strip()


def fjern_markdown(s):
    s = s or ""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = s.replace("**", "").replace("__", "")
    s = s.replace("_", "")
    s = html.unescape(s)
    return normaliser_rom(s)


def strip_quote(line):
    return re.sub(r"^\s*>\s?", "", line or "").strip()


def visningsnavn_lag(lag):
    return LAG_VISNING.get(lag, lag)


def aliases_for(lag):
    vals = [lag] + ALIASES.get(lag, []) + [visningsnavn_lag(lag)]
    out = []
    for v in vals:
        if v and v not in out:
            out.append(v)
    return out


def alias_regex(lag):
    return "|".join(re.escape(a) for a in aliases_for(lag) if a)


def tekst_inneholder_lag(tekst, lag):
    lav = (tekst or "").lower()
    return any(a.lower() in lav for a in aliases_for(lag))


def resultat_varianter(h, b):
    return [f"{h}-{b}", f"{h}–{b}", f"{h} - {b}", f"{h} to {b}"]


def resultat_i_tekst(tekst, h, b):
    lav = normaliser_dash((tekst or "").lower())
    return any(v.lower().replace("–", "-") in lav for v in resultat_varianter(h, b))


def fulltekst_matcher_riktig_kamp(tekst, kamp):
    return tekst_inneholder_lag(tekst, kamp["hjemmelag"]) and tekst_inneholder_lag(tekst, kamp["bortelag"])


def total_maal_i_kamp(kamp):
    return int(kamp.get("hjemme", 0) or 0) + int(kamp.get("borte", 0) or 0)


def vinner_lag_for_kamp(kamp):
    h, b = int(kamp.get("hjemme", 0) or 0), int(kamp.get("borte", 0) or 0)
    if h > b:
        return kamp.get("hjemmelag", "")
    if b > h:
        return kamp.get("bortelag", "")
    return ""


def taper_lag_for_kamp(kamp):
    h, b = int(kamp.get("hjemme", 0) or 0), int(kamp.get("borte", 0) or 0)
    if h > b:
        return kamp.get("bortelag", "")
    if b > h:
        return kamp.get("hjemmelag", "")
    return ""

# ── FIFA-PARSER ───────────────────────────────────────────────────────────────

def finn_fifa_kampblokk(tekst, kamp):
    """Returner relevant FIFA-artikkelblokk. Hopper over toppnavigasjon/video."""
    if not tekst:
        return ""
    text = normaliser_dash(tekst)
    h = kamp.get("hjemme")
    b = kamp.get("borte")
    home_re = alias_regex(kamp.get("hjemmelag", ""))
    away_re = alias_regex(kamp.get("bortelag", ""))

    # Beste start er kampresultat-headingen i Jina-markdown.
    patterns = [
        rf"(?im)^\s*>?\s*##\s+\[?\*\*(?:{home_re})\s+{h}\s*-\s*{b}\s+(?:{away_re})\*\*",
        rf"(?im)^\s*>?\s*##\s+\[?\*\*(?:{away_re})\s+{b}\s*-\s*{h}\s+(?:{home_re})\*\*",
        rf"(?im)^\s*>?\s*\*\*(?:{home_re})\s+goals?\*\*\s*:",
        rf"(?im)^\s*>?\s*\*\*(?:{away_re})\s+goals?\*\*\s*:",
    ]
    starts = []
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            starts.append(m.start())
    start = min(starts) if starts else max(0, text.lower().find("close related content"))
    if start < 0:
        start = 0

    # Stopp før sitater/siste nyheter. Behold Key stat og Player of the Match.
    tail = text[start:]
    stop_patterns = [
        r"(?im)^\s*>?\s*###\s+\*\*What they said",
        r"(?im)^\s*>?\s*###\s+Latest FIFA World Cup",
    ]
    stops = []
    for pat in stop_patterns:
        m = re.search(pat, tail)
        if m:
            stops.append(m.start())
    if stops:
        tail = tail[:min(stops)]
    return tail


def rens_spillernavn(navn, hjemme="", borte=""):
    navn = fjern_markdown(navn)
    # Hvis regexen har spist inn i brødtekst, stopp ved vanlige overgangsord.
    navn = re.split(r"(?i)\s+(?:and|before|after|who|which|when|while|with|from|past|into|for)\s+", navn, maxsplit=1)[0]
    navn = re.sub(r"\b(?:goal|goals|pen|penalty|own goal|og|scored|netted|struck|headed)\b", " ", navn, flags=re.I)
    navn = re.sub(r"\s+", " ", navn).strip(" ,;:-–—()[]{}")
    # Fjern lagord hvis regex har spist for mye.
    for lag in [hjemme, borte]:
        for alias in aliases_for(lag):
            navn = re.sub(rf"(?i)^\s*{re.escape(alias)}\s+", "", navn).strip()
    if not navn or len(navn) > 45:
        return ""
    if navn.lower() in {"goal", "goals", "penalty", "own goal", "the goal", "the winner"}:
        return ""
    return navn


def splitt_minutter(s):
    s = (s or "").replace("’", "'").replace(" ", "")
    s = re.sub(r"(?i)pen|penalty|og|owngoal", "", s)
    parts = re.split(r"[,/&]|\band\b|\bog\b", s)
    out = []
    for p in parts:
        m = re.search(r"(\d{1,2}(?:\+\d+)?)", p)
        if m:
            out.append(m.group(1))
    return out


def legg_til_goal_event(events, spiller, lag, minutt="", typ="goal"):
    spiller = rens_spillernavn(spiller)
    if not spiller or not lag:
        return
    ev = {"spiller": spiller, "lag": lag, "minutt": str(minutt or ""), "type": typ or "goal"}
    key = (ev["spiller"].lower(), ev["lag"].lower(), ev["minutt"].lower(), ev["type"].lower())
    for e in events:
        if (e.get("spiller", "").lower(), e.get("lag", "").lower(), e.get("minutt", "").lower(), e.get("type", "").lower()) == key:
            return
    events.append(ev)


def parse_goal_segment(segment, lag, kamp):
    events = []
    segment = fjern_markdown(segment)
    # Stopp ved tydelig overgang til brødtekst hvis segmentet ble for langt.
    segment = re.split(r"(?i)\b(?:The|A|An|After|In|Group|Key stat|Superior Player)\b", segment[:350])[0]
    # Navn (23, 36) / Navn pen (90+4)
    pat = re.compile(
        r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+){0,4})"
        r"(?:\s+(pen|penalty|og|own goal))?\s*\(([^)]*\d[^)]*)\)"
    )
    for m in pat.finditer(segment):
        typ = (m.group(2) or "goal").lower()
        if typ in {"pen"}:
            typ = "penalty"
        for minute in splitt_minutter(m.group(3)):
            legg_til_goal_event(events, m.group(1), lag, minute, typ)
    return events


def ekstraher_goal_events_fra_fifa(blokk, kamp):
    """Parse FIFA-mållinjene: '**Brazil goals:** Cunha (23, 36), Vinicius Jr. (45+3)'."""
    events = []
    if not blokk:
        return events
    text = normaliser_dash(blokk)
    hits = []
    for lag in [kamp.get("hjemmelag"), kamp.get("bortelag")]:
        if not lag:
            continue
        pat = re.compile(rf"(?i)(?:\*\*)?\s*(?:{alias_regex(lag)})\s+goals?(?:\*\*)?\s*:\s*")
        for m in pat.finditer(text):
            hits.append((m.start(), m.end(), lag))
    hits.sort(key=lambda x: x[0])
    for i, (start, end, lag) in enumerate(hits):
        next_hit = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        line_end = text.find("\n", end)
        if line_end == -1:
            line_end = end + 350
        segment_end = min(next_hit, line_end, end + 350)
        segment = text[end:segment_end]
        for ev in parse_goal_segment(segment, lag, kamp):
            legg_til_goal_event(events, ev["spiller"], ev["lag"], ev.get("minutt", ""), ev.get("type", "goal"))

    # Sikker fallback for énmåls-kamper hvis mållinjen ikke ble fanget.
    if not events and total_maal_i_kamp(kamp) == 1:
        vinner = vinner_lag_for_kamp(kamp)
        patterns = [
            r"(?i)(?:solitary|lone|only|winning)\s+goal\s+came\s+courtesy\s+of\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+){0,4})",
            r"(?i)([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+){0,4})\s+(?:scored|netted|struck|fired|headed).*?(?:only|solitary|winning)\s+goal",
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m and vinner:
                ctx = text[max(0, m.start() - 160):m.end() + 160]
                minute = finn_minutt_i_kontekst(ctx)
                legg_til_goal_event(events, m.group(1), vinner, minute, "goal")
                break
    return sorted(events, key=lambda e: minutt_sort_key(e))


def finn_minutt_i_kontekst(ctx):
    ctx = normaliser_dash(ctx or "")
    m = re.search(r"\((\d{1,2}(?:\+\d+)?)\)|\b(\d{1,2})(?:st|nd|rd|th)?\s+minute\b|\b(90\+\d+)\b", ctx, flags=re.I)
    if not m:
        return ""
    return next((g for g in m.groups() if g), "")


def minutt_sort_key(ev):
    txt = str((ev or {}).get("minutt", ""))
    m = re.search(r"\d+", txt)
    return int(m.group(0)) if m else 999


def ekstraher_relevante_avsnitt(blokk, kamp):
    """Hent brødtekstavsnitt etter mål-linjen. Brukes som grunnlag for enkle kampfakta."""
    avsnitt = []
    started = False
    for raw in blokk.splitlines():
        line = strip_quote(raw)
        if not line:
            continue
        clean = fjern_markdown(line)
        low = clean.lower()
        if re.search(r"\bgoals?\s*:", clean, flags=re.I):
            started = True
            continue
        if not started and ("close related content" in low or "watch highlights" in low):
            continue
        if clean.startswith("Group ") or clean.startswith("|") or clean.startswith("Image ") or clean.startswith("Audio ") or clean.startswith("Video "):
            continue
        if "watch highlights" in low or "related content" in low or "share video" in low:
            continue
        if clean.startswith("###") or clean.startswith("***") or clean.startswith("Key stat") or clean.startswith("Superior Player"):
            continue
        if len(clean) < 70:
            continue
        if not (tekst_inneholder_lag(clean, kamp["hjemmelag"]) or tekst_inneholder_lag(clean, kamp["bortelag"]) or re.search(r"\bgoal|scor|lead|level|winner|cross|header|shot|break|second half|stoppage|knockout|group\b", low)):
            continue
        avsnitt.append(clean)
        if len(avsnitt) >= 5:
            break
    return avsnitt


def ekstraher_key_stat(blokk):
    m = re.search(r"(?is)###\s*\*\*Key stat.*?\n\s*>?\s*(.+?)(?:\n\s*\* \* \*|\n\s*>?\s*###|$)", blokk)
    if not m:
        return ""
    stat = fjern_markdown(m.group(1))
    stat = re.sub(r"^Key stat\s*", "", stat, flags=re.I).strip()
    return stat[:450]


def ekstraher_player_of_match(blokk):
    m = re.search(r"(?is)Superior Player of the Match.*?\n\s*>?\s*\*\*([^*\n]+)\*\*\s*(?:\(([^)]+)\))?", blokk)
    if not m:
        return ""
    navn = rens_spillernavn(m.group(1))
    lag = fjern_markdown(m.group(2) or "")
    return f"{navn} ({visningsnavn_lag(lag)})" if navn and lag else navn


def spiller_lag(events, navn):
    for ev in events or []:
        if ev.get("spiller") == navn:
            return ev.get("lag", "")
    return ""


def antall_maal_per_spiller(events):
    data = []
    for ev in events or []:
        navn = ev.get("spiller", "")
        if not navn:
            continue
        item = next((x for x in data if x["spiller"] == navn), None)
        if not item:
            item = {"spiller": navn, "lag": ev.get("lag", ""), "minutter": [], "typer": []}
            data.append(item)
        if ev.get("minutt") and ev.get("minutt") not in item["minutter"]:
            item["minutter"].append(ev.get("minutt"))
        if ev.get("type") and ev.get("type") not in item["typer"]:
            item["typer"].append(ev.get("type"))
    for item in data:
        item["minutter"].sort(key=lambda x: int(re.search(r"\d+", x).group(0)) if re.search(r"\d+", x) else 999)
    return data


def analyser_kampavsnitt(avsnitt, kamp):
    """Små, generiske signaler fra FIFA-brødteksten."""
    samlet = " ".join(avsnitt)
    low = samlet.lower()
    return {
        "early_lead": bool(re.search(r"\bearly lead|inside five minutes|after .*?minute|fastest", low)),
        "came_from_behind": bool(re.search(r"come from behind|came from behind|from behind", low)),
        "knockout_secured": bool(re.search(r"advance to the knockouts|through to the .*round|through to the knockouts|see .* through", low)),
        "group_top": bool(re.search(r"top of group|summit of group|edged ahead .* goal difference|leapfrog", low)),
        "hit_bar": "crossbar" in low or "off the bar" in low,
        "late_pressure": bool(re.search(r"final 15 minutes|late pressure|piled on the pressure", low)),
        "stoppage": "stoppage time" in low or re.search(r"90\+\d+", samlet),
        "brace_text": "brace" in low or "scored twice" in low or "netted twice" in low,
    }



def minutt_nummer(minutt):
    m = re.search(r"\d+", str(minutt or ""))
    return int(m.group(0)) if m else None


def minutt_er_for_pause(minutt):
    n = minutt_nummer(minutt)
    return n is not None and n <= 45


def finn_scoringsutvikling(kamp, events):
    """Returnerer score etter hvert mål og enkel kampdynamikk."""
    hlag, blag = kamp.get("hjemmelag"), kamp.get("bortelag")
    hscore = bscore = 0
    utvikling = []
    for ev in sorted(events or [], key=minutt_sort_key):
        if ev.get("lag") == hlag:
            hscore += 1
        elif ev.get("lag") == blag:
            bscore += 1
        utvikling.append({**ev, "score_h": hscore, "score_b": bscore})
    return utvikling


def ekstraher_navn_etter(pattern, tekst):
    m = re.search(pattern, tekst or "", flags=re.I)
    if not m:
        return ""
    return rens_spillernavn(m.group(1))


def ekstraher_malkontekst(blokk, kamp, events, avsnitt):
    """Generiske fakta fra FIFA-brødtekst som kan brukes i norsk referat.

    Dette er bevisst ikke spillerspesifikt. Det leter etter faste fotballformer i
    FIFA-teksten: created the first goal, pass/cross from X, fastest, pressure osv.
    """
    samlet = " ".join([fjern_markdown(x) for x in ([blokk or ""] + (avsnitt or []))])
    low = samlet.lower()
    first = sorted(events or [], key=minutt_sort_key)[0] if events else {}
    first_scorer = first.get("spiller", "")
    context = {}

    # Forarbeid til åpningsmålet. Bruk bare trygge mønstre.
    assist = ""
    if first_scorer:
        # X created the first goal / X set up the opener.
        assist = ekstraher_navn_etter(
            r"([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+){0,4})\s+(?:created|made|set up|teed up)\s+(?:the\s+)?(?:first|opening|opener)",
            samlet,
        )
        # well-weighted ball/pass/cross from X ... scorer
        if not assist:
            scorer_pat = re.escape(first_scorer.split()[0])
            assist = ekstraher_navn_etter(
                rf"(?:ball|pass|cross|cut-back|delivery)\s+from\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+){{0,4}}).{{0,180}}{scorer_pat}",
                samlet,
            )
        # scorer ... from X's pass/cross
        if not assist:
            scorer_pat = re.escape(first_scorer.split()[0])
            assist = ekstraher_navn_etter(
                rf"{scorer_pat}.{{0,180}}(?:from|after)\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+){{0,4}})(?:'s|’s)?\s+(?:pass|cross|cut-back|delivery|ball)",
                samlet,
            )
    if assist and assist != first_scorer:
        context["first_goal_assist"] = assist

    context["first_goal_past_keeper"] = bool(re.search(r"\bpast\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+", samlet))
    context["all_goals_first_half"] = bool(events) and all(minutt_er_for_pause(e.get("minutt")) for e in events)
    context["lead_goal_early"] = minutt_nummer(first.get("minutt")) is not None and minutt_nummer(first.get("minutt")) <= 10
    context["lead_goal_stoppage"] = "+" in str(first.get("minutt", ""))

    utvikling = finn_scoringsutvikling(kamp, events)
    vinner = vinner_lag_for_kamp(kamp)
    if utvikling and vinner:
        first_team = utvikling[0].get("lag")
        context["winner_trailed_first"] = first_team and first_team != vinner
        last = utvikling[-1]
        last_min = minutt_nummer(last.get("minutt")) or 0
        context["late_winner"] = last.get("lag") == vinner and (last_min >= 85 or str(last.get("minutt", "")).startswith("90+"))
        # Siste mål avgjør hvis stillingen var uavgjort før scoringen og vinnerlaget scoret.
        if len(utvikling) >= 1:
            before_h = last.get("score_h", 0) - (1 if last.get("lag") == kamp.get("hjemmelag") else 0)
            before_b = last.get("score_b", 0) - (1 if last.get("lag") == kamp.get("bortelag") else 0)
            context["deciding_last_goal"] = last.get("lag") == vinner and before_h == before_b

    context["fastest_stat"] = "fastest" in low
    context["record_stat"] = "record" in low or "fastest ever" in low
    context["eliminated_loser"] = bool(re.search(r"eliminat(?:ed|ion)|cannot reach|knocked out", low))
    return context

def ekstraher_fakta_fra_fulltekst(tekst, kamp, kilde_url):
    blokk = finn_fifa_kampblokk(tekst, kamp)
    goal_events = ekstraher_goal_events_fra_fifa(blokk, kamp)
    avsnitt = ekstraher_relevante_avsnitt(blokk, kamp)
    signaler = analyser_kampavsnitt(avsnitt, kamp)
    context = ekstraher_malkontekst(blokk, kamp, goal_events, avsnitt)
    fakta = {
        "status": "ok",
        "versjon": FULLTEKST_CACHE_VERSION,
        "kilde_url": kilde_url,
        "kilde_tittel": "",
        "domene": domene_fra_url(kilde_url),
        "source_type": "article_report",
        "fulltekst_riktig_kamp": fulltekst_matcher_riktig_kamp(tekst, kamp),
        "resultat_i_fulltekst": resultat_i_tekst(tekst, kamp["hjemme"], kamp["borte"]),
        "kamp_hjemme_score": kamp["hjemme"],
        "kamp_borte_score": kamp["borte"],
        "score": 10 if blokk else 0,
        "hentet": iso_utc_na(),
        "goal_events": goal_events,
        "scorere": [x["spiller"] for x in antall_maal_per_spiller(goal_events)],
        "lead_scorer": goal_events[0]["spiller"] if goal_events else "",
        "brace_scorer": next((x["spiller"] for x in antall_maal_per_spiller(goal_events) if len(x["minutter"]) >= 2), ""),
        "hat_trick_player": next((x["spiller"] for x in antall_maal_per_spiller(goal_events) if len(x["minutter"]) >= 3), ""),
        "article_paragraphs": avsnitt[:6],
        "key_stat": ekstraher_key_stat(blokk),
        "player_of_match": ekstraher_player_of_match(blokk),
        **signaler,
        **context,
    }
    fakta["referat_fakta_score"] = referat_fakta_score(fakta, kamp)
    fakta["mangler"] = referat_fakta_mangler(fakta, kamp)
    fakta["rikt_fulltekstgrunnlag"] = fulltekst_kriterier_mott(fakta, kamp)
    return fakta

# ── KRITERIER ─────────────────────────────────────────────────────────────────

def referat_fakta_mangler(fakta, kamp):
    mangler = []
    if not isinstance(fakta, dict) or fakta.get("status") != "ok":
        return ["fulltekst"]
    if fakta.get("domene") != "fifa.com":
        mangler.append("fifa_fulltekst")
    if not fakta.get("fulltekst_riktig_kamp"):
        mangler.append("riktig_kamp")
    if not fakta.get("resultat_i_fulltekst"):
        mangler.append("resultat_i_fulltekst")
    total = total_maal_i_kamp(kamp)
    if total > 0 and len(fakta.get("goal_events") or []) < total:
        mangler.append("goal_events")
    if total == 0 and not fakta.get("article_paragraphs"):
        mangler.append("kampavsnitt")
    return mangler


def fulltekst_kriterier_mott(fakta, kamp):
    return not referat_fakta_mangler(fakta, kamp)


def referat_fakta_score(fakta, kamp):
    if not isinstance(fakta, dict) or fakta.get("status") != "ok":
        return 0
    score = 0
    if fakta.get("fulltekst_riktig_kamp"):
        score += 2
    if fakta.get("resultat_i_fulltekst"):
        score += 2
    total = total_maal_i_kamp(kamp)
    if total:
        score += min(6, len(fakta.get("goal_events") or []) * 2)
    if fakta.get("article_paragraphs"):
        score += 2
    if fakta.get("key_stat"):
        score += 1
    if fakta.get("player_of_match"):
        score += 1
    return score

# ── FULLTEKST-KJØRING/CACHE ──────────────────────────────────────────────────

def hent_fulltekst_fakta_for_kamp(kamp, start_index=0):
    urls = bygg_fifa_article_urls(kamp)
    print("  → FIFA-kandidater: " + "; ".join([domene_fra_url(u) + ":article_report" for u in urls[:4]]))
    beste = None
    forsok = 0
    for url in urls[start_index:]:
        forsok += 1
        print(f"  → Fulltekst: prøver fifa.com (article_report) {url}")
        tekst = hent_url_tekst(url)
        if not tekst:
            continue
        fakta = ekstraher_fakta_fra_fulltekst(tekst, kamp, url)
        fakta["fifa_direct_probe_url"] = url
        fakta["direct_fifa_probe"] = True
        fakta["_forsok_url"] = forsok
        if not beste or fakta.get("referat_fakta_score", 0) > beste.get("referat_fakta_score", 0):
            beste = fakta
        if fulltekst_kriterier_mott(fakta, kamp):
            print(f"  → Fulltekst: fifa.com OK, fakta_score {fakta['referat_fakta_score']}")
            return fakta, forsok
        print(f"    Fulltekst: relevant, men kriterier ikke møtt (fakta_score {fakta.get('referat_fakta_score', 0)}, mangler: {', '.join(fakta.get('mangler', []))})")
    if beste:
        return beste, forsok
    return {
        "status": "ikke_funnet",
        "versjon": FULLTEKST_CACHE_VERSION,
        "hentet": iso_utc_na(),
        "score": -100,
        "referat_fakta_score": 0,
        "mangler": ["fulltekst"],
        "_forsok_url": forsok,
    }, forsok


def oppdater_fulltekst_i_cache(kamp, cache, fulltekst_teller):
    key = cache_noekkel(kamp.get("kamp_id"), kamp.get("hjemme"), kamp.get("borte"))
    entry = cache.get(key) or {}
    fakta = (entry.get("fulltekst_fakta") or {}) if isinstance(entry, dict) else {}
    if cache_har_ferdig_fifa(entry, kamp):
        print("  → FIFA/fulltekst cache hit: kriterier møtt")
        return entry, fulltekst_teller
    if fulltekst_teller >= MAX_FULLTEKST_PER_KJORING:
        print("  → Fulltekst: hopper over, maks fulltekstforsøk brukt")
        return entry, fulltekst_teller
    fakta, forsok = hent_fulltekst_fakta_for_kamp(kamp)
    fulltekst_teller += int(forsok or 0)
    entry.update({
        "kamp_id": kamp.get("kamp_id"),
        "resultat": f"{kamp.get('hjemme')}-{kamp.get('borte')}",
        "sist_sokt": iso_utc_na(),
        "antall_sok": int(entry.get("antall_sok", 0) or 0) + 1,
        "beste_score": fakta.get("referat_fakta_score", 0),
        "status": "ok" if fulltekst_kriterier_mott(fakta, kamp) else "forelopig_fifa_kriterier_ikke_mott",
        "query": "direct_fifa_article_probe",
        "kandidater": [],
        "fulltekst_fakta": fakta,
    })
    cache[key] = entry
    return entry, fulltekst_teller

# ── NORSK REFERAT ─────────────────────────────────────────────────────────────

def resultatsetning(hjemme, borte, h, b):
    hjemme_n = visningsnavn_lag(hjemme)
    borte_n = visningsnavn_lag(borte)
    if h > b:
        return f"{hjemme_n} slo {borte_n} {h}–{b}."
    if b > h:
        return f"{borte_n} slo {hjemme_n} {b}–{h}."
    return f"{hjemme_n} og {borte_n} spilte {h}–{b}."


def minutt_til_norsk(minutt):
    if not minutt:
        return ""
    m = str(minutt).replace("'", "")
    if "+" in m:
        return f"på overtid ({m})"
    try:
        n = int(m)
        if n <= 10:
            return f"allerede i det {n}. minutt"
        return f"i det {n}. minutt"
    except Exception:
        return m


def join_navn(items):
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " og " + items[-1]


def grupper_goal_events(events):
    grupper = []
    for ev in sorted(events or [], key=minutt_sort_key):
        g = next((x for x in grupper if x["spiller"] == ev.get("spiller") and x["lag"] == ev.get("lag")), None)
        if not g:
            g = {"spiller": ev.get("spiller", ""), "lag": ev.get("lag", ""), "minutter": [], "typer": []}
            grupper.append(g)
        if ev.get("minutt") and ev.get("minutt") not in g["minutter"]:
            g["minutter"].append(ev.get("minutt"))
        if ev.get("type") and ev.get("type") not in g["typer"]:
            g["typer"].append(ev.get("type"))
    return grupper


def spillergruppe_tekst(g):
    navn = g.get("spiller", "")
    mins = g.get("minutter") or []
    suffix = " på straffe" if "penalty" in (g.get("typer") or []) else ""
    if len(mins) >= 2:
        return f"{navn}{suffix} ({join_navn(mins)})"
    if len(mins) == 1:
        return f"{navn}{suffix} ({mins[0]})"
    return f"{navn}{suffix}"


def spillergruppe_tekst_med_og(g):
    navn = g.get("spiller", "")
    mins = g.get("minutter") or []
    suffix = " på straffe" if "penalty" in (g.get("typer") or []) else ""
    if len(mins) >= 2:
        return f"{navn}{suffix} ({' og '.join(mins)})"
    if len(mins) == 1:
        return f"{navn}{suffix} ({mins[0]})"
    return f"{navn}{suffix}"


def splitt_artikkelsetninger(avsnitt):
    """Returner rensede setninger fra FIFA-brødtekst.

    Målet er å bruke de konkrete avsnittene FIFA gjør tilgjengelig som
    kampreferatgrunnlag, ikke bare som små signaler for scoring/fakta.
    """
    out = []
    for p in avsnitt or []:
        p = fjern_markdown(p)
        p = re.sub(r"\s+", " ", p).strip()
        if not p:
            continue
        for s in re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-Þ])", p):
            s = s.strip()
            if len(s) >= 45:
                out.append(s)
    return out


def finn_setning_med(setninger, *patterns):
    for s in setninger or []:
        low = s.lower()
        ok = True
        for pat in patterns:
            if not pat:
                continue
            if isinstance(pat, str):
                if pat.lower() not in low:
                    ok = False
                    break
            elif hasattr(pat, "search"):
                if not pat.search(s):
                    ok = False
                    break
            else:
                if not re.search(pat, s, flags=re.I):
                    ok = False
                    break
        if ok:
            return s
    return ""


def finn_setning_med_spiller(setninger, spiller, ekstra_regex=""):
    if not spiller:
        return ""
    navn_pat = re.escape(spiller.split()[0])
    for s in setninger or []:
        if not re.search(navn_pat, s, flags=re.I):
            continue
        if ekstra_regex and not re.search(ekstra_regex, s, flags=re.I):
            continue
        return s
    return ""


def navn_fra_regex(pattern, text):
    m = re.search(pattern, text or "", flags=re.I)
    if not m:
        return ""
    return rens_spillernavn(m.group(1)).rstrip(".")


def keeper_fra_kontekst(text):
    patterns = [
        r"past\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+){0,3})",
        r"beyond\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+){0,3})",
        r"(?:goalkeeper|keeper)\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+){0,3})",
        r"not\s+held\s+by\s+([A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+){0,3})",
    ]
    for pat in patterns:
        navn = navn_fra_regex(pat, text)
        if navn:
            return navn
    return ""


def norsk_forste_mal_detalj(kamp, fakta, setninger):
    events = sorted(fakta.get("goal_events") or [], key=minutt_sort_key)
    if not events:
        return ""
    first = events[0]
    scorer = first.get("spiller", "")
    lag = first.get("lag", "")
    assist = fakta.get("first_goal_assist", "")
    ctx = finn_setning_med_spiller(
        setninger,
        scorer,
        r"goal|scor|lead|opener|courtesy|finish|finished|slammed|pounced|rebound|created|pass|cross|ball|header",
    )
    if not ctx and assist:
        ctx = finn_setning_med_spiller(setninger, assist, r"created|set up|pass|cross|ball|rebound|not held")

    tid = minutt_til_norsk(first.get("minutt", ""))
    tid_txt = f" {tid}" if tid else ""
    lag_txt = visningsnavn_lag(lag)
    keeper = keeper_fra_kontekst(ctx)
    low = (ctx or "").lower()

    if "not held" in low or "rebound" in low or "pounced" in low:
        if assist and assist != scorer:
            return f"Åpningsmålet kom{tid_txt} etter at {assist} var involvert i forarbeidet, før {scorer} var først på returen."
        return f"Åpningsmålet kom{tid_txt} da {scorer} var våken på en retur."

    if assist and assist != scorer:
        if "well-weighted" in low or "ball from" in low or "pass" in low or "cross" in low or "delivery" in low:
            avslutning = f" og avsluttet forbi {keeper}" if keeper else " og avsluttet sikkert"
            return f"Forarbeidet kom fra {assist}, før {scorer} tok vare på muligheten{tid_txt}{avslutning}."
        return f"{scorer} sendte {lag_txt} i ledelsen{tid_txt} etter forarbeid fra {assist}."

    if total_maal_i_kamp(kamp) == 1:
        return f"{scorer} scoret kampens eneste mål{tid_txt} for {lag_txt}."
    return f"{scorer} sendte {lag_txt} i ledelsen{tid_txt}."


def bygg_artikkelbaserte_setninger(kamp, fakta):
    """Bygg norske referatsetninger fra FIFA-brødteksten.

    Dette er ikke en full maskinoversettelse. Det er en kontrollert komprimering
    av kampavsnittene: forarbeid, returer, treverk, sluttpress og konsekvens.
    """
    setninger = splitt_artikkelsetninger(fakta.get("article_paragraphs") or [])
    events = sorted(fakta.get("goal_events") or [], key=minutt_sort_key)
    out = []
    if not setninger or not events:
        return out

    vinner = vinner_lag_for_kamp(kamp)
    taper = taper_lag_for_kamp(kamp)
    vinner_txt = visningsnavn_lag(vinner) if vinner else ""
    taper_txt = visningsnavn_lag(taper) if taper else ""

    first_detail = norsk_forste_mal_detalj(kamp, fakta, setninger)
    if first_detail:
        out.append(first_detail)

    brace = fakta.get("brace_scorer", "")
    if brace:
        gruppe = next((g for g in grupper_goal_events(events) if g.get("spiller") == brace), None)
        fase = maal_fase_tekst((gruppe or {}).get("minutter") or [])
        if fase == "før pause":
            out.append(f"{brace} ble sentral i at kampen i praksis ble satt opp før pause, med to scoringer før hvilen.")
        elif fase == "etter pause":
            out.append(f"{brace} ble avgjørende etter pause, med to scoringer som endret kampbildet.")
        else:
            out.append(f"{brace} ble en av kampens store profiler med to scoringer.")

    if any(str(e.get("minutt", "")).startswith("45+") for e in events):
        ev = next(e for e in events if str(e.get("minutt", "")).startswith("45+"))
        if taper_txt:
            out.append(f"{ev.get('spiller')} økte ledelsen på overtid i første omgang og gjorde oppgaven enda tyngre for {taper_txt}.")

    if fakta.get("winner_trailed_first") and vinner and taper:
        out.append(f"{taper_txt} fikk kampen dit de ønsket med ledelse først, men {vinner_txt} slo tilbake og snudde oppgjøret.")

    # Ikke skriv om store sjanser, treverk eller sluttpress basert på generiske
    # engelske nøkkelord alene. Slike hendelser skal bare inn i referatet hvis
    # parseren senere kan knytte dem til konkret spiller, lag, handling og fase.
    # Dette hindrer at scriptet publiserer antakelser som ikke er sikkert forankret
    # i kampen, f.eks. samme treverk-/sluttpress-setning på flere ulike kamper.

    alltext = " ".join(setninger).lower()
    if re.search(r"eliminat(?:ed|ion)|cannot reach|knocked out", alltext) and taper_txt:
        out.append(f"Resultatet gjorde samtidig at {taper_txt} mistet muligheten til å gå videre.")
    elif re.search(r"top of group|top spot|moved top|goal difference", alltext) and vinner_txt:
        out.append(f"Med seieren styrket {vinner_txt} posisjonen i gruppen.")
    elif re.search(r"knockout|round of 32|last 32|progress", alltext) and vinner_txt:
        out.append(f"Seieren ga også {vinner_txt} et viktig steg mot utslagsrundene.")

    return unike_setninger(out)

def bygg_maalsetninger(kamp, fakta):
    events = fakta.get("goal_events") or []
    if not events:
        return []
    h, b = kamp["hjemme"], kamp["borte"]
    vinner = vinner_lag_for_kamp(kamp)
    taper = taper_lag_for_kamp(kamp)
    setninger = []

    if h + b == 1 and vinner:
        ev = sorted(events, key=minutt_sort_key)[0]
        tid = minutt_til_norsk(ev.get("minutt", ""))
        tid_txt = f" {tid}" if tid else ""
        assist = fakta.get("first_goal_assist", "")
        if assist:
            setninger.append(
                f"{ev['spiller']} scoret kampens eneste mål{tid_txt} for {visningsnavn_lag(vinner)}, etter forarbeid fra {assist}."
            )
        else:
            setninger.append(f"{ev['spiller']} scoret kampens eneste mål{tid_txt} for {visningsnavn_lag(vinner)}.")
        return setninger

    if h != b and vinner:
        win_events = [ev for ev in events if ev.get("lag") == vinner]
        lose_events = [ev for ev in events if ev.get("lag") == taper]
        win_txt = join_navn([spillergruppe_tekst_med_og(g) for g in grupper_goal_events(win_events)])
        lose_txt = join_navn([spillergruppe_tekst_med_og(g) for g in grupper_goal_events(lose_events)])
        if win_txt and lose_txt:
            setninger.append(f"Målene til {visningsnavn_lag(vinner)} kom ved {win_txt}, mens {lose_txt} scoret for {visningsnavn_lag(taper)}.")
        elif win_txt:
            setninger.append(f"Målene til {visningsnavn_lag(vinner)} kom ved {win_txt}.")
    else:
        htxt = join_navn([spillergruppe_tekst_med_og(g) for g in grupper_goal_events([ev for ev in events if ev.get("lag") == kamp["hjemmelag"]])])
        btxt = join_navn([spillergruppe_tekst_med_og(g) for g in grupper_goal_events([ev for ev in events if ev.get("lag") == kamp["bortelag"]])])
        if htxt and btxt:
            setninger.append(f"{htxt} scoret for {visningsnavn_lag(kamp['hjemmelag'])}, mens {btxt} scoret for {visningsnavn_lag(kamp['bortelag'])}.")
    return setninger


def maal_fase_tekst(minutter):
    nums = [minutt_nummer(m) for m in minutter if minutt_nummer(m) is not None]
    if not nums:
        return ""
    if all(n <= 45 for n in nums):
        return "før pause"
    if all(n > 45 for n in nums):
        return "etter pause"
    return ""


def bygg_kampforlop_fra_events(kamp, fakta):
    events = sorted(fakta.get("goal_events") or [], key=minutt_sort_key)
    if not events:
        return []
    setninger = []
    vinner = vinner_lag_for_kamp(kamp)
    taper = taper_lag_for_kamp(kamp)
    utvikling = finn_scoringsutvikling(kamp, events)
    first = events[0]
    first_team = first.get("lag")

    if fakta.get("winner_trailed_first") and vinner and taper:
        setninger.append(
            f"{visningsnavn_lag(taper)} tok ledelsen ved {first.get('spiller')} {minutt_til_norsk(first.get('minutt'))}, men {visningsnavn_lag(vinner)} svarte og snudde kampen."
        )
    elif fakta.get("lead_goal_early") and first_team and total_maal_i_kamp(kamp) > 1:
        setninger.append(
            f"Kampen fikk en tidlig retning da {first.get('spiller')} sendte {visningsnavn_lag(first_team)} i ledelsen {minutt_til_norsk(first.get('minutt'))}."
        )
    elif fakta.get("all_goals_first_half") and len(events) >= 2:
        setninger.append("Kampen ble i stor grad avgjort før pause, der alle scoringene kom før lagene gikk i garderoben.")

    # Trekk fram dobbeltscorere/hat trick på en kontrollert måte.
    flermal = [g for g in grupper_goal_events(events) if len(g.get("minutter") or []) >= 2]
    for g in flermal[:2]:
        fase = maal_fase_tekst(g.get("minutter") or [])
        fase_txt = f" {fase}" if fase else ""
        ant = "tre" if len(g.get("minutter") or []) >= 3 else "to"
        if g.get("spiller") not in " ".join(setninger):
            setninger.append(f"{g.get('spiller')} ble en av kampens store profiler med {ant} scoringer{fase_txt}.")

    if fakta.get("late_winner") and vinner:
        last = events[-1]
        setninger.append(f"Avgjørelsen kom sent, da {last.get('spiller')} scoret {minutt_til_norsk(last.get('minutt'))}.")
    elif fakta.get("deciding_last_goal") and vinner and len(events) >= 3:
        last = events[-1]
        setninger.append(f"{last.get('spiller')} satte inn målet som til slutt skilte lagene.")

    # Hvis favoritten leder stort før pause, skriv det konkret.
    if not any("pause" in s for s in setninger) and len(events) >= 3:
        first_half = [e for e in events if minutt_er_for_pause(e.get("minutt"))]
        if len(first_half) >= 3:
            setninger.append("Med flere scoringer før pause hadde kampen fått et tydelig preg allerede halvveis.")

    return unike_setninger(setninger)


def bygg_kampbilde_setninger(kamp, fakta):
    setninger = []
    vinner = vinner_lag_for_kamp(kamp)
    taper = taper_lag_for_kamp(kamp)

    # Ikke publiser generiske treverk-/sjanse-/sluttpress-setninger fra boolean-flagg.
    # Disse flaggene er for svake alene og kan gi hendelser som ikke faktisk skjedde
    # i den konkrete kampen.
    if fakta.get("eliminated_loser") and taper:
        setninger.append(f"Resultatet gjorde samtidig veien videre svært vanskelig for {visningsnavn_lag(taper)}.")
    if fakta.get("knockout_secured") and vinner:
        setninger.append(f"Seieren sikret samtidig {visningsnavn_lag(vinner)} plass i utslagsrunden.")
    elif fakta.get("group_top") and vinner:
        setninger.append(f"Resultatet sender {visningsnavn_lag(vinner)} opp i en sterk posisjon i gruppen.")

    if not setninger and vinner:
        if abs(kamp["hjemme"] - kamp["borte"]) >= 3:
            setninger.append(f"{visningsnavn_lag(vinner)} kontrollerte etter hvert kampen og tok en klar seier.")
        else:
            setninger.append(f"{visningsnavn_lag(vinner)} holdt unna og tok tre viktige poeng.")
    return unike_setninger(setninger)


def unike_setninger(setninger):
    out = []
    seen = set()
    for s in setninger or []:
        s = re.sub(r"\s+", " ", str(s)).strip()
        if not s:
            continue
        key = re.sub(r"[^a-z0-9æøå]+", "", s.lower())[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def oversett_key_stat_enkel(stat, kamp, fakta=None):
    """Bruk bare trygge, gjenkjennelige FIFA Key stat-mønstre.

    Hvis vi ikke kjenner mønsteret, returnerer vi tomt heller enn å lage dårlig
    blanding av engelsk og norsk.
    """
    if not stat:
        return ""
    fakta = fakta or {}
    low = stat.lower()
    first_scorer = (fakta.get("lead_scorer") or "").strip()
    vinner = vinner_lag_for_kamp(kamp)

    if "fastest" in low:
        if first_scorer:
            if "fastest so far" in low and "fastest ever" in low:
                return f"FIFA trakk fram at scoringen til {first_scorer} både var den raskeste i VM så langt og {visningsnavn_lag(vinner)}s raskeste VM-scoring noensinne."
            return f"FIFA trakk fram den tidlige scoringen til {first_scorer} som en av kampens nøkkelstatistikker."
        return "FIFA trakk fram den tidlige scoringen som en av kampens nøkkelstatistikker."

    if "first world cup goal" in low or "first fifa world cup goal" in low:
        scorer = fakta.get("brace_scorer") or first_scorer
        if scorer:
            return f"FIFA pekte også på at dette ga {scorer} hans første VM-scoring."

    if "overtook germany" in low or "top-scoring nation" in low or "most goals" in low:
        if kamp.get("hjemmelag") == "Brazil" or kamp.get("bortelag") == "Brazil":
            return "FIFA pekte også på at Brasil med scoringen passerte Tyskland som mestscorende nasjon i VM-historien."

    if "substitute" in low and fakta.get("brace_scorer"):
        return f"FIFA trakk også fram innhoppet til {fakta['brace_scorer']} som avgjørende."

    return ""


def bygg_historikkavsnitt(ref):
    historikk = (ref or {}).get("historikk", "")
    fakta = (ref or {}).get("fakta", []) or []
    linjer = []
    if historikk:
        linjer.append(historikk.rstrip("."))
    if fakta:
        linjer.append(str(fakta[0]).rstrip("."))
    return ". ".join([l for l in linjer if l]) + "." if linjer else ""


def bygg_forelopig_recap(kamp):
    return "\n".join([
        resultatsetning(kamp["hjemmelag"], kamp["bortelag"], kamp["hjemme"], kamp["borte"]),
        "Utfyllende kampreferat oppdateres automatisk når full måloversikt er tilgjengelig fra FIFA.",
    ])


def bygg_recap_tekst(kamp, ref, fakta):
    if not fulltekst_kriterier_mott(fakta, kamp):
        return bygg_forelopig_recap(kamp)

    setninger = [resultatsetning(kamp["hjemmelag"], kamp["bortelag"], kamp["hjemme"], kamp["borte"])]

    # Bruk FIFA-brødteksten som faktisk referatgrunnlag, ikke bare til kriteriesjekk.
    artikkel_setninger = bygg_artikkelbaserte_setninger(kamp, fakta)

    # For énmålskamper er den artikkelbaserte første-mål-setningen normalt mer
    # presis enn den rene mållinjen, fordi den kan ta med forarbeid/keeper/sluttpress.
    if total_maal_i_kamp(kamp) == 1 and artikkel_setninger:
        setninger.extend(artikkel_setninger)
    else:
        setninger.extend(bygg_maalsetninger(kamp, fakta))
        setninger.extend(artikkel_setninger)

    # Bruk de mer generiske event-/kampbildesetningene bare når artikkelflyten
    # ikke gir nok. Dette hindrer at rike FIFA-avsnitt erstattes av fylltekst.
    if len(artikkel_setninger) < 3:
        setninger.extend(bygg_kampforlop_fra_events(kamp, fakta))
        setninger.extend(bygg_kampbilde_setninger(kamp, fakta))

    key_stat = oversett_key_stat_enkel(fakta.get("key_stat", ""), kamp, fakta)
    if key_stat:
        setninger.append(key_stat)

    if fakta.get("player_of_match"):
        setninger.append(f"{fakta['player_of_match']} ble kåret til kampens spiller hos FIFA.")

    setninger = unike_setninger(setninger)

    # Når FIFA-artikkelen er rik, skal også referatet være rikere: 5–8 setninger.
    hoved = setninger[:3]
    detaljer = setninger[3:8]
    avsnitt = []
    if hoved:
        avsnitt.append("\n".join(hoved))
    if detaljer:
        avsnitt.append("\n".join(detaljer))

    if len(setninger) < 5:
        hist = bygg_historikkavsnitt(ref)
        if hist:
            avsnitt.append(hist)
    return "\n\n".join(avsnitt)

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("VM 2026 Kampreferat-generator")
    print("=" * 60)

    igaar = norsk_dato_igaar()
    igaar_str = str(igaar)
    print(f"\nGårsdagens dato (norsk tid): {igaar_str}")

    eksisterende = les_eksisterende_kamppost()
    eksisterende_for_dato = eksisterende if eksisterende and eksisterende.get("dato") == igaar_str else None

    print("\nLeser data/data.js...")
    vm_data = les_data_js()
    print("Leser data/kamp-referanser.json...")
    referanser = les_referanser()
    cache = les_cache()

    print(f"\nFinner kamper fra {igaar_str} (norsk tid) + ikke-OK retry-kamper siste {RETRY_SISTE_TIMER} timer...")
    arbeidskamper, rapport_ids, retry_ekstra = finn_kamper_for_rapport_og_retry(vm_data, cache, igaar_str)
    if not arbeidskamper:
        print(f"  → Ingen ferdigspilte kamper for {igaar_str}, og ingen retry-kamper. Avslutter.")
        return
    print(f"  → {len(rapport_ids)} rapportkamper funnet")
    if retry_ekstra:
        print(f"  → {retry_ekstra} ekstra ikke-OK kamp(er) prosesseres kun for cache/retry")

    kamposter = []
    fulltekst_teller = 0

    for i, kamp in enumerate(arbeidskamper, 1):
        hjemme = kamp["hjemmelag"]
        borte = kamp["bortelag"]
        h_score = kamp["hjemme"]
        b_score = kamp["borte"]
        kamp_id = kamp["kamp_id"]
        retry_only = kamp_id not in rapport_ids
        suffix = " (kun retry/cache)" if retry_only else ""
        print(f"\n[{i}/{len(arbeidskamper)}] {hjemme} {h_score}-{b_score} {borte}{suffix}")

        ref = referanser.get(kampreferat_noekkel(hjemme, borte), {})
        tippinger = hent_tippinger_for_kamp(vm_data, kamp_id)
        cache_entry, fulltekst_teller = oppdater_fulltekst_i_cache(kamp, cache, fulltekst_teller)
        fakta = cache_entry.get("fulltekst_fakta", {}) if isinstance(cache_entry, dict) else {}
        kriterier_ok = fulltekst_kriterier_mott(fakta, kamp)
        recap_status = "ok" if kriterier_ok else "forelopig_fifa_kriterier_ikke_mott"
        referat_score = fakta.get("referat_fakta_score", 0) if isinstance(fakta, dict) else 0

        print(f"  Eksakt: {len(tippinger['eksakt'])} | Riktig: {len(tippinger['riktig'])} | Bom: {len(tippinger['bom'])}")
        print(f"  Recap-kvalitet: status={recap_status}, fakta_score={referat_score}, fallback={not kriterier_ok}")

        if retry_only:
            time.sleep(0.15)
            continue

        kamposter.append({
            "kamp_id": kamp_id,
            "hjemmelag": hjemme,
            "bortelag": borte,
            "hjemme_score": h_score,
            "borte_score": b_score,
            "gruppe": ref.get("gruppe", kamp.get("gruppe", "")),
            "recap_tekst": bygg_recap_tekst(kamp, ref, fakta),
            "snippets_raa": [],
            "serper_kandidater": [],
            "recap_kvalitet": {
                "score": referat_score,
                "status": recap_status,
                "grunnlag": "fifa_fulltekst" if kriterier_ok else "forelopig",
                "source_type": fakta.get("source_type", "") if isinstance(fakta, dict) else "",
                "antall_sok": cache_entry.get("antall_sok", 0) if isinstance(cache_entry, dict) else 0,
                "fallback": not kriterier_ok,
                "cache_key": cache_noekkel(kamp_id, h_score, b_score),
                "serper_score": -100,
                "fulltekst_status": fakta.get("status", "ikke_sokt") if isinstance(fakta, dict) else "ikke_sokt",
                "fulltekst_kilde": fakta.get("domene", "") if isinstance(fakta, dict) else "",
                "fulltekst_score": fakta.get("score", -100) if isinstance(fakta, dict) else -100,
                "referat_fakta_score": referat_score,
                "venter_pa_bedre_kilde": not kriterier_ok,
                "next_retry_at": "",
                "retry_antall": cache_entry.get("antall_sok", 0) if isinstance(cache_entry, dict) else 0,
                "direct_fifa_probe": bool(fakta.get("direct_fifa_probe", False)) if isinstance(fakta, dict) else False,
                "fifa_direct_probe_url": fakta.get("fifa_direct_probe_url", "") if isinstance(fakta, dict) else "",
                "mangler": fakta.get("mangler", referat_fakta_mangler(fakta, kamp)) if isinstance(fakta, dict) else ["fulltekst"],
                "offisiell_kilde_sokt": [],
            },
            "tippinger": tippinger,
        })
        time.sleep(0.15)

    kamppost = {
        "dato": igaar_str,
        "dato_norsk": formater_norsk_dato(igaar_str),
        "generert": iso_utc_na(),
        "antall_kamper": len(kamposter),
        "kamper": kamposter,
        "stilling": bygg_stilling(vm_data, set(rapport_ids)),
    }

    if eksisterende_for_dato and uten_generert(eksisterende_for_dato) == uten_generert(kamppost):
        kamppost["generert"] = eksisterende_for_dato.get("generert", kamppost["generert"])
        print("\n✓ Kamppost regenerert, men innholdet er uendret. Beholder eksisterende generert-tidspunkt.")
    else:
        print(f"\n✓ Kamppost regenerert med endringer for {igaar_str}")

    skriv_json(KAMPPOST_JSON, kamppost)
    skriv_json(SERPER_CACHE_JSON, cache)
    print(f"✓ Skrev kamppost.json med {len(kamposter)} kamper for {igaar_str}")
    print("✓ Serper-søk brukt i denne kjøringen: 0")
    print(f"✓ Fulltekstforsøk brukt i denne kjøringen: {fulltekst_teller}")


if __name__ == "__main__":
    main()
