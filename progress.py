import os
import json
import logging
import redis

logger = logging.getLogger(__name__)

_REDIS_URL = os.getenv("REDIS_URL")
if not _REDIS_URL:
    raise EnvironmentError("REDIS_URL is required for progress tracking")

_client = redis.from_url(_REDIS_URL, decode_responses=True)

_TTL = 3600  # 1 hour


def set_progress(progress_info: dict, user_id: str = "global") -> None:
    try:
        _client.setex(f"progress:{user_id}", _TTL, json.dumps(progress_info))
    except Exception:
        logger.exception("Failed to write progress to Redis for user_id=%s", user_id)


def get_progress(user_id: str = "global") -> dict:
    try:
        raw = _client.get(f"progress:{user_id}")
        if raw:
            return json.loads(raw)
    except Exception:
        logger.exception("Failed to read progress from Redis for user_id=%s", user_id)
    return {"state": "not_started", "step": "Not started", "message": ""}
