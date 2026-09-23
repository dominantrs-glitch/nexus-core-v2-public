"""Generate synthetic originals locally. No private files or outbound requests."""
import argparse
import json
from pathlib import Path
import struct
import tempfile
import zipfile
import zlib

from nexus.artifacts import Access, ArtifactStore, LocalBinaryProvider


def write_png(path: Path, width: int, height: int, compression: int):
    def chunk(output, kind, data):
        output.write(struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data)))
    with path.open("xb") as output:
        output.write(b"\x89PNG\r\n\x1a\n")
        chunk(output, b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        compressor = zlib.compressobj(compression)
        # Three vertical stripes: red, green, blue. Deliberately uncompressed in
        # the large fixture so transport is exercised without private imagery.
        row = b"\x00" + b"\xff\x00\x00" * (width // 3) + b"\x00\xff\x00" * (width // 3) + b"\x00\x00\xff" * (width - 2 * (width // 3))
        for _ in range(height):
            data = compressor.compress(row)
            if data:
                chunk(output, b"IDAT", data)
        chunk(output, b"IDAT", compressor.flush())
        chunk(output, b"IEND", b"")


def create(directory: Path):
    directory.mkdir(parents=True, exist_ok=False)
    store = ArtifactStore(directory / "manifest.sqlite3", LocalBinaryProvider(directory / "blobs"))
    manifests = []
    with tempfile.TemporaryDirectory(dir=directory) as staging:
        temp = Path(staging)
        for name, width, height, level in [("small-png", 192, 64, 6), ("large-png", 3072, 3072, 0)]:
            image = temp / (name + ".png")
            write_png(image, width, height, level)
            with image.open("rb") as source:
                store.register_stream(name, "synthetic-probe", source, "image/png", ("remote-owner", "local-owner"))
            manifests.append(store.describe(name, 1, Access("local-owner", "synthetic-probe")))
        archive = temp / "synthetic.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as target:
            with target.open("synthetic-data.txt", "w") as output:
                for _ in range(41):
                    output.write(b"synthetic-only\n" * 74898 + b"x" * 4)  # ~1.07 MiB synthetic blocks
        with archive.open("rb") as source:
            store.register_stream("large-zip", "synthetic-probe", source, "application/zip", ("remote-owner", "local-owner"))
        manifests.append(store.describe("large-zip", 1, Access("local-owner", "synthetic-probe")))
    (directory / "fixture-manifest.json").write_text(json.dumps(manifests, indent=2), encoding="utf-8")
    return manifests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(create(args.data), indent=2))


if __name__ == "__main__":
    main()
