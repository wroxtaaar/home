# NexStream experimental branch

This branch adds an isolated test page at `/nexstream-test.html` and a server-side resolver endpoint. It does not change the existing Seedr/torrent player or movie browsing UI.

## Configure the API key

Set `NEXSTREAM_API_KEY` as an environment variable for the test container. Do not commit the key to Git or put it in frontend code. The API key previously used for testing should be rotated before relying on it.

## Test

1. Build and run this branch in a separate container/port; do not replace the production Home container.
2. Open `http://YOUR_VPS_IP:TEST_PORT/nexstream-test.html`.
3. Test the default movie (TMDB ID 597), then select TV and test TMDB ID 1399, season 1, episode 1.
4. Check playback, seeking, fullscreen, mobile behavior, subtitles, audio selection and buffering.
5. Confirm the provider's content rights and terms before any broader use.

The backend returns only HTTPS EmbedMaster player URLs on an explicit host allowlist. The API key remains on the server. NexStream's API response is a third-party iframe URL, not a direct media stream.
