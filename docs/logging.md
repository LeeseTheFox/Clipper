# Application logs

**Copy logs** includes engine output and Python messages from the `clipper`
logging namespace. Use a component logger such as
`logging.getLogger("clipper.editor.export")` to include messages from worker
threads. Write diagnostics in English; translate user-facing dialogs separately.

## What to log

Log when operations start and finish, which settings they use, why they fall
back, and how they fail. Avoid per-frame messages, audio samples, seeks during
dragging, and routine progress updates.

- Hardware probe rejections are informational. They explain why an encoder is
  unavailable without suggesting that an export failed. Probe summaries report
  driver causes before cascading FFmpeg cleanup errors.
- Identical one-frame Opus closing notices are counted together. Other codec
  warnings are kept.
- Audio route messages describe configured sources, not measured signal activity.
- Engine shutdown requests include the application action that triggered them.

Export logs include the source, options, duration, segment count, and audio
track count, followed by encoding, validation, and the result. Failures include
a bounded FFmpeg log tail, exit code, elapsed time, and last progress update.
Preview errors include the GStreamer element path and debug details to help
locate audio, decoding, or video output failures.

## History and sensitive data

The log window and **Copy logs** retain the latest 2,000 lines. Consecutive
identical single-line messages share an entry with a count and the first and
latest occurrence times. A repeat after another message gets a separate entry.
Terminal output prints every occurrence.

Logs may contain media paths and audio application selectors. Never log raw
configuration, portal restore tokens, or environment dumps.
