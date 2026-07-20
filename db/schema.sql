CREATE TABLE labels (
    id INT PRIMARY KEY,
    name TEXT NOT NULL,
    resource_url TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE artists (
    id INT PRIMARY KEY,
    name TEXT NOT NULL,
    resource_url TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE releases (
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
CREATE TABLE release_artists (
    release_id INT REFERENCES releases(id),
    artist_id INT REFERENCES artists(id),
    PRIMARY KEY (release_id, artist_id)
);
CREATE TABLE price_snapshots (
    id BIGSERIAL PRIMARY KEY,
    release_id INT NOT NULL REFERENCES releases(id),
    snapshot_date DATE NOT NULL,
    num_for_sale INT,
    lowest_price NUMERIC(10, 2),
    currency TEXT,
    UNIQUE (release_id, snapshot_date)
);
CREATE TABLE demand_snapshots (
    id BIGSERIAL PRIMARY KEY,
    release_id INT NOT NULL REFERENCES releases(id),
    snapshot_date DATE NOT NULL,
    want INT,
    have INT,
    rating_count INT,
    rating_average NUMERIC(3, 2),
    UNIQUE (release_id, snapshot_date)
);