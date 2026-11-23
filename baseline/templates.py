"""Fixed user, supplier, and review text for the deterministic baseline."""

from typing import Any


def clarify_intent(options: tuple[str, ...] = ()) -> str:
    if options:
        return "Уточните, какое действие нужно выполнить: " + ", ".join(options) + "."
    return ("Уточните запрос: я могу показать остатки, рассчитать потребность, сравнить "
            "поставщиков, оформить закупку, проверить заказ или подготовить ежедневный отчёт.")


def clarify_material() -> str:
    return "Уточните один материал из данных организации."


def unsupported_action() -> str:
    return "Это действие не поддерживается доступными инструментами и требует ручной обработки."


def supplier_clarification(reasons: list[str], material: str | None = None, *,
                           quantity: Any = None, required_by: Any = None) -> str:
    subject = f" по материалу «{material}»" if material else ""
    questions = []
    if "delivery_date" in reasons:
        questions.append("точную дату доставки")
    if "unit_price_rub" in reasons:
        questions.append("окончательную цену за единицу")
    if "delivery_fee_rub" in reasons:
        questions.append("стоимость доставки")
    if "confirmation" in reasons:
        questions.append("подтверждённое доступное количество")
    if "contradiction" in reasons:
        questions.append("однозначные коммерческие условия")
    requested = ", ".join(questions) if questions else "полные подтверждённые условия"
    context = ""
    if quantity is not None:
        context += f" Требуемое количество: {quantity}."
    if required_by is not None:
        context += f" Поставка нужна не позднее {required_by}."
    return f"Пожалуйста, подтвердите{subject}: {requested}.{context}"


def render_daily_review(activity: Any) -> str:
    data = activity.model_dump(mode="json") if hasattr(activity, "model_dump") else dict(activity)
    lines = [
        f"Ежедневный отчёт за {data['report_date']}",
        f"События: {len(data.get('events', []))}",
        f"Заказы: {len(data.get('orders', []))}",
        f"Согласования за день: {len(data.get('approvals', []))}",
        f"Ожидают согласования: {len(data.get('pending_approvals_current', []))}",
        f"Ошибки: {len(data.get('errors', []))}",
        f"Риски: {len(data.get('risks', []))}",
    ]
    completeness = data.get("completeness", "unknown")
    lines.append(f"Полнота данных: {completeness}; отчёт не считается доказанно полным.")
    limitations = data.get("limitations", [])
    if limitations:
        lines.append("Ограничения: " + " ".join(str(item) for item in limitations))
    return "\n".join(lines)
