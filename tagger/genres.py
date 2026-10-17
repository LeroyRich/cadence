import re


def genre_values(value):
    if isinstance(value, list):
        return list(value)
    return [value] if value else []


def clean_genres(values, split=False):
    result = []
    seen = set()
    for value in genre_values(values):
        for part in re.split(r"[,;]", value) if split else [value]:
            part = part.strip()
            if part and part.casefold() not in seen:
                result.append(part)
                seen.add(part.casefold())
    return result


def genre_needs_review(values):
    values = genre_values(values)
    cleaned = [value.strip().casefold() for value in values]
    return (len(set(cleaned)) != len(cleaned) or
            any(not value.strip() or value != value.strip() or "," in value or ";" in value or
                any(ord(character) < 32 for character in value) or
                value.strip().casefold() in ("unknown", "unspecified", "n/a", "none") for value in values))


def validate_genres(value):
    if not isinstance(value, (str, list)):
        raise ValueError("Genres must be text or a list of text genres")
    values = genre_values(value)
    if len(values) > 100:
        raise ValueError("Use no more than 100 genres")
    for index, item in enumerate(values, 1):
        if not isinstance(item, str):
            raise ValueError("Genre {} is not text".format(index))
        if len(item) > 1000:
            raise ValueError("Genre {} exceeds 1,000 characters".format(index))
        if any(ord(character) < 32 for character in item):
            raise ValueError("Genre {} contains a hidden control character; edit or remove it".format(index))


def normalize_edits(edits):
    return {field: clean_genres(value) if field == "genre" else value.strip() for field, value in edits.items()}


def validate_genre_options(options):
    if not isinstance(options, dict) or set(options) - {"mode", "values", "clean", "split", "upgrade"}:
        raise ValueError("Invalid genre options")
    if options.get("mode", "keep") not in ("keep", "replace", "add", "remove", "clear"):
        raise ValueError("Unknown genre edit mode")
    for key in ("clean", "split", "upgrade"):
        if key in options and not isinstance(options[key], bool):
            raise ValueError("Invalid genre option: " + key)
    validate_genres(options.get("values", []))


def apply_genre_options(current, edits, options):
    result = normalize_edits(edits)
    mode = options.get("mode", "keep")
    if mode == "keep" and not options.get("clean") and not options.get("split"):
        return result
    original = genre_values(result.get("genre", current))
    values = list(original)
    selected = clean_genres(options.get("values", []))
    if mode == "replace":
        values = selected
    elif mode == "add":
        present = {value.strip().casefold() for value in values}
        for value in selected:
            if value.casefold() not in present:
                values.append(value)
                present.add(value.casefold())
    elif mode == "remove":
        removed = {value.casefold() for value in selected}
        values = [value for value in values if value.strip().casefold() not in removed]
    elif mode == "clear":
        values = []
    if mode in ("add", "remove") and values == original and not options.get("clean") and not options.get("split"):
        return result
    if mode in ("keep", "replace", "clear") or options.get("clean") or options.get("split"):
        values = clean_genres(values, split=options.get("split", False))
    validate_genres(values)
    if values != genre_values(current) or "genre" in result:
        result["genre"] = values
    return result
