from dataclasses import dataclass

from app.models import Review


# Keywords that require manual handling because the review is likely risky.
NEGATIVE_RISK_KEYWORDS = (
    "брак",
    "сломался",
    "не работает",
    "возврат",
    "недокомплект",
    "поврежден",
    "дефект",
)


@dataclass
class ReviewClassificationResult:
    """Simple structure with all derived moderation values."""

    category: str
    risk_level: str
    automation_mode: str
    confidence_score: float


def _contains_negative_risk_keywords(text: str) -> bool:
    """Check whether the text contains any risky negative words."""
    lowered_text = (text or "").lower()
    return any(keyword in lowered_text for keyword in NEGATIVE_RISK_KEYWORDS)


def classify_review(source_type: str, rating: int | None, text: str) -> ReviewClassificationResult:
    """
    Classify a review or question using simple business rules.
    The logic is intentionally explicit so it is easy for a beginner to change.
    """
    normalized_source_type = (source_type or "review").strip().lower()
    has_negative_keywords = _contains_negative_risk_keywords(text)

    if normalized_source_type == "question":
        return ReviewClassificationResult(
            category="вопрос",
            risk_level="низкий",
            automation_mode="auto",
            confidence_score=0.98,
        )

    if rating is not None and rating >= 5:
        category = "позитив"
    elif rating == 4:
        category = "нейтральный"
    else:
        category = "негатив"

    if has_negative_keywords:
        return ReviewClassificationResult(
            category=category,
            risk_level="высокий",
            automation_mode="manual_only",
            confidence_score=0.97,
        )

    if rating is not None and rating >= 5:
        return ReviewClassificationResult(
            category=category,
            risk_level="низкий",
            automation_mode="auto",
            confidence_score=0.94,
        )

    if rating == 4:
        return ReviewClassificationResult(
            category=category,
            risk_level="средний",
            automation_mode="review_required",
            confidence_score=0.9,
        )

    return ReviewClassificationResult(
        category=category,
        risk_level="высокий",
        automation_mode="manual_only",
        confidence_score=0.93 if rating is not None else 0.75,
    )


def apply_review_classification(review: Review) -> Review:
    """Apply classification result directly to the SQLAlchemy Review object."""
    result = classify_review(
        source_type=review.source_type,
        rating=review.rating,
        text=review.text,
    )
    review.category = result.category
    review.risk_level = result.risk_level
    review.automation_mode = result.automation_mode
    review.confidence_score = result.confidence_score
    return review
