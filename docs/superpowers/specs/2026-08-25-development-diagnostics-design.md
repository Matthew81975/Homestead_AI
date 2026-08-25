# HCS Development Diagnostics Design

## Purpose

Add a development-only observability layer to HCS-AI so failures can be diagnosed from exact runtime evidence instead of screenshots and generic dialogs. The feature must remain unobtrusive during normal use and be switchable at runtime.

## Architecture

HCS will gain one shared diagnostics service that accepts structured events from all subsystems. The service is UI-agnostic and owns in-memory event retention, session log persistence, redaction, export, and the current telemetry snapshot used by the GUI status strip. GUI components subscribe/poll the service rather than implementing their own logging.

The first deeply instrumented paths are local LLM inference, model load/switch HTTP calls, and Knowledge Base ingest/classification. Other subsystems can adopt the same interface incrementally.

## Structured event model

Each event stores:
- timestamp
- severity: DEBUG, INFO, WARNING, ERROR
- subsystem
- operation
- message
- elapsed_seconds when applicable
- optional model name
- optional prompt/output token counts and rates
- optional HTTP method, endpoint, and status code
- optional structured context dictionary
- optional diagnostic payload dictionary
- optional exception/traceback text

Diagnostic payloads are separated from ordinary context so normal exports can exclude prompts, responses, paths, and other verbose/sensitive debugging content.

## Development Diagnostics mode

A global Development Diagnostics toggle defaults to off. When enabled, HCS may show additional runtime information without changing core behavior.

The persistent diagnostics/status strip should show, where available:
- Ready/Busy state
- backend state and port
- active model
- last response total duration
- prompt-processing duration/rate
- output tokens/sec
- most recent HTTP status
- active subsystem/operation
- last error summary
- an obvious indicator when diagnostic payload capture is enabled

Contextual developer information may also appear inside tabs. Knowledge Base should expose ingest/classifier stage and the Models tab should expose model-load request/result information. Diagnostics can be toggled without restarting HCS.

## Log tab

Add a top-level Log tab containing a scrollable structured log view. Controls:
- Development Diagnostics on/off
- Diagnostic payload capture on/off
- severity filter
- subsystem filter
- text search
- auto-scroll
- Clear View/Session events
- Copy Selected
- Copy Visible
- Export

The Log tab must remain usable even when Development Diagnostics mode is off; ordinary INFO/WARNING/ERROR events are still recorded. DEBUG/detail events and diagnostic UI are controlled by the development toggle.

## Persistence and rotation

Write session logs to an HCS data/logs directory in JSONL form. Keep approximately the ten most recent session log files and remove older session files at startup. Logging failures must never crash HCS.

## Export

Export either all current-session events or currently filtered events. Supported formats:
- .log or .txt: human-readable text
- .jsonl: one structured JSON object per event

A separate Include diagnostic payloads option defaults off. Without it, diagnostic_payload is omitted/redacted. Export is initiated with a standard save dialog.

## LLM telemetry

Reuse and extend existing per-model telemetry. One timing source should feed the Models tab, status strip, and logs. Record prompt processing and generation timing/rates when the backend provides them; always record total request duration.

## HTTP error handling

HTTP failures must retain and expose the backend response body in diagnostics. User-facing dialogs should include a concise reason derived from that body instead of only showing `HTTP Error 400: Bad Request`. Full body/request context belongs in the Log tab when diagnostics are enabled.

## Knowledge Base diagnostics

For each imported artifact, record:
- artifact name/path in diagnostic payload
- chunk count
- classification start/end
- classifier prompt size
- raw classifier response in diagnostic payload
- parsed classification result
- selected/created Knowledge Tree path
- elapsed time
- exception traceback on failure

Classification failure dialogs should display the concise failure reason and provide a View Log action that selects or surfaces the corresponding event.

## Safety and normal-user behavior

Development Diagnostics defaults off. Normal UI remains clean. Diagnostic payload capture defaults off independently. The diagnostics subsystem must not expose credentials or secrets; common authorization/token fields are redacted before persistence/export.

## Testing

Add unit tests for structured event creation, filtering, redaction, export serialization, session rotation, telemetry snapshot updates, HTTP error-body extraction, and Knowledge Base classification failure logging. Add GUI-facing tests where practical for Log tab/status formatting without requiring a live model.
