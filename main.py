"""Транспорт Гомеля — Telegram Mini App (FastAPI + парсер gomel.btrans.by)."""
import asyncio, os, re, time
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = "https://gomel.btrans.by"
SECTIONS = {"bus": "/avtobus/", "trolleybus": "/trollejbus/"}
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
    # 1. Заходим на главную, чтобы собрать номера маршрутов и их названия
    html = await fetch(BASE)
    if not html:
        return []
    soup = soup_of(html)
    found = {}
    
    # Ищем ссылки вида /avtobus/8 или /trollejbus/10
    pattern = re.compile(rf"{SECTIONS[kind]}(\d+[а-яa-z]?)")
    for a in soup.find_all("a", href=True):
        m = pattern.search(a["href"])
        if m:
            num = m.group(1)
            # Ищем название маршрута в тексте ссылки или рядом
            text = clean(a.get_text())
            parent_text = clean(a.parent.get_text()) if a.parent else ""
            # Если в тексте ссылки только номер, пробуем взять из родителя
            route_name = text if len(text) > 3 else parent_text
            found[num] = route_name

    def keyf(n):
        m = re.match(r"(\d+)(.*)", n)
        return (int(m.group(1)), m.group(2)) if m else (9999, n)

    return [{"type": kind, "number": n, "name": found[n] if found[n] else "Маршрут", "url": f"{BASE}{SECTIONS[kind]}{n}"} 
            for n in sorted(found.keys(), key=keyf)]

async def parse_stops_and_times(kind: str, number: str):
    url = f"{BASE}{SECTIONS[kind]}{number}"
    html = await fetch(url)
    if not html:
        return [], []
    soup = soup_of(html)
    
    stops = []
    times = []

    # На сайте BTrans остановки с временем находятся в блоке с классом, содержащим "timeline" или "route"
    # Мы ищем все элементы, которые похожи на остановки в таймлайне
    # Обычно это ссылки или div'ы, внутри которых есть название и время
    
    # Способ 1: Ищем все ссылки на остановки (в них есть название)
    for a in soup.find_all("a", href=True):
        if "/stop/" in a["href"] or "ostanovka" in a["href"].lower():
            name = clean(a.get_text())
            if name and len(name) > 2:
                # Ищем время рядом с этой ссылкой
                time_match = TIME_RE.search(a.parent.get_text() if a.parent else "")
                if time_match:
                    times.append(time_match.group(0))
                stops.append({"id": len(stops), "name": name})

    # Способ 2: Если ссылок нет, ищем просто текст, где чередуются время и название
    if not stops:
        lines = [clean(l) for l in soup.get_text("\n").splitlines() if clean(l)]
        for i, line in enumerate(lines):
            # Ищем строку с названием остановки (обычно это просто текст без цифр и времени)
            if 3 < len(line) < 50 and not TIME_RE.search(line) and not re.search(r"(мин|час|расписание|маршрут|троллейбус|автобус)", line, re.I):
                # Проверяем, есть ли время на следующей строке или через одну
                if i + 1 < len(lines) and TIME_RE.search(lines[i+1]):
                    stops.append({"id": len(stops), "name": line})
                    times.append(TIME_RE.search(lines[i+1]).group(0))
                elif i + 2 < len(lines) and TIME_RE.search(lines[i+2]):
                    stops.append({"id": len(stops), "name": line})
                    times.append(TIME_RE.search(lines[i+2]).group(0))

    # Убираем дубли
    seen = set()
    clean_stops = []
    for s in stops:
        if s["name"] not in seen:
            seen.add(s["name"])
            clean_stops.append(s)

    # Делим на два направления (для простоты пополам)
    mid = len(clean_stops) // 2 if len(clean_stops) > 10 else len(clean_stops)
    
    all_stops = [
        {"direction": 0, "stops": clean_stops[:mid]},
        {"direction": 1, "stops": clean_stops[mid:]}
    ]
    return all_stops, list(set(times))

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
    all_stops, _ = await parse_stops_and_times(type, number)
    chosen = all_stops[direction % 2] if len(all_stops) > 1 else all_stops[0]
    demo = not chosen["stops"]
    if demo:
        chosen["stops"] = [{"id": 0, "name": "Демо-остановка"}]
    return {"demo": demo, "direction": direction % 2, "stops": chosen["stops"]}

@app.get("/api/schedule/{type}/{number}/{stop_id}")
async def api_schedule(type: str, number: str, stop_id: int, direction: int = 0):
    if type not in SECTIONS:
        raise HTTPException(404, "Неизвестный тип")
    all_stops, times = await parse_stops_and_times(type, number)
    chosen = all_stops[direction % 2] if len(all_stops) > 1 else all_stops[0]
    demo = not times
    if demo:
        times = ["05:55", "06:20", "07:10"]
    return {"demo": demo, "stop": "Выбранная остановка", "times": sorted(times)}

STATIC = Path(__file__).parent / "static"

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")

app.mount("/", StaticFiles(directory=STATIC), name="static")
