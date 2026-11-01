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
