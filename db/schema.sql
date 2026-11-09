CREATE TABLE IF NOT EXISTS labels (
    id INT PRIMARY KEY,
    name TEXT NOT NULL,
    resource_url TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS artists (
    id INT PRIMARY KEY,
    name TEXT NOT NULL,
    resource_url TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS releases (
    id INT PRIMARY KEY,
    master_id INT,
    title TEXT NOT NULL,
    year INT,
    released DATE,
    country TEXT,
    label_id INT REFERENCES labels(id),
    catno TEXT,
    formats JSONB,
    genres TEXT [],
    styles TEXT [],
    is_physical BOOLEAN NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now ()
);
CREATE TABLE IF NOT EXISTS release_artists (
    release_id INT REFERENCES releases(id),
    artist_id INT REFERENCES artists(id),
    PRIMARY KEY (release_id, artist_id)
);
-- Every label a release was issued on, in any order; releases.label_id is only the
-- first one, which answers show. Co-releases often list a tracked label second (issue 94).
CREATE TABLE IF NOT EXISTS release_labels (
    release_id INT NOT NULL REFERENCES releases(id),
    label_id INT NOT NULL REFERENCES labels(id),
    catno TEXT,
    PRIMARY KEY (release_id, label_id)
);
CREATE INDEX IF NOT EXISTS release_labels_label_id ON release_labels (label_id);
-- Releases saved before the table existed: their first label is known, so it is
-- added here. Their other labels arrive with the weekly details refresh. Safe to run
-- again: a release whose first label is already listed is left as it is.
INSERT INTO release_labels (release_id, label_id, catno)
SELECT id, label_id, catno FROM releases WHERE label_id IS NOT NULL
ON CONFLICT (release_id, label_id) DO NOTHING;
CREATE TABLE IF NOT EXISTS price_snapshots (
    id BIGSERIAL PRIMARY KEY,
    release_id INT NOT NULL REFERENCES releases(id),
    snapshot_date DATE NOT NULL,
    num_for_sale INT,
    lowest_price NUMERIC(10, 2),
    currency TEXT,
    UNIQUE (release_id, snapshot_date)
);
CREATE TABLE IF NOT EXISTS demand_snapshots (
    id BIGSERIAL PRIMARY KEY,
    release_id INT NOT NULL REFERENCES releases(id),
    snapshot_date DATE NOT NULL,
    want INT,
    have INT,
    rating_count INT,
    rating_average NUMERIC(3, 2),
    UNIQUE (release_id, snapshot_date)
);
-- The labels the nightly run updates. added_by says whether the owner added one, approved a visitor's
-- request ('request') or ingestion/grow_labels.py added it ('nightly').
-- label_id is the Discogs label id; the labels row itself appears once the label is ingested.
CREATE TABLE IF NOT EXISTS tracked_labels (
    label_id INT PRIMARY KEY,
    name TEXT NOT NULL,
    added_by TEXT NOT NULL DEFAULT 'site',
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    active BOOLEAN NOT NULL DEFAULT true
);
-- Every label ingestion/grow_labels.py looked at, under the name Discogs' search gives it,
-- with what it found and why the label was added or left out.
CREATE TABLE IF NOT EXISTS label_candidates (
    name TEXT PRIMARY KEY,
    label_id INT,
    vinyl INT,
    techno INT,
    verdict TEXT NOT NULL CHECK (verdict IN ('added', 'not_techno', 'too_big', 'not_found', 'tracked',
                                             'rejected', 'no_room')),
    checked_at TIMESTAMPTZ NOT NULL
);

-- One row per nightly run, and one per label within it.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id BIGSERIAL PRIMARY KEY,
    snapshot_date DATE NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    message TEXT
);
-- Something to know that isn't this run's own failure, such as missed nights.
ALTER TABLE ingestion_runs ADD COLUMN IF NOT EXISTS warning TEXT;
CREATE TABLE IF NOT EXISTS ingestion_run_labels (
    run_id BIGINT NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
    label_id INT NOT NULL,
    listed INT NOT NULL DEFAULT 0,
    saved INT NOT NULL DEFAULT 0,
    not_vinyl INT NOT NULL DEFAULT 0,
    not_on_label INT NOT NULL DEFAULT 0,
    gone INT NOT NULL DEFAULT 0,
    failed INT NOT NULL DEFAULT 0,
    error TEXT,
    PRIMARY KEY (run_id, label_id)
);

-- Releases on a tracked label's list that turned out not to be vinyl, or only
-- credited to the label (for example as its studio). Nightly runs skip them
-- without asking Discogs, and check each again after about a month (issue 63).
CREATE TABLE IF NOT EXISTS skipped_releases (
    release_id INT NOT NULL,
    label_id INT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('not vinyl', 'not on label')),
    checked_on DATE NOT NULL,
    PRIMARY KEY (release_id, label_id)
);
