"""The local UI trusts literal loopback authorities, never arbitrary DNS names."""

import httpx
import pytest

from giraffe.models import RunConfig
from giraffe.reporting import write_report
from giraffe.web import create_app
from tests.test_web import config, report


@pytest.fixture
async def local_client(tmp_path):
    write_report(report(RunConfig.model_validate(config())), tmp_path / "synthetic-host-check")
    app = create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8787") as client:
        yield client


async def request_with_hosts(client, method, path, hosts, **kwargs):
    request = client.build_request(method, path, **kwargs)
    # HTTPX otherwise supplies Host automatically. Preserve absent/duplicate raw fields.
    headers = [(name, value) for name, value in request.headers.raw if name.lower() != b"host"]
    request.headers = httpx.Headers(headers + [(b"host", host) for host in hosts])
    return await client.send(request)


@pytest.mark.parametrize("host", [
    b"localhost", b"LOCALHOST", b"localhost:8787", b"localhost:80",
    b"localhost:65535", b"localhost:0", b"localhost:0008787",
    b"127.0.0.1", b"127.0.0.1:8787", b"127.2.3.4:8787", b"127.255.255.255",
    b"[::1]", b"[::1]:8787", b"[0:0:0:0:0:0:0:1]:8787", b"[::0001]:65535",
])
async def test_loopback_authorities_support_reads_and_same_origin_mutations(local_client, tmp_path, host):
    response = await request_with_hosts(local_client, "GET", "/api/runs", [host])
    assert response.status_code == 200
    assert response.json()["runs"][0]["id"] == "synthetic-host-check"
    response = await request_with_hosts(
        local_client, "POST", "/api/runs/synthetic-host-check/keep", [host],
        headers={"Origin": "http://" + host.decode("ascii"), "Sec-Fetch-Site": "same-origin"},
        json={"keep": True},
    )
    assert response.status_code == 200, response.text
    assert (tmp_path / "synthetic-host-check" / ".keep").is_file()


@pytest.mark.parametrize("hosts", [
    [], [b""], [b"attacker.invalid:8787"], [b"testserver"],
    [b"localhost.attacker.invalid"], [b"attacker.localhost"], [b"localhost."],
    [b"0.0.0.0"], [b"192.168.1.2"], [b"128.0.0.1"], [b"[::]"], [b"[::2]"],
    [b"127.1"], [b"2130706433"], [b"0177.0.0.1"], [b"127.00.0.1"], [b"0x7f000001"],
    [b"localhost:evil"], [b"localhost:"], [b"localhost:-1"], [b"localhost:+80"],
    [b"localhost:65536"], [b"localhost:999999999999999999999999999"],
    [b"[::1]:65536"], [b"127.0.0.1:65536"], [b"localhost:80:90"],
    [b"http://localhost"], [b"user@localhost"], [b"localhost@attacker.invalid"],
    [b"localhost/path"], [b"localhost?query"], [b"localhost#fragment"],
    [b"localhost\\attacker.invalid"], [b"localhost,attacker.invalid"],
    [b" localhost"], [b"localhost "], [b"local\thost"], [b"localhost\r\n"],
    [b"local%68ost"], [b"localhost%00"], [b"localhost\x00"], [b"localho\xeft"],
    [b"[::1"], [b"::1"], [b"::1:8787"], [b"[::1]evil.invalid"],
    [b"[127.0.0.1]"], [b"[::1%lo0]"], [b"[::1%25lo0]"], [b"[v1.localhost]"],
    [b"localhost:8787", b"attacker.invalid:8787"],
    [b"attacker.invalid:8787", b"localhost:8787"],
    [b"localhost:8787", b"localhost:8787"],
])
async def test_untrusted_or_ambiguous_authorities_block_reads_and_mutations(local_client, tmp_path, hosts):
    for method, path, kwargs in [
        ("GET", "/api/runs/synthetic-host-check", {}),
        ("POST", "/api/runs/synthetic-host-check/keep", {
            "json": {"keep": True},
            "headers": {"Origin": "http://attacker.invalid:8787", "Sec-Fetch-Site": "same-origin"},
        }),
    ]:
        response = await request_with_hosts(local_client, method, path, hosts, **kwargs)
        assert response.status_code == 400, (hosts, response.status_code, response.text)
        assert response.json() == {"detail": "Use a local Giraffe host"}
    assert not (tmp_path / "synthetic-host-check" / ".keep").exists()


@pytest.mark.parametrize("path", [
    "/", "/static/app.js", "/openapi.json", "/api/bootstrap", "/api/storage",
    "/api/runs", "/api/runs/synthetic-host-check/report.json", "/missing",
])
async def test_attacker_matching_host_and_origin_cannot_read_any_http_entry(local_client, path):
    response = await local_client.get(path, headers={
        "Host": "attacker.invalid:8787", "Origin": "http://attacker.invalid:8787",
        "Sec-Fetch-Site": "same-origin",
    })
    assert response.status_code == 400


@pytest.mark.parametrize("origin", [None, "http://attacker.invalid:8787"])
async def test_attacker_host_cannot_set_keep_with_matching_or_absent_origin(local_client, tmp_path, origin):
    headers = {"Host": "attacker.invalid:8787", "Sec-Fetch-Site": "same-origin"}
    if origin is not None:
        headers["Origin"] = origin
    response = await local_client.post("/api/runs/synthetic-host-check/keep",
                                       headers=headers, json={"keep": True})
    assert response.status_code == 400
    assert not (tmp_path / "synthetic-host-check" / ".keep").exists()


async def test_forwarded_headers_cannot_replace_host_or_origin(local_client, tmp_path):
    spoofed_local = {"Host": "attacker.invalid:8787", "X-Forwarded-Host": "localhost:8787",
                     "Forwarded": 'for=127.0.0.1;host="localhost:8787";proto=http',
                     "Origin": "http://attacker.invalid:8787"}
    assert (await local_client.get("/api/runs", headers=spoofed_local)).status_code == 400
    response = await local_client.post("/api/runs/synthetic-host-check/keep", headers=spoofed_local,
                                       json={"keep": True})
    assert response.status_code == 400
    forwarded_remote = {"Host": "localhost:8787", "X-Forwarded-Host": "attacker.invalid:8787",
                        "Forwarded": 'host="attacker.invalid:8787";proto=http'}
    assert (await local_client.get("/api/runs", headers=forwarded_remote)).status_code == 200
    response = await local_client.post("/api/runs/synthetic-host-check/keep", json={"keep": True},
        headers={**forwarded_remote, "Origin": "http://attacker.invalid:8787"})
    assert response.status_code == 403
    assert not (tmp_path / "synthetic-host-check" / ".keep").exists()
    response = await local_client.post("/api/runs/synthetic-host-check/keep", json={"keep": True},
        headers={**forwarded_remote, "Origin": "http://localhost:8787"})
    assert response.status_code == 200


@pytest.mark.parametrize("headers,status", [
    ({}, 200),
    ({"Origin": "http://127.0.0.1:8787"}, 200),
    ({"Origin": "http://attacker.invalid:8787"}, 403),
    ({"Origin": "http://127.0.0.1:8788"}, 403),
    ({"Origin": "https://127.0.0.1:8787"}, 403),
    ({"Origin": "null"}, 403),
    ({"Origin": "http://["}, 403),
    ({"Origin": "http://127.0.0.1:8787", "Sec-Fetch-Site": "cross-site"}, 403),
])
async def test_loopback_mutations_preserve_origin_and_fetch_metadata_checks(local_client, headers, status):
    response = await local_client.post("/api/runs/synthetic-host-check/keep", json={"keep": True},
                                       headers=headers)
    assert response.status_code == status


async def test_loopback_mutations_still_require_json(local_client):
    response = await local_client.post("/api/runs/synthetic-host-check/keep", content='{"keep":true}')
    assert response.status_code == 415
