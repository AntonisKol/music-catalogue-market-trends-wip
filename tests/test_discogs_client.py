import pytest
import requests
import responses
from responses import matchers

from ingestion.discogs_client import (
    BASE_URL,
    MIN_INTERVAL,
    RETRY_WAITS,
    get_label_releases,
    get_marketplace_stats,
    get_release,
)


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


@pytest.mark.parametrize("status", [401, 403, 404])
@responses.activate
def test_permanent_error_is_raised_without_retrying(status, discogs_clock):
    responses.get(f"{BASE_URL}/releases/1", json={"message": "error"}, status=status)

    with pytest.raises(requests.HTTPError) as error:
        get_release(1)
    assert error.value.response.status_code == status
    assert len(responses.calls) == 1
    assert discogs_clock == []


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@responses.activate
def test_temporary_error_is_retried_then_succeeds(status, discogs_clock):
    responses.get(f"{BASE_URL}/releases/1", status=status)
    responses.get(f"{BASE_URL}/releases/1", status=status)
    responses.get(f"{BASE_URL}/releases/1", json={"id": 1})

    assert get_release(1) == {"id": 1}
    assert len(responses.calls) == 3
    # Each retry waits its back-off; the pacing pause before it is already covered.
    assert discogs_clock == [RETRY_WAITS[0], RETRY_WAITS[1]]


@responses.activate
def test_temporary_error_that_never_clears_is_raised_after_the_retries(discogs_clock):
    responses.get(f"{BASE_URL}/releases/1", json={"message": "slow down"}, status=429)

    with pytest.raises(requests.HTTPError) as error:
        get_release(1)
    assert error.value.response.status_code == 429
    assert len(responses.calls) == len(RETRY_WAITS) + 1
    assert discogs_clock == list(RETRY_WAITS)


@responses.activate
def test_unreachable_discogs_is_retried_then_raised(discogs_clock):
    responses.get(f"{BASE_URL}/releases/1", body=requests.ConnectionError("no route to host"))

    with pytest.raises(requests.ConnectionError):
        get_release(1)
    assert len(responses.calls) == len(RETRY_WAITS) + 1


@responses.activate
def test_dropped_connection_recovers_on_retry():
    responses.get(f"{BASE_URL}/releases/1", body=requests.ConnectionError("connection reset"))
    responses.get(f"{BASE_URL}/releases/1", json={"id": 1})

    assert get_release(1) == {"id": 1}


@responses.activate
def test_requests_are_paced_across_endpoints(discogs_clock):
    responses.get(f"{BASE_URL}/releases/1", json={"id": 1})
    responses.get(f"{BASE_URL}/marketplace/stats/1", json={"num_for_sale": 0})

    get_release(1)
    get_marketplace_stats(1, "EUR")
    get_release(1)

    assert discogs_clock == [MIN_INTERVAL, MIN_INTERVAL]


@responses.activate
def test_requests_have_a_timeout():
    responses.get(f"{BASE_URL}/releases/1", json={"id": 1})

    get_release(1)

    assert responses.calls[0].request.req_kwargs["timeout"] == 30


@responses.activate
def test_get_label_releases_reads_every_page_in_order(discogs_clock):
    for page, ids in [(1, [1, 2]), (2, [3, 4]), (3, [5])]:
        responses.get(
            f"{BASE_URL}/labels/7/releases",
            match=[matchers.query_param_matcher({"page": page, "per_page": 100})],
            json=label_page(page, 3, ids),
        )

    releases = get_label_releases(7)

    assert [r["id"] for r in releases] == [1, 2, 3, 4, 5]
    assert len(responses.calls) == 3
    assert discogs_clock == [MIN_INTERVAL, MIN_INTERVAL]


@responses.activate
def test_get_label_releases_single_page_does_not_pause(discogs_clock):
    responses.get(f"{BASE_URL}/labels/7/releases", json=label_page(1, 1, [1]))

    assert [r["id"] for r in get_label_releases(7)] == [1]
    assert discogs_clock == []


@responses.activate
def test_get_label_releases_with_no_releases(discogs_clock):
    responses.get(f"{BASE_URL}/labels/7/releases", json=label_page(1, 0, []))

    assert get_label_releases(7) == []
    assert len(responses.calls) == 1


@responses.activate
def test_get_label_releases_stops_when_a_later_page_fails(discogs_clock):
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
    # Page 1, then page 2 with every retry; page 3 is never asked for.
    assert len(responses.calls) == 1 + len(RETRY_WAITS) + 1


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
