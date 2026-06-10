# =============================================================================
# osv1c/periods.py — період звіту: квартал або місяць
# =============================================================================
#
# Єдине місце, де визначено формат періоду:
#   - тег у БД/Excel:  "2025-Q1" (квартал), "2025-M03" (місяць);
#   - межі періоду як 1С-літерали ДАТАВРЕМЯ (див. чому літерали — нижче);
#   - людська назва для інтерфейсу: "1 квартал 2025", "березень 2025".
# =============================================================================

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass

MONTH_NAMES = [
    "січень", "лютий", "березень", "квітень", "травень", "червень",
    "липень", "серпень", "вересень", "жовтень", "листопад", "грудень",
]


@dataclass(frozen=True)
class Period:
    year: int
    kind: str   # "Q" — квартал, "M" — місяць
    num: int    # 1..4 для кварталу, 1..12 для місяця

    # -- межі ---------------------------------------------------------------

    @property
    def start_month(self) -> int:
        return (self.num - 1) * 3 + 1 if self.kind == "Q" else self.num

    @property
    def end_month(self) -> int:
        return self.start_month + 2 if self.kind == "Q" else self.num

    def literals(self) -> tuple[str, str]:
        """
        Межі періоду як 1С-літерали ДАТАВРЕМЯ для тексту запиту.

        ВАЖЛИВО: дати передаються саме літералом, а НЕ Python-datetime через
        параметр, бо pywin32 конвертує datetime у локальний час і зсуває межі
        на офсет часового поясу — реформація останнього дня попереднього
        періоду «затікає» всередину, а закриття останнього дня «випадає».
        """
        last_day = calendar.monthrange(self.year, self.end_month)[1]
        start = f"ДАТАВРЕМЯ({self.year}, {self.start_month}, 1, 0, 0, 0)"
        end = f"ДАТАВРЕМЯ({self.year}, {self.end_month}, {last_day}, 23, 59, 59)"
        return start, end

    # -- подання ------------------------------------------------------------

    @property
    def tag(self) -> str:
        """Ключ періоду в БД: '2025-Q1' або '2025-M03' (місяць з нулем —
        щоб алфавітне сортування збігалось із календарним)."""
        if self.kind == "Q":
            return f"{self.year}-Q{self.num}"
        return f"{self.year}-M{self.num:02d}"

    @property
    def label(self) -> str:
        if self.kind == "Q":
            return f"{self.num} квартал {self.year}"
        return f"{MONTH_NAMES[self.num - 1]} {self.year}"

    def sort_key(self) -> tuple:
        # місяць передує кварталу, що його містить (березень < 1 квартал)
        return (self.year, self.end_month, self.start_month)


# ---------------------------------------------------------------------------
# Парсинг
# ---------------------------------------------------------------------------

_RX = re.compile(
    r"^\s*(\d{4})\s*[-_ ]?\s*(?:"
    r"[QqКк]\s*([1-4])"          # 2025-Q1 / 2025К2
    r"|[MmМм]\s*(\d{1,2})"       # 2025-M03 / 2025-м3
    r"|([1-4])"                  # 2025-1 (історично — квартал)
    r")\s*$"
)


def parse(value) -> Period:
    """
    Розбирає період:
      - рядок: '2025-Q1', '2025Q1', '2025-1' (квартал), '2025-M03' (місяць);
      - кортеж (рік, квартал) — історичний формат config.PERIODS;
      - Period — повертається як є.
    """
    if isinstance(value, Period):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return Period(int(value[0]), "Q", int(value[1]))
    m = _RX.match(str(value))
    if not m:
        raise ValueError(
            f"Невірний формат періоду: '{value}'. "
            f"Очікую '2026-Q1' (квартал) або '2026-M03' (місяць)."
        )
    year = int(m.group(1))
    if m.group(2) or m.group(4):
        return Period(year, "Q", int(m.group(2) or m.group(4)))
    num = int(m.group(3))
    if not 1 <= num <= 12:
        raise ValueError(f"Невірний місяць у періоді '{value}': {num}")
    return Period(year, "M", num)


def tag_label(tag: str) -> str:
    """'2025-Q1' → '1 квартал 2025' (для підписів у UI); незнайомий тег — як є."""
    try:
        return parse(tag).label
    except ValueError:
        return tag


def sort_tags(tags: list[str]) -> list[str]:
    """Сортує теги періодів календарно (невідомі формати — в кінець)."""
    def key(t: str):
        try:
            return (0, *parse(t).sort_key())
        except ValueError:
            return (1, t)
    return sorted(tags, key=key)
