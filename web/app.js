// Madoz Balears — static web, vanilla JS.
// One fetch of data.json, then everything happens in memory.

const state = {
  entries: [],
  filtered: [],
  search: "",
  island: "",
  district: "",
  municipality: "",
  type: "",
  vol: "",
  conf: "",
  only_madoz: false,
  sort_col: "title",
  sort_dir: "asc",
};

const COLS = ["title", "place_type", "island", "judicial_district",
              "municipality", "vol", "page_printed", "confidence"];

function esc(s) {
  if (s == null) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function $(id) { return document.getElementById(id); }
function fmt(n) { return Number(n).toLocaleString("ca-ES"); }
function norm(s) {
  if (!s) return "";
  return s.toString().toLowerCase()
    .normalize("NFD").replace(/[̀-ͯ]/g, "");
}

// === TABS ===
function initTabs() {
  document.querySelectorAll(".tabs .tab").forEach(btn => {
    btn.addEventListener("click", () => {
      const t = btn.dataset.toptab;
      document.querySelectorAll(".tabs .tab").forEach(b =>
        b.classList.toggle("active", b.dataset.toptab === t));
      document.querySelectorAll(".tab-content").forEach(sec =>
        sec.classList.toggle("active", sec.dataset.toptab === t));
      if (t === "stats") renderStats();
    });
  });
}

// === FILTERS ===
function populateFilters() {
  const counts = (key, sort = "n") => {
    const m = new Map();
    for (const e of state.entries) {
      const v = e[key];
      if (!v) continue;
      m.set(v, (m.get(v) || 0) + 1);
    }
    const arr = [...m.entries()];
    if (sort === "n") arr.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    else arr.sort((a, b) => a[0].localeCompare(b[0], "ca", { numeric: true }));
    return arr;
  };
  const fill = (id, arr, allLabel) => {
    $(id).innerHTML = `<option value="">${allLabel}</option>` +
      arr.map(([v, n]) => `<option value="${esc(v)}">${esc(v)} (${n})</option>`).join("");
  };
  fill("f-island", counts("island"), "— Totes —");
  fill("f-district", counts("judicial_district"), "— Tots —");
  fill("f-municipality", counts("municipality"), "— Tots —");
  fill("f-type", counts("place_type"), "— Tots —");
  fill("f-vol", counts("vol", "key"), "— Tots —");
}

function applyFilters() {
  const q = norm(state.search);
  state.filtered = state.entries.filter(e => {
    if (state.island && e.island !== state.island) return false;
    if (state.district && e.judicial_district !== state.district) return false;
    if (state.municipality && e.municipality !== state.municipality) return false;
    if (state.type && e.place_type !== state.type) return false;
    if (state.vol && e.vol !== state.vol) return false;
    if (state.conf && e.confidence !== state.conf) return false;
    if (state.only_madoz && !e.madoz_url) return false;
    if (q) {
      const hay = norm((e.title || "") + " " + (e.description || ""));
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  sortFiltered();
}

function sortFiltered() {
  const k = state.sort_col;
  const dir = state.sort_dir === "desc" ? -1 : 1;
  state.filtered.sort((a, b) => {
    let av = a[k], bv = b[k];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "number" && typeof bv === "number") return (av - bv) * dir;
    return String(av).localeCompare(String(bv), "ca", { numeric: true }) * dir;
  });
}

function renderTable() {
  applyFilters();
  const tbody = $("tbody-madoz");
  const total = state.filtered.length;
  $("count").textContent = `${fmt(total)} entrades`;
  if (!total) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty">Cap entrada amb aquests filtres.</td></tr>`;
    return;
  }
  const slice = state.filtered.slice(0, 500);
  const dot = c => c === "high" ? "●" : c === "medium" ? "◐" : c === "low" ? "○" : "—";
  tbody.innerHTML = slice.map(e => {
    const mark = e.madoz_url ? '<span class="link-mark" title="Enllaçat al scrape canònic">★</span>' : "";
    return `<tr data-id="${e.id}" class="madoz-row">
      <td><strong>${esc(e.title)}</strong> ${mark}</td>
      <td>${esc(e.place_type || "—")}</td>
      <td>${esc(e.island || "—")}</td>
      <td>${esc(e.judicial_district || "—")}</td>
      <td>${esc(e.municipality || "—")}</td>
      <td>${esc(e.vol)}/${esc(e.leaf)}</td>
      <td>${esc(e.page_printed || "—")}</td>
      <td class="conf-${esc(e.confidence || "")}">${dot(e.confidence)}</td>
    </tr>`;
  }).join("") + (total > 500
    ? `<tr><td colspan="8" class="empty">Mostrant 500 de ${fmt(total)}. Afina els filtres per veure menys.</td></tr>`
    : "");
  tbody.querySelectorAll("tr.madoz-row").forEach(tr =>
    tr.addEventListener("click", () => toggleExpand(tr)));
}

function toggleExpand(tr) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("madoz-expand")) {
    next.remove(); tr.classList.remove("expanded"); return;
  }
  document.querySelectorAll(".madoz-expand").forEach(el => el.remove());
  document.querySelectorAll(".madoz-row.expanded").forEach(el => el.classList.remove("expanded"));
  const id = Number(tr.dataset.id);
  const e = state.entries.find(x => x.id === id);
  if (!e) return;

  let statsHtml = "";
  if (e.stats && typeof e.stats === "object") {
    const items = Object.entries(e.stats).filter(([_, v]) => v != null && v !== "");
    if (items.length) {
      statsHtml = `<div class="entry-stats"><strong>Estadístiques:</strong> ` +
        items.map(([k, v]) =>
          `<span class="stat-pill">${esc(k)}: <strong>${esc(typeof v === "number" ? fmt(v) : v)}</strong></span>`
        ).join(" ") + `</div>`;
    }
  }
  let crefsHtml = "";
  if (e.cross_references && e.cross_references.length) {
    crefsHtml = `<div class="entry-crefs"><strong>Referències creuades:</strong> ` +
      e.cross_references.map(c => `<code>${esc(c)}</code>`).join(", ") + `</div>`;
  }
  const madozLink = e.madoz_url
    ? `<a href="${esc(e.madoz_url)}" target="_blank" rel="noopener">Veure a diccionariomadoz.com →</a>`
    : `<span class="text-muted">(sense article al scrape)</span>`;
  const noteHtml = e.note ? `<p class="entry-note"><em>Nota:</em> ${esc(e.note)}</p>` : "";

  const exp = document.createElement("tr");
  exp.className = "madoz-expand";
  exp.innerHTML = `<td colspan="8">
    <div class="madoz-article">
      <p class="madoz-body">${esc(e.description || "")}</p>
      ${statsHtml}
      ${crefsHtml}
      ${noteHtml}
      <p class="madoz-source">
        <span>Tom ${esc(e.vol)} · full ${esc(e.leaf)} · pàg. ${esc(e.page_printed || "?")}</span>
        · ${madozLink}
      </p>
    </div>
  </td>`;
  tr.classList.add("expanded");
  tr.insertAdjacentElement("afterend", exp);
}

function initSort() {
  document.querySelectorAll("#table-madoz th").forEach((th, i) => {
    const col = COLS[i];
    if (!col) return;
    th.classList.add("sortable");
    th.addEventListener("click", () => {
      if (state.sort_col === col) state.sort_dir = state.sort_dir === "asc" ? "desc" : "asc";
      else { state.sort_col = col; state.sort_dir = "asc"; }
      document.querySelectorAll("#table-madoz th").forEach(x => x.classList.remove("sort-asc", "sort-desc"));
      th.classList.add(`sort-${state.sort_dir}`);
      renderTable();
    });
  });
}

function bindFilters() {
  let t;
  $("f-search").addEventListener("input", e => {
    clearTimeout(t);
    t = setTimeout(() => { state.search = e.target.value.trim(); renderTable(); }, 180);
  });
  const sel = (id, key) => $(id).addEventListener("change", e => { state[key] = e.target.value; renderTable(); });
  sel("f-island", "island"); sel("f-district", "district"); sel("f-municipality", "municipality");
  sel("f-type", "type"); sel("f-vol", "vol"); sel("f-conf", "conf");
  $("f-only-madoz").addEventListener("change", e => { state.only_madoz = e.target.checked; renderTable(); });
  $("f-clear").addEventListener("click", () => {
    Object.assign(state, { search: "", island: "", district: "", municipality: "",
                            type: "", vol: "", conf: "", only_madoz: false });
    $("f-search").value = "";
    ["f-island", "f-district", "f-municipality", "f-type", "f-vol", "f-conf"].forEach(id => $(id).value = "");
    $("f-only-madoz").checked = false;
    renderTable();
  });
  $("f-export").addEventListener("click", exportCSV);
}

function exportCSV() {
  const fields = ["vol", "leaf", "page_printed", "title", "place_type", "island",
                  "judicial_district", "municipality", "confidence", "description"];
  const cell = v => {
    if (v == null) return "";
    const s = String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [fields.join(",")];
  for (const e of state.filtered) lines.push(fields.map(f => cell(e[f])).join(","));
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `madoz_balears_${state.filtered.length}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

// === STATS ===
let statsRendered = false;
function renderStats() {
  if (statsRendered) return;
  statsRendered = true;
  const count = key => {
    const m = new Map();
    for (const e of state.entries) {
      const v = e[key];
      if (!v) continue;
      m.set(v, (m.get(v) || 0) + 1);
    }
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  };
  const fill = (id, rows, fmtRow) => {
    const tb = document.querySelector(`#${id} tbody`);
    tb.innerHTML = rows.length
      ? rows.map(fmtRow).join("")
      : '<tr><td class="empty">—</td></tr>';
  };
  fill("stat-by-island", count("island"),
    ([v, n]) => `<tr><td>${esc(v)}</td><td class="num">${fmt(n)}</td></tr>`);
  fill("stat-by-type", count("place_type").slice(0, 15),
    ([v, n]) => `<tr><td>${esc(v)}</td><td class="num">${fmt(n)}</td></tr>`);
  fill("stat-by-district", count("judicial_district"),
    ([v, n]) => `<tr><td>${esc(v)}</td><td class="num">${fmt(n)}</td></tr>`);
  fill("stat-by-muni", count("municipality").slice(0, 20),
    ([v, n]) => `<tr><td>${esc(v)}</td><td class="num">${fmt(n)}</td></tr>`);
  const byVol = count("vol").sort((a, b) =>
    a[0].localeCompare(b[0], "ca", { numeric: true }));
  fill("stat-by-vol", byVol,
    ([v, n]) => `<tr><td>tom ${esc(v)}</td><td class="num">${fmt(n)}</td></tr>`);

  const total = state.entries.length;
  const linked = state.entries.filter(e => e.madoz_url).length;
  fill("stat-links", [
    ["Total text_entries", total],
    ["Enllaçats al scrape canònic", linked],
    ["Sense article al scrape", total - linked],
    ["Total madoz_entries (scrape)", state.madozTotal],
  ], ([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${fmt(v)}</td></tr>`);
}

// === BOOTSTRAP ===
async function main() {
  initTabs();
  try {
    const res = await fetch("data.json");
    if (!res.ok) throw new Error(`HTTP ${res.status} carregant data.json`);
    const payload = await res.json();
    state.entries = payload.entries;
    state.madozTotal = payload.madoz_total;
    document.querySelector("#stat-text").textContent = fmt(payload.text_total);
    document.querySelector("#stat-madoz").textContent = fmt(payload.madoz_total);
    document.querySelector("#stat-volumes").textContent = fmt(new Set(state.entries.map(e => e.vol)).size);
    document.querySelector("#stat-islands").textContent = fmt(new Set(state.entries.map(e => e.island).filter(Boolean)).size);
    document.querySelector("#stat-types").textContent = fmt(new Set(state.entries.map(e => e.place_type).filter(Boolean)).size);
    populateFilters();
    bindFilters();
    initSort();
    renderTable();
  } catch (err) {
    console.error(err);
    $("tbody-madoz").innerHTML =
      `<tr><td colspan="8" class="empty" style="color:#b00">Error: ${esc(err.message)}</td></tr>`;
  }
}

document.addEventListener("DOMContentLoaded", main);
