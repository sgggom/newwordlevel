#!/usr/bin/env python3
"""Complete IPA (UK/RP priority), Chinese (ECDICT), and English (Kaikki/Wiktionary) annotations
for the 7,300-word BNC/COCA wordlist (length 3-8), targeting British English first-launch.
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Font

WORKSPACE = Path("/Users/sggg/Desktop/单词筛选")
INPUT_XLSX = WORKSPACE / "BNC_COCA_常用英语词表_长度3-8_英美拼写版.xlsx"
BACKUP_XLSX = Path("/tmp/dict_cache/input_backup.xlsx")
OUTPUT_XLSX = WORKSPACE / "BNC_COCA_常用英语词表_长度3-8_英美拼写版.xlsx"
OUTPUT_CSV = WORKSPACE / "BNC_COCA_常用英语词表_长度3-8_英美拼写版.csv"
REVIEW_CSV = WORKSPACE / "未匹配及变体回退复核清单.csv"

KAIKKI_SLIM = Path("/tmp/dict_cache/kaikki_slim.jsonl")
ECDICT_CSV = Path("/tmp/dict_cache/ecdict.csv")

POS_MAP = {
    "noun": "n.",
    "verb": "v.",
    "adj": "adj.",
    "adv": "adv.",
    "prep": "prep.",
    "pron": "pron.",
    "conj": "conj.",
    "det": "det.",
    "article": "art.",
    "num": "num.",
    "intj": "interj.",
    "aux": "aux.",
    "particle": "part.",
    "prefix": "pref.",
    "suffix": "suf.",
    "abbrev": "abbr.",
    "contraction": "contr.",
    "phrase": "phr.",
    "prep_phrase": "prep.",
    "postp": "postp.",
    "symbol": "sym.",
    "name": "n.",
}

POS_ORDER = [
    "n.", "v.", "adj.", "adv.", "pron.", "prep.", "conj.",
    "det.", "art.", "num.", "interj.", "aux.", "part.",
    "contr.", "phr.", "abbr.", "pref.", "suf.", "postp.", "sym.",
]
POS_RANK = {p: i for i, p in enumerate(POS_ORDER)}

POS_COLORS: dict[str, str] = {
    "n.": "#3B82F6",
    "v.": "#10B981",
    "adj.": "#F59E0B",
    "adv.": "#8B5CF6",
    "prep.": "#06B6D4",
    "pron.": "#EC4899",
    "conj.": "#D97706",
    "det.": "#14B8A6",
    "art.": "#0EA5E9",
    "num.": "#6366F1",
    "interj.": "#EF4444",
    "aux.": "#22C55E",
    "part.": "#A855F7",
    "contr.": "#64748B",
    "phr.": "#0284C7",
    "abbr.": "#78716C",
    "pref.": "#9333EA",
    "suf.": "#C026D3",
    "postp.": "#0891B2",
    "sym.": "#475569",
}
COLOR_TAG_RE = re.compile(r"</?color(?:=#[0-9A-Fa-f]{6})?>")


def format_pos_line(pos: str, body: str) -> str:
    """Wrap POS tag with rich-text <color=#HEX>...</color> markup."""
    color = POS_COLORS.get(pos, "#64748B")
    return f"<color={color}>{pos}</color> {body}"


def apply_rich_syntax_to_block(text: str) -> str:
    """Convert multi-line POS block into <color=#HEX>pos.</color> lines joined by literal \\n."""
    out: list[str] = []
    for raw_ln in text.replace("\\n", "\n").split("\n"):
        ln = raw_ln.strip()
        if not ln:
            continue
        parts = ln.split(" ", 1)
        if len(parts) == 2 and parts[0] in POS_COLORS:
            out.append(format_pos_line(parts[0], parts[1]))
        else:
            out.append(format_pos_line("n.", ln))
    return "\\n".join(out)


def strip_color_tags(text: str) -> str:
    """Remove rich-text <color> tags for plain POS inspection."""
    return COLOR_TAG_RE.sub("", text)

# Pure spelling-equivalent UK <-> US pairs (shared meaning and POS)
EQUIV_PAIRS = [
    ("anaemia", "anemia"), ("analogue", "analog"), ("analyse", "analyze"), ("appal", "appall"),
    ("armour", "armor"), ("artefact", "artifact"), ("axe", "ax"), ("bannister", "banister"),
    ("baptise", "baptize"), ("behaviour", "behavior"), ("catalogue", "catalog"), ("catalyse", "catalyze"),
    ("centre", "center"), ("chilli", "chili"), ("civilise", "civilize"), ("clamour", "clamor"),
    ("colour", "color"), ("cosy", "cozy"), ("defence", "defense"), ("despatch", "dispatch"),
    ("dialogue", "dialog"), ("diarrhoea", "diarrhea"), ("digitise", "digitize"), ("disc", "disk"),
    ("discolour", "discolor"), ("distil", "distill"), ("endeavour", "endeavor"), ("enrol", "enroll"),
    ("aesthetic", "esthetic"), ("exorcise", "exorcize"), ("favour", "favor"), ("favourite", "favorite"),
    ("fervour", "fervor"), ("foetal", "fetal"), ("foetus", "fetus"), ("fibre", "fiber"),
    ("fillet", "filet"), ("flavour", "flavor"), ("fulfil", "fulfill"), ("gauge", "gage"),
    ("glamour", "glamor"), ("grey", "gray"), ("gruelling", "grueling"), ("harbour", "harbor"),
    ("honour", "honor"), ("humour", "humor"), ("instil", "instill"), ("jewellery", "jewelry"),
    ("kilogramme", "kilogram"), ("labour", "labor"), ("leukaemia", "leukemia"), ("litre", "liter"),
    ("lustre", "luster"), ("maximise", "maximize"), ("meagre", "meager"), ("mediaeval", "medieval"),
    ("metre", "meter"), ("minimise", "minimize"), ("mobilise", "mobilize"), ("monologue", "monolog"),
    ("mould", "mold"), ("moulding", "molding"), ("moustache", "mustache"), ("neighbour", "neighbor"),
    ("odour", "odor"), ("offence", "offense"), ("omelette", "omelet"), ("optimise", "optimize"),
    ("organise", "organize"), ("paralyse", "paralyze"), ("parlour", "parlor"), ("penalise", "penalize"),
    ("phoney", "phony"), ("plough", "plow"), ("programme", "program"), ("prologue", "prolog"),
    ("pyjamas", "pajamas"), ("realise", "realize"), ("rigour", "rigor"), ("rumour", "rumor"),
    ("saviour", "savior"), ("savour", "savor"), ("savoury", "savory"), ("sceptic", "skeptic"),
    ("sheikh", "sheik"), ("smoulder", "smolder"), ("spectre", "specter"), ("splendour", "splendor"),
    ("sulphur", "sulfur"), ("theatre", "theater"), ("theorise", "theorize"), ("tumour", "tumor"),
    ("utilise", "utilize"), ("vapour", "vapor"), ("waggon", "wagon"), ("yoghourt", "yogurt"),
    ("yoghurt", "yogurt"), ("aluminium", "aluminum"), ("sizeable", "sizable"),
]

# Pairs with partial divergence in UK usage (handled carefully for definitions/POS)
SEMI_PAIRS = [
    ("practise", "practice"),
    ("licence", "license"),
    ("gramme", "gram"),
    ("draught", "draft"),
    ("tyre", "tire"),
    ("storey", "story"),
]

SPELLING_REF_RE = re.compile(
    r"^(?:\([^)]+\)\s*)*"
    r"(?:Alternative|British|UK|U\.K\.|US|U\.S\.|American|North American|Commonwealth|"
    r"Australia|Canadian|Canada|New Zealand|Irish|Ireland|South Africa|Non-Oxford|Oxford|"
    r"Standard|Nonstandard|Informal|Dated|Archaic|Rare|Obsolete|Deprecated|Uncommon|"
    r"Eye dialect|Pronunciation|Uncountable|Countable|Letter-case|Alternative letter-case)\b"
    r"[^.;]*?\b(?:standard\s+)?(?:spelling|form|variant)\s+of\s+([^\s.,;()\"“”]+(?:\.)?)",
    re.IGNORECASE,
)

REMOVE_ABBR_WORDS: set[str] = {
    "mrs", "dna", "etc", "ibid", "abc", "com", "ole",
    "non", "pre", "mega", "micro", "multi", "macro", "trans", "semi",
    "bon", "por", "qui", "sur", "une", "casa", "moi", "monde", "que",
}

# Tenses/inflections (-ed/-ing/-s/irregular), lexicalized clippings/acronyms, and capitalized proper/calendar nouns -> 仅额外词
EXTRA_ONLY_WORDS: set[str] = {
    # -ed past / past participle / participial forms (53)
    "ashamed", "closed", "owned", "directed", "lasted", "sorted", "armed", "disabled", "founded",
    "objected", "oriented", "battered", "boxed", "briefed", "fined", "marched", "peered", "rocked",
    "stuffed", "sued", "deceased", "typed", "assorted", "bobbed", "chaired", "defected", "felled",
    "ironed", "planed", "ragged", "stranded", "busted", "dogged", "donned", "exacted", "indebted",
    "mugged", "tempered", "wooded", "belated", "jacked", "leaded", "outdated", "paged", "skirted",
    "soiled", "lapped", "pursed", "seconded", "smelted", "steeped", "stunted", "tendered",
    # -ing present participle / gerund forms (28)
    "boring", "booking", "longing", "ongoing", "lining", "moulding", "molding", "pending", "plumbing",
    "pounding", "stocking", "winding", "fleeting", "footing", "kidding", "outing", "uprising", "icing",
    "casing", "flagging", "grueling", "upcoming", "vaulting", "hawking", "jarring", "outlying",
    "scathing", "vetting",
    # -s / 3sg / plural & irregular plural forms (53)
    "airways", "algae", "alps", "annals", "arrears", "auspices", "barracks", "bounds", "brethren",
    "cans", "clothes", "commons", "criteria", "data", "fairs", "fasts", "firms", "flats", "generals",
    "goods", "innings", "jeans", "majors", "marines", "masons", "media", "oats", "overalls",
    "pajamas", "pants", "patients", "plains", "pyjamas", "pylori", "revs", "rights", "roses",
    "sacks", "scissors", "seconds", "senses", "shorts", "slacks", "soles", "spectra", "stables",
    "stairs", "states", "strata", "tropics", "trousers", "webs", "wells",
    # Pure irregular past / past participle / comparative tense forms (7)
    "sawn", "slew", "wrought", "gilt", "molten", "laden", "meaner",
    # Retained lexicalized abbreviations / clippings / acronyms (29)
    "exam", "pub", "bike", "disco", "amp", "kilo", "perm", "polio", "teddy", "mini", "sitcom",
    "showbiz", "telecom", "memo", "logo", "cop", "cab", "fax", "mod", "prom", "prop", "quad",
    "vibe", "laser", "radar", "sonar", "unix", "internet", "teen",
    # Capitalized proper / calendar nouns (14)
    "april", "december", "february", "friday", "hispanic", "january", "july", "monday",
    "october", "saturday", "shiite", "sunday", "thursday", "tuesday",
}

ABBR_GLOSS_RE = re.compile(
    r"^(?:\([^)]+\)\s*)*(?:Abbreviation|Initialism|Acronym|Syllabic abbreviation|Clipping|Shortened form|Short form)\s+of\b",
    re.IGNORECASE,
)

# Manual curated supplements/fixes for defective ECDICT entries or lexicalized words
ZH_OVERRIDES: dict[str, str] = {
    "marines": "n. 海军陆战队；海军陆战队士兵；水兵（marine 的复数）",
    "smelted": "v. 熔炼，冶炼（smelt 的过去式和过去分词）",
    "fairs": "n. 集市，庙会；博览会，展销会（fair 的复数）",
    "fasts": "n. 斋戒期；绝食（fast 的复数）\nv. 斋戒；禁食（fast 的第三人称单数）",
    "revs": "n. （发动机的）旋转，转数（rev 的复数）\nv. （使发动机）快速旋转；激增（rev 的第三人称单数）",
    "sitcom": "n. 情景喜剧",
    "ram": "n. 公羊；撞锤，冲压机\nv. 猛撞，猛击；填塞",
    "macho": "adj. 男子气概十足的，大男子主义的\nn. 大男子主义者，硬汉",
    "internet": "n. 互联网，因特网\nv. 使用互联网",
    "genome": "n. 基因组，染色体组",
    "holistic": "adj. 整体的，全局的；全面医疗的",
    "redesign": "v. 重新设计\nn. 重新设计",
    "autistic": "adj. 患有自闭症的，孤独症的\nn. 自闭症患者",
    "sigma": "n. 西格玛（希腊字母表第18个字母 Σ/σ）；求和符号",
    "aquifer": "n. 含水层，蓄水层",
    "bourbon": "n. 波旁威士忌（产于美国的玉米威士忌）",
    "vetting": "n. 审查，严格审核，背景调查\nv. 审查，仔细检查（vet 的现在分词）",
    "telecom": "n. 电信，远程通信",
    "unix": "n. Unix（一种多用户、多任务计算机操作系统）",
    "cola": "n. 可乐（碳酸饮料）；可乐树（产可乐果）",
    "sawn": "v. 锯，锯开（saw 的过去分词）",
    "stuffed": "adj. 塞满的；吃饱的；填充的\nv. 塞满，填充（stuff 的过去式和过去分词）",
    "sacks": "n. 麻袋；解雇（sack 的复数）\nv. 解雇；洗劫（sack 的第三人称单数）",
    "paged": "v. 给……标页码；呼唤，传呼（page 的过去式和过去分词）\nadj. 分页的",
    "pylori": "n. 幽门（pylorus 的复数，尤指幽门螺杆菌 Helicobacter pylori）",
    "spectra": "n. 光谱，波谱；范围，系列（spectrum 的复数）",
    "lapped": "v. 拍打；舔；重叠；领先一圈（lap 的过去式和过去分词）\nadj. 搭接的，重叠的",
    "fillet": "n. 无骨肉片，鱼片；填角，圆角；头带\nv. 把（肉、鱼）切成无骨片",
    "filet": "n. 无骨肉片，鱼片（fillet 的美式及法式拼写）\nv. 把（肉、鱼）切成无骨片",
    "gramme": "n. 克（质量单位，gram 的英式拼写）",
    "analog": "n. 类似物，相似情况；模拟量\nadj. 模拟的（analogue 的美式拼写）",
    "analogue": "n. 类似物，相似情况；模拟量\nadj. 模拟的",
}

EN_FALLBACK_OVERRIDES: dict[str, str] = {
    "exam": "n. A formal test or assessment of a person's knowledge or proficiency in a subject\nv. To examine or test",
    "bike": "n. A bicycle or motorcycle\nv. To ride a bicycle or motorcycle",
    "disco": "n. A nightclub or party where people dance to recorded pop music; a style of dance music popular in the 1970s\nv. To dance at or go to a disco",
    "amp": "n. An ampere (the SI base unit of electric current); an amplifier for musical instruments or audio signals\nv. To amplify or excite",
    "kilo": "n. A kilogram (a unit of mass equal to 1,000 grams); a kilometre",
    "perm": "n. A hairstyle produced by chemically treating the hair to set curls or waves\nv. To treat (hair) with a permanent wave",
    "polio": "n. Poliomyelitis, an infectious viral disease affecting the central nervous system that can cause temporary or permanent paralysis",
    "teddy": "n. A soft stuffed toy bear (teddy bear); a women's one-piece undergarment combining a camisole and knickers",
    "mini": "adj. Miniature; very small of its kind\nn. A very small version of something; a miniskirt or small car",
    "sitcom": "n. A situation comedy, an episodic comedy television or radio programme",
    "showbiz": "n. The entertainment industry, especially theatre, film, television, and popular music",
    "telecom": "n. Telecommunications, the transmission of information over distances by electronic means",
}

IPA_FALLBACK_OVERRIDES: dict[str, str] = {
    "firework": "/ˈfaɪəˌwɜːk/",
    "petrify": "/ˈpɛt.ɹɪ.faɪ/",
    "vampire": "/ˈvæm.paɪə(ɹ)/",
}

BIO_PLACE_MARKERS = ("A surname", "A male given name", "A female given name", "A barangay", "A city,", "A town,", "An unincorporated community")


def build_variant_maps() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Build equivalence map (for defs/IPA) and full IPA variant map."""
    equiv: dict[str, list[str]] = defaultdict(list)
    ipa_map: dict[str, list[str]] = defaultdict(list)

    def add_pair(d: dict[str, list[str]], a: str, b: str) -> None:
        if b not in d[a]:
            d[a].append(b)
        if a not in d[b]:
            d[b].append(a)

    for u, s in EQUIV_PAIRS:
        add_pair(equiv, u, s)
        add_pair(ipa_map, u, s)
    add_pair(equiv, "yoghourt", "yoghurt")
    add_pair(ipa_map, "yoghourt", "yoghurt")

    for u, s in SEMI_PAIRS:
        add_pair(ipa_map, u, s)
    return equiv, ipa_map


EQUIV_MAP, IPA_VARIANT_MAP = build_variant_maps()


def load_kaikki(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Load slimmed Kaikki JSONL keyed by lowercased headword."""
    data: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            w = (obj.get("word") or "").strip().lower()
            if w:
                data[w].append(obj)
    return data


def load_ecdict(path: Path) -> dict[str, dict[str, str]]:
    """Load ECDICT CSV keyed by lowercased headword."""
    data: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            w = (row.get("word") or "").strip().lower()
            if w and w not in data:
                data[w] = row
    return data


def _is_bio_only_entry(e: dict[str, Any]) -> bool:
    senses = [sn for sn in (e.get("senses") or []) if sn.get("glosses")]
    if not senses:
        return False
    return all(any(m in sn["glosses"][0] for m in BIO_PLACE_MARKERS) for sn in senses)


def select_entries(word: str, kaikki: dict[str, list[dict[str, Any]]], include_capitalized: bool = False) -> list[dict[str, Any]]:
    """Select relevant Kaikki entries, preferring exact lowercase non-name entries."""
    entries = kaikki.get(word, [])
    if not entries:
        return []

    if include_capitalized:
        non_bio = [e for e in entries if not (e["pos"] == "name" and _is_bio_only_entry(e))]
        return non_bio or entries

    lower_non_name = [e for e in entries if e["word"] == word and e["pos"] != "name"]
    if lower_non_name:
        # Check if lowercase entries only point to capitalized forms (e.g. dna -> DNA, gothic -> Gothic, etc.)
        has_substantive_lower = any(
            not ("alt-of" in (sn.get("tags") or []) and any((item.get("word") or "").lower() == word for item in (sn.get("alt_of") or [])))
            for e in lower_non_name
            for sn in (e.get("senses") or [])
            if sn.get("glosses")
        )
        if word in ("internet", "unix", "gothic", "sabbath", "satan", "cossack", "kaiser", "mongol", "november", "arctic", "june"):
            cap_entries = [e for e in entries if e["word"] != word and not (e["pos"] == "name" and _is_bio_only_entry(e))]
            return lower_non_name + cap_entries
        if has_substantive_lower:
            return lower_non_name
        cap_entries = [e for e in entries if e["word"] != word and not (e["pos"] == "name" and _is_bio_only_entry(e))]
        return lower_non_name + cap_entries

    lower_all = [e for e in entries if e["word"] == word]
    if lower_all:
        return lower_all

    if word in ("april", "january", "february", "july", "october", "december"):
        non_bio = [e for e in entries if not (e["pos"] == "name" and _is_bio_only_entry(e))]
        return non_bio or entries

    cap_non_name = [e for e in entries if e["pos"] != "name"]
    if cap_non_name:
        return cap_non_name

    non_bio = [e for e in entries if not _is_bio_only_entry(e)]
    return non_bio or entries


def normalize_ipa_str(raw: str) -> str:
    """Extract clean phonemic /.../ or phonetic [...] IPA from a Kaikki ipa string."""
    s = raw.strip()
    if not s:
        return ""
    # Handle strings like '/t͡ʃɑːns/ [t͡ʃʰɑːns]'
    m_slash = re.search(r"/[^/]+/", s)
    if m_slash:
        return m_slash.group(0).strip()
    m_brack = re.search(r"\[[^\]]+\]", s)
    if m_brack:
        return m_brack.group(0).strip()
    return s


def is_valid_ipa(ipa: str) -> bool:
    """Exclude incomplete affix IPAs such as /-səz/ or /-ed/."""
    s = ipa.strip()
    if not s:
        return False
    if s.startswith(("/-", "[-")) or s.endswith(("-/", "-]")):
        return False
    if "/" not in s and "[" not in s:
        return False
    return True


def score_ipa(ipa: str, tags: set[str], note: str) -> int:
    """Score an IPA candidate for UK / RP priority."""
    note_l = (note or "").lower()
    score = 0
    if ipa.startswith("/") and ipa.endswith("/"):
        score += 200

    # Check if this pronunciation explicitly represents standard UK / RP
    is_rp = bool(tags & {"Received-Pronunciation", "RP", "Standard-Southern-British", "General-British"}) or ("received pronunciation" in note_l)
    is_uk = "UK" in tags
    is_eng_core = bool(tags & {"Southern-England", "England"})

    hard_bad = {"obsolete", "archaic", "dated", "nonstandard", "dialectal", "rare", "unstressed", "Early", "Modern"}
    if tags & hard_bad:
        score -= 4500
    if any(k in note_l for k in ("dated", "archaic", "obsolete", "nonstandard", "dialectal")):
        score -= 4500
    if "raising" in note_l and "without" not in note_l:
        score -= 2500

    # Only penalize regional dialect tags when NOT explicitly tagged RP/UK/England
    regional_bad = {
        "Cockney", "Multicultural-London-English", "Northumbria", "Scotland",
        "Ireland", "Northern-Ireland", "Northern-England", "Wales", "Midlands",
        "Appalachia", "Southern-US", "New-York-City", "Philippines", "India",
        "South-Asia", "Singapore", "cot-caught-merger", "pin-pen-merger",
    }

    if is_rp:
        score += 5000
    elif is_uk:
        score += 4600
    elif is_eng_core or ("British" in tags and not (tags & {"Scotland", "Northern-England", "Wales"})):
        score += 4100
    elif "British" in tags:
        score += 3600
    elif (tags & regional_bad):
        score += 200
    elif not (tags & {"Australia", "New-Zealand", "General-American", "US", "Canada", "Canadian"}) and not any(
        k in note_l for k in ("us", "american", "canada", "australia", "new zealand")
    ):
        score += 3400
    elif tags & {"Australia", "New-Zealand"}:
        score += 1800
    elif tags & {"General-American", "US", "Canada", "Canadian"}:
        score += 1000
    else:
        score += 1200

    return score


def pick_best_ipa_from_entries(entries: list[dict[str, Any]]) -> tuple[str | None, int]:
    """Return (formatted_ipa, best_score) from Kaikki entries."""
    best_ipa: str | None = None
    best_score = -10_000
    for e in entries:
        for s in e.get("sounds") or []:
            raw_ipa = s.get("ipa") or ""
            ipa = normalize_ipa_str(raw_ipa)
            if not is_valid_ipa(ipa):
                continue
            tags = set(s.get("tags") or [])
            note = s.get("note") or ""
            sc = score_ipa(ipa, tags, note)
            if sc > best_score:
                best_score = sc
                best_ipa = ipa
    if best_ipa and best_ipa.startswith("[") and best_ipa.endswith("]"):
        best_ipa = "/" + best_ipa[1:-1] + "/"
    return best_ipa, best_score


def derive_inflected_uk_ipa(word: str, base_word: str, base_ipa: str) -> str | None:
    """Derive regular English plural/-3sg/-ed/-ing/-er UK IPA from a base word's UK IPA."""
    if not base_ipa or not base_ipa.startswith("/") or not base_ipa.endswith("/"):
        return None
    core = base_ipa[1:-1]
    no_opt_r = re.sub(r"\(ɹ\)$", "", core)

    if word == base_word + "s" or (base_word.endswith(("s", "x", "z", "ch", "sh")) and word == base_word + "es"):
        end_c = no_opt_r[-1] if no_opt_r else ""
        if no_opt_r.endswith(("tʃ", "dʒ")) or end_c in ("s", "z", "ʃ", "ʒ"):
            return f"/{no_opt_r}ɪz/"
        if end_c in ("p", "t", "k", "f", "θ"):
            return f"/{no_opt_r}s/"
        return f"/{no_opt_r}z/"

    if word.endswith("ies") and base_word.endswith("y") and word[:-3] == base_word[:-1]:
        return f"/{no_opt_r}z/"

    if word in (base_word + "ed", base_word + "d") or (
        len(base_word) >= 3 and word == base_word + base_word[-1] + "ed"
    ):
        end_c = no_opt_r[-1] if no_opt_r else ""
        if end_c in ("t", "d"):
            return f"/{no_opt_r}ɪd/"
        if no_opt_r.endswith("tʃ") or end_c in ("p", "k", "f", "θ", "s", "ʃ"):
            return f"/{no_opt_r}t/"
        return f"/{no_opt_r}d/"

    if word == base_word + "ing" or (base_word.endswith("e") and word == base_word[:-1] + "ing") or (
        len(base_word) >= 3 and word == base_word + base_word[-1] + "ing"
    ):
        if core.endswith("(ɹ)"):
            return f"/{core[:-3]}ɹɪŋ/"
        return f"/{core}ɪŋ/"

    if word in (base_word + "er", base_word + "r"):
        if core.endswith("(ɹ)"):
            return f"/{core[:-3]}ɹə/"
        return f"/{core}ə/"

    return None


def clean_ecdict_phonetic(ph: str) -> str:
    """Normalize legacy ECDICT ASCII phonetic notation into standard UK IPA /.../."""
    s = ph.strip().split(",")[0].strip()
    if not s:
        return ""
    replacements = [
        ("є", "e"),
        ("ɒ:", "ɔː"),
        ("ә:", "ɜː"),
        ("ə:", "ɜː"),
        ("ә", "ə"),
        ("ei", "eɪ"),
        ("əu", "əʊ"),
        ("ai", "aɪ"),
        ("au", "aʊ"),
        ("ɔi", "ɔɪ"),
        ("iə", "ɪə"),
        ("uə", "ʊə"),
        (":", "ː"),
        ("^", "ɡ"),
        ("'", "ˈ"),
        ("g", "ɡ"),
    ]
    for old, new in replacements:
        s = s.replace(old, new)
    if s.startswith("."):
        s = "ˌ" + s[1:]
    s = s.replace("..", "·")
    if not (s.startswith("/") and s.endswith("/")):
        s = f"/{s.strip('/')}/"
    return s


def extract_cross_refs(entries: list[dict[str, Any]]) -> list[str]:
    """Collect alt_of / SPELLING_REF_RE target words from Kaikki entries."""
    refs: list[str] = []
    for e in entries:
        for sn in e.get("senses") or []:
            for item in sn.get("alt_of") or []:
                w = (item.get("word") or "").strip().lower()
                if w and w not in refs:
                    refs.append(w)
            for gl in sn.get("glosses") or []:
                m = SPELLING_REF_RE.match(gl)
                if m:
                    w = m.group(1).strip().lower().rstrip(".,;:")
                    if w and w not in refs:
                        refs.append(w)
    return refs


def extract_form_bases(entries: list[dict[str, Any]]) -> list[str]:
    """Collect form_of base words from Kaikki entries."""
    bases: list[str] = []
    for e in entries:
        for sn in e.get("senses") or []:
            for item in sn.get("form_of") or []:
                w = (item.get("word") or "").strip().lower()
                if w and w not in bases:
                    bases.append(w)
    return bases


def get_ecdict_bases(word: str, ecdict: dict[str, dict[str, str]]) -> list[str]:
    """Extract 0:base lemmas from ECDICT exchange column."""
    row = ecdict.get(word)
    if not row:
        return []
    bases: list[str] = []
    for part in (row.get("exchange") or "").split("/"):
        if part.startswith("0:"):
            b = part[2:].strip().lower()
            if b and b not in bases:
                bases.append(b)
    return bases


def extract_uk_ipa(
    word: str,
    sp_type: str,
    kaikki: dict[str, list[dict[str, Any]]],
    ecdict: dict[str, dict[str, str]],
) -> tuple[str, str]:
    """Extract UK / RP priority IPA and return (ipa, source_tag)."""
    if word in IPA_FALLBACK_OVERRIDES:
        return IPA_FALLBACK_OVERRIDES[word], "manual_loanword_ipa"

    UK_OR_UNIV = 3400
    entries = select_entries(word, kaikki)
    ipa, sc = pick_best_ipa_from_entries(entries)

    if sc < UK_OR_UNIV:
        cap_entries = select_entries(word, kaikki, include_capitalized=True)
        c_ipa, c_sc = pick_best_ipa_from_entries(cap_entries)
        if c_ipa and c_sc > sc:
            ipa, sc = c_ipa, c_sc

    variant_candidates = IPA_VARIANT_MAP.get(word, [])
    if sc < UK_OR_UNIV:
        for var in variant_candidates:
            var_entries = select_entries(var, kaikki)
            v_ipa, v_sc = pick_best_ipa_from_entries(var_entries)
            if v_ipa and v_sc > sc and v_sc >= UK_OR_UNIV:
                return v_ipa, f"variant_uk_ipa({var})"

    if ipa and sc >= UK_OR_UNIV:
        return ipa, "kaikki_direct"

    for ref in extract_cross_refs(entries):
        ref_entries = select_entries(ref, kaikki, include_capitalized=True)
        r_ipa, r_sc = pick_best_ipa_from_entries(ref_entries)
        if r_ipa and r_sc >= UK_OR_UNIV:
            return r_ipa, f"alt_ref_ipa({ref})"

    bases = extract_form_bases(entries) + get_ecdict_bases(word, ecdict)
    for b in bases:
        b_entries = select_entries(b, kaikki)
        b_ipa, b_sc = pick_best_ipa_from_entries(b_entries)
        if b_ipa and b_sc >= UK_OR_UNIV:
            derived = derive_inflected_uk_ipa(word, b, b_ipa)
            if derived:
                return derived, f"derived_from_base({b})"

    # If Kaikki only had non-UK IPA (e.g. US/GA), prefer ECDICT British RP phonetic when valid & untruncated
    ec_ph = ((ecdict.get(word) or {}).get("phonetic") or "").strip()
    if ec_ph and "\\" not in ec_ph:
        cleaned_ec = clean_ecdict_phonetic(ec_ph)
        if cleaned_ec and (not ipa or len(cleaned_ec) >= 0.72 * len(ipa)):
            return cleaned_ec, "ecdict_uk_fallback"

    if ipa:
        return ipa, "kaikki_non_uk_fallback"

    for var in variant_candidates:
        v_ipa, _ = pick_best_ipa_from_entries(select_entries(var, kaikki))
        if v_ipa:
            return v_ipa, f"variant_any_ipa({var})"

    for var in variant_candidates:
        v_ph = ((ecdict.get(var) or {}).get("phonetic") or "").strip()
        if v_ph:
            return clean_ecdict_phonetic(v_ph), f"variant_ecdict_phonetic({var})"

    return "", "MISSING"


def choose_gloss(sense: dict[str, Any]) -> str | None:
    """Pick clean gloss text from Kaikki sense, flattening nested parent headers."""
    gl = [g.strip() for g in (sense.get("glosses") or sense.get("raw_glosses") or []) if isinstance(g, str) and g.strip()]
    if not gl:
        return None
    leaf = re.sub(r"\s+", " ", gl[-1]).strip()
    if leaf.endswith(":"):
        return None
    return leaf


def collect_pos_senses(
    word: str,
    kaikki: dict[str, list[dict[str, Any]]],
    visited: set[str] | None = None,
    allow_form_enrich: bool = True,
    include_capitalized: bool = False,
) -> dict[str, list[tuple[int, str]]]:
    """Collect scored English glosses per normalized POS for a word, resolving spelling redirects."""
    visit_key = (word, include_capitalized)
    if visited is None:
        visited = set()
    if visit_key in visited:
        return {}
    visited = visited | {visit_key}

    entries = select_entries(word, kaikki, include_capitalized=include_capitalized)
    pos_buckets: dict[str, list[tuple[int, str]]] = defaultdict(list)
    redirect_targets: dict[str, list[tuple[str, bool]]] = defaultdict(list)

    skip_never = {
        "misspelling", "eye-dialect", "pronunciation-spelling",
        "abbreviation", "initialism", "acronym", "clipping", "contraction",
    }
    low_tags = {"dated", "rare", "historical", "dialectal", "slang", "nonstandard"}
    very_low_tags = {"obsolete", "archaic", "vulgar"}

    for e in entries:
        raw_pos = e.get("pos") or "noun"
        if raw_pos in ("abbrev", "contraction"):
            continue
        pos = POS_MAP.get(raw_pos, "n.")

        # Enforce UK POS restrictions on practise (v. only) and licence (n. primarily)
        if word == "practise" and pos != "v.":
            continue
        if word == "licence" and pos == "v.":
            continue

        for sn in e.get("senses") or []:
            tags = set(sn.get("tags") or [])
            gloss = choose_gloss(sn)
            if not gloss:
                continue

            if tags & skip_never or ABBR_GLOSS_RE.match(gloss):
                continue

            # Check if this sense is a spelling/letter-case redirect
            m_ref = SPELLING_REF_RE.match(gloss)
            if m_ref or ("alt-of" in tags):
                # If the redirect contains an inline definition after ':' (e.g. "US and Canada spelling of tyre: The rubber covering on a wheel")
                if ":" in gloss and not gloss.rstrip().endswith(":"):
                    inline_def = gloss.split(":", 1)[1].strip()
                    if len(inline_def) > 15 and not SPELLING_REF_RE.match(inline_def) and not ABBR_GLOSS_RE.match(inline_def):
                        pos_buckets[pos].append((105, inline_def))
                        continue
                target_raw = ""
                if sn.get("alt_of"):
                    target_raw = (sn["alt_of"][0].get("word") or "").strip()
                elif m_ref:
                    target_raw = m_ref.group(1).strip().rstrip(".,;:")
                # Clean trailing NATO phrase annotations like "November from the NATO/ICAO..."
                if " " in target_raw and target_raw.split(" ", 1)[0].lower() == word:
                    target_raw = target_raw.split(" ", 1)[0]
                target = target_raw.lower()
                is_cap_target = bool(target_raw) and target_raw != target
                if target and (target != word or (is_cap_target and not include_capitalized)):
                    pair = (target, is_cap_target)
                    if pair not in redirect_targets[pos]:
                        redirect_targets[pos].append(pair)
                continue

            # Skip pure bio/toponym senses inside mixed name entries
            if raw_pos == "name" and any(m in gloss for m in BIO_PLACE_MARKERS):
                continue

            sc = 100
            if tags & low_tags or "rhyming slang" in gloss.lower():
                sc -= 60
            if tags & very_low_tags:
                sc -= 150
            if tags & {"US", "North-America"} and not (tags & {"UK", "British", "Commonwealth"}):
                sc -= 15
            if sn.get("form_of"):
                sc -= 50
                if allow_form_enrich and len(gloss) < 60:
                    base_w = (sn["form_of"][0].get("word") or "").strip().lower()
                    if base_w and (base_w, False) not in visited:
                        base_buckets = collect_pos_senses(base_w, kaikki, visited, allow_form_enrich=False)
                        if pos in base_buckets and base_buckets[pos]:
                            base_top = sorted(base_buckets[pos], key=lambda x: -x[0])[0][1]
                            base_short = base_top.rstrip(".")
                            if len(base_short) > 80:
                                base_short = base_short[:77].rsplit(" ", 1)[0] + "..."
                            gloss = f"{gloss} ({base_short})"

            if word == "gramme" and "chickpea" in gloss.lower():
                continue

            pos_buckets[pos].append((sc, gloss))

    # Resolve spelling redirects for POS buckets that only had redirects (or draught partial redirect)
    for pos, targets in redirect_targets.items():
        if not pos_buckets[pos] or word == "draught":
            for t, t_cap in targets:
                t_buckets = collect_pos_senses(t, kaikki, visited, allow_form_enrich, include_capitalized=t_cap)
                cand_pos = pos if pos in t_buckets else ("n." if t_cap and "n." in t_buckets else None)
                if cand_pos and cand_pos in t_buckets:
                    for sc, gl in t_buckets[cand_pos]:
                        if word == "gramme" and "unit of mass" not in gl.lower():
                            continue
                        pos_buckets[pos].append((sc - 5, gl))

    # If still empty, check capitalized entries or EQUIV_MAP counterparts
    if not pos_buckets and not include_capitalized:
        cap_buckets = collect_pos_senses(word, kaikki, visited, allow_form_enrich, include_capitalized=True)
        for pos, items in cap_buckets.items():
            pos_buckets[pos].extend(items)

    if not pos_buckets:
        for var in EQUIV_MAP.get(word, []):
            v_buckets = collect_pos_senses(var, kaikki, visited, allow_form_enrich)
            for pos, items in v_buckets.items():
                if word == "gramme" and pos != "n.":
                    continue
                for sc, gl in items:
                    if word == "gramme" and "unit of mass" not in gl.lower():
                        continue
                    pos_buckets[pos].append((sc - 5, gl))

    return pos_buckets


def extract_en_definitions(word: str, sp_type: str, kaikki: dict[str, list[dict[str, Any]]]) -> tuple[str, str]:
    """Extract rich-text formatted English definitions (literal \\n-delimited by POS) and status tag."""
    if word in EN_FALLBACK_OVERRIDES:
        return apply_rich_syntax_to_block(EN_FALLBACK_OVERRIDES[word]), "manual_loanword_en"

    buckets = collect_pos_senses(word, kaikki)
    source_note = "kaikki_direct"

    raw_entries = select_entries(word, kaikki)
    if not raw_entries:
        source_note = "variant_or_missing"
    elif all(
        all(
            bool(SPELLING_REF_RE.match(choose_gloss(sn) or "")) or ("alt-of" in (sn.get("tags") or []))
            for sn in (e.get("senses") or []) if choose_gloss(sn)
        )
        for e in raw_entries
        if not (word == "practise" and e.get("pos") != "verb")
        and not (word == "licence" and e.get("pos") == "verb")
    ):
        source_note = "resolved_spelling_redirect"

    if not buckets:
        return "", "MISSING"

    lines: list[str] = []
    # Preserve natural POS insertion order from Kaikki entries
    for pos in buckets.keys():
        items = sorted(buckets[pos], key=lambda x: -x[0])
        chosen: list[str] = []
        seen_norm: set[str] = set()
        for sc, gl in items:
            if chosen and sc < 50:
                continue
            gl_clean = gl.rstrip(".")
            norm = gl_clean.lower()
            if norm in seen_norm:
                continue
            if any(norm.startswith(s) or s.startswith(norm) for s in seen_norm if len(min(norm, s, key=len)) > 20):
                continue
            seen_norm.add(norm)
            chosen.append(gl_clean)
            if len(chosen) >= 3:
                break
        if chosen:
            lines.append(format_pos_line(pos, "; ".join(chosen)))

    if not lines:
        return "", "MISSING"
    return "\\n".join(lines), source_note


ZH_POS_RE = re.compile(r"^([a-zA-Z]+(?:\.[a-zA-Z]+)*\.)\s*(.+)$")
ZH_POS_NORM = {
    "a.": "adj.",
    "adj.": "adj.",
    "ad.": "adv.",
    "adv.": "adv.",
    "n.": "n.",
    "pl.": "n.",
    "v.": "v.",
    "vt.": "v.",
    "vi.": "v.",
    "vt.vi.": "v.",
    "vi.vt.": "v.",
    "vbl.": "v.",
    "prep.": "prep.",
    "pron.": "pron.",
    "conj.": "conj.",
    "int.": "interj.",
    "interj.": "interj.",
    "num.": "num.",
    "art.": "art.",
    "det.": "det.",
    "aux.": "aux.",
    "abbr.": "abbr.",
    "pref.": "pref.",
}


def parse_ecdict_translation(tr: str, fallback_pos: str) -> list[tuple[str, str]]:
    """Parse ECDICT translation text into insertion-ordered [(norm_pos, gloss_str), ...]."""
    text = tr.replace("\\n", "\n")
    raw_lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    pos_items: dict[str, list[str]] = defaultdict(list)
    bracket_items: list[str] = []
    plain_items: list[str] = []

    for ln in raw_lines:
        if ln.startswith("["):
            if any(ln.startswith(bad) for bad in ("[人名]", "[地名]", "[电影]", "[网络]")):
                continue
            cleaned = re.sub(r"^\[[^\]]+\]\s*", "", ln).strip(" ,;；")
            if cleaned:
                bracket_items.append(cleaned)
            continue
        m = ZH_POS_RE.match(ln)
        if m:
            raw_p, body = m.group(1).lower(), m.group(2).strip()
            if raw_p == "abbr.":
                continue
            norm_p = ZH_POS_NORM.get(raw_p, raw_p)
            for part in re.split(r"[,，;；]\s*", body):
                part = part.strip()
                if not part:
                    continue
                if re.search(r"的(?:缩写|简称|简写)", part):
                    continue
                if part not in pos_items[norm_p]:
                    pos_items[norm_p].append(part)
        else:
            for part in re.split(r"[,，;；]\s*", ln):
                part = part.strip()
                if not part:
                    continue
                if re.search(r"的(?:缩写|简称|简写)", part):
                    continue
                if part not in plain_items:
                    plain_items.append(part)

    result: list[tuple[str, str]] = []
    if pos_items:
        for p in pos_items.keys():
            result.append((p, "；".join(pos_items[p])))
    elif plain_items:
        result.append((fallback_pos, "；".join(plain_items)))
    elif bracket_items:
        result.append((fallback_pos, "；".join(dict.fromkeys(bracket_items))))
    return result


def extract_zh_definitions(
    word: str,
    en_defs: str,
    ecdict: dict[str, dict[str, str]],
) -> tuple[str, str]:
    """Extract clean rich-text Chinese annotations (literal \\n-delimited) from ECDICT."""
    if word in ZH_OVERRIDES:
        return apply_rich_syntax_to_block(ZH_OVERRIDES[word]), "curated_override"

    plain_en_lines = [strip_color_tags(ln) for ln in en_defs.split("\\n") if ln]
    fallback_pos = "n."
    if plain_en_lines:
        first_token = plain_en_lines[0].split(" ", 1)[0]
        if first_token in POS_RANK:
            fallback_pos = first_token

    row = ecdict.get(word)
    tr = (row or {}).get("translation") or ""
    parsed = parse_ecdict_translation(tr, fallback_pos)
    source = "ecdict_direct"

    if not parsed:
        for var in EQUIV_MAP.get(word, []):
            v_tr = (ecdict.get(var) or {}).get("translation") or ""
            v_parsed = parse_ecdict_translation(v_tr, fallback_pos)
            if v_parsed:
                parsed = v_parsed
                source = f"variant_ecdict({var})"
                break

    if word in EQUIV_MAP and parsed:
        existing_pos = {p for p, _ in parsed}
        en_pos_set = {ln.split(" ", 1)[0] for ln in plain_en_lines}
        for var in EQUIV_MAP[word]:
            v_tr = (ecdict.get(var) or {}).get("translation") or ""
            v_parsed = parse_ecdict_translation(v_tr, fallback_pos)
            for vp, vbody in v_parsed:
                if vp not in existing_pos and vp in en_pos_set:
                    parsed.append((vp, vbody))
                    existing_pos.add(vp)
                    source = f"ecdict_enriched_by({var})"

    if not parsed:
        return "", "MISSING"

    return "\\n".join(format_pos_line(p, body) for p, body in parsed), source


def main() -> None:
    src_path = BACKUP_XLSX if BACKUP_XLSX.exists() else INPUT_XLSX
    print(f"Loading dictionary caches from {KAIKKI_SLIM} and {ECDICT_CSV}...")
    kaikki = load_kaikki(KAIKKI_SLIM)
    ecdict = load_ecdict(ECDICT_CSV)

    print(f"Loading workbook {src_path}...")
    wb = openpyxl.load_workbook(src_path)
    ws = wb["BNC_COCA_常用英语词表_长度3-8_英美拼写版"]

    headers = [ws.cell(1, c).value for c in range(1, 10)] + ["词条定位"]
    ws.cell(1, 10).value = "词条定位"
    ws.cell(1, 10).font = Font(name="Arial", bold=False)
    csv_rows: list[list[Any]] = [headers]
    review_rows: list[list[Any]] = [
        ["序号", "单词 (Word)", "拼写归属", "词条定位", "音标来源", "中文来源", "英文来源", "音标 (IPA)", "中文注释", "英文注释"]
    ]

    stats = {
        "total": 0,
        "by_sp": defaultdict(int),
        "by_role": defaultdict(int),
        "ipa_ok": defaultdict(int),
        "zh_ok": defaultdict(int),
        "en_ok": defaultdict(int),
        "ipa_sources": defaultdict(int),
        "zh_sources": defaultdict(int),
        "en_sources": defaultdict(int),
    }

    wrap_align = Alignment(wrap_text=True, vertical="center")
    mid_align = Alignment(vertical="center")
    data_font = Font(name="Arial", size=10)

    old_max_row = ws.max_row
    kept_input_rows: list[tuple[str, int, str, str, str]] = []
    removed_words: list[str] = []
    for r in range(2, old_max_row + 1):
        word = str(ws.cell(r, 2).value).strip().lower()
        if word in REMOVE_ABBR_WORDS:
            removed_words.append(word)
            continue
        length = int(ws.cell(r, 3).value)
        band = str(ws.cell(r, 4).value).strip()
        tier = str(ws.cell(r, 5).value).strip()
        sp_type = str(ws.cell(r, 6).value).strip()
        kept_input_rows.append((word, length, band, tier, sp_type))

    for new_idx, (word, length, band, tier, sp_type) in enumerate(kept_input_rows, start=1):
        r = new_idx + 1
        role = "仅额外词" if word in EXTRA_ONLY_WORDS else "目标词/候选词"
        ipa, ipa_src = extract_uk_ipa(word, sp_type, kaikki, ecdict)
        en_defs, en_src = extract_en_definitions(word, sp_type, kaikki)
        zh_defs, zh_src = extract_zh_definitions(word, en_defs, ecdict)

        ws.cell(r, 1).value = new_idx
        ws.cell(r, 2).value = word
        ws.cell(r, 2).data_type = "s"
        ws.cell(r, 3).value = length
        ws.cell(r, 4).value = band
        ws.cell(r, 5).value = tier
        ws.cell(r, 6).value = sp_type
        ws.cell(r, 7).value = ipa
        ws.cell(r, 8).value = zh_defs
        ws.cell(r, 9).value = en_defs
        ws.cell(r, 10).value = role

        for c in range(1, 7):
            ws.cell(r, c).alignment = mid_align
        ws.cell(r, 7).alignment = mid_align
        ws.cell(r, 7).font = data_font
        ws.cell(r, 8).alignment = wrap_align
        ws.cell(r, 8).font = data_font
        ws.cell(r, 9).alignment = wrap_align
        ws.cell(r, 9).font = data_font
        ws.cell(r, 10).alignment = mid_align
        ws.cell(r, 10).font = data_font

        csv_rows.append([new_idx, word, length, band, tier, sp_type, ipa, zh_defs, en_defs, role])

        stats["total"] += 1
        stats["by_sp"][sp_type] += 1
        stats["by_role"][role] += 1
        if ipa:
            stats["ipa_ok"][sp_type] += 1
        if zh_defs:
            stats["zh_ok"][sp_type] += 1
        if en_defs:
            stats["en_ok"][sp_type] += 1
        stats["ipa_sources"][ipa_src.split("(")[0]] += 1
        stats["zh_sources"][zh_src.split("(")[0]] += 1
        stats["en_sources"][en_src.split("(")[0]] += 1

        needs_review = (
            ipa_src != "kaikki_direct"
            or zh_src not in ("ecdict_direct",)
            or en_src not in ("kaikki_direct",)
        )
        if needs_review:
            review_rows.append([new_idx, word, sp_type, role, ipa_src, zh_src, en_src, ipa, zh_defs, en_defs])

    # Remove trailing obsolete rows from Excel data sheet
    new_last_row = len(kept_input_rows) + 1
    if old_max_row > new_last_row:
        ws.delete_rows(new_last_row + 1, old_max_row - new_last_row)

    # Update Summary sheet formula range and description count
    if "汇总统计" in wb.sheetnames:
        ws_sum = wb["汇总统计"]
        for sr in range(1, ws_sum.max_row + 1):
            for sc in range(1, ws_sum.max_column + 1):
                val = ws_sum.cell(sr, sc).value
                if isinstance(val, str):
                    updated = val.replace("$7301", f"${new_last_row}").replace("7,300", f"{len(kept_input_rows):,}")
                    if updated != val:
                        ws_sum.cell(sr, sc).value = updated

    print(f"Removed pure abbreviation/prefix/fragment words ({len(removed_words)}): {removed_words}")
    print(f"Role counts: {dict(stats['by_role'])}")

    ws.column_dimensions["G"].width = 20
    ws.column_dimensions["H"].width = 42
    ws.column_dimensions["I"].width = 68
    ws.column_dimensions["J"].width = 16

    print(f"Saving Excel to {OUTPUT_XLSX}...")
    wb.save(OUTPUT_XLSX)

    print(f"Saving UTF-8 BOM CSV to {OUTPUT_CSV}...")
    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(csv_rows)

    print(f"Saving review CSV to {REVIEW_CSV}...")
    with REVIEW_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(review_rows)

    print("\n=== Completion Statistics ===")
    tot = stats["total"]
    print(f"Total words: {tot}")
    print(f"IPA filled: {sum(stats['ipa_ok'].values())}/{tot} ({sum(stats['ipa_ok'].values())/tot*100:.2f}%)")
    print(f"ZH  filled: {sum(stats['zh_ok'].values())}/{tot} ({sum(stats['zh_ok'].values())/tot*100:.2f}%)")
    print(f"EN  filled: {sum(stats['en_ok'].values())}/{tot} ({sum(stats['en_ok'].values())/tot*100:.2f}%)")
    print("\nBy spelling category:")
    for sp, cnt in stats["by_sp"].items():
        print(f"  {sp}: total={cnt}, IPA={stats['ipa_ok'][sp]}/{cnt}, ZH={stats['zh_ok'][sp]}/{cnt}, EN={stats['en_ok'][sp]}/{cnt}")
    print("\nIPA sources:", dict(stats["ipa_sources"]))
    print("ZH  sources:", dict(stats["zh_sources"]))
    print("EN  sources:", dict(stats["en_sources"]))
    print(f"Review checklist rows (fallbacks/redirects/overrides): {len(review_rows) - 1}")


if __name__ == "__main__":
    main()
