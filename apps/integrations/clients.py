from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class IntegrationConfig:
    fd_token: str
    aps_key: str
    odds_key: str
    key_file: str
    sheet_name: str
    drive_folder: str
    email_recipient: str


def get_integration_config() -> IntegrationConfig:
    cfg = settings.GRIND_ALGO
    return IntegrationConfig(
        fd_token=cfg["FD_TOKEN"],
        aps_key=cfg["APS_KEY"],
        odds_key=cfg["ODDS_KEY"],
        key_file=cfg["KEY_FILE"],
        sheet_name=cfg["SHEET_NAME"],
        drive_folder=cfg["DRIVE_FOLDER"],
        email_recipient=cfg["EMAIL_RECIPIENT"],
    )
