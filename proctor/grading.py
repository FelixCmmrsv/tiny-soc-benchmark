"""Pure grading logic for the proctor. No I/O, no MCP framing -- kept separate
so it's easy to audit and unit-test independently of the stdio server.

Two supported manifest modes, both graded through the same canonicalize()
step so they can never silently disagree:
  - plaintext mode:  rule["expected"] is the correct answer as a string
                      (dev-authoring / manifest.source.yaml, never distributed)
  - hash mode:       rule["expected_hash"] + rule["salt"] instead of
                      "expected" -- produced by harness/tools/compile_manifest.py.
                      Nobody, including whoever runs the harness, can recover
                      the plaintext answer from these fields; grading only
                      ever computes sha256(salt + canonicalize(submitted)) and
                      compares hashes. This protects against a third party who
                      self-hosts the harness+scenarios reading the answer
                      straight out of the manifest file -- it does NOT resist
                      a determined local brute-force search over plausible
                      answers for low-entropy questions (Да/Нет, a handful of
                      MITRE technique combos, etc.) -- see harness/README.md.

Fuzzy (tolerance > 0) numeric/timestamp comparison is incompatible with hash
mode by construction (you can't hash-compare "within N of the target"
without the hash leaking the target under a range search) -- such rules must
stay in plaintext mode. compile_manifest.py refuses to hash them.

grade(answer, rule) -> "correct" | "wrong" | "ungraded"
"ungraded" means the manifest step has no confirmed expected value yet --
never silently scored as wrong.
"""
import hashlib
import re

UNIT_SEP = "\x1f"  # canonical join delimiter for list/set/composite hashing -- unlikely to collide with any real answer


def _s(x):
    return x if isinstance(x, str) else ("" if x is None else str(x))


def norm_ws(x):
    return _s(x).strip()


def _to_number(x):
    try:
        s = norm_ws(x)
        if re.match(r"^[+-]?\d+$", s):
            return int(s)
        return float(s)
    except Exception:
        return None


def _canonical_number_str(n):
    if isinstance(n, int):
        return str(n)
    if float(n).is_integer():
        return str(int(n))
    return repr(float(n))


def parse_timestamp_to_epoch(s):
    """Public: parse any of the timestamp formats this grader understands
    into a UTC epoch-seconds float, or None if unparseable. Used by the
    proctor to shift 'shift_from_source' expected values by the same delta
    import_data.py applied to the actual Elasticsearch data for this run."""
    return _parse_timestamp(s)


def _parse_timestamp(s):
    s = norm_ws(s)
    n = _to_number(s)
    if n is not None and n > 1_000_000_000:
        return float(n)
    import datetime
    s2 = s.replace("Z", "").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(s2, fmt)
            return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _split(answer, rule):
    delim = rule.get("delimiter")
    delim_re = rule.get("delimiter_regex")
    if delim_re:
        parts = re.split(delim_re, answer)
    else:
        parts = answer.split(delim if delim is not None else ",")
    return [p.strip() for p in parts if p.strip()]


# ---- canonicalization: the ONE place answer-normalization logic lives -----
# Called on the submitted answer at grading time, and on the plaintext
# expected value at manifest-compile time -- both paths MUST produce
# identical output for matching answers, or hash mode silently breaks.

def canonicalize(value, rule):
    rtype = rule["type"]
    s = norm_ws(value)

    if rtype == "exact":
        return s
    if rtype == "exact_ci":
        return s.casefold()
    if rtype == "numeric":
        n = _to_number(s)
        return _canonical_number_str(n) if n is not None else s
    if rtype == "hash":
        return re.sub(r"\s+", "", s).lower()
    if rtype == "timestamp":
        epoch = _parse_timestamp(s)
        return ("%.3f" % round(epoch, 3)) if epoch is not None else s
    if rtype == "single_choice":
        cs = rule.get("case_sensitive", False)
        return s if cs else s.casefold()
    if rtype == "list":
        return _canonical_multi(s, rule, ordered=rule.get("ordered", True))
    if rtype == "set":
        return _canonical_multi(s, rule, ordered=False)
    if rtype == "set_ordered_alpha":
        return _canonical_set_ordered_alpha(s, rule)
    if rtype == "composite":
        return _canonical_composite(s, rule)
    raise ValueError("unknown grading type %r" % rtype)


def _canonical_multi(s, rule, ordered):
    item_rule = rule.get("item_rule", {"type": "exact_ci"})
    items = [canonicalize(i, item_rule) for i in _split(s, rule)]
    if not ordered:
        items = sorted(map(str, items))
    return UNIT_SEP.join(map(str, items))


def _canonical_set_ordered_alpha(s, rule):
    delim = rule.get("delimiter", ",")
    items = [p.strip() for p in s.split(delim) if p.strip()]
    canon_items = sorted(i.casefold() for i in items)
    return UNIT_SEP.join(canon_items)


def _canonical_composite(s, rule):
    delim = rule.get("delimiter", ";")
    parts_rules = rule["parts"]
    parts = s.split(delim, len(parts_rules) - 1)
    if len(parts) != len(parts_rules):
        # can't canonicalize a malformed submission consistently -- return
        # something that will never equal a valid hash/plaintext match.
        return "\x00malformed\x00" + s
    canon_parts = [canonicalize(p.strip(), pr) for p, pr in zip(parts, parts_rules)]
    return UNIT_SEP.join(canon_parts)


def _enforce_alpha_order_ok(answer, rule):
    """set_ordered_alpha's order requirement is a property of the SUBMISSION
    alone (is what you gave me already alphabetically sorted?), independent
    of what the correct answer is -- checked separately from canonicalization/
    hash comparison so it works the same in both plaintext and hash mode."""
    if not rule.get("enforce_order", True):
        return True
    delim = rule.get("delimiter", ",")
    items = [p.strip() for p in norm_ws(answer).split(delim) if p.strip()]
    return items == sorted(items, key=str.casefold)


# ---- top-level grading dispatcher -----------------------------------------

def grade(answer, rule):
    rtype = rule["type"]
    has_hash = rule.get("expected_hash") is not None
    has_plain = rule.get("expected") is not None

    if not has_hash and not has_plain:
        return "ungraded"

    tolerance = rule.get("tolerance") or rule.get("tolerance_seconds") or 0
    if tolerance:
        if has_hash:
            raise ValueError("rule has tolerance>0 and expected_hash set -- incompatible, "
                              "hash-based grading cannot do fuzzy/range comparison")
        return _grade_with_tolerance(answer, rule, tolerance)

    if rtype == "set_ordered_alpha" and not _enforce_alpha_order_ok(answer, rule):
        return "wrong"

    try:
        canon = canonicalize(answer, rule)
    except Exception:
        return "wrong"

    if has_hash:
        salt = rule.get("salt", "")
        h = hashlib.sha256((salt + canon).encode("utf-8")).hexdigest()
        return "correct" if h == rule["expected_hash"] else "wrong"

    try:
        canon_expected = canonicalize(_s(rule["expected"]), rule)
    except Exception:
        return "wrong"
    return "correct" if canon == canon_expected else "wrong"


def _grade_with_tolerance(answer, rule, tolerance):
    rtype = rule["type"]
    if rtype == "numeric":
        a, e = _to_number(answer), _to_number(rule.get("expected"))
        if a is None or e is None:
            return "wrong"
        return "correct" if abs(a - e) <= tolerance else "wrong"
    if rtype == "timestamp":
        a, e = _parse_timestamp(answer), _parse_timestamp(norm_ws(rule.get("expected")))
        if a is None or e is None:
            # fall back to exact string comparison for exotic formats we don't parse
            return "correct" if norm_ws(answer) == norm_ws(rule.get("expected")) else "wrong"
        return "correct" if abs(a - e) <= tolerance else "wrong"
    raise ValueError("tolerance is only meaningful for numeric/timestamp rules, got %r" % rtype)


def hash_expected(expected_value, rule, salt):
    """Used by compile_manifest.py: produce the same hash grade() would need
    to match, from the plaintext expected value + a rule + a salt."""
    canon = canonicalize(_s(expected_value), rule)
    return hashlib.sha256((salt + canon).encode("utf-8")).hexdigest()
