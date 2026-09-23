"""Local-only diagnosis of the synthetic small-png incident (no cloud writes).

Compares stored original, real SDK/connector serialization, and the Base64
literal observed in the ChatGPT Python call. The last item is NOT a captured
network payload. This fixture inspector only supports RGB8/filter-0 PNGs.
"""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx2
from nexus.artifacts import Access, ArtifactStore, LocalBinaryProvider
from nexus.connector import local_response
from nexus.server import build_server

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = "8cc35def2b5017783ddb06748555ca36c212c8ff4f6343f96c05224fdcc76df0"
# Synthetic bytes copied from the observed Python TOOL CALL, not AI reasoning.
OBSERVED_LITERAL = "iVBORw0KGgoAAAANSUhEUgAAAMAAAABACAIAAADDDu+IAAAAAklEQVR4nGKkkSsAAAGUSURBVOXQsQ0AQQzDsOy/9P0QLoiHAPVMfO/u1/kL4g9ov76/5ue0X99f83Par++v+Tnt1/fX/Jz26/trfk779f01P6f9+v6an9N+fX/Nz2m/vr/m57Rf31/zc9qv76/5Oe3X99f8nPbr+2t+Tvv1/TU/p/36/pqf0359f83Pab++v+bntF/fX/Nz2q/vr/k57df31/yc9uv7a35O+/X9NT+n/fr+mp/Tfn1/zc9pv76/5ue0X99f83Par++v+Tnt1/fX/Jz26/trfk779f01P6f9+v6an9N+fX/Nz2m/vr/m57Rf31/zc9qv76/5Oe3X99f8nPbr+2t+Tvv1/TU/p/36/ppf+wCrPNLCOLddSQAAAABJRU5ErkJggg=="


def inspect_fixture(data):
    result = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    try:
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("invalid PNG signature")
        pos, compressed, ended = 8, bytearray(), False
        width = height = 0
        while pos < len(data):
            if pos + 12 > len(data):
                raise ValueError("truncated chunk header")
            length = struct.unpack(">I", data[pos:pos+4])[0]
            kind = data[pos+4:pos+8]
            end = pos + 12 + length
            if end > len(data):
                raise ValueError(f"truncated {kind.decode('ascii')} chunk: expected end {end}, file size {len(data)}")
            payload = data[pos+8:end-4]
            if zlib.crc32(kind + payload) != struct.unpack(">I", data[end-4:end])[0]:
                raise ValueError("PNG chunk CRC mismatch")
            if kind == b"IHDR":
                width, height, depth, color, comp, filt, interlace = struct.unpack(">IIBBBBB", payload)
                if (depth, color, comp, filt, interlace) != (8, 2, 0, 0, 0):
                    raise ValueError("outside RGB8 non-interlaced fixture scope")
                result["dimensions"] = [width, height]
            elif kind == b"IDAT":
                compressed.extend(payload)
            elif kind == b"IEND":
                ended = True
                if end != len(data):
                    raise ValueError("trailing bytes")
            pos = end
        if not ended:
            raise ValueError("missing IEND")
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(bytes(compressed)) + decoder.flush()
        if not decoder.eof or decoder.unused_data:
            raise ValueError("incomplete or extra zlib stream")
        stride = width * 3 + 1
        if len(pixels) != stride * height or any(pixels[y*stride] != 0 for y in range(height)):
            raise ValueError("outside filter-0 fixture scope or wrong scanline length")
        result["valid_fixture"] = True
        result["bottom_rgb_samples"] = [list(pixels[(height-1)*stride+1+x*3:(height-1)*stride+4+x*3]) for x in (0, 63, 64, 127, 128, 191)]
        result["black_pixel_count"] = sum(pixels[y*stride+1+x*3:y*stride+4+x*3] == b"\0\0\0" for y in range(height) for x in range(width))
    except (ValueError, zlib.error, struct.error) as error:
        result.update(valid_fixture=False, error=str(error))
    return result


async def main():
    data_root = ROOT / "runtime/local-relay-probe"
    original = (data_root / "blobs" / EXPECTED).read_bytes()
    assert hashlib.sha256(original).hexdigest() == EXPECTED
    store = ArtifactStore(data_root / "manifest.sqlite3", LocalBinaryProvider(data_root / "blobs"))
    app = build_server(store, Access("remote-owner", "synthetic-probe")).streamable_http_app(json_response=True, stateless_http=True)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://127.0.0.1:8000") as client:
            status, body = await local_response(client, {"protocol": "2025-11-25", "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_original", "arguments": {"artifact_id": "small-png", "revision": 1}}})})
    assert status == 200
    result = json.loads(body)["result"]
    image = next(item for item in result["content"] if item["type"] == "image")
    decoded = base64.b64decode(image["data"], validate=True)
    reconstructed = base64.b64decode(OBSERVED_LITERAL, validate=True)
    report = {
        "scope": "local original and real SDK/connector serialization; not live Cloudflare or ChatGPT binary capture",
        "original": inspect_fixture(original),
        "sdk_serialized_image": inspect_fixture(decoded),
        "sdk_equals_original": decoded == original,
        "chatgpt_python_literal": inspect_fixture(reconstructed),
        "chatgpt_literal_source": "observed Python tool call in synthetic test conversation; not original image asset",
        "conversation": "https://chatgpt.com/c/6aaf0f08-f004-83ee-8eda-8a2a132cb431",
    }
    assert report["original"]["valid_fixture"] and report["sdk_equals_original"]
    assert not report["chatgpt_python_literal"]["valid_fixture"]
    assert report["chatgpt_python_literal"]["sha256"] == "de29afe216eefcafd4a934f495d2fd185d3755d74f9883525c67bdfcd16f01b6"
    (ROOT / "docs/evidence/original-fidelity-local-2026-09-20.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
