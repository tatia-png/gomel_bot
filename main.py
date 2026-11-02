"""Транспорт Гомеля — Telegram Mini App (FastAPI + парсер gomel.btrans.by)."""
import asyncio, os, re, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = "https://gomel.btrans.by"
SECTIONS = {"bus": "/avtobus/", "trolleybus": "/trollejbus/"}
CACHE_TTL = 1800

app = FastAPI(title="Gomel Transport")
_cache: dict = {}
_session: aiohttp.ClientSession | None = None
ROUTE_NAMES: dict[tuple[str, str], str] = {}  # (тип, номер) -> «Вокзал - пос. Новая Жизнь»


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
    html = ""
    for attempt in range(3):  # сайт иногда отвечает ошибкой при пачке запросов — повторяем
        try:
            async with _session.get(url) as r:
                r.raise_for_status()
                html = await r.text()
            break
        except Exception as e:
            print(f"fetch failed for {url} (try {attempt + 1}): {e!r}")
            await asyncio.sleep(0.6 * (attempt + 1))
    if not html:
        return ""
    _cache[url] = (time.time(), html)
    return html


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def clean(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def route_url(kind: str, number: str) -> str:
    return f"{BASE}{SECTIONS[kind]}{number}"


def day_type_now() -> str:
    """'weekend' в сб/вс по Минску (UTC+3), иначе 'week'."""
    return "weekend" if (datetime.now(timezone.utc) + timedelta(hours=3)).weekday() >= 5 else "week"


# «Троллейбус №2: Вокзал - пос. Новая Жизнь» -> «Вокзал - пос. Новая Жизнь»
HEAD_RE = re.compile(r"^(?:Автобус|Троллейбус|Маршрутка)\s*№\s*\S+?\s*:\s*(.+)$", re.I)


def title_to_name(text: str) -> str:
    """Отрезает всё до двоеточия (включая слово «Автобус/Троллейбус» и номер)."""
    text = clean(text)
    m = HEAD_RE.match(text)
    return m.group(1).strip() if m else (text.split(":", 1)[1].strip() if ":" in text else text)


# ---------- ПАРСЕРЫ ----------
async def parse_route_page(kind: str, number: str) -> dict | None:
    """Страница маршрута -> {"directions": [{"title", "stops": [{"name","url"}], "times": {"week":[..],"weekend":[..]}}]}.
    Начинаем собирать данные только после заголовков «Троллейбус №N: A - B»."""
    url = route_url(kind, number)
    html = await fetch(url)
    if not html:
        return None
    soup = soup_of(html)
    prefix = f"{SECTIONS[kind]}{number}/".lower()
    directions = []
    for h in soup.find_all(["h2", "h3"]):
        text = clean(h.get_text())
        if not HEAD_RE.match(text):
            continue
        ul = h.find_next(["ul", "ol"])
        stops = []
        for a in (ul.find_all("a", href=True) if ul else []):
            path = unquote(urlparse(urljoin(url, a["href"])).path).lower()
            if path.startswith(prefix) and len(path) > len(prefix):  # ссылка на остановку, а не на маршрут
                stops.append({"name": clean(a.get_text()), "url": urljoin(url, a["href"])})
        if len(stops) < 2:
            continue
        # таблица «Час | Будни | Выходные» с конечной — сразу после списка остановок
        times = {"week": [], "weekend": []}
        table = ul.find_next("table")
        for tr in (table.find_all("tr") if table else []):
            cells = [clean(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
            if len(cells) >= 3 and cells[0].isdigit():
                for key, cell in (("week", cells[1]), ("weekend", cells[2])):
                    times[key] += [f"{int(cells[0]):02d}:{m}" for m in re.findall(r"\b\d{2}\b", cell)]
        directions.append({"title": title_to_name(text), "stops": stops, "times": times})
    if directions:
        ROUTE_NAMES[(kind, number)] = directions[0]["title"]
    return {"directions": directions} if directions else None


async def parse_routes(kind: str) -> list[dict]:
    html = await fetch(BASE)
    if not html:
        return []
    soup = soup_of(html)
    found: dict[str, str] = {}
    pattern = re.compile(rf"^{re.escape(SECTIONS[kind])}([^/?#]+)/?$")
    for a in soup.find_all("a", href=True):
        m = pattern.match(unquote(urlparse(a["href"]).path))
        if not m:
            continue
        num = m.group(1)
        # на главной текст ссылки — только номер; имя берём из заголовка страницы маршрута
        name = ROUTE_NAMES.get((kind, num), "")
        if not name:
            t = clean(a.get_text())
            name = title_to_name(t) if ":" in t else ""
        found[num] = name

    def keyf(n):
        m = re.match(r"(\d+)(.*)", n)
        return (int(m.group(1)), m.group(2)) if m else (9999, n)

    return [{"type": kind, "number": n, "name": found[n]} for n in sorted(found, key=keyf)]


async def parse_stop_times(url: str) -> dict | None:
    """Страница остановки: таблица «Час | Буд./Вых. | минуты».
    Возвращает {"week": ["06:36", ...], "weekend": [...]} или None, если страницу не удалось загрузить/разобрать.
    Пустой список для дня — это нормально (например, конечная или нет рейсов в выходные)."""
    html = await fetch(url)
    if not html:
        return None
    res = {"week": set(), "weekend": set()}
    hour, rows = None, 0
    for tr in soup_of(html).find_all("tr"):
        cells = [clean(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
        li = next((i for i, c in enumerate(cells) if c[:3].lower() in ("буд", "вых")), None)
        if li is None:
            continue
        # час — число в ячейке до метки; если ячейка пустая/объединена (rowspan) — берём час предыдущей строки
        for c in cells[:li]:
            if c.isdigit():
                hour = int(c)
        if hour is None:
            continue
        rows += 1
        key = "week" if cells[li][:3].lower() == "буд" else "weekend"
        for m in re.findall(r"\b\d{2}\b", " ".join(cells[li + 1:])):
            if int(m) < 60:
                res[key].add(f"{hour % 24:02d}:{m}")
    if not rows:
        return None
    return {k: sorted(v) for k, v in res.items()}


async def parse_stops_and_times(kind: str, number: str, day_type: str | None = None, direction: int = 0) -> list[dict]:
    """[{"id", "name", "url", "time"}]. На странице маршрута время есть только у конечной (в таблице),
    поэтому "time" у первой остановки — первый рейс дня, у остальных None;
    полное расписание остановки отдаёт parse_stop_times (страница остановки)."""
    day_type = day_type if day_type in ("week", "weekend") else day_type_now()
    page = await parse_route_page(kind, number)
    if not page:
        return []
    d = page["directions"][direction % len(page["directions"])]
    first = d["times"].get(day_type) or []
    return [{"id": i, "name": s["name"], "url": s["url"], "time": (first[0] if i == 0 and first else None)}
            for i, s in enumerate(d["stops"])]


async def warm_names():
    """Фоном подтягиваем названия маршрутов (на главной их нет, только номера)."""
    sem = asyncio.Semaphore(3)

    async def one(kind, num):
        async with sem:
            await parse_route_page(kind, num)

    for kind in SECTIONS:
        await asyncio.gather(*(one(kind, r["number"]) for r in await parse_routes(kind)))


@app.on_event("startup")
async def startup():
    asyncio.create_task(warm_names())


# ---------- API ----------
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
    stops = await parse_stops_and_times(type, number, None, direction)
    page = await parse_route_page(type, number)
    demo = not stops
    if demo:
        stops = [{"id": 0, "name": "Демо-остановка"}]
    n = len(page["directions"]) if page else 1
    return {"demo": demo, "direction": direction % n, "title": ROUTE_NAMES.get((type, number), ""),
            "stops": [{"id": s["id"], "name": s["name"], "time": s.get("time")} for s in stops]}


@app.get("/api/schedule/{type}/{number}/{stop_id}")
async def api_schedule(type: str, number: str, stop_id: int, direction: int = 0, day: str | None = None):
    """day=week|weekend (по умолчанию — сегодняшний день по Минску)."""
    if type not in SECTIONS:
        raise HTTPException(404, "Неизвестный тип")
    today = day_type_now()
    day = day if day in ("week", "weekend") else today
    stops = await parse_stops_and_times(type, number, day, direction)
    stop = next((s for s in stops if s["id"] == stop_id), None)
    if not stop:
        raise HTTPException(404, "Остановка не найдена")
    data = await parse_stop_times(stop["url"])
    if data is None:
        raise HTTPException(502, "Не удалось загрузить страницу остановки")
    # заглушек нет: если в этот день рейсов нет, вернём пустой список
    return {"demo": False, "day": day, "today": today, "stop": stop["name"],
            "times": data[day], "counts": {k: len(v) for k, v in data.items()}}


@app.get("/api/search")
async def api_search(from_: str = Query(..., alias="from"), to: str = Query(...)):
    a, b = from_.lower().strip(), to.lower().strip()
    routes = [r for k in SECTIONS for r in await parse_routes(k)]
    sem = asyncio.Semaphore(6)

    async def check(r):
        async with sem:
            page = await parse_route_page(r["type"], r["number"])
            for d, direction in enumerate((page or {}).get("directions", [])):
                names = [s["name"].lower() for s in direction["stops"]]
                ia = next((i for i, n in enumerate(names) if a in n), None)
                ib = next((i for i, n in enumerate(names) if b in n), None)
                if ia is not None and ib is not None and ia < ib:
                    return {"type": r["type"], "number": r["number"], "name": direction["title"],
                            "direction": d, "stops_count": ib - ia}

    found = [x for x in await asyncio.gather(*(check(r) for r in routes)) if x]
    return {"results": sorted(found, key=lambda x: x["stops_count"])}


STATIC = Path(__file__).parent / "static"


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/", StaticFiles(directory=STATIC), name="static")
