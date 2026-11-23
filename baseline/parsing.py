"""Deterministic request and supplier-message parsing."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo


Intent = Literal[
    "inventory", "material_need", "compare_suppliers", "purchase",
    "supplier_reply", "supplier_delay", "order_status", "daily_review",
    "no_purchase", "unsupported_table", "unsupported", "unknown",
]


@dataclass(frozen=True)
class NormalizedRequest:
    kind: str
    event_id: str
    data: dict[str, Any]
    text: str
    legacy: bool = False


@dataclass(frozen=True)
class ParsedCommand:
    intent: Intent
    date_from: date | None = None
    date_to: date | None = None
    quantity: Decimal | None = None
    ambiguous_intents: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedSupplierMessage:
    material: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    unit_price_rub: Decimal | None = None
    delivery_fee_rub: Decimal | None = None
    delivery_date: date | None = None
    available: bool | None = None
    confirmed: bool | None = None
    confirmed_quantity: Decimal | None = None
    remaining_confirmed: bool | None = None
    uncertain: bool = False
    ambiguities: tuple[str, ...] = ()


_INTENT_PATTERNS = {
    "inventory": (r"\bостат(?:ок|ка|ки|ков|ках)\b", r"\bсклад\w*\b",
                  r"\binventor(?:y|ies)\b", r"\bstock\b"),
    "material_need": (r"\bпотребност\w*\b", r"\bдефицит\w*\b", r"\bрасход\w*\b",
                      r"\b(?:нужно|надо)\s+ли\s+докуп\w*", r"\bпроверь\w*.*\bнадо\s+ли\s+закуп\w*",
                      r"\bmaterial need\b", r"\bshortage\b", r"\bdeficit\b",
                      r"\bdo we need to (?:buy|order|restock)\b",
                      r"\bне\s+хват(?:ит|ает|ало)\b",
                      r"\bнехватк\w*\b",
                      r"\bcheck whether .*(?:buy|purchase|restock)\b"),
    "compare_suppliers": (r"\bсравн\w*\b.*\bпоставщик", r"\bпредложени\w*\b",
                          r"\bcompare\b.*\bsuppli", r"\b(?:best|cheapest) offer\b"),
    "purchase": (r"\bзакуп\w*\b", r"\bзакаж\w*\b", r"\bкуп(?:ить|и)\b",
                 r"\bдокуп(?:ить|и|ите)\b", r"\bоформ(?:и|ите)\b.*\bзаказ\w*\b",
                 r"\bподготов\w*\b.*\bоформлен\w*\b(?:.*\bзакуп\w*\b)?",
                 r"\bpurchase\b", r"\border\b.*\bmaterial", r"\bplace\b.*\border\b",
                 r"\bprepare\b.*\bpurchase\b", r"\b(?:buy more|reorder)\b"),
    "order_status": (r"\bстатус\w*\b.*\bзаказ", r"\bгде\b.*\bзаказ", r"\border status\b",
                     r"\bshipment status\b",
                     r"\bчто\s+сейчас\s+с\s+заказ\w*",
                     r"\bпоставщик\s+получил\s+(?:его|заказ)",
                     r"\b(?:проверь|уточни|сверь)\w*.*\b(?:отправк|заказ|submission)",
                     r"\b(?:провер|свер)\w*.*\b(?:отправк|заказ|submission)"),
    "daily_review": (r"\bежедневн\w*\b.*\b(?:отч[её]т|ревью)", r"\bитоги дня\b",
                     r"\bdaily (?:review|report)\b"),
}
_NO_PURCHASE = (
    r"\bне\s+(?:нужно|надо)\s+(?:закуп\w*|докуп\w*|заказ\w*)",
    r"\bне\s+(?:закупай|заказывай|покупай)\w*",
    r"\b(?:do not|don't|no need to)\s+(?:buy|order|purchase|restock)\b",
)
_NEED_CHECK = (
    r"\b(?:нужно|надо)\s+ли\s+докуп\w*",
    r"\bпроверь\w*.*\bнадо\s+ли\s+закуп\w*",
    r"\bdo we need to (?:buy|order|restock)\b",
    r"\bshould we (?:buy|order|restock)\b",
    r"\bcheck whether .*(?:buy|purchase|restock)\b",
)
_DELAY = (
    r"\bзадерж\w*\b", r"\bперенос\w*\b", r"\bперен[её]с\w*\b",
    r"\bдостав\w*\b.*\bпозже\b", r"\bdelay\w*\b", r"\bpostpon\w*\b",
    r"\breschedul\w*\b", r"\blate delivery\b",
)
_UNSUPPORTED = (
    r"\bоплат\w*\b", r"\bpayment\b", r"\bотмен\w*\b", r"\bcancel\w*\b",
    r"\bизмен\w*\b.*\bзаказ", r"\bmodify\b.*\border", r"\bраздел\w*\b.*\bзаказ",
    r"\bsplit\b.*\border",
)
_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_NUMBER = r"\d+(?:[.,]\d+)?"


def normalize_request(request: Any, fallback_event_id: str | None = None) -> NormalizedRequest:
    """Normalize legacy and event requests once, before workflow routing."""
    event_id = fallback_event_id or "legacy"
    if isinstance(request, str):
        return NormalizedRequest("user_message", event_id, {"text": request}, request, True)
    if not isinstance(request, dict):
        return NormalizedRequest("user_message", event_id, {}, "", True)

    normalized = "event_id" in request and "data" in request
    if normalized:
        raw_data = request.get("data")
        data = dict(raw_data) if isinstance(raw_data, dict) else {"value": raw_data}
        kind = str(request.get("kind") or "user_message")
        event_id = str(request.get("event_id") or event_id)
        nested_kind = data.get("kind")
        if kind == "user_message" and nested_kind in (
            "user_message", "supplier_message", "supplier_event", "table",
        ):
            kind = str(nested_kind)
    else:
        data = dict(request)
        kind = str(data.pop("kind", "user_message"))
        event_id = str(data.pop("event_id", event_id))

    text = data.get("text") or data.get("message") or data.get("content") or ""
    return NormalizedRequest(kind, event_id, data, str(text), not normalized)


def local_date(now: datetime, timezone: str) -> date:
    return now.astimezone(ZoneInfo(timezone)).date()


def parse_dates(text: str, now: datetime, timezone: str) -> list[date]:
    """Return explicit dates in textual order using trusted scenario time."""
    today = local_date(now, timezone)
    lowered = text.casefold()
    matches: list[tuple[int, date]] = []

    for match in re.finditer(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", lowered):
        try:
            matches.append((match.start(), date(*(int(part) for part in match.groups()))))
        except ValueError:
            continue
    for match in re.finditer(r"\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b", lowered):
        try:
            day, month, year = (int(part) for part in match.groups())
            matches.append((match.start(), date(year, month, day)))
        except ValueError:
            continue

    names = "|".join(map(re.escape, _MONTHS))
    for match in re.finditer(rf"\b(\d{{1,2}})\s+({names})(?:\s+(20\d{{2}}))?\b", lowered):
        day, name, year_text = match.groups()
        year = int(year_text) if year_text else today.year
        try:
            value = date(year, _MONTHS[name], int(day))
            if year_text is None and value < today:
                value = date(year + 1, _MONTHS[name], int(day))
            matches.append((match.start(), value))
        except ValueError:
            continue

    relative = {
        "послезавтра": 2, "завтра": 1, "сегодня": 0,
        "day after tomorrow": 2, "tomorrow": 1, "today": 0,
    }
    for phrase, days in relative.items():
        for match in re.finditer(rf"\b{re.escape(phrase)}\b", lowered):
            matches.append((match.start(), today + timedelta(days=days)))
    for match in re.finditer(r"\b(?:через|in)\s+(\d+)\s+(?:дн(?:я|ей)?|days?)\b", lowered):
        matches.append((match.start(), today + timedelta(days=int(match.group(1)))))

    result = []
    for _, value in sorted(matches):
        if value not in result:
            result.append(value)
    return result


def parse_command(request: NormalizedRequest, now: datetime, timezone: str) -> ParsedCommand:
    kind = request.kind
    if kind == "table":
        return ParsedCommand("unsupported_table")
    if kind in ("supplier_message", "supplier_event") and is_delay_message(request.text):
        dates = parse_dates(request.text, now, timezone)
        return ParsedCommand("supplier_delay", dates[0] if dates else None,
                             dates[-1] if dates else None)
    if kind in ("supplier_message", "supplier_event", "supplier_reply"):
        return ParsedCommand("supplier_reply")
    if kind == "manager_decision":
        return ParsedCommand("purchase")
    if kind == "external_result":
        return ParsedCommand("order_status")
    if kind == "time_tick":
        return ParsedCommand("daily_review")

    text = request.text.casefold()
    if any(re.search(pattern, text) for pattern in _NO_PURCHASE):
        return ParsedCommand("no_purchase")
    if any(re.search(pattern, text) for pattern in _NEED_CHECK):
        dates = parse_dates(request.text, now, timezone)
        return ParsedCommand("material_need", dates[0] if dates else None,
                             dates[-1] if dates else None)
    if any(re.search(pattern, text) for pattern in _UNSUPPORTED):
        return ParsedCommand("unsupported")

    found = [name for name, patterns in _INTENT_PATTERNS.items()
             if any(re.search(pattern, text) for pattern in patterns)]
    if len(found) > 1:
        # Purchase naturally includes need/offer language; an explicit action wins.
        if "purchase" in found:
            found = ["purchase"]
        elif set(found) == {"material_need", "compare_suppliers"}:
            found = ["compare_suppliers"]
        else:
            return ParsedCommand("unknown", ambiguous_intents=tuple(sorted(found)))

    dates = parse_dates(request.text, now, timezone)
    quantity = _first_decimal(request.text, rf"({_NUMBER})\s*(?:шт\.?|ед\.?|units?|pcs?\.?|кг|kg)\b")
    return ParsedCommand(found[0] if found else "unknown",
                         dates[0] if dates else None, dates[-1] if dates else None, quantity)


def is_delay_message(text: str) -> bool:
    lowered = text.casefold()
    return any(re.search(pattern, lowered) for pattern in _DELAY)


def parse_delivery_date(text: str, now: datetime, timezone: str) -> date | None:
    """Extract one date explicitly tied to delivery wording."""
    names = "|".join(map(re.escape, _MONTHS))
    token = (rf"(?:20\d{{2}}-\d{{1,2}}-\d{{1,2}}|\d{{1,2}}[./]\d{{1,2}}[./]20\d{{2}}|"
             rf"\d{{1,2}}\s+(?:{names})(?:\s+20\d{{2}})?|послезавтра|завтра|сегодня|"
             rf"day after tomorrow|tomorrow|today)")
    patterns = (
        rf"\bперен[её]с\w*(?:\s+\w+){{0,3}}\s+на\s+({token})",
        rf"\bдостав\w*(?:\s+\w+){{0,3}}\s+(?:на|до|к)\s+({token})",
        rf"\bдоставим\s+({token})",
        rf"\bdelivery(?:\s+is)?\s+(?:delayed|postponed|rescheduled)\s+(?:until|to|for)\s+({token})",
        rf"\bdeliver\w*(?:\s+\w+){{0,3}}\s+(?:on|by|until)\s+({token})",
    )
    found = []
    lowered = text.casefold()
    for pattern in patterns:
        for match in re.finditer(pattern, lowered):
            dates = parse_dates(match.group(1), now, timezone)
            if len(dates) == 1 and dates[0] not in found:
                found.append(dates[0])
    return found[0] if len(found) == 1 else None


def match_materials(text: str, materials: list[str]) -> list[str]:
    lowered = " ".join(text.casefold().split())
    matched = [material for material in materials
               if " ".join(material.casefold().split()) in lowered]
    return sorted(set(matched), key=lambda value: (-len(value), value.casefold()))


def parse_supplier_message(text: str, materials: list[str], now: datetime,
                           timezone: str) -> ParsedSupplierMessage:
    lowered = " ".join(text.casefold().split())
    ambiguities: list[str] = []
    matched = match_materials(lowered, materials)
    if len(matched) > 1:
        ambiguities.append("multiple_materials")
    material = matched[0] if len(matched) == 1 else None

    quantity_matches = _decimals(lowered, rf"({_NUMBER})\s*(шт\.?|ед\.?|units?|pcs?\.?|кг|kg)\b")
    quantities = _unique(value for value, _ in quantity_matches)
    if len(quantities) > 1:
        ambiguities.append("multiple_quantities")
    quantity = quantities[0] if len(quantities) == 1 else None
    unit = quantity_matches[0][1] if quantity_matches else None

    non_final = bool(re.search(rf"\b(?:от|from|starting at)\s+{_NUMBER}\s*(?:руб|rub|₽)", lowered))
    price_matches = _decimals(lowered, rf"(?:по|цена\s*[:=]?|price\s*[:=]?)\s*({_NUMBER})\s*(?:руб|rub|₽)")
    if not price_matches:
        price_matches = _decimals(lowered, rf"({_NUMBER})\s*(?:руб|rub|₽)\s*(?:за|per)\s*(?:шт|ед|unit|pc)")
    prices = _unique(value for value, *_ in price_matches)
    if non_final:
        ambiguities.append("non_final_unit_price")
        unit_price = None
    elif len(prices) > 1:
        ambiguities.append("multiple_unit_prices")
        unit_price = None
    else:
        unit_price = prices[0] if prices else None

    fee_matches = _decimals(lowered, rf"(?:доставка|delivery(?: fee| cost)?)\s*(?:[:=]|стоит|costs?)?\s*({_NUMBER})\s*(?:руб|rub|₽)")
    fees = _unique(value for value, *_ in fee_matches)
    if len(fees) > 1:
        ambiguities.append("multiple_delivery_fees")
    delivery_fee = fees[0] if len(fees) == 1 else None
    if re.search(r"\b(?:бесплатн\w*\s+достав|free delivery)\b", lowered):
        delivery_fee = Decimal("0")

    dates = parse_dates(text, now, timezone)
    if len(dates) > 1:
        ambiguities.append("multiple_delivery_dates")
    delivery_date = dates[0] if len(dates) == 1 else None

    negative_pattern = r"\b(?:не\s+подтвержд\w*|нет\s+в\s+наличии|отказыва\w*|не\s+можем\s+постав\w*|unavailable|cannot\s+supply|declin\w*|not\s+confirm\w*)"
    denied = bool(re.search(negative_pattern, lowered))
    positive_source = re.sub(negative_pattern, "", lowered)
    positive = bool(re.search(r"\b(?:подтвержд(?:аем|ено)|есть\s+в\s+наличии|в\s+наличии|available|confirm(?:ed)?)\b", positive_source))
    partial = bool(re.search(r"\b(?:остальн\w*\s+не\s+подтвержд\w*|частичн\w*|remaining\s+(?:is\s+)?(?:unavailable|not\s+confirmed)|partial(?:ly)?)\b", lowered))
    uncertain = bool(re.search(r"\b(?:возможно|вероятно|ориентировочно|предварительно|может быть|maybe|possibly|probably|approximately|tentative)", lowered))

    if partial and (not positive or quantity is None):
        ambiguities.append("partial_confirmation_without_quantity")
        confirmed = None
    elif positive and denied and not partial:
        ambiguities.append("conflicting_confirmation")
        confirmed = None
    elif positive:
        confirmed = True
    elif denied:
        confirmed = False
    else:
        confirmed = None

    available = False if denied and not partial else True if positive and confirmed is True else None
    confirmed_quantity = quantity if confirmed is True else None
    remaining = False if partial and confirmed_quantity is not None else None

    return ParsedSupplierMessage(material, quantity, unit, unit_price, delivery_fee,
                                 delivery_date, available, confirmed, confirmed_quantity,
                                 remaining, uncertain, tuple(ambiguities))


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation:
        return None


def _first_decimal(text: str, pattern: str) -> Decimal | None:
    match = re.search(pattern, text.casefold())
    return _decimal(match.group(1)) if match else None


def _decimals(text: str, pattern: str) -> list[tuple]:
    result = []
    for match in re.finditer(pattern, text):
        value = _decimal(match.group(1))
        if value is not None:
            result.append((value, *match.groups()[1:]))
    
    return result


def _unique(values) -> list[Decimal]:
    result = []
    for value in values:
        if value not in result:
            result.append(value)

    return result
