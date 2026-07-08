"""
ai/models.py — Реєстр моделей і принцип їх призначення

АРХІТЕКТУРА: Кожен агент = окрема роль + найкраща для цієї ролі модель

Аналогія: будівля мосту.
- Сканер = Інженер-аналітик (DeepSeek R1 — найкращий у логічному аналізі)
- Калькулятор = Кошторисник (Qwen 2.5 72B — математика + structured output)
- Збирач = Нотаріус (Claude Haiku 4.5 — точне копіювання шаблонів)
- Перевіряючий = Прокурор (Claude Sonnet 4.5 — знаходить протиріччя)
- Роутер = Диспетчер (вибирає модель залежно від задачі і бюджету)

Fallback-ланцюг: якщо основна модель недоступна → пробуємо резервну.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ModelConfig:
    """Конфігурація однієї моделі."""
    id: str              # OpenRouter model ID (наприклад "deepseek/deepseek-r1")
    name: str            # Людська назва
    role: str            # Для чого використовується
    temperature: float   # Температура: 0.0 = детермінований, 1.0 = творчий
    max_tokens: int      # Максимум токенів у відповіді
    context_window: int  # Контекстне вікно (токени)
    cost_per_1m_input: float   # $ за 1M вхідних токенів
    cost_per_1m_output: float  # $ за 1M вихідних токенів
    supports_json: bool = True   # Підтримує JSON mode
    fallback: Optional[str] = None  # ID резервної моделі


# ═══════════════════════════════════════════════════════════════════
# РЕЄСТР МОДЕЛЕЙ
# Оновлений на підставі openrouter.ai/models (липень 2026)
# ═══════════════════════════════════════════════════════════════════

MODELS: dict[str, ModelConfig] = {

    # ── РІВЕНЬ 1: АНАЛІЗ І ЛОГІКА (для Сканера і Перевіряючого) ──────
    #
    # DeepSeek R1 — reinforcement learning, chain-of-thought reasoning
    # Найкращий для: виявлення прихованих протиріч, юридичного аналізу
    # Особливість: "думає вголос" перед відповіддю (reasoning tokens)
    "deepseek-r1": ModelConfig(
        id="deepseek/deepseek-r1",
        name="DeepSeek R1 (Reasoning)",
        role="Глибокий логічний аналіз, юридичне мислення",
        temperature=0.0,
        max_tokens=8192,
        context_window=128_000,
        cost_per_1m_input=0.55,
        cost_per_1m_output=2.19,
        fallback="claude-sonnet-4-5",
    ),

    # Claude Sonnet 4.5 (внутрішній ключ: claude-sonnet-4-5)
    # Найкращий для: фінальний критичний аудит, роль Перевіряючого
    "claude-sonnet-4-5": ModelConfig(
        id="anthropic/claude-sonnet-4-5",
        name="Claude 3.7/4.5 Sonnet",
        role="Гібридний: глибоке розуміння + якісний текст",
        temperature=0.1,
        max_tokens=8192,
        context_window=200_000,
        cost_per_1m_input=3.00,
        cost_per_1m_output=15.00,
        fallback="deepseek-r1",
    ),

    # ── РІВЕНЬ 2: СТРУКТУРОВАНА ОБРОБКА ДАНИХ (для Калькулятора) ─────
    #
    # Qwen 2.5 72B — математика, structured output, таблиці
    # Найкращий для: розрахунок собівартості, робота з числами та JSON
    "qwen-2.5-72b": ModelConfig(
        id="qwen/qwen-2.5-72b-instruct",
        name="Qwen 2.5 72B",
        role="Математика, структуровані розрахунки, JSON-вивід",
        temperature=0.0,  # Детермінований! Для математики варіативність неприпустима
        max_tokens=4096,
        context_window=131_072,
        cost_per_1m_input=0.35,
        cost_per_1m_output=0.40,
        fallback="claude-haiku-4-5",
    ),

    # Mistral Large — надійний, відмінна підтримка structured output
    "mistral-large": ModelConfig(
        id="mistralai/mistral-large-2411",
        name="Mistral Large 2411",
        role="Резервна для структурованих задач",
        temperature=0.0,
        max_tokens=4096,
        context_window=131_072,
        cost_per_1m_input=2.00,
        cost_per_1m_output=6.00,
        fallback="qwen-2.5-72b",
    ),

    # ── РІВЕНЬ 3: ГЕНЕРАЦІЯ ДОКУМЕНТІВ (для Збирача) ─────────────────
    #
    # Claude Haiku 4.5 (внутрішній ключ: claude-haiku-4-5)
    # Найкращий для: заповнення довідок за суворими шаблонами
    "claude-haiku-4-5": ModelConfig(
        id="anthropic/claude-haiku-4.5",
        name="Claude Haiku 4.5",
        role="Генерація документів за шаблонами, точне форматування",
        temperature=0.0,  # Детермінований!
        max_tokens=4096,
        context_window=200_000,
        cost_per_1m_input=0.80,
        cost_per_1m_output=4.00,
        fallback="gemini-2-5-pro",
    ),

    # ── РІВЕНЬ 4: ВЕЛИКИЙ КОНТЕКСТ (для великих ТД 300+ стор.) ───────
    #
    # Gemini 2.5 Pro — 1M токенів контекстного вікна, довгі документи
    # Найкращий для: аналіз ТД де весь документ треба тримати в контексті
    "gemini-2-5-pro": ModelConfig(
        id="google/gemini-2.5-pro",
        name="Gemini 2.5 Pro (Long Context)",
        role="Аналіз дуже довгих документів (до 1M токенів)",
        temperature=0.1,
        max_tokens=8192,
        context_window=1_000_000,
        cost_per_1m_input=1.25,
        cost_per_1m_output=10.00,
        fallback="deepseek-r1",
    ),

    # ── РІВЕНЬ 5: ШВИДКО І ДЕШЕВО (скринінг, класифікація) ───────────
    #
    # Gemini 2.0 Flash — наш "workhorse" для швидких задач
    "gemini-2-flash": ModelConfig(
        id="google/gemini-2.0-flash",
        name="Gemini 2.0 Flash",
        role="Швидкий скринінг, класифікація, прості задачі",
        temperature=0.1,
        max_tokens=4096,
        context_window=1_000_000,
        cost_per_1m_input=0.10,
        cost_per_1m_output=0.40,
        fallback="qwen-2.5-72b",
    ),

    # Llama 3.1 70B — безкоштовний на OpenRouter (є ліміт)
    "llama-3-1-70b-free": ModelConfig(
        id="meta-llama/llama-3.1-70b-instruct:free",
        name="Llama 3.1 70B (Free tier)",
        role="Безкоштовний fallback для тестування",
        temperature=0.1,
        max_tokens=2048,
        context_window=131_072,
        cost_per_1m_input=0.0,
        cost_per_1m_output=0.0,
        fallback=None,
    ),
}


# ═══════════════════════════════════════════════════════════════════
# ПРИЗНАЧЕННЯ МОДЕЛЕЙ ПО АГЕНТАХ
# ═══════════════════════════════════════════════════════════════════

AGENT_MODELS = {
    # Сканер: знаходить дискримінаційні вимоги
    # → DeepSeek R1 (логічне мислення, юридичний аналіз)
    # → Fallback: Claude Sonnet 4.5
    "scanner": "deepseek-r1",

    # Калькулятор: розраховує собівартість і маржу
    # → Qwen 2.5 72B (математика + строгий JSON)
    # → Fallback: Mistral Large
    "calculator": "qwen-2.5-72b",

    # Збирач: заповнює довідки за шаблонами
    # → Claude Haiku 4.5 (точне слідування шаблону)
    # → Fallback: Mistral Large
    "collector": "claude-haiku-4-5",

    # Перевіряючий: критичний аудит всього пакету
    # → Claude Sonnet 4.5 (найкращий у знаходженні протиріч)
    # → Fallback: DeepSeek R1
    "reviewer": "claude-sonnet-4-5",

    # Великі документи (300+ сторінок):
    # → Gemini 2.5 Pro (1M токенів)
    "large_document": "gemini-2-5-pro",

    # Скринінг (чи взагалі варто аналізувати тендер):
    # → Gemini 2.0 Flash (швидко і дешево)
    "screener": "gemini-2-flash",
}


def get_model(agent_name: str) -> ModelConfig:
    """Повертає конфігурацію моделі для вказаного агента."""
    model_key = AGENT_MODELS.get(agent_name, "gemini-2-flash")
    return MODELS[model_key]


def get_model_by_key(key: str) -> ModelConfig:
    """Повертає конфігурацію моделі за ключем реєстру або назвою агента."""
    actual_key = AGENT_MODELS.get(key, key)
    return MODELS.get(actual_key, MODELS["gemini-2-flash"])


def estimate_cost(input_tokens: int, output_tokens: int, model_key: str) -> float:
    """Розраховує орієнтовну вартість запиту в USD."""
    model = get_model_by_key(model_key)
    cost = (input_tokens / 1_000_000 * model.cost_per_1m_input +
            output_tokens / 1_000_000 * model.cost_per_1m_output)
    return round(cost, 6)
