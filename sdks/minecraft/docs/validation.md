# Alpha 3 validation — 2026-11-13

- Java 21 / Gradle 8.10.2 full build: passed. Unit tests cover chat model route, model output validation, bounded movement, stale-request rejection, and normal adapter behavior.
- Python tests: 52 passed, including an opt-in SBP gateway on the existing HTTPS server, the shared model provider, the SBP action lifecycle, and existing server startup regressions.
- Real local HTTPS integration: Java adapter to Python backend using a fake model; chat response, movement planning, action execution and observations passed.
- Fabric mod and player distribution are built separately from the existing alpha 1 and alpha 2 releases. Alpha 3 accepts a single fenced JSON response after the same strict coordinate checks; this was necessary for the selected local model.

The real selected local model returned a fenced coordinate object via the actual running SBP server; the alpha 3 parser accepts that format. In-game chat passed with the selected local model: `/superbot chat Hello!` displayed “Greetings!”, confirmed by the completed backend task. AI-directed movement was tested in a live Minecraft world: the user saw the NPC move, and the backend recorded a completed `minecraft.move_to` action with `navigation_started: true`. Arrival is not tracked or guaranteed. The mod does not implement continuous following, persistent chat history, or autonomous survival.

A separate existing manager test currently fails when the Minecraft bridge is disabled: the working-tree manager no longer exports the selected model settings in that mode. All four manager tests pass with the Minecraft bridge enabled; this change preserves the existing non-bridge behavior.
