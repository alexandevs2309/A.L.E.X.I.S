import os
from contextlib import asynccontextmanager

from psycopg.rows import dict_row

from alexis.storage import schema

DEFAULT_DSN = os.environ.get(
    "ALEXIS_DATABASE_URL",
    "postgresql://alexis:change-me@localhost:5433/alexis",
)


class Database:
    def __init__(self, dsn: str = DEFAULT_DSN):
        self.dsn = dsn
        self._pool = None

    async def open(self):
        from psycopg_pool import AsyncConnectionPool

        if self._pool is None:
            self._pool = AsyncConnectionPool(self.dsn, min_size=1, max_size=5, open=False)
            await self._pool.open()
        elif self._pool.closed:
            await self._pool.open()
        await self.execute("SELECT 1")

    async def close(self):
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def execute(self, query, params=None):
        async with self._pool.connection() as conn:
            await conn.execute(query, params)

    async def fetch(self, query, params=None):
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(query, params)
                return await cur.fetchall()

    @asynccontextmanager
    async def transaction(self):
        """Un ÚNICO contexto transaccional, tomado del driver.

        `execute()` abre y cierra una conexión del pool por sentencia, así que varias
        llamadas NO son atómicas. Para lo que debe serlo —guardar las entidades y sus
        relaciones del World Model como una unidad— hace falta una conexión fija y la
        transacción que el propio driver ofrece. No es un sistema de transacciones
        paralelo: es el de psycopg, expuesto una vez.

        Uso:
            async with db.transaction() as conn:
                await conn.execute(sql, params)
        """
        async with self._pool.connection() as conn:
            async with conn.transaction():
                yield conn

    async def migrate(self):
        await self.open()
        await self.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await self.execute(schema.SQL)