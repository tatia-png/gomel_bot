"""Транспорт Гомеля — Telegram Mini App (FastAPI + парсер gomel.btrans.by)."""
import asyncio, os, re, time
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ⚠️ Меняем источник на BTrans
BASE = "https://gomel.btrans.by"
SECTIONS = {"bus": "/bus/", "trolleybus": "/trolleybus/"}
CACHE_TTL = 600  # секунд

app = FastAPI(title="Gomel Transport")
_cache: dict = {}
_session: aiohttp.ClientSession | None = None

async def fetch(url: str) -> str:
    """Скачивает страницу с BTrans (aiohttp с заголовками браузера)."""
    global _session
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            timeout=aiohttp.ClientTimeout(total=20),
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

# ---------- ПАРСЕРЫ ----------
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")

async def page_lines(url: str) -> list[str]:
    html = await fetch(url)
    if not html:
        return []
    text = soup_of(html).get_text("\n")
    return [clean(l) for l in text.splitlines() if clean(l)]

async def parse_routes(kind: str) -> list[dict]:
    url = BASE + SECTIONS[kind]
    html = await fetch(url)
    if not html:
        return []
    
    soup = soup_of(html)
    found = {}
    
    # Ищем ссылки на маршруты (у BTrans структура вида /bus/1/)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(rf"/{kind}/\d+", href):
            m = re.search(r"/(\d+)", href)
            if m:
                num = m.group(1)
                name = clean(a.get_text())
                # Пытаемся вытащить название маршрута (обычно в тексте ссылки)
                if name:
                    found[num] = name

    # Сортируем по номеру
    def keyf(n):
        return int(n) if n.isdigit() else 9999
        
    return [{"type": kind, "number": n, "name": found[n], "url": f"{BASE}{SECTIONS[kind]}{n}/"} 
            for n in sorted(found.keys(), key=keyf)]

async def parse_stops(kind: str, number: str, direction: int = 0) -> list[dict]:
    # У BTrans страница маршрута обычно содержит список остановок
    url = f"{BASE}{SECTIONS[kind]}{number}/"
    lines = await page_lines(url)
    
    # Ищем блок с остановками (обычно это просто список строк)
    stops = []
    for i, line in enumerate(lines):
        # Простой фильтр: названия остановок обычно короче 50 символов и не содержат время
        if 3 < len(line) < 50 and not TIME_RE.search(line) and not re.search(r"^\d+$", line):
            # Исключаем служебные слова
            if not re.search(r"(расписание|маршрут|остановк|время|режим)", line, re.I):
                stops.append({"name": line, "pos": i})

    # Упрощенное деление на направления (BTrans часто показывает два списка)
    if len(stops) > 10:
        mid = len(stops) // 2
        chosen = stops[:mid] if direction == 0 else stops[mid:]
    else:
        chosen = stops

    return [{"id": i, "name": s["name"], "pos": s["pos"], "src": "direct"} for i, s in enumerate(chosen)]

def parse_times(lines: list[str], pos: int) -> list[str]:
    # Собираем все времена, которые идут после нашей остановки
    times = []
    for i in range(pos + 1, len(lines)):
        if TIME_RE.search(lines[i]):
            # Если нашли время, проверяем, не начался ли следующий блок
            times.extend(TIME_RE.findall(lines[i]))
            if len(times) > 50: # Защита от слишком длинных списков
                break
        elif times: # Если время уже началось, но мы наткнулись на текст, значит блок кончился
            break
    return sorted(set(times))

# ---------- Демо-данные ----------
DEMO_ROUTES = [{"type": "bus", "number": "1", "name": "Демо-маршрут", "url": ""}]
DEMO_STOPS = [["Вокзал", "Площадь Ленина", "Клёнковский"]]

@app.get("/api/routes")
async def api_routes(type: str | None = None):
    kinds = [type] if type in SECTIONS else list(SECTIONS)
    res = await asyncio.gather(*(parse_routes(k) for k in kinds))
    routes = [r for lst in res for r in lst]
    demo = not routes
    if demo:
        routes = [r for r in DEMO_ROUTES if r["type"] in kinds]
    for r in routes:
        r.pop("url", None)
    return {"demo": demo, "routes": routes}

@app.get("/api/stops/{type}/{number}")
async def api_stops(type: str, number: str, direction: int = 0):
    if type not in SECTIONS:
        raise HTTPException(404, "Неизвестный тип")
    stops = await parse_stops(type, number, direction)
    demo = not stops
    if demo:
        stops = [{"id": i, "name": n, "url": ""} for i, n in enumerate(DEMO_STOPS[0])]
    return {"demo": demo, "direction": direction % 2, "stops": [{"id": s["id"], "name": s["name"]} for s in stops]}

@app.get("/api/schedule/{type}/{number}/{stop_id}")
async def api_schedule(type: str, number: str, stop_id: int, direction: int = 0):
    if type not in SECTIONS:
        raise HTTPException(404, "Неизвестный тип")
    
    # Скачиваем страницу заново, чтобы найти время
    url = f"{BASE}{SECTIONS[kind]}{number}/" if (kind:=type) else ""
    lines = await page_lines(url)
    stops = await parse_stops(type, number, direction)
    stop = next((s for s in stops if s["id"] == stop_id), None)
    
    times = []
    if stop:
        times = parse_times(lines, stop["pos"])
    
    demo = not times
    if demo:
        times = ["08:00", "09:30", "12:00"]
    
    return {"demo": demo, "stop": stop["name"] if stop else "", "times": times}

# ---------- Фронтенд ----------
STATIC = Path(__file__).parent / "static"

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")

app.mount("/", StaticFiles(directory=STATIC), name="static")
