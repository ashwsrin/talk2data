import asyncio
import os
import dotenv
dotenv.load_dotenv()
from app.database import engine
from sqlalchemy import text

async def main():
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT DBMS_METADATA.GET_DDL('TABLE', 'CONVERSATIONS') FROM DUAL"))
        print(res.scalar())

asyncio.run(main())
