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
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function $(id) { return document.getElementById(id); }
function fmt(n) { return Number(n).toLocaleString("ca-ES"); }
function norm(s) {
  if (!s) return "";
  return s.toString().toLowerCase()
    .normalize("NFD").replace(/[̀-ͯ]/g, "");
}

// === TABS ===
function gotoTab(t) {
  document.querySelectorAll(".tabs .tab").forEach(b =>
    b.classList.toggle("active", b.dataset.toptab === t));
  document.querySelectorAll(".tab-content").forEach(sec =>
    sec.classList.toggle("active", sec.dataset.toptab === t));
  if (t === "stats") renderStats();
  if (t === "demografia") renderDemografia();
  if (t === "notes") renderNotes();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function initTabs() {
  document.querySelectorAll(".tabs .tab").forEach(btn => {
    btn.addEventListener("click", () => gotoTab(btn.dataset.toptab));
  });
  // "Per on començar" call-to-action buttons on the home tab.
  document.querySelectorAll(".home-action").forEach(btn => {
    btn.addEventListener("click", () => gotoTab(btn.dataset.goto));
  });
}

// === FILTERS ===
// Each dropdown key maps to a state slot + an entry field. Used by both
// the cascading refill and the predicate that filters the table.
const FILTER_DEFS = [
  { id: "f-island",       stateKey: "island",       field: "island",            allLabel: "— Totes —" },
  { id: "f-district",     stateKey: "district",     field: "judicial_district", allLabel: "— Tots —" },
  { id: "f-municipality", stateKey: "municipality", field: "municipality",      allLabel: "— Tots —" },
  { id: "f-type",         stateKey: "type",         field: "place_type",        allLabel: "— Tots —" },
  { id: "f-vol",          stateKey: "vol",          field: "vol",               allLabel: "— Tots —" },
  { id: "f-conf",         stateKey: "conf",         field: "confidence",        allLabel: "— Totes —" },
];

// True if `e` matches all currently-active filters EXCEPT the one
// identified by `exceptKey` (so a dropdown can list options as if its
// own filter were not applied).
function matchesExcept(e, exceptKey) {
  for (const f of FILTER_DEFS) {
    if (f.stateKey === exceptKey) continue;
    const v = state[f.stateKey];
    if (v && e[f.field] !== v) return false;
  }
  if (state.only_madoz && !e.madoz_url) return false;
  if (state.search) {
    const hay = norm((e.title || "") + " " + (e.description || ""));
    if (!hay.includes(norm(state.search))) return false;
  }
  return true;
}

// Repopulate every dropdown with the values that exist in the subset
// of entries matching all OTHER active filters. If the currently
// selected value disappears (e.g. you select Formentera and the
// previously-chosen Manacor district vanishes), clear it.
function refillFilters() {
  for (const f of FILTER_DEFS) {
    const counts = new Map();
    for (const e of state.entries) {
      if (!matchesExcept(e, f.stateKey)) continue;
      const v = e[f.field];
      if (v == null || v === "") continue;
      counts.set(v, (counts.get(v) || 0) + 1);
    }
    const arr = [...counts.entries()];
    if (f.id === "f-vol" || f.id === "f-municipality") {
      arr.sort((a, b) => a[0].localeCompare(b[0], "ca", { numeric: true }));
    } else if (f.id === "f-conf") {
      const order = { high: 0, medium: 1, low: 2 };
      arr.sort((a, b) => (order[a[0]] ?? 9) - (order[b[0]] ?? 9));
    } else {
      arr.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    }
    // Drop selection if it no longer fits the current cascade.
    const cur = state[f.stateKey];
    if (cur && !counts.has(cur)) state[f.stateKey] = "";

    const labelMap = f.id === "f-conf"
      ? { high: "Alta", medium: "Mitjana", low: "Baixa" }
      : null;
    const opts = arr.map(([v, n]) => {
      const label = labelMap ? (labelMap[v] || v) : v;
      return `<option value="${esc(v)}">${esc(label)} (${n})</option>`;
    }).join("");
    const sel = $(f.id);
    sel.innerHTML = `<option value="">${f.allLabel}</option>` + opts;
    sel.value = state[f.stateKey] || "";
  }
}

function applyFilters() {
  state.filtered = state.entries.filter(e => matchesExcept(e, null));
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
    const mark = e.madoz_url ? '<span class="link-mark" title="També té article a diccionariomadoz.com">★</span>' : "";
    const volLeaf = e.ia_url
      ? `<a href="${esc(e.ia_url)}" target="_blank" rel="noopener" class="ia-link" title="Obre el facsímil a Internet Archive (pàgina sencera)">${esc(e.vol)}/${esc(e.leaf)} ↗</a>`
      : `${esc(e.vol)}/${esc(e.leaf)}`;
    return `<tr data-id="${e.id}" class="madoz-row">
      <td><strong>${esc(e.title)}</strong> ${mark}</td>
      <td>${esc(e.place_type || "—")}</td>
      <td>${esc(e.island || "—")}</td>
      <td>${esc(e.judicial_district || "—")}</td>
      <td>${esc(e.municipality || "—")}</td>
      <td>${volLeaf}</td>
      <td>${esc(e.page_printed || "—")}</td>
      <td class="conf-${esc(e.confidence || "")}">${dot(e.confidence)}</td>
    </tr>`;
  }).join("") + (total > 500
    ? `<tr><td colspan="8" class="empty">Mostrant 500 de ${fmt(total)}. Afina els filtres per veure menys.</td></tr>`
    : "");
  tbody.querySelectorAll("tr.madoz-row").forEach(tr =>
    tr.addEventListener("click", ev => {
      if (ev.target.closest("a")) return;  // let inline links (IA, etc.) navigate
      toggleExpand(tr);
    }));
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
    : `<span class="text-muted">(sense article corresponent a diccionariomadoz.com)</span>`;
  const noteHtml = e.note
    ? `<details class="entry-note">
        <summary>📝 Nota d'extracció</summary>
        <p class="entry-note-body">${esc(e.note)}</p>
      </details>`
    : "";

  // Mega-article complement: when diccionariomadoz.com has significantly
  // more text than our OCR, offer a toggle to read their fuller version.
  // Marked clearly as external transcription, not our facsimile-faithful OCR.
  const supplementHtml = e.madoz_content
    ? `<details class="madoz-supplement">
        <summary>📖 Versió ampliada de diccionariomadoz.com
          (${fmt(e.madoz_content.length)} caràcters vs. ${fmt((e.description || '').length)} els nostres)</summary>
        <div class="madoz-supplement-body">${esc(e.madoz_content)}</div>
        <p class="madoz-supplement-note">
          Transcripció web de tercers, no del nostre OCR. Sovint més completa per articles
          grans (villes amb ayuntamiento) però amb errors propis de transcripció.
        </p>
      </details>`
    : "";

  const exp = document.createElement("tr");
  exp.className = "madoz-expand";
  exp.innerHTML = `<td colspan="8">
    <div class="madoz-article">
      <p class="madoz-body">${esc(e.description || "")}</p>
      ${statsHtml}
      ${crefsHtml}
      ${noteHtml}
      ${supplementHtml}
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

function update() {
  refillFilters();
  renderTable();
}

function bindFilters() {
  let t;
  $("f-search").addEventListener("input", e => {
    clearTimeout(t);
    t = setTimeout(() => { state.search = e.target.value.trim(); update(); }, 180);
  });
  const sel = (id, key) => $(id).addEventListener("change", e => { state[key] = e.target.value; update(); });
  sel("f-island", "island"); sel("f-district", "district"); sel("f-municipality", "municipality");
  sel("f-type", "type"); sel("f-vol", "vol"); sel("f-conf", "conf");
  $("f-only-madoz").addEventListener("change", e => { state.only_madoz = e.target.checked; update(); });
  $("f-clear").addEventListener("click", () => {
    Object.assign(state, { search: "", island: "", district: "", municipality: "",
                            type: "", vol: "", conf: "", only_madoz: false });
    $("f-search").value = "";
    $("f-only-madoz").checked = false;
    update();
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
    ["Total d'entrades del nostre OCR", total],
    ["També presents a diccionariomadoz.com", linked],
    ["Només al nostre OCR", total - linked],
    ["Total d'articles a diccionariomadoz.com", state.madozTotal],
  ], ([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${fmt(v)}</td></tr>`);
}

// === DEMOGRAFIA TAB ===
// Renders SVG bar charts from the per-entry `stats` JSON. Pure inline
// SVG — no chart library — so the page stays dependency-free.

let demoRendered = false;

// Inline horizontal-bar chart. `rows` is [[label, value, sub?], ...]
// sorted descending. `fmtVal` formats the numeric value for display.
function svgBars(rows, opts = {}) {
  const fmtVal = opts.fmt || fmt;
  const colour = opts.colour || "var(--accent)";
  const labelW = opts.labelW ?? 160;
  const barH = opts.barH ?? 18;
  const gap = opts.gap ?? 6;
  const valueW = opts.valueW ?? 90;
  const width = 720;
  const innerW = width - labelW - valueW - 20;
  const max = Math.max(...rows.map(r => r[1]));
  const height = rows.length * (barH + gap);
  const lines = rows.map((r, i) => {
    const [label, val, sub] = r;
    const w = max > 0 ? Math.max(1, (val / max) * innerW) : 0;
    const y = i * (barH + gap);
    return (
      `<g transform="translate(0,${y})">` +
      `<text x="${labelW - 6}" y="${barH * 0.72}" text-anchor="end" class="bar-label">${esc(label)}</text>` +
      `<rect x="${labelW}" y="0" width="${w}" height="${barH}" rx="2" fill="${colour}"/>` +
      `<text x="${labelW + w + 6}" y="${barH * 0.72}" class="bar-value">${esc(fmtVal(val))}${sub ? ` <tspan class="bar-sub">${esc(sub)}</tspan>` : ""}</text>` +
      `</g>`
    );
  }).join("");
  return `<svg viewBox="0 0 ${width} ${height}" class="bars-svg" preserveAspectRatio="xMinYMin meet" role="img">${lines}</svg>`;
}

function statsOf(e) {
  return (e.stats && typeof e.stats === "object") ? e.stats : null;
}

// Exclude part. jud. and isla aggregates from per-municipality charts —
// their population is the SUM of their constituent municipios and would
// double-count if shown alongside individual munis.
function isMunicipality(e) {
  return e.place_type && e.place_type !== "partido judicial";
}

function renderDemografia() {
  if (demoRendered) return;
  demoRendered = true;

  // === Coverage line ===
  const withAlmas = state.entries.filter(e => isMunicipality(e) && statsOf(e)?.almas != null);
  const withVec = state.entries.filter(e => isMunicipality(e) && statsOf(e)?.vecinos != null);
  const withRiq = state.entries.filter(e => isMunicipality(e) && statsOf(e)?.riqueza_imponible != null);
  $("demo-coverage").innerHTML =
    `<strong>Cobertura de dades:</strong> ` +
    `${withAlmas.length} entrades amb total d'animes · ` +
    `${withVec.length} amb vecinos · ` +
    `${withRiq.length} amb riquesa imponible. ` +
    `Les entrades sense xifres pròpies (alqueries, predios, llogarets satèl·lits) queden fora dels gràfics; Madoz les agrega al municipi pare.`;

  // === Top 20 by almas ===
  const byAlmas = withAlmas
    .map(e => [e.title, statsOf(e).almas, statsOf(e).vecinos ? `${fmt(statsOf(e).vecinos)} vec.` : null])
    .sort((a, b) => b[1] - a[1])
    .slice(0, 20);
  $("demo-chart-almas").innerHTML = byAlmas.length
    ? svgBars(byAlmas)
    : '<p class="empty">Sense dades.</p>';

  // === Top 20 by riqueza imponible ===
  const byRiq = withRiq
    .map(e => [e.title, statsOf(e).riqueza_imponible, "rs."])
    .sort((a, b) => b[1] - a[1])
    .slice(0, 20);
  $("demo-chart-riq").innerHTML = byRiq.length
    ? svgBars(byRiq, { colour: "var(--accent-secondary, #c2410c)" })
    : '<p class="empty">Sense dades.</p>';

  // === Population aggregated per island ===
  const islandTotals = new Map();
  for (const e of withAlmas) {
    const island = e.island || "(sense illa)";
    islandTotals.set(island, (islandTotals.get(island) || 0) + statsOf(e).almas);
  }
  const byIsla = [...islandTotals.entries()]
    .map(([k, v]) => [k, v, "habitants"])
    .sort((a, b) => b[1] - a[1]);
  $("demo-chart-illa").innerHTML = byIsla.length
    ? svgBars(byIsla, { colour: "#0f766e", labelW: 130 })
    : '<p class="empty">Sense dades.</p>';

  // === Almas / vecinos ratio per municipality (household size) ===
  const ratioRows = state.entries
    .filter(e => isMunicipality(e) && statsOf(e)?.almas && statsOf(e)?.vecinos)
    .map(e => {
      const s = statsOf(e);
      return [e.title, +(s.almas / s.vecinos).toFixed(2), `${fmt(s.vecinos)} → ${fmt(s.almas)}`];
    })
    .sort((a, b) => b[1] - a[1])
    .slice(0, 25);
  $("demo-chart-ratio").innerHTML = ratioRows.length
    ? svgBars(ratioRows, {
        colour: "#7c3aed",
        fmt: v => v.toFixed(2),
      })
    : '<p class="empty">Sense dades.</p>';

  // === Industry totals across all entries ===
  const INDUSTRY_KEYS = [
    ["molinos_viento", "Molins de vent"],
    ["molinos_agua", "Molins d'aigua"],
    ["molinos_aceite", "Molins d'oli"],
    ["alambiques", "Alambics (aiguardent)"],
    ["fab_aguardiente", "Fàb. d'aiguardent"],
    ["fab_fideos", "Fàb. de fideus"],
    ["jabonerias_jabon_fuerte", "Jaboneries (sabó fort)"],
    ["jabonerias_blanco", "Jaboneries (sabó blanc)"],
    ["tahonas", "Tafones"],
    ["tejares", "Teulares"],
    ["telares_lienzo", "Telers de lli"],
    ["herrerias", "Ferreries"],
  ];
  const industryTotals = INDUSTRY_KEYS.map(([key, label]) => {
    let sum = 0, n = 0;
    for (const e of state.entries) {
      const s = statsOf(e);
      if (s && typeof s[key] === "number") { sum += s[key]; n++; }
    }
    return [label, sum, `en ${n} municipis`];
  }).filter(r => r[1] > 0)
    .sort((a, b) => b[1] - a[1]);
  $("demo-chart-ind").innerHTML = industryTotals.length
    ? svgBars(industryTotals, { colour: "#92400e", labelW: 200 })
    : '<p class="empty">Sense dades.</p>';

  // === Contribución per ánima (rs./hab) ===
  const percapRows = state.entries
    .filter(e => isMunicipality(e) && statsOf(e)?.contribucion_rs && statsOf(e)?.almas)
    .map(e => {
      const s = statsOf(e);
      const rate = s.contribucion_rs / s.almas;
      return [e.title, +rate.toFixed(2), `${fmt(s.contribucion_rs)} rs. / ${fmt(s.almas)} hab.`];
    })
    .sort((a, b) => b[1] - a[1])
    .slice(0, 25);
  $("demo-chart-percap").innerHTML = percapRows.length
    ? svgBars(percapRows, {
        colour: "#be123c",
        fmt: v => `${v.toFixed(2)} rs.`,
      })
    : '<p class="empty">Sense dades.</p>';
}

// === NOTES TAB (Madoz abbreviations) ===
let notesRendered = false;
async function renderNotes() {
  if (notesRendered) return;
  notesRendered = true;
  const container = document.getElementById("abbreviations-container");
  try {
    const res = await fetch("abbreviations.json");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    // Helper: render an alphabetical 2-col table from a list of [abbr, meaning].
    const renderTable = items => {
      const sorted = [...items].sort(
        (a, b) => norm(a[0]).localeCompare(norm(b[0]), "es")
      );
      const rows = sorted.map(([a, m]) =>
        `<tr><td class="abbr-cell">${esc(a)}</td><td>${esc(m)}</td></tr>`
      ).join("");
      return `<table class="abbr-table"><tbody>${rows}</tbody></table>`;
    };

    // Official Madoz list (flat alphabetical from all categories).
    const officialItems = data.categories.flatMap(c => c.items);
    const officialHtml = renderTable(officialItems);

    // Optional supplementary table (in-entry abbreviations not in the source).
    const supp = data.supplementary;
    const suppHtml = supp ? `
      <div class="modern-intro" style="margin-top:2.5em">
        <h3>${esc(supp.title)}</h3>
        <p>${esc(supp.intro)}</p>
      </div>
      ${renderTable(supp.items)}
    ` : "";

    const notes = (data.context_notes || []).map(n => {
      const html = esc(n).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
      return `<li>${html}</li>`;
    }).join("");

    container.innerHTML = `
      ${officialHtml}
      ${suppHtml}
      ${notes ? `<div class="modern-intro" style="margin-top:2em">
        <h3>Notes contextuals</h3>
        <ul class="notes-list">${notes}</ul>
        <p style="font-size:0.85em;color:var(--text-muted);margin-top:1.5em">
          Font: ${esc(data.source)}
        </p>
      </div>` : ""}
    `;
  } catch (err) {
    container.innerHTML = `<p class="empty" style="color:#b00">Error carregant abreviatures: ${esc(err.message)}</p>`;
    console.error(err);
  }
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

    // Explore-tab stat bar.
    const types = new Set(state.entries.map(e => e.place_type).filter(Boolean)).size;
    $("stat-text").textContent = fmt(payload.text_total);
    $("stat-volumes").textContent = fmt(new Set(state.entries.map(e => e.vol)).size);
    $("stat-islands").textContent = fmt(new Set(state.entries.map(e => e.island).filter(Boolean)).size);
    $("stat-types").textContent = fmt(types);

    // Home-tab hero + per-island cards.
    $("home-stat-entries").textContent = fmt(payload.text_total);
    $("home-stat-types").textContent = fmt(types);
    const byIsland = state.entries.reduce((m, e) => {
      const k = e.island || "_none";
      m[k] = (m[k] || 0) + 1;
      return m;
    }, {});
    $("home-src-mallorca").textContent = fmt(byIsland.Mallorca || 0);
    $("home-src-menorca").textContent = fmt(byIsland.Menorca || 0);
    $("home-src-ibiza").textContent = fmt(byIsland.Ibiza || 0);
    $("home-src-formentera").textContent = fmt(byIsland.Formentera || 0);
    $("home-src-cabrera").textContent = fmt(byIsland.Cabrera || 0);
    $("home-src-balears").textContent = fmt(byIsland.Baleares || 0);
    bindFilters();
    initSort();
    update();
  } catch (err) {
    console.error(err);
    $("tbody-madoz").innerHTML =
      `<tr><td colspan="8" class="empty" style="color:#b00">Error: ${esc(err.message)}</td></tr>`;
  }
}

document.addEventListener("DOMContentLoaded", main);
