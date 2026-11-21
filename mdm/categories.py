"""
File categorization module for My Download Manager (Stage 5).
Categorizes files based on extension with support for user overrides.
"""
from pathlib import Path
from typing import Dict, Set

CATEGORY_EXTENSIONS: Dict[str, Set[str]] = {
    "Videos": {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg"},
    "Audio": {"mp3", "wav", "flac", "aac", "ogg", "wma", "m4a", "opus"},
    "Documents": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "epub", "csv"},
    "Images": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "ico", "tiff"},
    "Archives": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz"},
    "Software": {"exe", "msi", "iso", "dmg", "pkg", "deb", "rpm", "apk", "appimage", "msp"},
}

ALL_CATEGORIES = [
    "All",
    "Videos",
    "Audio",
    "Documents",
    "Images",
    "Archives",
    "Software",
    "Other",
]


def get_category_for_filename(filename: str) -> str:
    """
    Determines category based on file extension.
    Returns one of: Videos, Audio, Documents, Images, Archives, Software, Other.
    """
    if not filename:
        return "Other"

    # Extract extension without leading dot
    p = Path(filename)
    ext = p.suffix.lower().lstrip(".")
    if not ext:
        return "Other"

    for category, extensions in CATEGORY_EXTENSIONS.items():
        if ext in extensions:
            return category

    return "Other"
