"""
analytics/cpm_engine.py — Модуль 2б: Критичний шлях (CPM) + технологічні затримки

Ключове технічне рішення (проти hardcoded assumptions):
  - Затримки НЕ захардкожені. AI спочатку витягує їх з ТД.
  - Тільки якщо ТД нічого не говорить → застосовуємо TECH_DEFAULTS.
  - TECH_DEFAULTS — словник з посиланнями на ДБН/ДСТУ для прозорості.
  - networkx.DiGraph + longest_path для розрахунку критичного шляху.

Формат Task (вхідний блок):
    {
        "id": "concrete_01",
        "name": "Бетонування фундаменту",
        "duration_days": 3,        # тривалість самої роботи
        "tech_delay_days": 28,     # очікування до наступного кроку (твердіння)
        "depends_on": ["excavation_01"]
    }
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import networkx as nx
from loguru import logger


# ── Технологічні затримки за замовчуванням ────────────────────────────────────
# ВАЖЛИВО: Це резервні значення. Якщо у ТД є інші строки — вони мають пріоритет.
# Джерела: ДБН В.2.6-98:2009, ДСТУ Б А.3.1-3, виробничі норми ДСТУ EN.

TECH_DEFAULTS: dict[str, dict] = {
    "concrete_monolithic": {
        "delay_days": 28,
        "source": "ДБН В.2.6-98:2009 п.5.4 (набір 70% міцності при +20°C)",
        "description": "Монолітний бетон — твердіння до проведення наступних робіт",
    },
    "concrete_prefab": {
        "delay_days": 7,
        "source": "ДСТУ Б В.2.7-73 (збірні конструкції)",
        "description": "Збірний ЗБ — монтажний стик + витримка",
    },
    "plaster_cement": {
        "delay_days": 14,
        "source": "ДБН В.2.6-22 (штукатурні роботи)",
        "description": "Цементна штукатурка — сушіння до нанесення фінішу",
    },
    "plaster_gypsum": {
        "delay_days": 7,
        "source": "Технічні умови виробника (гіпсова штукатурка)",
        "description": "Гіпсова штукатурка — сушіння",
    },
    "waterproofing": {
        "delay_days": 3,
        "source": "ДБН В.2.6-22 (гідроізоляція)",
        "description": "Бітумна гідроізоляція — полімеризація",
    },
    "screed": {
        "delay_days": 21,
        "source": "ДБН В.2.6-22 (підлоги)",
        "description": "Цементна стяжка — сушіння до фінішного покриття",
    },
    "paint": {
        "delay_days": 2,
        "source": "Технічні умови виробника (фарба)",
        "description": "Лакофарбове покриття — сушіння між шарами",
    },
    "masonry": {
        "delay_days": 3,
        "source": "ДБН В.2.6-162 (мурування)",
        "description": "Кам'яне мурування — схоплення розчину",
    },
}


# ── Результат ─────────────────────────────────────────────────────────────────

@dataclass
class CPMTask:
    """Одна операція в графі робіт."""
    id: str
    name: str
    duration_days: int       # тривалість самої роботи
    tech_delay_days: int = 0 # технологічне очікування після роботи
    depends_on: list[str] = field(default_factory=list)
    tech_delay_source: str = "manual"   # "td_extracted" | "tech_defaults" | "manual"

    @property
    def total_days(self) -> int:
        return self.duration_days + self.tech_delay_days


@dataclass
class CPMResult:
    tasks_count: int = 0
    critical_path: list[str] = field(default_factory=list)   # список task_id
    critical_path_days: int = 0
    deadline_days: Optional[int] = None
    deadline_risk: bool = False       # True якщо CPM > deadline
    slack_days: Optional[int] = None  # запас у днях (від'ємний = прострочення)
    tech_defaults_used: list[str] = field(default_factory=list)  # які defaults застосовано
    error: Optional[str] = None

    def summary_text(self) -> str:
        if self.error:
            return f"📅 *CPM Engine:* Помилка — {self.error}"

        risk_label = (
            f"🔴 РИЗИК ПРОСТРОЧЕННЯ (бракує {-self.slack_days} дн.)"
            if self.deadline_risk
            else f"🟢 Укладається в строк (запас {self.slack_days} дн.)"
        )
        path_names = " → ".join(self.critical_path[:5])
        if len(self.critical_path) > 5:
            path_names += f" → ...({len(self.critical_path)} кроків)"

        lines = [
            f"📅 *Календарний аналіз (CPM)* [{risk_label}]",
            f"⏱ Мінімальний строк реалізації: *{self.critical_path_days} дн.*",
        ]
        if self.deadline_days:
            lines.append(f"📋 Дедлайн за тендером: {self.deadline_days} дн.")
        lines.append(f"🔑 Критичний шлях: _{path_names}_")
        if self.tech_defaults_used:
            lines.append(
                f"\n⚠️ *Технологічні затримки (ДБН):*\n"
                + "\n".join(
                    f"  • `{k}`: {TECH_DEFAULTS[k]['delay_days']} дн. "
                    f"({TECH_DEFAULTS[k]['source']})"
                    for k in self.tech_defaults_used if k in TECH_DEFAULTS
                )
            )
        return "\n".join(lines)


# ── Двигун CPM ───────────────────────────────────────────────────────────────

class CPMEngine:
    """
    Метод критичного шляху на основі networkx.DiGraph.

    Приклад використання:
        engine = CPMEngine()
        tasks = engine.parse_tasks_from_td(td_text, scope_items)
        result = engine.compute(tasks, deadline_days=90)
    """

    def compute(
        self,
        tasks: list[CPMTask],
        deadline_days: Optional[int] = None,
    ) -> CPMResult:
        """Розраховує критичний шлях і ризик прострочення."""
        result = CPMResult(tasks_count=len(tasks), deadline_days=deadline_days)
        result.tech_defaults_used = [t.tech_delay_source for t in tasks if t.tech_delay_source.startswith("tech_defaults")]

        if not tasks:
            result.error = "Список робіт порожній"
            return result

        try:
            G = self._build_graph(tasks)
            if not nx.is_directed_acyclic_graph(G):
                result.error = "Граф містить цикли — перевірте залежності між роботами"
                return result

            longest_path = nx.dag_longest_path(G, weight="weight")
            result.critical_path = longest_path
            result.critical_path_days = int(nx.dag_longest_path_length(G, weight="weight"))

            if deadline_days is not None:
                result.slack_days = deadline_days - result.critical_path_days
                result.deadline_risk = result.critical_path_days > deadline_days

            logger.info(
                f"CPMEngine: {len(tasks)} задач, "
                f"критичний шлях = {result.critical_path_days} дн."
            )

        except Exception as exc:
            logger.error(f"CPMEngine error: {exc}")
            result.error = str(exc)

        return result

    def _build_graph(self, tasks: list[CPMTask]) -> nx.DiGraph:
        """Будує зважений орграф. Вага ребра = total_days попередньої задачі."""
        G = nx.DiGraph()
        task_map = {t.id: t for t in tasks}

        # Додаємо вузли-задачі
        for task in tasks:
            G.add_node(task.id, duration=task.total_days)

        # Додаємо вузли-"старт" і "фініш" для зручності
        G.add_node("__START__", duration=0)
        G.add_node("__END__",   duration=0)

        for task in tasks:
            if not task.depends_on:
                G.add_edge("__START__", task.id, weight=0)
            else:
                for dep_id in task.depends_on:
                    if dep_id in task_map:
                        G.add_edge(dep_id, task.id, weight=task_map[dep_id].total_days)
                    else:
                        logger.warning(f"CPM: задача {dep_id} не знайдена (залежність {task.id})")

            # Якщо на задачу ніхто не посилається — вона кінцева
            successors = list(G.successors(task.id))
            if not successors:
                G.add_edge(task.id, "__END__", weight=task.total_days)

        return G

    def parse_tasks_from_td(
        self,
        td_text: str,
        scope_items: list[dict],
    ) -> list[CPMTask]:
        """
        Витягує задачі з тексту ТД та переліку позицій кошторису.

        scope_items приклад:
            [{"name": "Бетонування фундаменту", "volume_m3": 120, "type": "concrete_monolithic"}]

        Логіка:
          1. Спочатку шукаємо строки у тексті ТД ("строк твердіння — 21 день").
          2. Якщо не знайдено → беремо з TECH_DEFAULTS.
          3. Відмічаємо джерело затримки у CPMTask.tech_delay_source.
        """
        tasks: list[CPMTask] = []
        previous_id: Optional[str] = None

        for i, item in enumerate(scope_items):
            work_type = item.get("type", "generic")
            name      = item.get("name", f"Робота {i+1}")
            volume    = item.get("volume_m3") or item.get("volume") or 0
            task_id   = f"task_{i:03d}"

            # Тривалість роботи: виходячи з обсягу або явно вказана
            duration = item.get("duration_days") or max(1, int(volume / 20))

            # Технологічна затримка
            tech_delay, source = self._get_tech_delay(work_type, td_text)

            task = CPMTask(
                id=task_id,
                name=name,
                duration_days=duration,
                tech_delay_days=tech_delay,
                depends_on=[previous_id] if previous_id else [],
                tech_delay_source=source,
            )
            tasks.append(task)
            previous_id = task_id

        return tasks

    def _get_tech_delay(
        self,
        work_type: str,
        td_text: str,
    ) -> tuple[int, str]:
        """
        Визначає технологічну затримку.
        Returns: (days, source_label)
        """
        # Крок 1: шукаємо явні строки у ТД
        td_lower = td_text.lower()
        patterns = [
            r"(?:строк|термін)\s+тверд[іе]ння[^.]*?(\d+)\s*(?:день|дн|доба)",
            r"витримк[аи][^.]*?(\d+)\s*(?:день|дн|доба)",
            r"не\s+менш[іе]\s+(\d+)\s*(?:день|дн|доба)",
        ]
        import re
        for pat in patterns:
            m = re.search(pat, td_lower)
            if m:
                try:
                    days = int(m.group(1))
                    if 1 <= days <= 365:
                        return days, "td_extracted"
                except (ValueError, IndexError):
                    pass

        # Крок 2: беремо з TECH_DEFAULTS
        if work_type in TECH_DEFAULTS:
            return TECH_DEFAULTS[work_type]["delay_days"], f"tech_defaults:{work_type}"

        # Крок 3: нема даних → 0 затримки (не наша відповідальність)
        return 0, "no_data"
