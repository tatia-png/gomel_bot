const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready(); tg.expand();
  if (tg.colorScheme === "dark") document.body.classList.add("dark");
  // Фоны переопределены на пастельные в CSS; из темы Telegram берём только цвет шапки
  tg.setHeaderColor?.(tg.colorScheme === "dark" ? "#2A2840" : "#F5F5FA");
  tg.setBackgroundColor?.(tg.colorScheme === "dark" ? "#2A2840" : "#F5F5FA");
}
const $app = document.getElementById("app");
const $title = document.getElementById("title");
const $sub = document.getElementById("subtitle");
const TYPE_NAMES = { bus: "🚌 Автобусы", trolleybus: "🚎 Троллейбусы" };
const stack = []; // история экранов

const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const loading = () => ($app.innerHTML = '<div class="loader">⏳ Загрузка…</div>');
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}
const demoNote = d => (d ? '<div class="notice">⚠️ Показаны демо-данные: сайт недоступен или его вёрстка не распознана.</div>' : "");
const head = (t, s = "") => { $title.textContent = t; $sub.textContent = s; };

function go(fn) { stack.push(fn); fn(); updateBack(); }
function back() { stack.pop(); (stack[stack.length - 1] || home)(); updateBack(); }
function updateBack() {
  if (!tg) return;
  stack.length ? tg.BackButton.show() : tg.BackButton.hide();
}
tg?.BackButton.onClick(back);

function home() {
  head("🚏 Транспорт Гомеля", "Выберите, что нужно");
  $app.innerHTML = `<div class="stack">
    <button class="btn" data-t="bus">🚌 Автобусы</button>
    <button class="btn" data-t="trolleybus">🚎 Троллейбусы</button>
    <button class="btn pink" id="search">🗺️ Откуда-Куда</button></div>`;
  $app.querySelectorAll("[data-t]").forEach(b => b.onclick = () => go(() => routesView(b.dataset.t)));
  document.getElementById("search").onclick = () => go(searchView);
}

async function routesView(type) {
  head(TYPE_NAMES[type], "Выберите маршрут"); loading();
  try {
    const d = await api(`/api/routes?type=${type}`);
    $app.innerHTML = demoNote(d.demo) + '<div class="stack">' + d.routes.map(r =>
      `<div class="card" data-n="${esc(r.number)}"><span class="badge">${esc(r.number)}</span><span>${esc(r.name || "Маршрут " + r.number)}</span></div>`).join("") + "</div>";
    $app.querySelectorAll(".card").forEach(c => c.onclick = () => go(() => stopsView(type, c.dataset.n, 0)));
  } catch { $app.innerHTML = '<div class="notice">Не удалось загрузить маршруты</div>'; }
}

async function stopsView(type, number, direction) {
  head(`${TYPE_NAMES[type].split(" ")[0]} № ${number}`, direction ? "Обратное направление" : "Прямое направление"); loading();
  try {
    const d = await api(`/api/stops/${type}/${encodeURIComponent(number)}?direction=${direction}`);
    $app.innerHTML = demoNote(d.demo) + '<button class="btn soft" id="flip">🔄 Сменить направление</button><div class="stack" style="margin-top:12px">' +
      d.stops.map(s => `<div class="card" data-id="${s.id}"><span class="badge">${s.id + 1}</span><span>${esc(s.name)}</span></div>`).join("") + "</div>";
    document.getElementById("flip").onclick = () => { stack.pop(); go(() => stopsView(type, number, 1 - d.direction)); };
    $app.querySelectorAll(".card").forEach(c => c.onclick = () => go(() => scheduleView(type, number, +c.dataset.id, d.direction)));
  } catch { $app.innerHTML = '<div class="notice">Не удалось загрузить остановки</div>'; }
}

function minskNowMinutes() {
  const p = new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Minsk", hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(new Date());
  return +p.find(x => x.type === "hour").value % 24 * 60 + +p.find(x => x.type === "minute").value;
}

async function scheduleView(type, number, stopId, direction, day = null) {
  head("🕐 Расписание", ""); loading();
  try {
    const q = day ? `&day=${day}` : "";
    const d = await api(`/api/schedule/${type}/${encodeURIComponent(number)}/${stopId}?direction=${direction}${q}`);
    head("🕐 " + (d.stop || "Расписание"), `№ ${number}`);
    const now = minskNowMinutes();
    const isToday = d.day === d.today;               // прошедшие рейсы красим только для сегодняшнего дня
    const mins = t => +t.slice(0, 2) * 60 + +t.slice(3);
    const nextIdx = isToday ? d.times.findIndex(t => mins(t) >= now) : -1;
    const tab = (k, label) => `<button class="tab ${d.day === k ? "active" : ""}" data-day="${k}">${label} <small>${d.counts?.[k] ?? ""}</small></button>`;
    $app.innerHTML = `<div class="tabs">${tab("week", "Будни")}${tab("weekend", "Выходные")}</div>` +
      (d.times.length
        ? `<div class="grid">${d.times.map((t, i) =>
            `<div class="time ${isToday && mins(t) < now ? "past" : "future"} ${i === nextIdx ? "next" : ""}">${t}</div>`).join("")}</div>`
        : '<div class="notice">В этот день рейсов с этой остановки нет (возможно, это конечная).</div>');
    $app.querySelectorAll(".tab").forEach(b => b.onclick = () => {
      if (b.dataset.day !== d.day) scheduleView(type, number, stopId, direction, b.dataset.day);
    });
  } catch { $app.innerHTML = '<div class="notice">Не удалось загрузить расписание</div>'; }
}

function searchView() {
  head("🗺️ Откуда-Куда", "Введите названия остановок");
  $app.innerHTML = `<div class="stack">
    <input id="from" placeholder="Откуда (например, Вокзал)">
    <input id="to" placeholder="Куда (например, Площадь Ленина)">
    <button class="btn" id="go">🔍 Найти маршруты</button></div><div id="res" style="margin-top:14px"></div>`;
  document.getElementById("go").onclick = async () => {
    const f = document.getElementById("from").value.trim(), t = document.getElementById("to").value.trim();
    const res = document.getElementById("res");
    if (!f || !t) return;
    res.innerHTML = '<div class="loader">⏳ Ищем…</div>';
    try {
      const d = await api(`/api/search?from=${encodeURIComponent(f)}&to=${encodeURIComponent(t)}`);
      res.innerHTML = d.results.length ? '<div class="stack">' + d.results.map(r =>
        `<div class="card" data-t="${r.type}" data-n="${esc(r.number)}" data-d="${r.direction}"><span class="badge">${esc(r.number)}</span>
         <span>${r.type === "bus" ? "🚌" : "🚎"} ${esc(r.name)}<br><small class="muted">остановок: ${r.stops_count}</small></span></div>`).join("") + "</div>"
        : '<div class="notice">Прямых маршрутов не найдено</div>';
      res.querySelectorAll(".card").forEach(c => c.onclick = () => go(() => stopsView(c.dataset.t, c.dataset.n, +c.dataset.d)));
    } catch { res.innerHTML = '<div class="notice">Ошибка поиска</div>'; }
  };
}

home();
