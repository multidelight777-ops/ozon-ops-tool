from app.models import Review


def build_reply_template(category: str | None) -> str:
    """Return a Russian draft reply template based on the review category."""
    normalized_category = (category or "").strip().lower()

    if normalized_category == "позитив":
        return (
            "Спасибо за ваш отзыв! Нам очень приятно, что вам понравился товар. "
            "Будем рады видеть вас снова среди покупателей Multi Delight."
        )

    if normalized_category == "нейтральный":
        return (
            "Спасибо за отзыв! Если у вас появятся дополнительные вопросы или пожелания — "
            "обязательно напишите нам в личном кабинете или по юзу telegram - @multidelight."
        )

    if normalized_category == "негатив":
        return (
            "Нам очень жаль, что у вас возникла такая ситуация. "
            "Пожалуйста, напишите нам в личном кабинете или по юзу telegram подробнее, "
            "чтобы мы могли помочь вам разобраться."
        )

    if normalized_category == "вопрос":
        return (
            "Спасибо за ваш вопрос!. Если потребуется дополнительная информация — мы всегда на связи."
        )

    return "Спасибо за обращение! Мы подготовим ответ в ближайшее время."


def apply_reply_template(review: Review) -> Review:
    """Generate and save draft_reply directly on the Review object."""
    review.draft_reply = build_reply_template(review.category)
    return review
