"""Signed-in visitors can ask for more labels (the rules are in web/label_requests.py):
    POST /api/label-requests     {"name"}: stores a request for the owner to review
    GET  /api/label-requests     this account's requests and their status
"""
from flask import Blueprint, jsonify
from pydantic import BaseModel, ConfigDict, Field

from web import accounts, label_requests
from web.routes.shared import error, read_json, services, session_token

blueprint = Blueprint("label_requests", __name__)


class LabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=label_requests.MAX_NAME_LENGTH * 2)


@blueprint.post("/api/label-requests")
def request_label():
    payload, refused = read_json(LabelRequest, "a label name")
    if refused:
        return refused
    try:
        name = label_requests.clean_name(payload.name)
    except ValueError as problem:
        return error("invalid_label_name", str(problem), 400)
    outside = services()
    with outside.connect_limits() as conn:
        account = accounts.session_account(conn, session_token(), outside.now())
        if not account:
            return error("not_signed_in", "Sign in to request a label.", 401)
        # Read in full, or the request is refused: a failed read must not let a tracked
        # label be requested again. Labels still being imported count as tracked.
        known = label_requests.tracked_match(name, outside.known_labels())
        if known:
            return jsonify({"status": "tracked", "message": f"{known} is already tracked. You can ask about it now."})
        new = label_requests.request_label(conn, account[0], name, outside.now())
    if not new:
        return jsonify({"status": "requested", "message": f"You have already asked for {name}."})
    response = jsonify({"status": "requested",
                        "message": f"Thanks. {name} is waiting for review; requests are checked by hand."})
    response.status_code = 201
    return response


@blueprint.get("/api/label-requests")
def my_label_requests():
    outside = services()
    with outside.connect_limits() as conn:
        account = accounts.session_account(conn, session_token(), outside.now())
        if not account:
            return error("not_signed_in", "Sign in to see your label requests.", 401)
        return jsonify({"requests": label_requests.requests_of(conn, account[0])})
