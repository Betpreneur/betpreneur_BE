from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class IntegrationConfig:
    aps_key: str
    aps_max_fixtures: str
    gemini_api_key: str
    gemini_model: str
    key_file: str
    sheet_name: str
    drive_folder: str
    email_recipient: str


def get_integration_config() -> IntegrationConfig:
    cfg = settings.GRIND_ALGO
    return IntegrationConfig(
        aps_key=cfg["APS_KEY"],
        aps_max_fixtures=cfg["APS_MAX_FIXTURES"],
        gemini_api_key=cfg["GEMINI_API_KEY"],
        gemini_model=cfg["GEMINI_MODEL"],
        key_file=cfg["KEY_FILE"],
        sheet_name=cfg["SHEET_NAME"],
        drive_folder=cfg["DRIVE_FOLDER"],
        email_recipient=cfg["EMAIL_RECIPIENT"],
    )
