"""S1-S7 stage logic (spec §5-§7). Each stage is a pure, independently-
testable function: pydantic artifact in -> pydantic artifact out. Disk IO is
centralized in tessera.artifacts so --from/--to reruns are real (§3.6)."""
