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
//
// Visitors get a few questions without an account. For more they sign up with an
// email address and a password, and confirm the address with an emailed link. The
// API then keeps them signed in with a cookie the page cannot read, sent along
// because every request uses credentials: "include".

const MAX_QUESTION_LENGTH = 500;
const MIN_PASSWORD_LENGTH = 12;
// Remembered in this browser only, so a returning visitor sees the sign-in form first.
const HAS_ACCOUNT_KEY = "mt-ask-has-account";
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

// The three things the account form can do.
const ACCOUNT_MODES = {
  signUp: {
    title: "Sign up for more questions", button: "Sign up", path: "/api/sign-up", autocomplete: "new-password",
    placeholder: `Password, at least ${MIN_PASSWORD_LENGTH} characters`,
    hint: "We email you a link to confirm your address. Then you return here, verified.",
  },
  signIn: {
    title: "Sign in", button: "Sign in", path: "/api/sign-in", autocomplete: "current-password",
    placeholder: "Password", hint: "With the email and password you signed up with.",
  },
  reset: {
    title: "Forgot your password?", button: "Email me a link", path: "/api/password-reset",
    hint: "We email you a link to choose a new one.",
  },
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

// {hour: 2, day: 2} -> "2 questions left today"
export function remainingText(remaining) {
  if (!remaining) return "";
  const left = Math.min(remaining.hour, remaining.day);
  const when = remaining.hour < remaining.day ? "this hour" : "today";
  return `${left} ${left === 1 ? "question" : "questions"} left ${when}`;
}

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
  // Filled in from the API, and stays hidden if the list can't be loaded.
  const labels = el("aside", { class: "mt-ask-labels", "aria-label": "Labels this site tracks", hidden: "" });
  // Asking for a label the site doesn't track yet: signed-in visitors only, and the
  // site's owner decides. Shown once it is known whether the visitor is signed in.
  const requestName = el("input", {
    id: `${id}-label-request`, type: "text", name: "label", maxlength: "100", autocomplete: "off",
    placeholder: "Label name, as on Discogs", "aria-label": "Label name",
  });
  const requestButton = el("button", { type: "submit", class: "mt-ask-submit" }, "Request");
  const requestStatus = el("p", { class: "mt-ask-status", role: "status", "aria-live": "polite" });
  const requestList = el("ul", { class: "mt-ask-requests", "aria-label": "Your label requests" });
  const requestForm = el("form", { class: "mt-ask-request-form", novalidate: "", hidden: "" },
    el("div", { class: "mt-ask-sign-in-row" }, requestName, requestButton),
    requestStatus,
    requestList,
  );
  const requestLine = el("p", { class: "mt-ask-request-line" });
  const requestArea = el("div", { class: "mt-ask-request", hidden: "" }, requestLine, requestForm);
  const form = el("form", { class: "mt-ask-form", novalidate: "" },
    el("label", { for: textarea.id, class: "mt-ask-label" }, "Ask about the vinyl market"),
    textarea,
    el("div", { class: "mt-ask-row" }, hint, counter),
    check,
    el("div", { class: "mt-ask-actions" }, button),
  );

  // The account line: questions left, and sign in or the signed-in address.
  const account = el("div", { class: "mt-ask-account", hidden: "" });
  const email = el("input", {
    id: `${id}-email`, type: "email", name: "email", autocomplete: "username", required: "",
    placeholder: "you@example.com", "aria-label": "Email address", maxlength: "254",
  });
  const password = el("input", {
    id: `${id}-password`, type: "password", name: "password", required: "",
    placeholder: "Password", "aria-label": "Password", maxlength: "256",
  });
  const signInTitle = el("p", { class: "mt-ask-label" });
  const signInHint = el("p", { class: "mt-ask-hint" });
  const signInButton = el("button", { type: "submit", class: "mt-ask-submit" });
  const signInStatus = el("p", { class: "mt-ask-status", role: "status", "aria-live": "polite" });
  const signInLinks = el("p", { class: "mt-ask-switch" });
  const signInForm = el("form", { class: "mt-ask-sign-in", novalidate: "", hidden: "" },
    signInTitle,
    signInHint,
    el("div", { class: "mt-ask-sign-in-row" }, email, password, signInButton),
    signInStatus,
    signInLinks,
    el("p", { class: "mt-ask-privacy" },
      "We store your email address, a scrambled copy of your password that can't be turned back into it, "
      + "when you confirmed and last signed in, and how many questions you asked in the last two days. "
      + "Resend sends our emails. You can delete your account at any time."),
  );
  const notice = el("p", { class: "mt-ask-privacy" },
    "Your questions are sent to Anthropic's Claude to write the answer. We don't store them. "
    + "To spot abuse, our logs keep a scrambled version of your network address, never the address itself. "
    + "Cloudflare's human check also sees your address. ",
    el("a", { href: "/privacy/" }, "Privacy"), ".");
  root.replaceChildren(labels, requestArea, form, account, signInForm, status, examples, notice, output);

  let token = "";
  let widgetId = null;
  let busy = false;
  let state = null;  // from GET /api/account
  let mode = "signUp";

  function request(path, options = {}) {
    return fetchImpl(`${api}${path}`, { credentials: "include", ...options });
  }

  function postJson(path, body, signal) {
    return request(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
    });
  }

  function link(text, onclick) {
    return el("button", { type: "button", class: "mt-ask-link", onclick }, text);
  }

  function setMode(name) {
    mode = name;
    const settings = ACCOUNT_MODES[name];
    signInTitle.textContent = settings.title;
    signInHint.textContent = settings.hint;
    signInButton.textContent = settings.button;
    password.hidden = !settings.autocomplete;
    password.value = "";
    if (settings.autocomplete) {
      password.autocomplete = settings.autocomplete;
      password.placeholder = settings.placeholder;
    }
    signInStatus.textContent = "";
    const switches = {
      signUp: [["Already have an account? Sign in", "signIn"], ["Forgot your password?", "reset"]],
      signIn: [["New here? Sign up", "signUp"], ["Forgot your password?", "reset"]],
      reset: [["Back to sign in", "signIn"]],
    }[name];
    const links = switches.flatMap(([text, target], index) => [
      ...(index ? [" · "] : []),
      link(text, () => { setMode(target); email.focus(); }),
    ]);
    signInLinks.replaceChildren(...links);
    if (name === "signIn" && !signInForm.hidden) fillSavedPassword();
  }

  function remembersAccount() {
    try {
      return window.localStorage.getItem(HAS_ACCOUNT_KEY) === "yes";
    } catch {
      return false;
    }
  }

  function rememberAccount() {
    try {
      window.localStorage.setItem(HAS_ACCOUNT_KEY, "yes");
    } catch {
      // Private windows may refuse; the form then opens on sign-up.
    }
  }

  // The form appears after a click, so the browser doesn't fill it by itself. Asking
  // for a saved password lets the browser offer it (Chrome and Edge).
  function fillSavedPassword() {
    try {
      if (!window.PasswordCredential || !navigator.credentials?.get || email.value || password.value) return;
      navigator.credentials.get({ password: true, mediation: "optional" }).then((credential) => {
        if (credential?.type !== "password" || mode !== "signIn" || email.value || password.value) return;
        email.value = credential.id;
        password.value = credential.password;
      }).catch(() => {});
    } catch {
      // Filling is a convenience; typing still works.
    }
  }

  function showSignIn() {
    setMode(remembersAccount() ? "signIn" : "signUp");
    signInForm.hidden = false;
    if (mode === "signIn") fillSavedPassword();
    email.focus();
  }

  const REQUEST_STATUS = { waiting: "waiting for review", added: "added", declined: "not added" };

  async function loadMyRequests() {
    try {
      const response = await request("/api/label-requests");
      const mine = response.ok ? (await response.json()).requests : [];
      requestList.replaceChildren(...mine.map((item) => el("li", {}, `${item.name}: ${REQUEST_STATUS[item.status] ?? item.status}`)));
    } catch {
      // The list is a convenience; requesting still works.
    }
  }

  function toggleRequestForm() {
    requestForm.hidden = !requestForm.hidden;
    if (!requestForm.hidden) {
      requestName.focus();
      loadMyRequests();
    }
  }

  function renderRequestArea() {
    if (!state) {
      requestArea.hidden = true;
      return;
    }
    requestLine.replaceChildren("Missing a label? ", state.signed_in
      ? link("Request it", toggleRequestForm)
      : link("Sign up or sign in to request it", showSignIn));
    if (!state.signed_in) requestForm.hidden = true;
    requestArea.hidden = false;
  }

  requestForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = requestName.value.trim();
    if (name.length < 2) {
      requestStatus.textContent = "Please type the label's name.";
      requestStatus.className = "mt-ask-status mt-ask-status-error";
      requestName.focus();
      return;
    }
    requestButton.disabled = true;
    requestStatus.className = "mt-ask-status";
    requestStatus.textContent = "Sending…";
    try {
      const response = await postJson("/api/label-requests", { name });
      const body = await response.json().catch(() => null);
      if (response.ok) {
        requestStatus.textContent = body?.message ?? "Thanks.";
        requestName.value = "";
        await loadMyRequests();
      } else {
        requestStatus.textContent = errorMessage(response.status, body, response.headers.get("Retry-After"));
        requestStatus.className = "mt-ask-status mt-ask-status-error";
      }
    } catch {
      requestStatus.textContent = "Could not reach the question service. Please try again.";
      requestStatus.className = "mt-ask-status mt-ask-status-error";
    } finally {
      requestButton.disabled = false;
    }
  });

  function renderAccount() {
    renderRequestArea();
    if (!state) {
      account.hidden = true;
      return;
    }
    const left = el("span", {}, remainingText(state.remaining));
    if (state.signed_in) {
      signInForm.hidden = true;
      account.replaceChildren(
        el("span", {}, "Signed in as ", el("strong", {}, state.email)), " ",
        ...(state.verified ? [el("span", { class: "mt-ask-badge" }, "✓ Verified"), " "] : []), "· ", left, " · ",
        link("Sign out", signOut), " · ", link("Delete my account", deleteAccount),
      );
    } else {
      account.replaceChildren(left, " · ", link("Sign up or sign in", showSignIn));
    }
    account.hidden = false;
  }

  async function loadAccount() {
    try {
      const response = await request("/api/account");
      state = response.ok ? await response.json() : null;
    } catch {
      state = null;
    }
    renderAccount();
  }

  async function loadLabels() {
    try {
      const response = await request("/api/labels");
      const names = response.ok ? (await response.json()).labels : [];
      if (!names?.length) return;
      labels.replaceChildren(
        el("strong", {}, `Labels we track (${names.length}): `),
        names.join(", "),
      );
      labels.hidden = false;
    } catch {
      // The box works without the list.
    }
  }

  async function signOut() {
    try {
      await postJson("/api/sign-out", {});
      showStatus("You are signed out.");
    } catch {
      showStatus("Could not reach the question service. Please try again.", "error");
    }
    await loadAccount();
  }

  async function deleteAccount() {
    if (!window.confirm("Delete your account? Your email address and question counts are removed. This cannot be undone.")) return;
    try {
      const response = await postJson("/api/account/delete", {});
      const body = await response.json().catch(() => null);
      showStatus(response.ok ? "Your account has been deleted." : errorMessage(response.status, body), response.ok ? "" : "error");
    } catch {
      showStatus("Could not reach the question service. Please try again.", "error");
    }
    await loadAccount();
  }

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

  // The page signs in without loading a new page, so browsers need to be asked
  // to save the password. Browsers without this ask by themselves, or not at all.
  function offerToSavePassword(address, typed) {
    try {
      if (!window.PasswordCredential || !navigator.credentials?.store) return;
      navigator.credentials.store(new window.PasswordCredential({ id: address, password: typed })).catch(() => {});
    } catch {
      // Saving is a convenience; signing in has already worked.
    }
  }

  function showAccountProblem(message, field) {
    signInStatus.textContent = message;
    signInStatus.className = "mt-ask-status mt-ask-status-error";
    field?.focus();
  }

  signInForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    const settings = ACCOUNT_MODES[mode];
    const address = email.value.trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(address)) {
      showAccountProblem("Please type a valid email address.", email);
      return;
    }
    if (settings.autocomplete && !password.value) {
      showAccountProblem("Please type your password.", password);
      return;
    }
    if (mode === "signUp" && password.value.length < MIN_PASSWORD_LENGTH) {
      showAccountProblem(`Please choose a password of at least ${MIN_PASSWORD_LENGTH} characters.`, password);
      return;
    }
    busy = true;
    signInButton.disabled = true;
    signInStatus.className = "mt-ask-status";
    signInStatus.textContent = "One moment…";
    try {
      if (!token && !(await waitForToken())) {
        showAccountProblem("The human check hasn't finished. If a check appears above the Ask button, please complete it, then try again.");
        return;
      }
      const typed = password.value;
      const fields = settings.autocomplete ? { password: typed } : {};
      const response = await postJson(settings.path, { email: address, ...fields, captcha_token: token });
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        showAccountProblem(errorMessage(response.status, body, response.headers.get("Retry-After")));
        return;
      }
      if (settings.autocomplete) {
        offerToSavePassword(address, typed);
        rememberAccount();
      }
      if (mode === "signIn") {
        signInStatus.textContent = "";
        showStatus("You are signed in.");
        await loadAccount();
      } else {
        signInStatus.textContent = body?.message ?? "Please check your inbox.";
      }
    } catch {
      showAccountProblem("Could not reach the question service. Please check your connection and try again.");
    } finally {
      // The password is never kept on the page, whether it worked or not.
      password.value = "";
      busy = false;
      signInButton.disabled = false;
      resetCheck();
    }
  });

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
    // Signed-in visitors skip the human check; the API knows them by their cookie.
    if (!token && !state?.signed_in) {
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
      const response = await postJson("/api/questions", { question, captcha_token: token }, controller.signal);
      const body = await response.json().catch(() => null);
      if (!response.ok || !body || typeof body.answer !== "string") {
        showStatus(errorMessage(response.status, body, response.headers.get("Retry-After")), "error");
        if (body?.error?.code === "visitor_limit_reached") showSignIn();
        if (state && response.status === 429) {
          state.remaining = { hour: 0, day: 0 };
          renderAccount();
        }
        return;
      }
      showStatus("");
      showAnswer(body);
      if (state && body.remaining) {
        state.remaining = body.remaining;
        renderAccount();
      }
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

  // Coming back from a link in an email.
  const arrival = new URLSearchParams(window.location.search);
  if (arrival.has("verified")) showStatus("Your email is confirmed. You are signed in.");
  if (arrival.has("password_changed")) showStatus("Your password is changed. You are signed in.");
  if (arrival.has("verified") || arrival.has("password_changed")) {
    rememberAccount();
    window.history.replaceState(null, "", window.location.pathname);
  }

  setMode("signUp");
  loadAccount();
  loadLabels();

  return { labels, requestForm, form, textarea, button, status, output, account, signInForm, email, password };
}

if (typeof document !== "undefined") {
  for (const root of document.querySelectorAll(".mt-ask[data-api]")) mount(root);
}
