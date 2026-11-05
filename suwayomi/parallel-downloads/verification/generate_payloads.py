#!/usr/bin/env python3
"""Generate deterministic valid ~0.5-1MiB PNG payloads on host for native probe."""
import hashlib
import json
import os
import random
import struct
import sys
import zlib
from pathlib import Path

PAYLOAD_COUNT = 12
WIDTH = 500
HEIGHT = 500

def create_deterministic_png(seed: int, width: int = WIDTH, height: int = HEIGHT) -> bytes:
    rng = random.Random(seed)
    raw_rows = []
    for _ in range(height):
        # 1 filter byte (0 = None) followed by width * 3 RGB bytes
        raw_rows.append(b'\x00' + rng.randbytes(width * 3))
    raw_data = b''.join(raw_rows)
    compressed = zlib.compress(raw_data, level=6)

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        length = struct.pack('>I', len(data))
        crc = struct.pack('>I', zlib.crc32(chunk_type + data) & 0xffffffff)
        return length + chunk_type + data + crc

    ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr_data) + chunk(b'IDAT', compressed) + chunk(b'IEND', b'')

def main():
    if len(sys.argv) > 1:
        target_dir = Path(sys.argv[1]).resolve()
    else:
        # Default to ignored .work/payloads
        repo_root = Path(__file__).resolve().parents[3]
        target_dir = (repo_root / ".work" / "payloads").resolve()

    target_dir.mkdir(parents=True, exist_ok=True)
    manifest = {}
    print(f"Generating {PAYLOAD_COUNT} deterministic PNG payloads in {target_dir}...")
    for i in range(PAYLOAD_COUNT):
        seed = 0x12345678 ^ (i * 0x9e3779b9)
        png_bytes = create_deterministic_png(seed)
        out_path = target_dir / f"payload_{i:02d}.png"
        out_path.write_bytes(png_bytes)
        sha = hashlib.sha256(png_bytes).hexdigest()
        manifest[out_path.name] = {
            "bytes": len(png_bytes),
            "sha256": sha,
            "seed": seed,
        }
        print(f"  {out_path.name}: {len(png_bytes)} bytes ({len(png_bytes) / 1024:.1f} KiB) sha256={sha[:12]}...")

    manifest_path = target_dir / "payload-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"Done generating payloads. Manifest written to {manifest_path}")

if __name__ == '__main__':
    main()
