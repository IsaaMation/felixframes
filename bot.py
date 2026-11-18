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
