# Daily Investment Podcast transcripts

Runs every 3 hours on GitHub Actions. No manual steps.

Shows: All-In, Forward Guidance (Blockworks), Systematic Investor (Top Traders Unplugged, "SI###" titles only).

- `status/latest.md`: newest episode per show, status, link, transcript result.
- `transcripts/`: one .md per episode, saved ONLY if the transcript covers the full episode.
- Issues: opened automatically when a transcript fails after retries or a feed looks stale.

How it decides
1. Latest episode = official RSS feed (found via Apple Podcasts lookup). No cached web pages.
2. Transcript order: RSS transcript tag > official episode page > YouTube captions > Whisper on official audio (after 12h).
3. Completeness check: last transcript timestamp must reach the episode length (5 min tolerance).

Manual run: Actions tab > podcast-transcripts > Run workflow.
Override a feed: set env FEED_ALL_IN, FEED_FORWARD_GUIDANCE, or FEED_SYSTEMATIC_INVESTOR.
