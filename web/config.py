"""Settings for the web API.

Like queries/config.py, it reads only what this part needs: the Turnstile secret,
the key for hashing visitors' IP addresses, and the counting login. The Discogs
token and the writing login's password are never read here.
"""
import os

from dotenv import load_dotenv

load_dotenv()

POSTGRES_HOST = os.environ["POSTGRES_HOST"]
POSTGRES_PORT = os.environ["POSTGRES_PORT"]
POSTGRES_DB = os.environ["POSTGRES_DB"]
POSTGRES_LIMITS_USER = os.environ["POSTGRES_LIMITS_USER"]
POSTGRES_LIMITS_PASSWORD = os.environ["POSTGRES_LIMITS_PASSWORD"]

# Cloudflare Turnstile, the human check on every question.
TURNSTILE_SECRET_KEY = os.environ["TURNSTILE_SECRET_KEY"]
# Visitors are counted by a keyed hash of their IP address, never the address itself.
VISITOR_HASH_SECRET = os.environ["VISITOR_HASH_SECRET"]

QUESTIONS_PER_VISITOR_HOUR = int(os.environ.get("QUESTIONS_PER_VISITOR_HOUR", "10"))
QUESTIONS_PER_VISITOR_DAY = int(os.environ.get("QUESTIONS_PER_VISITOR_DAY", "30"))
QUESTIONS_PER_SITE_DAY = int(os.environ.get("QUESTIONS_PER_SITE_DAY", "500"))

# Web pages on these origins may call the API from the browser, e.g.
# "https://adnsaudio.com,https://www.adnsaudio.com". Empty: same origin only.
ALLOWED_ORIGINS = [origin.strip() for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",") if origin.strip()]
# How many proxies in front of the API (e.g. 1 behind a load balancer) set
# X-Forwarded-For. Only then is that header used for the visitor's address.
TRUSTED_PROXIES = int(os.environ.get("TRUSTED_PROXIES", "0"))
