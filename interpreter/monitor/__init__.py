"""Case Monitor (FR-52): one queue showing whose turn it is on every open case.

Two feeds share the queue: cases the AI agents handed to a human, and cases
waiting on engineering or a third party in the connected case system.

Pure logic: links, redact, verdict, timeline, brief, similar, drafts.
I/O: config, readers, store, sync, service.
"""
