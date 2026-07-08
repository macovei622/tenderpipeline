"""
db/database.py — Універсальний адаптер бази даних (SQLite / PostgreSQL)

Підтримує SQLite за замовчуванням для розробки та MVP.
Підтримує PostgreSQL (через asyncpg) при зміні DB_TYPE у конфігурації.
"""
from __future__ import annotations

import aiosqlite
from typing import Optional, Any
from loguru import logger
from config import DB_TYPE, DATABASE_PATH, DATABASE_URL


# ─── Контекстний менеджер підключення ──────────────────────────────────────────

class DbConnection:
    """
    Асинхронний контекстний менеджер для підключення до БД.
    Вибирає SQLite або PostgreSQL залежно від налаштувань.
    """

    def __init__(self):
        self.db_type = DB_TYPE
        self.sqlite_conn: Optional[aiosqlite.Connection] = None
        self.pg_conn: Optional[Any] = None

    async def __aenter__(self) -> DbConnection:
        if self.db_type == "postgres":
            import asyncpg
            try:
                self.pg_conn = await asyncpg.connect(DATABASE_URL)
            except Exception as e:
                logger.error(f"❌ Помилка підключення до PostgreSQL: {e}")
                raise e
            return self
        else:
            self.sqlite_conn = await aiosqlite.connect(DATABASE_PATH)
            self.sqlite_conn.row_factory = aiosqlite.Row
            return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.sqlite_conn:
            await self.sqlite_conn.close()
        if self.pg_conn:
            await self.pg_conn.close()

    def _translate(self, query: str) -> str:
        """Транслює SQLite діалект у PostgreSQL за потреби."""
        if self.db_type != "postgres":
            return query

        # Заміна ? на $1, $2... для asyncpg
        count = 1
        translated = ""
        for char in query:
            if char == '?':
                translated += f"${count}"
                count += 1
            else:
                translated += char

        # Заміна типів даних та ключових слів
        translated = translated.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        translated = translated.replace("datetime('now')", "CURRENT_TIMESTAMP")
        
        # INSERT OR IGNORE -> INSERT ... ON CONFLICT DO NOTHING
        if "INSERT OR IGNORE" in query:
            translated = translated.replace("INSERT OR IGNORE", "INSERT")
            if "seen_tenders" in query:
                translated += " ON CONFLICT (prozorro_id) DO NOTHING"
            elif "clients" in query:
                translated += " ON CONFLICT (telegram_id) DO NOTHING"
        
        return translated

    async def execute(self, query: str, params: tuple = ()) -> Any:
        """Виконує запис (INSERT, UPDATE, DELETE)."""
        q = self._translate(query)
        if self.db_type == "postgres":
            # Для INSERT в Postgres повертаємо останній згенерований ID
            if "INSERT INTO tenders" in query:
                q += " RETURNING id"
                return await self.pg_conn.fetchval(q, *params)
            elif "INSERT INTO clients" in query:
                q += " RETURNING id"
                return await self.pg_conn.fetchval(q, *params)
            
            return await self.pg_conn.execute(q, *params)
        else:
            cursor = await self.sqlite_conn.execute(q, params)
            await self.sqlite_conn.commit()
            return cursor

    async def fetchone(self, query: str, params: tuple = ()) -> Optional[dict]:
        """Повертає один рядок у вигляді словника."""
        q = self._translate(query)
        if self.db_type == "postgres":
            row = await self.pg_conn.fetchrow(q, *params)
            return dict(row) if row else None
        else:
            cursor = await self.sqlite_conn.execute(q, params)
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def fetchall(self, query: str, params: tuple = ()) -> list[dict]:
        """Повертає список рядків у вигляді словників."""
        q = self._translate(query)
        if self.db_type == "postgres":
            rows = await self.pg_conn.fetch(q, *params)
            return [dict(r) for r in rows]
        else:
            cursor = await self.sqlite_conn.execute(q, params)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# ─── Функції ініціалізації та запитів ─────────────────────────────────────────

async def init_db():
    """Створює таблиці, якщо вони відсутні."""
    async with DbConnection() as db:
        if db.db_type == "postgres":
            # Схема для PostgreSQL
            await db.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                company_name TEXT,
                contact_name TEXT,
                phone TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            await db.execute("""
            CREATE TABLE IF NOT EXISTS tenders (
                id SERIAL PRIMARY KEY,
                client_id INTEGER REFERENCES clients(id),
                prozorro_id TEXT,
                tender_title TEXT,
                procuring_entity TEXT,
                amount REAL,
                deadline TEXT,
                status TEXT DEFAULT 'new',
                scan_result TEXT,
                notes TEXT,
                success_fee_amount REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            await db.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id SERIAL PRIMARY KEY,
                tender_id INTEGER REFERENCES tenders(id),
                action TEXT,
                actor TEXT DEFAULT 'system',
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            await db.execute("""
            CREATE TABLE IF NOT EXISTS seen_tenders (
                prozorro_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            await db.execute("""
            CREATE TABLE IF NOT EXISTS outreach_leads (
                prozorro_id TEXT,
                title TEXT,
                amount REAL,
                procuring_entity TEXT,
                winner_name TEXT,
                winner_amount REAL,
                disqualified_name TEXT,
                disqualified_edrpou TEXT,
                disqualified_amount REAL,
                diff_amount REAL,
                status TEXT DEFAULT 'new',
                target_region TEXT,
                target_cpv TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (prozorro_id, disqualified_edrpou)
            );
            """)
            await db.execute("""
            CREATE TABLE IF NOT EXISTS otp_codes (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                code TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            await db.execute("""
            CREATE TABLE IF NOT EXISTS offer_acceptances (
                telegram_id BIGINT PRIMARY KEY,
                accepted_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
        else:
            # Схема для SQLite
            async with db.sqlite_conn.cursor() as cursor:
                await db.sqlite_conn.executescript("""
                CREATE TABLE IF NOT EXISTS clients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    company_name TEXT,
                    contact_name TEXT,
                    phone TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS tenders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id INTEGER REFERENCES clients(id),
                    prozorro_id TEXT,
                    tender_title TEXT,
                    procuring_entity TEXT,
                    amount REAL,
                    deadline TEXT,
                    status TEXT DEFAULT 'new',
                    scan_result TEXT,
                    notes TEXT,
                    success_fee_amount REAL,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER REFERENCES tenders(id),
                    action TEXT,
                    actor TEXT DEFAULT 'system',
                    details TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS seen_tenders (
                    prozorro_id TEXT PRIMARY KEY,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS outreach_leads (
                    prozorro_id TEXT,
                    title TEXT,
                    amount REAL,
                    procuring_entity TEXT,
                    winner_name TEXT,
                    winner_amount REAL,
                    disqualified_name TEXT,
                    disqualified_edrpou TEXT,
                    disqualified_amount REAL,
                    diff_amount REAL,
                    status TEXT DEFAULT 'new',
                    director_name TEXT,
                    email TEXT,
                    phone TEXT,
                    target_region TEXT,
                    target_cpv TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (prozorro_id, disqualified_edrpou)
                );
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS otp_codes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        telegram_id BIGINT NOT NULL,
                        code TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        used_at TEXT,
                        created_at TEXT DEFAULT (datetime('now'))
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS offer_acceptances (
                        telegram_id BIGINT PRIMARY KEY,
                        accepted_at TEXT NOT NULL,
                        created_at TEXT DEFAULT (datetime('now'))
                    )
                """)
        logger.info("✅ База даних ініціалізована")
        
        # Запускаємо міграцію колонок профілю
        columns = [
            ("edrpou", "TEXT"),
            ("director_name", "TEXT"),
            ("director_title", "TEXT DEFAULT 'Директор'"),
            ("profile_json", "TEXT"),
            ("is_profile_complete", "INTEGER DEFAULT 0")
        ]
        for col_name, col_type in columns:
            try:
                await db.execute(f"ALTER TABLE clients ADD COLUMN {col_name} {col_type}")
                logger.info(f"⚙️ Міграція: додано колонку {col_name} до clients")
            except Exception:
                pass
                
        # Міграція billing_scheme
        try:
            await db.execute("ALTER TABLE clients ADD COLUMN billing_scheme TEXT DEFAULT 'success_fee'")
            logger.info("⚙️ Міграція: додано колонку billing_scheme до clients")
        except Exception:
            pass
                
        # Міграція для tenders
        try:
            await db.execute("ALTER TABLE tenders ADD COLUMN is_fee_paid INTEGER DEFAULT 0")
            logger.info("⚙️ Міграція: додано колонку is_fee_paid до tenders")
        except Exception:
            pass

        # Міграція для outreach_leads (target_region та target_cpv)
        for col in ["target_region", "target_cpv"]:
            try:
                await db.execute(f"ALTER TABLE outreach_leads ADD COLUMN {col} TEXT")
                logger.info(f"⚙️ Міграція: додано колонку {col} до outreach_leads")
            except Exception:
                pass


async def get_or_create_client(telegram_id: int, **kwargs) -> dict:
    """Знаходить або створює клієнта за telegram_id."""
    async with DbConnection() as db:
        row = await db.fetchone("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,))
        if row:
            return row

        await db.execute(
            "INSERT INTO clients (telegram_id, company_name, contact_name) VALUES (?, ?, ?)",
            (telegram_id, kwargs.get("company_name"), kwargs.get("contact_name"))
        )
        row = await db.fetchone("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,))
        return row or {}


async def update_client_profile(telegram_id: int, **kwargs) -> None:
    """Оновлює профільні дані клієнта."""
    async with DbConnection() as db:
        fields = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [telegram_id]
        await db.execute(
            f"UPDATE clients SET {fields} WHERE telegram_id = ?",
            tuple(values)
        )


async def get_client_by_telegram_id(telegram_id: int) -> Optional[dict]:
    """Повертає клієнта за його telegram_id."""
    async with DbConnection() as db:
        return await db.fetchone("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,))


async def create_tender(client_id: int, prozorro_id: str, **kwargs) -> int:
    """Створює новий запис тендера. Повертає ID запису."""
    async with DbConnection() as db:
        res = await db.execute(
            """INSERT INTO tenders 
               (client_id, prozorro_id, tender_title, procuring_entity, amount, deadline, status)
               VALUES (?, ?, ?, ?, ?, ?, 'analyzing')""",
            (client_id, prozorro_id,
             kwargs.get("title"), kwargs.get("procuring_entity"),
             kwargs.get("amount"), kwargs.get("deadline"))
        )
        if db.db_type == "postgres":
            return res  # RETURNING id повертає саме ціле число
        else:
            return res.lastrowid


async def update_tender(tender_id: int, **kwargs):
    """Оновлює поля тендера."""
    async with DbConnection() as db:
        fields = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [tender_id]
        await db.execute(
            f"UPDATE tenders SET {fields}, updated_at = datetime('now') WHERE id = ?",
            tuple(values)
        )


async def get_client_tenders(client_id: int) -> list[dict]:
    """Повертає останні 10 тендерів клієнта."""
    async with DbConnection() as db:
        return await db.fetchall(
            "SELECT * FROM tenders WHERE client_id = ? ORDER BY created_at DESC LIMIT 10",
            (client_id,)
        )


async def log_action(tender_id: int, action: str, actor: str = "system", details: str = ""):
    """Додає запис в лог дій."""
    async with DbConnection() as db:
        await db.execute(
            "INSERT INTO activity_log (tender_id, action, actor, details) VALUES (?, ?, ?, ?)",
            (tender_id, action, actor, details)
        )


async def is_tender_seen(prozorro_id: str) -> bool:
    """Перевіряє, чи тендер вже був надісланий у моніторингу (дедуплікація)."""
    async with DbConnection() as db:
        row = await db.fetchone("SELECT 1 FROM seen_tenders WHERE prozorro_id = ?", (prozorro_id,))
        return row is not None


async def mark_tender_as_seen(prozorro_id: str):
    """Позначає тендер як надісланий."""
    async with DbConnection() as db:
        await db.execute("INSERT OR IGNORE INTO seen_tenders (prozorro_id) VALUES (?)", (prozorro_id,))


# ─── OTP / Акцепт публічної оферти ────────────────────────────────────────────

async def generate_and_save_otp(telegram_id: int) -> str:
    """
    Генерує 6-значний OTP-код, зберігає в БД (термін дії 15 хвилин),
    анулює всі попередні активні коди для цього telegram_id.
    Повертає рядок з кодом.
    Підстава: ст. 12 ЗУ «Про електронну комерцію» — акцепт оферти
    через одноразовий код прирівнюється до письмової форми договору.
    """
    import secrets
    import datetime
    code = str(secrets.randbelow(900_000) + 100_000)  # 6 цифр, ніколи не менше 100000
    expires_at = (datetime.datetime.utcnow() + datetime.timedelta(minutes=15)).isoformat()

    async with DbConnection() as db:
        # Анулюємо старі невикористані коди цього клієнта
        await db.execute(
            "UPDATE otp_codes SET used_at = datetime('now') WHERE telegram_id = ? AND used_at IS NULL",
            (telegram_id,)
        )
        await db.execute(
            "INSERT INTO otp_codes (telegram_id, code, expires_at) VALUES (?, ?, ?)",
            (telegram_id, code, expires_at)
        )
    return code


async def verify_otp(telegram_id: int, code: str) -> bool:
    """
    Перевіряє OTP-код: він має бути невикористаним і не простроченим.
    При успіху позначає код як використаний (used_at = now).
    Повертає True при успіху, False при будь-якій помилці.
    """
    import datetime
    now = datetime.datetime.utcnow().isoformat()
    async with DbConnection() as db:
        row = await db.fetchone(
            """SELECT id FROM otp_codes
               WHERE telegram_id = ? AND code = ?
                 AND used_at IS NULL AND expires_at > ?
               ORDER BY created_at DESC LIMIT 1""",
            (telegram_id, code, now)
        )
        if not row:
            return False
        await db.execute(
            "UPDATE otp_codes SET used_at = datetime('now') WHERE id = ?",
            (row["id"],)
        )
        return True


async def has_accepted_offer(telegram_id: int) -> bool:
    """Перевіряє, чи клієнт вже акцептував публічну оферту."""
    async with DbConnection() as db:
        row = await db.fetchone(
            "SELECT 1 FROM offer_acceptances WHERE telegram_id = ?",
            (telegram_id,)
        )
        return row is not None


async def record_offer_acceptance(telegram_id: int) -> None:
    """
    Записує факт акцепту оферти клієнтом.
    Якщо клієнт вже акцептував — оновлює дату (повторний акцепт при зміні умов).
    """
    async with DbConnection() as db:
        await db.execute(
            """INSERT INTO offer_acceptances (telegram_id, accepted_at)
               VALUES (?, datetime('now'))
               ON CONFLICT(telegram_id) DO UPDATE SET accepted_at = datetime('now')""",
            (telegram_id,)
        )


async def get_matching_leads(region: str, cpv: str) -> list[dict]:
    """
    Знаходить раніше збережених лідів, чиї профілі (область та CPV-код)
    збігаються з новим тендером.
    """
    async with DbConnection() as db:
        cpv_prefix = cpv[:3] if cpv else "45"
        # Шукаємо збіг за першими 3 символами CPV-коду (наприклад, 452xxxxx)
        # та перевіряємо входження назви регіону ліда у регіон нового тендера
        return await db.fetchall(
            """SELECT * FROM outreach_leads
               WHERE ? LIKE '%' || target_region || '%'
                 AND target_cpv LIKE ? || '%'""",
            (region, cpv_prefix)
        )
