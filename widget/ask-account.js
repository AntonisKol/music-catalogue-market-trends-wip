// The account line and form: questions left, signing up, signing in, a forgotten
// password, signing out and deleting the account.
//
// The API keeps a visitor signed in with a cookie the page cannot read; every
// request sends it along with credentials: "include".

import { UNREACHABLE, el, errorMessage, link } from "./ask-dom.js";

const MIN_PASSWORD_LENGTH = 12;
// Remembered in this browser only, so a returning visitor sees the sign-in form first.
const HAS_ACCOUNT_KEY = "mt-ask-has-account";

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

// {hour: 2, day: 2} -> "2 questions left today"
export function remainingText(remaining) {
  if (!remaining) return "";
  const left = Math.min(remaining.hour, remaining.day);
  const when = remaining.hour < remaining.day ? "this hour" : "today";
  return `${left} ${left === 1 ? "question" : "questions"} left ${when}`;
}

export function rememberAccount() {
  try {
    window.localStorage.setItem(HAS_ACCOUNT_KEY, "yes");
  } catch {
    // Private windows may refuse; the form then opens on sign-up.
  }
}

function remembersAccount() {
  try {
    return window.localStorage.getItem(HAS_ACCOUNT_KEY) === "yes";
  } catch {
    return false;
  }
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

// What the account needs from the rest of the box:
//   id          prefixes the element ids
//   request, postJson   talk to the API
//   showStatus  shows a message in the box's status line
//   check       the human check: token(), waitForToken(), resetCheck()
//   lock        { busy }, shared with the question form so only one request runs
//   onRender    called with the account first, each time the account line is drawn
export function createAccount({ id, request, postJson, showStatus, check, lock, onRender }) {
  let state = null;  // from GET /api/account
  let mode = "signUp";

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

  function render() {
    onRender(state);
    if (!state) {
      account.hidden = true;
      return;
    }
    const left = el("span", {}, remainingText(state.remaining));
    if (state.signed_in) {
      signInForm.hidden = true;
      account.replaceChildren(
        el("span", {}, "Signed in as ", el("strong", {}, state.email)), " ",
        el("span", { class: "mt-ask-badge" }, "✓ Verified"), " ", "· ", left, " · ",
        link("Sign out", signOut), " · ", link("Delete my account", deleteAccount),
      );
    } else {
      account.replaceChildren(left, " · ", link("Sign up or sign in", showSignIn));
    }
    account.hidden = false;
  }

  async function load() {
    try {
      const response = await request("/api/account");
      state = response.ok ? await response.json() : null;
    } catch {
      state = null;
    }
    render();
  }

  async function signOut() {
    try {
      await postJson("/api/sign-out", {});
      showStatus("You are signed out.");
    } catch {
      showStatus(UNREACHABLE, "error");
    }
    await load();
  }

  async function deleteAccount() {
    if (!window.confirm("Delete your account? Your email address and question counts are removed. This cannot be undone.")) return;
    try {
      const response = await postJson("/api/account/delete", {});
      const body = await response.json().catch(() => null);
      showStatus(response.ok ? "Your account has been deleted." : errorMessage(response.status, body), response.ok ? "" : "error");
    } catch {
      showStatus(UNREACHABLE, "error");
    }
    await load();
  }

  function showAccountProblem(message, field) {
    signInStatus.textContent = message;
    signInStatus.className = "mt-ask-status mt-ask-status-error";
    field?.focus();
  }

  signInForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (lock.busy) return;
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
    lock.busy = true;
    signInButton.disabled = true;
    signInStatus.className = "mt-ask-status";
    signInStatus.textContent = "One moment…";
    try {
      if (!check.token() && !(await check.waitForToken())) {
        showAccountProblem("The human check hasn't finished. If a check appears above the Ask button, please complete it, then try again.");
        return;
      }
      const typed = password.value;
      const fields = settings.autocomplete ? { password: typed } : {};
      const response = await postJson(settings.path, { email: address, ...fields, captcha_token: check.token() });
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
        await load();
      } else {
        signInStatus.textContent = body?.message ?? "Please check your inbox.";
      }
    } catch {
      showAccountProblem(UNREACHABLE);
    } finally {
      // The password is never kept on the page, whether it worked or not.
      password.value = "";
      lock.busy = false;
      signInButton.disabled = false;
      check.resetCheck();
    }
  });

  return {
    account, signInForm, email, password,
    state: () => state, render, load, setMode, showSignIn,
  };
}
