from sqlalchemy.orm import Session

from app.models import ActionLog


def log_action(db: Session, action: str, details: str) -> None:
    """Save an action to the audit table."""
    db.add(ActionLog(action=action, details=details))
    db.commit()
