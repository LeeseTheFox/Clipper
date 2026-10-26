# Clipper danger patterns

These patterns can lose clips or edits, interrupt recording, misidentify games,
or leave background work running after its owner closes. Apply the guidance to
code in scope. The examples illustrate mistakes to avoid, not additional APIs
or a requirement to refactor every similar-looking function.

## Silent exceptions and misleading fallback values

An unavailable source is different from a successful discovery with no sources.
An unknown process-monitor state is different from a confirmed empty snapshot.
Do not turn errors into ordinary empty results that can clear selections, stop
recording or permanently cache unsupported capabilities.

For example, this hides a failed audio query as an empty device list:

```python
try:
    return discover_audio_sources()
except Exception:
    return []
```

Let the error reach an owner that can preserve usable state, recover, or provide
diagnostics and feedback. Catch specific expected exceptions when possible.
Broad catches are appropriate at worker or callback boundaries only when the
failure is reported and the operation's ownership is settled.

A missing optional thumbnail or disposable cache can legitimately fall back.
An explicit migration may recover usable project data from invalid undo history.
Keep such recovery deliberate and explain the non-obvious invariant. A fallback
must preserve the operation's contract, not merely prevent an exception.

Route diagnostics through the component logging namespace described in
[application diagnostics](../../docs/logging.md). Do not dump configuration,
portal tokens or environment variables while adding error context.

## Generic logic that loses platform or capture identity

Do not reduce process identity to an executable basename. Two games can have the
same filename, Proton/Wine processes can need launcher ancestry, and native and
sandboxed installations can expose different paths and Steam accounts.

Likewise, display capture and game capture have different startup, authorization
and frame-readiness requirements. Audio application selectors and user-facing
labels are different kinds of identity. Shared helpers must preserve those
distinctions and the ownership of user-managed Steam launch options.

Read the existing dispatch and normalization code before extracting a common
path. Extend the existing implementation when it fits. Keep distinct supported
platform cases in regression tests when they protect different production paths.

## Defaults that conceal broken invariants

Validate user input, deserialized configuration and drafts, media probes, IPC
messages and network responses at their boundaries. Reject unsupported data or
apply an intentional compatibility migration there. Do not silently manufacture
an apparently valid object from malformed external data.

For example, a missing or invalid monitor snapshot must not become a confirmed
empty snapshot via `message.get("rules", [])`. See `validate_monitor_event` in
`ui/monitor_ipc.py` for the actual boundary validation.

Once a boundary establishes a required invariant, internal code should use it.
Adding `getattr`, empty dictionaries or repeated `None` checks everywhere can
hide a missing initialization or incorrect callback order. Fix the producer or
the lifecycle instead. A genuine optional value still needs an explicit path.

## Control flow that hides state transitions

Prefer guard clauses for unsupported inputs, retired requests and completed
operations. Avoid an `else` after an unconditional `return`, `raise`, `continue`
or `break`. Keep the normal operation readable without tracing several nested
success conditions.

Flattening is useful only when it preserves behavior. Keep cleanup in
`finally` or the relevant context manager; an early return must not skip release
of an engine lease, stream, temporary file or GLib source. Preserve explicit
state-machine branches when they express distinct lifecycle transitions.

## Speculative abstractions and duplicate implementations

Do not introduce a new manager, generic backend, mode or option for a use case
the task does not need. Extract helpers for actual duplication, a real domain
concept or a boundary, including a single-use helper when it isolates one of
those boundaries. A class or helper is not justified by its name alone.

Check existing controls, discovery services, process matching and persistence
code before adding siblings. An abstraction must make its inputs, outputs and
resource ownership clearer. Avoid wrappers that only forward arguments and
configuration switches that introduce unneeded behavior combinations.

## Asynchronous work without an owner

Dispatching a callback to GLib does not establish that its window, connection or
request is still current. A late result can overwrite newer state or restart
work during shutdown even when every widget access runs on the GTK thread.

Reject retired results before applying them. For a generation-based request,
the callback needs the request's captured generation, not the current value
looked up when the worker finishes:

```python
if self._closed or generation != self._generation:
    return
```

Use the existing ownership mechanism rather than adding a second one. See
`RecordingCapabilityProbe` in `ui/capability_response.py` for request retirement,
subscriber removal and balanced engine acquisition/release.

Keep blocking discovery, media probing and subprocess waits off the GTK loop.
Bound external work and retries. On close, cancel or retire pending work, remove
timers and subscriptions, and release only resources the operation acquired.
Ensure completion and failure callbacks settle an operation once, including
cancellation and reconnect. Starting a worker thread does not replace teardown.

Follow the [engine IPC contract](../../engine/README.md#ipc) when handling replies
and save completion. Follow the [update restart contract](../../docs/auto-updates.md#installation-and-restart)
when a callback can replace the running application or interrupt recording.

## Destructive cleanup and premature publication

An operation may clean up only the artifacts it created or explicitly owns.
Never infer a recursive deletion target from a source file's parent directory.
Preserve user clips, saved edits, unrelated Steam options and the last working
capture payload when an operation fails. Treat disposable cache cleanup and
user-data deletion as different failure contracts.

For example, a failed export must clean its unique partial file rather than the
destination, which may still hold a valid earlier export:

```python
# Unsafe failure cleanup after overwrite was requested.
destination.unlink(missing_ok=True)

# Clean only the temporary artifact owned by this export.
partial_path.unlink(missing_ok=True)
```

Use the existing staging and atomic replacement paths. Validate a completed
export or capture payload before activation, and retain the working destination
until replacement is ready. Atomic replacement prevents partial reads; it does
not coordinate competing writers. Preserve the ownership rules for shared
configuration in [the engine documentation](../../engine/README.md#shared-configuration).

Check failed preparation, failed publication and cancellation as well as the
happy path. Exercise actual filesystem outcomes or production callbacks in
tests; source-text assertions do not prove safe cleanup or callback ordering.
