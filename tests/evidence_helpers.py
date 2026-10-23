"""Keep optional capability setup distinct in workload integration assertions."""


def workload_records(records):
    result = []
    for record in records:
        value = record if isinstance(record, dict) else record.model_dump()
        if value.get("scenario") == "performance_evidence_probe":
            assert not value.get("check_ids")
            if "workload" in value:
                assert value["workload"].get("performance_evidence_probe") is True
            continue
        result.append(record)
    return result


def workload_payloads(payloads):
    # HTTP fixture payloads have no scenario field. Only the exact one-token
    # capability request is excluded; all actual workload payloads stay asserted.
    return [value for value in payloads if not (
        value.get("max_tokens") == 1 and value.get("messages") == [
            {"role": "user", "content": "Reply with 1."}])]
