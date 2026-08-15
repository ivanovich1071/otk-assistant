"use strict";

const $ = (id) => document.getElementById(id);
const STATUS = { queued: "в очереди", running: "выполняется", done: "готово", failed: "ошибка" };

let chosen = null;      // выбранный файл
let current = null;     // id открытого задания
let job = null;         // карточка открытого задания
let markup = null;      // разметка, загруженная в редактор
let dirty = false;      // есть несохранённые правки
let timer = null;       // опрос состояния
let tick = null;        // живой счётчик времени
let zoom = 1;

async function api(path, options) {
  const answer = await fetch(path, options);
  if (!answer.ok) {
    const body = await answer.json().catch(() => ({}));
    throw new Error(body.detail || answer.statusText);
  }
  return answer.status === 204 ? null : answer.json();
}

const text = (node, value) => { node.textContent = value; };

/** Секунды в «1 мин 12 с» — цифры в минутах читаются быстрее, чем в сотнях секунд. */
function human(seconds) {
  if (!seconds) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)} с`;
  const min = Math.floor(seconds / 60);
  return `${min} мин ${Math.round(seconds - min * 60)} с`;
}

function row(cells, className) {
  const tr = document.createElement("tr");
  if (className) tr.className = className;
  for (const cell of cells) {
    const td = document.createElement("td");
    if (cell instanceof Node) td.appendChild(cell); else td.textContent = cell ?? "";
    tr.appendChild(td);
  }
  return tr;
}

function badge(status) {
  const span = document.createElement("span");
  span.className = "s-" + status;
  span.textContent = STATUS[status] || status;
  return span;
}

// ── список заданий ────────────────────────────────────────────────────────────

async function refresh() {
  const list = await api("/api/jobs");
  const body = $("joblist").querySelector("tbody");
  body.innerHTML = "";
  for (const item of list) {
    const tr = row([item.title || item.source,
                    item.mode === "tz" ? "ТЗ" : "Карта обмера",
                    badge(item.status), human(item.elapsed)],
                   item.id === current ? "sel" : "");
    tr.onclick = () => open(item.id);
    body.appendChild(tr);
  }
  $("empty").hidden = list.length > 0;
  text($("stat-jobs"), "заданий: " + list.length);

  if (current && list.some((j) => j.id === current)) await open(current, true);
  else if (current) { current = null; $("card").hidden = true; $("detail").hidden = false; }

  const busy = list.some((j) => j.status === "running" || j.status === "queued");
  if (busy && !timer) timer = setInterval(refresh, 1000);
  if (!busy && timer) { clearInterval(timer); timer = null; }
}

// ── карточка задания ──────────────────────────────────────────────────────────

async function open(id, keep) {
  const fresh = await api("/api/jobs/" + id);
  const changed = current !== id;
  current = id;
  job = fresh;

  $("detail").hidden = true;
  $("card").hidden = false;
  text($("title"), job.title || job.source);

  const props = $("props");
  props.innerHTML = "";
  const pairs = [
    ["Файл", job.source],
    ["Режим", job.mode === "tz" ? "Требования по изготовлению" : "Карта обмера"],
    ["Состояние", STATUS[job.status] || job.status],
    ["Время обработки", human(job.elapsed)],
    ["Создано", new Date(job.created * 1000).toLocaleString("ru-RU")],
  ];
  if (job.error) pairs.push(["Ошибка", job.error]);
  for (const [name, value] of pairs) {
    const a = document.createElement("div"); a.textContent = name;
    const b = document.createElement("div"); b.textContent = value;
    if (name === "Ошибка") b.className = "s-failed";
    if (name === "Время обработки") b.id = "elapsed";
    props.append(a, b);
  }

  const stages = $("stages").querySelector("tbody");
  stages.innerHTML = "";
  for (const stage of job.stages) {
    stages.appendChild(row([stage.name, badge(stage.status), stage.note,
                            stage.status === "done" ? human(stage.seconds) : ""]));
  }
  if (!job.stages.length) stages.appendChild(row(["—", "", "", ""]));

  const warnings = $("warnings");
  warnings.innerHTML = "";
  for (const line of job.warnings) {
    const li = document.createElement("li");
    li.textContent = line;
    warnings.appendChild(li);
  }
  if (!job.warnings.length) warnings.innerHTML = '<li class="none">замечаний нет</li>';

  fillFiles();
  $("restart").disabled = false;
  $("remove").disabled = false;

  liveTimer();
  if (changed) {
    markup = null; dirty = false;
    showTab("job");
    if (!keep) $("right").scrollTop = 0;
  }
}

/** Пока задание выполняется, время в карточке идёт само, а не ждёт опроса. */
function liveTimer() {
  if (tick) { clearInterval(tick); tick = null; }
  text($("stat-time"), job && job.elapsed ? "последняя обработка: " + human(job.elapsed) : "");
  if (!job || job.status !== "running") return;
  const from = Date.now() - job.elapsed * 1000;
  tick = setInterval(() => {
    const node = $("elapsed");
    if (!node) return;
    text(node, human((Date.now() - from) / 1000));
  }, 200);
}

function fillFiles() {
  const body = $("files").querySelector("tbody");
  body.innerHTML = "";
  for (const name of job.files) {
    const actions = document.createElement("span");
    const view = document.createElement("a");
    view.href = `/api/jobs/${job.id}/view/${encodeURIComponent(name)}`;
    view.target = "_blank";
    view.textContent = "открыть";
    const save = document.createElement("a");
    save.href = `/api/jobs/${job.id}/files/${encodeURIComponent(name)}`;
    save.textContent = "скачать";
    actions.append(view, save);
    body.appendChild(row([name, actions]));
  }
  if (!job.files.length) body.appendChild(row(["файлов пока нет", ""]));
}

// ── вкладки ───────────────────────────────────────────────────────────────────

function showTab(name) {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("act", tab.dataset.tab === name);
  }
  for (const pane of ["job", "draw", "table", "files"]) {
    $("pane-" + pane).hidden = pane !== name;
  }
  if (name === "draw") loadDrawing();
  if (name === "table") loadMarkup();
}

for (const tab of document.querySelectorAll(".tab")) {
  tab.onclick = () => showTab(tab.dataset.tab);
}

// ── просмотр чертежа ──────────────────────────────────────────────────────────

function loadDrawing() {
  const picture = job.files.find((n) => /\.(jpg|jpeg|png)$/i.test(n));
  $("draw-none").hidden = Boolean(picture);
  $("viewport").hidden = !picture;
  $("drawbar").hidden = !picture;
  if (!picture) return;
  const image = $("drawing");
  const href = `/api/jobs/${job.id}/view/${encodeURIComponent(picture)}`;
  if (image.dataset.src !== href) {
    image.dataset.src = href;
    image.src = href;
    image.onload = fitWidth;
  } else if (image.naturalWidth) {
    fitWidth();       // ширину окна видно только когда вкладка показана
  }
}

function applyZoom() {
  const image = $("drawing");
  image.style.width = image.naturalWidth * zoom + "px";
  text($("zoom-label"), Math.round(zoom * 100) + " %");
}

function fitWidth() {
  const image = $("drawing");
  if (!image.naturalWidth) return;
  zoom = ($("viewport").clientWidth - 4) / image.naturalWidth;
  applyZoom();
}

$("zoom-in").onclick = () => { zoom = Math.min(zoom * 1.4, 8); applyZoom(); };
$("zoom-out").onclick = () => { zoom = Math.max(zoom / 1.4, 0.02); applyZoom(); };
$("zoom-fit").onclick = fitWidth;
$("zoom-1").onclick = () => { zoom = 1; applyZoom(); };

(function panning() {
  const view = $("viewport");
  let active = false, x = 0, y = 0, left = 0, top = 0;
  view.addEventListener("mousedown", (event) => {
    active = true; x = event.clientX; y = event.clientY;
    left = view.scrollLeft; top = view.scrollTop;
    view.classList.add("drag");
    event.preventDefault();
  });
  addEventListener("mousemove", (event) => {
    if (!active) return;
    view.scrollLeft = left - (event.clientX - x);
    view.scrollTop = top - (event.clientY - y);
  });
  addEventListener("mouseup", () => { active = false; view.classList.remove("drag"); });
})();

// ── таблица карты обмера: просмотр и правка ───────────────────────────────────

async function loadMarkup() {
  if (job.mode !== "karta") {
    $("editor").innerHTML = "";
    $("table-none").hidden = false;
    text($("table-none"), "Правка доступна только для карты обмера. "
                        + "Требования по изготовлению смотрите во вкладке «Файлы».");
    $("tablebar").hidden = true;
    return;
  }
  $("tablebar").hidden = false;
  if (markup) return drawEditor();
  try {
    markup = await api(`/api/jobs/${job.id}/markup`);
  } catch {
    $("editor").innerHTML = "";
    $("table-none").hidden = false;
    return;
  }
  $("table-none").hidden = true;
  drawEditor();
}

function drawEditor() {
  const host = $("editor");
  host.innerHTML = "";
  let count = 0;

  for (const group of markup.groups) {
    const caption = document.createElement("div");
    caption.className = "gname";
    caption.textContent = group.name;
    host.appendChild(caption);

    const table = document.createElement("table");
    table.innerHTML = '<thead><tr><th style="width:64px">№ п/п</th>'
                    + '<th>Значение по чертежу</th><th class="drop"></th></tr></thead>';
    const body = document.createElement("tbody");

    for (const item of group.items) {
      const tr = document.createElement("tr");
      if (item.confidence === "low") tr.className = "low";

      const no = document.createElement("td");
      no.className = "no";
      no.textContent = item.no;

      const value = document.createElement("td");
      const input = document.createElement("input");
      input.value = item.value;
      input.oninput = () => { item.value = input.value; markDirty(); };
      value.appendChild(input);

      const drop = document.createElement("td");
      drop.className = "drop";
      const button = document.createElement("button");
      button.textContent = "×";
      button.title = "убрать позицию из карты";
      button.onclick = () => {
        item.removed = !item.removed;
        tr.classList.toggle("gone", Boolean(item.removed));
        markDirty();
      };
      drop.appendChild(button);

      tr.append(no, value, drop);
      body.appendChild(tr);
      count += 1;
    }
    table.appendChild(body);
    host.appendChild(table);
  }

  if (markup.tech_requirements && markup.tech_requirements.length) {
    const caption = document.createElement("div");
    caption.className = "gname";
    caption.textContent = "Технические требования чертежа";
    host.appendChild(caption);
    const table = document.createElement("table");
    const body = document.createElement("tbody");
    for (const line of markup.tech_requirements) {
      const tr = document.createElement("tr");
      const no = document.createElement("td");
      no.className = "no"; no.style.width = "64px";
      no.textContent = line.no || "";
      const value = document.createElement("td");
      const input = document.createElement("input");
      input.value = line.text;
      input.oninput = () => { line.text = input.value; markDirty(); };
      value.appendChild(input);
      tr.append(no, value);
      body.appendChild(tr);
    }
    table.appendChild(body);
    host.appendChild(table);
  }

  text($("edited"), `позиций: ${count}`);
}

function markDirty() {
  dirty = true;
  $("save").disabled = false;
  $("rebuild").disabled = true;
  text($("edited"), "есть несохранённые правки");
}

/** Удалённые позиции выбрасываются, номера идут заново сквозь все группы. */
function renumber(data) {
  let number = 1;
  for (const group of data.groups) {
    group.items = group.items.filter((i) => !i.removed);
    for (const item of group.items) { item.no = String(number); number += 1; }
  }
  data.groups = data.groups.filter((g) => g.items.length);
  return data;
}

$("save").onclick = async () => {
  const data = renumber(JSON.parse(JSON.stringify(markup)));
  const answer = await api(`/api/jobs/${job.id}/markup`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  markup = data;
  dirty = false;
  $("save").disabled = true;
  $("rebuild").disabled = false;
  drawEditor();
  text($("edited"), `сохранено, позиций: ${answer.positions}`);
};

$("rebuild").onclick = async () => {
  $("rebuild").disabled = true;
  await api(`/api/jobs/${job.id}/rebuild`, { method: "POST" });
  $("drawing").dataset.src = "";
  await refresh();
  showTab("job");
};

// ── панель инструментов ───────────────────────────────────────────────────────

$("pick").onclick = () => $("file").click();
$("file").onchange = () => {
  chosen = $("file").files[0] || null;
  text($("filename"), chosen ? chosen.name : "файл не выбран");
  $("filename").className = chosen ? "" : "dim";
  $("start").disabled = !chosen;
};

$("start").onclick = async () => {
  if (!chosen) return;
  const form = new FormData();
  form.append("mode", $("mode").value);
  form.append("file", chosen);
  $("start").disabled = true;
  try {
    const created = await api("/api/jobs", { method: "POST", body: form });
    current = created.id;
    markup = null;
    await refresh();
  } catch (error) {
    alert("Не удалось запустить: " + error.message);
  } finally {
    $("start").disabled = !chosen;
  }
};

$("restart").onclick = async () => {
  if (!current) return;
  await api(`/api/jobs/${current}/restart`, { method: "POST" });
  markup = null;
  await refresh();
};

$("remove").onclick = async () => {
  if (!current) return;
  await api("/api/jobs/" + current, { method: "DELETE" });
  current = null; job = null; markup = null;
  $("card").hidden = true;
  $("detail").hidden = false;
  $("restart").disabled = true;
  $("remove").disabled = true;
  await refresh();
};

$("clear").onclick = async () => {
  if (!confirm("Убрать все обработанные задания из списка?\n"
             + "Готовые файлы в папке результатов останутся.")) return;
  const answer = await api("/api/jobs", { method: "DELETE" });
  current = null; job = null; markup = null;
  $("card").hidden = true;
  $("detail").hidden = false;
  $("restart").disabled = true;
  $("remove").disabled = true;
  await refresh();
  text($("stat-time"), `убрано заданий: ${answer.removed}`);
};

// ── папка для сохранения ──────────────────────────────────────────────────────

let desktop = false;

async function saveFolder(value) {
  const state = await api("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ output_dir: value }),
  });
  $("folder").value = state.output_dir;
  text($("stat-out"), "результаты: " + state.output_dir);
  return state;
}

$("folder-save").onclick = async () => {
  try {
    await saveFolder($("folder").value);
    text($("folder-hint"), "папка сохранена");
  } catch (error) {
    text($("folder-hint"), "не удалось: " + error.message);
  }
};

$("browse").onclick = async () => {
  try {
    const state = await api("/api/pick-folder", { method: "POST" });
    $("folder").value = state.output_dir;
    text($("stat-out"), "результаты: " + state.output_dir);
    text($("folder-hint"), "папка выбрана");
  } catch (error) {
    text($("folder-hint"), error.message);
  }
};

$("export").onclick = async () => {
  if (!job) return;
  try {
    const answer = await api(`/api/jobs/${job.id}/export`, { method: "POST" });
    text($("folder-hint"), answer.saved.length
      ? `сохранено файлов: ${answer.saved.length} в ${answer.folder}`
      : "у задания пока нет готовых файлов");
  } catch (error) {
    text($("folder-hint"), "не удалось: " + error.message);
  }
};

addEventListener("beforeunload", (event) => {
  if (dirty) { event.preventDefault(); event.returnValue = ""; }
});

(async function boot() {
  const state = await api("/api/state");
  text($("key"), state.key ? "ключ OpenRouter: есть" : "ключ OpenRouter: не задан");
  $("key").className = state.key ? "" : "dim";
  text($("stat-out"), "результаты: " + state.output);
  $("folder").value = state.output;
  desktop = state.desktop;
  $("browse").disabled = !desktop;
  text($("folder-hint"), desktop
    ? "«Обзор…» открывает системный выбор папки"
    : "В браузере системный выбор папки недоступен — впишите путь и нажмите «Применить». "
      + "В оконной версии заработает кнопка «Обзор…».");
  $("pick").title = "Поддерживаются: " + state.formats.join(" ");
  await refresh();
})();
