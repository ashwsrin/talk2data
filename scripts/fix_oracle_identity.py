import asyncio
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.config import settings

async def fix_oracle_identity():
    if not settings.oracle_db_dsn:
        print("Error: ORACLE_DB_DSN not set")
        return

    print(f"Connecting to Oracle DSN: {settings.oracle_db_dsn}")
    
    params = {
        "user": settings.oracle_db_user,
        "password": settings.oracle_db_password,
        "dsn": settings.oracle_db_dsn,
    }
    database_url = "oracle+oracledb://{user}:{password}@{dsn}".format(**params)

    # Create engine directly here to avoid circular imports if any
    connect_args = {}
    if settings.oracle_wallet_path:
        connect_args = {
            "config_dir": settings.oracle_wallet_path,
            "wallet_location": settings.oracle_wallet_path,
            "wallet_password": settings.oracle_wallet_password,
        }

    engine = create_async_engine(
        database_url,
        echo=False,
        connect_args=connect_args
    )

    tables_to_fix = [
        "conversations",
        "chat_messages",
        "chat_message_attachments",
        "model_configs",
        "mcp_servers"
    ]

    async with engine.begin() as conn:
        for table in tables_to_fix:
            print(f"Checking table: {table}")
            try:
                # 0. Check data
                result = await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
                count = result.scalar()
                print(f"  Row Count: {count}")

                # 1. Get current max ID
                result = await conn.execute(text(f"SELECT MAX(id) FROM {table}"))
                max_id = result.scalar()
                if max_id is None:
                    max_id = 0
                
                next_val = max_id + 1
                print(f"  Current Max ID: {max_id}. Setting sequence start to: {next_val}")

                # 2. Add Auto-Increment via Sequence + Trigger (Robust Fallback)
                
                # Drop existing Identity property if possible (to avoid conflict with trigger logic?)
                # Actually, no need to drop default if Trigger overrides it.
                # But trigger overrides NULL. If column has default/identity, trigger fires FIRST? Or AFTER?
                # BEFORE INSERT Trigger fires before constraints.
                
                seq_name = f"{table}_id_seq".upper()
                # Truncate trigger name if too long (Oracle limit is 128 chars but safe to keep short)
                trg_name = f"{table}_id_trg".upper()
                if len(seq_name) > 30: seq_name = seq_name[:30] # Limit for old Oracle, modern 128. Safe.
                if len(trg_name) > 30: trg_name = trg_name[:30]

                # Create Sequence
                try:
                    await conn.execute(text(f"CREATE SEQUENCE {seq_name} START WITH {next_val} INCREMENT BY 1 NOCACHE NOCYCLE"))
                    print(f"  Created sequence {seq_name} (start={next_val})")
                except Exception as e:
                    if "ORA-00955" in str(e): # name is already used by an existing object
                        print(f"  Sequence {seq_name} exists. Recreating...")
                        try:
                            await conn.execute(text(f"DROP SEQUENCE {seq_name}"))
                            await conn.execute(text(f"CREATE SEQUENCE {seq_name} START WITH {next_val} INCREMENT BY 1 NOCACHE NOCYCLE"))
                            print(f"  Recreated sequence {seq_name} (start={next_val})")
                        except Exception as e2:
                             print(f"  Failed to recreate sequence: {e2}")
                    else:
                        print(f"  Failed to create sequence: {e}")

                # Create Trigger
                # Logic: IF :new.id IS NULL THEN select sequence.nextval
                try:
                     # Standard SQL syntax for create trigger in Oracle
                     # Use \: to escape bind parameters in SQLAlchemy text()
                     sql = f"""
                     CREATE OR REPLACE TRIGGER {trg_name}
                     BEFORE INSERT ON {table}
                     FOR EACH ROW
                     BEGIN
                         IF :new.id IS NULL THEN
                             :new.id := {seq_name}.nextval;
                         END IF;
                     END;
                     """
                     # Replace : with \: for SQLAlchemy if it parses binds
                     # Actually, text() binds parameters. We need to escape them.
                     sql = sql.replace(":new", "\:new") 
                     
                     await conn.execute(text(sql))
                     print(f"  Created trigger {trg_name}")
                except Exception as e:
                     print(f"  Failed to create trigger: {e}")

            except Exception as e:
                print(f"Error processing {table}: {e}")

    await engine.dispose()
    print("Fix script completed.")

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(fix_oracle_identity())
