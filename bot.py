from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import cv2
from atproto import Client
from atproto.exceptions import AtProtocolError

# -----------------------------------------------------------------------------
# BLUESKY CONFIGURATION
# -----------------------------------------------------------------------------
HANDLE = "malteseshakeframes.bsky.social"
APP_PASSWORD = os.getenv("BLUESKY_APP_PASSWORD") or os.getenv("BSKY_PASSWORD")

# -----------------------------------------------------------------------------
# INTERNET ARCHIVE EPISODE LIBRARY
# The two directories currently contain 13 Season 1 MP4s and 8 Season 2 MP4s.
# The directory is scanned at runtime so new MP4s can be picked up automatically.
# -----------------------------------------------------------------------------
ARCHIVE_ROOT = (
    "https://archive.org/download/"
    "the-twisted-tales-of-felix-the-cat-ntsc_202506/"
)
SEASON_DIRECTORIES = ("S01/", "S02/")
SERIES_TITLE = "The Twisted Tales of Felix the Cat"

# -----------------------------------------------------------------------------
# POSTING / LOCAL STATE
# -----------------------------------------------------------------------------
MAX_BYTES = 950_000
HISTORY_FILE = "random_frame_history.json"
HISTORY_LIMIT = 250
SESSION_FILE = "bsky_session.txt"
TEMP_IMG = "temp_frame.png"
DIRECTORY_TIMEOUT = 45
VIDEO_OPEN_ATTEMPTS = 3
BLUESKY_POST_ATTEMPTS = 3


class ArchiveDirectoryParser(HTMLParser):
    """Collect links from an Internet Archive directory index."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.hrefs.append(href)


def request_text(url, timeout=DIRECTORY_TIMEOUT):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; RandomFrameBot/1.0)"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_episode_catalog():
    """Discover MP4s in both season folders; ignore .sfk and other sidecar files."""
    episodes = []
    seen_urls = set()

    for season_directory in SEASON_DIRECTORIES:
        directory_url = urllib.parse.urljoin(ARCHIVE_ROOT, season_directory)
        print(f"📚 Reading episode directory: {season_directory}")

        last_error = None
        html = None
        for attempt in range(1, 4):
            try:
                html = request_text(directory_url)
                break
            except Exception as exc:
                last_error = exc
                print(
                    f"⚠️ Directory request attempt {attempt}/3 failed: {exc}"
                )
                if attempt < 3:
                    time.sleep(attempt * 4)

        if html is None:
            raise RuntimeError(
                f"Could not read Internet Archive directory {directory_url}: "
                f"{last_error}"
            )

        parser = ArchiveDirectoryParser()
        parser.feed(html)

        season_found = 0
        for href in parser.hrefs:
            if href in ("../", "./") or href.startswith(("#", "?")):
                continue

            video_url = urllib.parse.urljoin(directory_url, href)
            path = urllib.parse.urlsplit(video_url).path
            filename = urllib.parse.unquote(path.rsplit("/", 1)[-1])

            # Episode files end in .mp4. This excludes Vegas .sfk sidecars.
            if not filename.lower().endswith(".mp4"):
                continue

            match = re.search(r"_S(\d{2})E(\d{2})_", filename, re.IGNORECASE)
            if not match:
                continue

            if video_url in seen_urls:
                continue
            seen_urls.add(video_url)

            season_number = int(match.group(1))
            episode_number = int(match.group(2))
            title = filename[match.end():]
            title = re.sub(r"\.mp4-NTSC\.mp4$", "", title, flags=re.IGNORECASE)
            title = re.sub(r"\.mp4$", "", title, flags=re.IGNORECASE)

            episodes.append(
                {
                    "season": season_number,
                    "episode": episode_number,
                    "title": title.replace("_", " ").strip(),
                    "filename": filename,
                    "url": video_url,
                }
            )
            season_found += 1

        print(f"   Found {season_found} MP4 episode file(s) in {season_directory}.")
        if season_found == 0:
            raise RuntimeError(
                f"No episode MP4s were found in {directory_url}. "
                "The directory listing format or archive contents may have changed."
            )

    episodes.sort(key=lambda item: (item["season"], item["episode"], item["filename"]))
    return episodes


def load_history():
    """Load recently posted episode/frame keys to avoid immediate repeats."""
    if not os.path.exists(HISTORY_FILE):
        return []

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as history_file:
            data = json.load(history_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {HISTORY_FILE}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("recent"), list):
        raise RuntimeError(
            f"{HISTORY_FILE} has an unexpected format. Please inspect the file "
            "rather than overwriting posting history."
        )

    return data["recent"][-HISTORY_LIMIT:]


def save_history(history):
    """Atomically save the most recent successful selections."""
    data = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "recent": history[-HISTORY_LIMIT:],
    }
    temporary_path = HISTORY_FILE + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as history_file:
        json.dump(data, history_file, ensure_ascii=False, indent=2)
        history_file.write("\n")
    os.replace(temporary_path, HISTORY_FILE)


def resolve_archive_url(url):
    """Resolve Archive.org's redirect to a direct storage-server URL if possible."""
    try:
        request = urllib.request.Request(
            url,
            method="HEAD",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.url
    except Exception as exc:
        print(
            "⚠️ Could not pre-resolve the Archive.org redirect; "
            f"using the original URL: {exc}"
        )
        return url


def open_random_episode(episode):
    """Open the selected remote MP4 and return its capture and total frame count."""
    print(
        "🎬 Selected episode: "
        f"S{episode['season']:02d}E{episode['episode']:02d} — {episode['title']}"
    )
    stream_url = resolve_archive_url(episode["url"])
    print(f"🔗 Stream endpoint: {stream_url}")

    for attempt in range(1, VIDEO_OPEN_ATTEMPTS + 1):
        cap = cv2.VideoCapture(stream_url)
        if cap.isOpened():
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total_frames > 0:
                print(f"📊 Video opened. Total frames: {total_frames:,}")
                return cap, total_frames
            cap.release()
            print("⚠️ OpenCV opened the video but returned no frame count.")
        else:
            cap.release()
            print(f"⚠️ Video connection attempt {attempt}/{VIDEO_OPEN_ATTEMPTS} failed.")

        if attempt < VIDEO_OPEN_ATTEMPTS:
            time.sleep(attempt * 4)

    raise RuntimeError(
        "Could not open the selected Internet Archive episode or read its frame count."
    )


def choose_random_frame(episode, total_frames, recent_keys):
    """Choose a random frame, avoiding frame selections in the recent history."""
    # The per-episode choice is uniform, then the frame within that episode is uniform.
    for _ in range(100):
        frame_index = secrets.randbelow(total_frames)
        selection_key = f"{episode['filename']}::{frame_index}"
        if selection_key not in recent_keys:
            return frame_index, selection_key

    # Extremely unlikely fallback if many recent records belong to this episode.
    frame_index = secrets.randbelow(total_frames)
    selection_key = f"{episode['filename']}::{frame_index}"
    return frame_index, selection_key


def extract_frame(cap, frame_index):
    """Seek to a zero-based frame index and return the decoded image."""
    seek_ok = cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
    if not seek_ok:
        print("⚠️ OpenCV did not confirm the seek; attempting to read the target frame anyway.")

    ret, frame = cap.read()
    if not ret or frame is None:
        raise RuntimeError(f"Failed to extract selected frame {frame_index + 1:,}.")
    return frame


def write_compressed_frame(frame):
    """Write PNG and resize until it fits below the Bluesky upload-size target."""
    write_ok = cv2.imwrite(
        TEMP_IMG,
        frame,
        [int(cv2.IMWRITE_PNG_COMPRESSION), 3],
    )
    if not write_ok:
        raise RuntimeError("OpenCV could not write the extracted frame image.")

    file_size = os.path.getsize(TEMP_IMG)
    while file_size > MAX_BYTES:
        original_height, original_width = frame.shape[:2]
        width = int(original_width * 0.90)
        height = int(original_height * 0.90)

        if width < 100 or height < 100:
            raise RuntimeError(
                f"Frame image remains too large ({file_size:,} bytes) "
                f"for the {MAX_BYTES:,}-byte upload target."
            )

        frame = cv2.resize(
            frame,
            (width, height),
            interpolation=cv2.INTER_AREA,
        )
        write_ok = cv2.imwrite(
            TEMP_IMG,
            frame,
            [int(cv2.IMWRITE_PNG_COMPRESSION), 4],
        )
        if not write_ok:
            raise RuntimeError("OpenCV could not write the resized frame image.")
        file_size = os.path.getsize(TEMP_IMG)

    print(f"🖼️ Prepared upload image: {file_size:,} bytes.")


def get_authenticated_client():
    """Reuse a saved session, falling back to a Bluesky app-password login."""
    client = Client()

    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as session_file:
                session_string = session_file.read().strip()

            if session_string:
                print("🔑 Resuming saved Bluesky session...")
                client.login(session_string=session_string)
                with open(SESSION_FILE, "w", encoding="utf-8") as session_file:
                    session_file.write(client.export_session_string())
                return client
        except Exception as exc:
            print(f"⚠️ Saved session could not be resumed; logging in fresh: {exc}")

    if not APP_PASSWORD:
        raise RuntimeError(
            "Bluesky app password is missing. Set BLUESKY_APP_PASSWORD "
            "or BSKY_PASSWORD in the workflow environment."
        )

    print("🔐 Performing fresh login with Bluesky App Password...")
    client.login(HANDLE, APP_PASSWORD)
    with open(SESSION_FILE, "w", encoding="utf-8") as session_file:
        session_file.write(client.export_session_string())
    return client


def make_post_text(episode, frame_index, total_frames):
    episode_code = f"S{episode['season']:02d}E{episode['episode']:02d}"
    title_line = episode["title"]
    return (
        "🎲 Random Frame\n"
        f"{SERIES_TITLE}\n"
        f"{episode_code} — {title_line}\n"
        f"Frame {frame_index + 1:,} of {total_frames:,}"
    )


def main():
    print(f"🚀 Random Frame execution started at {datetime.now().strftime('%H:%M:%S')}")

    if not APP_PASSWORD and not os.path.exists(SESSION_FILE):
        raise RuntimeError(
            "Bluesky app password is not configured. Set BLUESKY_APP_PASSWORD "
            "or BSKY_PASSWORD."
        )

    print("🌐 Discovering episodes from Internet Archive...")
    episodes = fetch_episode_catalog()
    print(f"📚 Episode library ready: {len(episodes)} MP4 episode(s).")

    history = load_history()
    recent_keys = {
        item.get("key")
        for item in history
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }

    # Each episode has the same chance of being selected, regardless of duration.
    episode = secrets.choice(episodes)
    cap = None

    try:
        cap, total_frames = open_random_episode(episode)
        frame_index, selection_key = choose_random_frame(
            episode,
            total_frames,
            recent_keys,
        )
        print(f"🎲 Random frame selected: {frame_index + 1:,} / {total_frames:,}")

        frame = extract_frame(cap, frame_index)
        write_compressed_frame(frame)

        post_text = make_post_text(episode, frame_index, total_frames)
        with open(TEMP_IMG, "rb") as image_file:
            image_data = image_file.read()

        posted = False
        for attempt in range(1, BLUESKY_POST_ATTEMPTS + 1):
            try:
                client = get_authenticated_client()
                client.send_image(
                    text=post_text,
                    image=image_data,
                    image_alt=(
                        f"{SERIES_TITLE}, season {episode['season']}, "
                        f"episode {episode['episode']}, frame {frame_index + 1:,}"
                    ),
                )
                posted = True
                print(
                    "🎯 SUCCESS: Posted random frame "
                    f"{frame_index + 1:,} from "
                    f"S{episode['season']:02d}E{episode['episode']:02d}."
                )
                break
            except AtProtocolError as exc:
                print(
                    f"⚠️ Bluesky API error (attempt {attempt}/"
                    f"{BLUESKY_POST_ATTEMPTS}): {exc}"
                )
                if attempt == BLUESKY_POST_ATTEMPTS:
                    raise
                wait_seconds = 15 * attempt
                print(f"⏳ Waiting {wait_seconds} seconds before retrying...")
                time.sleep(wait_seconds)

        if not posted:
            raise RuntimeError("Bluesky post did not complete successfully.")

        history.append(
            {
                "key": selection_key,
                "season": episode["season"],
                "episode": episode["episode"],
                "filename": episode["filename"],
                "frame_index": frame_index,
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        save_history(history)
        print(f"💾 Saved recent-post history to {HISTORY_FILE}.")

    finally:
        if cap is not None:
            cap.release()
        if os.path.exists(TEMP_IMG):
            try:
                os.remove(TEMP_IMG)
            except OSError as exc:
                print(f"⚠️ Could not remove temporary image: {exc}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"❌ ERROR: {exc}")
        sys.exit(1)
