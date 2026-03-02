import asyncio
import os
import dotenv
dotenv.load_dotenv()
from app.database import engine, Base

async def main():
    async with engine.begin() as conn:
        print("Dropping tables...")
        await conn.run_sync(Base.metadata.drop_all)
        print("Tables dropped.")

asyncio.run(main())
