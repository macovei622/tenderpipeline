"""
tests/test_cpm_engine.py — Unit тести для CPMEngine
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analytics.cpm_engine import CPMEngine, CPMTask, CPMResult, TECH_DEFAULTS


class TestCPMTaskTotalDays:
    def test_total_days_simple(self):
        t = CPMTask(id="t1", name="Роботи", duration_days=5, tech_delay_days=28)
        assert t.total_days == 33

    def test_total_days_no_delay(self):
        t = CPMTask(id="t2", name="Роботи", duration_days=10)
        assert t.total_days == 10


class TestTechDefaultsSource:
    """Тестує правильність вибору джерела затримки."""

    engine = CPMEngine()

    def test_td_extracted_wins(self):
        """Якщо ТД явно вказує строк — беремо звідти, не з TECH_DEFAULTS."""
        td_text = "Строк тверднення бетону — не менше 21 день від дати укладання."
        days, source = self.engine._get_tech_delay("concrete_monolithic", td_text)
        assert source == "td_extracted"
        assert days == 21

    def test_fallback_to_defaults(self):
        """Якщо ТД нічого не каже → беремо з TECH_DEFAULTS."""
        td_text = "Загальний опис об'єкту без часових норм."
        days, source = self.engine._get_tech_delay("concrete_monolithic", td_text)
        assert source == "tech_defaults:concrete_monolithic"
        assert days == TECH_DEFAULTS["concrete_monolithic"]["delay_days"]

    def test_unknown_type_no_data(self):
        """Невідомий тип роботи без ТД-строків → 0 затримки."""
        td_text = ""
        days, source = self.engine._get_tech_delay("some_exotic_work", td_text)
        assert source == "no_data"
        assert days == 0


class TestCPMCompute:
    """Тестує розрахунок критичного шляху."""

    engine = CPMEngine()

    def _make_linear_tasks(self, n: int, duration: int = 5, delay: int = 0) -> list[CPMTask]:
        """Створює ланцюжок з n задач."""
        tasks = []
        for i in range(n):
            tasks.append(CPMTask(
                id=f"t{i}",
                name=f"Задача {i}",
                duration_days=duration,
                tech_delay_days=delay,
                depends_on=[f"t{i-1}"] if i > 0 else [],
            ))
        return tasks

    def test_linear_chain(self):
        """3 задачі по 10 днів → критичний шлях = 30 днів."""
        tasks = self._make_linear_tasks(3, duration=10, delay=0)
        result = self.engine.compute(tasks)
        assert result.critical_path_days == 30
        assert result.error is None

    def test_with_tech_delay(self):
        """2 задачі: 5 днів + 28 днів затримки, потім 3 дні → разом 36."""
        tasks = [
            CPMTask("t0", "Бетон", 5, 28, []),
            CPMTask("t1", "Штукатурка", 3, 0, ["t0"]),
        ]
        result = self.engine.compute(tasks)
        # t0.total_days=33, t1.duration=3 → 33+3=36
        assert result.critical_path_days == 36

    def test_deadline_ok(self):
        tasks = self._make_linear_tasks(2, duration=10)
        result = self.engine.compute(tasks, deadline_days=30)
        assert result.deadline_risk is False
        assert result.slack_days == 10

    def test_deadline_risk(self):
        tasks = self._make_linear_tasks(4, duration=10)  # 40 днів
        result = self.engine.compute(tasks, deadline_days=30)
        assert result.deadline_risk is True
        assert result.slack_days == -10

    def test_empty_tasks(self):
        result = self.engine.compute([])
        assert result.error is not None

    def test_parallel_branches(self):
        """Паралельні гілки → критичний шлях = довша гілка."""
        tasks = [
            CPMTask("start", "Підготовка",   5, 0, []),
            CPMTask("a1",    "Гілка А (20)", 20, 0, ["start"]),
            CPMTask("b1",    "Гілка Б (10)", 10, 0, ["start"]),
            CPMTask("end",   "Фінал",        2, 0, ["a1", "b1"]),
        ]
        result = self.engine.compute(tasks)
        # start(5) + a1(20) + end(2) = 27 > start(5) + b1(10) + end(2) = 17
        assert result.critical_path_days == 27

    def test_summary_text_risk(self):
        tasks = self._make_linear_tasks(5, duration=10)  # 50 днів
        result = self.engine.compute(tasks, deadline_days=30)
        text = result.summary_text()
        assert "РИЗИК" in text
        assert "20" in text  # бракує 20 днів

    def test_summary_text_ok(self):
        tasks = self._make_linear_tasks(2, duration=10)
        result = self.engine.compute(tasks, deadline_days=40)
        text = result.summary_text()
        assert "Укладається в строк" in text


class TestTechDefaults:
    """Перевіряє що всі TECH_DEFAULTS мають required поля."""

    def test_all_defaults_have_source(self):
        for key, val in TECH_DEFAULTS.items():
            assert "delay_days" in val, f"{key}: відсутнє delay_days"
            assert "source" in val, f"{key}: відсутнє source (посилання на ДБН)"
            assert val["delay_days"] > 0, f"{key}: delay_days має бути > 0"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
