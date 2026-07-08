import pytest
import asyncio
from db.database import (
    init_db,
    generate_and_save_otp,
    verify_otp,
    has_accepted_offer,
    record_offer_acceptance,
)

@pytest.mark.asyncio
async def test_otp_flow():
    # 1. Ініціалізуємо БД
    await init_db()
    
    telegram_id = 999111222
    
    # 2. Перевіряємо, що спочатку оферта не прийнята
    accepted_before = await has_accepted_offer(telegram_id)
    assert accepted_before is False
    
    # 3. Генеруємо OTP-код
    code = await generate_and_save_otp(telegram_id)
    assert len(code) == 6
    assert code.isdigit()
    
    # 4. Перевіряємо неправильний код
    wrong_verify = await verify_otp(telegram_id, "000000")
    assert wrong_verify is False
    
    # 5. Перевіряємо правильний код
    correct_verify = await verify_otp(telegram_id, code)
    assert correct_verify is True
    
    # 6. Спроба використати той самий код ще раз має повернути False
    reuse_verify = await verify_otp(telegram_id, code)
    assert reuse_verify is False
    
    # 7. Фіксуємо акцепт оферти
    await record_offer_acceptance(telegram_id)
    accepted_after = await has_accepted_offer(telegram_id)
    assert accepted_after is True
