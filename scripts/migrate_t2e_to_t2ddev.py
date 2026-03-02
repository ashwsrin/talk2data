import asyncio
import os
import sys

# Ensure we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import dotenv
dotenv.load_dotenv()

from app.database import init_db, engine
import oracledb
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

async def main():
    # 1. Initialize T2DDEV tables
    print("Initializing T2DDEV tables...")
    await init_db()

    # 2. Connect to T2E
    dsn = os.environ.get("ORACLE_DB_DSN")
    t2e_user = "T2E"
    t2e_password = "rT!lMni86Er29"
    wallet_path = os.environ.get("ORACLE_WALLET_PATH")
    wallet_password = os.environ.get("ORACLE_WALLET_PASSWORD")
    
    print(f"Connecting to source schema {t2e_user} on {dsn}...")
    try:
        source_conn = await oracledb.connect_async(
            user=t2e_user,
            password=t2e_password,
            dsn=dsn,
            config_dir=wallet_path,
            wallet_location=wallet_path,
            wallet_password=wallet_password
        )
    except Exception as e:
        print(f"Failed to connect to source T2E schema: {e}")
        return

    # Helper function to copy table
    async def copy_table(table_name, target_conn, exclude_cols=None, filter_condition=""):
        if exclude_cols is None:
            exclude_cols = []
            
        print(f"Migrating {table_name}...")
        
        try:
            # Get source columns
            async with source_conn.cursor() as cursor:
                await cursor.execute(f"SELECT * FROM {table_name} WHERE 1=0")
                all_cols = [col[0].lower() for col in cursor.description]
        except Exception as e:
            print(f"Table {table_name} not found or failed query in source: {e}")
            return
            
        source_cols = [c for c in all_cols if c not in exclude_cols]
        if not source_cols:
            print(f"No columns to copy for {table_name}")
            return
            
        cols_str = ", ".join(source_cols)
        query = f"SELECT {cols_str} FROM {table_name} {filter_condition}"
        
        async with source_conn.cursor() as cursor:
            await cursor.execute(query)
            rows = await cursor.fetchall()

        if not rows:
            print(f"No data in {table_name}")
            return
            
        bind_placeholders = ", ".join([f":{i+1}" for i in range(len(source_cols))])
        insert_query = f"INSERT INTO {table_name} ({cols_str}) VALUES ({bind_placeholders})"
        
        # Insert into T2DDEV using SQLAlchemy engine
        async with engine.begin() as t_conn:
            # (Deletion is handled before migration now)
            for row in rows:
                clean_row = []
                for val in row:
                    if hasattr(val, "read"):
                        if asyncio.iscoroutinefunction(val.read) or asyncio.iscoroutine(val.read()):
                            # Wait, val.read might just be a regular method returning a coroutine in early python, but asyncio.iscoroutinefunction(val.read) or inspect.iscoroutinefunction handles it. Actually, just:
                            res = val.read()
                            if asyncio.iscoroutine(res):
                                val = await res
                            else:
                                val = res
                    clean_row.append(val)
                
                binds = {str(i+1): clean_row[i] for i in range(len(clean_row))}
                try:
                    await t_conn.execute(text(insert_query), binds)
                except IntegrityError as e:
                    print(f"Warning: integrity error inserting row in {table_name}: {e}")
                except Exception as e:
                    print(f"Error inserting row in {table_name}: {e}")
                
        print(f"Migrated {len(rows)} rows for {table_name}")

    try:
        # Pre-wipe target tables in reverse dependency order to satisfy foreign keys
        tables_to_wipe = [
            "sql_sandbox_versions",
            "chat_message_attachments",
            "chat_messages",
            "conversations",

            "mcp_tool_description_override",
            "mcp_tool_visibility",
            "mcp_servers",
            "app_settings"
        ]
        print("Wiping existing data for clean import...")
        async with engine.begin() as t_conn:
            for tbl in tables_to_wipe:
                try:
                    await t_conn.execute(text(f"DELETE FROM {tbl}"))
                except Exception as e:
                    print(f"Failed to wipe {tbl}: {e}")

        # Tables to copy directly
        await copy_table("app_settings", engine)
        
        # Migrating mcp_servers with rules
        async with source_conn.cursor() as cursor:
            await cursor.execute(f"SELECT * FROM mcp_servers WHERE 1=0")
            mcp_cols = [col[0].lower() for col in cursor.description]
            
        exclude_mcp = ['command', 'args', 'env', 'cwd', 'sub_agent_group']
        filter_mcp = ""
        if 'transport_type' in mcp_cols:
            filter_mcp = "WHERE transport_type != 'stdio'"
            
        await copy_table("mcp_servers", engine, exclude_cols=exclude_mcp, filter_condition=filter_mcp)
        
        # Remaining tables
        await copy_table("mcp_tool_visibility", engine)
        await copy_table("mcp_tool_description_override", engine)

        await copy_table("conversations", engine)
        await copy_table("chat_messages", engine)
        await copy_table("chat_message_attachments", engine)
        await copy_table("sql_sandbox_versions", engine)

        print("Migration script completed successfully!")
    finally:
        await source_conn.close()

if __name__ == "__main__":
    asyncio.run(main())
