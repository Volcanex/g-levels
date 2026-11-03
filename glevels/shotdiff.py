"""Mechanical half of correct.kit.studio-unchanged: compare before and after screenshots.

    shotdiff.py BEFORE_DIR AFTER_DIR

Pairs PNGs by their path under each directory (subdirectories included) and prints, for each
pair, the share of pixels that changed in any channel and the largest change in one channel,
or that the sizes differ. Then it lists the screenshots only one side has. It reports and never
judges: whether a change is visible, and whether it matters, is the reviewer's call with the two
screenshots open side by side.

Exit 0 when it compared, 2 when it could not run (a directory missing, no PNG on either side,
Pillow not installed).
"""
import sys
from pathlib import Path


def pngs(root):
    return {str(p.relative_to(root)): p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() == ".png"}


def compare(before, after):
    """(changed pixels, total pixels, largest channel change), or a string saying why not."""
    from PIL import Image, ImageChops
    with Image.open(before) as a, Image.open(after) as b:
        a, b = a.convert("RGBA"), b.convert("RGBA")
    if a.size != b.size:
        return f"size differs: {a.size[0]}x{a.size[1]} before, {b.size[0]}x{b.size[1]} after"
    channels = ImageChops.difference(a, b).split()
    peak = channels[0]
    for c in channels[1:]:
        peak = ImageChops.lighter(peak, c)
    hist = peak.histogram()
    total = a.size[0] * a.size[1]
    return total - hist[0], total, max((v for v, n in enumerate(hist) if n), default=0)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or not all(argv):
        print("usage: shotdiff.py BEFORE_DIR AFTER_DIR")
        return 2
    before_dir, after_dir = Path(argv[0]), Path(argv[1])
    for d in (before_dir, after_dir):
        if not d.is_dir():
            print(f"{d} is not a directory")
            return 2
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Pillow is not installed for this python: pip install pillow, or run it on h")
        return 2
    before, after = pngs(before_dir), pngs(after_dir)
    if not before and not after:
        print(f"no PNG under {before_dir} or {after_dir}")
        return 2
    counts = {"identical": 0, "changed": 0, "size differs": 0, "unreadable": 0}
    width = max(len(k) for k in before.keys() | after.keys())
    for name in sorted(before.keys() & after.keys()):
        try:
            result = compare(before[name], after[name])
        except Exception as e:  # a corrupt or truncated file is a line in the report, not a crash
            counts["unreadable"] += 1
            print(f"{name:<{width}}  unreadable: {e}")
            continue
        if isinstance(result, str):
            counts["size differs"] += 1
            print(f"{name:<{width}}  {result}")
            continue
        changed, total, peak = result
        counts["identical" if not changed else "changed"] += 1
        print(f"{name:<{width}}  {100 * changed / total:8.4f}% changed ({changed} of {total} px), "
              f"largest channel change {peak}")
    only_before = sorted(before.keys() - after.keys())
    only_after = sorted(after.keys() - before.keys())
    for name in only_before:
        print(f"missing after: {name}")
    for name in only_after:
        print(f"new after:     {name}")
    print(f"compared {len(before.keys() & after.keys())}: "
          + ", ".join(f"{n} {k}" for k, n in counts.items())
          + f"; missing after {len(only_before)}, new after {len(only_after)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
