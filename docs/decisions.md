# Decisions and actions

This file records what was decided while building the project, why, and how it shows up in the code. It is written for two kinds of readers:

- **Everyone** (recruiters, product people, anyone curious): read *The need*, *The choice* and *In plain words*. No coding knowledge is needed. A [glossary](#glossary) at the end explains the technical words.
- **Developers**: *In the code* shows where each decision lives and what it looks like.

Each task adds a section. Newest at the bottom.

---

## Task #2: Test the Discogs client and saving releases to PostgreSQL

*November 2026 · [issue #2](https://github.com/AntonisKol/music-catalogue-market-trends-wip/issues/2) · [pull request #16](https://github.com/AntonisKol/music-catalogue-market-trends-wip/pull/16)*

**Goal.** The app reads vinyl release data from Discogs (a large online music database and marketplace) and saves it in our own database. Before building more on top of that, we need automated checks that prove it works and keeps working: that it talks to Discogs correctly, copes when Discogs fails, and saves data without duplicates or half-finished writes.

**Result.** 45 automated tests that run in about 3 seconds, need no passwords or accounts, and never touch real data.

### Decision 1: How the tests pretend to be Discogs

**The need.** The tests must check our Discogs code without calling the real Discogs service. Real calls are slow, need a private access token, are rate-limited, and return data that changes over time.

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| Mock the call (`unittest.mock`) | Replace the one line of code that calls Discogs with a fake that hands back prepared data | Rejected |
| **`responses` library** | Let the code make a real web request, but intercept it before it leaves the computer and answer with prepared data | **Chosen** |
| Record and replay (`vcrpy`) | Call the real Discogs once, save the answers to files, replay them afterwards | Rejected |

**The choice.** `responses`. Mocking was the first pick, and it was changed after weighing the trade-offs.

**In plain words.** Think of checking how a new hire handles phone calls. Mocking only checks that they *reached for the phone correctly*. With `responses` they make a real call, which reaches a stand-in reading from a script, so you hear what they actually said and how they handled a busy line.

- With a mock, errors such as "not found" or "too many requests" are imitated by hand. The test can pass while the real error handling is broken.
- Mock tests check *how* the code calls Discogs, so they break when the code is tidied up even though nothing changed for Discogs.
- Record-and-replay needs the real token once, and the saved files can capture it. The task required "no live credentials".

**Risk avoided.** The daily data collection will meet Discogs outages and rate limits. That path is now tested for real, not imitated.

**In the code.** [`tests/test_discogs_client.py`](../tests/test_discogs_client.py). Each test registers the answer Discogs should give, then calls our real client code:

```python
@responses.activate
def test_requests_identify_the_app_and_send_the_token():
    responses.get(f"{BASE_URL}/releases/1", json={"id": 1})

    get_release(1)

    headers = responses.calls[0].request.headers
    assert headers["Authorization"] == "Discogs token=test-token"
```

Error cases use `status=404` (also 401, 429, 500) and check that a real `requests.HTTPError` is raised. Pagination tests answer page 1, 2 and 3 separately, using `matchers.query_param_matcher`. The one-second pause between pages is swapped for a recorder (`monkeypatch.setattr(discogs_client.time, "sleep", pauses.append)`), so the test checks the pauses happen without waiting for them.

### Decision 2: How the tests get their settings

**The need.** The app reads its settings (Discogs token, database address and password) when it starts, and **stops immediately if one is missing**. A fresh copy of the project has no settings file, so the tests could not even start.

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| Loosen the app's settings code | Let the app carry on with empty values when a setting is missing | Rejected |
| Commit a `.env.test` file | Store fake settings in a file in the repository | Rejected |
| **Fake values in `tests/conftest.py`** | The tests set obviously fake settings themselves, before any app code loads | **Chosen** |

**The choice.** Fake values set by the tests. Loosening the app was the first pick, and it was changed after weighing the trade-offs.

**In plain words.** A pre-flight checklist refuses take-off without fuel. Loosening the settings code would mean deleting that line because it is awkward in the flight simulator: the simulator gets easier, but a real plane could take off empty. In practice a typo in the real settings would no longer stop the app at once. It would start, then fail hours later in the nightly run with confusing errors.

A committed `.env.test` was also rejected. The app also reads the developer's real settings file, so real values could still slip into a test run, and two settings files tend to drift apart.

**Risk avoided.** A misconfigured deployment failing silently in an overnight job, and a real token being used or exposed by tests.

**In the code.** [`tests/conftest.py`](../tests/conftest.py). pytest loads this file before any test, so the fake values are in place before [`ingestion/config.py`](../ingestion/config.py) is imported. `config.py` itself is unchanged and stays strict.

```python
FAKE_SETTINGS = {
    "DISCOGS_TOKEN": "test-token",
    "DISCOGS_USER_AGENT": "MusicCatalogueMarketTrendsTests/0.1",
    ...
}

os.environ.update(FAKE_SETTINGS)
```

`load_dotenv()` in `config.py` never overwrites variables that are already set, so the real `.env` cannot win. This was checked in a copy that has a real `.env`.

### Decision 3: Which database the tests use

**The need.** Saving data has to be tested against a real PostgreSQL database with our exact table design, but never against a database that holds real data.

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| The developer's existing database | Run tests against the local database and delete test rows afterwards | Rejected |
| A second database in Docker Compose | A test-only database the developer starts by hand before testing | Rejected |
| **Testcontainers** | The tests start a brand-new database themselves and delete it when they finish | **Chosen** |

**The choice.** Testcontainers.

**In plain words.** Every exam gets a fresh, blank answer sheet that is shredded afterwards, so nobody can scribble on the master copy.

- Using the real database is genuinely risky. A test that crashes halfway skips its clean-up and leaves junk behind, and a clean-up bug could delete real data.
- A hand-started test database works, but people forget to start it and then get confusing errors. It also collects leftovers between runs.
- Testcontainers needs Docker running and adds about 2 seconds per run. That is a small price for a database that is always fresh and always the same version as the real one (PostgreSQL 16).

**Risk avoided.** Tests damaging or depending on real data.

**In the code.** The `postgres_url` fixture in [`tests/conftest.py`](../tests/conftest.py). It starts once per test run and loads [`db/schema.sql`](../db/schema.sql):

```python
with PostgresContainer("postgres:16", driver=None) as container:
    url = container.get_connection_url()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SCHEMA.read_text())
    yield url
```

The container gets a free port of its own, so it never collides with the developer's database on port 5432.

### Decision 4: Keeping tests from affecting each other

**The need.** Every database test must start from empty tables, or one test's leftovers could make another pass or fail by accident.

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| A new database for every test | Start and delete a container per test | Rejected |
| Undo every test (roll back) | One database; each test runs inside a transaction that is cancelled at the end | Rejected |
| **Empty the tables before each test** | One database; tables are emptied first, then the test's saves are committed for real | **Chosen** |

**The choice.** Empty the tables before each test.

**In plain words.** Think of testing a cash register. Rolling back is like ringing every sale up in training mode, where nothing is ever really recorded. Emptying the tables is like starting each test with an empty till and ringing sales up for real.

- Rolling back is a popular pattern, but it does not fit this code. Our save function commits its own transaction, and inside a test's transaction that commit would never really happen. The test could not show that a failed save leaves nothing behind for other users of the database. It also could not show that saving again refreshes the "last fetched" time, because the database clock stands still within one transaction.
- A new database per test would take about 2 seconds each. Slow tests stop being run.

**Risk avoided.** Tests that pass in "training mode" while the real save behaves differently.

**In the code.** The `db` fixture in [`tests/conftest.py`](../tests/conftest.py):

```python
with psycopg.connect(postgres_url, autocommit=True) as admin:
    admin.execute(f"TRUNCATE {TABLES} RESTART IDENTITY")
with psycopg.connect(postgres_url) as conn:
    yield conn
```

The tests then check results through a **separate** connection (the `committed` fixture in [`tests/test_repository.py`](../tests/test_repository.py)), so they only see what was really saved. *Maintenance note:* `TABLES` lists the tables by hand. A table added by a later task must be added there too.

### Decision 5: What happens when Docker is not running

**The need.** The database tests need Docker; the other tests do not. Someone may run the tests without Docker open.

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| Fail loudly | Database tests report errors | Rejected |
| Only run them with a flag | A plain `pytest` never runs them | Rejected |
| **Skip with a reason, but fail in CI** | On a laptop they are marked "skipped: Docker is not running"; on an automated build server they fail | **Chosen** |

**The choice.** Skip on a laptop, fail in CI. Plain "skip" was picked first, and the CI rule was added afterwards.

**In plain words.**
- Failing loudly punishes someone who changed something unrelated. They see red, learn to ignore red, and that is worse.
- A flag means the most important tests quietly never run, because everyone types the plain command.
- A skip states honestly what did not run. But a skip also counts as a pass, so on a future automated build server (CI) a broken Docker setup could skip every database test and still show a green tick. Build servers announce themselves with `CI=true`, so there the tests fail instead.

**Risk avoided.** A green build that silently tested nothing about the database.

**In the code.** [`tests/conftest.py`](../tests/conftest.py). Only "Docker is unreachable" leads to a skip; any other problem, such as a broken schema, still fails:

```python
if not docker_is_running():
    message = "Docker is not running, so the database tests cannot start PostgreSQL"
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)
```

### What the tests check

| File | What it proves |
| --- | --- |
| [`tests/test_discogs_client.py`](../tests/test_discogs_client.py) | Requests carry the token and the app's name; errors (401, 404, 429, 500, no connection) are raised, not hidden; all pages of a label are read in order, with a pause between pages; a failing later page stops the read |
| [`tests/test_mapping.py`](../tests/test_mapping.py) (from task #5) | Discogs data is turned into the right label, artist and release rows |
| [`tests/test_repository.py`](../tests/test_repository.py) | A release, its label and artists are saved; **saving again updates instead of duplicating**; artists no longer credited are unlinked without touching other releases; **a failed save keeps nothing**, and a failed re-save keeps the earlier version; the connection still works after a failure |
| [`tests/test_save_release.py`](../tests/test_save_release.py) | The `save_release` command prints the saved release, and gives a clear message and exit status for wrong input, a Discogs failure, an unreachable database and unusable data |

### How we proved the tests work

A test that can never fail is worthless. So bugs were planted in the real code one at a time, and the tests had to catch each one (a technique called *mutation testing*):

| Planted bug | Tests that failed |
| --- | --- |
| Save not wrapped in a transaction | 7 |
| Errors from Discogs not raised | 5 |
| Old artist links never removed | 2 |
| Title not updated on re-save | 1 |
| "Last fetched" time not refreshed | 1 |
| Artist clean-up not limited to one release | 1 |
| No pause between Discogs pages | 1 |

Every planted bug was caught. The code was restored afterwards.

### Other actions

- **Dependencies.** Added `responses`, `testcontainers` and their dependencies (`docker`, `PyYAML`, `wrapt`) to [`requirements.txt`](../requirements.txt), each pinned to an exact version so every machine installs the same thing.
- **README.** Added "Run the tests" and "Save one release" sections, marked tasks #5 and #2 as done, and removed the outdated sentence saying the app does not write to PostgreSQL.
- **Docker on the development Mac (Colima).** During the work, the virtual machine that runs Docker had disk write errors and switched its storage to read-only, so no container could start. A restart of Colima repaired it. The local database container still pointed to the project's old Desktop folder, so it was recreated from the current folder with `docker compose up -d`. It keeps the same data volume, and no data was lost (that database had been empty since 27 October). A leftover test container from an earlier session was removed.
- **Colima detail for developers.** Testcontainers' clean-up helper mounts the Docker socket, and with Colima the Mac-side path does not exist inside the VM. `conftest.py` therefore sets `TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock` unless it is already set. This path also works with Docker Desktop and on Linux.
- **Scope.** Tests for the daily price and demand snapshots belong to task #3, together with that feature.

---

## Task #3: Capture daily price and demand snapshots

*November 2026 · [issue #3](https://github.com/AntonisKol/music-catalogue-market-trends-wip/issues/3)*

**Goal.** Once a day, record for each release how the market looks: the cheapest copy on sale and how many copies are for sale (**price and supply**), and how many people want it, own it, and how they rate it (**demand**). Over time these daily rows become the price and demand history that the question feature will answer from.

**Result.** A `save_snapshot` command and the code behind it, tested with 23 new tests (68 in total), and checked live against Discogs.

**How the facts were established.** The Discogs documentation page refuses automated reading, so the endpoints were checked by calling the live API with the project's own client (read-only, a dozen requests). Findings for release 249504 (Rick Astley, "Never Gonna Give You Up"):

| Endpoint | What it returned |
| --- | --- |
| `/releases/{id}` | `num_for_sale: 124`, `lowest_price: 0.59` (a bare number with no currency, unchanged when another currency is requested), and `community`: `want 594`, `have 4159`, `rating 3.88 from 249 votes` |
| `/marketplace/stats/{id}` | `num_for_sale: 124`, `lowest_price: {value: 0.59, currency: "EUR"}`; with `curr_abbr=USD`: `0.67 USD` |
| `/releases/{id}/rating` | only the rating |
| `/marketplace/price_suggestions/{id}` | suggested selling prices per condition, from the account's seller settings |
| A release with nothing for sale (30000001) | `num_for_sale: 0`, `lowest_price: null`; rating `count 0, average 0.0` |
| An unknown release | 404, "Release not found." |

Discogs allows 60 requests a minute for an authenticated app.

### Decision 1: Where the numbers come from

**The need.** Each daily snapshot needs price, supply, want, have and rating, from as few requests as possible (the 60-a-minute limit decides how long a daily run takes), and with prices whose currency is known.

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| Release only | One request: `/releases/{id}` for everything | Rejected |
| **Stats + release** | `/marketplace/stats/{id}` for price and supply; `/releases/{id}` for want, have and rating | **Chosen** |
| Stats, rating, suggestions | Three dedicated endpoints | Rejected |

**The choice.** Stats + release. The three-endpoint option was picked first, and it was changed after checking what each endpoint actually returns.

**In plain words.** You want a shop's cheapest price and how many people are queuing outside. The three-endpoint option asks the cashier, a reviewer and a sales advisor, but nobody counts the queue: the rating endpoint has no want or have numbers, so the demand table's two key columns would stay empty. Price suggestions are advice to sellers, not prices anyone is asking, and storing them next to real prices would mislead. It is also the slowest: about 50 minutes per 1,000 releases, against 33 for two requests.

"Release only" is the fastest, but its price is a bare number in whatever currency the account that runs the job uses, with nothing saying which. The statistics endpoint states the currency. Because the full pipeline (#6) fetches `/releases/{id}` for the catalogue anyway, the demand numbers come free there, and the real cost is one extra request per release.

**Risk avoided.** An empty demand history, and prices of unknown currency.

**In the code.** [`ingestion/discogs_client.py`](../ingestion/discogs_client.py) gets a second request function:

```python
def get_marketplace_stats(release_id: int, currency: str) -> dict:
    response = session.get(
        f"{BASE_URL}/marketplace/stats/{release_id}",
        params={"curr_abbr": currency},
    )
    response.raise_for_status()
    return response.json()
```

[`ingestion/mapping.py`](../ingestion/mapping.py) turns its answer into a `price_snapshots` row (`map_price_snapshot`), and the release's `community` block into a `demand_snapshots` row (`map_demand_snapshot`).

### Decision 2: Which currency prices are stored in

**The need.** Prices must be comparable from one day to the next.

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| Account default | Store whatever currency the running account uses, with its code | Rejected |
| **Fixed currency, asked every time** | Always request EUR | **Chosen** |
| Convert later | Store the account currency; convert with exchange rates when querying | Rejected |

**The choice.** Always ask for EUR. The account default was picked first, and it was changed after weighing the trade-offs.

**In plain words.** Picture a weather station that records in whatever unit the person on duty prefers. Each reading is labelled, but the yearly chart still mixes Celsius and Fahrenheit. With the account default, the unit depends on whose Discogs account runs the job. A changed setting, the hosted version running under another account, or a contributor using their own token would switch prices to dollars from that day on. "This record got 12% more expensive" would then be wrong unless every query filtered by currency, and the question feature (#7) could compare euros with dollars without noticing. Converting later needs a historical exchange-rate source, a new dependency doing work Discogs already does for us.

**Risk avoided.** A price history that silently mixes currencies.

**In the code.** One constant in [`ingestion/config.py`](../ingestion/config.py), passed to every statistics request. The currency code Discogs returns is still stored, so each row says what it is:

```python
SNAPSHOT_CURRENCY = "EUR"
```

### Decision 3: Storing "nothing for sale" and "nobody rated it"

**The need.** Many releases have no copies for sale or no ratings, and Discogs reports these as `lowest_price: null` and `rating average 0.0`.

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| Store as sent | Price empty, rating average 0.0 | Rejected |
| **No data = NULL** | Keep real counts (0 for sale, 0 wants), but store "unknown" where there is nothing to measure | **Chosen** |
| Skip the snapshot | Store no row that day when price or rating is missing | Rejected |

**The choice.** No data = NULL.

**In plain words.** A restaurant guide. "0 tables free" is a fact worth recording, but a restaurant with no reviews is *not rated yet*, not "rated 0 stars". Storing 0.0 would make every unrated record look like the worst-rated one and pull down any average, and the question feature would repeat that error confidently. Skipping the day throws away real facts: "nothing for sale" is itself a market signal (scarcity), want and have still exist, and gaps make trend lines unreliable.

**Risk avoided.** Wrong averages and lost scarcity signals.

**In the code.** [`ingestion/mapping.py`](../ingestion/mapping.py):

```python
"lowest_price": Decimal(str(value)) if value is not None else None,
"currency": lowest.get("currency") if value is not None else None,
...
"rating_average": Decimal(str(average)) if rating_count and average is not None else None,
```

Prices go through `Decimal(str(...))`, so `0.59` is stored as exactly 0.59 rather than the nearest binary fraction.

### Decision 4: Running twice on the same day

**The need.** The tables allow one snapshot per release per day. A retry after a crash, or a manual rerun, must not fail or duplicate.

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| **Update to latest** | Overwrite the day's row with the new values, in one step | **Chosen** |
| Keep the first | Ignore the new values | Rejected |
| Delete, then insert | Remove the day's row, then add a new one | Rejected |

**The choice.** Update to latest.

**In plain words.** Reruns usually happen *because* something went wrong: a bug fixed mid-morning, or a run cut short by the rate limit. "Keep the first" would lock the bad values in until someone edits the database by hand. "Delete, then insert" reaches the same result in two steps, and if the second step fails, the day's row is simply gone. One atomic update is safe to repeat, and it matches how releases are already saved.

**Risk avoided.** Stale or lost daily data after a retry.

**In the code.** [`ingestion/repository.py`](../ingestion/repository.py). Both rows are written in one transaction, separate from the catalogue save, so a failed snapshot keeps neither row and never undoes the saved release:

```sql
INSERT INTO price_snapshots (release_id, snapshot_date, num_for_sale, lowest_price, currency)
VALUES (...)
ON CONFLICT (release_id, snapshot_date) DO UPDATE SET
    num_for_sale = EXCLUDED.num_for_sale, ...
```

### Decision 5: What "today" means

**The need.** "One snapshot per day" depends on which day it is, and the job may run on a Mac in Berlin now and on a server later.

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| Database's date | PostgreSQL fills in `CURRENT_DATE` | Rejected |
| Local date | The date on the computer running the job | Rejected |
| **UTC, passed in** | Python works out today's date in UTC and passes it to the save function | **Chosen** |

**The choice.** UTC, passed in.

**In plain words.** Airlines run every schedule on one clock (UTC), whichever city they are in, or a 23:30 departure would be logged on two different days. On local time, a run at 00:30 in Berlin is still yesterday on a UTC server, so moving the job would shift or double a day, and summer time adds a further wrinkle. The database's date depends on a server setting outside our code, and tests cannot control it. Passing the date in keeps one visible definition, and tests simply hand over a fixed date.

**Risk avoided.** Missing or doubled days in the history, and tests that fail around midnight.

**In the code.** [`ingestion/save_snapshot.py`](../ingestion/save_snapshot.py):

```python
snapshot_date = datetime.now(timezone.utc).date()
```

`save_snapshots(conn, release_id, stats, release, snapshot_date)` takes the date as a parameter. One test fixes the clock at 23:30 UTC on 1 November, which is already 2 November in Berlin, and checks that the snapshot is dated 1 November.

### What the tests check

| File | What it proves |
| --- | --- |
| [`tests/test_discogs_client.py`](../tests/test_discogs_client.py) | The statistics request always asks for the currency; an unknown release raises an error |
| [`tests/test_mapping.py`](../tests/test_mapping.py) | Price and demand rows are mapped exactly; nothing for sale keeps the count 0 with no price or currency; no ratings gives no average; a missing community block gives unknown values |
| [`tests/test_snapshots.py`](../tests/test_snapshots.py) | Both rows are saved; **the same day again replaces that day's values**; each day gets its own row; a release not in the catalogue is refused; **a failed snapshot keeps neither row and leaves the catalogue intact**; a failed same-day resave keeps the earlier values |
| [`tests/test_save_snapshot.py`](../tests/test_save_snapshot.py) | The command saves the release and today's snapshot and prints it; prints "0 for sale" and "not rated" correctly; uses the UTC day; Discogs failure saves nothing; an unreachable database and a failed snapshot give clear messages, and a failed snapshot still keeps the saved release |

### How we proved the tests work

| Planted bug | Tests that failed |
| --- | --- |
| Snapshot not wrapped in a transaction | 5 |
| Currency not requested | 5 |
| No-ratings stored as 0.0 | 3 |
| Release not saved before its snapshot | 3 |
| Same day keeps the first values | 1 |
| Local date instead of UTC | 1 |
| Price converted through a float | 1 |
| Currency stored without a price | 1 (after a test was added for it) |

The last one survived at first: no test had covered Discogs sending a currency without a value, so a test was added.

### Live check

`python -m ingestion.save_snapshot` was run against the real Discogs API and a throwaway database: 249504 gave "124 for sale from 0.59 EUR; want 594, have 4159; rated 3.88 by 249"; 30000001 gave "0 for sale; want 0, have 1; not rated"; a second run of 249504 the same day still left one row; an unknown release gave a clear 404 message and exit status 1.

### Other actions

- Removed a stray `...` line left in `ingestion/discogs_client.py`.
- README: marked #3 done, unblocked #6, documented `save_snapshot`.

---

## Task #6: Ingest a whole Discogs label

*November 2026 · [issue #6](https://github.com/AntonisKol/music-catalogue-market-trends-wip/issues/6)*

**Goal.** One command that takes a record label and does everything from the earlier tasks for each of its releases: fetch the details, save the release, and record today's price and demand snapshot. It must be safe to rerun, polite to Discogs, and honest about anything that went wrong.

**Result.** `python -m ingestion.ingest_label <label_id>`, with 39 new tests (107 in total), checked live on a real label.

**What the real data showed first.** A live look at label 1 (Planet E) shaped the decisions:

- The label list has 597 entries but only **511 unique releases**. Some releases are listed twice, identically.
- Its format text is loose and abbreviated (`12"`, `2xLP`, `CD, Comp`, `14xFile, AIFF, Album, 24-`). About 323 releases look like vinyl and 188 like CD, digital or cassette. The exact format names, such as "Vinyl", come only with each release's full details.
- At 60 requests a minute and about 2 requests per vinyl release, a label of this size takes roughly 15 minutes.

### Decision 1: What happens when one release fails

**The need.** In a run of hundreds of releases, one may fail: Discogs returns an error, or a database write is refused. The issue requires failures to be visible and never silently skipped.

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| Stop the run | Stop at the first failure; rerun once fixed | Rejected |
| Retry, then skip | Try up to 3 times, then move on so the run always completes | Rejected as proposed |
| **Retry temporary errors, continue, report, exit 1** | Retry only what can recover; record the rest, carry on, list every failure at the end, and exit with status 1 | **Chosen** |

**The choice.** Retry temporary errors only, then report. "Retry, then skip" was picked first, and it was refined after debate: the retry idea stayed, the silent skip went.

**In plain words.** A delivery driver who can't deliver one parcel. *Stopping the run* drives the whole van back to the depot, so today's data for the other 510 releases is lost because of one. *Retry, then skip* knocks three times and quietly takes the parcel home, and the nightly job reports success while data is missing. It also retries things that can never work: a deleted release (404) or a database rule fails the same way every time. The good driver rings again if nobody answered (a temporary error), delivers the rest of the round, and hands in a list of undelivered parcels at the end.

**Risk avoided.** Missing data hidden behind a "successful" run, and one bad release costing a whole day's data.

**In the code.** [`ingestion/pipeline.py`](../ingestion/pipeline.py) records each failure and carries on:

```python
try:
    saved = ingest_release(conn, release_id, snapshot_date)
except (requests.RequestException, MappingError, psycopg.Error) as error:
    run.failed[release_id] = str(error)
    log.error("%s failed: %s", step, error)
    if conn.broken:
        raise DatabaseConnectionLost(str(error)) from error
    continue
```

[`ingestion/ingest_label.py`](../ingestion/ingest_label.py) lists every failed release at the end and returns `0 if run.ok else 1`. One deliberate exception: if the database connection itself is lost, every later save would fail too, so the run stops at once rather than logging hundreds of identical errors.

### Decision 2: Staying under Discogs' rate limit

**The need.** Discogs allows 60 requests a minute and answers "429 Too Many Requests" beyond that.

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| **Fixed pause (+ back-off on 429)** | Keep requests at least 1.1 s apart; if a 429 still happens, wait and retry | **Chosen** |
| Read the headers (+ back-off) | Full speed while many requests remain, slow down when the "remaining" count is low | Rejected |
| Back off on 429 only | No pacing; wait and retry whenever Discogs says "too many" | Rejected |

**The choice.** A fixed pause, with back-off kept as a safety net. Back-off only was picked first, and it was changed after debate.

**In plain words.** Driving with no speedometer and using speed cameras as the speed limit: you arrive, but every flash is a fine, and enough fines cost the licence. Back-off only would hit Discogs' limit about once a minute for a whole run. That wastes every rejected request, makes releases likelier to end up on the failure list, and risks the token or home connection being throttled, which could also hit other apps sharing it. Reading the headers is faster for small runs, but it is more logic to get right, and an overnight job gains little from speed. A fixed pause is simple, predictable and always polite, and the earlier label paging already worked that way.

**Risk avoided.** Being throttled or blocked by Discogs, and needless failures.

**In the code.** Every request now goes through one helper in [`ingestion/discogs_client.py`](../ingestion/discogs_client.py), which paces it and retries only temporary failures:

```python
MIN_INTERVAL = 1.1
RETRY_WAITS = (5, 15, 60)
TEMPORARY_STATUSES = {429, 500, 502, 503, 504}
```

Dropped connections and timeouts are retried too; anything else (401, 404, ...) is raised at once. After the last wait a request has waited 80 seconds in total, longer than Discogs' 60-second window. Requests also time out after 30 seconds, so a hung connection can no longer stall a run forever.

### Decision 3: Vinyl only, and how to tell

**The need.** The project tracks the vinyl market, but labels also release CDs, files and cassettes.

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| Everything | Save and snapshot every release | Rejected |
| Filter on the list text | Fetch details only for entries whose text looks like vinyl | Refined |
| Filter on details | Fetch every release's details, keep those with "Vinyl" | Rejected |
| **Refined list filter** | Skip only entries that are *clearly* not vinyl; let the details decide everything else | **Chosen** |

**The choice.** The list-text filter was picked, and checked against Planet E's real data: it held up well. Two safeguards were then added. Combinations (any `+`) and anything unclear are never skipped from the text alone, and every kept release must show "Vinyl" in its exact formats, which costs nothing because the details are fetched anyway.

**In plain words.** Sorting post by the envelope: bin the obvious junk mail unopened, and open anything you're unsure about. Saving everything would mix CD and download prices into a "vinyl" market history. Fetching every release's details first is accurate but spends about 188 requests (3.5 minutes) per run on CDs. The plain text rule is cheap, but the text is abbreviated and loose: a "CD + 2×LP" set could be skipped, and nobody would ever notice the missing record.

**Risk avoided.** Non-vinyl prices polluting the data, and vinyl releases silently missing.

**In the code.** [`ingestion/mapping.py`](../ingestion/mapping.py):

```python
def is_clearly_not_vinyl(summary_format):
    if not summary_format or "+" in summary_format:
        return False
    first = summary_format.split(",")[0].strip()
    return re.sub(r"^\d+x", "", first) in NON_VINYL_SUMMARY_FORMATS

def is_vinyl(formats):
    return any(f.get("name") == "Vinyl" for f in formats)
```

`NON_VINYL_SUMMARY_FORMATS` lists CD, CDr, SACD, File, Cass, VHS, DVD, DVDr, Blu-ray, Minidisc and Floppy Disk. Only the format named exactly "Vinyl" counts; Discogs' separate formats such as lathe cuts and flexi-discs are not included. The run's summary reports how many releases were skipped as not vinyl, so the filter's effect stays visible.

### Decision 4: How the run reports progress

**The need.** A run takes 15 to 20 minutes and will later run unattended every night (#10).

**Options considered**

| Option | What it means | Verdict |
| --- | --- | --- |
| print() per release | A plain line per release | Rejected |
| **Logging, timestamped** | Python's logging: time, level (INFO, WARNING, ERROR) and message per line | **Chosen** |
| Summary only | Silent until one summary at the end | Rejected |

**The choice.** Timestamped logging.

**In plain words.** A ship's log rather than shouting across the deck. When something goes wrong at 03:12, the log shows when each step happened and how serious it was. Plain prints have no times or levels, so "show me only the errors" is impossible. A silent run can't be told apart from a stuck one, and if it crashes halfway there is no summary at all.

**Risk avoided.** Problems in the nightly run that nobody can diagnose afterwards.

**In the code.** The command sets up the format once:

```python
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
```

A real run looks like this:

```text
2026-11-01 18:58:41,355 INFO Label 800000: 11 releases listed
2026-11-01 18:58:43,388 INFO [1/11] 1832937 saved: 38 for sale from 2.99 EUR; want 39, have 328; rated 2.54 by 35
2026-11-01 18:58:43,388 INFO [2/11] 1844080 skipped, not vinyl (CD, Album, Comp)
...
2026-11-01 18:59:01,003 INFO Label 800000 done: 11 listed, 9 saved, 2 not vinyl, 0 failed
```

Retries are logged as WARNING, and failed releases as ERROR.

### A bug found before it shipped

While the pipeline was being written, a review of the code turned up a real problem. The database library opens a transaction for every read, and the read-back after each saved release left one open. Every *later* save in the run would then have become a savepoint inside it, committed only when the run ended. A crash at release 400 would have lost the 399 saved before it. The fix is to close the read in its own transaction. A test now proves that while release 3 is being fetched, releases 1 and 2 are already visible to another database connection:

```python
with conn.transaction():
    return get_saved_snapshot(conn, release_id, snapshot_date)
```

### What the tests check

| File | What it proves |
| --- | --- |
| [`tests/test_discogs_client.py`](../tests/test_discogs_client.py) | Permanent errors are raised at once; 429 and 5xx are retried with the right waits, then succeed or are raised; dropped connections are retried; requests are paced across all endpoints; requests have a timeout |
| [`tests/test_pipeline.py`](../tests/test_pipeline.py) | The vinyl checks; duplicates are ingested once; clearly non-vinyl entries cost no request; combinations and unclear entries are decided by their details; a failed release is recorded while the rest are saved; a temporary error is retried within the run; **each release is committed as soon as it is saved**; a database error on one release does not stop the others; a lost connection stops the run; a failing label list is raised; running twice the same day adds nothing |
| [`tests/test_ingest_label.py`](../tests/test_ingest_label.py) | Exit 0 with a summary when all is well; failed releases listed and exit 1; clear messages and exit 1 when the label list fails, the database is unreachable or the connection is lost; usage errors exit 2 |

All tests share a fake clock (the `discogs_clock` fixture in [`tests/conftest.py`](../tests/conftest.py)), so pauses and retry waits are recorded, not waited, and the suite still runs in about 6 seconds.

### How we proved the tests work

| Planted bug | Caught |
| --- | --- |
| Read left open, so saves are committed only at the end | Yes |
| Run stops at the first failed release | Yes |
| Failures dropped from the result | Yes |
| Run carries on after the database connection is lost | Yes |
| No de-duplication of the label list | Yes |
| No "Vinyl" check on the details | Yes |
| Combinations (`+`) skipped from the text | Yes, after adding a test (`CD, Album + 2xLP`) |
| 404 retried as if temporary | Yes |
| No pacing between requests | Yes |
| Dropped connections not retried | Yes |
| Exit 0 despite failures | Yes |

### Live check

`python -m ingestion.ingest_label 800000` was run against the real Discogs API and a throwaway database. The label's 12 entries were 11 unique releases: 9 vinyl releases were saved with snapshots, and 2 CDs were skipped from the list text without a request. There were 0 failures and exit status 0, at about 2.2 seconds per vinyl release. A second run the same day still left exactly 9 rows in each table.

### Other actions

- README: documented `ingest_label`, marked #6 done, and unblocked #10 and #11.
- The single-release snapshot command now reuses the pipeline's snapshot description, so both commands print the same text.

---

## Glossary

- **API**: a way for programs to talk to each other. Discogs' API is how our code asks Discogs for release data.
- **Token**: a private password-like key that identifies us to Discogs. It must never be shared or committed.
- **Database / PostgreSQL**: where the app stores its data in tables. PostgreSQL is the database product we use.
- **Upsert**: "insert, or update if it already exists". It is what makes saving the same release twice safe.
- **Transaction**: a group of database changes that either all happen or none do, so a failure halfway leaves nothing half-saved.
- **Docker / container**: a way to run software, such as a database, in a sealed box that can be created and thrown away in seconds. Colima is the program that runs Docker on this Mac.
- **Mock / fake**: a stand-in for a real service, used in tests.
- **Fixture**: a piece of setup that tests share, such as "an empty database".
- **CI (continuous integration)**: a server that runs all tests automatically on every change, before it is accepted.
- **Mutation testing**: deliberately planting bugs to check that the tests notice them.
- **Endpoint**: one specific address of an API that answers one kind of question, such as "market statistics for this release".
- **Rate limit**: how many requests a service accepts per minute; Discogs allows 60.
- **NULL**: the database's way of saying "unknown" or "nothing to measure", which is different from zero.
- **UTC**: the world's reference clock, with no time zones or summer time.
- **Idempotent**: safe to run again; a second run gives the same result instead of duplicates.
- **Retry / back-off**: trying a failed request again after waiting, and waiting longer each time.
- **Exit status**: the number a command reports when it ends; 0 means success, anything else tells a scheduler that something went wrong.
- **Logging**: writing timestamped, graded messages (INFO, WARNING, ERROR) about what a program is doing, so they can be read and filtered later.
- **Savepoint**: a checkpoint inside an open database transaction; nothing in it is permanent until the outer transaction is committed.
