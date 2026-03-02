"""
CLI script to delete conversations (and their messages and attachments) older than N days.
Run from project root: uv run python scripts/cleanup_old_conversations.py [older_than_days]
Default: 7 days (by updated_at).
"""
import asyncio
import sys
from datetime import datetime, timedelta

from sqlalchemy import select

from app.database import AsyncSessionLocal, Conversation


async def main(older_than_days: int = 7) -> int:
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Conversation).where(Conversation.updated_at < cutoff)
        )
        conversations = result.scalars().all()
        for conversation in conversations:
            await db.delete(conversation)
        await db.commit()
        return len(conversations)


if __name__ == "__main__":
    days = 7
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            print("Usage: python scripts/cleanup_old_conversations.py [older_than_days]", file=sys.stderr)
            sys.exit(1)
    deleted = asyncio.run(main(older_than_days=days))
    print(f"Deleted {deleted} conversation(s) older than {days} days.")
