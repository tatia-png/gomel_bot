"""Транспорт Гомеля — Telegram Mini App (FastAPI + парсер gomel.btrans.by)."""
import asyncio, os, re, time
from pathlib import Path

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
    html = await fetch(BASE)
    if not html:
        return []
    soup = soup_of(html)
    found = {}
    
    # Ищем все ссылки на маршруты
    pattern = re.compile(rf"{SECTIONS[kind]}(\d+[а-яa-z]?)")
    for a in soup.find_all("a", href=True):
        m = pattern.search(a["href"])
        if m:
            num = m.group(1)
            # Берем текст ссылки и очищаем от лишнего
            text = clean(a.get_text())
            
            # Ищем "Автобус 2: Начало - Конец" или просто "Начало - Конец"
            route_name = text
            if ":" in text:
                route_name = text.split(":", 1)[1].strip()
            
            # Если название не найдено в ссылке, пробуем найти в родительском блоке
            if len(route_name) < 4:
                parent_text = clean(a.parent.get_text()) if a.parent else ""
                if ":" in parent_text:
                    route_name = parent_text.split(":", 1)[1].strip()

            if not route_name or len(route_name) < 3:
                route_name = f"Маршрут №{num}"
                
            found[num] = route_name

    def keyf(n):
        m = re.match(r"(\d+)(.*)", n)
        return (int(m.group(1)), m.group(2)) if m else (9999, n)

    return [{"type": kind, "number": n, "name": found[n], "url": f"{BASE}{SECTIONS[kind]}{n}"} 
            for n in sorted(found.keys(), key=keyf)]

async def parse_stops_and_times(kind: str, number: str):
    url = f"{BASE}{SECTIONS[kind]}{number}"
    html = await fetch(url)
    if not html:
        return [], []
    soup = soup_of(html)
    
    stops = []
    
    # САМЫЙ ТОЧНЫЙ СПОСОБ: Ищем ссылки на остановки.
    # В BTrans они обычно ведут на /stop/... и внутри содержат название.
    for a in soup.find_all("a", href=True):
        if "/stop/" in a["href"] or "ostanovka" in a["href"].lower():
            stop_name = clean(a.get_text())
            if not stop_name or len(stop_name) < 2:
                continue
                
            # Ищем время. Оно может быть в этом же блоке (родителе) или рядом.
            # Обычно время лежит в теге <span> или просто текстом.
            parent = a.parent
            if parent:
                parent_text = parent.get_text(" ", strip=True)
                time_match = TIME_RE.search(parent_text)
                if time_match:
                    stop_time = time_match.group(0)
                else:
                    # Если время не в родителе, ищем в следующем соседе
                    next_sibling = parent.find_next_sibling()
                    if next_sibling:
                        next_text = next_sibling.get_text(" ", strip=True)
                        time_match = TIME_RE.search(next_text)
                        stop_time = time_match.group(0) if time_match else ""
                    else:
                        stop_time = ""
            else:
                stop_time = ""
                
            # Добавляем остановку
            stops.append({"id": len(stops), "name": stop_name, "time": stop_time})

    # Если ссылок не нашли, пробуем старый метод (по тексту)
    if not stops:
        text = soup.get_text("\n")
        lines = [clean(l) for l in text.splitlines() if clean(l)]
        start_collecting = False
        for line in lines:
            if re.search(r"(Автобус|Троллейбус)\s*№\s*\d+", line, re.I):
                start_collecting = True
                continue
            if not start_collecting:
                continue
                
            if TIME_RE.search(line):
                continue
                
            if 2 < len(line) < 50 and not re.search(r"(мин|час|рейс|маршрут|расписание)", line, re.I):
                stops.append({"id": len(stops), "name": line, "time": ""})

    # Убираем дубли
    seen = set()
    clean_stops = []
    for s in stops:
        if s["name"] not in seen:
            seen.add(s["name"])
            clean_stops.append(s)

    # Делим на два направления
    mid = len(clean_stops) // 2 if len(clean_stops) > 5 else len(clean_stops)
    all_stops = [
        {"direction": 0, "stops": clean_stops[:mid]},
        {"direction": 1, "stops": clean_stops[mid:]}
    ]
    
    return all_stops, []

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
    all_stops, _ = await parse_stops_and_times(type, number)
    chosen = all_stops[direction % 2] if len(all_stops) > 1 else all_stops[0]
    
    # Ищем выбранную остановку и берем ЕЕ время
    stop = next((s for s in chosen["stops"] if s["id"] == stop_id), None)
    
    times = []
    if stop and stop.get("time"):
        times = [stop["time"]]
    else:
        # Если точное время не нашли, отдаем заглушку (чтобы не ломалось)
        times = ["05:55", "06:20", "07:10"]
        
    demo = not (stop and stop.get("time"))
    return {"demo": demo, "stop": stop["name"] if stop else "Остановка", "times": times}

STATIC = Path(__file__).parent / "static"

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")

app.mount("/", StaticFiles(directory=STATIC), name="static")
