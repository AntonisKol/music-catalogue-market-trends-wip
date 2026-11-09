"""POST /api/questions: {"question", "captcha_token"} in, {"answer", "sources",
"warning", "model", "remaining"} out.

Each question passes, in this order: a check of the request, the Turnstile human
check (skipped when signed in), the visitor's or account's limits and the site's,
and only then the question service, so nothing reaches the Anthropic API before
every guard has passed.
"""
import logging
from datetime import datetime

import psycopg
from flask import Blueprint, jsonify
from pydantic import BaseModel, ConfigDict, Field

from queries.questions import MAX_QUESTION_LENGTH, QuestionError
from web import accounts, config, limits
from web.routes.shared import (CAPTCHA_TOKEN_LENGTH, allowance, counter, error, human_check, read_json,
                               services, session_token)

log = logging.getLogger(__name__)

blueprint = Blueprint("questions", __name__)

# Question service errors: HTTP status and, for temporary ones, seconds to wait.
QUESTION_ERRORS = {
    "invalid_question": (400, None),
    "busy": (503, 60),
    "unavailable": (503, 60),
    "database": (503, 60),
    "configuration": (500, None),
}


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    # Not needed when signed in.
    captcha_token: str = Field(default="", max_length=CAPTCHA_TOKEN_LENGTH)


@blueprint.post("/api/questions")
def ask():
    payload, refused = read_json(QuestionRequest, "a question")
    if refused:
        return refused
    outside = services()
    asked_at = outside.now()
    with outside.connect_limits() as conn:
        account = accounts.session_account(conn, session_token(), asked_at)
        if not account:
            refused = human_check(payload.captcha_token)
            if refused:
                return refused
        per_hour, per_day = allowance(account)
        left = limits.take_question(
            conn, counter(account), asked_at, per_hour, per_day, config.QUESTIONS_PER_SITE_DAY,
            signed_in=bool(account),
        )

    try:
        result = outside.answer(payload.question)
    except QuestionError as problem:
        # Nothing was answered, so the question doesn't count against the visitor.
        give_back_question(counter(account), asked_at)
        status, retry_after = QUESTION_ERRORS.get(problem.code, (500, None))
        return error(problem.code, str(problem), status, retry_after)
    return jsonify({"answer": result.text, "sources": result.sources,
                    "warning": result.warning, "model": result.model, "remaining": left})


def give_back_question(who: str, asked_at: datetime) -> None:
    try:
        with services().connect_limits() as conn:
            limits.give_back_question(conn, who, asked_at)
    except psycopg.Error as problem:
        log.error("A failed question could not be given back: %s", problem)
