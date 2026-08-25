"""Queue names, in one place.

docker-compose runs one worker per queue with these same env vars, so a name
changed here must be changed there too.
"""
from decouple import config

SLIP_REVIEW = config("SLIP_REVIEW_QUEUE", default="slip_review")
SLIP_REVIEW_IMPORT = config("SLIP_REVIEW_IMPORT_QUEUE", default="slip_review_import")
SLIP_REVIEW_LEG = config("SLIP_REVIEW_LEG_QUEUE", default="slip_review_leg")
SLIP_REVIEW_FINALIZE = config("SLIP_REVIEW_FINALIZE_QUEUE", default="slip_review_finalize")

ALGO_DAILY = config("ALGO_DAILY_QUEUE", default="algo_daily")
ALGO_SCORING = config("ALGO_SCORING_QUEUE", default="algo_scoring")
ALGO_LLM = config("ALGO_LLM_QUEUE", default="algo_llm")
ALGO_STATPAL = config("ALGO_STATPAL_QUEUE", default="algo_statpal")
ALGO_SETTLEMENT = config("ALGO_SETTLEMENT_QUEUE", default="algo_settlement")
ALGO_MAINTENANCE = config("ALGO_MAINTENANCE_QUEUE", default="algo_maintenance")
