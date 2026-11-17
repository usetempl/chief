"""CommonMark to Telegram text/entities; no HTML evaluation or network access."""
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

PARSER = MarkdownIt("commonmark", {"html": False}).enable("strikethrough")


def units(text):
    return len(text.encode("utf-16-le")) // 2


def clip_entities(entities, start, end, prefix=0):
    result = []
    for entity in entities:
        left = max(start, entity["offset"])
        right = min(end, entity["offset"] + entity["length"])
        if right > left:
            result.append(entity | {"offset": left - start + prefix, "length": right - left})
    return result


def render(source):
    """Use CommonMark parsing, then express supported styles as native entities."""
    chunks, entities, opened, lists = [], [], [], []
    position = 0

    def append(text):
        nonlocal position
        chunks.append(text)
        position += units(text)

    def linebreak(count=1):
        tail = "".join(chunks[-2:])
        existing = len(tail) - len(tail.rstrip("\n"))
        if position and existing < count:
            append("\n" * (count - existing))

    def begin(kind, **extra):
        opened.append({"type": kind, "offset": position, **extra})

    def end():
        entity = opened.pop()
        if position > entity["offset"]:
            entities.append(entity | {"length": position - entity["offset"]})

    def inline(tokens):
        for token in tokens:
            kind = token.type
            if kind in ("text", "html_inline"):
                append(token.content)
            elif kind in ("softbreak", "hardbreak"):
                append("\n")
            elif kind in ("strong_open", "em_open", "s_open"):
                begin({"strong_open": "bold", "em_open": "italic", "s_open": "strikethrough"}[kind])
            elif kind in ("strong_close", "em_close", "s_close"):
                end()
            elif kind == "code_inline":
                begin("code")
                append(token.content)
                end()
            elif kind == "link_open":
                url = token.attrGet("href") or ""
                begin("text_link" if urlsplit(url).scheme in ("http", "https", "mailto", "tg") else "literal_link", url=url)
            elif kind == "link_close":
                if opened[-1]["type"] == "literal_link":
                    append(" (" + opened.pop()["url"] + ")")
                else:
                    end()
            elif kind == "image":
                append(token.content + " (" + (token.attrGet("src") or "") + ")")
            else:
                append(token.content)

    for token in PARSER.parse(source):
        kind = token.type
        if kind == "inline":
            inline(token.children or [])
        elif kind == "heading_open":
            linebreak(2)
            begin("bold")
        elif kind == "heading_close":
            end()
            linebreak(2)
        elif kind == "paragraph_close":
            linebreak(1 if lists else 2)
        elif kind in ("bullet_list_open", "ordered_list_open"):
            linebreak()
            lists.append(None if kind == "bullet_list_open" else int(token.attrGet("start") or 1))
        elif kind in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
            linebreak(1 if lists else 2)
        elif kind == "list_item_open":
            linebreak()
            number = lists[-1]
            append("  " * (len(lists) - 1) + ("• " if number is None else str(number) + ". "))
            if number is not None:
                lists[-1] += 1
        elif kind == "blockquote_open":
            linebreak(2)
            append("› ")
        elif kind == "blockquote_close":
            linebreak(2)
        elif kind in ("fence", "code_block"):
            linebreak(2)
            language = token.info.strip().split()[0] if token.info.strip() else ""
            begin("pre", **({"language": language} if language else {}))
            append(token.content)
            end()
            linebreak(2)
        elif kind == "hr":
            linebreak(2)
            append("———")
            linebreak(2)

    # Plain replies keep their original whitespace and punctuation exactly.
    if not entities:
        return source, []
    text = "".join(chunks).rstrip("\n")
    entities = clip_entities(entities, 0, units(text))
    # Telegram does not allow another entity to overlap code/pre entities.
    code = [e for e in entities if e["type"] in ("code", "pre")]
    clean = []
    for entity in entities:
        pieces = [entity]
        if entity["type"] not in ("code", "pre"):
            for excluded in code:
                pieces = [part for piece in pieces for part in (
                    clip_entities([piece], 0, excluded["offset"])
                    + clip_entities([piece], excluded["offset"] + excluded["length"], units(text),
                                    excluded["offset"] + excluded["length"]))]
        clean.extend(pieces)
    return text, sorted(clean, key=lambda e: (e["offset"], -e["length"], e["type"]))


def text_payloads(text, entities, splitter):
    chunks = splitter(text)
    payloads, start = [], 0
    for chunk in chunks:
        prefix, content = (chunk.partition("\n")[0] + "\n", chunk.partition("\n")[2]) if len(chunks) > 1 else ("", chunk)
        end = start + units(content)
        spans = clip_entities(entities, start, end, units(prefix))
        payloads.append({"method": "sendMessage", "payload": {
            "text": chunk, "entities": spans, "link_preview_options": {"is_disabled": True}}})
        start = end
    return payloads


def delivery_plan(source, splitter, photo=None):
    original = photo["caption"] if photo else source
    text, entities = render(original)
    if not entities:
        return None
    if photo and units(text) <= 1024:
        return [{"method": "sendPhoto", "payload": {
            "photo": photo["photo"], "caption": text, "caption_entities": entities}}]
    payloads = text_payloads(text, entities, splitter)
    if photo:
        payloads.insert(0, {"method": "sendPhoto", "payload": {"photo": photo["photo"], "caption": ""}})
    return payloads


def receipt_entities_match(actual, expected, sent_text, received_text):
    """Allow Telegram's boundary trimming and automatic URL/mention entities."""
    if not expected:
        return True
    if not isinstance(actual, list) or not isinstance(received_text, str):
        return False
    trim = 0 if sent_text == received_text else units(sent_text) - units(sent_text.lstrip(" \n"))
    spans = clip_entities(expected, trim, trim + units(received_text))
    return all(any(isinstance(found, dict) and all(found.get(k) == v for k, v in span.items())
                   for found in actual) for span in spans)
