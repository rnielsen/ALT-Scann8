"""Frame storage shared by the scanner and AfterScan (keep both copies in sync)."""
import fnmatch
import os
import re

FRAMES_PER_FOLDER = 1000
FRAME_NAME = re.compile(r"^(?:picture|frame|picture_out|hdrpic|still-picture)-(\d+)(?:\.\d+|-\d+)?\.[^.]+$", re.IGNORECASE)


def frame_number(filename):
    match = FRAME_NAME.match(os.path.basename(filename))
    if not match:
        raise ValueError("Not a frame filename: " + filename)
    return int(match.group(1))


def frame_path(folder, filename, create=False):
    """Resolve old flat files or a numbered bucket; create parents only for writes.

    All writes use bucket = frame number // 1000, padded to four digits.
    Reads prefer bucket files, then fall back to existing flat frames.
    """
    folder = os.path.abspath(folder)
    bucket = os.path.join(folder, f"{frame_number(filename) // FRAMES_PER_FOLDER:04d}")
    path = os.path.join(bucket, filename)
    flat = os.path.join(folder, filename)
    if not create and not os.path.isfile(path) and os.path.isfile(flat):
        return flat
    if create:
        os.makedirs(bucket, exist_ok=True)
    return path


def frame_files(folder, pattern="*", include_other=False):
    """List only root files and immediate numeric buckets, sorted by frame number.

    Never recurse into output, template or other project directories. Prefer the
    bucket copy if an old flat frame has also been copied into its bucket.
    """
    pattern_re = re.compile(fnmatch.translate(pattern.replace("?????", "__FRAME_NUMBER__"))
                            .replace("__FRAME_NUMBER__", r"[0-9]{5,}"))
    folder = os.path.abspath(folder)
    folders = [folder]
    with os.scandir(folder) as entries:
        folders += sorted(e.path for e in entries
                          if e.name.isascii() and e.name.isdigit() and e.is_dir(follow_symlinks=False))
    found = {}
    for directory in folders:
        with os.scandir(directory) as entries:
            for entry in entries:
                if ((include_other or FRAME_NAME.match(entry.name)) and pattern_re.match(entry.name)
                        and entry.is_file()):
                    found[entry.name] = entry.path
    return sorted(found.values(), key=lambda p: (frame_number(p) if FRAME_NAME.match(os.path.basename(p))
                                                 else float("inf"), os.path.basename(p)))
