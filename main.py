"""Транспорт Гомеля — Telegram Mini App (FastAPI + парсер gomeltrans.net)."""
import asyncio, os, re, time
from pathlib import Path
from urllib.parse import urljoin

from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = "https://gomeltrans.net"
SECTIONS = {"bus": "/routes/bus/", "trolleybus": "/routes/trolleybus/"}
CACHE_TTL = 600  # секунд

app = FastAPI(title="Gomel Transport")
_cache: dict = {}

async def fetch(url: str) -> str:
    """Скачивает страницу через curl_cffi (подделка браузера Chrome)."""
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    
    try:
        # Запускаем синхронный requests в отдельном потоке, чтобы не блокировать FastAPI
        def _get():
            return curl_requests.get(url, impersonate="chrome", timeout=15)
            
        response = await asyncio.to_thread(_get)
        response.raise_for_status()
        html = response.text
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
STOP_KW = re.compile(
    r"(\bул\.|\bулица|\bпросп|\bпр-т|\bпроспект|\bпер\.|\bпереулок|\bпл\.|\bплощадь|"
    r"\bмикрорайон|\bмкр|\bбульвар|\bб-р|\bшоссе|\bпроезд|\bнабережная|\bвокзал|"
    r"\bпос\.|\bпоселок|\bпосёлок|\bтупик)", re.I)
NOISE = re.compile(
    r"^(расписание|режим работы|будни|выходн|рабоч|суббот|воскрес|ежедневно|интервал|"
    r"направлен|примечан|остановки?\b|время|дни\b|пн\b|вт\b|ср\b|чт\b|пт\b|сб\b|вс\b|"
    r"маршрут\b|новости|главная|©|копирование)", re.I)

async def page_lines(url: str) -> list[str]:
    html = await fetch(url)
    if not html:
        return []
    text = soup_of(html).get_text("\n")
    return [clean(l) for l in text.splitlines() if clean(l)]

def route_section(lines: list[str]) -> list[str]:
    start = next((i for i, l in enumerate(lines) if re.match(r"^расписание", l, re.I)), -1)
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^режим работы", lines[i], re.I)), len(lines))
    return lines[start + 1:end]

def parse_times(lines: list[str], pos: int | None = None, full: list[str] | None = None) -> list[str]:
    def grab(chunk):
        out = set()
        for l in chunk:
            for t in TIME_RE.findall(l):
                h, m = map(int, t.split(":"))
                if h < 24 and m < 60:
                    out.add(f"{h:02d}:{m:02d}")
        return sorted(out)

    if pos is not None:
        stop_idx = {s["pos"] for s in find_stop_lines(lines)}
        chunk = []
        for i in range(pos + 1, len(lines)):
            if i in stop_idx:
                break
            chunk.append(lines[i])
        times = grab(chunk)
        if times:
            return times
    return grab(lines) or grab(full or [])

def find_stop_lines(lines: list[str]) -> list[dict]:
    def cand(l):
        return (3 <= len(l) <= 70 and re.search(r"[А-Яа-яЁё]", l) and not TIME_RE.search(l)
                and not NOISE.match(l))
    ok = [bool(cand(l)) for l in lines]
    kw = [ok[i] and bool(STOP_KW.search(lines[i])) for i in range(len(lines))]
    res = []
    def neighbor_kw(i, step):
        j = i + step
        while 0 <= j < len(lines) and TIME_RE.search(lines[j]):
            j += step
        return 0 <= j < len(lines) and kw[j]
    for i, l in enumerate(lines):
        if kw[i] or (ok[i] and (neighbor_kw(i, -1) or neighbor_kw(i, 1))):
            res.append({"name": l, "pos": i})
    return res

def split_directions(stops: list[dict]) -> list[list[dict]]:
    for j in range(2, len(stops) - 1):
        if stops[j]["name"].lower() == stops[j - 1]["name"].lower():
            return [stops[:j], stops[j:]]
    return [stops, list(reversed(stops))]

async def route_lines(kind: str, number: str) -> tuple[list[str], list[str]]:
    lines = await page_lines(f"{BASE}{SECTIONS[kind]}{number}/")
    return route_section(lines), lines

async def parse_routes(kind: str) -> list[dict]:
    url = BASE + SECTIONS[kind]
    html = await fetch(url)
    if not html:
        return []
    soup = soup_of(html)
    found: dict[str, str] = {}
    pat = re.compile(re.escape(SECTIONS[kind]) + r"([0-9]+[а-яa-zэ]*)/?$", re.I)
    for a in soup.find_all("a", href=True):
        m = pat.search(a["href"])
        if m:
            found.setdefault(m.group(1), clean(a.get_text()))
    if not found:
        lines = route_section([clean(l) for l in soup.get_text("\n").splitlines() if clean(l)])
        for l in lines:
            m = re.match(r"^(?:№|N)?\s*(\d{1,3}[а-яa-zэ]?)\b[\s.:—-]*(.*)$", l, re.I)
            if m:
                found.setdefault(m.group(1), m.group(2))
    def keyf(n):
        m = re.match(r"(\d+)(.*)", n)
        return (int(m.group(1)), m.group(2)) if m else (9999, n)
    return [{"type": kind, "number": n, "name": re.sub(r"^№?\s*" + re.escape(n) + r"\b[\s.:—-]*", "", t),
             "url": f"{BASE}{SECTIONS[kind]}{n}/"} for n, t in sorted(found.items(), key=lambda x: keyf(x[0]))]

async def parse_stops(kind: str, number: str, direction: int = 0) -> list[dict]:
    section, full = await route_lines(kind, number)
    stops = find_stop_lines(section) or find_stop_lines(full)
    src = section if find_stop_lines(section) else full
    if not stops:
        return []
    chosen = split_directions(stops)[direction % 2]
    return [{"id": i, "name": s["name"], "pos": s["pos"], "src": "section" if src is section else "full"}
            for i, s in enumerate(chosen)]

# Эндпоинты API (как было раньше)
@app.get("/api/routes")
async def api_routes(type: str = Query(..., regex="^(bus|trolleybus)$")):
    routes = await parse_routes(type)
    if not routes:
        # Если не удалось спарсить, отдаем демо-данные, чтобы приложение не падало
        return {"demo": True, "routes": [
            {"type": type, "number": "1", "name": "Вокзал — Любенский", "url": ""},
            {"type": type, "number": "2", "name": "Южный — Химзавод", "url": ""}
        ]}
    return {"demo": False, "routes": routes}

@app.get("/api/stops/{kind}/{number}")
async def api_stops(kind: str, number: str, direction: int = 0):
    stops = await parse_stops(kind, number, direction)
    if not stops:
        return {"demo": True, "stops": [{"id": 0, "name": "Демо-остановка 1"}, {"id": 1, "name": "Демо-остановка 2"}]}
    return {"demo": False, "stops": stops}

@app.get("/api/schedule/{kind}/{number}/{stop_id}")
async def api_schedule(kind: str, number: str, stop_id: int):
    section, full = await route_lines(kind, number)
    stops = find_stop_lines(section) or find_stop_lines(full)
    if not stops or stop_id >= len(stops):
        return {"demo": True, "times": ["08:00", "09:30", "12:00"]}
    
    chosen = split_directions(stops)[0] # Берем первое направление для простоты
    if stop_id < len(chosen):
        pos = chosen[stop_id]["pos"]
        times = parse_times(section if find_stop_lines(section) else full, pos=pos)
        if times:
            return {"demo": False, "times": times}
    return {"demo": True, "times": ["08:00", "09:30", "12:00"]}

# Статика для мини-приложения
STATIC = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
