def escape_query_str(value: str) -> str:
    """Escape a string literal for a Drive ``files.list`` query.

    Backslashes are escaped before single quotes so the added escapes are not
    themselves re-escaped. See
    https://developers.google.com/workspace/drive/api/guides/search-files.

    Args:
        value: Raw string to embed inside single quotes in a query.

    Returns:
        The escaped string, safe to interpolate into ``'...'`` in a query.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def merge_fields(base: str, extra: str | None) -> str:
    """Merge two comma-separated Drive field masks, dropping duplicate tokens.

    Args:
        base: Base fields string, always retained.
        extra: Additional fields to append; ``None`` or empty returns ``base``
            unchanged.

    Returns:
        The merged field mask, order-preserving with duplicate tokens removed.
    """
    if not extra:
        return base
    fields = [f.strip() for f in base.split(",") if f.strip()]
    fields.extend(f.strip() for f in extra.split(",") if f.strip())
    return ",".join(dict.fromkeys(fields))
