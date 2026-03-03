from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, text, LargeBinary, Identity
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from datetime import datetime
import os

from app.config import settings

# Create async engine
if settings.oracle_db_dsn and settings.oracle_db_user:
    import oracledb
    
    # Enable thin mode (default in python-oracledb 2.0+, but strict here)
    # For ADB mTLS, we need wallet_location in connect_args or init_oracle_client(lib_dir=...) if using thick mode.
    # Thin mode supports wallets too.
    
    print(f"[DATABASE] Using Oracle Autonomous Database: {settings.oracle_db_dsn}")
    
    # Construct SQLAlchemy URL for async oracledb
    # format: oracle+oracledb_async://user:password@dsn
    db_url = f"oracle+oracledb_async://{settings.oracle_db_user}:{settings.oracle_db_password}@{settings.oracle_db_dsn}"
    
    # Connection arguments
    connect_args = {}
    if settings.oracle_wallet_path:
        connect_args["wallet_location"] = settings.oracle_wallet_path
        connect_args["config_dir"] = settings.oracle_wallet_path
    if settings.oracle_wallet_password:
        connect_args["wallet_password"] = settings.oracle_wallet_password
        
    engine = create_async_engine(
        db_url,
        echo=False,
        connect_args=connect_args,
        pool_pre_ping=True,     # Test connections before use (detects stale ones)
        pool_size=10,           # Keep 10 connections ready in the pool
        max_overflow=10,        # Allow up to 10 additional connections under burst load
        pool_recycle=1800,      # Recycle connections every 30 min (keeps them alive longer)
        pool_timeout=10,        # Wait up to 10s for a connection from the pool before erroring
    )
else:
    print(f"[DATABASE] Using SQLite: {settings.database_url}")
    engine = create_async_engine(
        settings.database_url,
        echo=False,
    )

# Create async session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# Base class for declarative models
class Base(DeclarativeBase):
    pass


# MCPServer model
class MCPServer(Base):
    __tablename__ = "mcp_servers"
    
    id = Column(Integer, Identity(start=1000), primary_key=True)
    name = Column(String(255), nullable=False)
    transport_type = Column(String(50), nullable=False, default='sse')  # 'sse', 'stdio', or 'streamable_http'
    url = Column(String(1024), nullable=True)  # For SSE and streamable_http
    api_key = Column(String(1024), nullable=True)  # Only needed for SSE
    is_active = Column(Boolean, default=True, nullable=False)
    exclude_optional_params = Column(Boolean, default=False, nullable=False)
    include_in_llm = Column(Boolean, default=True, nullable=False)
    system_instruction = Column(Text, nullable=True)
    # stdio connection params
    command = Column(String(512), nullable=True)
    args = Column(Text, nullable=True) # JSON array of args
    env_vars = Column(Text, nullable=True)  # JSON dict of env vars
    cwd = Column(String(1024), nullable=True)
    # OAuth 2.0 Client Credentials (optional; used instead of api_key when set)
    oauth2_access_token_url = Column(String(1024), nullable=True)
    oauth2_client_id = Column(String(255), nullable=True)
    oauth2_client_secret = Column(String(1024), nullable=True)
    oauth2_scope = Column(String(1024), nullable=True)


# Conversation model
class Conversation(Base):
    __tablename__ = "conversations"
    
    id = Column(Integer, Identity(start=1000), primary_key=True)
    title = Column(String(255), nullable=False, default="New Conversation")
    user_name = Column(String(255), nullable=False, default="User")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationship to messages
    messages = relationship("ChatMessage", back_populates="conversation", cascade="all, delete-orphan")


# ChatMessage model
class ChatMessage(Base):
    __tablename__ = "chat_messages"
    
    id = Column(Integer, Identity(start=1000), primary_key=True)
    role = Column(String(50), nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    execution_metadata = Column(Text, nullable=True)  # JSON: prompt_messages, tool_calls
    
    # Relationship to conversation
    conversation = relationship("Conversation", back_populates="messages")
    attachments = relationship(
        "ChatMessageAttachment",
        back_populates="message",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ChatMessageAttachment(Base):
    __tablename__ = "chat_message_attachments"

    id = Column(Integer, Identity(start=1000), primary_key=True)
    message_id = Column(
        Integer,
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mime_type = Column(Text, nullable=False)
    data_bytes = Column(LargeBinary, nullable=False)
    thumbnail_bytes = Column(LargeBinary, nullable=True)
    file_name = Column(Text, nullable=True)  # Optional display name (e.g. for PDFs)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    message = relationship("ChatMessage", back_populates="attachments")


# SQL Sandbox: persisted versions (original + user edits) per message code block
class SqlSandboxVersion(Base):
    __tablename__ = "sql_sandbox_versions"

    id = Column(Integer, Identity(start=1000), primary_key=True)
    message_id = Column(
        Integer,
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number = Column(Integer, nullable=False, default=0)  # 0 = original
    sql_query = Column(Text, nullable=False)
    results_json = Column(Text, nullable=True)   # JSON string
    analysis = Column(Text, nullable=True)        # LLM markdown
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# MCP tool visibility: which tools are sent to the LLM (sanitized full name -> enabled)
class MCPToolVisibility(Base):
    __tablename__ = "mcp_tool_visibility"
    
    tool_name = Column(String(255), primary_key=True)  # sanitized full name e.g. SalesDB_connect
    enabled = Column(Boolean, default=True, nullable=False)


# MCP tool description override: custom description per tool (sanitized full name -> description)
class MCPToolDescriptionOverride(Base):
    __tablename__ = "mcp_tool_description_override"
    
    tool_name = Column(String(255), primary_key=True)  # sanitized full name e.g. SalesDB_connect
    description = Column(Text, nullable=False)



# App settings: key-value store for configurable app settings (api_base_url, cors_origins, etc.)
class AppSetting(Base):
    __tablename__ = "app_settings"
    
    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=True)





# Initialize database - create tables if they don't exist
async def init_db():
    """Create all tables if they don't exist and migrate schema if needed."""
    try:
        async with engine.begin() as conn:
            # Create tables if they don't exist
            await conn.run_sync(Base.metadata.create_all)
            
            # Migrate existing mcp_servers table to add new columns
            await migrate_mcp_servers_table(conn)
            # Migrate chat_messages table to add execution_metadata if needed
            await migrate_chat_messages_table(conn)
            # Create chat_message_attachments table if it doesn't exist
            await migrate_chat_message_attachments_table(conn)
            # Add file_name column to chat_message_attachments if missing
            await migrate_chat_message_attachments_file_name(conn)
            # Create mcp_tool_visibility table if it doesn't exist
            await migrate_mcp_tool_visibility_table(conn)
            # Create mcp_tool_description_override table if it doesn't exist
            await migrate_mcp_tool_description_override_table(conn)
            # Create mcp_tool_approval_config table if it doesn't exist
            await migrate_mcp_tool_approval_config_table(conn)
            # Create app_settings table if it doesn't exist
            await migrate_app_settings_table(conn)
        # Seed app_settings defaults (outside conn so we can use async session)
        await seed_app_settings_defaults_v2()
        print("[INIT_DB] Database initialization and migration completed")
    except Exception as e:
        print(f"[INIT_DB] Error during database initialization: {e}")
        import traceback
        traceback.print_exc()
        raise


async def migrate_mcp_servers_table(conn):
    """Add new columns to mcp_servers table if they don't exist, and fix url column nullability."""
    try:
        # Get table info using run_sync
        def get_table_info(sync_conn):
            inspector = inspect(sync_conn)
            if 'mcp_servers' not in inspector.get_table_names():
                return None
            columns = inspector.get_columns('mcp_servers')
            return {col['name']: col for col in columns}
        
        # Check if table exists and get existing columns
        existing_columns_dict = await conn.run_sync(get_table_info)
        
        if existing_columns_dict is None:
            print("[MIGRATION] mcp_servers table does not exist yet, will be created by create_all")
            return  # Table doesn't exist yet, create_all will handle it
        
        existing_columns = list(existing_columns_dict.keys())
        print(f"[MIGRATION] Existing columns in mcp_servers: {existing_columns}")
        
        # List of new columns to add
        new_columns = [
            ('transport_type', 'TEXT NOT NULL DEFAULT \'sse\''),
            ('exclude_optional_params', 'BOOLEAN NOT NULL DEFAULT 0'),
            ('include_in_llm', 'BOOLEAN NOT NULL DEFAULT 1'),
            ('system_instruction', 'TEXT'),
            ('command', 'VARCHAR(512)'),
            ('args', 'TEXT'),
            ('env_vars', 'TEXT'),
            ('cwd', 'VARCHAR(1024)'),
            ('oauth2_access_token_url', 'TEXT'),
            ('oauth2_client_id', 'TEXT'),
            ('oauth2_client_secret', 'TEXT'),
            ('oauth2_scope', 'TEXT'),
        ]
        
        # Just add missing columns
        added_count = 0
        for col_name, col_def in new_columns:
            if col_name not in existing_columns:
                try:
                    # Oracle syntax is ALTER TABLE name ADD column_name type
                    # SQLite supports ALTER TABLE name ADD COLUMN column_name type
                    # ADD column_name is standard and works in both Oracle and SQLite
                    await conn.execute(text(f'ALTER TABLE mcp_servers ADD {col_name} {col_def}'))
                    print(f"[MIGRATION] Added column {col_name} to mcp_servers table")
                    added_count += 1
                except Exception as e:
                    print(f"[MIGRATION] Warning: Could not add column {col_name}: {e}")
                    import traceback
                    traceback.print_exc()
        
        if added_count == 0:
            print("[MIGRATION] All required columns already exist in mcp_servers table")
        else:
            print(f"[MIGRATION] Migration complete: added {added_count} column(s)")

    except Exception as e:
        print(f"[MIGRATION] Error during migration: {e}")
        import traceback
        traceback.print_exc()
        # Don't raise - allow the app to start even if migration fails
        # The error will be caught when trying to use the columns


async def migrate_chat_messages_table(conn):
    """Add execution_metadata column to chat_messages if it doesn't exist."""
    try:
        def get_chat_messages_columns(sync_conn):
            inspector = inspect(sync_conn)
            if "chat_messages" not in inspector.get_table_names():
                return None
            columns = inspector.get_columns("chat_messages")
            return {col["name"]: col for col in columns}

        existing = await conn.run_sync(get_chat_messages_columns)
        if existing is None:
            return
        if "execution_metadata" not in existing:
            await conn.execute(text("ALTER TABLE chat_messages ADD COLUMN execution_metadata TEXT"))
            print("[MIGRATION] Added column execution_metadata to chat_messages table")
        else:
            print("[MIGRATION] chat_messages.execution_metadata already exists")
    except Exception as e:
        print(f"[MIGRATION] migrate_chat_messages_table: {e}")
        import traceback
        traceback.print_exc()


async def migrate_chat_message_attachments_table(conn):
    """Create chat_message_attachments table if it doesn't exist."""
    try:
        def table_exists(sync_conn):
            inspector = inspect(sync_conn)
            return "chat_message_attachments" in inspector.get_table_names()

        if not await conn.run_sync(table_exists):
            await conn.run_sync(
                lambda sync_conn: ChatMessageAttachment.__table__.create(sync_conn, checkfirst=True)
            )
            print("[MIGRATION] Created chat_message_attachments table")
        else:
            print("[MIGRATION] chat_message_attachments table already exists")
    except Exception as e:
        print(f"[MIGRATION] migrate_chat_message_attachments_table: {e}")
        import traceback
        traceback.print_exc()


async def migrate_chat_message_attachments_file_name(conn):
    """Add file_name column to chat_message_attachments if it doesn't exist."""
    try:
        def get_columns(sync_conn):
            inspector = inspect(sync_conn)
            if "chat_message_attachments" not in inspector.get_table_names():
                return []
            return [c["name"] for c in inspector.get_columns("chat_message_attachments")]

        existing = await conn.run_sync(get_columns)
        if "file_name" not in existing and existing:
            await conn.execute(text("ALTER TABLE chat_message_attachments ADD COLUMN file_name TEXT"))
            print("[MIGRATION] Added file_name to chat_message_attachments")
        elif existing:
            print("[MIGRATION] chat_message_attachments.file_name already exists")
    except Exception as e:
        print(f"[MIGRATION] migrate_chat_message_attachments_file_name: {e}")
        import traceback
        traceback.print_exc()


async def migrate_mcp_tool_visibility_table(conn):
    """Create mcp_tool_visibility table if it doesn't exist."""
    try:
        def table_exists(sync_conn):
            inspector = inspect(sync_conn)
            return "mcp_tool_visibility" in inspector.get_table_names()

        if not await conn.run_sync(table_exists):
            await conn.run_sync(lambda sync_conn: MCPToolVisibility.__table__.create(sync_conn, checkfirst=True))
            print("[MIGRATION] Created mcp_tool_visibility table")
        else:
            print("[MIGRATION] mcp_tool_visibility table already exists")
    except Exception as e:
        print(f"[MIGRATION] migrate_mcp_tool_visibility_table: {e}")
        import traceback
        traceback.print_exc()


async def migrate_mcp_tool_description_override_table(conn):
    """Create mcp_tool_description_override table if it doesn't exist."""
    try:
        def table_exists(sync_conn):
            inspector = inspect(sync_conn)
            return "mcp_tool_description_override" in inspector.get_table_names()

        if not await conn.run_sync(table_exists):
            await conn.run_sync(lambda sync_conn: MCPToolDescriptionOverride.__table__.create(sync_conn, checkfirst=True))
            print("[MIGRATION] Created mcp_tool_description_override table")
        else:
            print("[MIGRATION] mcp_tool_description_override table already exists")
    except Exception as e:
        print(f"[MIGRATION] migrate_mcp_tool_description_override_table: {e}")
        import traceback
        traceback.print_exc()


async def migrate_mcp_tool_approval_config_table(conn):
    """Create mcp_tool_approval_config table if it doesn't exist."""
    try:
        def table_exists(sync_conn):
            inspector = inspect(sync_conn)
            return "mcp_tool_approval_config" in inspector.get_table_names()

        if not await conn.run_sync(table_exists):
            await conn.run_sync(lambda sync_conn: MCPToolApprovalConfig.__table__.create(sync_conn, checkfirst=True))
            print("[MIGRATION] Created mcp_tool_approval_config table")
        else:
            print("[MIGRATION] mcp_tool_approval_config table already exists")
    except Exception as e:
        print(f"[MIGRATION] migrate_mcp_tool_approval_config_table: {e}")
        import traceback
        traceback.print_exc()


async def migrate_app_settings_table(conn):
    """Create app_settings table if it doesn't exist."""
    try:
        def table_exists(sync_conn):
            inspector = inspect(sync_conn)
            return "app_settings" in inspector.get_table_names()

        if not await conn.run_sync(table_exists):
            await conn.run_sync(lambda sync_conn: AppSetting.__table__.create(sync_conn, checkfirst=True))
            print("[MIGRATION] Created app_settings table")
        else:
            print("[MIGRATION] app_settings table already exists")
    except Exception as e:
        print(f"[MIGRATION] migrate_app_settings_table: {e}")
        import traceback
        traceback.print_exc()




async def seed_app_settings_defaults_v2():
    """Seed default app_settings if rows are missing (system_prompt)."""
    from sqlalchemy import select, text
    async with AsyncSessionLocal() as session:
        try:
            # Check if system_prompt setting exists
            result = await session.execute(
                text("SELECT 1 FROM app_settings WHERE key = 'system_prompt'")
            )
            if result.scalar() is not None:
                print("[INIT_DB] app_settings: system_prompt already exists.")
                return

            print("[INIT_DB] app_settings: seeding default system_prompt...")
            await session.execute(
                text("INSERT INTO app_settings (key, value) VALUES (:key, :value)"),
                {"key": "system_prompt", "value": ""}
            )
            await session.commit()
            print("[INIT_DB] Seeded system_prompt")
            
        except Exception as e:
            await session.rollback()
            print(f"[INIT_DB] Warning: could not seed app_settings: {e}")


# Dependency to get database session
async def get_db():
    """Dependency function to get database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

