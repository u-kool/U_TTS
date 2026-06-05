# core/piper_preprocessor.py
import logging
import regex as re

logger = logging.getLogger(__name__)

try:
    from core.russian_normalizer import RussianNormalizer
    RUS_NORMALIZER_AVAILABLE = True
except ImportError as e:
    logger.warning(f"RussianNormalizer not available: {e}")
    RUS_NORMALIZER_AVAILABLE = False

try:
    from core.english_normalizer import EnglishNormalizer
    ENG_NORMALIZER_AVAILABLE = True
except ImportError as e:
    logger.warning(f"EnglishNormalizer not available: {e}")
    ENG_NORMALIZER_AVAILABLE = False

try:
    from silero_stress import Accentor
    SILERO_AVAILABLE = True
except ImportError:
    logger.warning("silero-stress not available")
    SILERO_AVAILABLE = False


CORRECTION_WORDS = {
    "адреса", "атлас", "берега", "ведра", "века", "ветра",
    "вина", "волны", "ворота", "гвоздика", "глаза", "города",
    "грозы", "дела", "доктора", "дома", "духи", "душа",
    "замки", "земли", "зеркала", "звуки", "иглы", "игры",
    "клубы", "кольца", "кружки", "крыла", "леса", "лица",
    "места", "моря", "ноги", "номера", "облака", "окна",
    "острова", "поля", "реки", "рога", "руки", "сёла",
    "столы", "стекла", "стоны", "страны", "тела", "тени",
    "толпы", "тона", "уши", "языки", "яйца",
}


_STRESS_MARK = "\u0301"
_RUSSIAN_VOWELS = "аеёиоуыэюяАЕЁИОУЫЭЮЯ"
_RUSSIAN_VOWELS_SET = set(_RUSSIAN_VOWELS)
_PUNCTUATION = ".,!?:[]{}<>'—–-/"


_rus_normalizer = None
_eng_normalizer = None
_accentor = None


def get_normalizers():
    global _rus_normalizer, _eng_normalizer, _accentor
    
    if _rus_normalizer is None and RUS_NORMALIZER_AVAILABLE:
        try:
            _rus_normalizer = RussianNormalizer(use_yo=True)
            logger.info("RussianNormalizer initialized (yo=True)")
        except Exception as e:
            logger.error(f"Failed to init RussianNormalizer: {e}")
    
    if _eng_normalizer is None and ENG_NORMALIZER_AVAILABLE:
        try:
            _eng_normalizer = EnglishNormalizer()
            logger.info("EnglishNormalizer initialized")
        except Exception as e:
            logger.error(f"Failed to init EnglishNormalizer: {e}")
    
    if _accentor is None and SILERO_AVAILABLE:
        try:
            _accentor = Accentor()
            logger.info("Silero Accentor initialized")
        except Exception as e:
            logger.error(f"Failed to load Silero Accentor: {e}")
    
    return _rus_normalizer, _eng_normalizer, _accentor


def _count_vowels(word: str) -> int:
    return sum(1 for char in word if char in _RUSSIAN_VOWELS_SET)


def preprocess_text_for_stress(text: str, accentor) -> str:
    if accentor is None:
        return text
    
    current_text = text
    
    try:
        text_with_silero_stress = accentor(text)
        
        split_pattern = f'([\\s{re.escape(_PUNCTUATION)}]+)'
        
        original_parts = re.split(split_pattern, text)
        stressed_parts = re.split(split_pattern, text_with_silero_stress)
        
        if len(original_parts) == len(stressed_parts):
            final_parts = []
            for orig_part, stressed_part in zip(original_parts, stressed_parts):
                clean_word = orig_part.lower().strip(_PUNCTUATION)
                if clean_word in CORRECTION_WORDS:
                    final_parts.append(stressed_part)
                else:
                    final_parts.append(orig_part)
            current_text = "".join(final_parts)
    except Exception as e:
        logger.debug(f"Silero stress error: {e}")
    
    return current_text


def preprocess_text(text: str) -> str:
    rus_norm, eng_norm, accentor = get_normalizers()
    
    result = text
    
    if eng_norm:
        try:
            result = eng_norm.normalize(result)
            logger.debug(f"After EnglishNormalizer: {result[:50]}...")
        except Exception as e:
            logger.debug(f"EnglishNormalizer error: {e}")
    
    if rus_norm:
        try:
            result = rus_norm.normalize(result)
            logger.debug(f"After RussianNormalizer: {result[:50]}...")
        except Exception as e:
            logger.debug(f"RussianNormalizer error: {e}")
    
    if accentor:
        try:
            result = preprocess_text_for_stress(result, accentor)
            logger.debug(f"After Silero: {result[:50]}...")
        except Exception as e:
            logger.debug(f"Silero error: {e}")
    
    return result


def get_accentor():
    _, _, accentor = get_normalizers()
    return accentor
