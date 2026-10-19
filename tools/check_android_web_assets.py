"""Read-only check that an APK contains exactly the current Web resources."""
import argparse
from collections import Counter
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile


def check_web_assets(apk_path, root):
    root = Path(root)
    expected = {path.relative_to(root).as_posix(): path
                for directory in ("static", "templates")
                for path in (root / directory).rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}
    if not expected:
        return ["No source Web resources found"]
    with ZipFile(apk_path) as apk, ZipFile(BytesIO(apk.read("assets/chaquopy/app.imy"))) as bundle:
        names = [name for name in bundle.namelist()
                 if name.startswith(("static/", "templates/")) and not name.endswith("/")]
        problems = [f"Duplicate: {name}" for name, count in Counter(names).items() if count > 1]
        actual = set(names)
        problems += [f"Missing: {name}" for name in sorted(expected.keys() - actual)]
        problems += [f"Obsolete: {name}" for name in sorted(actual - expected.keys())]
        problems += [f"Content mismatch: {name}" for name in sorted(actual & expected.keys())
                     if bundle.read(name) != expected[name].read_bytes()]
        return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        problems = check_web_assets(args.apk, args.root)
    except Exception as error:
        parser.exit(1, f"APK Web resource verification failed: {error}\n")
    if problems:
        parser.exit(1, "\n".join(problems) + "\n")
    print("APK Web resources match source exactly (no missing, stale or duplicate files)")


if __name__ == "__main__":
    main()
