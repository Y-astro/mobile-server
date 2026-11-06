# Antra music downloads: proposed design

Status: approved by the user with "continue" on 2026-11-06. Research is complete; implementation and deployment are authorized within this design.

## Goal

An administrator pastes a supported song or playlist link into a small page matching the current dark Material 3 website. A phone-side worker fetches and saves music after the local browser closes. Only verified 24-bit FLAC files enter the finished library. Show completed, skipped, failed, and waiting tracks explicitly.

## Evidence and corrected assumptions

Antra's live frontend v108 explicitly chunks ZIP downloads into groups of 100 because its server must hold source files and ZIP output together. Its folder mode downloads each completed track and acknowledges it after writing, allowing Antra to reclaim its copy. That mode removes the 100-track UI cap. This is separate from the advertised free account limits of ten albums, five playlists, and 100 singles per day.

Antra exposes login/register, resolution, job submission, status/WebSocket, per-track download, and acknowledgement endpoints. A signed-in account is required; the research worker's claim that anonymous access suffices was incorrect. The public repository is primarily the desktop application and does not establish the hosted backend implementation.

The queue frontend assumes artifacts expire ten minutes after job creation. Download promptly; do not wait for a completed large ZIP. Expiry behavior for long folder-mode jobs still needs a representative live test.

The phone's ordinary Python HTTPS request failed DNS resolution before making an HTTP request. A read-only test using the existing, hash-pinned Suwayomi DNS library in a separate Python process reached the homepage (200) and the unauthenticated account endpoint (401). No service configuration was changed. No CAPTCHA requirement was observed in this test; account creation and authenticated downloads remain untested.

Public source snapshots reviewed:
- https://antra.hoshi.cfd/static/app.js?v=108 — SHA256 a56336fd4cfa39b1221e5396ece0de1dc038fb4874f65f37cd530466c3f4b6d8
- https://antra.hoshi.cfd/static/queue.js?v=108 — SHA256 b3ab2ca524f0579a6bedf20124e52426014da30f63deb3895d48bcb0d10abab5
- https://antra.hoshi.cfd/static/fswriter.js?v=108 — SHA256 d1d000fa6d2496a05cdb3fc359c93ca958824eec91a7ed89cdb547d75bb647d5
- Linked repository reviewed at anandprtp/Antra commit 6060c3cf1e65282487b71022093c41563bba6721.

## Recommended approach

Use a lightweight Python API worker, rather than a persistent browser. Reuse existing Python, portal authentication, shared UI styles, supervisor, and reviewed release infrastructure. Keep one active Antra job initially, matching its free queue behavior.

Use one dedicated Antra account with a simple, unique generated passphrase. Store credentials and session cookies only in a private phone file with mode 0600, outside source and release bundles. Additional accounts are not needed merely to continue past track 100. If a real daily limit is reached, retain pending work and resume after the quota resets. This account-policy correction needs user approval because the original request proposed new accounts per 100 tracks.

Prefer Antra's streaming folder mode and save tracks as they finish, acknowledging only after durable local handling. Retain complete playlist indexing and local per-track progress. If live retention constraints require bounded batches, continue through their remaining index ranges automatically using the same account. Never mark a playlist complete merely because its first batch finished.

Alternative: a persistent server browser could automate the website, but adds memory use, browser installation, download-dialog handling, and UI-selector maintenance. It is a fallback only if authenticated API access proves unsuitable.

## Service and data ownership

Add one independently supervised download worker and one administrator page. The existing dashboard owns authenticated job/status requests; the worker owns outbound Antra requests and file writes. No new publicly exposed port is needed.

Keep the queue and track state in a private SQLite database, using atomic state transitions and a single-worker lock. Persist upstream job identifiers before tracking/downloading. Browser closure has no effect. Worker or phone restart resumes known work and reconciles interrupted local files.

A timed-out submission with no confirmed upstream job identifier enters an explicit uncertain state. Do not blindly resubmit and consume quota twice. Recover known jobs where the upstream API permits it; otherwise show the required manual decision.

Proposed output directory: /storage/emulated/0/Music/Antra, subject to a read/write/free-space preflight. Preserve existing music. Use stable track identifiers to avoid duplicate output; reject remote path traversal and symlink escapes. Stream to temporary files and atomically publish verified output. Pause safely when storage is insufficient.

Load the existing pinned DNS library only for the new worker process if still required. Do not alter global DNS settings or Suwayomi.

## Strict quality behavior

Always request flac24. Verify actual downloaded FLAC STREAMINFO bit depth and sample rate before publishing; do not trust a format label, extension, or requested setting alone. Accept 24-bit FLAC with its original sample rate. Show that rate in the UI. Do not re-encode, upsample, or silently accept 16-bit FLAC, ALAC, AAC, or MP3. Report unavailable quality as skipped.

This proves the file's encoded format, not whether the upstream provider's master was originally recorded at that quality.

## UI and scope

Provide link input, Add to queue, current progress, per-track outcomes, retry for failed work, and cancel. Keep the page administrator-only. Files remain accessible through existing cloud storage permissions. Appearance controls remain administrator-only.

No player, music streaming server, scheduled playlist mirroring, alternate downloader providers, or changes to held YouTube/media/torrent work.

## Deployment and acceptance

The current runtime has 16 managed services. Its general activation profile intentionally refuses catalog additions. Extend the reviewed lifecycle mechanism narrowly for this one worker addition; verify preservation of all prior service definitions and disabled intent. Do not weaken that check or use the obsolete 15-service operations snapshot.

Implement in maintained source through bounded AGY High owners. Verify exact API fixtures, malformed paths, strict quality rejection, queue persistence, uncertain submissions, quota behavior, and an actual role-template browser flow. Exercise worker termination/restart and browser closure using a representative playlist with more than 100 tracks, supplied by the user.

Before activation, verify complete source ownership, manifest hashes, recovery assets, and service mapping. Preserve the Suwayomi JAR, DNS files, account scopes, and disabled-service flags. Retain rollback separately from downloaded music and queue data.

Unverified: real account registration/login, authenticated job protocol, long-playlist retention behavior, and actual audio retrieval. No account or music download has been created during research.
