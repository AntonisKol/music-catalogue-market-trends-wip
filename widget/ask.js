// Market Trends question box for adnsaudio.com.
//
// Put this on a page:
//
//   <link rel="stylesheet" href="ask.css">
//   <div class="mt-ask" data-api="https://api.example.com" data-turnstile-sitekey="0x4AAAA..."></div>
//   <script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" async defer></script>
//   <script type="module" src="ask.js"></script>
//
// It sends a question with a Turnstile token to POST {data-api}/api/questions and
// shows the answer, the model that wrote it, any warning, and the rows the answer
// is based on. Everything from the API is added to the page as text (textContent),
// never as HTML, so nothing in an answer can run as code.

const MAX_QUESTION_LENGTH = 500;
const REQUEST_TIMEOUT_MS = 90_000;
// The human check runs invisibly; a question asked before it has finished waits this long.
const CHECK_WAIT_MS = 8_000;

const EXAMPLES = [
  "Which Planet E records are the most wanted?",
  "What is the cheapest record on Underground Resistance right now?",
  "How did the price of Throw change over the last month?",
  "Compare Planet E and Underground Resistance.",
];

// Readable names for the columns the query tools return; others are not shown.
const COLUMNS = {
  title: "Title",
  artists: "Artists",
  label: "Label",
  catno: "Cat. no.",
  year: "Year",
  snapshot_date: "Date",
  as_of: "As of",
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

// --- Small helpers ------------------------------------------------------------------

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "class") node.className = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, value);
  }
  for (const child of children) {
    if (child == null) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

// "claude-haiku-5-5" -> "Claude Haiku 5.5"
export function modelName(id) {
  if (typeof id !== "string" || !id) return "an AI model";
  const parts = id.split("-");
  const words = [];
  const version = [];
  for (const part of parts) {
    if (/^\d+$/.test(part)) version.push(part);
    else words.push(part.charAt(0).toUpperCase() + part.slice(1));
  }
  return [...words, version.join(".")].filter(Boolean).join(" ");
}

// --- Safe Markdown subset: paragraphs, bold, inline code, lists, tables ----------

function inline(text) {
  const fragment = document.createDocumentFragment();
  const pattern = /\*\*(.+?)\*\*|`([^`]+)`/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) fragment.append(document.createTextNode(text.slice(last, match.index)));
    fragment.append(match[1] !== undefined ? el("strong", {}, match[1]) : el("code", {}, match[2]));
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
      while (i < lines.length && lines[i].trim().startsWith("|")) body.push(tableCells(lines[i++]));
      const table = el("table", {},
        el("thead", {}, el("tr", {}, ...head.map((cell) => el("th", { scope: "col" }, inline(cell))))),
        el("tbody", {}, ...body.map((row) => el("tr", {}, ...row.map((cell) => el("td", {}, inline(cell)))))),
      );
      fragment.append(el("div", { class: "mt-ask-table" }, table));
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
    fragment.append(el("p", { class: "mt-ask-source-title" }, `${title} · ${summary}`));
    if (!rows.length) continue;
    const columns = Object.keys(COLUMNS).filter((key) => rows.some((row) => key in row));
    const priced = rows.some((row) => row.currency);
    const table = el("table", {},
      el("thead", {}, el("tr", {}, ...columns.map((key) => el("th", { scope: "col" },
        COLUMNS[key] + (priced && /price/.test(key) ? " (EUR)" : ""))))),
      el("tbody", {}, ...rows.map((row) => el("tr", {}, ...columns.map((key) => el("td", {}, display(row[key], key)))))),
    );
    fragment.append(el("div", { class: "mt-ask-table" }, table));
  }
  return fragment;
}

// --- The question box ------------------------------------------------------------------

function errorMessage(status, body, retryAfter) {
  const message = body?.error?.message;
  if (status === 429 && retryAfter) {
    const minutes = Math.ceil(Number(retryAfter) / 60);
    const wait = minutes >= 60 ? `${Math.ceil(minutes / 60)} hours` : `${minutes} minute${minutes === 1 ? "" : "s"}`;
    return `${message ?? "Too many questions."} You can ask again in about ${wait}.`;
  }
  return message ?? "Something went wrong. Please try again later.";
}

export function mount(root, { fetch: fetchImpl = window.fetch.bind(window), turnstile = () => window.turnstile } = {}) {
  const api = (root.dataset.api ?? "").replace(/\/$/, "");
  const sitekey = root.dataset.turnstileSitekey ?? "";
  const id = `mt-ask-${Math.random().toString(36).slice(2, 8)}`;

  const textarea = el("textarea", {
    id: `${id}-question`, name: "question", rows: "3", maxlength: String(MAX_QUESTION_LENGTH),
    placeholder: "e.g. Which Planet E records got more expensive this month?",
    "aria-describedby": `${id}-hint`,
  });
  const counter = el("span", { class: "mt-ask-counter", "aria-hidden": "true" }, `0 / ${MAX_QUESTION_LENGTH}`);
  const hint = el("p", { id: `${id}-hint`, class: "mt-ask-hint" },
    "Answers come from Discogs data on the labels this site tracks: prices, copies for sale, and demand.");
  const check = el("div", { class: "mt-ask-check" });
  const button = el("button", { type: "submit", class: "mt-ask-submit" }, "Ask");
  const status = el("p", { class: "mt-ask-status", role: "status", "aria-live": "polite" });
  const output = el("section", { class: "mt-ask-output", "aria-labelledby": `${id}-answer-title`, hidden: "" });
  const examples = el("div", { class: "mt-ask-examples" },
    el("p", { class: "mt-ask-examples-title" }, "Try asking"),
    el("ul", {}, ...EXAMPLES.map((example) => el("li", {},
      el("button", { type: "button", class: "mt-ask-example", onclick: () => {
        textarea.value = example;
        update();
        textarea.focus();
      } }, example)))),
  );
  const form = el("form", { class: "mt-ask-form", novalidate: "" },
    el("label", { for: textarea.id, class: "mt-ask-label" }, "Ask about the vinyl market"),
    textarea,
    el("div", { class: "mt-ask-row" }, hint, counter),
    check,
    el("div", { class: "mt-ask-actions" }, button),
  );
  root.replaceChildren(form, status, examples, output);

  let token = "";
  let widgetId = null;
  let busy = false;

  function update() {
    counter.textContent = `${textarea.value.length} / ${MAX_QUESTION_LENGTH}`;
  }
  textarea.addEventListener("input", update);
  textarea.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) form.requestSubmit();
  });

  function renderCheck() {
    const ts = turnstile();
    if (!ts) {
      setTimeout(renderCheck, 200);
      return;
    }
    widgetId = ts.render(check, {
      sitekey,
      theme: "dark",
      // Invisible for normal visitors; Cloudflare shows the box only if it is unsure.
      appearance: "interaction-only",
      callback: (value) => { token = value; },
      "expired-callback": () => { token = ""; },
      "error-callback": () => { token = ""; },
    });
  }
  renderCheck();

  async function waitForToken() {
    const started = Date.now();
    while (!token && Date.now() - started < CHECK_WAIT_MS) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return token;
  }

  function resetCheck() {
    token = "";
    const ts = turnstile();
    if (ts && widgetId !== null) ts.reset(widgetId);
  }

  function setBusy(value) {
    busy = value;
    button.disabled = value;
    button.textContent = value ? "Looking it up…" : "Ask";
    form.setAttribute("aria-busy", String(value));
  }

  function showStatus(message, kind) {
    status.textContent = message;
    status.className = `mt-ask-status${kind ? ` mt-ask-status-${kind}` : ""}`;
  }

  function showAnswer(body) {
    const heading = el("h2", { id: `${id}-answer-title`, class: "mt-ask-answer-title", tabindex: "-1" }, "Answer");
    const parts = [heading, el("div", { class: "mt-ask-answer" }, renderAnswer(body.answer))];
    if (body.warning) parts.push(el("p", { class: "mt-ask-warning", role: "note" }, body.warning));
    const rows = renderSources(body.sources);
    if (rows.childNodes.length) {
      const details = el("details", { class: "mt-ask-sources" }, el("summary", {}, "Show the data"), rows);
      if (body.warning) details.open = true;
      parts.push(details);
    }
    parts.push(el("p", { class: "mt-ask-model" }, `Answered by ${modelName(body.model)}. Prices are the cheapest copy on sale on Discogs, in any condition, without shipping.`));
    output.replaceChildren(...parts);
    output.hidden = false;
    heading.focus();
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    const question = textarea.value.trim();
    if (!question) {
      showStatus("Please type a question.", "error");
      textarea.focus();
      return;
    }
    setBusy(true);
    if (!token) {
      showStatus("Checking you're not a bot…");
      if (!(await waitForToken())) {
        setBusy(false);
        showStatus("The human check hasn't finished. If a check appears above the Ask button, please complete it, then ask again.", "error");
        return;
      }
    }
    output.hidden = true;
    showStatus("Looking it up… this can take a few seconds.");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetchImpl(`${api}/api/questions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, captcha_token: token }),
        signal: controller.signal,
      });
      const body = await response.json().catch(() => null);
      if (!response.ok || !body || typeof body.answer !== "string") {
        showStatus(errorMessage(response.status, body, response.headers.get("Retry-After")), "error");
        return;
      }
      showStatus("");
      showAnswer(body);
    } catch (error) {
      showStatus(error?.name === "AbortError"
        ? "The answer took too long. Please try again."
        : "Could not reach the question service. Please check your connection and try again.", "error");
    } finally {
      clearTimeout(timer);
      setBusy(false);
      resetCheck();
    }
  });

  return { form, textarea, button, status, output };
}

if (typeof document !== "undefined") {
  for (const root of document.querySelectorAll(".mt-ask[data-api]")) mount(root);
}
