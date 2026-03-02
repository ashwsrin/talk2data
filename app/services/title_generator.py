from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_oci import ChatOCIGenAI
from oci_openai import OciOpenAI, OciUserPrincipalAuth
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, Conversation
from app.agent import OPENAI_MODEL_ID, oci_response_to_aimessage

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a helpful assistant. Generate a concise, professional title (3-5 words max) "
    "for a data analysis conversation based on the user's initial query. Do not use quotes. "
    "Do not be chatty. Output ONLY the title."
)


def _clean_title(raw: str) -> str:
    s = (raw or "").strip()
    # Remove surrounding quotes/backticks if the model includes them.
    s = re.sub(r"^[\"'`]+|[\"'`]+$", "", s).strip()
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    # Safety cap
    if len(s) > 100:
        s = s[:100].rstrip()
    return s


def _build_title_llm() -> Optional[ChatOCIGenAI]:
    """
    Build a non-streaming ChatOCIGenAI client for background title generation.

    Auth uses standard OCI config/profile. Model ID is read from env:
    - OCI_GENAI_MODEL_ID (preferred)
    """
    # Ensure auth env vars are present (same pattern as app/agent.py).
    os.environ["OCI_CONFIG_FILE"] = settings.oci_config_file
    os.environ["OCI_CONFIG_PROFILE"] = settings.oci_profile

    model_id = (os.environ.get("OCI_GENAI_MODEL_ID") or "").strip()
    if not model_id:
        return None

    return ChatOCIGenAI(
        model_id=model_id,
        compartment_id=settings.compartment_id,
        model_kwargs={"temperature": 0.3},
        is_stream=False,
    )


def _generate_title_sync(user_message: str) -> str:
    llm = _build_title_llm()
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", _SYSTEM_PROMPT),
            ("human", "{user_message}"),
        ]
    )
    if llm is not None:
        chain = prompt | llm
        res = chain.invoke({"user_message": user_message})
        content = getattr(res, "content", None)
        return _clean_title(content if isinstance(content, str) else str(content or ""))

    # Fallback: use OCI OpenAI Responses API (same auth/config as app/agent.py)
    try:
        client = OciOpenAI(
            region=settings.region,
            auth=OciUserPrincipalAuth(
                config_file=settings.oci_config_file, profile_name=settings.oci_profile
            ),
            compartment_id=settings.compartment_id,
        )
        input_list = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        resp = client.responses.create(
            model=OPENAI_MODEL_ID,
            input=input_list,
            store=False,
            stream=False,
            max_output_tokens=32,
            temperature=0.3,
        )
        ai = oci_response_to_aimessage(resp)
        return _clean_title(ai.content or "")
    except Exception:
        return ""


async def generate_and_save_conversation_title(conversation_id: int, user_message: str) -> None:
    """
    Background task: generate a short title from the first user message and update DB.

    Creates its own DB session (do not pass request session).
    Runs LLM invocation in a thread to avoid blocking the event loop.
    """
    msg = (user_message or "").strip()
    if not msg:
        return
    try:
        title = await asyncio.to_thread(_generate_title_sync, msg)
        title = _clean_title(title)
        if not title:
            return

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Conversation).where(Conversation.id == conversation_id))
            conversation = result.scalar_one_or_none()
            if conversation is None:
                logger.warning("Conversation not found for title update: id=%s", conversation_id)
                return
            conversation.title = title
            await db.commit()
        logger.info("Title updated to: %s", title)
    except Exception:
        logger.exception("Title generation failed for conversation_id=%s", conversation_id)

