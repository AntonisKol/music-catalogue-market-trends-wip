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
