# =============================================================================
# osv1c/connector.py — пряме підключення до 1С через COM (V83.COMConnector)
# =============================================================================
#
# Замінює крихку UI-автоматизацію (pyautogui/OCR). Дані ОСВ беруться прямо з
# бази запитом до регістру бухгалтерії — без відкриття вікон і кліків по екрану.
#
# Передумова (одноразово, з правами адміністратора):
#     regsvr32 "C:\Program Files\1cv8\<версія>\bin\comcntr.dll"
#
# =============================================================================

from __future__ import annotations

import re
from dataclasses import dataclass

import win32com.client

import config
from osv1c.periods import Period, parse as parse_period


# ---------------------------------------------------------------------------
# Структури даних
# ---------------------------------------------------------------------------

@dataclass
class SummaryRow:
    """Один рядок зведеної ОСВ (по рахунку)."""
    period: str
    account_code: str
    account_name: str
    level: int
    saldo_start_dt: float
    saldo_start_ct: float
    turnover_dt: float
    turnover_ct: float
    saldo_end_dt: float
    saldo_end_ct: float


@dataclass
class AccountDim:
    """Рядок плану рахунків (для побудови ієрархії ОСВ)."""
    code: str
    name: str
    parent_code: str        # код батьківського рахунку ("" для кореня)
    kind: str               # вид: "Активний" / "Пасивний" / "Активний/Пасивний"
    off_balance: bool       # забалансовий (не входить у збалансований підсумок)


@dataclass
class DetailRow:
    """Один рядок деталізації по рахунку (розріз субконто)."""
    period: str
    parent_account: str          # код рахунку, по якому деталізація ("631")
    account_code: str            # рахунок/субрахунок з віртуальної таблиці
    subconto1: str               # 1-ше субконто (напр. контрагент)
    subconto2: str               # 2-ге субконто (напр. договір)
    subconto3: str               # 3-тє субконто
    saldo_start_dt: float
    saldo_start_ct: float
    turnover_dt: float
    turnover_ct: float
    saldo_end_dt: float
    saldo_end_ct: float


# ---------------------------------------------------------------------------
# Утиліти
# ---------------------------------------------------------------------------
# Період (квартал/місяць), його межі-літерали ДАТАВРЕМЯ і тег — в osv1c/periods.py.

def code_level(code: str) -> int:
    """Рівень вкладеності рахунку за довжиною коду (1=розділ … 4=аналітика)."""
    digits = re.sub(r"[^0-9]", "", code or "")
    n = len(digits)
    if n <= 1:
        return 1
    if n == 2:
        return 2
    if n == 3:
        return 3
    return 4


def _f(v) -> float:
    """COM повертає float або None → нормалізуємо до float."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _s(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


# ---------------------------------------------------------------------------
# Конектор
# ---------------------------------------------------------------------------

class Osv1C:
    """Підключення до 1С та вивантаження ОСВ через запити до регістру."""

    def __init__(self,
                 base_path: str = config.C1_BASE_PATH,
                 user: str = config.C1_USER,
                 password: str = config.C1_PASSWORD,
                 register: str = "Хозрасчетный",
                 chart: str = "Хозрасчетный",
                 organization: str | None = getattr(config, "C1_ORGANIZATION", None)):
        self.base_path = base_path
        self.user = user
        self.password = password
        self.register = register
        self.chart = chart
        self.organization = organization or None
        self._conn = None
        self._org_ref = None

    # ------------------------------------------------------------------

    def connect(self):
        """Встановлює COM-з'єднання з базою. Кидає зрозумілу помилку, якщо
        конектор не зареєстровано."""
        conn_str = (
            f"File='{self.base_path}';"
            f"Usr='{self.user}';"
            f"Pwd='{self.password}';"
        )
        try:
            connector = win32com.client.Dispatch("V83.COMConnector")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "COM-конектор 1С (V83.COMConnector) не зареєстровано.\n"
                "Виконайте один раз від імені адміністратора:\n"
                '    regsvr32 "C:\\Program Files\\1cv8\\<версія>\\bin\\comcntr.dll"'
            ) from e

        try:
            self._conn = connector.Connect(conn_str)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Не вдалося підключитись до бази 1С: {self.base_path}\n"
                f"Перевірте шлях/логін/пароль у config.py.\nДеталі: {e}"
            ) from e

        print(f"[1С] Підключено до бази: {self.base_path}")
        self._verify_metadata()
        self._resolve_organization()
        return self

    def _resolve_organization(self):
        """Знаходить посилання на організацію за підрядком назви (для фільтра ОСВ)."""
        if not self.organization:
            print("[1С] Організація не задана — ОСВ по ВСІХ організаціях.")
            return
        sel = self._conn.Справочники.Организации.Выбрать()
        found = []
        target = self.organization.lower()
        while sel.Следующий():
            name = _s(sel.Наименование)
            if target in name.lower():
                found.append((name, sel.Ссылка))
        if not found:
            # перелічимо доступні для підказки
            names = []
            sel2 = self._conn.Справочники.Организации.Выбрать()
            while sel2.Следующий():
                names.append(_s(sel2.Наименование))
            raise RuntimeError(
                f"Організацію за підрядком '{self.organization}' не знайдено. "
                f"Доступні: {names}"
            )
        self._org_ref = found[0][1]
        print(f"[1С] Організація для ОСВ: {found[0][0]}")

    def _verify_metadata(self):
        """Перевіряє, що регістр і план рахунків існують (інакше — підказка)."""
        md = self._conn.Metadata
        reg_names = {r.Имя for r in md.РегистрыБухгалтерии}
        coa_names = {p.Имя for p in md.ПланыСчетов}
        if self.register not in reg_names:
            raise RuntimeError(
                f"Регістр бухгалтерії '{self.register}' не знайдено. "
                f"Доступні: {sorted(reg_names)}"
            )
        if self.chart not in coa_names:
            raise RuntimeError(
                f"План рахунків '{self.chart}' не знайдено. "
                f"Доступні: {sorted(coa_names)}"
            )

    # ------------------------------------------------------------------

    def _new_query(self, text: str):
        """Створює запит і одразу ставить параметр організації (якщо заданий).
        Дати в текст запиту підставляються літералом ДАТАВРЕМЯ (див. quarter_literals)."""
        q = self._conn.NewObject("Запрос")
        q.Текст = text
        self._set_org_param(q)
        return q

    def _account_ref(self, code: str):
        """Посилання на рахунок за кодом (для фільтра деталізації)."""
        chart = getattr(self._conn.ПланыСчетов, self.chart)
        return chart.НайтиПоКоду(code)

    def _org_where(self, alias: str) -> str:
        """WHERE-фільтр по організації (порожній, якщо орг не задана)."""
        return f"\n        ГДЕ {alias}.Организация = &Орг" if self._org_ref is not None else ""

    def _set_org_param(self, query):
        if self._org_ref is not None:
            query.УстановитьПараметр("Орг", self._org_ref)

    # ------------------------------------------------------------------
    # Зведена ОСВ
    # ------------------------------------------------------------------

    def fetch_summary(self, period: Period | str) -> list[SummaryRow]:
        """
        Зведена ОСВ за період (квартал або місяць): по одному рядку на рахунок.

        Сальдо рахується через підзапит на рівні субконто і потім сумується —
        це дає *розгорнуте* сальдо (як у стандартній ОСВ), щоб активно-пасивні
        рахунки (напр. 631, 361) показували обидві сторони й узгоджувались із
        деталізацією. Якщо брати сальдо прямо на рівні рахунку, 1С згортає Дт/Кт.
        """
        period = parse_period(period)
        start, end = period.literals()
        tag = period.tag

        text = f"""
        ВЫБРАТЬ
            Т.Код КАК Код,
            Т.Наим КАК Наим,
            СУММА(Т.нДт)  КАК нДт,
            СУММА(Т.нКт)  КАК нКт,
            СУММА(Т.обДт) КАК обДт,
            СУММА(Т.обКт) КАК обКт,
            СУММА(Т.кДт)  КАК кДт,
            СУММА(Т.кКт)  КАК кКт
        ИЗ (
            ВЫБРАТЬ
                Остатки.Счет.Код КАК Код,
                Остатки.Счет.Наименование КАК Наим,
                Остатки.Субконто1 КАК С1, Остатки.Субконто2 КАК С2, Остатки.Субконто3 КАК С3,
                Остатки.СуммаНачальныйОстатокДт КАК нДт,
                Остатки.СуммаНачальныйОстатокКт КАК нКт,
                Остатки.СуммаОборотДт           КАК обДт,
                Остатки.СуммаОборотКт           КАК обКт,
                Остатки.СуммаКонечныйОстатокДт  КАК кДт,
                Остатки.СуммаКонечныйОстатокКт  КАК кКт
            ИЗ
                РегистрБухгалтерии.{self.register}.ОстаткиИОбороты(
                    {start}, {end}, , , , , ) КАК Остатки{self._org_where("Остатки")}
        ) КАК Т
        СГРУППИРОВАТЬ ПО
            Т.Код, Т.Наим
        УПОРЯДОЧИТЬ ПО
            Код
        """
        q = self._new_query(text)
        sel = q.Выполнить().Выбрать()

        rows: list[SummaryRow] = []
        while sel.Следующий():
            code = _s(sel.Код)
            if not code:
                continue
            rows.append(SummaryRow(
                period=tag,
                account_code=code,
                account_name=_s(sel.Наим),
                level=code_level(code),
                saldo_start_dt=_f(sel.нДт), saldo_start_ct=_f(sel.нКт),
                turnover_dt=_f(sel.обДт),   turnover_ct=_f(sel.обКт),
                saldo_end_dt=_f(sel.кДт),   saldo_end_ct=_f(sel.кКт),
            ))
        return rows

    # ------------------------------------------------------------------
    # Деталізація по рахунку (субконто)
    # ------------------------------------------------------------------

    def fetch_detail(self, account_code: str, period: Period | str) -> list[DetailRow]:
        """Деталізація по рахунку в розрізі субконто за період."""
        period = parse_period(period)
        start, end = period.literals()
        tag = period.tag

        acc = self._account_ref(account_code)
        if acc is None or acc.Пустая():
            return []

        text = f"""
        ВЫБРАТЬ
            Остатки.Счет.Код КАК Код,
            ПРЕДСТАВЛЕНИЕ(Остатки.Субконто1) КАК Суб1,
            ПРЕДСТАВЛЕНИЕ(Остатки.Субконто2) КАК Суб2,
            ПРЕДСТАВЛЕНИЕ(Остатки.Субконто3) КАК Суб3,
            Остатки.СуммаНачальныйОстатокДт КАК нДт,
            Остатки.СуммаНачальныйОстатокКт КАК нКт,
            Остатки.СуммаОборотДт           КАК обДт,
            Остатки.СуммаОборотКт           КАК обКт,
            Остатки.СуммаКонечныйОстатокДт  КАК кДт,
            Остатки.СуммаКонечныйОстатокКт  КАК кКт
        ИЗ
            РегистрБухгалтерии.{self.register}.ОстаткиИОбороты(
                {start}, {end}, , , Счет В ИЕРАРХИИ (&Счет), , ) КАК Остатки{self._org_where("Остатки")}
        УПОРЯДОЧИТЬ ПО
            Код
        """
        q = self._new_query(text)
        q.УстановитьПараметр("Счет", acc)
        sel = q.Выполнить().Выбрать()

        rows: list[DetailRow] = []
        while sel.Следующий():
            rows.append(DetailRow(
                period=tag,
                parent_account=account_code,
                account_code=_s(sel.Код),
                subconto1=_s(sel.Суб1),
                subconto2=_s(sel.Суб2),
                subconto3=_s(sel.Суб3),
                saldo_start_dt=_f(sel.нДт), saldo_start_ct=_f(sel.нКт),
                turnover_dt=_f(sel.обДт),   turnover_ct=_f(sel.обКт),
                saldo_end_dt=_f(sel.кДт),   saldo_end_ct=_f(sel.кКт),
            ))
        # 1С не дозволяє УПОРЯДОЧИТЬ по ПРЕДСТАВЛЕНИЕ(), тож сортуємо тут
        rows.sort(key=lambda r: (r.account_code, r.subconto1, r.subconto2, r.subconto3))
        return rows

    # ------------------------------------------------------------------
    # План рахунків (ієрархія)
    # ------------------------------------------------------------------

    def fetch_accounts(self) -> list[AccountDim]:
        """Повертає весь план рахунків: код, назва, батько, вид."""
        text = f"""
        ВЫБРАТЬ
            Сч.Код КАК Код,
            Сч.Наименование КАК Наим,
            Сч.Родитель.Код КАК РодКод,
            ПРЕДСТАВЛЕНИЕ(Сч.Вид) КАК Вид,
            Сч.Забалансовый КАК Заб
        ИЗ
            ПланСчетов.{self.chart} КАК Сч
        УПОРЯДОЧИТЬ ПО
            Код
        """
        q = self._conn.NewObject("Запрос")
        q.Текст = text
        sel = q.Выполнить().Выбрать()

        rows: list[AccountDim] = []
        while sel.Следующий():
            code = _s(sel.Код)
            if not code:
                continue
            rows.append(AccountDim(
                code=code,
                name=_s(sel.Наим),
                parent_code=_s(sel.РодКод),
                kind=_s(sel.Вид),
                off_balance=bool(sel.Заб),
            ))
        return rows

    # ------------------------------------------------------------------

    @staticmethod
    def codes_to_drill(summary_rows: list[SummaryRow]) -> list[str]:
        """Коди рахунків для деталізації: пропускаємо нульові й службові."""
        codes: list[str] = []
        for r in summary_rows:
            if r.account_code in config.SKIP_DETAIL_CODES:
                continue
            # рахунок без жодного руху і без сальдо деталізувати нема сенсу
            has_data = any((
                r.saldo_start_dt, r.saldo_start_ct,
                r.turnover_dt, r.turnover_ct,
                r.saldo_end_dt, r.saldo_end_ct,
            ))
            if has_data:
                codes.append(r.account_code)
        return codes


# ---------------------------------------------------------------------------
# Швидка перевірка
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    bot = Osv1C().connect()
    summary = bot.fetch_summary("2025-Q1")
    print(f"\nЗведена ОСВ 2025-Q1: {len(summary)} рахунків")
    for r in summary[:8]:
        print(f"  {r.account_code:8} {r.account_name[:28]:28} "
              f"обДт={r.turnover_dt} кДт={r.saldo_end_dt}")

    codes = Osv1C.codes_to_drill(summary)
    print(f"\nРахунків для деталізації: {len(codes)}")
    if codes:
        sample = codes[0]
        det = bot.fetch_detail(sample, "2025-Q1")
        print(f"Деталі по рахунку {sample}: {len(det)} рядків")
        for r in det[:5]:
            print(f"  {r.account_code:6} | {r.subconto1[:30]:30} | кКт={r.saldo_end_ct}")
