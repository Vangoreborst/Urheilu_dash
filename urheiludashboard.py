#!/usr/bin/env python3
"""Urheilu-dashboard (Pydroid 3 + Chrome). Ei vaadi asennettavia paketteja.
Käynnistys: aja tiedosto -> avaa Chromessa http://127.0.0.1:8765
Chromessa: valikko (⋮) -> "Asenna sovellus" (tai sivun "Asenna"-painike).

Omat tapahtumat: muokkaa omat_tapahtumat.json. Automaattiset kalenterisyötteet (ICS): lahteet.json. Kelvolliset sport-arvot:
rally, athletics, winter, f23, endurance, dakar, fin_nt, fin_u21, fin_u17
"""
import json, os, re, time, math, zlib, struct, threading, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", 8765))  # pilvipalvelu antaa portin ympäristömuuttujana
CLOUD = "PORT" in os.environ
HERE = os.path.dirname(os.path.abspath(__file__))
MANUAL_FILE = os.path.join(HERE, "omat_tapahtumat.json")
TTL = 120  # oletusvälimuisti 2 min (live-tulokset)
_cache, _lock = {}, threading.Lock()

# Seurasarjat (ESPN-sarjakoodit)
LEAGUES = {
    "eng.1": "Valioliiga", "ger.1": "Bundesliga", "uefa.champions": "Mestarien liiga",
    "uefa.europa": "Eurooppa-liiga", "uefa.europa.conf": "Konferenssiliiga", "fin.1": "Veikkausliiga",
    "esp.1": "La Liga", "ita.1": "Serie A", "fra.1": "Ligue 1", "eng.2": "Championship",
    "swe.1": "Allsvenskan",
}
# Maajoukkuekilpailut (ESPN). Avain -> (slug, nimi)
NAT = {
    "nl": ("uefa.nations", "Nations League"), "euroq": ("uefa.euroq", "EM-karsinta"),
    "wcq": ("fifa.worldq.uefa", "MM-karsinta"), "euro": ("uefa.euro", "EM-kisat"),
    "wc": ("fifa.world", "MM-kisat"), "friendly": ("fifa.friendly", "Maaottelu"),
}
NAT_CAT = {"nl": "nt_nl", "euroq": "nt_euroq", "wcq": "nt_wcq", "euro": "nt_euro", "wc": "nt_wc"}
GROUPED = {"nl", "euroq", "wcq"}  # kilpailut, joissa on lohkot -> "Suomen lohkon pelit"
LONG = {"euro", "wc"}             # kaukana tulevaisuudessa -> pidempi hakuikkuna
ALLOWED_SLUGS = set(LEAGUES) | {v[0] for v in NAT.values()}
SAFE = re.compile(r"^[\w.\-]{0,40}$")

FI = {"Finland": "Suomi", "Belarus": "Valko-Venäjä", "Spain": "Espanja", "Germany": "Saksa", "France": "Ranska",
      "England": "Englanti", "Italy": "Italia", "Netherlands": "Hollanti", "Belgium": "Belgia", "Sweden": "Ruotsi",
      "Norway": "Norja", "Denmark": "Tanska", "Estonia": "Viro", "Lithuania": "Liettua", "Poland": "Puola",
      "Czechia": "Tšekki", "Czech Republic": "Tšekki", "Hungary": "Unkari", "Austria": "Itävalta",
      "Switzerland": "Sveitsi", "Croatia": "Kroatia", "Greece": "Kreikka", "Turkey": "Turkki", "Türkiye": "Turkki",
      "Ukraine": "Ukraina", "Russia": "Venäjä", "Scotland": "Skotlanti", "Northern Ireland": "Pohjois-Irlanti",
      "Republic of Ireland": "Irlanti", "Ireland": "Irlanti", "Iceland": "Islanti", "Kazakhstan": "Kazakstan",
      "Luxembourg": "Luxemburg", "Cyprus": "Kypros", "Bosnia and Herzegovina": "Bosnia ja Hertsegovina",
      "North Macedonia": "Pohjois-Makedonia", "Faroe Islands": "Färsaaret", "United States": "Yhdysvallat",
      "Brazil": "Brasilia", "Argentina": "Argentiina", "Japan": "Japani", "Mexico": "Meksiko", "Azerbaijan": "Azerbaidžan"}
fi = lambda n: FI.get(n, n)

def get_json(url, ttl=TTL):
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < hit[2]:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "urheilu-dashboard/2.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read().decode("utf-8"))
    with _lock:
        if len(_cache) > 500:  # siivotaan vanhentuneet
            for k in [k for k, v in _cache.items() if now - v[0] >= v[2]]:
                _cache.pop(k, None)
        _cache[url] = (now, data, ttl)
    return data

# Lähetysoikeudet Suomessa (kausi 2026-27, tarkista ajantasaisuus tilaajapalvelusta)
TV = {"f1": "Viaplay", "nhl": "Viaplay", "eng.1": "Viaplay", "ger.1": "Viaplay", "fra.1": "Viaplay",
      "eng.2": "Viaplay", "uefa.europa": "Viaplay", "uefa.champions": "MTV Katsomo+ (osa MTV3)",
      "ita.1": "MTV Katsomo+", "swe.1": "MTV Katsomo+", "fin.1": "Ruutu+ (osa Yle)",
      "esp.1": "tarkista", "uefa.europa.conf": "tarkista", "tennis": "tarkista",
      "rally": "MTV Katsomo+ / Yle Areena (tarkista)", "athletics": "Yle / MTV Katsomo+ (tarkista)",
      "winter": "Yle / Viaplay / MTV Katsomo+ (tarkista)",
      "golf": "Viaplay (tarkista)", "f23": "Viaplay (tarkista)", "endurance": "Eurosport / HBO Max (tarkista)",
      "dakar": "tarkista", "fin_nt": "Yle (tarkista)", "fin_u21": "Yle (Areena)", "fin_u17": "tarkista"}
DUR = {"football": 125, "nhl": 170, "f1": 120}  # lähetyksen arvioitu kesto minuutteina

def ev(sport, title, sub, start, end=None, src="", **kw):
    tv = kw.pop("tv", None) or TV.get(kw.get("key") or sport, "")
    dur = kw.pop("dur", None)
    d = {"sport": sport, "title": title, "sub": sub, "start": start, "end": end, "src": src,
         "tv": tv, "dur": DUR.get(sport, 0) if dur is None else dur}
    d.update(kw)
    return d

def espn_range(url, d0, d1):
    """Hakee ESPN:stä päiväväliltä. Jos haku kerralla epäonnistuu, pilkotaan pienempiin osiin."""
    try:
        return get_json(f"{url}?dates={d0:%Y%m%d}-{d1:%Y%m%d}&limit=400")
    except Exception as e:
        err = e
    span = (d1 - d0).days
    if span > 200:
        raise err
    step = 14 if span > 30 else 1
    evs, day, ok = [], d0, False
    while day <= d1:
        end = min(day + timedelta(days=step - 1), d1)
        rng = f"{day:%Y%m%d}" if step == 1 else f"{day:%Y%m%d}-{end:%Y%m%d}"
        try:
            evs += get_json(f"{url}?dates={rng}&limit=400").get("events", []); ok = True
        except Exception as e:
            err = e
        day = end + timedelta(days=1)
    if not ok:
        raise err
    return {"events": evs}

def parse_match(e):
    comp = sorted(e["competitions"][0]["competitors"], key=lambda c: c.get("homeAway") != "home")
    if len(comp) < 2:
        return None
    names = [c["team"]["displayName"] for c in comp]
    state = e.get("status", {}).get("type", {}).get("state", "pre")
    sc = f'{comp[0].get("score","")}–{comp[1].get("score","")}' if state != "pre" else ""
    return names, state, sc

def f1():
    d = get_json("https://api.jolpi.ca/ergast/f1/current.json", 6 * 3600)
    out = []
    keys = [("Qualifying", "Aika-ajo", "q", 70), ("SprintQualifying", "Sprint-aika-ajo", "sq", 60),
            ("SprintShootout", "Sprint-aika-ajo", "sq", 60), ("Sprint", "Sprint", "sprint", 60)]
    for r in d["MRData"]["RaceTable"]["Races"]:
        base = dict(kind="f1", round=r["round"], src="Jolpica F1")
        for k, label, ses, dur in keys:
            if k in r:
                out.append(ev("f1", r["raceName"], label, r[k]["date"] + "T" + r[k].get("time", "12:00:00Z"), session=ses, dur=dur, **base))
        out.append(ev("f1", r["raceName"], "Kilpailu", r["date"] + "T" + r.get("time", "12:00:00Z"), session="race", dur=125, **base))
    return out

def nhl(d0, d1):
    out, seen = [], set()
    day = d0
    while day <= d1 + timedelta(days=1):
        j = get_json("https://api-web.nhle.com/v1/schedule/" + day.isoformat(), 300)
        for w in j.get("gameWeek", []):
            for g in w.get("games", []):
                if g["id"] in seen:
                    continue
                seen.add(g["id"])
                a, h = g["awayTeam"], g["homeTeam"]
                an = a.get("commonName", {}).get("default") or a["abbrev"]
                hn = h.get("commonName", {}).get("default") or h["abbrev"]
                st = {"FUT": "pre", "PRE": "pre", "LIVE": "in", "CRIT": "in"}.get(g.get("gameState"), "post")
                sc = f'{a["score"]}–{h["score"]}' if st != "pre" and "score" in a else ""
                out.append(ev("nhl", f"{an} @ {hn}", "NHL", g["startTimeUTC"], src="NHL Web API", kind="nhl", id=g["id"], state=st, score=sc))
        day += timedelta(days=7)
    return out

def club(slug, d0, d1):
    j = espn_range(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard", d0, d1)
    out, seen = [], set()
    for e in j.get("events", []):
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        m = parse_match(e)
        if not m:
            continue
        names, state, sc = m
        out.append(ev("football", " – ".join(names), LEAGUES[slug], e["date"], src="ESPN", key=slug, kind="football",
                      id=e["id"], slug=slug, state=state, score=sc))
    return out

def nat_comp(key, d0, d1):
    """Maajoukkuekilpailu. Jokaiselle ottelulle lasketaan luokat (cats):
    fin_nt = Suomen peli, fin_grp = Suomen lohkon peli, nt_* = kilpailun kaikki pelit."""
    slug, name = NAT[key]
    j = espn_range(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard", d0, d1)
    games, seen, group = [], set(), set()
    for e in j.get("events", []):
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        m = parse_match(e)
        if not m:
            continue
        isfin = any("Finland" in n for n in m[0])
        if isfin:
            group.update(m[0])  # Suomen vastustajat + Suomi = Suomen lohko
        games.append((e, m, isfin))
    out = []
    for e, (names, state, sc), isfin in games:
        if key == "friendly" and not isfin:
            continue
        cats = []
        if isfin:
            cats.append("fin_nt")
        if key in GROUPED and all(n in group for n in names):
            cats.append("fin_grp")
        if key in NAT_CAT:
            cats.append(NAT_CAT[key])
        if not cats:
            continue
        tv = ("Yle" if key == "nl" else "Yle (tarkista)") if isfin and key != "friendly" else ("Yle / MTV (tarkista)" if isfin else "tarkista")
        out.append(ev("football", " – ".join(fi(n) for n in names), name, e["date"], src="ESPN", kind="football",
                      id=e["id"], slug=slug, state=state, score=sc, cats=cats, tv=tv))
    return out

def tennis(d0, d1):
    out = []
    for tour in ("atp", "wta"):
        j = espn_range(f"https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard", d0, d1)
        for e in j.get("events", []):
            out.append(ev("tennis", e.get("name", "Turnaus"), tour.upper(), e["date"], e.get("endDate"), src="ESPN", allday=True))
    return out

def golf(d0, d1):
    out, err = [], None
    for tour, name in (("pga", "PGA Tour"), ("eur", "DP World Tour"), ("lpga", "LPGA")):
        try:
            j = espn_range(f"https://site.api.espn.com/apis/site/v2/sports/golf/{tour}/scoreboard", d0, d1)
        except Exception as e:
            err = e; continue
        for e in j.get("events", []):
            out.append(ev("golf", e.get("name", "Turnaus"), name, e["date"], e.get("endDate"), src="ESPN", allday=True))
    if not out and err: raise err
    return out

# ---- Sisäänrakennetut kalenterit (tarkistettu 10.10.2026). Vanhat (yli 14 pv) siivotaan pois automaattisesti. ----
# Sardinian MM-ralli 1.-4.10.2026 oli kauden päätösralli (Saudi-Arabia poistui kalenterista). Lähde: atleetti.fi / etusuora.com
_MTV = "MTV Katsomo+ / MTV Max"
_SARD = [("2026-10-01", "10:00", "", "Shakedown", 120, _MTV), ("2026-10-01", "16:00", "", "Pre Show", 45, _MTV),
    ("2026-10-01", "16:45", "", "EK1 Ittiri Arena Show", 60, _MTV),
    ("2026-10-02", "08:45", "", "EK2–EK4", 150, _MTV), ("2026-10-02", "15:15", "", "EK5–EK7", 180, _MTV),
    ("2026-10-02", "19:15", "", "Service Park", 30, _MTV),
    ("2026-10-03", "08:45", "", "EK8–EK10", 150, _MTV), ("2026-10-03", "15:15", "", "EK11–EK13", 180, _MTV),
    ("2026-10-03", "19:15", "", "Service Park", 30, _MTV),
    ("2026-10-04", "09:15", "11:00", "EK14", 0, _MTV), ("2026-10-04", "11:00", "12:30", "EK15", 0, _MTV),
    ("2026-10-04", "12:30", "14:30", "EK16", 0, _MTV),
    ("2026-10-04", "14:30", "15:00", "MM-rallistudio", 0, "MTV3 + MTV Katsomo+ / MTV Max"),
    ("2026-10-04", "15:00", "16:30", "EK17 Power Stage", 0, "MTV3 (ilmainen) + MTV Katsomo+ / MTV Max")]
BUILTIN = [{"sport": "rally", "title": "Sardinian MM-ralli – " + t, "sub": "WRC, kauden päätösralli",
            "start": d + " " + a, "end": (d + " " + e) if e else "", "dur": du, "tv": tv}
           for d, a, e, t, du, tv in _SARD]

# Päivämääräkohtaiset lisäkalenterit. Lähteet: FIA, FIA WEC (kalenteri päivitetty 28.7.2026: Barcelona + Monza), Dakar.com, Suomen Urheiluliitto
_D = [("f23", "Formula 2 – Qatar (Lusail)", "F2 · F3-kausi päättyi syyskuussa", "2026-11-27", "2026-11-29", "Viaplay (tarkista)"),
      ("f23", "Formula 2 – Abu Dhabi (Yas Marina)", "F2 · kauden päätös", "2026-12-04", "2026-12-06", "Viaplay (tarkista)"),
      ("endurance", "WEC 6 Hours of Barcelona", "WEC · kisapäivä su 18.10.", "2026-10-16", "2026-10-18", "Eurosport / HBO Max (tarkista)"),
      ("endurance", "WEC 6 Hours of Monza", "WEC · kauden päätös, kisapäivä su 8.11.", "2026-11-06", "2026-11-08", "Eurosport / HBO Max (tarkista)"),
      ("dakar", "Dakar-ralli 2027", "Saudi-Arabia, King Abdullah Economic City", "2027-01-01", "2027-01-15", "tarkista"),
      ("athletics", "SM-maastot", "Maastojuoksun SM", "2026-10-24", "", "Yle (tarkista)"),
      ("athletics", "EM-maastot", "Maastojuoksun EM", "2026-12-13", "", "Yle (tarkista)")]
BUILTIN += [{"sport": a, "title": t, "sub": b, "start": c, "end": d, "dur": 0, "tv": tv} for a, t, b, c, d, tv in _D]

# Talvilajit 2026-27 (lähde: etusuora.com / Yle, tarkistettu 10.10.2026). Mäkihypyn ja alppihiihdon kalenterit ovat osittaisia.
_W = [("Hiihto", "Maailmancup", "Ruka", "2026-11-27", "2026-11-29"), ("Hiihto", "Maailmancup", "Trondheim", "2026-12-04", "2026-12-06"),
      ("Hiihto", "Maailmancup", "Davos", "2026-12-11", "2026-12-13"), ("Hiihto", "Maailmancup", "Engadin", "2027-01-22", "2027-01-24"),
      ("Hiihto", "Maailmancup", "Toblach", "2027-01-29", "2027-01-31"), ("Hiihto", "Maailmancup", "Lahti", "2027-02-12", "2027-02-14"),
      ("Hiihto", "MM-kisat", "Falun", "2027-02-24", "2027-03-07"), ("Hiihto", "Maailmancup", "Ulricehamn", "2027-03-19", "2027-03-21"),
      ("Hiihto", "Suomen Cup", "Vuokatti", "2026-10-31", "2026-11-01"), ("Hiihto", "Suomen Cup", "Ruka", "2026-11-21", "2026-11-22"),
      ("Mäkihyppy", "Maailmancup", "Lillehammer", "2026-11-20", "2026-11-22"), ("Mäkihyppy", "Maailmancup (M)", "Ruka", "2026-11-28", "2026-11-29"),
      ("Mäkihyppy", "Maailmancup (N)", "Hinzenbach", "2026-11-28", "2026-11-29"), ("Mäkihyppy", "Maailmancup", "Wisła", "2026-12-05", "2026-12-06"),
      ("Mäkihyppy", "Maailmancup", "Titisee-Neustadt", "2026-12-11", "2026-12-13"), ("Mäkihyppy", "Maailmancup", "Engelberg", "2026-12-19", "2026-12-20"),
      ("Mäkihyppy", "Maailmancup (N)", "Ljubno", "2027-01-09", "2027-01-10"), ("Mäkihyppy", "Maailmancup (N)", "Zhangjiakou", "2027-01-15", "2027-01-16"),
      ("Mäkihyppy", "Maailmancup", "Willingen", "2027-01-29", "2027-01-31"), ("Mäkihyppy", "Maailmancup", "Lahti", "2027-02-11", "2027-02-14"),
      ("Mäkihyppy", "Maailmancup", "Vikersund", "2027-02-18", "2027-02-21"), ("Mäkihyppy", "Maailmancup", "Oslo", "2027-03-13", "2027-03-14"),
      ("Mäkihyppy", "Maailmancup", "Planica", "2027-03-18", "2027-03-21"),
      ("Ampumahiihto", "Maailmancup", "Kontiolahti", "2026-11-26", "2026-11-29"), ("Ampumahiihto", "Maailmancup", "Hochfilzen", "2026-12-04", "2026-12-06"),
      ("Ampumahiihto", "Maailmancup", "Annecy", "2026-12-10", "2026-12-13"), ("Ampumahiihto", "Maailmancup", "Pokljuka", "2027-01-02", "2027-01-03"),
      ("Ampumahiihto", "Maailmancup", "Ruhpolding", "2027-01-06", "2027-01-10"), ("Ampumahiihto", "Maailmancup", "Nové Město", "2027-01-21", "2027-01-24"),
      ("Ampumahiihto", "EM-kisat", "Pokljuka", "2027-01-27", "2027-01-31"), ("Ampumahiihto", "Maailmancup", "Oberhof", "2027-03-04", "2027-03-07"),
      ("Ampumahiihto", "Maailmancup", "Östersund", "2027-03-11", "2027-03-14"), ("Ampumahiihto", "Maailmancup", "Oslo Holmenkollen", "2027-03-18", "2027-03-21"),
      ("Yhdistetty", "Maailmancup", "Ruka", "2026-11-27", "2026-11-29"), ("Yhdistetty", "Maailmancup", "Trondheim", "2026-12-03", "2026-12-06"),
      ("Yhdistetty", "Maailmancup", "Val di Fiemme", "2026-12-10", "2026-12-13"), ("Yhdistetty", "Maailmancup", "Ramsau", "2026-12-18", "2026-12-20"),
      ("Yhdistetty", "Maailmancup", "Klingenthal", "2027-01-08", "2027-01-10"), ("Yhdistetty", "Maailmancup", "Otepää", "2027-01-14", "2027-01-17"),
      ("Yhdistetty", "Maailmancup", "Schonach", "2027-01-22", "2027-01-24"), ("Yhdistetty", "Maailmancup", "Seefeld", "2027-01-28", "2027-01-31"),
      ("Yhdistetty", "Maailmancup", "Lake Placid", "2027-02-04", "2027-02-06"), ("Yhdistetty", "Maailmancup", "Lahti", "2027-02-11", "2027-02-13"),
      ("Yhdistetty", "Maailmancup", "Oslo", "2027-03-11", "2027-03-13")]
BUILTIN += [{"sport": "winter", "title": f"{l}: {p}", "sub": t, "start": a, "end": b, "dur": 0,
             "tv": "Yle (tarkista)" if l in ("Ampumahiihto", "Hiihto") else "Yle / Viaplay / MTV Katsomo+ (tarkista)", "src": "etusuora.com / Yle"}
            for l, t, p, a, b in _W]

# Suomen maajoukkueet (Suomen Palloliitto). Ajat Suomen aikaa.
_PL = "Suomen Palloliitto"
BUILTIN += [
    # varalla, jos ESPN ei vielä listaa ottelua (piilotetaan automaattisesti, kun ESPN:ssä on Suomen peli samalle päivälle)
    {"sport": "fin_nt", "title": "Suomi – San Marino", "sub": "Nations League C · Bolt Arena, Helsinki",
     "start": "2026-11-15 19:00", "end": "", "dur": 125, "tv": "Yle", "fb": True, "src": _PL},
    {"sport": "fin_u21", "title": "Suomi – Espanja", "sub": "U21 EM-karsinta · Tammelan Stadion", "start": "2026-09-25 18:30", "end": "", "dur": 120, "src": _PL},
    {"sport": "fin_u21", "title": "Romania – Suomi", "sub": "U21 EM-karsinta · Târgoviște", "start": "2026-09-30 19:00", "end": "", "dur": 120, "src": _PL},
    {"sport": "fin_u21", "title": "Kosovo – Suomi", "sub": "U21 EM-karsinta · Pristina", "start": "2026-10-06 20:00", "end": "", "dur": 120, "src": _PL},
]

def manual():
    if not os.path.exists(MANUAL_FILE):
        sample = [{"sport": "fin_u17", "title": "ESIMERKKI Suomi U17 – Ruotsi", "sub": "EM-karsinta",
                   "start": "2000-01-01 12:00", "end": ""}]
        with open(MANUAL_FILE, "w", encoding="utf-8") as f:
            json.dump(sample, f, ensure_ascii=False, indent=2)
    with open(MANUAL_FILE, encoding="utf-8") as f:
        items = json.load(f)
    cut = (datetime.now(timezone.utc).date() - timedelta(days=14)).isoformat()
    items = [b for b in BUILTIN if (b.get("end") or b["start"])[:10] >= cut] + items
    out = []
    for i in items:  # start kirjoitetaan Suomen ajassa -> selain muuntaa (local=True)
        extra = {"fb": True} if i.get("fb") else {}
        out.append(dict(ev(i["sport"], i["title"], i.get("sub", ""), i["start"], i.get("end") or None,
                           i.get("src", "omat_tapahtumat.json"), tv=i.get("tv"), dur=i.get("dur"), **extra), local=True))
    return out


# ---------- Kalenterisyötteet (ICS): automaattinen haku mille tahansa lajille ----------
FEEDS_FILE = os.path.join(HERE, "lahteet.json")  # [{"sport":"winter","name":"FIS","url":"https://.../kalenteri.ics"}]

def get_text(url, ttl=6 * 3600):
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < hit[2]:
            return hit[1]
    req = urllib.request.Request(url.replace("webcal://", "https://"), headers={"User-Agent": "urheilu-dashboard/2.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        text = r.read(3_000_000).decode("utf-8", "replace")
    with _lock:
        _cache[url] = (now, text, ttl)
    return text

def _ics_dt(v, params):
    v = v.strip()
    if "VALUE=DATE" in params or len(v) == 8:
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}", "date"
    m = re.match(r"(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)", v)
    if not m:
        return None, None
    y, mo, d, h, mi = m.groups()
    return (f"{y}-{mo}-{d}T{h}:{mi}:00Z", "utc") if v.endswith("Z") else (f"{y}-{mo}-{d} {h}:{mi}", "local")

def parse_ics(text, sport, name, d0, d1):
    text = re.sub(r"\r?\n[ \t]", "", text)  # rivinvaihdot pois (line folding)
    out, cur = [], None
    for line in text.splitlines():
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            a, ka = cur.get("DTSTART", (None, None)) if "DTSTART" in cur else (None, None)
            if a:
                b, kb = cur.get("DTEND", (None, None)) if "DTEND" in cur else (None, None)
                if ka == "date" and b:  # ICS:n päättymispäivä on eksklusiivinen
                    b = (date.fromisoformat(b) - timedelta(days=1)).isoformat()
                    if b == a: b = ""
                last = (b or a)[:10]
                if d0.isoformat() <= last and a[:10] <= d1.isoformat():
                    title = cur.get("SUMMARY", "Tapahtuma").replace("\\,", ",").replace("\\n", " ")
                    out.append(dict(ev(sport, title, name, a, b or None, name, dur=120 if ka != "date" and not b else 0), local=(ka != "utc")))
            cur = None
        elif cur is not None and ":" in line:
            k, v = line.split(":", 1)
            n = k.split(";")[0].upper()
            if n in ("DTSTART", "DTEND"):
                dt, kind = _ics_dt(v, k.upper())
                if dt: cur[n] = (dt, kind)
            elif n == "SUMMARY":
                cur[n] = v
    return out

def feeds(d0, d1):
    if not os.path.exists(FEEDS_FILE):
        return []
    with open(FEEDS_FILE, encoding="utf-8") as f:
        items = json.load(f)
    out = []
    for i in items:
        out += parse_ics(get_text(i["url"]), i["sport"], i.get("name", "Kalenteri"), d0, d1)
    return out

def build(sources, slugs, nat):
    today = datetime.now(timezone.utc).date()
    d0, d1 = today - timedelta(days=8), today + timedelta(days=21)
    n0 = today - timedelta(days=35)
    jobs = []  # (nimi, funktio, hiljainen)
    if "f1" in sources: jobs.append(("Formula 1", f1, False))
    if "nhl" in sources: jobs.append(("NHL", lambda: nhl(d0, d1), False))
    if "golf" in sources: jobs.append(("Golf", lambda: golf(d0, d1), False))
    if "tennis" in sources: jobs.append(("Tennis", lambda: tennis(d0, d1), False))
    if "manual" in sources:
        jobs.append(("Omat tapahtumat", manual, False))
        if os.path.exists(FEEDS_FILE): jobs.append(("Kalenterisyötteet", lambda: feeds(today - timedelta(days=8), today + timedelta(days=150)), False))
    if "football" in sources:
        for s in slugs:
            if s in LEAGUES:
                jobs.append((LEAGUES[s], (lambda s=s: club(s, d0, d1)), False))
    for k in nat:
        if k in NAT:
            end = today + timedelta(days=400 if k in LONG else 130)
            jobs.append((NAT[k][1], (lambda k=k, end=end: nat_comp(k, n0, end)), k in LONG))  # EM/MM-kisat: ei virheilmoitusta jos ei dataa
    events, errors = [], []
    def run(job):
        try: return job[1](), None
        except Exception as e: return [], (None if job[2] else f"{job[0]}: {e}"[:70])
    with ThreadPoolExecutor(10) as ex:
        for res, err in ex.map(run, jobs):
            events += res
            if err: errors.append(err)
    # varatapahtumat pois, jos ESPN:ssä on Suomen peli samalle päivälle (±1 pv)
    fin = set()
    for e in events:
        if "fin_nt" in e.get("cats", []) and not e.get("fb"):
            d = date.fromisoformat(e["start"][:10])
            fin |= {(d + timedelta(days=i)).isoformat() for i in (-1, 0, 1)}
    events = [e for e in events if not (e.get("fb") and e["start"][:10] in fin)]
    return {"events": events, "errors": errors, "leagues": LEAGUES}

def detail(q):
    g = lambda k: q.get(k, [""])[0]
    if not all(SAFE.match(g(k)) for k in ("kind", "id", "slug", "round", "session")):
        return {"lines": ["Virheellinen pyyntö."]}
    kind, lines = g("kind"), []
    if kind == "football":
        if g("slug") not in ALLOWED_SLUGS:
            return {"lines": ["Virheellinen pyyntö."]}
        j = get_json(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{g('slug')}/summary?event={g('id')}", 60)
        v = j.get("gameInfo", {}).get("venue", {}).get("fullName")
        if v: lines.append("Paikka: " + v)
        for k in j.get("keyEvents", []):
            if any(x in k.get("type", {}).get("text", "") for x in ("Goal", "Card", "Penalty")):
                lines.append(f'{k.get("clock", {}).get("displayValue", "")} {k.get("shortText") or k.get("text", "")}')
    elif kind == "nhl":
        j = get_json(f"https://api-web.nhle.com/v1/gamecenter/{g('id')}/landing", 60)
        v = j.get("venue", {}).get("default")
        if v: lines.append("Paikka: " + v)
        for per in j.get("summary", {}).get("scoring", []):
            for gl in per.get("goals", []):
                nm = gl.get("name", {}).get("default") or gl.get("lastName", {}).get("default", "")
                lines.append(f'{per["periodDescriptor"]["number"]}. erä {gl.get("timeInPeriod", "")} {gl.get("teamAbbrev", {}).get("default", "")} {nm} ({gl.get("awayScore")}–{gl.get("homeScore")})')
    elif kind == "f1":
        ses = g("session")
        ep = {"q": ("qualifying", "QualifyingResults"), "sprint": ("sprint", "SprintResults"), "race": ("results", "Results")}.get(ses)
        if ep:
            j = get_json(f"https://api.jolpi.ca/ergast/f1/current/{g('round')}/{ep[0]}.json", 300)
            rs = j["MRData"]["RaceTable"]["Races"]
            for r in (rs[0].get(ep[1], []) if rs else [])[:20]:
                t = r.get("Time", {}).get("time") or r.get("Q3") or r.get("Q2") or r.get("Q1") or r.get("status", "")
                lines.append(f'{r["position"]}. {r["Driver"]["familyName"]} – {r["Constructor"]["name"]}  {t}')
    return {"lines": lines or ["Tuloksia tai lisätietoja ei ole vielä saatavilla."]}

# ---------- PWA: manifesti, service worker, ikonit ----------
MANIFEST = json.dumps({
    "name": "Urheilu tänään", "short_name": "Urheilu", "lang": "fi", "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#12161c", "theme_color": "#12161c",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]})

SW = r"""const V='urh-v2';
self.addEventListener('install',e=>{e.waitUntil(caches.open(V).then(c=>c.addAll(['/','/manifest.webmanifest','/icon-192.png'])).then(()=>self.skipWaiting()))});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x!==V).map(x=>caches.delete(x)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',e=>{const r=e.request;if(r.method!=='GET')return;const u=new URL(r.url);if(u.origin!==location.origin)return;
e.respondWith(fetch(r).then(res=>{if(res.ok){const c=res.clone();caches.open(V).then(x=>x.put(r,c))}return res}).catch(()=>caches.match(r).then(m=>{if(m)return m;
if(r.mode==='navigate')return caches.match('/');
const body=u.pathname==='/api/detail'?'{"lines":["Ei yhteyttä."]}':'{"events":[],"errors":["Ei yhteyttä palvelimeen"],"leagues":{}}';
return new Response(body,{headers:{'Content-Type':'application/json'}})})))});
"""

_icons = {}
def icon(size):
    """Piirtää ikonin (rengas + piste) puhtaalla Pythonilla; sisältö pidetään maskable-turva-alueella."""
    if size in _icons:
        return _icons[size]
    bg, acc = (18, 22, 28), (79, 195, 247)
    c, R, w, rd = size / 2, size * 0.30, size * 0.06, size * 0.10
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            d = math.hypot(x + .5 - c, y + .5 - c)
            t = max(max(0.0, min(1.0, w / 2 + .5 - abs(d - R))), max(0.0, min(1.0, rd + .5 - d)))
            rows += bytes((int(bg[0] + (acc[0] - bg[0]) * t), int(bg[1] + (acc[1] - bg[1]) * t), int(bg[2] + (acc[2] - bg[2]) * t)))
    def ch(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    png = (b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + ch(b"IDAT", zlib.compress(bytes(rows), 9)) + ch(b"IEND", b""))
    _icons[size] = png
    return png

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, body, ctype, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/api/events":
            q = parse_qs(u.query)
            split = lambda k: [x for x in (q.get(k, [""])[0]).split(",") if x]
            self.send(json.dumps(build(split("sources"), split("leagues"), split("nat"))).encode(), "application/json", {"Cache-Control": "no-store"})
        elif p == "/api/detail":
            try: res = detail(parse_qs(u.query))
            except Exception as e: res = {"lines": ["Haku epäonnistui: " + str(e)[:60]]}
            self.send(json.dumps(res).encode(), "application/json", {"Cache-Control": "no-store"})
        elif p == "/api/leagues":
            self.send(json.dumps(LEAGUES).encode(), "application/json")
        elif p == "/manifest.webmanifest":
            self.send(MANIFEST.encode(), "application/manifest+json")
        elif p == "/sw.js":
            self.send(SW.encode(), "application/javascript", {"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})
        elif p in ("/icon-192.png", "/favicon.ico"):
            self.send(icon(192), "image/png", {"Cache-Control": "max-age=86400"})
        elif p == "/icon-512.png":
            self.send(icon(512), "image/png", {"Cache-Control": "max-age=86400"})
        elif p in ("/", "/index.html"):
            self.send(PAGE.encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-cache"})
        else:
            self.send_error(404)

PAGE = r"""<!doctype html><html lang="fi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#12161c">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png"><link rel="apple-touch-icon" href="/icon-192.png">
<title>Urheilu tänään</title>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;800&family=Barlow:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#12161c;--panel:#1b212a;--ink:#eef1f5;--mute:#8d98a8;--line:#2a323e;--acc:#4fc3f7;--bgimg:none}
body[data-theme=futis]{--bg:#0e2a1a;--panel:#14391f;--line:#235634;--acc:#7be495;--bgimg:repeating-linear-gradient(90deg,#0e2a1a 0 60px,#102f1d 60px 120px)}
body[data-theme=ralli]{--bg:#1d1610;--panel:#2a1f15;--line:#4a3623;--acc:#ff8a2b;--mute:#b39b84}
body[data-theme=formula]{--bg:#0b0b0d;--panel:#17171b;--line:#2b2b31;--acc:#e10600}
body[data-theme=tennis]{--bg:#2a1a12;--panel:#3a251a;--line:#5d3a28;--acc:#d7e84a;--mute:#c4a894}
body[data-theme=nhl]{--bg:#0d1624;--panel:#142239;--line:#243b5c;--acc:#8cc8ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);background-image:var(--bgimg);color:var(--ink);font:16px/1.4 Barlow,system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 14px}
.top{display:flex;gap:8px;align-items:center}
h1{font:800 26px 'Barlow Condensed',sans-serif;margin:0 auto 0 0;letter-spacing:.3px}
.tabs{display:flex;gap:6px;margin-top:8px}.tabs button,#gear,#inst{font:600 16px 'Barlow Condensed',sans-serif;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:7px 14px}
#inst{background:var(--acc);color:#111;border-color:var(--acc)}#inst[hidden]{display:none}
#gear{width:42px;height:42px;padding:0;font-size:22px;line-height:1;border-radius:50%}
.tabs button.on{background:var(--acc);color:#111;border-color:var(--acc)}
main{max-width:760px;margin:0 auto;padding:6px 14px 60px}
h2{font:800 22px 'Barlow Condensed',sans-serif;margin:22px 0 8px;text-transform:capitalize}
.row{display:grid;grid-template-columns:64px 1fr;gap:12px;background:var(--panel);border-left:5px solid var(--c);border-radius:6px;padding:9px 12px;margin-bottom:6px}
.t{font:800 24px/1 'Barlow Condensed',sans-serif;padding-top:3px}.t.r{font-size:15px;color:var(--mute);font-weight:600}
.n{font-weight:500}.s{color:var(--mute);font-size:13.5px}.s b{color:var(--c);font-weight:600}
.msg{color:var(--mute);padding:30px 0;text-align:center}.err{color:#ffb4a8;font-size:13px;margin-top:14px}
.row{position:relative;cursor:pointer}.t small{display:block;font:600 14px 'Barlow Condensed';color:var(--mute);margin-top:3px}
.sc{background:var(--c);color:#111;font:800 15px 'Barlow Condensed';border-radius:5px;padding:1px 8px;margin-left:6px}
.lv{background:#e10600;color:#fff;font:800 12px Barlow;border-radius:4px;padding:1px 6px;margin-left:6px}
.bar{position:absolute;left:0;bottom:0;height:3px;background:var(--c);border-radius:0 3px 3px 0}.post{opacity:.82}
.now{display:flex;align-items:center;gap:8px;color:#ff4d3d;font:800 15px 'Barlow Condensed';margin:6px 0}
.now:after{content:'';flex:1;height:2px;background:#ff4d3d;box-shadow:0 0 8px #ff4d3d}
.det{margin-top:8px;padding-top:8px;border-top:1px solid var(--line);font-size:14px}.det div{padding:1px 0}.det .m{color:var(--mute)}
.tabs{flex-wrap:wrap}.tabs button{padding:6px 10px}
#intro{height:78px;overflow:hidden;position:relative;max-width:760px;margin:0 auto;transition:height .6s ease,opacity .6s ease}
#intro.gone{height:0;opacity:0}#intro:after{content:'';position:absolute;left:14px;right:14px;bottom:0;height:2px;background:var(--line)}
#intro i{position:absolute;bottom:2px;left:var(--x);font-style:normal;font-size:30px;line-height:1;transform-origin:50% 100%;animation:bnc var(--d) var(--w) infinite backwards}
#intro b{display:inline-block;font-weight:400;animation:spin var(--d) var(--w) linear infinite backwards}
@keyframes bnc{0%{transform:translateY(-44px);animation-timing-function:cubic-bezier(.55,0,1,.45)}
48%{transform:translateY(0) scale(1,1)}52%{transform:translateY(0) scale(1.25,.75)}56%{transform:translateY(0) scale(1,1);animation-timing-function:cubic-bezier(0,.55,.45,1)}100%{transform:translateY(-44px)}}
@keyframes spin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){#intro{display:none}}
footer{color:var(--mute);font-size:12.5px;padding:0 14px 40px;max-width:760px;margin:auto}
dialog{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:12px;max-width:440px;width:92%;max-height:88vh;overflow:auto;padding:18px}
dialog::backdrop{background:#000a}dialog h3{font:800 20px 'Barlow Condensed';margin:14px 0 6px}
.hint{color:var(--mute);font-size:13px;margin:2px 0 6px}
label{display:flex;gap:8px;align-items:center;padding:4px 0}.chips{display:flex;gap:6px;flex-wrap:wrap}
.chips label{border:1px solid var(--line);border-radius:20px;padding:5px 12px}
.sw{width:10px;height:10px;border-radius:50%;background:var(--c)}
#ok{margin-top:16px;width:100%;padding:10px;font:800 18px 'Barlow Condensed';background:var(--acc);border:0;border-radius:8px}
</style></head><body>
<header><div class="top"><h1>Urheilu</h1><button id="inst" hidden>Asenna</button><button id="gear" aria-label="Asetukset" title="Asetukset">⚙</button></div>
<div class="tabs"><button data-v="today">Tänään</button><button data-v="yday">Eilen</button><button data-v="weekend">Vkonloppu</button><button data-v="week">Viikko</button><button data-v="lastweek">Viime vko</button><button data-v="next">Seuraavat</button></div></header>
<div id="intro" aria-hidden="true"><i style="--x:6%;--d:1.15s;--w:0s"><b>⚽</b></i><i style="--x:24%;--d:1.35s;--w:.25s"><b>⚽</b></i><i style="--x:42%;--d:1.05s;--w:.5s"><b>⚽</b></i><i style="--x:60%;--d:1.4s;--w:.15s"><b>⚽</b></i><i style="--x:78%;--d:1.2s;--w:.4s"><b>⚽</b></i><i style="--x:92%;--d:1.3s;--w:.65s"><b>⚽</b></i></div>
<main id="out"><div class="msg">Ladataan…</div></main>
<footer id="foot"></footer>
<dialog id="dlg"><h3>Teema</h3><div class="chips" id="themes"></div>
<h3>Jalkapallo</h3><div class="hint">Suomen lohkon pelit = Suomen oman lohkon ottelut (esim. Nations League C). Lohko päätellään Suomen vastustajista.</div><div id="spf"></div>
<h3>Seurasarjat</h3><div id="lg"></div>
<h3>Muut lajit</h3><div id="sp"></div>
<button id="ok">Tallenna</button></dialog>
<script>
const TZ='Europe/Helsinki';
const SP={
fin_nt:{n:'Suomen maajoukkue',c:'#3b7dff',s:'nat',g:'fb',src:'ESPN / Palloliitto'},
fin_grp:{n:'Suomen lohkon pelit',c:'#6fb1ff',s:'nat',g:'fb',src:'ESPN'},
fin_u21:{n:'Suomi U21',c:'#8f7bff',s:'manual',g:'fb',src:'Palloliitto / omat_tapahtumat.json'},
fin_u17:{n:'Suomi U17',c:'#b57bff',s:'manual',g:'fb',src:'omat_tapahtumat.json'},
nt_nl:{n:'Nations League (kaikki)',c:'#00c2a8',s:'nat',g:'fb',src:'ESPN'},
nt_euroq:{n:'EM-karsinnat (kaikki)',c:'#ffb02e',s:'nat',g:'fb',src:'ESPN'},
nt_wcq:{n:'MM-karsinnat (kaikki)',c:'#ff7a59',s:'nat',g:'fb',src:'ESPN'},
nt_euro:{n:'EM-kisat',c:'#ffd93b',s:'nat',g:'fb',src:'ESPN'},
nt_wc:{n:'MM-kisat',c:'#e8c27a',s:'nat',g:'fb',src:'ESPN'},
football:{n:'Seurajalkapallo',c:'#2fbf6b',s:'football',g:'fb',src:'ESPN'},
f1:{n:'Formula 1',c:'#e10600',s:'f1',src:'Jolpica F1 (Ergast-data)'},
rally:{n:'Ralli (EM/MM)',c:'#ff8a2b',s:'manual',src:'omat_tapahtumat.json'},
nhl:{n:'NHL',c:'#4a90e2',s:'nhl',src:'NHL Web API'},
tennis:{n:'Tennis',c:'#d7e84a',s:'tennis',src:'ESPN (turnaukset)'},
athletics:{n:'Yleisurheilu',c:'#e0457b',s:'manual',src:'omat_tapahtumat.json'},
f23:{n:'F2 / F3',c:'#b04bff',s:'manual',src:'FIA (kalenteri)'},
golf:{n:'Golf',c:'#00b3a4',s:'golf',src:'ESPN (turnaukset)'},
endurance:{n:'Kestävyysajot (WEC, GT)',c:'#f2b705',s:'manual',src:'FIA WEC (kalenteri)'},
dakar:{n:'Dakar',c:'#c97b3a',s:'manual',src:'Dakar.com / Red Bull'},
winter:{n:'Talvilajit',c:'#7fd6ff',s:'manual',src:'omat_tapahtumat.json'}};
const THEMES={auto:'Yleinen',futis:'Futis',ralli:'Ralli',formula:'Formula',tennis:'Tennis',nhl:'NHL'};
const DEF={theme:'auto',view:'today',sports:Object.fromEntries(Object.keys(SP).map(k=>[k,!/^nt_|^fin_u/.test(k)])),
leagues:['eng.1','ger.1','uefa.champions','fin.1']};
let st=DEF,LG={};
try{st={...DEF,...JSON.parse(localStorage.getItem('urh')||'{}')}}catch(e){}
st.sports={...DEF.sports,...st.sports};if(st.view==='team')st.view='today';const save=()=>{try{localStorage.setItem('urh',JSON.stringify(st))}catch(e){}};
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dkey=d=>d.toLocaleDateString('sv-SE',{timeZone:TZ});
const addDays=(k,n)=>{const d=new Date(k+'T12:00:00Z');d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10)};
const hel=(s)=>{const m=s.match(/(\d+)-(\d+)-(\d+)(?:[ T](\d+):(\d+))?/);const t=Date.UTC(+m[1],m[2]-1,+m[3],+(m[4]||0),+(m[5]||0));
const p=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:TZ,hourCycle:'h23',year:'numeric',month:'numeric',day:'numeric',hour:'numeric',minute:'numeric'}).formatToParts(new Date(t)).map(x=>[x.type,x.value]));
return new Date(t-(Date.UTC(p.year,p.month-1,p.day,p.hour,p.minute)-t))};
const tm=d=>d.toLocaleTimeString('fi-FI',{timeZone:TZ,hour:'2-digit',minute:'2-digit'}).replace('.',':');
function keys(){const t=dkey(new Date());
if(st.view==='today'||st.view==='next')return[t];if(st.view==='yday')return[addDays(t,-1)];if(st.view==='lastweek')return[...Array(7)].map((_,i)=>addDays(t,-7+i));
if(st.view==='week')return[...Array(7)].map((_,i)=>addDays(t,i));
const dow=new Date(t+'T12:00:00Z').getUTCDay();const sat=dow===0?addDays(t,-1):addDays(t,6-dow);return[sat,addDays(sat,1)]}
function natComps(){const s=new Set();
if(st.sports.fin_nt)['nl','euroq','wcq','friendly','euro','wc'].forEach(x=>s.add(x));
if(st.sports.fin_grp)['nl','euroq','wcq'].forEach(x=>s.add(x));
[['nt_nl','nl'],['nt_euroq','euroq'],['nt_wcq','wcq'],['nt_euro','euro'],['nt_wc','wc']].forEach(([k,c])=>{if(st.sports[k])s.add(c)});return[...s]}
let DATA=null;
async function load(silent){
const on=Object.keys(SP).filter(k=>st.sports[k]);const srcs=[...new Set(on.map(k=>SP[k].s))];
if(!silent)$('#out').innerHTML='<div class="msg">Ladataan…</div>';
try{const r=await fetch('/api/events?sources='+srcs.join(',')+'&leagues='+st.leagues.join(',')+'&nat='+natComps().join(','));DATA=await r.json();DATA.t=Date.now();LG=DATA.leagues}
catch(e){if(!silent||!DATA)$('#out').innerHTML='<div class="msg">Palvelimeen ei saatu yhteyttä. Onko skripti käynnissä?</div>';return}
render()}
const OPEN=new Set(),DET={},EVS={};
function stateOf(e,now){if(e.state)return e.state;if(e.range)return'in';const t=e.a.getTime();return now<t?'pre':now<(e.z?e.z.getTime():t+(e.dur||120)*6e4)?'in':'post'}
function prep(e){const a=e.local?hel(e.start):new Date(e.start);if(isNaN(a))return null;
let b=e.end?(e.local?hel(e.end):new Date(e.end)):null;const ad=e.allday||(e.local&&e.start.length<=10);
if(b&&e.local&&e.end.length<=10)b=new Date(b.getTime()+864e5-6e4);
const z=b||(e.dur?new Date(a.getTime()+e.dur*6e4):(ad?new Date(a.getTime()+864e5-6e4):null));
return{...e,a,b,z,ad}}
// ottelu näytetään kerran: ensimmäinen valittu luokka (Suomen peli > Suomen lohko > kilpailu)
const sportOf=e=>{if(e.cats){for(const c of e.cats)if(st.sports[c])return c;return null}return st.sports[e.sport]?e.sport:null};
const prepAll=()=>DATA.events.map(e0=>{const s=sportOf(e0);return s?prep({...e0,sport:s}):null}).filter(Boolean);
const dshort=d=>d.toLocaleDateString('fi-FI',{timeZone:TZ,weekday:'short',day:'numeric',month:'numeric'});
function relTxt(e,now){const d=e.a.getTime()-now;if(d<=0)return'käynnissä';const m=Math.round(d/6e4),dd=Math.floor(m/1440),hh=Math.floor(m%1440/60);
return dd?`${dd} pv ${hh} h päästä`:hh?`${hh} h ${m%60} min päästä`:`${m} min päästä`}
function rowHtml(e,now,showDate){
const S=SP[e.sport],key=`${e.sport}|${e.title}|${e.start}|${e.sub}`;EVS[key]=e;
const stt=stateOf(e,now),open=OPEN.has(key),multi=e.range||(e.b&&dkey(e.b)!==dkey(e.a));
let time;
if(e.range)time=`<div class="t r">${e.first?(e.ad?'alkaa':tm(e.a)):'käynnissä'}</div>`;
else if(e.ad)time=`<div class="t r">${showDate?dshort(e.a):'koko päivä'}</div>`;
else time=`<div class="t">${tm(e.a)}${e.z?`<small>–${tm(e.z)}</small>`:''}${showDate?`<small>${dshort(e.a)}</small>`:''}</div>`;
const sub=multi&&e.b?`asti ${e.b.toLocaleDateString('fi-FI',{timeZone:TZ,day:'numeric',month:'numeric'})}`:e.sub;
const badge=(e.score?`<span class="sc">${esc(e.score)}</span>`:'')+(stt==='in'&&!multi?'<span class="lv">LIVE</span>':'');
const pct=stt==='in'&&!multi&&e.z?Math.min(100,(now-e.a)/(e.z-e.a)*100):0;
let det='';if(open){const m=`<div class="m">Lähetys ${e.ad?dshort(e.a):tm(e.a)}${e.z&&!e.ad?'–'+tm(e.z):''}${e.dur&&!e.end?' (arvio)':''} · 📺 ${esc(e.tv)||'ei tietoa'} · Lähde: ${esc(e.src)}</div>`;
det=`<div class="det">${m}${(DET[key]||[]).map(x=>`<div>${esc(x)}</div>`).join('')}</div>`}
return `<div class="row ${stt==='post'?'post':''}" data-k="${esc(key)}" style="--c:${S.c}">${time}<div><div class="n">${esc(e.title)}${badge}</div><div class="s"><b>${S.n}</b> · ${e.rel?e.rel+' · ':''}${esc(sub)}${e.tv?' · 📺 '+esc(e.tv):''}</div>${det}</div>${pct?`<div class="bar" style="width:${pct}%"></div>`:''}</div>`}
function listHtml(now){
const ks=keys(),by={};ks.forEach(k=>by[k]=[]);
for(const e of prepAll()){const k0=dkey(e.a);
if(e.b&&dkey(e.b)!==k0){const k1=dkey(e.b);for(const k of ks)if(k>=k0&&k<=k1)by[k].push({...e,range:true,first:k===k0})}
else if(by[k0])by[k0].push(e)}
const today=dkey(new Date()),past=st.view==='yday'||st.view==='lastweek';let h='';
for(const k of(past?[...ks].reverse():ks)){const L=by[k].sort((x,y)=>(!!y.range-!!x.range)||x.a-y.a);if(!L.length)continue;
const lab=new Date(k+'T12:00:00Z').toLocaleDateString('fi-FI',{timeZone:'UTC',weekday:'long',day:'numeric',month:'numeric'});
h+=`<h2>${k===today?'Tänään · ':''}${lab}</h2>`;let placed=k!==today;const nl=`<div class="now">Nyt ${tm(new Date(now))}</div>`;
for(const e of L){if(!placed&&!e.range&&e.a.getTime()>now){h+=nl;placed=true}h+=rowHtml(e,now,false)}
if(!placed)h+=nl}
return h||'<div class="msg">Ei tapahtumia valituilla asetuksilla tälle jaksolle.<br>Ralli, yleisurheilu, talvilajit ja junioripelit lisätään tiedostoon omat_tapahtumat.json.</div>'}
function nextHtml(now){let h='';const all=prepAll();
for(const[k,S]of Object.entries(SP)){if(!st.sports[k])continue;
const L=all.filter(e=>e.sport===k&&(e.z||e.a).getTime()>now).sort((x,y)=>x.a-y.a).slice(0,3);
h+=`<h2 style="color:${S.c}">${S.n}</h2>`;
if(!L.length){h+='<div class="msg" style="padding:8px 0;text-align:left">Ei tulevia tapahtumia haetulla ajanjaksolla.</div>';continue}
L.forEach((e,i)=>{if(i===0)e.rel=relTxt(e,now);h+=rowHtml(e,now,true)})}
return h}
function render(){
if(!DATA)return;document.body.dataset.theme=st.theme==='auto'?'':st.theme;
document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.v===st.view));
const now=Date.now();
let h=st.view==='next'?nextHtml(now):listHtml(now);
if(DATA.errors.length)h+=`<div class="err">Osa lähteistä ei vastannut: ${DATA.errors.map(esc).join(', ')}</div>`;
$('#out').innerHTML=h;
$('#foot').innerHTML='Kellonajat Suomen aikaa. Lähetysoikeudet kaudelta 2026–27 (MTV:n Mestarien liiga siirtyy Ruudulle 2027), tarkista aina palvelusta. Lähteet: '+[...new Set(Object.keys(SP).filter(k=>st.sports[k]).map(k=>SP[k].n+': '+SP[k].src))].join(' · ')}
$('#out').onclick=async ev=>{const r=ev.target.closest('.row');if(!r)return;const k=r.dataset.k;
if(OPEN.has(k)){OPEN.delete(k);render();return}
OPEN.add(k);const e=EVS[k];
if(e&&e.kind&&e.kind!=='tennis'&&stateOf(e,Date.now())!=='pre'&&!DET[k]){DET[k]=['Haetaan…'];render();
try{const p=new URLSearchParams({kind:e.kind,id:e.id||'',slug:e.slug||'',round:e.round||'',session:e.session||''});
DET[k]=(await(await fetch('/api/detail?'+p)).json()).lines}catch(x){DET[k]=['Tietojen haku epäonnistui.']}}
render()};
setInterval(render,60000);
setInterval(()=>{if(!document.hidden)load(true)},300000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&DATA&&Date.now()-DATA.t>300000)load(true)});
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{st.view=b.dataset.v;save();render()});
$('#gear').onclick=()=>{
$('#themes').innerHTML=Object.entries(THEMES).map(([k,v])=>`<label><input type="radio" name="th" value="${k}" ${st.theme===k?'checked':''}>${v}</label>`).join('');
const row=([k,v])=>`<label style="--c:${v.c}"><input type="checkbox" data-sp="${k}" ${st.sports[k]?'checked':''}><span class="sw"></span>${v.n}</label>`,E=Object.entries(SP);
$('#spf').innerHTML=E.filter(([k,v])=>v.g==='fb').map(row).join('');
$('#sp').innerHTML=E.filter(([k,v])=>v.g!=='fb').map(row).join('');
$('#lg').innerHTML=Object.entries(LG).map(([k,v])=>`<label><input type="checkbox" data-lg="${k}" ${st.leagues.includes(k)?'checked':''}>${esc(v)}</label>`).join('');
$('#dlg').showModal()};
$('#ok').onclick=()=>{
st.theme=(document.querySelector('input[name=th]:checked')||{value:'auto'}).value;
document.querySelectorAll('[data-sp]').forEach(i=>st.sports[i.dataset.sp]=i.checked);
st.leagues=[...document.querySelectorAll('[data-lg]')].filter(i=>i.checked).map(i=>i.dataset.lg);
save();$('#dlg').close();load()};
// aloitusanimaatio: pallot pomppivat hetken ja katoavat
setTimeout(()=>{const i=$('#intro');if(i){i.classList.add('gone');setTimeout(()=>i.remove(),700)}},3500);
// PWA: service worker + asennuspainike
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});
let DP=null;
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();DP=e;$('#inst').hidden=false});
window.addEventListener('appinstalled',()=>{$('#inst').hidden=true;DP=null});
$('#inst').onclick=async()=>{if(!DP)return;DP.prompt();await DP.userChoice;DP=null;$('#inst').hidden=true};
fetch('/api/leagues').then(r=>r.json()).then(j=>{LG=j}).catch(()=>{});
document.body.dataset.theme=st.theme==='auto'?'':st.theme;load();
</script></body></html>"""

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0" if CLOUD else "127.0.0.1", PORT), H)
    print("Dashboard käynnissä portissa", PORT)
    if not CLOUD:
        try:
            import webbrowser; webbrowser.open(f"http://127.0.0.1:{PORT}")
        except Exception:
            pass
    srv.serve_forever()
