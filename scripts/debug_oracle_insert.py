import asyncio
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from app.database import engine, Base, Conversation
from app.config import settings

async def debug_insert():
    print(f"Connecting to Oracle DSN: {settings.oracle_db_dsn}")
    
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    
    async with async_session() as session:
        try:
            print("Attempting to insert a test conversation...")
            # Create a conversation WITHOUT explicit ID (relying on autoincrement/identity)
            new_conv = Conversation(title="Debug Test Conversation", user_name="DebugUser")
            session.add(new_conv)
            await session.commit()
            await session.refresh(new_conv)
            print(f"Success! Inserted conversation with ID: {new_conv.id}")
            
            # Clean up
            print("Cleaning up test conversation...")
            await session.delete(new_conv)
            await session.commit()
            print("Cleanup complete.")
            
        except Exception as e:
            print(f"Error inserting conversation: {e}")
            await session.rollback()
            # Check for sequence mismatch
            if "ORA-00001" in str(e):
                print("\n[DIAGNOSIS] ORA-00001 Detected!")
                print("This likely means the Identity Column Sequence is out of sync with max(ID).")
                print("Checking Max ID vs Sequence could help, but we should probably reset sequences.")

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(debug_insert())
