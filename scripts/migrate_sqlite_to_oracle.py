import asyncio
import os
import sys

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.database import engine as oracle_engine, init_db, Base
from app.config import settings

# Force SQLite URL for source
SQLITE_URL = "sqlite+aiosqlite:///./local_app.db"

async def migrate():
    print("Starting migration from SQLite to Oracle...")
    
    if not settings.oracle_db_dsn:
        print("Error: ORACLE_DB_DSN is not set. Please configure Oracle settings in .env")
        return

    # 1. Ensure Oracle tables exist
    print("Creating tables in Oracle...")
    try:
        await init_db()
        print("Oracle tables created (if not existed).")
    except Exception as e:
        print(f"Error creating tables in Oracle: {e}")
        return

    # 2. Connect to SQLite
    print(f"Connecting to source SQLite: {SQLITE_URL}")
    sqlite_engine = create_async_engine(SQLITE_URL)
    
    # 3. Migrate data
    async with sqlite_engine.connect() as sqlite_conn:
        async with oracle_engine.begin() as oracle_conn:
            # Migrate data for each table defined in our models
            # We iterate in dependency order if possible, but for simple schemas simple order might work.
            # Base.metadata.sorted_tables gives a topological sort.
            
            # Step 3a: Clear existing data in REVERSE dependency order to avoid FK errors
            print("Cleaning specific Oracle tables (in reverse dependency order)...")
            tables_reversed = list(reversed(Base.metadata.sorted_tables))
            
            for table in tables_reversed:
                table_name = table.name
                try:
                    # Use delete. Truncate is faster but DDL (implicit commit), cannot rollback if later fails?
                    # Actually we are in a transaction. Delete is safer for atomicity.
                    await oracle_conn.execute(text(f"DELETE FROM {table_name}"))
                    print(f"  Cleared {table_name}")
                except Exception as e:
                    print(f"  Warning: Could not clear {table_name}: {e}")
                    # We continue, hoping it's empty or will be handled.
            
            # Step 3b: Migrate data in forward dependency order
            
            # Keep track of valid IDs for foreign keys content filtering
            # Map table_name -> set of IDs
            valid_ids = {}
            
            for table in Base.metadata.sorted_tables:
                table_name = table.name
                # Skip if table name not in tables we want? No, migrate all.
                print(f"Migrating table: {table_name}")
                
                try:
                    # Check if table exists in SQLite and has data
                    result = await sqlite_conn.execute(text(f"SELECT * FROM {table_name}"))
                    rows = result.fetchall()
                    
                    if not rows:
                        print(f"  No data found in SQLite for {table_name}")
                        # Even if empty, store empty set for reference
                        valid_ids[table_name] = set()
                        continue
                        
                    print(f"  Found {len(rows)} rows. Inserting into Oracle...")
                    
                    # Convert rows to list of dicts
                    data = [dict(row._mapping) for row in rows]
                    
                    # Fix data types: SQLite driver might return strings for DateTime, 
                    # but Oracle dialect needs datetime objects.
                    # Also handles Integer conversions (SQLite might return str).
                    import datetime
                    from sqlalchemy import DateTime, Boolean, Integer
                    
                    for col in table.columns:
                        if isinstance(col.type, DateTime):
                            # Convert string to datetime for this column in all rows
                            for row_dict in data:
                                val = row_dict.get(col.name)
                                if isinstance(val, str):
                                    try:
                                        # Handle SQLite default format 'YYYY-MM-DD HH:MM:SS.mmmmmm'
                                        if " " in val:
                                             val = val.replace(" ", "T")
                                        row_dict[col.name] = datetime.datetime.fromisoformat(val)
                                    except ValueError:
                                        print(f"    Warning: Could not parse datetime '{val}' for {col.name}")
                        elif isinstance(col.type, Boolean):
                             # Ensure booleans are python bools (sqlite might give 0/1)
                             for row_dict in data:
                                 val = row_dict.get(col.name)
                                 if val is not None:
                                     row_dict[col.name] = bool(val)
                        elif isinstance(col.type, Integer):
                             # Ensure integers are python ints (sqlite might give str)
                             for row_dict in data:
                                 val = row_dict.get(col.name)
                                 if isinstance(val, str) and val.isdigit():
                                     row_dict[col.name] = int(val)

                    # SPECIAL HANDLING FOR CONSTRAINTS:
                    # 1. chat_messages.conversation_id cannot be NULL. Filter out orphans.
                    if table_name == "chat_messages":
                        original_count = len(data)
                        # Filter NULL conversation_id
                        data = [r for r in data if r.get("conversation_id") is not None]
                        # Also filter if conversation_id points to non-existent conversation (if we tracked them)
                        if "conversations" in valid_ids:
                             valid_convs = valid_ids["conversations"]
                             data = [r for r in data if r.get("conversation_id") in valid_convs]

                        if len(data) < original_count:
                             print(f"    Filtered out {original_count - len(data)} orphan chat_messages (conversation_id is NULL or invalid)")
                    
                    # 2. chat_message_attachments.message_id cannot be NULL and must exist.
                    if table_name == "chat_message_attachments":
                        original_count = len(data)
                        # Filter NULL message_id
                        data = [r for r in data if r.get("message_id") is not None]
                        # Filter invalid message_id
                        if "chat_messages" in valid_ids:
                             valid_msgs = valid_ids["chat_messages"]
                             data = [r for r in data if r.get("message_id") in valid_msgs]
                             
                        if len(data) < original_count:
                             print(f"    Filtered out {original_count - len(data)} orphan attachments (message_id is NULL or invalid)")

                    # Store valid IDs for future reference (assuming 'id' column exists and is PK)
                    # Most tables have 'id' PK
                    if data and "id" in data[0]:
                         valid_ids[table_name] = set(r["id"] for r in data)
                    else:
                         valid_ids[table_name] = set()

                    if data:
                        # Insert in batches if large
                        batch_size = 1000
                        for i in range(0, len(data), batch_size):
                            batch = data[i:i+batch_size]
                            await oracle_conn.execute(table.insert(), batch)
                            print(f"    Inserted batch {i}-{min(i+batch_size, len(data))}")
                            
                    print(f"  Successfully migrated {table_name}")
                        
                except Exception as e:
                    print(f"  Error migrating {table_name}: {e}")
                    # If table doesn't exist in SQLite, we skip
                    if "no such table" in str(e).lower():
                        print(f"  Skipping {table_name} (not found in SQLite)")
                    else:
                        print(f"  Critical error for {table_name}, stopping.")
                        raise e
    
    print("Migration complete.")
    await sqlite_engine.dispose()
    await oracle_engine.dispose()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(migrate())
