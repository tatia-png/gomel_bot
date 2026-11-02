"""Транспорт Гомеля — Telegram Mini App (FastAPI + парсер gomeltrans.net)."""
import asyncio, os, re, time
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = "https://gomeltrans.net"
# ⚠️ Пути разделов сайта. Если структура сайта отличается — поправьте здесь.
SECTIONS = {"bus": "/routes/bus/", "trolleybus": "/routes/trolleybus/"}
CACHE_TTL = 600  # секунд

app = FastAPI(title="Gomel Transport")
_cache: dict = {}
_session: aiohttp.ClientSession | None = None


SCRAPEOPS_KEY = os.getenv("SCRAPEOPS_API_KEY", "").strip()
SCRAPEOPS_PROXY = f"http://scrapeops:{SCRAPEOPS_KEY}@residential-proxy.scrapeops.io:8181"


async def fetch(url: str) -> str:
    """Скачивает страницу. Если задан SCRAPEOPS_API_KEY — запрос идёт через прокси ScrapeOps (Proxy Port)."""
    global _session
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                     "Accept-Language": "ru-RU,ru;q=0.9", "Accept": "text/html,application/xhtml+xml"},
            timeout=aiohttp.ClientTimeout(total=60 if SCRAPEOPS_KEY else 15),
        )
    try:
        if SCRAPEOPS_KEY:
            # Эквивалент http://scrapeops:KEY@residential-proxy.scrapeops.io:8181, ssl=False — без проверки сертификатов
          async with _session.get(
                url, proxy=SCRAPEOPS_PROXY, ssl=False,
            ) as r:
                r.raise_for_status()
                html = await r.text()
        else:
            async with _session.get(url) as r:
                r.raise_for_status()
                html = await r.text()
    except Exception as e:
        print(f"fetch failed for {url}: {e!r}")  # видно в логах Render
        return ""
    _cache[url] = (time.time(), html)
    return html


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def clean(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


# ---------- ПАРСЕРЫ: работают с ЧИСТЫМ ТЕКСТОМ страницы (get_text) ----------
# Формат ссылок: https://gomeltrans.net/routes/bus/2/  и  /routes/trolleybus/...
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
    """Скачивает страницу и возвращает непустые строки чистого текста."""
    html = await fetch(url)
    if not html:
        return []
    text = soup_of(html).get_text("\n")
    return [clean(l) for l in text.splitlines() if clean(l)]


def route_section(lines: list[str]) -> list[str]:
    """Текст после заголовка «Расписание…» и до «Режим работы». Нет маркеров — вся страница."""
    start = next((i for i, l in enumerate(lines) if re.match(r"^расписание", l, re.I)), -1)
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^режим работы", lines[i], re.I)), len(lines))
    return lines[start + 1:end]


def parse_times(lines: list[str], pos: int | None = None, full: list[str] | None = None) -> list[str]:
    """Время HH:MM. Если pos задан — только то, что идёт после строки остановки до следующей остановки."""
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
    return grab(lines) or grab(full or [])  # запасной вариант: все времена блока/страницы


def find_stop_lines(lines: list[str]) -> list[dict]:
    """Строки-остановки: с ключевыми словами (ул., просп., Микрорайон…) и их соседи-названия в списке."""
    def cand(l):
        return (3 <= len(l) <= 70 and re.search(r"[А-Яа-яЁё]", l) and not TIME_RE.search(l)
                and not NOISE.match(l))
    ok = [bool(cand(l)) for l in lines]
    kw = [ok[i] and bool(STOP_KW.search(lines[i])) for i in range(len(lines))]
    res = []
    def neighbor_kw(i, step):
        j = i + step
        while 0 <= j < len(lines) and TIME_RE.search(lines[j]):  # пропускаем строки со временем
            j += step
        return 0 <= j < len(lines) and kw[j]
    for i, l in enumerate(lines):
        if kw[i] or (ok[i] and (neighbor_kw(i, -1) or neighbor_kw(i, 1))):
            res.append({"name": l, "pos": i})
    return res


def split_directions(stops: list[dict]) -> list[list[dict]]:
    """Делит общий список на 2 направления: конечная повторяется подряд (… C | C …)."""
    for j in range(2, len(stops) - 1):
        if stops[j]["name"].lower() == stops[j - 1]["name"].lower():
            return [stops[:j], stops[j:]]
    return [stops, list(reversed(stops))]  # одно направление — обратное строим сами


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
    # 1) номера из ссылок вида /routes/bus/2/ (по шаблону URL, а не по вёрстке)
    pat = re.compile(re.escape(SECTIONS[kind]) + r"([0-9]+[а-яa-zэ]*)/?$", re.I)
    for a in soup.find_all("a", href=True):
        m = pat.search(a["href"])
        if m:
            found.setdefault(m.group(1), clean(a.get_text()))
    # 2) запасной путь — по чистому тексту: «№ 17э Медгородок — …»
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


# ---------- Демо-данные (если сайт недоступен/вёрстка не распознана) ----------
DEMO_ROUTES = [
    {"type": "bus", "number": "1", "name": "Демо-маршрут", "url": ""},
    {"type": "bus", "number": "17", "name": "Медгородок — Клёнковский", "url": ""},
    {"type": "trolleybus", "number": "2", "name": "Демо-маршрут", "url": ""},
]
DEMO_STOPS = [["Вокзал", "Площадь Ленина", "Универмаг «Гомель»", "Клёнковский"],
              ["Клёнковский", "Универмаг «Гомель»", "Площадь Ленина", "Вокзал"]]


def demo_times() -> list[str]:
    return [f"{h:02d}:{m:02d}" for h in range(5, 24) for m in (5, 25, 45)]


# ---------- API ----------
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
        names = DEMO_STOPS[direction % 2]
        stops = [{"id": i, "name": n, "url": ""} for i, n in enumerate(names)]
    stops = [{"id": s["id"], "name": s["name"]} for s in stops]
    return {"demo": demo, "direction": direction % 2, "stops": stops}


@app.get("/api/schedule/{type}/{number}/{stop_id}")
async def api_schedule(type: str, number: str, stop_id: int, direction: int = 0):
    if type not in SECTIONS:
        raise HTTPException(404, "Неизвестный тип")
    stops = await parse_stops(type, number, direction)
    stop = next((s for s in stops if s["id"] == stop_id), None)
    times = []
    if stop:
        section, full = await route_lines(type, number)
        times = parse_times(section if stop["src"] == "section" else full, stop["pos"], full)
    demo = not times
    if demo:
        times = demo_times()
    return {"demo": demo, "stop": stop["name"] if stop else "", "times": times}


@app.get("/api/search")
async def api_search(from_: str = Query(..., alias="from"), to: str = Query(...)):
    a, b = from_.lower().strip(), to.lower().strip()
    routes = [r for k in SECTIONS for r in await parse_routes(k)]
    sem = asyncio.Semaphore(6)

    async def check(r):
        async with sem:
            for d in (0, 1):
                names = [s["name"].lower() for s in await parse_stops(r["type"], r["number"], d)]
                ia = next((i for i, n in enumerate(names) if a in n), None)
                ib = next((i for i, n in enumerate(names) if b in n), None)
                if ia is not None and ib is not None and ia < ib:
                    return {"type": r["type"], "number": r["number"], "name": r["name"],
                            "direction": d, "stops_count": ib - ia}
        return None

    found = [x for x in await asyncio.gather(*(check(r) for r in routes)) if x]
    return {"results": sorted(found, key=lambda x: x["stops_count"])}


# ---------- Фронтенд ----------
STATIC = Path(__file__).parent / "static"


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/", StaticFiles(directory=STATIC), name="static")
