# HCS Tab Architecture

Top-level HCS tabs are feature modules composed by the HCS shell through
`hcs_ai.core.tabs.TabRegistry`.

## Boundary rules

1. **A tab owns presentation and tab-specific workflow.**
   New tab UI code should live under `hcs_ai/tabs/<feature>/`.

2. **Shared capabilities are services, not tabs.**
   Email/messaging, inference, credentials, HKR access, MCP, storage, logging,
   Tornado routing, and similar reusable capabilities should be registered in
   `ServiceContainer` and consumed through that container.

3. **Tabs do not import other tabs.**
   Cross-feature communication goes through shared services or an event/message
   interface. This keeps one tab independently replaceable and testable.

4. **The shell owns composition and ordering.**
   The HCS shell decides which `TabDefinition` objects are enabled and their
   top-level order. It should not contain feature implementation details.

5. **Migration is incremental.**
   Existing `App.build_*` methods may use `method_tab(...)` as a compatibility
   adapter. Move each tab into its feature package independently instead of
   rewriting the whole GUI at once.

## Feature package target

A mature feature can grow toward:

```text
hcs_ai/tabs/<feature>/
    __init__.py
    tab.py          # TabDefinition / composition contract
    view.py         # Tk widgets and presentation
    controller.py   # tab-specific UI workflow
    models.py       # tab-local view/state models
```

Runtime code that is useful outside that feature should move to an HCS service
instead of being placed in the tab package.
