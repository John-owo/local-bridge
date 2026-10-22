import re


def slugify(text, max_len=40):
    """Lowercase, replace runs of non-alphanumerics with '-', strip '-' at both ends, cap length without a trailing '-'."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s
