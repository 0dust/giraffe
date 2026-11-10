"""JSON decoding for evidence received from gateways."""
import json


def parse_gateway_json(body):
    """Read unambiguous JSON for gateway evidence."""
    def unique_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate gateway JSON member")
            value[key] = item
        return value

    def reject_constant(_):
        raise ValueError("Invalid gateway JSON constant")

    try:
        return json.loads(body, object_pairs_hook=unique_keys, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        return None
