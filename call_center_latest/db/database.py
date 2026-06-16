import asyncpg, os
from dotenv import load_dotenv
load_dotenv()

_pool: asyncpg.Pool | None = None


async def init_db():
    global _pool
    _pool = await asyncpg.create_pool(
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT")),
        min_size=5,
        max_size=20,
    )
    print("✅ DB pool initialized")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialized. Call init_db() first.")
    return _pool


async def close_db():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
