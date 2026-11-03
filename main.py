"""Транспорт Гомеля — Telegram Mini App (FastAPI + парсер gomel.btrans.by)."""
import asyncio, os, re, time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import aiohttp
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = "https://gomel.btrans.by"
SECTIONS = {
    "bus": "/avtobus/", 
    "trolleybus": "/trollejbus/", 
    "marshrutka": "/marshrutka/"
}
CACHE_TTL = 600

app = FastAPI(title="Gomel Transport")
_cache: dict = {}
_session: aiohttp.ClientSession | None = None

async def fetch(url: str) -> str:
    global _session
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
            timeout=aiohttp.ClientTimeout(total=15),
        )
    try:
        async with _session.get(url) as r:
            r.raise_for_status()
            html = await r.text()
    except Exception as e:
        print(f"fetch failed for {url}: {e!r}")
        return ""
    _cache[url] = (time.time(), html)
    return html

def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")

def clean(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()

TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")

async def parse_routes(kind: str) -> list[dict]:
    html = await fetch(BASE)
    if not html:
        return []
    soup = soup_of(html)
    found = {}
    
    pattern = re.compile(rf"{SECTIONS[kind]}(\d+[а-яa-z]?)")
    for a in soup.find_all("a", href=True):
        m = pattern.search(a["href"])
        if m:
            num = m.group(1)
            text = clean(a.get_text())
            route_name = text.split(":", 1)[1].strip() if ":" in text else text
            if len(route_name) < 3:
                route_name = f"Маршрут №{num}"
            found[num] = route_name

    def keyf(n):
        m = re.match(r"(\d+)(.*)", n)
        return (int(m.group(1)), m.group(2)) if m else (9999, n)

    return [{"type": kind, "number": n, "name": found[n], "url": f"{BASE}{SECTIONS[kind]}{n}"} 
            for n in sorted(found.keys(), key=keyf)]

async def parse_stops(kind: str, number: str):
    url = f"{BASE}{SECTIONS[kind]}{number}"
    html = await fetch(url)
    if not html:
        return []
    soup = soup_of(html)
    
    stops = []
    for a in soup.find_all("a", href=True):
        if "/stop/" in a["href"]:
            name = clean(a.get_text())
            if name and 2 < len(name) < 50:
                stop_url = f"{BASE}{a['href']}" if a['href'].startswith('/') else a['href']
                stops.append({"id": len(stops), "name": name, "url": stop_url})

    seen = set()
    clean_stops = []
    for s in stops:
        if s["name"] not in seen:
            seen.add(s["name"])
            clean_stops.append(s)
            
    mid = len(clean_stops) // 2 if len(clean_stops) > 5 else len(clean_stops)
    return [clean_stops[:mid], clean_stops[mid:]]

async def parse_stop_times(stop_url: str, day_type: str):
    html = await fetch(stop_url)
    if not html:
        return []
    soup = soup_of(html)
    text = soup.get_text("\n")
    lines = [clean(l) for l in text.splitlines() if clean(l)]
    
    times = []
    is_weekend = day_type == "weekend"
    
    for i, line in enumerate(lines):
        if TIME_RE.search(line):
            context = " ".join(lines[max(0, i-3):i+3]).lower()
            if is_weekend and any(w in context for w in ["вых", "сб", "вс", "weekend"]):
                times.extend(TIME_RE.findall(line))
            elif not is_weekend and any(w in context for w in ["буд", "пн", "вт", "week"]):
                times.extend(TIME_RE.findall(line))
            elif not is_weekend and not any(w in context for w in ["вых", "сб", "вс"]):
                times.extend(TIME_RE.findall(line))

    return sorted(list(set(times)))

@app.get("/api/routes")
async def api_routes(type: str | None = None):
    kinds = [type] if type in SECTIONS else list(SECTIONS)
    res = await asyncio.gather(*(parse_routes(k) for k in kinds))
    routes = [r for lst in res for r in lst]
    demo = not routes
    if demo:
        routes = [{"type": "bus", "number": "8", "name": "Вокзал — Мильча"}]
    return {"demo": demo, "routes": routes}

@app.get("/api/stops/{type}/{number}")
async def api_stops(type: str, number: str, direction: int = 0):
    if type not in SECTIONS:
        raise HTTPException(404, "Неизвестный тип")
    all_dirs = await parse_stops(type, number)
    chosen = all_dirs[direction % 2] if len(all_dirs) > 1 else all_dirs[0]
    demo = not chosen
    if demo:
        chosen = [{"id": 0, "name": "Демо-остановка", "url": ""}]
    return {"demo": demo, "direction": direction % 2, "stops": chosen}

@app.get("/api/schedule/{type}/{number}/{stop_id}")
async def api_schedule(type: str, number: str, stop_id: int, direction: int = 0, day: str | None = None):
    if type not in SECTIONS:
        raise HTTPException(404, "Неизвестный тип")
    
    if not day:
        now = datetime.now(timezone.utc) + timedelta(hours=3)
        day = "weekend" if now.weekday() >= 5 else "week"

    all_dirs = await parse_stops(type, number)
    chosen = all_dirs[direction % 2] if len(all_dirs) > 1 else all_dirs[0]
    
    stop = next((s for s in chosen if s["id"] == stop_id), None)
    times = []
    
    if stop and stop.get("url"):
        times = await parse_stop_times(stop["url"], day)
        
    return {"demo": False, "stop": stop["name"] if stop else "", "times": times, "day": day}

STATIC = Path(__file__).parent / "static"

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

app.mount("/", StaticFiles(directory=STATIC), name="static")
