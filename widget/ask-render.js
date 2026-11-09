// What an answer looks like: a safe subset of Markdown for the answer itself, and
// the tables of rows it is based on, each with a copy button. Everything is added
// as text (textContent), never as HTML, so nothing in an answer can run as code.

import { el } from "./ask-dom.js";

// Readable names for the columns the query tools return; others are not shown.
const COLUMNS = {
  title: "Title",
  artists: "Artists",
  label: "Label",
  catno: "Cat. no.",
  year: "Year",
  snapshot_date: "Date",
  as_of: "Prices as of",
  demand_as_of: "Want/have as of",
  lowest_price: "Lowest price",
  num_for_sale: "For sale",
  want: "Want",
  have: "Have",
  rating_average: "Rating",
  start_value: "From",
  end_value: "To",
  change_pct: "Change %",
  vinyl_releases: "Vinyl releases",
  releases_on_sale: "On sale",
  copies_for_sale: "Copies for sale",
  average_lowest_price: "Average lowest price",
  median_lowest_price: "Median lowest price",
  total_want: "Total want",
  total_have: "Total have",
  average_rating: "Average rating",
};

const TOOL_NAMES = {
  search_releases: "Search",
  rank_releases: "Ranking",
  release_history: "Price history",
  market_changes: "Market changes",
  compare_labels: "Label comparison",
};

// --- Copying a table -------------------------------------------------------------------

const SVG = "http://www.w3.org/2000/svg";
const COPIED_MS = 2_000;

// Two overlapping sheets: the usual "copy" sign. Drawn here, so the box needs no image files.
function copyIcon() {
  const icon = document.createElementNS(SVG, "svg");
  for (const [name, value] of Object.entries({ viewBox: "0 0 24 24", width: "16", height: "16", fill: "none",
    stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true", focusable: "false" })) icon.setAttribute(name, value);
  const sheet = document.createElementNS(SVG, "rect");
  for (const [name, value] of Object.entries({ x: "9", y: "9", width: "12", height: "12", rx: "2" })) sheet.setAttribute(name, value);
  const back = document.createElementNS(SVG, "path");
  back.setAttribute("d", "M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1");
  icon.append(sheet, back);
  return icon;
}

function cellText(cell) {
  return cell.textContent.replace(/\s+/g, " ").trim();
}

// Tab-separated rows paste into spreadsheets as cells, and read fine as plain text.
export function tableText(table) {
  return [...table.rows].map((row) => [...row.cells].map(cellText).join("\t")).join("\n");
}

// A bare copy of the table, without the page's classes, for editors that take formatted text.
function tableHtml(table) {
  const head = [...table.tHead.rows[0].cells].map((cell) => el("th", {}, cellText(cell)));
  const body = [...table.tBodies[0].rows].map((row) => el("tr", {}, ...[...row.cells].map((cell) => el("td", {}, cellText(cell)))));
  return el("table", {}, el("thead", {}, el("tr", {}, ...head)), el("tbody", {}, ...body)).outerHTML;
}

async function copyTable(table) {
  const text = tableText(table);
  if (window.ClipboardItem && navigator.clipboard?.write) {
    try {
      await navigator.clipboard.write([new window.ClipboardItem({
        "text/plain": new Blob([text], { type: "text/plain" }),
        "text/html": new Blob([tableHtml(table)], { type: "text/html" }),
      })]);
      return;
    } catch {
      // Some browsers refuse formatted text; plain text below still works.
    }
  }
  await navigator.clipboard.writeText(text);
}

// A table with its own copy button, and a heading beside the button if there is one.
function tableBlock(table, label, heading = null) {
  const note = el("span", { class: "mt-ask-copy-note", role: "status", "aria-live": "polite" });
  let timer = null;
  const button = el("button", { type: "button", class: "mt-ask-copy", "aria-label": label, title: label, onclick: async () => {
    clearTimeout(timer);
    try {
      await copyTable(table);
      note.textContent = "Copied";
    } catch {
      note.textContent = "Could not copy";
    }
    timer = setTimeout(() => { note.textContent = ""; }, COPIED_MS);
  } }, copyIcon());
  return el("div", { class: "mt-ask-table-block" },
    el("div", { class: "mt-ask-table-bar" }, heading, note, button),
    el("div", { class: "mt-ask-table" }, table));
}

// --- Safe Markdown subset: paragraphs, bold, italic, inline code, lists, tables ----------

function inline(text) {
  const fragment = document.createDocumentFragment();
  // Bold, italic (a lone * as in "2 * 3" stays as it is), and inline code.
  const pattern = /\*\*(.+?)\*\*|(?<![\w*])\*(?![\s*])([^*\n]*?[^\s*])\*(?![\w*])|`([^`]+)`/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) fragment.append(document.createTextNode(text.slice(last, match.index)));
    if (match[1] !== undefined) fragment.append(el("strong", {}, match[1]));
    else if (match[2] !== undefined) fragment.append(el("em", {}, match[2]));
    else fragment.append(el("code", {}, match[3]));
    last = match.index + match[0].length;
  }
  if (last < text.length) fragment.append(document.createTextNode(text.slice(last)));
  return fragment;
}

function tableCells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const BULLET = /^\s*[-*•]\s+/;
const NUMBERED = /^\s*\d+[.)]\s+/;

export function renderAnswer(text) {
  const fragment = document.createDocumentFragment();
  const tables = [];
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
    } else if (line.trim().startsWith("|") && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
      const head = tableCells(line);
      const body = [];
      i += 2;
      // A header row followed by its rule starts the next table, even without a blank line between.
      while (i < lines.length && lines[i].trim().startsWith("|")
             && !(i + 1 < lines.length && TABLE_RULE.test(lines[i + 1]))) body.push(tableCells(lines[i++]));
      const table = el("table", {},
        el("thead", {}, el("tr", {}, ...head.map((cell) => el("th", { scope: "col" }, inline(cell))))),
        el("tbody", {}, ...body.map((row) => el("tr", {}, ...row.map((cell) => el("td", {}, inline(cell)))))),
      );
      tables.push(table);
      fragment.append(tableBlock(table, "Copy table"));
    } else if (BULLET.test(line) || NUMBERED.test(line)) {
      const numbered = NUMBERED.test(line);
      const marker = numbered ? NUMBERED : BULLET;
      const list = el(numbered ? "ol" : "ul");
      while (i < lines.length && marker.test(lines[i])) list.append(el("li", {}, inline(lines[i++].replace(marker, ""))));
      fragment.append(list);
    } else if (/^\s*#{1,6}\s+/.test(line)) {
      fragment.append(el("p", { class: "mt-ask-heading" }, inline(line.replace(/^\s*#{1,6}\s+/, ""))));
      i += 1;
    } else {
      const paragraph = [];
      while (i < lines.length && lines[i].trim() && !lines[i].trim().startsWith("|")
             && !BULLET.test(lines[i]) && !NUMBERED.test(lines[i]) && !/^\s*#{1,6}\s+/.test(lines[i])) {
        paragraph.push(lines[i++].trim());
      }
      fragment.append(el("p", {}, inline(paragraph.join(" "))));
    }
  }
  // With more than one table, each copy button says which table it copies.
  if (tables.length > 1) {
    tables.forEach((table, index) => {
      const copy = table.closest(".mt-ask-table-block").querySelector(".mt-ask-copy");
      copy.setAttribute("aria-label", `Copy table ${index + 1} of ${tables.length}`);
      copy.title = copy.getAttribute("aria-label");
    });
  }
  return fragment;
}

// --- The data behind an answer --------------------------------------------------------

function display(value, key) {
  if (value == null) return "–";
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "number" && /price|_value$/.test(key)) return value.toFixed(2);
  return String(value);
}

export function renderSources(sources) {
  const fragment = document.createDocumentFragment();
  for (const source of sources ?? []) {
    const result = source.result;
    if (!result) continue;
    const rows = result.rows ?? [];
    const title = TOOL_NAMES[source.tool] ?? source.tool;
    const summary = rows.length === result.total
      ? `${rows.length} ${rows.length === 1 ? "row" : "rows"}`
      : `${rows.length} of ${result.total} rows`;
    const heading = el("p", { class: "mt-ask-source-title" }, `${title} · ${summary}`);
    if (!rows.length) {
      fragment.append(heading);
      continue;
    }
    const columns = Object.keys(COLUMNS).filter((key) => rows.some((row) => key in row));
    const priced = rows.some((row) => row.currency);
    const table = el("table", {},
      el("thead", {}, el("tr", {}, ...columns.map((key) => el("th", { scope: "col" },
        COLUMNS[key] + (priced && /price/.test(key) ? " (EUR)" : ""))))),
      el("tbody", {}, ...rows.map((row) => el("tr", {}, ...columns.map((key) => el("td", {}, display(row[key], key)))))),
    );
    fragment.append(tableBlock(table, `Copy the ${title} table`, heading));
  }
  return fragment;
}
