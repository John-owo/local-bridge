import re

# Each mutant violates the documented contract in one way.
def m1(text, max_len=40):  # does not strip edges
    s = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return s[:max_len]

def m2(text, max_len=40):  # no lowercase
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return s[:max_len].rstrip("-")

def m3(text, max_len=40):  # leaves trailing '-' after cap
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len]

def m4(text, max_len=40):  # ignores max_len
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")

def m5(text, max_len=40):  # single non-alnum chars only, runs become '--'
    s = re.sub(r"[^a-z0-9]", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-")

MUTANTS = [m1, m2, m3, m4, m5]
