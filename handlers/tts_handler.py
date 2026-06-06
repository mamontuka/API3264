# Copyright (C) 2026
#
# Authors:
#
# Production-grade version by Oleh Mamont - https://github.com/mamontuka
#
# Based on:
# y13sint - https://github.com/y13sint
# raz0r-code - https://github.com/raz0r-code
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
"""
TTS Handler Module
==============================
Consolidated implementation of TTS functionality integrated into the main architecture.

Key Features:
- Centralized Configuration: All settings loaded from `Config` (env vars).
- Unified Temp Directory: Uses `Config.TEMP_FILES_DIR` for all temporary audio files.
- No Hardcodes: Voices, fallbacks, patterns, and silence data are strictly config-driven.
- Language Detection: Supports configurable regex patterns and explicit labels.
- Voice Caching: Per-client gender caching with TTL.
- Fallback Mechanism: Automatic voice fallback on generation errors.
- SSE Streaming: Support for Server-Sent Events streaming.
- Sanitization: Aggressive text cleaning with configurable trash words.

Integration:
- Import this module in `qwenapi.py` to expose TTS endpoints.
- Ensure `Config.ensure_dirs()` is called at startup to create temp directories.
"""

import os
import re
import json
import base64
import time
import html
import asyncio
import tempfile
from typing import Dict, List, Tuple, Optional
from pathlib import Path
import edge_tts
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# =============================================================================
# IMPORTS FROM CORE
# =============================================================================
from config import Config
import db_async

import logging
logger = logging.getLogger("FreeQwenApi")

# =============================================================================
# CONSTANTS & CONFIGURATION HELPERS
# =============================================================================

# MIME types mapping for audio response formats
AUDIO_FORMAT_MIME_TYPES = {
    "mp3": "audio/mpeg",
    "webm": "audio/webm",
    "opus": "audio/opus",
    "pcm": "audio/pcm",
    "wav": "audio/wav",
    "flac": "audio/flac"
}

# In-memory voice cache: client_key -> {"gender": str, "time": float}
# Used to persist voice gender preference per client session
VOICE_CACHE: Dict[str, dict] = {}

# =============================================================================
# CONFIGURATION LOADING
# =============================================================================
# All configuration values are loaded from Config class which reads environment variables.
# No hardcoded defaults here - everything must be defined in .env or config.py defaults.

# Load voices configuration from JSON string in env
try:
    VOICES_CONFIG = json.loads(Config.TTS_VOICES_CONFIG) if Config.TTS_VOICES_CONFIG else {}
except json.JSONDecodeError as e:
    logger.error(f"TTS: Invalid TTS_VOICES_CONFIG JSON in env: {e}")
    VOICES_CONFIG = {}

# Load fallback voices mapping from JSON string in env
try:
    FALLBACK_VOICES = json.loads(Config.TTS_FALLBACK_VOICES) if Config.TTS_FALLBACK_VOICES else {}
except json.JSONDecodeError as e:
    logger.error(f"TTS: Invalid TTS_FALLBACK_VOICES JSON in env: {e}")
    FALLBACK_VOICES = {}

# =============================================================================
# TEXT SANITIZATION
# =============================================================================

def sanitize_text(text: str) -> str:
    """
    Aggressively sanitize input text before TTS processing.

    Removes:
    - HTML tags and entities
    - URLs and links
    - CSS styles and values (if enabled)
    - Configured trash words
    - Emojis and special characters
    - Excessive whitespace

    Args:
        text: Raw input text

    Returns:
        Cleaned text safe for TTS processing
    """
    if not text:
        return ""

    if Config.DEBUG_LOGGING:
        logger.debug(f"TTS Sanitize start: len={len(text)}")

    # Decode HTML entities
    text = html.unescape(text)

    # Remove script/style/iframe tags with content
    text = re.sub(r'<\s*(script|style|iframe)[^>]*>.*?(<\s*/\s*\1\s*>|$)', '', text, flags=re.IGNORECASE | re.DOTALL)

    # Remove all remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)

    # Remove URLs
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'www\.\S+', '', text)

    # Remove CSS values if configured
    if Config.TTS_REMOVE_CSS_VALUES:
        text = re.sub(r'style\s*=\s*"[^"]*"', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\{\s*[^}]+\}', '', text)

    # Remove configured trash words
    for word in Config.TTS_TRASH_WORDS:
        text = re.sub(rf'\b{word}\b', '', text, flags=re.IGNORECASE)

    # Remove quotes
    text = re.sub(r'["\']', '', text)

    # Remove special characters except basic punctuation
    text = re.sub(r'[^\w\s.,!?;:\-\(\)\'\"«»\n]', '', text, flags=re.UNICODE)

    # Remove emojis
    text = re.sub(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF]', '', text)

    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    if Config.DEBUG_LOGGING:
        logger.debug(f"TTS Sanitize end: len={len(text)}, result='{text[:50]}...'")

    return text


def is_meaningful_text(text: str) -> bool:
    """
    Check if text contains meaningful content worth synthesizing.

    Args:
        text: Text to check

    Returns:
        True if text contains letters/words, False otherwise
    """
    if not text:
        return False

    if Config.TTS_MEANINGFUL_CHECK_LETTERS_ONLY:
        # Check for actual letters in supported alphabets
        return bool(re.search(r'[a-zA-Zа-яА-ЯёЁґҐєЄіІїЇ]', text))

    # Basic word character check
    return bool(re.search(r'\w', text))


def split_text_into_chunks(text: str, max_len: Optional[int] = None) -> List[str]:
    """
    Split text into chunks for processing, respecting sentence boundaries.

    Args:
        text: Text to split
        max_len: Maximum chunk length (defaults to Config.TTS_CHUNK_MAX_LEN)

    Returns:
        List of text chunks
    """
    if max_len is None:
        max_len = Config.TTS_CHUNK_MAX_LEN

    if not text:
        return []

    # Split by sentence endings
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current_chunk = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        if len(current_chunk) + len(sentence) <= max_len:
            current_chunk += sentence + " "
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = sentence + " "

    if current_chunk:
        chunks.append(current_chunk.strip())

    return chunks if chunks else [text[:max_len]]

# =============================================================================
# VOICE LOGIC & CACHE MANAGEMENT
# =============================================================================

async def get_last_model_gender() -> str:
    """
    Determine voice gender based on the last used model in OpenWebUI database.

    Uses the global `_db_pool` from `db_async` module directly.
    Checks model name against TTS_KEYWORDS_MALE to detect male voice preference.
    Falls back to TTS_DEFAULT_GENDER if no match, DB disabled, or pool unavailable.

    Returns:
        "male" or "female"
    """
    pool = getattr(db_async, '_db_pool', None)

    # Check if DB is enabled and pool is initialized
    if not Config.OPENWEBUI_DB_ENABLED or pool is None:
        logger.debug(f"TTS Gender: DB enabled={Config.OPENWEBUI_DB_ENABLED}, pool={pool is not None}, using default {Config.TTS_DEFAULT_GENDER}")
        return Config.TTS_DEFAULT_GENDER

    try:
        # We use the current pool
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT chat FROM chat ORDER BY updated_at DESC LIMIT 1")

            if row:
                chat_data = row['chat']

                if isinstance(chat_data, str):
                    chat_data = json.loads(chat_data)

                models = chat_data.get('models', [])

                if not models and 'messages' in chat_data:
                    messages = chat_data['messages']
                    if messages:
                        model_id = messages[-1].get('model', '')
                        if model_id:
                            models = [model_id]

                # 🔥 Log what we found
                logger.debug(f"TTS Gender check: found models={models}")

                for m in models:
                    for keyword in Config.TTS_KEYWORDS_MALE:
                        if keyword in str(m).lower():
                            logger.debug(f"TTS ✅ Male keyword '{keyword}' matched model '{m}' -> MALE")
                            return "male"

                logger.debug(f"TTS Gender: no male keywords in {models}, using {Config.TTS_DEFAULT_GENDER}")

    except Exception as e:
        logger.error(f"TTS Database error in get_last_model_gender: {e}", exc_info=True)

    return Config.TTS_DEFAULT_GENDER


def detect_language(text: str) -> str:
    """
    Detect language from text using configurable regex patterns.
    
    Priority: Ukrainian > Russian > English (default)
    Patterns are loaded from TTS_PATTERN_UK and TTS_PATTERN_RU env vars.
    
    Args:
        text: Text to analyze
        
    Returns:
        Language code: "uk", "ru", or "en"
    """
    if re.search(Config.TTS_PATTERN_UK, text):
        return "uk"
    elif re.search(Config.TTS_PATTERN_RU, text):
        return "ru"
    return "en"


def get_voice_by_gender_lang(gender: str, lang: str) -> Optional[str]:
    """
    Get voice ID for given gender and language combination.
    
    Args:
        gender: "male" or "female"
        lang: Language code ("en", "ru", "uk")
        
    Returns:
        Voice ID string or None if not configured
    """
    return VOICES_CONFIG.get(gender, {}).get(lang)


def get_speed_for_gender_lang(gender: str, lang: str, default: float = 1.0) -> float:
    """
    Get speech speed setting for gender/language combination.
    
    Looks up TTS_SPEED_{GENDER}_{LANG_SUFFIX} env var, falls back to default.
    
    Language suffix mapping:
    - "en" → "EN"
    - "ru" → "RU"  
    - "uk" → "UA"
    
    Args:
        gender: "male" or "female"
        lang: Language code ("en", "ru", "uk")
        default: Default speed value
        
    Returns:
        Speed multiplier (e.g., 1.0, 1.1)
    """
    # 🔥 Mapping languages ​​to environment variable suffixes
    lang_to_suffix = {
        "en": "EN",
        "ru": "RU",
        "uk": "UA",
    }
    
    suffix = lang_to_suffix.get(lang, lang.upper())
    key = f"TTS_SPEED_{gender.upper()}_{suffix}"
    
    env_val = os.getenv(key)
    if env_val:
        try:
            speed = float(env_val)
            logger.debug(f"TTS Speed: {key}={speed}")
            return speed
        except ValueError:
            logger.warning(f"TTS Speed: invalid value for {key}={env_val}, using default {default}")
    
    logger.debug(f"TTS Speed: {key} not found, using default {default}")
    return default


def update_voice_cache(client_key: str, gender: str):
    """
    Update voice cache with current gender for client.
    
    Also performs cleanup of expired entries based on TTS_VOICE_CACHE_TTL.
    
    Args:
        client_key: Unique client identifier
        gender: Gender to cache
    """
    current_time = time.time()
    VOICE_CACHE[client_key] = {"gender": gender, "time": current_time}
    
    # Cleanup expired entries
    ttl = Config.TTS_VOICE_CACHE_TTL
    for k in list(VOICE_CACHE.keys()):
        if current_time - VOICE_CACHE[k]["time"] > ttl:
            del VOICE_CACHE[k]


def get_cached_gender(client_key: str) -> Optional[str]:
    """
    Retrieve cached gender for client if TTL not expired.
    
    Args:
        client_key: Unique client identifier
        
    Returns:
        Cached gender or None
    """
    cached = VOICE_CACHE.get(client_key)
    
    if cached and time.time() - cached["time"] < Config.TTS_VOICE_CACHE_TTL:
        return cached["gender"]
    
    return None

# =============================================================================
# SILENCE FALLBACK HANDLING
# =============================================================================

async def get_silence_response() -> bytes:
    """
    Return silence MP3 audio data for empty/meaningless input.
    
    Priority:
    1. Read from TTS_SILENCE_FILE_PATH if exists
    2. Decode TTS_SILENCE_MP3_B64 base64 string
    3. Return empty bytes
    
    Returns:
        MP3 audio bytes
    """
    # Try file first
    if os.path.exists(Config.TTS_SILENCE_FILE_PATH):
        try:
            with open(Config.TTS_SILENCE_FILE_PATH, 'rb') as f:
                return f.read()
        except Exception as e:
            logger.warning(f"TTS Silence file read error: {e}")
    
    # Fallback to base64
    if Config.TTS_SILENCE_MP3_B64:
        try:
            return base64.b64decode(Config.TTS_SILENCE_MP3_B64)
        except Exception as e:
            logger.error(f"TTS Failed to decode SILENCE_MP3_B64: {e}")
    
    return b''

# =============================================================================
# LANGUAGE BLOCK SPLITTING
# =============================================================================

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=2), retry=retry_if_exception_type(Exception))
def split_by_language_blocks(text: str) -> List[Tuple[str, str]]:
    """
    Split text into blocks by language, supporting explicit labels.
    
    Supports explicit language labels via TTS_LANGUAGE_LABELS mapping.
    Format: "label:text" where label maps to language code.
    
    Args:
        text: Input text
        
    Returns:
        List of (language_code, segment_text) tuples
    """
    if not text:
        return []
    
    blocks = []
    lines = text.split('\n')
    current_lang = None
    current_buffer = []
    
    # Load label mapping from config
    label_map = Config.TTS_LANGUAGE_LABELS
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        detected_lang = None
        clean_line = line
        lower_line = line.lower()
        
        # Check for explicit labels
        for label, lang in label_map.items():
            if lower_line.startswith(label):
                detected_lang = lang
                clean_line = line[len(label):].strip()
                break
        
        # Auto-detect if no label
        if detected_lang is None:
            detected_lang = detect_language(line)
        
        # Flush buffer on language change
        if detected_lang != current_lang:
            if current_buffer:
                blocks.append((current_lang, ' '.join(current_buffer)))
                current_buffer = []
            current_lang = detected_lang
        
        current_buffer.append(clean_line)
    
    # Flush remaining buffer
    if current_buffer:
        blocks.append((current_lang, ' '.join(current_buffer)))
    
    return blocks

# =============================================================================
# AUDIO GENERATION WITH RETRY LOGIC
# =============================================================================

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=2), retry=retry_if_exception_type(Exception))
async def generate_chunk_with_retry(chunk_text: str, voice: str, response_format: str, speed: float) -> str:
    """
    Generate single audio chunk with retry logic using centralized temp directory.
    
    Files are created in Config.TEMP_FILES_DIR with unique names to prevent
    race conditions in multi-worker environments.
    
    Args:
        chunk_text: Text to synthesize
        voice: Voice ID
        response_format: Audio format (mp3, wav, etc.)
        speed: Speech speed multiplier
        
    Returns:
        Path to generated audio file
        
    Raises:
        Exception: If generation fails after retries
    """
    rate = f"+{int((speed - 1) * 100)}%"
    communicate = edge_tts.Communicate(chunk_text, voice, rate=rate)
    
    # Generate unique filename using timestamp and PID
    timestamp = int(time.time() * 1000)
    filename = f"tts_chunk_{timestamp}_{os.getpid()}.{response_format}"
    temp_path = Config.TEMP_FILES_DIR / filename
    
    try:
        await communicate.save(str(temp_path))
        
        # Validate file was created and not empty
        if temp_path.stat().st_size == 0:
            raise ValueError("Empty audio file generated")
        
        return str(temp_path)
        
    except Exception as e:
        logger.error(f"TTS EdgeTTS Error: {type(e).__name__} - {str(e)}")
        
        # Cleanup on failure
        if temp_path.exists():
            temp_path.unlink()
        
        raise

# =============================================================================
# MAIN SPEECH GENERATION FUNCTION
# =============================================================================

async def generate_speech_async(text: str, voice: str, response_format: str, speed: float, use_fallback: bool = True) -> str:
    """
    Generate complete speech audio with language block splitting and fallback support.
    
    Process:
    1. Split text into language blocks
    2. For each block, select appropriate voice
    3. Split into chunks and generate audio
    4. Apply fallback voice on errors if configured
    5. Concatenate all chunks into final file
    6. Cleanup intermediate chunk files
    
    Args:
        text: Full text to synthesize
        voice: Primary voice ID
        response_format: Output audio format
        speed: Speech speed
        use_fallback: Enable fallback voice mechanism
        
    Returns:
        Path to final concatenated audio file
        
    Raises:
        Exception: If all chunks fail to generate
    """
    blocks = split_by_language_blocks(text)
    
    if Config.DEBUG_LOGGING:
        logger.debug(f"TTS Language blocks: {blocks}")
    
    audio_files = []
    
    for lang, segment in blocks:
        # Skip empty segments
        if not segment or not is_meaningful_text(segment):
            if Config.DEBUG_LOGGING:
                logger.debug(f"TTS Skipping empty block for lang={lang}")
            continue
        
        # Determine gender from current voice
        gender = 'female'
        for g, voices in VOICES_CONFIG.items():
            if voice in voices.values():
                gender = g
                break
        
        # Select voice for this language
        voice_id = VOICES_CONFIG.get(gender, {}).get(lang)
        if not voice_id:
            if Config.DEBUG_LOGGING:
                logger.debug(f"TTS No voice for {gender}/{lang}, using fallback {voice}")
            voice_id = voice
        
        logger.debug(f"TTS BLOCK: lang={lang} -> voice={voice_id}")
        
        # Split into chunks
        chunks = split_text_into_chunks(segment)
        current_voice = voice_id
        fallback_triggered = False
        
        for i, chunk in enumerate(chunks):
            try:
                chunk_file = await generate_chunk_with_retry(chunk, current_voice, response_format, speed)
                audio_files.append(chunk_file)
                
            except Exception as e:
                logger.error(f"TTS Chunk {i+1} error: {e}")
                
                # Try fallback voice if available
                if use_fallback and not fallback_triggered and current_voice in FALLBACK_VOICES:
                    fallback_voice = FALLBACK_VOICES[current_voice]
                    logger.info(f"TTS Fallback triggered: {current_voice} -> {fallback_voice}")
                    current_voice = fallback_voice
                    fallback_triggered = True
                    
                    try:
                        chunk_file = await generate_chunk_with_retry(chunk, current_voice, response_format, speed)
                        audio_files.append(chunk_file)
                        continue
                    except Exception:
                        pass
                
                if Config.DEBUG_LOGGING:
                    logger.debug(f"TTS Skipping chunk {i+1}")
                continue
    
    if not audio_files:
        raise Exception("All chunks failed to generate")
    
    # Create final output file in Config.TEMP_FILES_DIR
    timestamp = int(time.time() * 1000)
    final_filename = f"tts_final_{timestamp}_{os.getpid()}.{response_format}"
    final_path = Config.TEMP_FILES_DIR / final_filename
    
    # Concatenate all chunks
    with open(final_path, 'wb') as outfile:
        for fname in audio_files:
            with open(fname, 'rb') as infile:
                outfile.write(infile.read())
            # Remove chunk immediately after concatenation
            os.unlink(fname)
    
    if Config.DEBUG_LOGGING:
        logger.debug(f"TTS Speech generated: {final_path}")
    
    return str(final_path)

# =============================================================================
# SSE STREAMING SUPPORT
# =============================================================================

async def collect_async(gen):
    """
    Collect all items from async generator into list.
    
    Args:
        gen: Async generator
        
    Returns:
        List of collected items
    """
    res = []
    async for item in gen:
        res.append(item)
    return res


def generate_sse_response(text: str, voice: str, speed: float, response_format: str):
    """
    Generate Server-Sent Events streaming response for TTS.
    
    Yields SSE events with base64-encoded audio chunks.
    
    Args:
        text: Text to synthesize
        voice: Voice ID
        speed: Speech speed
        response_format: Audio format
        
    Returns:
        Async generator yielding SSE events
    """
    async def stream():
        for chunk in split_text_into_chunks(text):
            try:
                rate = f"+{int((speed - 1) * 100)}%"
                comm = edge_tts.Communicate(chunk, voice, rate=rate)
                
                async for msg in comm.stream():
                    if msg["type"] == "audio":
                        yield msg["data"]
                        
            except Exception as e:
                logger.error(f"TTS Stream error: {e}")
    
    async def event_generator():
        data_list = await collect_async(stream())
        
        for data in data_list:
            yield f"data: {json.dumps({'type': 'speech.audio.delta', 'audio': base64.b64encode(data).decode()})}\n\n"
        
        yield f"data: {json.dumps({'type': 'speech.audio.done'})}\n\n"
    
    return event_generator()
