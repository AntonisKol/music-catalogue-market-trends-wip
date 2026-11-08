"""The question box in a real browser (issue 13).

Most tests run widget/ask.js in Chromium against a faked API: asking, answers,
loading, validation, errors, the account form, keyboard use, labels, focus,
announcements for screen readers, and an accessibility audit with axe. The last
test runs the box against the real web API and the throwaway test database.

They need Playwright's Chromium: python -m playwright install chromium. Without
it they are skipped with a reason, except in CI (CI=true), where they fail.
"""
import functools
import http.server
import json
import os
import re
import time
import shutil
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
WIDGET = ROOT / "widget"
API = "https://api.example.test"
EXAMPLE = "Which Ostgut Ton records are the most wanted?"

# Cloudflare's script is replaced: tokens come at once, or never if the page sets
# window.__holdToken before the box starts.
TURNSTILE = """window.turnstile = {
  render(element, options) {
    window.__turnstile = options;
    if (!window.__holdToken) setTimeout(() => options.callback("human-token"), 20);
    return 1;
  },
  reset() {
    if (!window.__holdToken) setTimeout(() => window.__turnstile.callback("human-token"), 20);
  },
};"""

ANSWER = {
    "answer": "**Throw** is the cheapest at 8.90 EUR.\n\n| Title | Price |\n| --- | --- |\n| *Throw* | 8.90 |",
    "sources": [{"tool": "rank_releases", "result": {
        "rows": [{"title": "Throw", "artists": ["Paperclip People"], "lowest_price": 8.9, "currency": "EUR"}],
        "total": 1,
    }}],
    "warning": None,
    "model": "claude-haiku-5-5",
    "remaining": {"hour": 2, "day": 2},
}
ANONYMOUS = {"signed_in": False, "email": None,
             "limits": {"hour": 3, "day": 3}, "remaining": {"hour": 3, "day": 3}}


# --- The page, the browser and a faked API --------------------------------------------

class Site:
    """The widget's files and a demo page per API address, served over http."""

    def __init__(self, directory: Path):
        self.directory = directory
        for name in ("ask.js", "ask.css"):
            shutil.copy(WIDGET / name, directory / name)
        handler = functools.partial(_QuietHandler, directory=str(directory))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def page(self, name: str, api: str) -> str:
        html = (WIDGET / "index.html").read_text()
        html = re.sub(r'data-api="[^"]*"', f'data-api="{api}"', html)
        (self.directory / name).write_text(html)
        return f"{self.origin}/{name}"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class FakeApi:
    """Answers the box's requests and records them. A reply can be held back, so the
    test can look at the box while it waits."""

    def __init__(self, origin: str):
        self.origin = origin
        self.requests = []
        self.account = dict(ANONYMOUS)
        self.labels = ["Planet E", "Tresor"]
        self.label_requests = []
        self.replies = {}
        self.hold = False
        self.held = []

    def reply(self, path: str, status: int, body=None, headers=None, abort=False):
        self.replies[path] = (status, body, headers or {}, abort)

    def handle(self, route):
        request = route.request
        path = urlsplit(request.url).path
        cors = {
            "Access-Control-Allow-Origin": self.origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Expose-Headers": "Retry-After",
        }
        if request.method == "OPTIONS":
            route.fulfill(status=204, headers=cors)
            return
        body = json.loads(request.post_data) if request.post_data else None
        self.requests.append((request.method, path, body))
        if path == "/api/account":
            reply = (200, self.account, {}, False)
        elif path == "/api/label-requests" and request.method == "GET":
            reply = (200, {"requests": self.label_requests}, {}, False)
        elif path == "/api/labels":
            reply = self.replies.get(path, (200, {"labels": self.labels}, {}, False))
        elif path == "/api/questions":
            reply = self.replies.get(path, (200, ANSWER, {}, False))
        else:
            reply = self.replies.get(path, (200, {"status": "ok", "message": "Check your inbox."}, {}, False))
        if self.hold and path == "/api/questions":
            self.held.append((route, reply, cors))
            return
        self._send(route, reply, cors)

    def release(self):
        while self.held:
            self._send(*self.held.pop(0))

    @staticmethod
    def _send(route, reply, cors):
        status, body, headers, abort = reply
        if abort:
            route.abort()
        elif isinstance(body, str):
            route.fulfill(status=status, headers={**cors, **headers}, content_type="text/plain", body=body)
        else:
            route.fulfill(status=status, headers={**cors, **headers}, content_type="application/json",
                          body=json.dumps(body))

    def sent(self, path: str) -> list:
        return [body for method, request_path, body in self.requests if method == "POST" and request_path == path]


class Box:
    def __init__(self, page, api: FakeApi):
        self.page = page
        self.api = api
        self.status = page.locator(".mt-ask > .mt-ask-status")
        self.textarea = page.get_by_label("Ask about the vinyl market")
        self.ask_button = page.get_by_role("button", name="Ask", exact=True)
        self.account = page.locator(".mt-ask-account")
        self.form = page.locator(".mt-ask-sign-in")
        self.form_status = self.form.locator(".mt-ask-status")
        self.email = page.get_by_role("textbox", name="Email address")
        self.password = page.get_by_label("Password", exact=True)

    def ask(self, question: str = EXAMPLE):
        self.textarea.fill(question)
        self.ask_button.click()

    def focused(self, script: str = "document.activeElement"):
        return self.page.evaluate(f"(() => {{ const e = {script}; return e && (e.name || e.id || e.textContent); }})()")


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch()
        except PlaywrightError as error:
            if os.environ.get("CI") == "true":
                raise
            pytest.skip("Playwright's Chromium is not installed; run: python -m playwright install chromium "
                        f"({str(error).splitlines()[0]})")
        yield chromium
        chromium.close()


@pytest.fixture(scope="session")
def site(tmp_path_factory):
    served = Site(tmp_path_factory.mktemp("site"))
    yield served
    served.server.shutdown()


def open_page(browser, url, api=None, init_script="", width=1100, fake_clock=False):
    context = browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    if fake_clock:
        page.clock.install()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    if init_script:
        page.add_init_script(init_script)
    if api:
        page.route(f"{API}/**", api.handle)
    page.route("https://challenges.cloudflare.com/**",
               lambda route: route.fulfill(content_type="text/javascript", body=TURNSTILE))
    page.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"), lambda route: route.abort())
    page.goto(url)
    page.locator(".mt-ask-account:not([hidden])").wait_for()
    return context, page, errors


@pytest.fixture
def make_box(browser, site):
    opened = []

    def make(init_script="", width=1100, query="", account=None, label_requests=None, fake_clock=False):
        api = FakeApi(site.origin)
        if account:
            api.account = account
        if label_requests is not None:
            api.label_requests = label_requests
        context, page, errors = open_page(browser, site.page("fake.html", API) + query, api, init_script, width,
                                          fake_clock)
        opened.append((context, errors))
        return Box(page, api)

    yield make
    for context, errors in opened:
        context.close()
        assert errors == [], f"the page threw: {errors}"


@pytest.fixture
def box(make_box):
    return make_box()


# --- Asking and answers -------------------------------------------------------------------

def test_a_question_is_sent_with_the_human_check_and_its_answer_shown(box):
    box.ask("  " + EXAMPLE + "  ")

    box.page.locator(".mt-ask-answer").wait_for()
    assert box.api.sent("/api/questions") == [{"question": EXAMPLE, "captcha_token": "human-token"}]
    answer = box.page.locator(".mt-ask-answer")
    assert answer.locator("strong").first.inner_text() == "Throw"
    assert answer.locator("table em").inner_text() == "Throw"
    assert answer.locator("th").all_inner_texts() == ["Title", "Price"]
    assert box.page.locator(".mt-ask-model").inner_text().startswith("Answered by Claude Haiku 5.5.")
    assert box.account.inner_text().startswith("2 questions left today")


def test_the_field_is_emptied_for_the_next_question_and_the_question_stays_with_its_answer(box):
    box.ask(EXAMPLE)

    box.page.locator(".mt-ask-answer").wait_for()
    assert box.textarea.input_value() == ""
    assert box.page.locator(".mt-ask-counter").inner_text() == "0 / 500"
    assert box.page.locator(".mt-ask-asked").inner_text() == f"You asked: {EXAMPLE}"


def test_the_data_behind_an_answer_is_one_click_away(box):
    box.ask()
    details = box.page.locator(".mt-ask-sources")
    details.wait_for()

    assert details.get_attribute("open") is None
    box.page.get_by_text("Show the data").click()
    assert details.locator("th").all_inner_texts() == ["Title", "Artists", "Lowest price (EUR)"]
    assert details.locator("td").all_inner_texts() == ["Throw", "Paperclip People", "8.90"]


def test_a_warning_is_shown_and_opens_the_data(box):
    box.api.reply("/api/questions", 200, {**ANSWER, "warning": "Some figures could not be checked."})
    box.ask()

    box.page.locator(".mt-ask-warning").wait_for()
    assert box.page.get_by_role("note").inner_text() == "Some figures could not be checked."
    assert box.page.locator(".mt-ask-sources").get_attribute("open") is not None


def test_nothing_in_an_answer_runs_as_code(box):
    box.api.reply("/api/questions", 200, {**ANSWER, "answer": '<img src=x onerror="window.__ran=1"> **<b>bold</b>**'})
    box.ask()

    box.page.locator(".mt-ask-answer").wait_for()
    assert box.page.locator(".mt-ask-answer img, .mt-ask-answer b").count() == 0
    assert "<img src=x" in box.page.locator(".mt-ask-answer").inner_text()
    assert box.page.evaluate("window.__ran") is None


def clipboard(box):
    """Grants the page the clipboard; returns copy(button name) -> the text that button put there."""
    box.page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=box.page.url)

    def copy(name, exact=False):
        box.page.evaluate("navigator.clipboard.writeText('')")
        button = box.page.get_by_role("button", name=name, exact=exact)
        button.click()
        expect(button.locator("xpath=..").locator(".mt-ask-copy-note")).to_have_text("Copied")
        return box.page.evaluate("navigator.clipboard.readText()")

    return copy


def test_a_table_in_the_answer_can_be_copied(box):
    copy = clipboard(box)
    box.ask()

    assert copy("Copy table", exact=True) == "Title\tPrice\nThrow\t8.90"
    html = box.page.evaluate("""async () => {
      const [item] = await navigator.clipboard.read();
      return (await item.getType("text/html")).text();
    }""")
    assert "<th>Title</th>" in html and "<td>Throw</td>" in html and "class=" not in html


def test_tables_written_back_to_back_are_split_and_copied_one_at_a_time(box):
    copy = clipboard(box)
    box.api.reply("/api/questions", 200, {**ANSWER, "answer": (
        "Two labels:\n\n| Label | Records |\n| --- | --- |\n| Ostgut Ton | 12 |\n"
        "| Label | Want |\n| --- | --- |\n| Tresor | 900 |\n| Planet E | 450 |")})
    box.ask()

    answer = box.page.locator(".mt-ask-answer")
    answer.locator("table").first.wait_for()
    assert answer.locator("table").count() == 2
    assert answer.locator("table").nth(0).locator("td").all_inner_texts() == ["Ostgut Ton", "12"]
    assert copy("Copy table 2 of 2") == "Label\tWant\nTresor\t900\nPlanet E\t450"
    assert copy("Copy table 1 of 2") == "Label\tRecords\nOstgut Ton\t12"


def test_each_data_table_has_its_own_copy_button(box):
    copy = clipboard(box)
    box.api.reply("/api/questions", 200, {**ANSWER, "sources": ANSWER["sources"] + [{"tool": "compare_labels", "result": {
        "rows": [{"label": "Tresor", "vinyl_releases": 40}], "total": 1}}]})
    box.ask()
    box.page.get_by_text("Show the data").click()

    assert copy("Copy the Label comparison table") == "Label\tVinyl releases\nTresor\t40"
    assert copy("Copy the Ranking table") == "Title\tArtists\tLowest price (EUR)\nThrow\tPaperclip People\t8.90"


def test_a_failed_copy_says_so(box):
    box.page.evaluate("""() => {
      const refuse = () => Promise.reject(new Error("denied"));
      Object.defineProperty(navigator, "clipboard", { value: { write: refuse, writeText: refuse } });
    }""")
    box.ask()

    box.page.get_by_role("button", name="Copy table", exact=True).click()
    expect(box.page.locator(".mt-ask-answer .mt-ask-copy-note")).to_have_text("Could not copy")


# --- Loading and validation ----------------------------------------------------------------

def test_while_waiting_the_box_says_so_and_sends_only_one_question(box):
    box.api.hold = True
    box.ask()
    box.page.wait_for_function("document.querySelector('.mt-ask > .mt-ask-status').textContent.startsWith('Looking')")

    assert box.page.locator(".mt-ask-form").get_attribute("aria-busy") == "true"
    assert box.page.locator(".mt-ask-form .mt-ask-submit").inner_text() == "Looking it up…"
    assert box.page.locator(".mt-ask-form .mt-ask-submit").is_disabled()
    assert box.status.inner_text() == "Looking it up… this can take a few seconds."
    box.textarea.press("Control+Enter")
    box.page.locator(".mt-ask-form .mt-ask-submit").click(force=True)
    assert len(box.api.sent("/api/questions")) == 1

    box.api.release()
    box.page.locator(".mt-ask-answer").wait_for()
    assert box.ask_button.is_enabled()
    assert box.page.locator(".mt-ask-form").get_attribute("aria-busy") == "false"


@pytest.mark.parametrize("question", ["", "    "])
def test_an_empty_question_is_not_sent(box, question):
    box.ask(question)

    assert box.status.inner_text() == "Please type a question."
    assert box.focused() == "question"
    assert box.api.sent("/api/questions") == []


def test_the_question_is_counted_and_capped_at_500_characters(box):
    box.textarea.fill("x" * 12)
    assert box.page.locator(".mt-ask-counter").inner_text() == "12 / 500"

    box.textarea.fill("")
    box.textarea.press_sequentially("y" * 510, delay=0)
    assert len(box.textarea.input_value()) == 500


def test_without_a_finished_human_check_nothing_is_sent(make_box):
    box = make_box(init_script="window.__holdToken = true;")
    box.ask()

    box.page.wait_for_function("document.querySelector('.mt-ask > .mt-ask-status').textContent.includes('hasn')",
                               timeout=15000)
    assert "human check hasn't finished" in box.status.inner_text()
    assert box.api.sent("/api/questions") == []
    assert box.ask_button.is_enabled()


# --- Errors -----------------------------------------------------------------------------------

def test_the_visitor_limit_explains_when_to_come_back_and_offers_sign_up(box):
    box.api.reply("/api/questions", 429, {"error": {"code": "visitor_limit_reached",
                                                    "message": "You can ask 3 questions a day."}},
                  headers={"Retry-After": "1800"})
    box.ask()

    box.form.wait_for()
    assert box.status.inner_text() == "You can ask 3 questions a day. You can ask again in about 30 minutes."
    assert box.page.locator(".mt-ask-sign-in .mt-ask-label").inner_text() == "Sign up for more questions"
    assert box.focused() == "email"
    assert box.account.inner_text().startswith("0 questions left")


@pytest.mark.parametrize("status, body, abort, expected", [
    (503, {"error": {"code": "busy", "message": "The question service is busy."}}, False,
     "The question service is busy."),
    (500, "Internal Server Error", False, "Something went wrong. Please try again later."),
    (0, None, True, "Could not reach the question service. Please check your connection and try again."),
])
def test_failures_become_plain_messages(box, status, body, abort, expected):
    box.api.reply("/api/questions", status, body, abort=abort)
    box.ask()

    box.page.wait_for_function("document.querySelector('.mt-ask > .mt-ask-status').classList.contains('mt-ask-status-error')")
    assert box.status.inner_text() == expected
    assert box.page.locator(".mt-ask-output").is_hidden()
    assert box.ask_button.is_enabled()


# --- The label list -----------------------------------------------------------------------

def test_the_tracked_labels_are_listed_above_the_box(box):
    box.page.locator(".mt-ask-labels:not([hidden])").wait_for()

    assert box.page.locator(".mt-ask-labels").inner_text() == "Labels we track (2): Planet E, Tresor"


def test_the_box_works_when_the_label_list_cannot_load(browser, site):
    api = FakeApi(site.origin)
    api.reply("/api/labels", 503, {"error": {"code": "database"}})
    context, page, errors = open_page(browser, site.page("fake.html", API), api)
    try:
        assert page.locator(".mt-ask-labels").is_hidden()
        Box(page, api).ask()
        page.locator(".mt-ask-answer").wait_for()
        assert errors == []
    finally:
        context.close()


# --- The account form ----------------------------------------------------------------------

def test_signing_up_sends_the_address_and_password_and_keeps_neither_on_the_page(box):
    box.page.get_by_role("button", name="Sign up or sign in", exact=True).click()
    box.email.fill("listener@example.com")
    box.password.fill("a long enough password")
    box.page.get_by_role("button", name="Sign up", exact=True).click()

    box.page.wait_for_function("document.querySelector('.mt-ask-sign-in .mt-ask-status').textContent.includes('inbox')")
    assert box.api.sent("/api/sign-up") == [{"email": "listener@example.com", "password": "a long enough password",
                                            "captcha_token": "human-token"}]
    assert box.password.input_value() == ""


@pytest.mark.parametrize("email, password, message", [
    ("not an address", "a long enough password", "Please type a valid email address."),
    ("listener@example.com", "", "Please type your password."),
    ("listener@example.com", "too short", "Please choose a password of at least 12 characters."),
])
def test_the_sign_up_form_checks_before_sending(box, email, password, message):
    box.page.get_by_role("button", name="Sign up or sign in", exact=True).click()
    box.email.fill(email)
    box.password.fill(password)
    box.page.get_by_role("button", name="Sign up", exact=True).click()

    assert box.form_status.inner_text() == message
    assert box.api.sent("/api/sign-up") == []


def test_the_form_switches_between_sign_up_sign_in_and_a_new_password(box):
    box.page.get_by_role("button", name="Sign up or sign in", exact=True).click()
    assert box.password.get_attribute("autocomplete") == "new-password"

    box.page.get_by_role("button", name="Already have an account? Sign in").click()
    assert box.page.get_by_role("button", name="Sign in", exact=True).is_visible()
    assert box.password.get_attribute("autocomplete") == "current-password"

    box.page.get_by_role("button", name="Forgot your password?").click()
    assert box.password.is_hidden()
    box.email.fill("listener@example.com")
    box.page.get_by_role("button", name="Email me a link").click()
    box.page.wait_for_function("document.querySelector('.mt-ask-sign-in .mt-ask-status').textContent.includes('inbox')")
    assert box.api.sent("/api/password-reset") == [{"email": "listener@example.com", "captcha_token": "human-token"}]


def test_a_wrong_password_shows_the_api_message(box):
    box.api.reply("/api/sign-in", 401, {"error": {"code": "invalid_credentials",
                                                  "message": "That email and password don't match."}})
    box.page.get_by_role("button", name="Sign up or sign in", exact=True).click()
    box.page.get_by_role("button", name="Already have an account? Sign in").click()
    box.email.fill("listener@example.com")
    box.password.fill("not the password")
    box.password.press("Enter")

    box.page.wait_for_function("document.querySelector('.mt-ask-sign-in .mt-ask-status').textContent.includes('match')")
    assert box.form_status.inner_text() == "That email and password don't match."
    assert box.password.input_value() == ""


SIGNED_IN = {"signed_in": True, "email": "listener@example.com",
             "limits": {"hour": 10, "day": 50}, "remaining": {"hour": 10, "day": 50}}


def test_signed_in_visitors_see_the_badge_and_skip_the_human_check(make_box):
    box = make_box(init_script="window.__holdToken = true;", account=SIGNED_IN)

    assert box.account.inner_text().startswith("Signed in as listener@example.com ✓ Verified")
    box.ask()
    box.page.locator(".mt-ask-answer").wait_for()
    assert box.api.sent("/api/questions") == [{"question": EXAMPLE, "captcha_token": ""}]


def test_deleting_the_account_asks_first(make_box):
    box = make_box(account=SIGNED_IN)

    box.page.once("dialog", lambda dialog: dialog.dismiss())
    box.page.get_by_role("button", name="Delete my account").click()
    assert box.api.sent("/api/account/delete") == []

    box.page.once("dialog", lambda dialog: dialog.accept())
    box.page.get_by_role("button", name="Delete my account").click()
    box.page.wait_for_function("document.querySelector('.mt-ask > .mt-ask-status').textContent.includes('deleted')")
    assert box.api.sent("/api/account/delete") == [{}]


def test_coming_back_from_the_confirmation_link_says_so_and_cleans_the_address(make_box):
    box = make_box(query="?verified=1", account=SIGNED_IN)

    assert box.status.inner_text() == "Your email is confirmed. You are signed in."
    assert "verified" not in box.page.url


# --- Asking for a label ----------------------------------------------------------------------

def test_visitors_without_an_account_are_offered_sign_up_to_request_a_label(box):
    box.page.get_by_role("button", name="Sign up or sign in to request it").click()

    assert box.form.is_visible()
    assert box.focused() == "email"


def test_signed_in_visitors_can_request_a_label_and_see_their_requests(make_box):
    box = make_box(account=SIGNED_IN, label_requests=[{"name": "Arts", "status": "added"}])
    box.api.reply("/api/label-requests", 201, {"status": "requested", "message": "Thanks. Orbe Records is waiting for review."})

    box.page.get_by_role("button", name="Request it").click()
    field = box.page.get_by_role("textbox", name="Label name")
    assert box.focused() == "label"
    requests = box.page.get_by_role("list", name="Your label requests")
    requests.locator("li").first.wait_for()
    assert requests.locator("li").all_inner_texts() == ["Arts: added"]

    box.api.label_requests = [{"name": "Orbe Records", "status": "waiting"}, {"name": "Arts", "status": "added"}]
    field.fill("Orbe Records")
    field.press("Enter")

    box.page.wait_for_function("document.querySelector('.mt-ask-request-form .mt-ask-status').textContent.includes('Thanks')")
    assert box.api.sent("/api/label-requests") == [{"name": "Orbe Records"}]
    assert field.input_value() == ""
    assert requests.locator("li").all_inner_texts() == ["Orbe Records: waiting for review", "Arts: added"]


def test_each_request_shows_where_it_stands(make_box):
    box = make_box(account=SIGNED_IN, label_requests=[
        {"name": "Arts", "status": "waiting", "reason": None},
        {"name": "Pole Recordings", "status": "added", "reason": None},
        {"name": "Wax", "status": "rejected", "reason": "Not a techno label."},
    ])

    requests = box.page.get_by_role("list", name="Your label requests")
    requests.locator("li").first.wait_for()

    assert requests.locator("li").all_inner_texts() == [
        "Arts: waiting for review", "Pole Recordings: added",
        "Wax: rejected (Not a techno label.)"]


def test_a_signed_in_visitors_requests_show_without_a_click(make_box):
    box = make_box(account=SIGNED_IN, label_requests=[{"name": "Arts", "status": "added", "reason": None}])

    requests = box.page.get_by_role("list", name="Your label requests")
    requests.locator("li").first.wait_for()

    assert requests.locator("li").all_inner_texts() == ["Arts: added"]
    assert box.page.locator(".mt-ask-request-form").is_hidden()


def test_without_requests_no_empty_list_shows(make_box):
    box = make_box(account=SIGNED_IN)
    loaded_requests(box, 1)

    assert box.page.locator(".mt-ask-requests").is_hidden()


def test_an_open_request_updates_without_a_reload(make_box):
    box = make_box(account=SIGNED_IN, fake_clock=True,
                   label_requests=[{"name": "Arts", "status": "waiting", "reason": None}])
    requests = box.page.get_by_role("list", name="Your label requests")
    requests.locator("li").first.wait_for()
    assert requests.locator("li").all_inner_texts() == ["Arts: waiting for review"]

    assert bullet_colour(box) == YELLOW

    # Still waiting at the first check; added by the second. The colour changes with the text.
    box.page.clock.run_for(31_000)
    loaded_requests(box, 2)
    box.api.label_requests = [{"name": "Arts", "status": "added", "reason": None}]
    box.page.clock.run_for(31_000)
    box.page.wait_for_function("document.querySelector('.mt-ask-requests li').textContent.endsWith('added')")
    assert bullet_colour(box) == GREEN

    # Nothing is open any more, so the checks stop.
    box.page.clock.run_for(120_000)
    box.page.wait_for_timeout(500)
    assert loaded_requests(box, 3) == 3


YELLOW, GREEN, RED = "rgb(243, 201, 105)", "rgb(127, 217, 154)", "rgb(255, 143, 143)"


def bullet_colour(box, number=0):
    return box.page.evaluate(
        f"getComputedStyle(document.querySelectorAll('.mt-ask-requests li')[{number}], '::marker').color")


def test_each_bullet_has_its_status_colour(make_box):
    box = make_box(account=SIGNED_IN, label_requests=[
        {"name": "Arts", "status": "waiting", "reason": None},
        {"name": "Pole Recordings", "status": "added", "reason": None},
        {"name": "Wax", "status": "rejected", "reason": "Not a techno label."},
    ])
    box.page.get_by_role("list", name="Your label requests").locator("li").first.wait_for()

    assert [bullet_colour(box, number) for number in range(3)] == [YELLOW, GREEN, RED]


def loaded_requests(box, at_least):
    """How often the box fetched the visitor's requests, once it has done so at least this often."""
    for _ in range(50):
        count = len([sent for sent in box.api.requests if sent[:2] == ("GET", "/api/label-requests")])
        if count >= at_least:
            return count
        # A Playwright call, not time.sleep: the fake API only answers while one runs.
        box.page.wait_for_timeout(100)
    raise AssertionError(f"the requests were fetched {count} times, not {at_least}")


def test_a_label_request_needs_a_name(make_box):
    box = make_box(account=SIGNED_IN)
    box.page.get_by_role("button", name="Request it").click()
    box.page.get_by_role("button", name="Request", exact=True).click()

    assert box.page.locator(".mt-ask-request-form .mt-ask-status").inner_text() == "Please type the label's name."
    assert box.api.sent("/api/label-requests") == []


def test_the_request_limit_is_explained(make_box):
    box = make_box(account=SIGNED_IN)
    box.api.reply("/api/label-requests", 429, {"error": {"code": "request_limit_reached",
                                                         "message": "You can request 3 labels a day."}},
                  headers={"Retry-After": "7200"})
    box.page.get_by_role("button", name="Request it").click()
    box.page.get_by_role("textbox", name="Label name").fill("Arts")
    box.page.get_by_role("button", name="Request", exact=True).click()

    box.page.wait_for_function("document.querySelector('.mt-ask-request-form .mt-ask-status').classList.contains('mt-ask-status-error')")
    assert box.page.locator(".mt-ask-request-form .mt-ask-status").inner_text() == (
        "You can request 3 labels a day. You can ask again in about 2 hours.")


# --- Keyboard, labels, focus and announcements ----------------------------------------------

def test_the_box_can_be_used_with_the_keyboard_alone(box):
    # In the order they appear: the label request link under the label list, then the question.
    box.page.keyboard.press("Tab")
    assert box.focused() == "Sign up or sign in to request it"
    box.page.keyboard.press("Tab")
    assert box.focused() == "question"
    box.page.keyboard.type("Cheapest Tresor records?")
    box.page.keyboard.press("Tab")
    assert box.focused() == "Ask"
    box.page.keyboard.press("Enter")

    box.page.locator(".mt-ask-answer").wait_for()
    assert box.api.sent("/api/questions")[0]["question"] == "Cheapest Tresor records?"


def test_control_enter_sends_the_question(box):
    box.textarea.fill(EXAMPLE)
    box.textarea.press("Control+Enter")

    box.page.locator(".mt-ask-answer").wait_for()
    assert len(box.api.sent("/api/questions")) == 1


def test_an_example_question_can_be_chosen_with_the_keyboard(box):
    box.page.get_by_role("button", name=EXAMPLE).focus()
    box.page.keyboard.press("Enter")

    assert box.textarea.input_value() == EXAMPLE
    assert box.focused() == "question"


def test_focus_moves_to_the_answer_when_it_arrives(box):
    box.ask()
    box.page.locator(".mt-ask-answer").wait_for()

    assert box.page.evaluate("document.activeElement.textContent") == "Answer"
    assert box.page.get_by_role("region", name="Answer").is_visible()


def test_every_control_has_a_name_a_screen_reader_can_read(box):
    box.page.get_by_role("button", name="Sign up or sign in", exact=True).click()

    assert box.textarea.get_attribute("aria-describedby")
    for role, name in [("textbox", "Ask about the vinyl market"), ("button", "Ask"), ("textbox", "Email address"),
                       ("button", "Sign up"), ("button", "Already have an account? Sign in"),
                       ("button", "Forgot your password?")]:
        assert box.page.get_by_role(role, name=name, exact=True).count() == 1, (role, name)
    assert box.password.count() == 1


def test_changes_are_announced_politely(box):
    for status in (box.status, box.form_status):
        assert status.get_attribute("role") == "status"
        assert status.get_attribute("aria-live") == "polite"


@pytest.mark.parametrize("state", ["start", "answer", "account form", "error", "label request"])
def test_no_serious_accessibility_problems(make_box, state):
    from axe_playwright_python.sync_playwright import Axe

    box = make_box()
    if state == "answer":
        box.ask()
        box.page.locator(".mt-ask-answer").wait_for()
        box.page.get_by_text("Show the data").click()
    elif state == "account form":
        box.page.get_by_role("button", name="Sign up or sign in", exact=True).click()
    elif state == "label request":
        box = make_box(account=SIGNED_IN, label_requests=[{"name": "Arts", "status": "waiting"}])
        box.page.get_by_role("button", name="Request it").click()
        box.page.get_by_role("list", name="Your label requests").locator("li").first.wait_for()
    elif state == "error":
        box.api.reply("/api/questions", 503, {"error": {"code": "busy", "message": "The question service is busy."}})
        box.ask()
        box.page.wait_for_function("document.querySelector('.mt-ask > .mt-ask-status').textContent.length > 0")

    results = Axe().run(box.page, context=".mt-ask")
    problems = [f"{v['id']} ({v['impact']}): {v['help']}" for v in results.response["violations"]
                if v["impact"] in ("serious", "critical")]
    assert problems == []


def test_nothing_scrolls_sideways_on_a_phone(make_box):
    box = make_box(width=375)
    box.page.get_by_role("button", name="Sign up or sign in", exact=True).click()
    box.ask()
    box.page.locator(".mt-ask-answer").wait_for()

    assert box.page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


# --- End to end: the box against the real web API --------------------------------------------

def test_end_to_end_with_the_real_api_and_database(browser, site, limits_db, reader, db, monkeypatch):
    from werkzeug.serving import make_server

    from queries.questions import Answer
    from web import config
    from web.app import create_app

    db.execute("INSERT INTO tracked_labels (label_id, name) VALUES (1, 'Planet E'), (2, 'Wax (4)')")
    for label_id in (1, 2):  # the page lists a label once its releases are in
        db.execute("INSERT INTO labels (id, name) VALUES (%s, 'x')", (label_id,))
        db.execute("INSERT INTO releases (id, title, label_id, is_physical) VALUES (%s, 'x', %s, true)",
                   (label_id, label_id))
    db.commit()
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", [site.origin])
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    app = create_app(answer=lambda question: Answer(text=f"**{question}** is answered.", model="claude-haiku-5-5"),
                     verify_captcha=lambda token, secret, ip: token == "human-token")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    context = None
    try:
        context, page, errors = open_page(browser, site.page("e2e.html", f"http://127.0.0.1:{server.server_port}"))
        box = Box(page, None)
        page.locator(".mt-ask-labels:not([hidden])").wait_for()
        assert page.locator(".mt-ask-labels").inner_text() == "Labels we track (2): Planet E, Wax"
        assert box.account.inner_text().startswith(f"{config.QUESTIONS_PER_VISITOR_DAY} questions left today")

        box.ask("Cheapest Planet E record?")

        page.locator(".mt-ask-answer").wait_for()
        assert page.locator(".mt-ask-answer strong").inner_text() == "Cheapest Planet E record?"
        assert box.account.inner_text().startswith(f"{config.QUESTIONS_PER_VISITOR_DAY - 1} question")

        # Over the limit: the page reads how long to wait from the real API's answer.
        monkeypatch.setattr(config, "QUESTIONS_PER_VISITOR_HOUR", 1)
        box.ask("And Tresor?")
        page.wait_for_function("document.querySelector('.mt-ask > .mt-ask-status').classList.contains('mt-ask-status-error')")
        assert "You can ask again in about" in box.status.inner_text()
        assert errors == []
    finally:
        if context:
            context.close()
        server.shutdown()
