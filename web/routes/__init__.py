"""The web API's routes, one Flask blueprint per part; web/app.py registers them all.

    questions.py        POST /api/questions
    accounts.py         sign-up, sign-in, password reset, sign-out, deleting, account status
    label_requests.py   POST and GET /api/label-requests
    labels.py           GET /api/labels, /api/data-dates and /api/health
    pages.py            the HTML pages email links open, and the session cookie
    shared.py           the injected services, error answers, reading JSON, the human check
"""
