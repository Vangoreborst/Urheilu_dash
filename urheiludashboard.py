#!/usr/bin/env python3
"""Urheilu-dashboard (Pydroid 3 + Chrome). Ei vaadi asennettavia paketteja.
Käynnistys: aja tiedosto -> avaa Chromessa http://127.0.0.1:8765
Omat tapahtumat (ralli, yleisurheilu, talvilajit): muokkaa omat_tapahtumat.json
"""
import json, os, time, threading, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", 8765))  # pilvipalvelu antaa portin ympäristömuuttujana
CLOUD = "PORT" in os.environ
HERE = os.path.dirname(os.path.abspath(__file__))
MANUAL_FILE = os.path.join(HERE, "omat_tapahtumat.json")
TTL = 15 * 60  # välimuisti 15 min
_cache, _lock = {}, threading.Lock()

# ESPN:n sarjakoodit. national=True -> vain Suomen pelit
LEAGUES = {
    "eng.1": ("Valioliiga", False), "ger.1": ("Bundesliga", False),
    "uefa.champions": ("Mestarien liiga", False), "uefa.europa": ("Eurooppa-liiga", False),
    "fin.1": ("Veikkausliiga", False), "esp.1": ("La Liga", False),
    "ita.1": ("Serie A", False), "fra.1": ("Ligue 1", False),
    "eng.2": ("Championship", False), "swe.1": ("Allsvenskan", False),
    "uefa.nations": ("Nations League (Suomi)", True),
    "fifa.friendly": ("Maaottelu (Suomi)", True),
    "fifa.worldq.uefa": ("MM-karsinta (Suomi)", True),
    "uefa.euroq": ("EM-karsinta (Suomi)", True),
}

def get_json(url):
    with _lock:
        hit = _cache.get(url)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "urheilu-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read().decode("utf-8"))
    with _lock:
        _cache[url] = (time.time(), data)
    return data

# Lähetysoikeudet Suomessa (kausi 2026-27, tarkista ajantasaisuus tilaajapalvelusta)
TV = {"f1": "Viaplay", "nhl": "Viaplay", "eng.1": "Viaplay", "ger.1": "Viaplay", "fra.1": "Viaplay",
      "eng.2": "Viaplay", "uefa.europa": "Viaplay", "uefa.champions": "MTV Katsomo+ (osa MTV3)",
      "ita.1": "MTV Katsomo+", "swe.1": "MTV Katsomo+", "fin.1": "Ruutu+ (osa Yle)",
      "uefa.nations": "Yle / MTV (tarkista)", "fifa.friendly": "Yle / MTV (tarkista)",
      "fifa.worldq.uefa": "Yle / MTV (tarkista)", "uefa.euroq": "Yle / MTV (tarkista)",
      "esp.1": "tarkista", "tennis": "tarkista",
      "rally": "MTV Katsomo+ / Yle Areena (tarkista)", "athletics": "Yle / MTV Katsomo+ (tarkista)",
      "winter": "Yle / Viaplay / MTV Katsomo+ (tarkista)",
      "golf": "Viaplay (tarkista)", "f23": "Viaplay (tarkista)", "endurance": "Eurosport / HBO Max (tarkista)", "dakar": "tarkista"}
DUR = {"football": 125, "nhl": 170, "f1": 120}  # lähetyksen arvioitu kesto minuutteina

def ev(sport, title, sub, start, end=None, src="", **kw):
    tv = kw.pop("tv", None) or TV.get(kw.get("key") or sport, "")
    dur = kw.pop("dur", None)
    d = {"sport": sport, "title": title, "sub": sub, "start": start, "end": end, "src": src,
         "tv": tv, "dur": DUR.get(sport, 0) if dur is None else dur}
    d.update(kw)
    return d

def espn_range(url, d0, d1):
    try:
        return get_json(f"{url}?dates={d0:%Y%m%d}-{d1:%Y%m%d}")
    except Exception as e:
        err = e
    evs, day, ok = [], d0, False  # varakeino: päivä kerrallaan
    while day <= d1:
        try:
            evs += get_json(f"{url}?dates={day:%Y%m%d}").get("events", []); ok = True
        except Exception as e:
            err = e
        day += timedelta(days=1)
    if not ok:
        raise err
    return {"events": evs}

def f1():
    d = get_json("https://api.jolpi.ca/ergast/f1/current.json")
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
        j = get_json("https://api-web.nhle.com/v1/schedule/" + day.isoformat())
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

def football(slugs, d0, d1):
    def one(slug):
        name, nat = LEAGUES[slug]
        j = espn_range(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard", d0, d1)
        out, seen = [], set()
        for e in j.get("events", []):
            if e["id"] in seen: continue
            seen.add(e["id"])
            comp = sorted(e["competitions"][0]["competitors"], key=lambda c: c.get("homeAway") != "home")
            names = [c["team"]["displayName"] for c in comp]
            if nat and not any("Finland" in n for n in names):
                continue
            state = e.get("status", {}).get("type", {}).get("state", "pre")
            sc = f'{comp[0].get("score","")}–{comp[1].get("score","")}' if state != "pre" else ""
            out.append(ev("football", " – ".join(names), name, e["date"], src="ESPN", key=slug, kind="football",
                          id=e["id"], slug=slug, state=state, score=sc))
        return out
    return one, slugs

def tennis(d0, d1):
    out = []
    rng = d0.strftime("%Y%m%d") + "-" + d1.strftime("%Y%m%d")
    for tour in ("atp", "wta"):
        j = espn_range(f"https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard", d0, d1)
        for e in j.get("events", []):
            out.append(ev("tennis", e.get("name", "Turnaus"), tour.upper(), e["date"], e.get("endDate"), src="ESPN", allday=True))
    return out

# Sardinian MM-ralli 1.-4.10.2026, MTV:n lähetysaikataulu (Suomen aika). Lähde: atleetti.fi / etusuora.com
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

# Päivämääräkohtaiset (ei kellonaikoja) lisäkalenterit. Lähteet: FIA, FIA WEC, Dakar.com/Red Bull, Suomen Urheiluliitto
_D = [("f23", "Formula 2 – Qatar (Lusail)", "F2 · F3-kausi päättyi syyskuussa", "2026-11-27", "2026-11-29", "Viaplay (tarkista)"),
      ("f23", "Formula 2 – Abu Dhabi (Yas Marina)", "F2 · kauden päätös", "2026-12-04", "2026-12-06", "Viaplay (tarkista)"),
      ("endurance", "WEC 6 Hours of Barcelona", "WEC · kisapäivä su 18.10.", "2026-10-16", "2026-10-18", "Eurosport / HBO Max (tarkista)"),
      ("endurance", "WEC 6 Hours of Monza", "WEC · kisapäivä su 8.11.", "2026-11-06", "2026-11-08", "Eurosport / HBO Max (tarkista)"),
      ("dakar", "Dakar-ralli 2027", "Saudi-Arabia, KAEC", "2027-01-01", "2027-01-15", "tarkista"),
      ("athletics", "SM-maastot", "Maastojuoksun SM", "2026-10-24", "", "Yle (tarkista)"),
      ("athletics", "EM-maastot", "Maastojuoksun EM", "2026-12-13", "", "Yle (tarkista)")]
BUILTIN += [{"sport": a, "title": t, "sub": b, "start": c, "end": d, "dur": 0, "tv": tv} for a, t, b, c, d, tv in _D]

def manual():
    if not os.path.exists(MANUAL_FILE):
        sample = [{"sport": "rally", "title": "ESIMERKKI Ralli EM – EK1", "sub": "Suomen aika",
                   "start": "2000-01-01 10:00", "end": ""}]
        with open(MANUAL_FILE, "w", encoding="utf-8") as f:
            json.dump(sample, f, ensure_ascii=False, indent=2)
    with open(MANUAL_FILE, encoding="utf-8") as f:
        items = json.load(f)
    items = BUILTIN + items
    # start kirjoitetaan Suomen ajassa -> selain muuntaa (local=True)
    return [dict(ev(i["sport"], i["title"], i.get("sub", ""), i["start"], i.get("end") or None, "omat_tapahtumat.json", tv=i.get("tv"), dur=i.get("dur")), local=True) for i in items]

def build(sources, slugs):
    today = datetime.now(timezone.utc).date()
    d0, d1 = today - timedelta(days=8), today + timedelta(days=21)
    jobs = []
    if "f1" in sources: jobs.append(("Formula 1", f1))
    if "nhl" in sources: jobs.append(("NHL", lambda: nhl(d0, d1)))
    if "golf" in sources: jobs.append(("Golf", lambda: golf(d0, d1)))
    if "tennis" in sources: jobs.append(("Tennis", lambda: tennis(d0, d1)))
    if "manual" in sources: jobs.append(("Omat tapahtumat", manual))
    if "football" in sources:
        one, _ = football(slugs, d0, d1)
        for s in slugs:
            if s in LEAGUES:
                jobs.append((LEAGUES[s][0], (lambda s=s: one(s))))
    events, errors = [], []
    def run(job):
        try: return job[1](), None
        except Exception as e: return [], f"{job[0]}: {e}"[:70]
    with ThreadPoolExecutor(8) as ex:
        for res, err in ex.map(run, jobs):
            events += res
            if err: errors.append(err)
    return {"events": events, "errors": errors, "leagues": {k: v[0] for k, v in LEAGUES.items()}}

def detail(q):
    g = lambda k: q.get(k, [""])[0]
    kind, lines = g("kind"), []
    if kind == "football":
        j = get_json(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{g('slug')}/summary?event={g('id')}")
        v = j.get("gameInfo", {}).get("venue", {}).get("fullName")
        if v: lines.append("Paikka: " + v)
        for k in j.get("keyEvents", []):
            if any(x in k.get("type", {}).get("text", "") for x in ("Goal", "Card", "Penalty")):
                lines.append(f'{k.get("clock", {}).get("displayValue", "")} {k.get("shortText") or k.get("text", "")}')
    elif kind == "nhl":
        j = get_json(f"https://api-web.nhle.com/v1/gamecenter/{g('id')}/landing")
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
            j = get_json(f"https://api.jolpi.ca/ergast/f1/current/{g('round')}/{ep[0]}.json")
            rs = j["MRData"]["RaceTable"]["Races"]
            for r in (rs[0].get(ep[1], []) if rs else [])[:20]:
                t = r.get("Time", {}).get("time") or r.get("Q3") or r.get("Q2") or r.get("Q1") or r.get("status", "")
                lines.append(f'{r["position"]}. {r["Driver"]["familyName"]} – {r["Constructor"]["name"]}  {t}')
    return {"lines": lines or ["Tuloksia tai lisätietoja ei ole vielä saatavilla."]}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/events":
            q = parse_qs(u.query)
            src = (q.get("sources", [""])[0]).split(",")
            lg = [x for x in (q.get("leagues", [""])[0]).split(",") if x]
            body, ctype = json.dumps(build(src, lg)).encode(), "application/json"
        elif u.path == "/api/detail":
            try: res = detail(parse_qs(u.query))
            except Exception as e: res = {"lines": ["Haku epäonnistui: " + str(e)[:60]]}
            body, ctype = json.dumps(res).encode(), "application/json"
        elif u.path == "/api/leagues":
            body, ctype = json.dumps({k: v[0] for k, v in LEAGUES.items()}).encode(), "application/json"
        else:
            body, ctype = PAGE.encode("utf-8"), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

PAGE = r"""<!doctype html><html lang="fi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
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
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
h1{font:800 26px 'Barlow Condensed',sans-serif;margin:0 auto 0 0;letter-spacing:.3px}
.tabs{display:flex;gap:6px}.tabs button,#gear{font:600 16px 'Barlow Condensed',sans-serif;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:7px 14px}
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
.tabs{flex-wrap:wrap}.tabs button{padding:6px 10px}#tq{display:flex;gap:6px;max-width:760px;margin:10px auto 0;padding:0 14px}#tq[hidden]{display:none}#tq input{flex:1;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:8px;font:16px Barlow}#tq button{background:var(--acc);color:#111;border:0;border-radius:8px;padding:0 16px;font:800 16px 'Barlow Condensed'}
footer{color:var(--mute);font-size:12.5px;padding:0 14px 40px;max-width:760px;margin:auto}
dialog{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:12px;max-width:440px;width:92%;padding:18px}
dialog::backdrop{background:#000a}dialog h3{font:800 20px 'Barlow Condensed';margin:14px 0 6px}
label{display:flex;gap:8px;align-items:center;padding:4px 0}.chips{display:flex;gap:6px;flex-wrap:wrap}
.chips label{border:1px solid var(--line);border-radius:20px;padding:5px 12px}
.sw{width:10px;height:10px;border-radius:50%;background:var(--c)}
#ok{margin-top:16px;width:100%;padding:10px;font:800 18px 'Barlow Condensed';background:var(--acc);border:0;border-radius:8px}
</style></head><body>
<header><h1>Urheilu</h1>
<div class="tabs"><button data-v="today">Tänään</button><button data-v="yday">Eilen</button><button data-v="weekend">Vkonloppu</button><button data-v="week">Viikko</button><button data-v="lastweek">Viime vko</button><button data-v="next">Seuraavat</button></div>
<button id="gear" aria-label="Asetukset">Asetukset</button></header>
<main id="out"><div class="msg">Ladataan…</div></main>
<footer id="foot"></footer>
<dialog id="dlg"><h3>Teema</h3><div class="chips" id="themes"></div>
<h3>Lajit</h3><div id="sp"></div><h3>Jalkapallosarjat</h3><div id="lg"></div>
<button id="ok">Tallenna</button></dialog>
<script>
const TZ='Europe/Helsinki';
const SP={f1:{n:'Formula 1',c:'#e10600',s:'f1',src:'Jolpica F1 (Ergast-data)'},
rally:{n:'Ralli (EM/MM)',c:'#ff8a2b',s:'manual',src:'omat_tapahtumat.json'},
football:{n:'Jalkapallo',c:'#2fbf6b',s:'football',src:'ESPN'},
nhl:{n:'NHL',c:'#4a90e2',s:'nhl',src:'NHL Web API'},
tennis:{n:'Tennis',c:'#d7e84a',s:'tennis',src:'ESPN (turnaukset)'},
athletics:{n:'Yleisurheilu',c:'#e0457b',s:'manual',src:'omat_tapahtumat.json'},
f23:{n:'F2 / F3',c:'#b04bff',s:'manual',src:'FIA (kalenteri)'},
golf:{n:'Golf',c:'#00b3a4',s:'golf',src:'ESPN (turnaukset)'},
endurance:{n:'Kestävyysajot (WEC, GT)',c:'#f2b705',s:'manual',src:'FIA WEC (kalenteri)'},
dakar:{n:'Dakar',c:'#c97b3a',s:'manual',src:'Dakar.com / Red Bull'},
winter:{n:'Talvilajit',c:'#7fd6ff',s:'manual',src:'omat_tapahtumat.json'}};
const THEMES={auto:'Yleinen',futis:'Futis',ralli:'Ralli',formula:'Formula',tennis:'Tennis',nhl:'NHL'};
const DEF={theme:'auto',view:'today',sports:Object.fromEntries(Object.keys(SP).map(k=>[k,true])),
leagues:['eng.1','ger.1','uefa.champions','fin.1','uefa.nations','fifa.friendly','fifa.worldq.uefa','uefa.euroq']};
let st=DEF,LG={};
try{st={...DEF,...JSON.parse(localStorage.getItem('urh')||'{}')}}catch(e){}
st.sports={...DEF.sports,...st.sports};if(st.view==='team')st.view='today';const save=()=>{try{localStorage.setItem('urh',JSON.stringify(st))}catch(e){}};
const $=s=>document.querySelector(s);
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
let DATA=null;
async function load(){
const on=Object.keys(SP).filter(k=>st.sports[k]);const srcs=[...new Set(on.map(k=>SP[k].s))];
$('#out').innerHTML='<div class="msg">Ladataan…</div>';
try{const r=await fetch('/api/events?sources='+srcs.join(',')+'&leagues='+st.leagues.join(','));DATA=await r.json();LG=DATA.leagues}
catch(e){$('#out').innerHTML='<div class="msg">Palvelimeen ei saatu yhteyttä. Onko skripti käynnissä?</div>';return}
render()}
const OPEN=new Set(),DET={},EVS={};
function stateOf(e,now){if(e.state)return e.state;if(e.range)return'in';const t=e.a.getTime();return now<t?'pre':now<(e.z?e.z.getTime():t+(e.dur||120)*6e4)?'in':'post'}
function prep(e){const a=e.local?hel(e.start):new Date(e.start);if(isNaN(a))return null;
let b=e.end?(e.local?hel(e.end):new Date(e.end)):null;const ad=e.allday||(e.local&&e.start.length<=10);
if(b&&e.local&&e.end.length<=10)b=new Date(b.getTime()+864e5-6e4);
const z=b||(e.dur?new Date(a.getTime()+e.dur*6e4):(ad?new Date(a.getTime()+864e5-6e4):null));
return{...e,a,b,z,ad}}
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
const badge=(e.score?`<span class="sc">${e.score}</span>`:'')+(stt==='in'&&!multi?'<span class="lv">LIVE</span>':'');
const pct=stt==='in'&&!multi&&e.z?Math.min(100,(now-e.a)/(e.z-e.a)*100):0;
let det='';if(open){const m=`<div class="m">Lähetys ${e.ad?dshort(e.a):tm(e.a)}${e.z&&!e.ad?'–'+tm(e.z):''}${e.dur&&!e.end?' (arvio)':''} · 📺 ${e.tv||'ei tietoa'} · Lähde: ${e.src}</div>`;
det=`<div class="det">${m}${(DET[key]||[]).map(x=>`<div>${x}</div>`).join('')}</div>`}
return `<div class="row ${stt==='post'?'post':''}" data-k="${key.replace(/"/g,'&quot;')}" style="--c:${S.c}">${time}<div><div class="n">${e.title}${badge}</div><div class="s"><b>${S.n}</b> · ${e.rel?e.rel+' · ':''}${sub||''}${e.tv?' · 📺 '+e.tv:''}</div>${det}</div>${pct?`<div class="bar" style="width:${pct}%"></div>`:''}</div>`}
function listHtml(now){
const ks=keys(),by={};ks.forEach(k=>by[k]=[]);
for(const e0 of DATA.events){if(!st.sports[e0.sport])continue;const e=prep(e0);if(!e)continue;const k0=dkey(e.a);
if(e.b&&dkey(e.b)!==k0){const k1=dkey(e.b);for(const k of ks)if(k>=k0&&k<=k1)by[k].push({...e,range:true,first:k===k0})}
else if(by[k0])by[k0].push(e)}
const today=dkey(new Date()),past=st.view==='yday'||st.view==='lastweek';let h='';
for(const k of(past?[...ks].reverse():ks)){const L=by[k].sort((x,y)=>(!!y.range-!!x.range)||x.a-y.a);if(!L.length)continue;
const lab=new Date(k+'T12:00:00Z').toLocaleDateString('fi-FI',{timeZone:'UTC',weekday:'long',day:'numeric',month:'numeric'});
h+=`<h2>${k===today?'Tänään · ':''}${lab}</h2>`;let placed=k!==today;const nl=`<div class="now">Nyt ${tm(new Date(now))}</div>`;
for(const e of L){if(!placed&&!e.range&&e.a.getTime()>now){h+=nl;placed=true}h+=rowHtml(e,now,false)}
if(!placed)h+=nl}
return h||'<div class="msg">Ei tapahtumia valituilla asetuksilla tälle jaksolle.<br>Ralli, yleisurheilu ja talvilajit lisätään tiedostoon omat_tapahtumat.json.</div>'}
function nextHtml(now){let h='';
for(const[k,S]of Object.entries(SP)){if(!st.sports[k])continue;
const L=DATA.events.filter(e=>e.sport===k).map(prep).filter(e=>e&&(e.z||e.a).getTime()>now).sort((x,y)=>x.a-y.a).slice(0,3);
h+=`<h2 style="color:${S.c}">${S.n}</h2>`;
if(!L.length){h+='<div class="msg" style="padding:8px 0;text-align:left">Ei tulevia tapahtumia 3 viikon sisällä.</div>';continue}
L.forEach((e,i)=>{if(i===0)e.rel=relTxt(e,now);h+=rowHtml(e,now,true)})}
return h}
function render(){
if(!DATA)return;document.body.dataset.theme=st.theme==='auto'?'':st.theme;
document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.v===st.view));
const now=Date.now();
let h=st.view==='next'?nextHtml(now):listHtml(now);
if(DATA.errors.length)h+=`<div class="err">Osa lähteistä ei vastannut: ${DATA.errors.join(', ')}</div>`;
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
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{st.view=b.dataset.v;save();render()});
$('#gear').onclick=()=>{
$('#themes').innerHTML=Object.entries(THEMES).map(([k,v])=>`<label><input type="radio" name="th" value="${k}" ${st.theme===k?'checked':''}>${v}</label>`).join('');
$('#sp').innerHTML=Object.entries(SP).map(([k,v])=>`<label style="--c:${v.c}"><input type="checkbox" data-sp="${k}" ${st.sports[k]?'checked':''}><span class="sw"></span>${v.n}</label>`).join('');
$('#lg').innerHTML=Object.entries(LG).map(([k,v])=>`<label><input type="checkbox" data-lg="${k}" ${st.leagues.includes(k)?'checked':''}>${v}</label>`).join('');
$('#dlg').showModal()};
$('#ok').onclick=()=>{
st.theme=(document.querySelector('input[name=th]:checked')||{value:'auto'}).value;
document.querySelectorAll('[data-sp]').forEach(i=>st.sports[i.dataset.sp]=i.checked);
st.leagues=[...document.querySelectorAll('[data-lg]')].filter(i=>i.checked).map(i=>i.dataset.lg);
save();$('#dlg').close();load()};
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
