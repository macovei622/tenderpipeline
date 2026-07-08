"""
ai/client.py — Єдиний HTTP-клієнт для всіх моделей через OpenRouter

OpenRouter — це OpenAI-сумісний API.
Підключення: просто міняємо base_url і api_key.
Ніякого vendor lock-in — модель можна змінити одним рядком.

Особливості:
- Автоматичний fallback: якщо основна модель недоступна → резервна
- Structured JSON output через response_format (підтримується більшістю моделей)
- Підрахунок витрат по кожному запиту
- Retry з exponential backoff (захист від rate limit)
"""
import json
import asyncio
import time
from typing import Optional, Any
from loguru import logger
from openai import AsyncOpenAI, APIStatusError, APIConnectionError, RateLimitError

from config import OPENROUTER_API_KEY
from ai.models import ModelConfig, get_model_by_key, MODELS, estimate_cost

# ── Ініціалізація клієнта ────────────────────────────────────────
_client: Optional[AsyncOpenAI] = None


def get_client() -> AsyncOpenAI:
    """Singleton AsyncOpenAI клієнт для OpenRouter."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
            default_headers={
                # OpenRouter рекомендує передавати site і назву проекту
                "HTTP-Referer": "https://tender-service.vinnytsia.ua",
                "X-Title": "TenderAI Vinnytsia",
            }
        )
    return _client


# ── Основна функція виклику ──────────────────────────────────────

async def call_model(
    model_key: str,
    messages: list[dict],
    json_mode: bool = False,
    retries: int = 3,
    fallback_enabled: bool = True,
) -> Optional[tuple[str, dict]]:
    """
    Викликає модель через OpenRouter з автоматичним fallback і retry.

    Args:
        model_key:        Ключ з реєстру моделей (наприклад "deepseek-r1")
        messages:         Список повідомлень у форматі OpenAI
        json_mode:        Якщо True — просимо модель відповісти строгим JSON
        retries:          Кількість спроб при помилці
        fallback_enabled: Якщо True — при невдачі пробуємо резервну модель

    Returns:
        (text, usage_info) або None при помилці
    """
    model_cfg = get_model_by_key(model_key)
    return await _call_with_retry(model_cfg, messages, json_mode, retries, fallback_enabled)


async def _call_with_retry(
    model_cfg: ModelConfig,
    messages: list[dict],
    json_mode: bool,
    retries: int,
    fallback_enabled: bool,
) -> Optional[tuple[str, dict]]:
    """Внутрішня функція з retry логікою."""
    client = get_client()
    last_error = None

    for attempt in range(retries):
        try:
            kwargs: dict[str, Any] = {
                "model": model_cfg.id,
                "messages": messages,
                "temperature": model_cfg.temperature,
                "max_tokens": model_cfg.max_tokens,
            }

            # JSON mode — якщо модель підтримує
            if json_mode and model_cfg.supports_json:
                kwargs["response_format"] = {"type": "json_object"}

            logger.debug(f"🤖 [{model_cfg.name}] Запит (спроба {attempt + 1}/{retries})")
            start_time = time.time()

            response = await client.chat.completions.create(**kwargs)

            elapsed = round(time.time() - start_time, 2)
            
            # Безпечне отримання choices
            if not response or not getattr(response, "choices", None):
                raise ValueError("API returned an empty response or missing choices field (likely internal provider error)")
            
            content = response.choices[0].message.content or ""

            # Безпечне отримання використання токенів (об'єкт або словник)
            usage = getattr(response, "usage", None)
            
            def get_usage_tokens(u_obj, key: str) -> int:
                if not u_obj:
                    return 0
                if isinstance(u_obj, dict):
                    return int(u_obj.get(key) or 0)
                return int(getattr(u_obj, key, 0) or 0)

            prompt_tokens = get_usage_tokens(usage, "prompt_tokens")
            completion_tokens = get_usage_tokens(usage, "completion_tokens")

            # Підрахунок витрат
            model_key = next((k for k, v in MODELS.items() if v.id == model_cfg.id), None)
            cost = estimate_cost(
                prompt_tokens,
                completion_tokens,
                model_key=model_key,
            ) if model_key else 0.0

            usage_info = {
                "model": model_cfg.name,
                "model_id": model_cfg.id,
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
                "cost_usd": cost,
                "elapsed_sec": elapsed,
            }

            logger.info(
                f"✅ [{model_cfg.name}] Відповідь за {elapsed}с | "
                f"Токени: {usage_info['input_tokens']}↑ {usage_info['output_tokens']}↓ | "
                f"Вартість: ${cost:.4f}"
            )

            return content, usage_info

        except RateLimitError as e:
            wait_time = 2 ** attempt  # exponential backoff: 1, 2, 4 сек
            logger.warning(f"⏳ Rate limit [{model_cfg.name}]. Чекаємо {wait_time}с...")
            last_error = e
            await asyncio.sleep(wait_time)

        except APIStatusError as e:
            if e.status_code in (500, 502, 503, 504):
                wait_time = 2 ** attempt
                logger.warning(f"⚠️ Сервер [{model_cfg.name}] помилка {e.status_code}. Чекаємо {wait_time}с...")
                last_error = e
                await asyncio.sleep(wait_time)
            elif e.status_code == 400 and json_mode:
                logger.warning(f"⚠️ Помилка 400 від [{model_cfg.name}]. Можливо, провайдер не підтримує JSON format. Вимикаємо response_format і пробуємо знову...")
                json_mode = False  # вимикаємо для наступних спроб
                last_error = e
                continue
            else:
                logger.error(f"❌ [{model_cfg.name}] API помилка: {e.status_code} — {e.message}")
                last_error = e
                break  # Не повторюємо при 4xx помилках

        except APIConnectionError as e:
            logger.error(f"❌ Не вдалося підключитися до OpenRouter: {e}")
            last_error = e
            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"❌ Несподівана помилка [{model_cfg.name}]: {e}")
            last_error = e
            break

    # Усі спроби вичерпані — пробуємо fallback
    if fallback_enabled and model_cfg.fallback:
        fallback_key = model_cfg.fallback
        fallback_cfg = get_model_by_key(fallback_key)
        logger.warning(
            f"🔄 Fallback: {model_cfg.name} → {fallback_cfg.name} "
            f"(причина: {type(last_error).__name__})"
        )
        return await _call_with_retry(fallback_cfg, messages, json_mode, retries=2, fallback_enabled=False)

    logger.error(f"❌ Всі спроби вичерпані для [{model_cfg.name}]")
    return None


# ── Утиліти ─────────────────────────────────────────────────────

def parse_json_response(text: str) -> Optional[dict]:
    """
    Безпечний парсинг JSON з відповіді моделі.
    Обробляє markdown-обгортку типу ```json ... ```.
    """
    if not text:
        return None

    # Видаляємо markdown-блоки
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Прибираємо першу і останню строки (``` і ```)
        cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Шукаємо JSON-об'єкт всередині тексту
        import re
        json_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                pass

    logger.error(f"❌ Не вдалося розпарсити JSON:\n{text[:300]}")
    return None


def build_messages(system_prompt: str, user_content: str) -> list[dict]:
    """Будує стандартний список повідомлень для API."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
