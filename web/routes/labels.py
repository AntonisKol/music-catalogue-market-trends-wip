"""What the page shows before anyone asks, and the health check.

GET /api/labels returns {"labels": [...]}, the labels the site follows whose releases
are in the database, for the list on the page. GET /api/data-dates returns
{"tracked_since", "updated"}, the first and latest day prices were saved, for
"About the data" on the page; browsers may reuse both for five minutes.
GET /api/health returns {"status": "ok"}.
"""
from flask import Blueprint, jsonify

from web.routes.shared import services

blueprint = Blueprint("labels", __name__)

# How long browsers may reuse the list of labels and the data dates, which change rarely.
LABELS_CACHE_SECONDS = 300


@blueprint.get("/api/labels")
def labels():
    response = jsonify({"labels": services().list_labels()})
    response.headers["Cache-Control"] = f"public, max-age={LABELS_CACHE_SECONDS}"
    return response


@blueprint.get("/api/data-dates")
def data_dates_of_the_page():
    response = jsonify(services().dates())
    response.headers["Cache-Control"] = f"public, max-age={LABELS_CACHE_SECONDS}"
    return response


@blueprint.get("/api/health")
def health():
    return jsonify({"status": "ok"})
