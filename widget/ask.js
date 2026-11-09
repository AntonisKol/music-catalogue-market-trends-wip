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
//
// This file builds the box and asks the questions. The other parts are modules
// next to it, loaded by the browser from the same folder:
//   ask-dom.js       building elements, and error messages
//   ask-render.js    the answer's Markdown, and the tables of data behind it
//   ask-account.js   the account line and form: sign up, sign in, reset, delete
//   ask-requests.js  asking for a label the site doesn't track yet
//   ask-about.js     the labels tracked, and "About the data"

import { UNREACHABLE, el, errorMessage } from "./ask-dom.js";
import { renderAnswer, renderSources } from "./ask-render.js";
import { createAccount, rememberAccount } from "./ask-account.js";
import { createLabelRequests } from "./ask-requests.js";
import { createAbout } from "./ask-about.js";

export { renderAnswer, renderSources, tableText } from "./ask-render.js";
export { remainingText } from "./ask-account.js";
export { longDate } from "./ask-about.js";

const MAX_QUESTION_LENGTH = 500;
const REQUEST_TIMEOUT_MS = 90_000;
// The human check runs invisibly; a question asked before it has finished waits this long.
const CHECK_WAIT_MS = 8_000;

const EXAMPLES = [
  "Which Ostgut Ton records are the most wanted?",
  "What is the cheapest record on Underground Resistance right now?",
  "How has the price of Throw changed recently?",
  "Compare Ostgut Ton and Underground Resistance.",
];

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

export function mount(root, { fetch: fetchImpl = window.fetch.bind(window), turnstile = () => window.turnstile } = {}) {
  const api = (root.dataset.api ?? "").replace(/\/$/, "");
  const sitekey = root.dataset.turnstileSitekey ?? "";
  const id = `mt-ask-${Math.random().toString(36).slice(2, 8)}`;

  let token = "";
  let widgetId = null;
  const lock = { busy: false };  // one request at a time, shared with the account form

  function request(path, options = {}) {
    return fetchImpl(`${api}${path}`, { credentials: "include", ...options });
  }

  function postJson(path, body, signal) {
    return request(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
    });
  }

  const textarea = el("textarea", {
    id: `${id}-question`, name: "question", rows: "3", maxlength: String(MAX_QUESTION_LENGTH),
    placeholder: "e.g. Which Ostgut Ton records got more expensive recently?",
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

  const about = createAbout({ request });
  const account = createAccount({
    id, request, postJson, showStatus, lock,
    check: { token: () => token, waitForToken, resetCheck },
    // The label requests follow the account: who is signed in, if anyone.
    onRender: (state) => requests.render(state),
  });
  const requests = createLabelRequests({ id, request, postJson, showSignIn: account.showSignIn });

  const notice = el("p", { class: "mt-ask-privacy" },
    "Your questions are sent to Anthropic's Claude to write the answer. We don't store them. "
    + "To spot abuse, our logs keep a scrambled version of your network address, never the address itself. "
    + "Cloudflare's human check also sees your address. ",
    el("a", { href: "/privacy/" }, "Privacy"), ".");
  root.replaceChildren(about.labels, requests.requestArea, form, account.account, account.signInForm, status,
    examples, about.about, notice, output);

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
    lock.busy = value;
    button.disabled = value;
    button.textContent = value ? "Looking it up…" : "Ask";
    form.setAttribute("aria-busy", String(value));
  }

  function showStatus(message, kind) {
    status.textContent = message;
    status.className = `mt-ask-status${kind ? ` mt-ask-status-${kind}` : ""}`;
  }

  function showAnswer(body, question) {
    const heading = el("h2", { id: `${id}-answer-title`, class: "mt-ask-answer-title", tabindex: "-1" }, "Answer");
    // The field is emptied for the next question, so the question stays with its answer.
    const parts = [heading, el("p", { class: "mt-ask-asked" }, "You asked: ", el("q", {}, question)),
                   el("div", { class: "mt-ask-answer" }, renderAnswer(body.answer))];
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
    if (lock.busy) return;
    const question = textarea.value.trim();
    if (!question) {
      showStatus("Please type a question.", "error");
      textarea.focus();
      return;
    }
    setBusy(true);
    // Signed-in visitors skip the human check; the API knows them by their cookie.
    if (!token && !account.state()?.signed_in) {
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
        if (body?.error?.code === "visitor_limit_reached") account.showSignIn();
        const state = account.state();
        if (state && response.status === 429) {
          state.remaining = { hour: 0, day: 0 };
          account.render();
        }
        return;
      }
      showStatus("");
      showAnswer(body, question);
      textarea.value = "";
      update();
      const state = account.state();
      if (state && body.remaining) {
        state.remaining = body.remaining;
        account.render();
      }
    } catch (error) {
      showStatus(error?.name === "AbortError"
        ? "The answer took too long. Please try again."
        : UNREACHABLE, "error");
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

  account.setMode("signUp");
  account.load();
  about.loadLabels();
  about.loadDates();

  return {
    labels: about.labels, requestForm: requests.requestForm, form, textarea, button, status, output,
    account: account.account, signInForm: account.signInForm, email: account.email, password: account.password,
  };
}

if (typeof document !== "undefined") {
  for (const root of document.querySelectorAll(".mt-ask[data-api]")) mount(root);
}
