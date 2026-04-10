"""
ai_handler.py
© 2026 Fayaz Ahmed Shaik. All rights reserved.
─────────────
Handles all AI intelligence:
  - Maintains per-user conversation memory (in-memory dict)
  - Detects basic intent (greeting, question, farewell, etc.)
  - Sends prompts to Groq LLM and returns the response
  - Applies a friendly, human-like assistant personality

All free – uses Groq's generous free tier (no credit card required).
Sign up at: https://console.groq.com/
"""

import os
import logging
from collections import defaultdict
from datetime import datetime
from groq import Groq

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
#  Groq client – uses GROQ_API_KEY from .env automatically
# ──────────────────────────────────────────────────────────────
_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
_ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Fury AI")

# ──────────────────────────────────────────────────────────────
#  System prompt – personality & instructions for the LLM
#  Injected at the start of every conversation.
# ──────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = f"""
You are {_ASSISTANT_NAME}, a warm, friendly, and highly intelligent AI voice assistant.
Your job is to help users via voice and text messages – just like a helpful personal assistant.

Personality guidelines:
- Be conversational, empathetic, and concise (keep replies under 3 sentences when possible).
- Use natural spoken language – avoid bullet points, markdown, or headers.
- Show personality: be warm and occasionally witty, but always professional.
- If you don't know something, say so honestly rather than making things up.
- Adapt your tone to the user's mood (if they sound stressed, be calming).

Important: Your replies will be converted to audio, so respond as you would speak – naturally.
Today's date is {datetime.now().strftime("%A, %B %d, %Y")}.
""".strip()

# ──────────────────────────────────────────────────────────────
#  Per-user conversation memory
#  Key: platform user_id (int or str)
#  Value: list of {"role": "user"|"assistant", "content": str}
#
#  Note: This is in-memory only. Memory is lost on bot restart.
#  For persistence, swap this dict with a SQLite/Redis store.
# ──────────────────────────────────────────────────────────────
_memory: dict[str | int, list[dict]] = defaultdict(list)

# How many past messages to keep per user (controls context window)
_MAX_HISTORY_PAIRS = 10  # 10 pairs = 20 messages kept


# ──────────────────────────────────────────────────────────────
#  Intent Detection  (rule-based, no ML needed)
# ──────────────────────────────────────────────────────────────

_INTENT_PATTERNS: dict[str, list[str]] = {
    "creator": ["who is your creator", "who created you", "who made you", "who is your developer"],
    "greeting": ["hi", "hello", "hey", "good morning", "good afternoon", "good evening", "howdy", "sup"],
    "farewell": ["bye", "goodbye", "see you", "take care", "later", "ciao", "gotta go"],
    "gratitude": ["thank", "thanks", "thank you", "appreciate", "cheers"],
    "help": ["help", "can you", "could you", "assist", "support", "i need"],
    "question": ["what", "when", "where", "why", "how", "who", "which", "?"],
    "affirmation": ["yes", "yeah", "yep", "sure", "okay", "ok", "absolutely", "of course"],
    "negation": ["no", "nope", "nah", "not really", "i don't think so"],
}


def detect_intent(text: str) -> str:
    """
    Returns the dominant intent category of the user's message.
    Uses simple keyword matching — no ML required.

    Args:
        text: Raw transcribed user input.

    Returns:
        Intent label (e.g., 'greeting', 'question', 'unknown').
    """
    text_lower = text.lower()
    for intent, keywords in _INTENT_PATTERNS.items():
        if any(kw in text_lower for kw in keywords):
            return intent
    return "unknown"


# ──────────────────────────────────────────────────────────────
#  Memory helpers
# ──────────────────────────────────────────────────────────────

def get_history(session_id: str) -> list[dict]:
    """Returns the stored conversation history for a specific session."""
    return _memory[session_id]


def add_to_history(session_id: str, role: str, content: str) -> None:
    """
    Appends a message to the session's history and trims older messages.
    """
    _memory[session_id].append({"role": role, "content": content})

    # Trim: keep only the most recent N exchanges (2 msgs per exchange)
    max_messages = _MAX_HISTORY_PAIRS * 2
    if len(_memory[session_id]) > max_messages:
        _memory[session_id] = _memory[session_id][-max_messages:]


def load_history_to_memory(session_id: str, messages: list[dict]) -> None:
    """
    Pre-populates the in-memory context from the database history.
    Messages should be a list of {"role": "user"|"assistant", "message": "..."}.
    """
    if session_id in _memory and len(_memory[session_id]) > 0:
        return # Already loaded or active
    
    formatted = []
    for m in messages:
        # Convert DB 'assistant' role to AI handler's 'assistant'
        role = 'assistant' if m['role'] in ['assistant', 'ai'] else 'user'
        formatted.append({"role": role, "content": m['message']})
    
    _memory[session_id] = formatted
    logger.info(f"Loaded {len(formatted)} messages into memory for session {session_id}")


def clear_history(session_id: str) -> None:
    """Wipes conversation memory for a session."""
    _memory[session_id] = []
    logger.info(f"Memory cleared for session {session_id}.")


def generate_session_title(user_text: str) -> str:
    """
    Generates a very short (3-5 word) title for the conversation based on the first input.
    """
    try:
        prompt = f"Generate a 3 to 4 word title for a conversation that starts with: '{user_text}'. Return ONLY the title, no quotes or punctuation."
        response = _client.chat.completions.create(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=20,
            temperature=0.5
        )
        title = response.choices[0].message.content.strip()
        # Clean up in case LLM added quotes
        title = title.replace('"', '').replace("'", "")
        return title
    except Exception as e:
        logger.error(f"Title generation failed: {e}")
        return "New Conversation"


# ──────────────────────────────────────────────────────────────
#  Main AI response function
# ──────────────────────────────────────────────────────────────

def generate_response(session_id: str, user_text: str) -> str:
    """
    Generates an AI response using Groq LLM, with full conversation memory.
    Now keyed by session_id instead of user_id.
    """
    intent = detect_intent(user_text)
    logger.info(f"Session {session_id} | Intent: {intent} | Input: '{user_text[:80]}'")

    if intent == "creator":
        creator_reply = "My creator is Fayaz Ahmed, His screen name is Fury So he named me Fury"
        add_to_history(session_id, "user", user_text)
        add_to_history(session_id, "assistant", creator_reply)
        return creator_reply

    # Store user's message in memory
    add_to_history(session_id, "user", user_text)

    # Build the full message list for the API call
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}] + get_history(session_id)

    try:
        response = _client.chat.completions.create(
            model=_MODEL,
            messages=messages,
            max_tokens=150,       # Short = faster + better for voice
            temperature=0.75,     # Balanced creativity vs. consistency
            top_p=0.9,
        )

        reply = response.choices[0].message.content.strip()
        logger.info(f"AI reply for session {session_id}: '{reply[:80]}'")

        # Store assistant's reply in memory for next turn
        add_to_history(session_id, "assistant", reply)

        return reply

    except Exception as e:
        logger.error(f"LLM call failed for session {session_id}: {e}", exc_info=True)
        # Friendly fallback so the bot doesn't go silent on errors
        return "I'm sorry, I ran into a little hiccup. Could you try saying that again?"
