// Asking for a label the site doesn't track yet: signed-in visitors only, and the
// site's owner decides. A signed-in visitor's requests show under "Missing a label?".

import { UNREACHABLE, el, errorMessage, link } from "./ask-dom.js";

// While a label request is open, its status is checked this often, so a decision shows without a reload.
const REQUEST_CHECK_MS = 30_000;

const REQUEST_STATUS = {
  waiting: "waiting for review", added: "added", rejected: "rejected",
};

function describeRequest({ name, status, reason }) {
  const text = `${name}: ${REQUEST_STATUS[status] ?? status}`;
  return reason ? `${text} (${reason})` : text;
}

// id prefixes the element ids; request and postJson talk to the API; showSignIn
// opens the account form. Returns the area and render(state), called with the
// account (from GET /api/account) each time it changes.
export function createLabelRequests({ id, request, postJson, showSignIn }) {
  // Shown once it is known whether the visitor is signed in.
  const requestName = el("input", {
    id: `${id}-label-request`, type: "text", name: "label", maxlength: "100", autocomplete: "off",
    placeholder: "Label name, as on Discogs", "aria-label": "Label name",
  });
  const requestButton = el("button", { type: "submit", class: "mt-ask-submit" }, "Request");
  const requestStatus = el("p", { class: "mt-ask-status", role: "status", "aria-live": "polite" });
  const requestList = el("ul", { class: "mt-ask-requests", "aria-label": "Your label requests", hidden: "" });
  const requestForm = el("form", { class: "mt-ask-request-form", novalidate: "", hidden: "" },
    el("div", { class: "mt-ask-sign-in-row" }, requestName, requestButton),
    requestStatus,
  );
  const requestLine = el("p", { class: "mt-ask-request-line" });
  const requestArea = el("div", { class: "mt-ask-request", hidden: "" }, requestLine, requestForm, requestList);

  // While a request is open, the list is checked again every REQUEST_CHECK_MS, and
  // only while the page is visible.
  let requestsShownFor = null;  // the signed-in address the list belongs to
  let requestCheck = null;
  let checkDue = false;  // the check came while the page was hidden

  function stopCheckingRequests() {
    clearTimeout(requestCheck);
    requestCheck = null;
    checkDue = false;
  }

  function checkRequestsLater() {
    stopCheckingRequests();
    requestCheck = setTimeout(() => {
      requestCheck = null;
      if (document.hidden) checkDue = true;
      else loadMyRequests();
    }, REQUEST_CHECK_MS);
  }

  async function loadMyRequests() {
    const owner = requestsShownFor;
    let mine = null;
    try {
      const response = await request("/api/label-requests");
      if (response.ok) mine = (await response.json()).requests ?? [];
    } catch {
      // The list is a convenience; requesting still works. It is tried again later.
    }
    if (owner === null || owner !== requestsShownFor) return;  // signed out meanwhile
    if (mine) {
      // The bullet takes the status's colour, set with the text so both change together.
      requestList.replaceChildren(...mine.map((item) => el("li", { class: `mt-ask-request-${item.status}` },
        describeRequest(item))));
      requestList.hidden = mine.length === 0;
    }
    if (!mine || mine.some((item) => item.status === "waiting")) checkRequestsLater();
  }

  document.addEventListener("visibilitychange", () => {
    // Back on the page after the check was due: check now.
    if (!document.hidden && checkDue && requestsShownFor !== null) {
      checkDue = false;
      loadMyRequests();
    }
  });

  function toggleRequestForm() {
    requestForm.hidden = !requestForm.hidden;
    if (!requestForm.hidden) requestName.focus();
  }

  function render(state) {
    if (!state) {
      requestArea.hidden = true;
      return;
    }
    requestLine.replaceChildren("Missing a label? ", state.signed_in
      ? link("Request it", toggleRequestForm)
      : link("Sign up or sign in to request it", showSignIn));
    const signedInAs = state.signed_in ? state.email : null;
    if (signedInAs !== requestsShownFor) {
      requestsShownFor = signedInAs;
      stopCheckingRequests();
      requestList.replaceChildren();
      requestList.hidden = true;
      if (signedInAs !== null) loadMyRequests();
    }
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
      requestStatus.textContent = UNREACHABLE;
      requestStatus.className = "mt-ask-status mt-ask-status-error";
    } finally {
      requestButton.disabled = false;
    }
  });

  return { requestArea, requestForm, render };
}
