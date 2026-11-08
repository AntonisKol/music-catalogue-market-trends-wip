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
-- The labels the nightly run updates. Visitors' requests (issue 37) will add to it.
-- label_id is the Discogs label id; the labels row itself appears once the label is ingested.
CREATE TABLE IF NOT EXISTS tracked_labels (
    label_id INT PRIMARY KEY,
    name TEXT NOT NULL,
    added_by TEXT NOT NULL DEFAULT 'site',
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    active BOOLEAN NOT NULL DEFAULT true
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
