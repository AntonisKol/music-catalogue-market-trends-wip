import pytest
import requests
import responses
from responses import matchers

from ingestion import discogs_client
from ingestion.discogs_client import BASE_URL, get_label_releases, get_marketplace_stats, get_release


@pytest.fixture
def no_sleep(monkeypatch):
    """Record the pauses between label pages instead of waiting for them."""
    pauses = []
    monkeypatch.setattr(discogs_client.time, "sleep", pauses.append)
    return pauses


def label_page(page, pages, release_ids):
    return {
        "pagination": {"page": page, "pages": pages, "per_page": 100},
        "releases": [{"id": i, "title": f"Release {i}"} for i in release_ids],
    }


@responses.activate
def test_get_release_returns_the_release_json():
    responses.get(f"{BASE_URL}/releases/249504", json={"id": 249504, "title": "Never Gonna Give You Up"})

    assert get_release(249504) == {"id": 249504, "title": "Never Gonna Give You Up"}


@responses.activate
def test_requests_identify_the_app_and_send_the_token():
    responses.get(f"{BASE_URL}/releases/1", json={"id": 1})

    get_release(1)

    headers = responses.calls[0].request.headers
    assert headers["Authorization"] == "Discogs token=test-token"
    assert headers["User-Agent"] == "MusicCatalogueMarketTrendsTests/0.1"


@pytest.mark.parametrize("status", [401, 404, 429, 500])
@responses.activate
def test_get_release_raises_on_error_status(status):
    responses.get(f"{BASE_URL}/releases/1", json={"message": "error"}, status=status)

    with pytest.raises(requests.HTTPError) as error:
        get_release(1)
    assert error.value.response.status_code == status


@responses.activate
def test_get_release_raises_when_discogs_is_unreachable():
    responses.get(f"{BASE_URL}/releases/1", body=requests.ConnectionError("no route to host"))

    with pytest.raises(requests.ConnectionError):
        get_release(1)


@responses.activate
def test_get_label_releases_reads_every_page_in_order(no_sleep):
    for page, ids in [(1, [1, 2]), (2, [3, 4]), (3, [5])]:
        responses.get(
            f"{BASE_URL}/labels/7/releases",
            match=[matchers.query_param_matcher({"page": page, "per_page": 100})],
            json=label_page(page, 3, ids),
        )

    releases = get_label_releases(7)

    assert [r["id"] for r in releases] == [1, 2, 3, 4, 5]
    assert len(responses.calls) == 3
    assert no_sleep == [1.1, 1.1]


@responses.activate
def test_get_label_releases_single_page_does_not_pause(no_sleep):
    responses.get(f"{BASE_URL}/labels/7/releases", json=label_page(1, 1, [1]))

    assert [r["id"] for r in get_label_releases(7)] == [1]
    assert no_sleep == []


@responses.activate
def test_get_label_releases_with_no_releases(no_sleep):
    responses.get(f"{BASE_URL}/labels/7/releases", json=label_page(1, 0, []))

    assert get_label_releases(7) == []
    assert len(responses.calls) == 1


@responses.activate
def test_get_label_releases_stops_when_a_later_page_fails(no_sleep):
    responses.get(
        f"{BASE_URL}/labels/7/releases",
        match=[matchers.query_param_matcher({"page": 1, "per_page": 100})],
        json=label_page(1, 3, [1, 2]),
    )
    responses.get(
        f"{BASE_URL}/labels/7/releases",
        match=[matchers.query_param_matcher({"page": 2, "per_page": 100})],
        status=429,
    )

    with pytest.raises(requests.HTTPError):
        get_label_releases(7)
    assert len(responses.calls) == 2


@responses.activate
def test_get_marketplace_stats_asks_for_the_currency():
    stats = {"num_for_sale": 124, "lowest_price": {"value": 0.59, "currency": "EUR"}, "blocked_from_sale": False}
    responses.get(
        f"{BASE_URL}/marketplace/stats/249504",
        match=[matchers.query_param_matcher({"curr_abbr": "EUR"})],
        json=stats,
    )

    assert get_marketplace_stats(249504, "EUR") == stats


@responses.activate
def test_get_marketplace_stats_raises_for_unknown_release():
    responses.get(f"{BASE_URL}/marketplace/stats/1", json={"message": "Release not found."}, status=404)

    with pytest.raises(requests.HTTPError):
        get_marketplace_stats(1, "EUR")
