"""Verify the V21-T01 Live2D sample end to end.

This is the reproducible evidence entry point for issue #21. It checks the
artefacts the chain is supposed to produce, and it deliberately fails loudly on
the two mistakes that would otherwise pass silently:

**A cutout that is not really transparent.** The approved artwork is flat RGB.
An earlier generated attempt produced a picture *of* a checkerboard instead of
real alpha, so transparency is measured here, not assumed.

**A moc3 the client cannot read.** Cubism 5.3 exports moc3 version 6 by default.
The pinned Aemeath client's Core stops at version 5, so a default export loads
in the editor and is rejected by the client. The version is therefore read
straight out of the file header.

Usage::

    python scripts/verify_live2d_sample.py
    python scripts/verify_live2d_sample.py --export-dir <dir>

Exit code 0 means every check that could run passed; 1 means at least one failed.
Checks whose inputs are absent are reported as ``SKIP`` with the reason, and do
not on their own fail the run -- the export step is manual and may not exist yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The pinned client's Core tops out here. A model above this cannot load.
CLIENT_MAX_MOC_VERSION = 5

#: The approved artwork is the identity anchor and must never be overwritten.
#: The hash is fixed by docs/live2d-production.md.
APPROVED_SHA256 = "252825a4ef3c802d5e9ae4e5a76a242eef4cfc6f8ca8e2af704d31d3215fd123"

APPROVED = ROOT / "docs" / "images" / "live2d" / "aemeath-approved-v4.png"
WORK = ROOT / "references" / "character" / "live2d" / "work"
DEFAULT_EXPORT = ROOT / "references" / "character" / "live2d" / "export"


class Report:
    """Collects check results and decides the exit code."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def ok(self, label: str, detail: str = "") -> None:
        self.passed += 1
        print(f"  PASS  {label}" + (f" -- {detail}" if detail else ""))

    def fail(self, label: str, detail: str) -> None:
        self.failed += 1
        print(f"  FAIL  {label} -- {detail}")

    def skip(self, label: str, reason: str) -> None:
        self.skipped += 1
        print(f"  SKIP  {label} -- {reason}")

    def summary(self) -> int:
        print()
        print(f"{self.passed} passed, {self.failed} failed, {self.skipped} skipped")
        return 1 if self.failed else 0


def sha256(path: Path) -> str:
    """Hex SHA256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_approved(report: Report) -> None:
    """The approved artwork is untouched -- the identity anchor still holds."""
    print("[1] approved artwork integrity")
    if not APPROVED.is_file():
        report.fail("approved artwork present", f"missing: {APPROVED}")
        return

    actual = sha256(APPROVED)
    if actual != APPROVED_SHA256:
        report.fail(
            "approved artwork unmodified",
            f"SHA256 is {actual}, contract says {APPROVED_SHA256}; "
            "the identity anchor was overwritten",
        )
        return
    report.ok("approved artwork unmodified", "SHA256 matches the contract")


def check_cutout(report: Report) -> None:
    """The cutout carries real per-pixel alpha, not a drawn checkerboard."""
    print("[2] transparent cutout")
    cutout = WORK / "aemeath-cutout-rgba.png"
    if not cutout.is_file():
        report.skip("cutout has real alpha", f"missing: {cutout}")
        return

    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency guard
        report.skip("cutout has real alpha", f"pillow/numpy unavailable: {exc}")
        return

    image = Image.open(cutout)
    if image.mode != "RGBA":
        report.fail("cutout is RGBA", f"mode is {image.mode}, expected RGBA")
        return
    report.ok("cutout is RGBA", f"{image.size[0]}x{image.size[1]}")

    alpha = np.asarray(image)[:, :, 3]
    total = alpha.size
    fully_transparent = int((alpha == 0).sum())
    fully_opaque = int((alpha == 255).sum())
    soft = total - fully_transparent - fully_opaque

    if fully_transparent == 0:
        report.fail("cutout has transparent pixels", "no alpha=0 pixels; not a real matte")
    else:
        report.ok(
            "cutout has transparent pixels",
            f"{fully_transparent / total:.1%} of the canvas",
        )

    if soft == 0:
        report.fail("cutout has soft edges", "alpha is binary; edges would alias")
    else:
        report.ok("cutout has soft edges", f"{soft / total:.2%} partial alpha")

    if fully_opaque == 0:
        report.fail("cutout has opaque pixels", "nothing survived the cutout")


def check_psd(report: Report) -> None:
    """The layered PSD exists and carries the expected layer names."""
    print("[3] layered PSD")
    psd = WORK / "aemeath-sample.psd"
    if not psd.is_file():
        report.skip("layered PSD present", f"missing: {psd}")
        return

    blob = psd.read_bytes()
    if not blob.startswith(b"8BPS"):
        report.fail("PSD has a valid signature", "file does not start with 8BPS")
        return
    report.ok("PSD has a valid signature", f"{len(blob)} bytes")

    expected = [
        b"70_hair_front",
        b"60_mouth",
        b"50_brows",
        b"41_eye_R",
        b"40_eye_L",
        b"30_face_base",
        b"10_body_reference",
        b"00_reference_locked",
    ]
    missing = [name.decode() for name in expected if name not in blob]
    if missing:
        report.fail("PSD carries every sample layer", f"missing from file: {missing}")
    else:
        report.ok("PSD carries every sample layer", f"{len(expected)} layers by name")


def read_moc_header(path: Path) -> tuple[str, int]:
    """The magic and version at the head of a moc3 file.

    The format starts with the ASCII magic ``MOC3`` followed by a little-endian
    int32 version, which is what the Core checks before reviving the model.
    """
    with path.open("rb") as handle:
        head = handle.read(8)
    magic = head[:4].decode("ascii", errors="replace")
    version = struct.unpack("<i", head[4:8])[0]
    return magic, version


def check_export(report: Report, export_dir: Path) -> None:
    """The exported model exists and its moc3 version is loadable by the client."""
    print("[4] exported runtime model")

    moc_files = sorted(export_dir.rglob("*.moc3")) if export_dir.is_dir() else []
    if not moc_files:
        report.skip(
            "exported moc3 present",
            f"no .moc3 under {export_dir} (the Cubism export step is manual)",
        )
        return

    for moc in moc_files:
        magic, version = read_moc_header(moc)
        if magic != "MOC3":
            report.fail(f"{moc.name} is a moc3", f"magic is {magic!r}, expected 'MOC3'")
            continue

        if version > CLIENT_MAX_MOC_VERSION:
            report.fail(
                f"{moc.name} loadable by the pinned client",
                f"moc3 version is {version} but the client Core stops at "
                f"{CLIENT_MAX_MOC_VERSION}; re-export with an older "
                "'Export version' in Cubism's export settings",
            )
        else:
            report.ok(
                f"{moc.name} loadable by the pinned client",
                f"moc3 version {version} <= {CLIENT_MAX_MOC_VERSION}",
            )

    # A model3.json next to the moc has to point at files that exist.
    for model3 in sorted(export_dir.rglob("*.model3.json")):
        try:
            data = json.loads(model3.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report.fail(f"{model3.name} parses", str(exc))
            continue
        refs = data.get("FileReferences") or {}
        needed = [refs.get("Moc")] + list(refs.get("Textures") or [])
        missing = [
            name for name in needed
            if name and not (model3.parent / name).is_file()
        ]
        if missing:
            report.fail(f"{model3.name} references resolve", f"missing: {missing}")
        else:
            report.ok(f"{model3.name} references resolve", f"{len(needed)} file refs")


def write_manifest(export_dir: Path) -> None:
    """Record path, size and SHA256 for everything in the export package."""
    if not export_dir.is_dir():
        return
    entries = []
    for path in sorted(export_dir.rglob("*")):
        if path.is_file():
            entries.append(
                {
                    "path": path.relative_to(export_dir).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
    if not entries:
        return
    manifest = export_dir / "MANIFEST.json"
    manifest.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nwrote {manifest} ({len(entries)} files)")


def main() -> int:
    """Run every check and report a single exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--export-dir",
        type=Path,
        default=DEFAULT_EXPORT,
        help="directory holding the exported runtime model (default: %(default)s)",
    )
    args = parser.parse_args()

    report = Report()
    check_approved(report)
    check_cutout(report)
    check_psd(report)
    check_export(report, args.export_dir)
    write_manifest(args.export_dir)
    return report.summary()


if __name__ == "__main__":
    sys.exit(main())
