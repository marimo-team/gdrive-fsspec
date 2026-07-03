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
