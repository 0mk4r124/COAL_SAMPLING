"""
SCRIPTS/DEPENDANT/RAW_IMAGE_UTILS.py

Package the raw model-training images collected during a sampling session
(RAW_IMG/<uid>/<CAM1|CAM2|CAM3>/*.jpg) into ZIP archives for the
end-of-cycle mail.

IMPORTANT — the images are archived BYTE-FOR-BYTE. Nothing is resized,
recompressed, rotated or re-encoded: the 3840x2160 originals go into the zip
exactly as the cameras wrote them, so they stay usable as model training data.

Because JPEG is already compressed, zip DEFLATE would only save ~1-2% while
burning a lot of CPU on 4K frames — so the archive uses ZIP_STORED (container
only). Expect the zip to be roughly the same size as the folder.

Since a full-resolution session is far too large to attach to a mail, the
archive is split into parts (default 200 MB each) that get uploaded and
linked instead.

Entry point:
    parts, n_images, total_mb = archive_raw_images(uid)
        parts -> list of zip file paths (1 or more)
"""

import os
import zipfile
import logging
import traceback

logger = logging.getLogger(__name__)

BASE_FILE_PATH = os.environ.get('BASE_FILE_PATH', 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/')
RAW_PATH = os.path.join(BASE_FILE_PATH, "RAW_IMG")

IMG_EXTS = (".jpg", ".jpeg", ".png")

# Max size of ONE zip part. Keeps each OneDrive upload to a sane size so a
# network hiccup only costs one part instead of the whole session.
# Set to 0 to always produce a single zip regardless of size.
PART_SIZE_MB = 200


def _iter_images(session_dir: str):
    """Yield (absolute_path, arcname, size_bytes) for every image in the session."""
    for root, _dirs, files in os.walk(session_dir):
        for fname in sorted(files):
            if not fname.lower().endswith(IMG_EXTS):
                continue
            src = os.path.join(root, fname)
            try:
                size = os.path.getsize(src)
            except OSError:
                continue
            # Keep the CAM1/CAM2/CAM3 folder structure inside the zip
            yield src, os.path.relpath(src, session_dir), size


def archive_raw_images(uid: str, part_size_mb: int = PART_SIZE_MB) -> tuple[list[str], int, float]:
    """
    Archive every raw image of a session into one or more zips, UNMODIFIED.

    Input : RAW_IMG/<uid>/<CAM_NAME>/<uid>_<timestamp>_<count>.jpg
    Output: RAW_IMG/<uid>_raw_images.zip                  (single part)
            RAW_IMG/<uid>_raw_images_part1.zip, _part2.zip …  (split)

    Returns (list_of_zip_paths, image_count, total_size_mb).
    Returns ([], 0, 0.0) when the session has no raw images or on failure —
    never raises, so the mail flow can't crash the state machine.
    """
    session_dir = os.path.join(RAW_PATH, uid)
    if not os.path.isdir(session_dir):
        logger.warning(f"[RAW_ZIP] No raw image folder for UID {uid}: {session_dir}")
        return [], 0, 0.0

    images = list(_iter_images(session_dir))
    if not images:
        logger.warning(f"[RAW_ZIP] No images found for UID {uid}")
        return [], 0, 0.0

    raw_mb = sum(sz for _s, _a, sz in images) / (1024 * 1024)
    print(f"[RAW_ZIP] UID {uid}: {len(images)} images, {raw_mb:.1f} MB on disk")
    logger.info(f"[RAW_ZIP] UID {uid}: {len(images)} images, {raw_mb:.1f} MB on disk")

    limit_bytes = part_size_mb * 1024 * 1024 if part_size_mb else 0
    split = bool(limit_bytes) and (raw_mb > part_size_mb)

    parts: list[str] = []
    count = 0

    def _part_path(idx: int) -> str:
        if not split:
            return os.path.join(RAW_PATH, f"{uid}_raw_images.zip")
        return os.path.join(RAW_PATH, f"{uid}_raw_images_part{idx}.zip")

    try:
        part_idx = 1
        zf = zipfile.ZipFile(_part_path(part_idx), "w", zipfile.ZIP_STORED,
                             allowZip64=True)
        parts.append(_part_path(part_idx))
        part_bytes = 0

        for src, arcname, size in images:
            # Roll over to a new part BEFORE exceeding the limit
            # (a single file always goes in, even if larger than the limit)
            if split and part_bytes > 0 and (part_bytes + size) > limit_bytes:
                zf.close()
                part_idx += 1
                zf = zipfile.ZipFile(_part_path(part_idx), "w", zipfile.ZIP_STORED,
                                     allowZip64=True)
                parts.append(_part_path(part_idx))
                part_bytes = 0

            try:
                # ZIP_STORED -> bytes are copied verbatim, image untouched
                zf.write(src, arcname)
                part_bytes += size
                count += 1
            except Exception:
                logger.error(f"[RAW_ZIP] Skipped {src}:\n{traceback.format_exc()}")

        zf.close()

        # Drop any empty trailing part
        parts = [p for p in parts if os.path.exists(p) and os.path.getsize(p) > 0]

        if count == 0 or not parts:
            logger.warning(f"[RAW_ZIP] Zero images archived for UID {uid}")
            cleanup_archives(parts)
            return [], 0, 0.0

        total_mb = sum(os.path.getsize(p) for p in parts) / (1024 * 1024)
        print(f"[RAW_ZIP] {count} images -> {len(parts)} zip part(s), {total_mb:.1f} MB")
        logger.info(f"[RAW_ZIP] UID {uid}: {count} images -> {len(parts)} part(s), {total_mb:.1f} MB")
        return parts, count, round(total_mb, 2)

    except Exception:
        logger.error(f"[RAW_ZIP] Archive failed for UID {uid}:\n{traceback.format_exc()}")
        cleanup_archives(parts)
        return [], 0, 0.0


def cleanup_archives(paths: list[str]) -> None:
    """Delete the zip parts once they're uploaded — the originals stay on disk."""
    for p in paths or []:
        try:
            if p and os.path.exists(p):
                os.remove(p)
                logger.debug(f"[RAW_ZIP] Removed {p}")
        except OSError:
            logger.warning(f"[RAW_ZIP] Could not remove {p}")