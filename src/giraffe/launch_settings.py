"""Compare bounded launch settings as data, without interpreting or executing shell code."""

import json
import shlex

from .caps import MAX_LAUNCH_ARGUMENTS, MAX_SANITIZE_STRING_CHARS


LAUNCH_FIELD = "serving.launch_arguments"
SETTING_FIELDS = {
    "--speculative-config": "extension.requested_speculative_config",
    "--max-num-seqs": "serving.max_num_seqs",
    "--max-num-batched-tokens": "serving.max_num_batched_tokens",
}
WHOLE_FIELD_WARNING = (
    "The whole serving.launch_arguments field was declared intended; additional changes "
    "within that field are not checked separately."
)
BOOLEAN_FLAGS = {"--enable-prefix-caching", "--no-enable-prefix-caching", "--enforce-eager"}
SPECULATIVE_METHODS = {"eagle", "eagle3", "ngram", "mtp", "draft_model"}


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _speculation(value, *, metadata=False):
    if metadata and value is None:
        return None
    if isinstance(value, str) and len(value) <= MAX_SANITIZE_STRING_CHARS:
        value = json.loads(value, object_pairs_hook=_unique_keys)
    allowed = {"method", "num_speculative_tokens", "k"} if metadata else {"method", "num_speculative_tokens"}
    if not isinstance(value, dict) or set(value) - allowed or value.get("method") not in SPECULATIVE_METHODS:
        raise ValueError("unsupported speculative configuration")
    if "k" in value and "num_speculative_tokens" in value:
        raise ValueError("ambiguous speculative token count")
    count = value.get("num_speculative_tokens", value.get("k"))
    if type(count) is not int or count <= 0:
        raise ValueError("unsupported speculative token count")
    return {"method": value["method"], "num_speculative_tokens": count}


def safe_speculation(value):
    """Retain only the supported non-secret representation; redact everything else."""
    try:
        return json.dumps(_speculation(value), sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError):
        return "[redacted]"


def comparison_warnings(diff):
    """Also qualify old reports that predate launch_comparison."""
    warnings = list((diff.get("launch_comparison") or {}).get("warnings", []))
    intended = diff.get("intended_change")
    fields = intended if isinstance(intended, list) else [intended]
    if LAUNCH_FIELD in fields and WHOLE_FIELD_WARNING not in warnings:
        warnings.append(WHOLE_FIELD_WARNING)
    return warnings


def setting_change_text(row):
    before = json.dumps(row["baseline"], sort_keys=True)
    after = json.dumps(row["current"], sort_keys=True)
    scope = ("equivalence unverified" if row["change"] == "unverified"
             else "intended" if row["intended"] else "additional change")
    return f'{row["flag"]} ({row["source"]}): {before} → {after} ({row["change"]}; {scope})'


def _redacted(value):
    return (isinstance(value, str) and "[redacted]" in value
            or isinstance(value, list) and any(_redacted(item) for item in value))


def _parse(value, allowed_flags):
    warnings, settings, order = [], {}, []
    try:
        args = shlex.split(value) if isinstance(value, str) else value
    except ValueError:
        args = None
    if (not isinstance(args, list) or len(args) > MAX_LAUNCH_ARGUMENTS
            or not all(isinstance(arg, str) for arg in args)
            or args[:2] != ["vllm", "serve"] or len(args) < 3 or args[2].startswith("--")):
        return None, [], ["Individual launch comparison requires a bounded vllm serve command with a model operand."]
    settings["model operand"] = [args[2]]
    if _redacted(args[2]):
        warnings.append("The launch model operand is redacted; its equivalence is unverified.")
    index = 3
    while index < len(args):
        flag, equals, value = args[index].partition("=")
        if not flag.startswith("--"):
            warnings.append("Additional positional launch arguments are unverified.")
            index += 1
            continue
        if not equals:
            value = True
            if index + 1 < len(args) and not args[index + 1].startswith("--"):
                index += 1
                value = args[index]
        if flag in settings:
            warnings.append(f"Duplicate launch flag {flag}; precedence is unverified.")
        order.append(flag)
        if flag not in allowed_flags and flag != "--speculative-config":
            warnings.append(f"Unknown launch option {flag}; its value and effects are unverified.")
            value = "[redacted]"
        elif flag == "--config":
            warnings.append("Launch configuration-file merging and defaults are unverified.")
        elif flag in BOOLEAN_FLAGS and value is not True:
            warnings.append(f"Unsupported boolean representation for {flag}.")
        elif flag not in BOOLEAN_FLAGS and value is True:
            warnings.append(f"Missing launch value for {flag}.")
        if _redacted(value):
            warnings.append(f"The value of {flag} is redacted; equivalence is unverified.")
        else:
            try:
                if flag == "--speculative-config":
                    value = _speculation(value)
                elif flag in SETTING_FIELDS:
                    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
                        raise ValueError("unsupported integer")
                    value = int(value)
                    if value <= 0:
                        raise ValueError("unsupported integer")
            except (ValueError, TypeError, RecursionError):
                value = "[redacted]"
                warnings.append(f"Unsupported or redacted value for {flag}; equivalence is unverified.")
        settings.setdefault(flag, []).append(value)
        index += 1
    if {"--enable-prefix-caching", "--no-enable-prefix-caching"} <= settings.keys():
        warnings.append("Competing prefix-caching flags; precedence is unverified.")
    return {flag: values[0] if len(values) == 1 else values for flag, values in settings.items()}, order, warnings


def _sources(snapshot, field):
    row = snapshot.get("fields", {}).get(field) or {}
    return {key: value for key, value in row.items() if isinstance(value, dict)}


def _same_context(before, after):
    return all(before.get(key) == after.get(key) for key in ("provenance", "scope", "status"))


def compare_launch_settings(current, baseline, intended, allowed_flags):
    """Return the comparison, covered whole fields, and observed additional settings."""
    declared = {flag for flag in SETTING_FIELDS if f"{LAUNCH_FIELD}.{flag}" in intended}
    whole_field = LAUNCH_FIELD in intended
    result = {"status": "not_requested", "settings": [], "warnings": []}
    if whole_field:
        result.update(status="whole_field", warnings=[WHOLE_FIELD_WARNING])
    unsupported = [field for field in intended if isinstance(field, str)
                   and field.startswith(LAUNCH_FIELD + ".")
                   and field not in {f"{LAUNCH_FIELD}.{flag}" for flag in SETTING_FIELDS}]
    if unsupported:
        result.update(status="unverified", warnings=result["warnings"] + [
            "Unsupported individual launch setting declaration; equivalence is unverified."])
    if not declared:
        return result, set(), False
    result["status"] = "checked"
    before, after = _sources(baseline, LAUNCH_FIELD), _sources(current, LAUNCH_FIELD)
    covered, parsed, extra = set(), {}, False
    comparable = bool(before and after and before.keys() == after.keys())
    for source in sorted(before.keys() | after.keys()):
        old, new = before.get(source, {}), after.get(source, {})
        left, left_order, warnings = _parse(old.get("value"), allowed_flags)
        right, right_order, more = _parse(new.get("value"), allowed_flags)
        result["warnings"].extend(warnings + more)
        parsed[source] = (left, right)
        if left is None or right is None:
            comparable = False
            continue
        if not _same_context(old, new):
            comparable = False
            result["warnings"].append("Launch evidence provenance, scope or status changed; equivalence is unverified.")
        if [flag for flag in left_order if flag in right] != [flag for flag in right_order if flag in left]:
            result["warnings"].append("Launch argument order changed; precedence is unverified.")
        for flag in sorted(left.keys() | right.keys()):
            if flag in left and flag in right and left[flag] == right[flag]:
                continue
            change = ("added" if flag not in left else "removed" if flag not in right
                      else "unverified" if _redacted(left[flag]) or _redacted(right[flag]) else "changed")
            is_intended = flag in declared or whole_field
            result["settings"].append({"flag": flag, "source": source, "change": change,
                                       "baseline": left.get(flag), "current": right.get(flag),
                                       "intended": is_intended})
            extra |= not is_intended and change != "unverified"
    if comparable:
        covered.add(LAUNCH_FIELD)
    else:
        result["warnings"].append("Launch evidence is missing or not comparable in both runs.")
    for flag in declared:
        field = SETTING_FIELDS[flag]
        old_meta, new_meta = _sources(baseline, field), _sources(current, field)
        if not old_meta and not new_meta:
            continue
        equivalent = comparable
        for source in old_meta.keys() | new_meta.keys():
            old, new = old_meta.get(source, {}), new_meta.get(source, {})
            if old and new:
                equivalent &= _same_context(old, new)
            for row, command in zip((old, new), parsed.get(source, (None, None)), strict=True):
                value = row.get("value")
                try:
                    if flag == "--speculative-config":
                        value = _speculation(value, metadata=True)
                    elif value is not None and (type(value) is not int or value <= 0):
                        raise ValueError("unsupported metadata integer")
                    equivalent &= command is not None and value == command.get(flag)
                except (ValueError, TypeError, RecursionError):
                    equivalent = False
        if equivalent:
            covered.add(field)
        else:
            result["warnings"].append(f"Launch/metadata equivalence for {field} could not be established.")
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    if result["warnings"]:
        result["status"] = "whole_field" if whole_field else "unverified"
    return result, covered, extra
