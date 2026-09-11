# Application diagnostics

The application's “Copy logs” output includes engine output and Python
diagnostics under the `clipper` logging namespace. Use a component logger
(for example, `logging.getLogger("clipper.editor.export")`) so diagnostics
reach the application log even from worker threads. Diagnostic messages use
stable English; translated dialogs remain separate.

Log operation boundaries, selected settings, fallback decisions, and failures.
Avoid logging frames, audio samples, seeks during dragging, or routine progress
updates. Expected hardware probe rejections are informational: they explain why
an encoder is unavailable, without implying that a user export failed.
Probe summaries prefer driver causes over cascading FFmpeg cleanup messages.
Identical one-frame Opus closing notices are counted together; other codec
warnings remain intact. Audio routes describe configuration, not measured
signal activity. Engine shutdown requests include their application trigger.

An export records its source, options, duration, segment and audio track counts,
then encoding, validation, and completion or failure. Failures retain a bounded
FFmpeg diagnostic tail, exit code, elapsed time, and last progress. Preview
pipeline errors include the GStreamer element path and debug detail to identify
audio, decoding, or video output failures.

The live/copyable log retains the latest 2,000 lines. Consecutive identical
single-line messages share an entry with an occurrence count and first/latest
times; recurrence after another event remains a separate entry. Terminal output
still prints each occurrence. Logs can include media paths and audio application
selectors, but should never include raw configuration, portal restore tokens,
or environment dumps.
