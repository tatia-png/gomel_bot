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
SECTIONS = {"bus": "/avtobus/", "trolleybus": "/trollejbus/", "marshrutka": "/marshrutka/"}
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


def parse_header_table(table) -> dict:
    """Таблица «Час | колонка дней | …»: колонки определяются по заголовку, а не по номеру."""
    store, cols = {}, {}
    for tr in table.find_all("tr"):
        cells = [clean(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        if not cols and not cells[0].isdigit():
            found = {i: (c, label_days(c)) for i, c in enumerate(cells) if i > 0 and label_days(c)}
            if found:
                cols = found
                continue
        if cells[0].isdigit():
            for i, (label, days) in (cols or {1: ("Будни", set(WORKDAYS)), 2: ("Выходные", set(WEEKEND))}).items():
                if i < len(cells):
                    _acc(store, label, days, int(cells[0]), cells[i])
    return by_type(store)


def route_url(kind: str, number: str) -> str:
    return f"{BASE}{SECTIONS[kind]}{number}"


def day_type_now() -> str:
    """'weekend' в сб/вс по Минску (UTC+3), иначе 'week'."""
    return "weekend" if minsk_now().weekday() >= 5 else "week"


# «Троллейбус №2: Вокзал - пос. Новая Жизнь» -> «Вокзал - пос. Новая Жизнь»
HEAD_RE = re.compile(r"^(?:Автобус|Троллейбус|Маршрутка)\s*№\s*\S+?\s*:\s*(.+)$", re.I)


def title_to_name(text: str) -> str:
    """Отрезает всё до двоеточия (включая слово «Автобус/Троллейбус» и номер)."""
    text = clean(text)
    m = HEAD_RE.match(text)
    return m.group(1).strip() if m else (text.split(":", 1)[1].strip() if ":" in text else text)


# ---------- Подписи дней: «Буд.», «Вых.», «Пн,Вт,Ср», «Чт,Пт», «Пн-Пт» ----------
WEEKDAYS = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
WORKDAYS, WEEKEND = {0, 1, 2, 3, 4}, {5, 6}


def label_days(label: str) -> set[int] | None:
    """Подпись колонки/строки -> набор дней недели (0=Пн), либо None, если это не подпись дней."""
    l = clean(label).lower().strip(" .:")
    if not l or len(l) > 30:
        return None
    if l.startswith("буд"):
        return set(WORKDAYS)
    if l.startswith("вых"):
        return set(WEEKEND)
    if l.startswith(("ежедн", "все дни")):
        return set(range(7))
    days: set[int] = set()
    for part in re.split(r"\s*,\s*|\s+", l):
        if not part:
            continue
        m = re.fullmatch(r"([а-я]{2})(?:[-–—]([а-я]{2}))?", part)
        if not m or m.group(1) not in WEEKDAYS or (m.group(2) and m.group(2) not in WEEKDAYS):
            return None
        a = WEEKDAYS[m.group(1)]
        if m.group(2):
            b = WEEKDAYS[m.group(2)]
            days |= set(range(a, b + 1)) if a <= b else set(range(a, 7)) | set(range(0, b + 1))
        else:
            days.add(a)
    return days or None


def _acc(store: dict, label: str, days: set[int], hour: int | None, text: str):
    g = store.setdefault(label, {"label": label, "days": days, "times": set()})
    if hour is None:
        return
    for m in re.findall(r"\b\d{2}\b", text):
        if int(m) < 60:
            g["times"].add(f"{hour % 24:02d}:{m}")


def by_type(store: dict) -> dict:
    """Группы расписания -> {"week": [{label, days, times}], "weekend": [...]}. Пустые группы отбрасываются."""
    out = {"week": [], "weekend": []}
    for g in store.values():
        if not g["times"]:
            continue
        item = {"label": g["label"], "days": sorted(g["days"]), "times": sorted(g["times"])}
        if g["days"] & WORKDAYS:
            out["week"].append(item)
        if g["days"] & WEEKEND:
            out["weekend"].append(item)
    return out


def pick_group(groups: list[dict], weekday: int, group: int | None = None) -> int:
    """Какую группу показывать: заданную, иначе ту, куда попадает сегодняшний день, иначе первую."""
    if group is not None and 0 <= group < len(groups):
        return group
    return next((i for i, g in enumerate(groups) if weekday in g["days"]), 0)


def minsk_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=3)


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
        # таблица «Час | Будни | Выходные» (у маршруток: «Пн,Вт,Ср | Чт,Пт | Вых.») с конечной — после списка остановок
        table = ul.find_next("table")
        times = parse_header_table(table) if table else {"week": [], "weekend": []}
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
    """Страница остановки -> {"week": [{label, days, times}], "weekend": [...], "partial": bool}.
    None — только если страницу не удалось скачать. Если таблица не распознана, берём всё, что видно."""
    html = await fetch(url)
    if not html:
        return None
    soup = soup_of(html)

    # 1) Основной путь: строки таблицы «[час] | подпись дня | минуты». Час может быть пустым/объединённым (rowspan).
    store, hour = {}, None
    for tr in soup.find_all("tr"):
        cells = [clean(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
        li = next((i for i, c in enumerate(cells) if label_days(c)), None)
        if li is None:
            continue
        for c in cells[:li]:
            if c.isdigit():
                hour = int(c)
        if hour is not None:
            _acc(store, cells[li], label_days(cells[li]), hour, " ".join(cells[li + 1:]))
    res = by_type(store)
    if res["week"] or res["weekend"]:
        return {**res, "partial": False}

    # 2) Запасной путь: текст построчно (час, подпись, минуты) — если вёрстка перестала быть таблицей
    store, hour, label, consumed = {}, None, None, False
    for line in (clean(l) for l in soup.get_text("\n").splitlines()):
        if not line:
            continue
        days = label_days(line)
        if days:
            label, consumed = line, False
            store.setdefault(label, {"label": label, "days": days, "times": set()})
        elif re.fullmatch(r"(\d{2}\s*)+", line) and label and not consumed:
            _acc(store, label, store[label]["days"], hour, line)
            consumed = True
        elif line.isdigit() and int(line) <= 24:
            hour, label, consumed = int(line), None, False
    res = by_type(store)
    if res["week"] or res["weekend"]:
        return {**res, "partial": True}

    # 3) Последний шанс: любые времена вида 06:36 на странице
    found = sorted({f"{int(h):02d}:{m}" for h, m in re.findall(r"\b(\d{1,2}):(\d{2})\b", soup.get_text(" "))
                    if int(h) < 24 and int(m) < 60})
    if found:
        g = [{"label": "Время на странице", "days": list(range(7)), "times": found}]
        return {"week": g, "weekend": g, "partial": True}
    return {"week": [], "weekend": [], "partial": False}


async def parse_stops_and_times(kind: str, number: str, day_type: str | None = None, direction: int = 0) -> list[dict]:
    """[{"id", "name", "url", "time"}]. На странице маршрута время есть только у конечной (в таблице),
    поэтому "time" у первой остановки — первый рейс дня, у остальных None;
    полное расписание остановки отдаёт parse_stop_times (страница остановки)."""
    day_type = day_type if day_type in ("week", "weekend") else day_type_now()
    page = await parse_route_page(kind, number)
    if not page:
        return []
    d = page["directions"][direction % len(page["directions"])]
    groups = d["times"].get(day_type) or []
    first = groups[pick_group(groups, minsk_now().weekday())]["times"] if groups else []
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
async def api_schedule(type: str, number: str, stop_id: int, direction: int = 0,
                       day: str | None = None, group: int | None = None):
    """day=week|weekend (по умолчанию — сегодня по Минску). group — номер подгруппы дней, если их несколько
    (у маршруток: «Пн,Вт,Ср» / «Чт,Пт»)."""
    if type not in SECTIONS:
        raise HTTPException(404, "Неизвестный тип")
    today, wd = day_type_now(), minsk_now().weekday()
    day = day if day in ("week", "weekend") else today
    stops = await parse_stops_and_times(type, number, day, direction)
    stop = next((s for s in stops if s["id"] == stop_id), None)
    if not stop:
        raise HTTPException(404, "Остановка не найдена")
    data = await parse_stop_times(stop["url"])
    if data is None:
        raise HTTPException(502, "Не удалось загрузить страницу остановки")
    groups = data[day]
    idx = pick_group(groups, wd, group) if groups else 0
    counts = {k: len(v[pick_group(v, wd)]["times"]) if v else 0 for k, v in ((k, data[k]) for k in ("week", "weekend"))}
    return {"demo": False, "partial": data["partial"], "day": day, "today": today, "stop": stop["name"],
            "times": groups[idx]["times"] if groups else [],
            "group": idx, "groups": [g["label"] for g in groups] if len(groups) > 1 else [],
            "counts": counts}


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
