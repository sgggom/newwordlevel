#!/usr/bin/env python3
"""Build layered game dictionary `game_dictionary.json` from BNC/COCA 7,300 wordlist and ECDICT.

Layers:
1. core_words: 7,300 base words (default target pool: id <= 6000; 6001..7300 naturally act as 8k-9k obscure base words).
2. tutorial_2letter_words: Curated 2-letter guide words allowed exclusively for Level 1.
3. inflections: Plurals, past tense, past participle, -ing, 3rd-person singular, comparatives, superlatives derived from the 7,300 core words.
4. obscure_words: Lower-frequency / derivative English words (BNC/COCA rank <= 20000 in ECDICT) not in core_words or inflections, excluding proper nouns & abbreviations.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parent
CORE_CSV = WORKSPACE / "BNC_COCA_常用英语词表_长度3-8_英美拼写版.csv"
ECDICT_CSV = Path("/tmp/dict_cache/ecdict.csv")
KAIKKI_SLIM = Path("/tmp/dict_cache/kaikki_slim.jsonl")
OUTPUT_JSON = WORKSPACE / "game_dictionary.json"

COLOR_TAG_RE = re.compile(r"</?color(?:=#[0-9A-Fa-f]{6})?>")
PROPER_NOUN_RE = re.compile(
    r"(人名|男子名|女子名|男名|女名|姓氏|地名|城市名|国名|河名|山名|岛名|洲名|罗马数字|abbr\.|缩写|简称|缩略|字母|等于[A-Z])"
)
POS_LINE_RE = re.compile(
    r"\b(n\.|v\.|vt\.|vi\.|a\.|adj\.|adv\.|prep\.|conj\.|pron\.|int\.)\s+[^（\(]*(?:的|；|,|，|$)"
)
VOWELS = set("aeiouy")

BLACKLIST_WORDS = {
    # Profanity / vulgar / sensitive words
    "ass", "asses", "assed", "arse", "arses", "asshole", "bastard", "bastards",
    "bitch", "bitches", "bloody", "bollocks", "boob", "boobs", "bugger", "bum",
    "bums", "butt", "butts", "cock", "cocks", "coon", "crap", "craps", "cum",
    "cums", "cunt", "cunts", "damn", "damned", "damns", "dick", "dicks", "dildo",
    "dyke", "fag", "fags", "faggot", "fart", "farts", "fuck", "fucked", "fucker",
    "fucking", "fucks", "goddamn", "hell", "hells", "homo", "horny", "jerk",
    "jerks", "kike", "knob", "knobs", "lust", "lusts", "nazi", "nigga", "nigger",
    "orgasm", "pee", "peed", "pees", "peeing", "penis", "piss", "pissed",
    "pisses", "pissing", "poop", "porn", "porno", "prick", "pube", "pussy",
    "queer", "rape", "raped", "rapes", "raping", "rapist", "rectum", "retard",
    "retards", "scrotum", "semen", "sex", "sexes", "sexed", "sexing", "sexy",
    "shag", "shit", "shits", "shitted", "shitting", "shitty", "slut", "sluts",
    "smut", "spic", "spunk", "stripper", "tit", "tits", "titty", "turd", "twat",
    "vagina", "vaginas", "vulva", "wank", "wanker", "whore", "whores", "wog",
    # Abbreviations, Roman numerals, foreign fragments, short name noise
    "mrs", "dna", "dnas", "etc", "abc", "abcs", "sic", "sics", "que", "pre",
    "com", "moi", "por", "qui", "sur", "une", "bon", "dal", "dis", "doo", "git",
    "ole", "aye", "ayes", "nah", "nay", "casa",
    "ari", "ben", "dan", "dee", "dom", "don", "dow", "edn", "fer", "han",
    "iii", "vii", "viii", "xii", "xiv", "kay", "ken", "kew", "ley", "lin",
    "mac", "mel", "mhm", "mmm", "mph", "mth", "nan", "nat", "naw", "och",
    "ooh", "owt", "pas", "pic", "plc", "ppm", "psi", "ref", "rep", "rev",
    "rio", "sal", "shh", "sib", "sri", "sue", "tam", "tbsp", "tec", "ted",
    "tod", "tom", "tsp", "ugh", "umm", "und", "vic", "viz", "wha", "wye",
    "yer", "zoe",
}


def is_clean_word(word: str) -> bool:
    """Return True if word is purely alphabetic, contains a vowel/y, and is not blacklisted."""
    return word.isalpha() and word not in BLACKLIST_WORDS and bool(set(word) & VOWELS)

EXCHANGE_LABELS = {
    "p": "过去式",
    "d": "过去分词",
    "i": "现在分词",
    "3": "第三人称单数",
    "s": "复数",
    "r": "比较级",
    "t": "最高级",
}

TUTORIAL_2_LETTERS = [
    "at", "in", "on", "to", "go", "up", "do", "be", "we", "he", "me",
    "no", "so", "or", "by", "my", "us", "it", "is", "as", "am", "an",
    "if", "of", "hi", "ok",
]


def clean_text(text: str, max_lines: int = 2) -> str:
    """Strip color tags and literal newline escapes, keeping concise lines."""
    plain = COLOR_TAG_RE.sub("", text or "").replace("\\n", "\n")
    lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
    # Filter out [网络] noise lines if there are standard lines
    non_net = [ln for ln in lines if not ln.startswith("[网络]")]
    chosen = non_net if non_net else lines
    return " | ".join(chosen[:max_lines])


def load_ecdict() -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Load ECDICT rows indexed by lowercase word, plus set of capitalized proper nouns."""
    if not ECDICT_CSV.exists():
        return {}, set()
    by_word: dict[str, dict[str, Any]] = {}
    cap_words: set[str] = set()
    with ECDICT_CSV.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = (row.get("word") or "").strip()
            if not raw:
                continue
            low = raw.lower()
            if raw[0].isupper() and raw.isalpha():
                cap_words.add(low)
                # Prefer lowercase entry if already stored
                if low in by_word:
                    continue
            if not raw.isalpha():
                continue
            bnc = int(row["bnc"]) if (row.get("bnc") or "").isdigit() else 0
            frq = int(row["frq"]) if (row.get("frq") or "").isdigit() else 0
            entry = {
                "raw": raw,
                "phonetic": (row.get("phonetic") or "").strip(),
                "translation": (row.get("translation") or "").strip(),
                "exchange": (row.get("exchange") or "").strip(),
                "bnc": bnc,
                "frq": frq,
            }
            # Keep entry with lowercase form or better frequency
            prev = by_word.get(low)
            if prev is None or (prev["raw"][0].isupper() and raw.islower()):
                by_word[low] = entry
    return by_word, cap_words


def build_dictionary() -> dict[str, Any]:
    """Construct layered dictionary dictionary and write to OUTPUT_JSON."""
    ecdict_map, cap_words = load_ecdict()

    core_words: list[dict[str, Any]] = []
    core_set: set[str] = set()
    removed_core = 0
    with CORE_CSV.open("r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            word = row["单词 (Word)"].strip().lower()
            if not word or not is_clean_word(word):
                removed_core += 1
                continue
            zh_clean = clean_text(row["中文注释"], max_lines=2)
            if "abbr." in zh_clean.lower() and not POS_LINE_RE.search(zh_clean):
                removed_core += 1
                continue
            role = (row.get("词条定位") or "目标词/候选词").strip()
            extra_only = role == "仅额外词"
            core_set.add(word)
            core_words.append(
                {
                    "id": int(row["序号"]),
                    "word": word,
                    "length": int(row["字母长度"]),
                    "band": row["词频层级 (Band)"].strip(),
                    "tier": row["分类 (Tier)"].strip(),
                    "spelling": row["拼写归属"].strip(),
                    "ipa": row["音标 (IPA)"].strip(),
                    "zh": zh_clean,
                    "role": role,
                    "extra_only": extra_only,
                }
            )

    # 1. Tutorial 2-letter words
    tutorial_2: dict[str, dict[str, str]] = {}
    for w in TUTORIAL_2_LETTERS:
        ec = ecdict_map.get(w, {})
        tutorial_2[w] = {
            "word": w,
            "ipa": f"/{ec['phonetic']}/" if ec.get("phonetic") else "",
            "zh": clean_text(ec.get("translation", ""), max_lines=1),
        }

    # 2. Inflections from core_words' exchange fields
    inflections: dict[str, dict[str, Any]] = {}

    def add_inflection(form: str, lemma: str, tag_codes: list[str]) -> None:
        if (
            not is_clean_word(form)
            or not is_clean_word(lemma)
            or not (3 <= len(form) <= 8)
            or form in core_set
        ):
            return
        ec_form = ecdict_map.get(form, {})
        if form not in inflections:
            inflections[form] = {
                "lemma": lemma,
                "tags": [],
                "zh": clean_text(ec_form.get("translation", ""), max_lines=1),
            }
        existing_tags = inflections[form]["tags"]
        for code in tag_codes:
            label = EXCHANGE_LABELS.get(code)
            if label and label not in existing_tags:
                existing_tags.append(label)

    for cw in core_words:
        lemma = cw["word"]
        ec = ecdict_map.get(lemma)
        if not ec or not ec["exchange"]:
            continue
        for part in ec["exchange"].split("/"):
            if ":" not in part:
                continue
            k, val = part.split(":", 1)
            val = val.strip().lower()
            if k in EXCHANGE_LABELS:
                add_inflection(val, lemma, [k])

    # Recover valid high-frequency irregular inflections pointing via 0:<lemma>
    for low, ec in ecdict_map.items():
        if (
            not ec["raw"].islower()
            or not low.isalpha()
            or not (3 <= len(low) <= 8)
            or low in core_set
            or low in inflections
        ):
            continue
        if not (ec["bnc"] > 0 or ec["frq"] > 0):
            continue
        ex = ec["exchange"]
        lemma = ""
        codes: list[str] = []
        for part in ex.split("/"):
            if part.startswith("0:"):
                cand = part[2:].strip().lower()
                if cand in core_set:
                    lemma = cand
            elif part.startswith("1:"):
                codes = [c for c in part[2:].strip() if c in EXCHANGE_LABELS]
        if lemma:
            add_inflection(low, lemma, codes or ["p"])

    # Fallback / supplement from Kaikki if ECDICT was unavailable
    if not inflections and KAIKKI_SLIM.exists():
        with KAIKKI_SLIM.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                w = (obj.get("word") or "").strip().lower()
                if not w.isalpha() or not (3 <= len(w) <= 8) or w in core_set:
                    continue
                for form_of in obj.get("form_of") or []:
                    lemma = (form_of.get("word") or "").strip().lower()
                    if lemma in core_set:
                        add_inflection(w, lemma, ["s"])
                        break

    # 3. Obscure / extended base words (BNC or COCA rank <= 20000, not in core_set or inflections)
    obscure_words: dict[str, dict[str, Any]] = {}
    for low, ec in ecdict_map.items():
        if (
            not ec["raw"].islower()
            or not is_clean_word(low)
            or not (3 <= len(low) <= 8)
            or low in core_set
            or low in inflections
        ):
            continue
        bnc, frq = ec["bnc"], ec["frq"]
        if not (0 < bnc <= 20000 or 0 < frq <= 20000):
            continue
        tr = ec["translation"]
        if not tr:
            continue
        if PROPER_NOUN_RE.search(tr):
            continue
        if low in cap_words and ("人名" in tr or "地名" in tr or "名）" in tr):
            continue
        zh = clean_text(tr, max_lines=1)
        if not zh or zh.startswith("[网络]"):
            continue
        obscure_words[low] = {
            "bnc": bnc,
            "frq": frq,
            "zh": zh,
        }

    # Format tag summary on inflections
    for w, info in inflections.items():
        tags_str = "/".join(info["tags"]) if info["tags"] else "变形"
        info["tag_desc"] = f"{info['lemma']} 的{tags_str}"
        if not info["zh"]:
            info["zh"] = info["tag_desc"]

    payload = {
        "meta": {
            "source_core": CORE_CSV.name,
            "core_count": len(core_words),
            "target_eligible_count": sum(1 for item in core_words if not item["extra_only"]),
            "extra_only_core_count": sum(1 for item in core_words if item["extra_only"]),
            "default_target_max_rank": 6000,
            "tutorial_2letter_count": len(tutorial_2),
            "inflection_count": len(inflections),
            "obscure_count": len(obscure_words),
        },
        "core_words": core_words,
        "tutorial_2letter_words": tutorial_2,
        "inflections": inflections,
        "obscure_words": obscure_words,
    }
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return payload


if __name__ == "__main__":
    data = build_dictionary()
    print("Built game_dictionary.json successfully:")
    print(json.dumps(data["meta"], ensure_ascii=False, indent=2))
